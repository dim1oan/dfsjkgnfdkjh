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
from dataclasses import dataclass

import aiohttp

from config import settings
from xui_client import XUIClient

logger = logging.getLogger(__name__)


class ProvisioningError(RuntimeError):
    """Ошибка автоаренды сервера."""


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
        if not (
            settings.hostvds_username
            and settings.hostvds_password
            and settings.hostvds_project_name
        ):
            raise ProvisioningError(
                "Не заданы HOSTVDS_USERNAME / HOSTVDS_PASSWORD / "
                "HOSTVDS_PROJECT_NAME в .env"
            )
        self._token: str | None = None
        self._compute_url: str | None = None

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
        async with session.post(
            f"{settings.hostvds_auth_url}/auth/tokens", json=payload
        ) as resp:
            if resp.status not in (200, 201):
                raise ProvisioningError(
                    f"Keystone auth failed ({resp.status}): {await resp.text()}"
                )
            self._token = resp.headers["X-Subject-Token"]
            body = await resp.json()

        for svc in body["token"]["catalog"]:
            if svc["type"] == "compute":
                for ep in svc["endpoints"]:
                    if ep["interface"] == "public":
                        self._compute_url = ep["url"]
                        break
        if not self._compute_url:
            raise ProvisioningError("Compute endpoint не найден в каталоге")

    def _headers(self) -> dict:
        return {"X-Auth-Token": self._token or ""}

    async def _find_id(
        self, session: aiohttp.ClientSession, path: str, key: str, name: str
    ) -> str:
        """Ищет ID flavor/image по имени."""
        async with session.get(
            f"{self._compute_url}{path}", headers=self._headers()
        ) as resp:
            data = await resp.json()
        for item in data.get(key, []):
            if item["name"].lower() == name.lower():
                return item["id"]
        raise ProvisioningError(f"{key}: «{name}» не найден. Доступны: "
                                + ", ".join(i["name"] for i in data.get(key, [])))

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
            async with session.post(
                f"{self._compute_url}/servers",
                json=payload,
                headers=self._headers(),
            ) as resp:
                if resp.status not in (200, 202):
                    raise ProvisioningError(
                        f"Создание сервера отклонено ({resp.status}): "
                        f"{await resp.text()}"
                    )
                server_id = (await resp.json())["server"]["id"]
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
