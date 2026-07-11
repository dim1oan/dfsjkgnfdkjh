"""
Автоаренда VPS в HostVDS через OpenStack API + автоустановка 3x-ui.

Как это работает:
    1. Аутентификация в Keystone (получаем токен и каталог сервисов).
    2. Создание сервера (Nova) с cloud-init скриптом, который ставит
       Docker + 3x-ui и задаёт логин/пароль панели.
    3. Ожидание статуса ACTIVE и получение публичного IP.
    4. Ожидание, пока панель 3x-ui поднимется, затем создание
       VLESS/Reality-инбаунда через её API.
    5. Сервер сохраняется в БД и становится доступен для продажи.

ВАЖНО: провижининг занимает 3–7 минут, поэтому вызывается только
администратором (/add_server), а не клиентами при покупке.

Названия зон доступности (locations) HostVDS уточните в панели или через
GET /v2.1/os-availability-zone — ниже заготовка карты стран.
"""

import asyncio
import base64
import logging
import re
from dataclasses import dataclass
from urllib.parse import urlsplit, urlunsplit

import aiohttp

from config import settings
from xui_client import XUIClient

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    """Ошибка автоаренды сервера."""


def _strip_html(text: str) -> str:
    """Убирает HTML-теги из ответа сервера (например, страниц ошибок nginx),
    чтобы текст можно было безопасно отправить в Telegram."""
    return re.sub(r"<[^>]+>", " ", text).strip()


# ── Карта стран → зоны/регионы HostVDS ───────────────────────────────────────
# Сверьте значения availability_zone со своей панелью HostVDS
# (или получите список: GET {compute}/os-availability-zone).
COUNTRIES: dict[str, dict] = {
    "nl": {"name": "Нидерланды", "flag": "🇳🇱", "az": "Amsterdam"},
    "fr": {"name": "Франция", "flag": "🇫🇷", "az": "Paris"},
    "us": {"name": "США", "flag": "🇺🇸", "az": "Dallas"},
    "hk": {"name": "Гонконг", "flag": "🇭🇰", "az": "Hong Kong"},
    "kz": {"name": "Казахстан", "flag": "🇰🇿", "az": "Almaty"},
}


@dataclass
class ProvisionedServer:
    """Результат провижининга."""

    ip: str
    panel_url: str
    panel_username: str
    panel_password: str
    inbound_id: int
    vless_port: int
    public_key: str
    sni: str
    short_id: str


def _cloud_init(panel_port: int, panel_user: str, panel_pass: str) -> str:
    """
    cloud-init: Docker + 3x-ui + фиксированные логин/пароль панели.

    Панель поднимается на http://IP:{panel_port} без basePath.
    """
    return f"""#cloud-config
runcmd:
  - curl -fsSL https://get.docker.com | sh
  - mkdir -p /opt/3x-ui/db /opt/3x-ui/cert
  - >
    docker run -d --name 3x-ui --restart unless-stopped --network host
    -v /opt/3x-ui/db:/etc/x-ui -v /opt/3x-ui/cert:/root/cert
    ghcr.io/mhsanaei/3x-ui:latest
  - sleep 20
  - docker exec 3x-ui /app/x-ui setting -username {panel_user} -password {panel_pass} -port {panel_port} -webBasePath /
  - docker restart 3x-ui
"""


class HostVDSClient:
    """Минимальный OpenStack-клиент (Keystone + Nova) через aiohttp."""

    def __init__(self) -> None:
        if not settings.hostvds_auth_url:
            raise ProvisioningError(
                "Не задан HOSTVDS_AUTH_URL в .env.\n"
                "Скачайте файл openrc.sh в панели HostVDS "
                "(раздел API → «OpenStack CLI клиент»), найдите в нём строку "
                "export OS_AUTH_URL=... и скопируйте её значение:\n"
                "HOSTVDS_AUTH_URL=https://<адрес-из-openrc>/v3"
            )
        if not (
            settings.hostvds_username
            and settings.hostvds_password
            and settings.hostvds_project_name
        ):
            raise ProvisioningError(
                "Не заданы HOSTVDS_USERNAME / HOSTVDS_PASSWORD / "
                "HOSTVDS_PROJECT_NAME в .env"
            )
        # Нормализуем URL: убираем хвостовой слэш, гарантируем /v3 на конце.
        auth_url = settings.hostvds_auth_url.rstrip("/")
        if not auth_url.endswith("/v3"):
            auth_url += "/v3"
        self._auth_url = auth_url
        self._token: str | None = None
        self._compute_url: str | None = None
        self._network_url: str | None = None

    async def _authenticate(self, session: aiohttp.ClientSession) -> None:
        """Keystone: получаем токен и endpoint сервиса compute."""
        payload = {
            "auth": {
                "identity": {
                    "methods": ["password"],
                    "password": {
                        "user": {
                            "name": settings.hostvds_username,
                            "domain": {"name": settings.hostvds_domain_name},
                            "password": settings.hostvds_password,
                        }
                    },
                },
                "scope": {
                    "project": {
                        "name": settings.hostvds_project_name,
                        "domain": {"name": settings.hostvds_domain_name},
                    }
                },
            }
        }
        # Кандидаты URL: сначала как задано в .env; если порт не указан,
        # дополнительно пробуем стандартный порт Keystone :5000
        # (частая причина 405: URL указывает на веб-сайт, а не на API).
        candidates = [self._auth_url]
        parsed = urlsplit(self._auth_url)
        if parsed.port is None:
            with_port = parsed._replace(
                netloc=f"{parsed.hostname}:5000"
            )
            candidates.append(urlunsplit(with_port))

        body: dict | None = None
        errors: list[str] = []
        for auth_url in candidates:
            try:
                async with session.post(
                    f"{auth_url}/auth/tokens", json=payload
                ) as resp:
                    if resp.status in (200, 201):
                        self._token = resp.headers["X-Subject-Token"]
                        body = await resp.json()
                        self._auth_url = auth_url
                        break
                    text = _strip_html(await resp.text())[:300]
                    errors.append(f"{auth_url} → HTTP {resp.status}: {text}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                errors.append(f"{auth_url} → {exc}")

        if body is None:
            raise ProvisioningError(
                "Keystone auth не удался. Пробовал:\n"
                + "\n".join(f"  • {e}" for e in errors)
                + "\n\nЕсли видите «405 Not Allowed» — HOSTVDS_AUTH_URL "
                "указывает на веб-сайт, а не на API. Возьмите точное значение "
                "OS_AUTH_URL из файла openrc.sh (панель HostVDS → API). "
                "Если «401» — проверьте HOSTVDS_USERNAME / HOSTVDS_PASSWORD "
                "(нужен API-пароль из панели, не пароль от аккаунта)."
            )

        wanted_interface = (settings.hostvds_interface or "public").lower()
        wanted_region = settings.hostvds_region_name
        for svc in body["token"]["catalog"]:
            if svc["type"] not in ("compute", "network"):
                continue
            for ep in svc["endpoints"]:
                if ep["interface"] != wanted_interface:
                    continue
                if wanted_region and ep.get("region") != wanted_region:
                    continue
                if svc["type"] == "compute":
                    self._compute_url = ep["url"]
                else:
                    self._network_url = ep["url"].rstrip("/")
                break
        if not self._compute_url:
            raise ProvisioningError(
                "Compute endpoint не найден в каталоге "
                f"(interface={wanted_interface}, region={wanted_region or 'любой'}). "
                "Проверьте HOSTVDS_REGION_NAME / HOSTVDS_INTERFACE в .env."
            )

    def _headers(self) -> dict:
        return {"X-Auth-Token": self._token or ""}

    async def _find_id(
        self, session: aiohttp.ClientSession, path: str, key: str, name: str
    ) -> str:
        """Ищет ID flavor/image по имени.

        Сначала точное совпадение (без учёта регистра), затем «мягкое»:
        пробелы/дефисы/подчёркивания считаются одинаковыми, суффикс -amd64
        не обязателен. Например, «Ubuntu 24.04» найдёт «Ubuntu-24.04-amd64».
        """
        async with session.get(
            f"{self._compute_url}{path}", headers=self._headers()
        ) as resp:
            data = await resp.json()
        items = data.get(key, [])

        # 1. Точное совпадение без учёта регистра
        for item in items:
            if item["name"].lower() == name.lower():
                return item["id"]

        # 2. Мягкое совпадение: нормализуем разделители и суффикс -amd64.
        #    Образы с префиксом OLD_ пропускаем — это устаревшие версии.
        def norm(s: str) -> str:
            s = s.lower()
            s = re.sub(r"[-_\s]+", "-", s)
            s = re.sub(r"-amd64$", "", s)
            return s

        target = norm(name)
        for item in items:
            if item["name"].startswith("OLD_"):
                continue
            if norm(item["name"]) == target:
                logger.info(
                    "%s: «%s» сопоставлен с «%s»", key, name, item["name"]
                )
                return item["id"]

        # Не найден — показываем только актуальные (не OLD_) варианты
        actual = [i["name"] for i in items if not i["name"].startswith("OLD_")]
        raise ProvisioningError(
            f"{key}: «{name}» не найден. Доступны: " + ", ".join(actual)
        )

    async def _find_network_id(self, session: aiohttp.ClientSession) -> str:
        """Возвращает UUID сети для подключения сервера.

        Сначала пробуем Nova-прокси /os-networks; если он недоступен —
        Neutron (сервис network из каталога Keystone). Предпочитаем сеть
        с именем, содержащим public/ext/internet; иначе берём первую.
        """
        def pick(nets: list[dict]) -> str | None:
            if not nets:
                return None
            for net in nets:
                label = (net.get("label") or net.get("name") or "").lower()
                if any(w in label for w in ("public", "ext", "internet")):
                    return net["id"]
            return nets[0]["id"]

        # 1. Nova: GET /os-networks
        try:
            async with session.get(
                f"{self._compute_url}/os-networks", headers=self._headers()
            ) as resp:
                if resp.status == 200:
                    net_id = pick((await resp.json()).get("networks", []))
                    if net_id:
                        return net_id
        except (aiohttp.ClientError, asyncio.TimeoutError):
            pass

        # 2. Neutron: GET /v2.0/networks
        if self._network_url:
            try:
                async with session.get(
                    f"{self._network_url}/v2.0/networks",
                    headers=self._headers(),
                ) as resp:
                    if resp.status == 200:
                        net_id = pick((await resp.json()).get("networks", []))
                        if net_id:
                            return net_id
            except (aiohttp.ClientError, asyncio.TimeoutError):
                pass

        raise ProvisioningError(
            "Не удалось найти сеть для сервера: провайдер отклонил "
            "networks='auto', а список сетей получить не получилось."
        )

    async def create_server(
        self, country_code: str, panel_user: str, panel_pass: str
    ) -> str:
        """
        Создаёт VPS в нужной стране, ждёт ACTIVE, возвращает публичный IP.
        """
        country = COUNTRIES.get(country_code)
        if country is None:
            raise ProvisioningError(f"Неизвестная страна: {country_code}")

        user_data = base64.b64encode(
            _cloud_init(settings.xui_panel_port, panel_user, panel_pass).encode()
        ).decode()

        async with aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=60)
        ) as session:
            await self._authenticate(session)

            flavor_id = await self._find_id(
                session, "/flavors", "flavors", settings.hostvds_flavor
            )
            image_id = await self._find_id(
                session, "/images", "images", settings.hostvds_image
            )

            payload = {
                "server": {
                    "name": f"vpn-{country_code}",
                    "flavorRef": flavor_id,
                    "imageRef": image_id,
                    "availability_zone": country["az"],
                    "user_data": user_data,
                    "networks": "auto",
                }
            }
            # networks="auto" требует микроверсию Nova API >= 2.37,
            # поэтому передаём соответствующий заголовок.
            create_headers = {
                **self._headers(),
                "X-OpenStack-Nova-API-Version": "2.37",
            }
            async with session.post(
                f"{self._compute_url}/servers",
                json=payload,
                headers=create_headers,
            ) as resp:
                if resp.status in (200, 202):
                    server_id = (await resp.json())["server"]["id"]
                elif resp.status == 400:
                    # Микроверсия не поддерживается — выбираем сеть по UUID.
                    err_text = await resp.text()
                    logger.warning(
                        "networks='auto' отклонён (%s), пробую явный UUID сети",
                        err_text[:200],
                    )
                    net_id = await self._find_network_id(session)
                    payload["server"]["networks"] = [{"uuid": net_id}]
                    async with session.post(
                        f"{self._compute_url}/servers",
                        json=payload,
                        headers=self._headers(),
                    ) as resp2:
                        if resp2.status not in (200, 202):
                            raise ProvisioningError(
                                f"Создание сервера отклонено ({resp2.status}): "
                                f"{await resp2.text()}"
                            )
                        server_id = (await resp2.json())["server"]["id"]
                else:
                    raise ProvisioningError(
                        f"Создание сервера отклонено ({resp.status}): "
                        f"{await resp.text()}"
                    )
            logger.info("Сервер %s создаётся (id=%s)…", country_code, server_id)

            # Ждём ACTIVE (до ~5 минут)
            for _ in range(60):
                await asyncio.sleep(5)
                async with session.get(
                    f"{self._compute_url}/servers/{server_id}",
                    headers=self._headers(),
                ) as resp:
                    info = (await resp.json())["server"]
                if info["status"] == "ACTIVE":
                    ip = _extract_public_ip(info)
                    logger.info("Сервер ACTIVE, IP=%s", ip)
                    return ip
                if info["status"] == "ERROR":
                    raise ProvisioningError(f"Сервер в статусе ERROR: {info}")

        raise ProvisioningError("Таймаут ожидания статуса ACTIVE")


def _extract_public_ip(server_info: dict) -> str:
    """Достаёт публичный IPv4 из ответа Nova."""
    for network in server_info.get("addresses", {}).values():
        for addr in network:
            if addr.get("version") == 4:
                return addr["addr"]
    raise ProvisioningError("Публичный IPv4 не найден у сервера")


async def provision_server(country_code: str) -> ProvisionedServer:
    """
    Полный цикл: аренда VPS → ожидание панели → создание Reality-инбаунда.
    """
    panel_user = settings.xui_panel_username
    panel_pass = settings.xui_panel_password
    if not panel_pass:
        raise ProvisioningError("Не задан XUI_PANEL_PASSWORD в .env")

    client = HostVDSClient()
    ip = await client.create_server(country_code, panel_user, panel_pass)

    panel_url = f"http://{ip}:{settings.xui_panel_port}"

    # Ждём, пока cloud-init поставит Docker и поднимет панель (до ~6 минут)
    await _wait_for_panel(panel_url)

    async with XUIClient(panel_url, panel_user, panel_pass) as xui:
        inbound = await xui.create_reality_inbound(port=443, sni=settings.vless_sni)

    return ProvisionedServer(
        ip=ip,
        panel_url=panel_url,
        panel_username=panel_user,
        panel_password=panel_pass,
        inbound_id=inbound.inbound_id,
        vless_port=inbound.port,
        public_key=inbound.public_key,
        sni=inbound.sni,
        short_id=inbound.short_id,
    )


async def _wait_for_panel(panel_url: str, attempts: int = 72) -> None:
    """Опрашивает панель каждые 5 секунд, пока она не начнёт отвечать."""
    async with aiohttp.ClientSession() as session:
        for _ in range(attempts):
            await asyncio.sleep(5)
            try:
                async with session.get(
                    panel_url, timeout=aiohttp.ClientTimeout(total=5)
                ) as resp:
                    if resp.status < 500:
                        logger.info("Панель %s отвечает", panel_url)
                        return
            except (aiohttp.ClientError, asyncio.TimeoutError):
                continue
    raise ProvisioningError(f"Панель {panel_url} не поднялась за отведённое время")
