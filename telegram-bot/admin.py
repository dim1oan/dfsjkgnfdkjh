"""
Админ-команды управления серверами.

Доступны только Telegram-ID из ADMIN_IDS (.env):
    /servers                — список серверов и их статус
    /add_server <код>       — автоаренда VPS в HostVDS + установка 3x-ui
                              (коды: nl, fr, us, hk, kz — см. provisioning.COUNTRIES)
    /toggle_server <id>     — включить/выключить продажу на сервере
"""

import asyncio
import html
import logging

from aiogram import Router
from aiogram.filters import Command, Filter
from aiogram.types import Message
from sqlalchemy import select

from config import settings
from database import get_session
from models import Server
from provisioning import COUNTRIES, ProvisioningError, provision_server

logger = logging.getLogger(__name__)

router = Router(name="admin")


class IsAdmin(Filter):
    """Пропускает только администраторов из ADMIN_IDS."""

    async def __call__(self, message: Message) -> bool:
        return message.from_user.id in settings.admin_id_list


@router.message(Command("servers"), IsAdmin())
async def servers_list_handler(message: Message) -> None:
    async with get_session() as session:
        result = await session.execute(select(Server).order_by(Server.id))
        servers = result.scalars().all()

    if not servers:
        await message.answer(
            "Серверов пока нет.\n"
            "Добавьте первый: <code>/add_server nl</code>\n\n"
            "Доступные коды стран: "
            + ", ".join(f"<code>{c}</code>" for c in COUNTRIES)
        )
        return

    lines = ["<b>Серверы</b>", "━━━━━━━━━━━━━━━━━━"]
    for s in servers:
        status = "🟢 активен" if s.is_active else "🔴 отключён"
        lines.append(
            f"#{s.id} {s.flag} <b>{s.country_name}</b> — {status}\n"
            f"   IP: <code>{s.ip}</code> | панель: {s.panel_url}\n"
            f"   inbound: {s.inbound_id}"
        )
    await message.answer("\n".join(lines))


@router.message(Command("add_server"), IsAdmin())
async def add_server_handler(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or parts[1].lower() not in COUNTRIES:
        await message.answer(
            "Использование: <code>/add_server &lt;код страны&gt;</code>\n"
            "Доступные коды: "
            + ", ".join(
                f"<code>{c}</code> ({v['flag']} {v['name']})"
                for c, v in COUNTRIES.items()
            )
        )
        return

    code = parts[1].lower()
    country = COUNTRIES[code]
    await message.answer(
        f"⏳ Начинаю аренду сервера {country['flag']} <b>{country['name']}</b> "
        "в HostVDS.\n"
        "Это займёт 3–7 минут: создание VPS → установка 3x-ui → настройка "
        "Reality. Я напишу, когда всё будет готово."
    )

    # Запускаем в фоне, чтобы не блокировать бота
    asyncio.create_task(_provision_and_notify(message, code))


async def _provision_and_notify(message: Message, code: str) -> None:
    country = COUNTRIES[code]
    try:
        result = await provision_server(code)
    except ProvisioningError as exc:
        logger.exception("Ошибка провижининга %s", code)
        await message.answer(
            f"❌ Не удалось создать сервер: {html.escape(str(exc))}"
        )
        return
    except Exception as exc:  # noqa: BLE001 — сообщаем админу о любой ошибке
        logger.exception("Неожиданная ошибка провижининга %s", code)
        await message.answer(
            f"❌ Неожиданная ошибка: {html.escape(str(exc))}"
        )
        return

    async with get_session() as session:
        server = Server(
            country_code=code,
            country_name=country["name"],
            flag=country["flag"],
            ip=result.ip,
            panel_url=result.panel_url,
            panel_username=result.panel_username,
            panel_password=result.panel_password,
            inbound_id=result.inbound_id,
            vless_port=result.vless_port,
            public_key=result.public_key,
            sni=result.sni,
            short_id=result.short_id,
            is_active=True,
        )
        session.add(server)
        await session.flush()
        server_id = server.id

    await message.answer(
        f"✅ Сервер {country['flag']} <b>{country['name']}</b> готов!\n"
        f"ID: #{server_id}\n"
        f"IP: <code>{result.ip}</code>\n"
        f"Панель: {result.panel_url}\n\n"
        "Сервер активен и уже доступен клиентам при покупке."
    )


@router.message(Command("toggle_server"), IsAdmin())
async def toggle_server_handler(message: Message) -> None:
    parts = (message.text or "").split()
    if len(parts) != 2 or not parts[1].isdigit():
        await message.answer("Использование: <code>/toggle_server &lt;id&gt;</code>")
        return

    async with get_session() as session:
        server = await session.get(Server, int(parts[1]))
        if server is None:
            await message.answer("Сервер с таким ID не найден.")
            return
        server.is_active = not server.is_active
        status = "включён" if server.is_active else "отключён"
        name = f"{server.flag} {server.country_name}"

    await message.answer(f"Сервер #{parts[1]} {name} теперь <b>{status}</b>.")
