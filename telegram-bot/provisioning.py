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
# У HostVDS локация сервера определяется РЕГИОНОМ OpenStack (Amsterdam,
# Paris, Dallas...), а не зоной доступности (зона обычно одна — «nova»).
# "region_keywords" — слова для поиска нужного региона в каталоге Keystone.
# "az" / "az_keywords" — на случай, если у региона несколько зон.
COUNTRIES: dict[str, dict] = {
    "nl": {
        "name": "Нидерланды", "flag": "🇳🇱",
        "az": "Amsterdam", "az_keywords": ["amsterdam", "nl"],
        "region_keywords": ["amsterdam", "ams", "nl"],
    },
    "fr": {
        "name": "Франция", "flag": "🇫🇷",
        "az": "Paris", "az_keywords": ["paris", "fr"],
        "region_keywords": ["paris", "par", "fr"],
    },
    "us": {
        "name": "США", "flag": "🇺🇸",
        "az": "Dallas", "az_keywords": ["dallas", "us"],
        "region_keywords": ["dallas", "dal", "us"],
    },
    "hk": {
        "name": "Гонконг", "flag": "🇭🇰",
        "az": "Hong Kong", "az_keywords": ["hong", "hk"],
        "region_keywords": ["hong", "hk"],
    },
    "kz": {
        "name": "Казахстан", "flag": "🇰🇿",
        "az": "Almaty", "az_keywords": ["almaty", "kz"],
        "region_keywords": ["almaty", "alm", "kz"],
    },
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
        self._region: str | None = None
        # {region: {"compute": url, "network": url}} — заполняется
        # при аутентификации из каталога Keystone.
        self._endpoints: dict[str, dict[str, str]] = {}

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

        # Собираем endpoints ВСЕХ регионов: {region: {"compute": url, ...}}.
        # Локация сервера у HostVDS определяется регионом, поэтому для
        # мультистрановости нужно знать все доступные регионы.
        wanted_interface = (settings.hostvds_interface or "public").lower()
        self._endpoints = {}
        for svc in body["token"]["catalog"]:
            if svc["type"] not in ("compute", "network"):
                continue
            for ep in svc["endpoints"]:
                if ep["interface"] != wanted_interface:
                    continue
                region = ep.get("region") or "default"
                self._endpoints.setdefault(region, {})[svc["type"]] = (
                    ep["url"].rstrip("/")
                )

        regions_with_compute = [
            r for r, eps in self._endpoints.items() if "compute" in eps
        ]
        if not regions_with_compute:
            raise ProvisioningError(
                "Compute endpoint не найден в каталоге "
                f"(interface={wanted_interface}). "
                "Проверьте HOSTVDS_INTERFACE в .env."
            )
        logger.info(
            "Доступные регионы: %s", ", ".join(sorted(regions_with_compute))
        )

        # Регион по умолчанию: из .env или первый доступный.
        default_region = settings.hostvds_region_name
        if default_region not in regions_with_compute:
            default_region = regions_with_compute[0]
        self._use_region(default_region)

    def _use_region(self, region: str) -> None:
        """Переключает клиента на endpoints указанного региона."""
        eps = self._endpoints.get(region, {})
        if "compute" not in eps:
            raise ProvisioningError(
                f"В регионе «{region}» нет compute endpoint. Доступные: "
                + ", ".join(sorted(self._endpoints))
            )
        self._compute_url = eps["compute"]
        self._network_url = eps.get("network")
        self._region = region
        logger.info("Используем регион «%s»", region)

    def _select_region_for_country(self, country: dict) -> str:
        """Подбирает регион по ключевым словам страны.

        Если совпадений нет и регион всего один — используем его.
        Иначе — ошибка со списком доступных регионов.
        """
        regions = sorted(
            r for r, eps in self._endpoints.items() if "compute" in eps
        )
        keywords = country.get("region_keywords", [])
        for kw in keywords:
            for r in regions:
                if kw in r.lower():
                    return r
        if len(regions) == 1:
            logger.info(
                "Регион для «%s» не найден по ключевым словам, "
                "используем единственный «%s»",
                country["name"], regions[0],
            )
            return regions[0]
        raise ProvisioningError(
            f"Регион для «{country['name']}» не найден. "
            "Доступные регионы у провайдера: " + ", ".join(regions)
            + "\nДобавьте подходящее ключевое слово в region_keywords "
            "в COUNTRIES (provisioning.py)."
        )

    def _headers(self) -> dict:
        return {"X-Auth-Token": self._token or ""}

    async def _find_id(
        self, session: aiohttp.ClientSession, path: str, key: str, name: str
    ) -> str:
        """Ищет ID flavor/image по имени.

        Сначала точное совпадени�� (без учёта регистра), затем «мягкое»:
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

    async def _resolve_availability_zone(
        self, session: aiohttp.ClientSession, country: dict
    ) -> str | None:
        """Подбирает реальное имя зоны доступности из списка провайдера.

        Возвращает имя зоны или None, если список получить не удалось
        (тогда зону в запросе лучше не указывать вовсе).
        """
        try:
            async with session.get(
                f"{self._compute_url}/os-availability-zone",
                headers=self._headers(),
            ) as resp:
                if resp.status != 200:
                    logger.warning(
                        "os-availability-zone → HTTP %s, зону не указываем",
                        resp.status,
                    )
                    return None
                data = await resp.json()
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            logger.warning("Не удалось получить список зон: %s", exc)
            return None

        zones = [
            z["zoneName"]
            for z in data.get("availabilityZoneInfo", [])
            if z.get("zoneState", {}).get("available", True)
        ]
        if not zones:
            return None
        logger.info("Доступные зоны: %s", ", ".join(zones))

        # 1. Точное совпадение с предпочитаемым именем
        preferred = country["az"].lower()
        for z in zones:
            if z.lower() == preferred:
                return z

        # 2. Поиск по ключевым словам страны (amsterdam-1 и т.п.)
        for kw in country.get("az_keywords", []):
            for z in zones:
                if kw in z.lower():
                    return z

        # 3. Совпадений нет. Если зона всего одна (типичный случай —
        #    единственная зона «nova»: локация тогда определяется регионом
        #    из HOSTVDS_REGION_NAME, а не зоной) — используем её.
        if len(zones) == 1:
            logger.info(
                "Совпадений по стране нет, используем единственную зону «%s» "
                "(локация определяется регионом OS_REGION_NAME)",
                zones[0],
            )
            return zones[0]

        # Зон несколько, но ни одна не подходит — не рискуем, сообщаем.
        raise ProvisioningError(
            f"Зона для «{country['name']}» не найдена. "
            "Доступные зоны у провайдера: " + ", ".join(zones)
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

            # Переключаемся на регион, соответствующий выбранной стране
            # (локация сервера у HostVDS определяется регионом).
            region = self._select_region_for_country(country)
            self._use_region(region)

            flavor_id = await self._find_id(
                session, "/flavors", "flavors", settings.hostvds_flavor
            )
            image_id = await self._find_id(
                session, "/images", "images", settings.hostvds_image
            )

            az = await self._resolve_availability_zone(session, country)

            payload = {
                "server": {
                    "name": f"vpn-{country_code}",
                    "flavorRef": flavor_id,
                    "imageRef": image_id,
                    "user_data": user_data,
                    "networks": "auto",
                }
            }
            if az:
                payload["server"]["availability_zone"] = az
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
