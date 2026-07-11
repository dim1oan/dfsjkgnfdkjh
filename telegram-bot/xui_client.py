"""
Асинхронный клиент API панели 3x-ui (https://github.com/MHSanaei/3x-ui).

Возможности:
    - логин в панель (cookie-сессия)
    - первичная настройка сервера: создание VLESS/Reality-инбаунда
    - добавление клиента (реальная выдача ключа при покупке)
    - продление клиента (обновление expiryTime)
    - сборка vless:// ссылки для приложения

Endpoints соответствуют 3x-ui v2.x. Если у вас другая версия панели,
сверьте пути в разделе Panel Settings → API документации.
"""

import json
import logging
import secrets
import uuid as uuid_lib
from dataclasses import dataclass
from urllib.parse import quote

import aiohttp

logger = logging.getLogger(__name__)


class XUIError(RuntimeError):
    """Ошибка при обращении к API 3x-ui."""


@dataclass
class RealityInbound:
    """Параметры созданного Reality-инбаунда."""

    inbound_id: int
    port: int
    public_key: str
    sni: str
    short_id: str


class XUIClient:
    """
    Клиент одной панели 3x-ui.

    Использование:
        async with XUIClient(panel_url, username, password) as xui:
            await xui.add_client(...)
    """

    def __init__(self, panel_url: str, username: str, password: str) -> None:
        # panel_url вида http://1.2.3.4:2053 (без завершающего /)
        self.base = panel_url.rstrip("/")
        self.username = username
        self.password = password
        self._session: aiohttp.ClientSession | None = None

    async def __aenter__(self) -> "XUIClient":
        self._session = aiohttp.ClientSession()
        await self._login()
        return self

    async def __aexit__(self, *exc) -> None:
        if self._session:
            await self._session.close()

    # ── Внутренние помощники ─────────────────────────────────────────────────

    async def _login(self) -> None:
        """Логин: панель ставит session-cookie, aiohttp хранит её сама."""
        assert self._session is not None
        async with self._session.post(
            f"{self.base}/login",
            data={"username": self.username, "password": self.password},
            timeout=aiohttp.ClientTimeout(total=15),
        ) as resp:
            data = await resp.json(content_type=None)
            if not data.get("success"):
                raise XUIError(f"Не удалось войти в панель {self.base}: {data}")
        logger.info("Успешный вход в панель %s", self.base)

    async def _post(self, path: str, payload: dict) -> dict:
        assert self._session is not None
        async with self._session.post(
            f"{self.base}{path}",
            json=payload,
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
            if not data.get("success"):
                raise XUIError(f"POST {path} failed: {data}")
            return data

    async def _get(self, path: str) -> dict:
        assert self._session is not None
        async with self._session.get(
            f"{self.base}{path}",
            timeout=aiohttp.ClientTimeout(total=20),
        ) as resp:
            data = await resp.json(content_type=None)
            if not data.get("success"):
                raise XUIError(f"GET {path} failed: {data}")
            return data

    # ── Первичная настройка сервера ──────────────────────────────────────────

    async def create_reality_inbound(
        self, port: int = 443, sni: str = "yahoo.com"
    ) -> RealityInbound:
        """
        Создаёт VLESS + Reality (xtls-rprx-vision) инбаунд.

        Вызывается один раз при вводе нового сервера в строй.
        """
        # Панель генерирует пару ключей x25519
        keys = await self._get("/server/getNewX25519Cert")
        private_key = keys["obj"]["privateKey"]
        public_key = keys["obj"]["publicKey"]

        short_id = secrets.token_hex(4)

        stream_settings = {
            "network": "tcp",
            "security": "reality",
            "realitySettings": {
                "show": False,
                "xver": 0,
                "dest": f"{sni}:443",
                "serverNames": [sni, f"www.{sni}"],
                "privateKey": private_key,
                "shortIds": [short_id],
                "settings": {
                    "publicKey": public_key,
                    "fingerprint": "chrome",
                    "spiderX": "/",
                },
            },
            "tcpSettings": {"header": {"type": "none"}},
        }

        payload = {
            "enable": True,
            "remark": "bot-clients",
            "listen": "",
            "port": port,
            "protocol": "vless",
            "expiryTime": 0,
            "settings": json.dumps(
                {"clients": [], "decryption": "none", "fallbacks": []}
            ),
            "streamSettings": json.dumps(stream_settings),
            "sniffing": json.dumps(
                {"enabled": True, "destOverride": ["http", "tls", "quic"]}
            ),
        }

        data = await self._post("/panel/api/inbounds/add", payload)
        inbound_id = data["obj"]["id"]
        logger.info("Создан Reality-инбаунд id=%s на %s", inbound_id, self.base)

        return RealityInbound(
            inbound_id=inbound_id,
            port=port,
            public_key=public_key,
            sni=sni,
            short_id=short_id,
        )

    # ── Работа с клиентами ───────────────────────────────────────────────────

    async def add_client(
        self,
        inbound_id: int,
        email: str,
        expiry_ms: int,
        limit_ip: int = 1,
    ) -> str:
        """
        Добавляет клиента в инбаунд. Возвращает UUID клиента.

        email — уникальный идентификатор клиента в панели (используем tg-id),
        expiry_ms — срок действия в миллисекундах Unix-времени,
        limit_ip — максимум одновременных устройств (по тарифу).
        """
        client_uuid = str(uuid_lib.uuid4())
        client = {
            "id": client_uuid,
            "flow": "xtls-rprx-vision",
            "email": email,
            "limitIp": limit_ip,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": "",
            "subId": "",
        }
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]}),
        }
        await self._post("/panel/api/inbounds/addClient", payload)
        logger.info("Добавлен клиент %s (inbound %s)", email, inbound_id)
        return client_uuid

    async def update_client_expiry(
        self,
        inbound_id: int,
        client_uuid: str,
        email: str,
        expiry_ms: int,
        limit_ip: int = 1,
    ) -> None:
        """Продлевает срок действия существующего клиента."""
        client = {
            "id": client_uuid,
            "flow": "xtls-rprx-vision",
            "email": email,
            "limitIp": limit_ip,
            "totalGB": 0,
            "expiryTime": expiry_ms,
            "enable": True,
            "tgId": "",
            "subId": "",
        }
        payload = {
            "id": inbound_id,
            "settings": json.dumps({"clients": [client]}),
        }
        await self._post(f"/panel/api/inbounds/updateClient/{client_uuid}", payload)
        logger.info("Продлён клиент %s до %s", email, expiry_ms)


def build_vless_link(
    client_uuid: str,
    server_ip: str,
    port: int,
    public_key: str,
    sni: str,
    short_id: str,
    label: str,
) -> str:
    """Собирает vless:// Reality-ссылку для импорта в приложение."""
    return (
        f"vless://{client_uuid}@{server_ip}:{port}"
        f"?type=tcp&security=reality&flow=xtls-rprx-vision"
        f"&pbk={public_key}&fp=chrome&sni={sni}&sid={short_id}&spx=%2F"
        f"#{quote(label)}"
    )
