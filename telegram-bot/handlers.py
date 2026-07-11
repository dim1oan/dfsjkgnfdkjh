"""
Обработчики бота: /start, главное меню, покупка подписки (FSM + callback).
"""

import logging
from datetime import datetime, timedelta, timezone

from aiogram import F, Router
from aiogram.filters import CommandStart
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.types import CallbackQuery, Message
from sqlalchemy import select

from database import get_session
from keyboards import (
    BTN_BUY,
    BTN_GUIDES,
    BTN_PROFILE,
    BTN_SUPPORT,
    DURATIONS,
    PLANS,
    buy_vpn_inline_keyboard,
    countries_keyboard,
    durations_keyboard,
    guides_keyboard,
    main_menu_keyboard,
    payment_keyboard,
    plans_keyboard,
    support_keyboard,
)
from models import Server, User
from xui_client import XUIClient, XUIError, build_vless_link

logger = logging.getLogger(__name__)

router = Router(name="main")


# ── FSM ──────────────────────────────────────────────────────────────────────


class PurchaseFlow(StatesGroup):
    """Состояния сценария покупки подписки."""

    choosing_plan = State()
    choosing_country = State()
    choosing_duration = State()
    awaiting_payment = State()


# ── Вспомогательные функции ──────────────────────────────────────────────────


async def get_or_create_user(telegram_id: int, username: str | None) -> User:
    """Возвращает пользователя из БД, создавая его при первом обращении."""
    async with get_session() as session:
        result = await session.execute(
            select(User).where(User.telegram_id == telegram_id)
        )
        user = result.scalar_one_or_none()
        if user is None:
            user = User(telegram_id=telegram_id, username=username, balance=0)
            session.add(user)
        elif username and user.username != username:
            user.username = username
        await session.flush()
        await session.refresh(user)
        return user


async def issue_real_vless_link(
    user: User, server: Server, expires_at: datetime, limit_ip: int
) -> str:
    """
    Создаёт (или продлевает) реального клиента в панели 3x-ui сервера
    и возвращает vless:// ссылку.
    """
    expiry_ms = int(expires_at.timestamp() * 1000)
    email = f"tg-{user.telegram_id}"

    async with XUIClient(
        server.panel_url, server.panel_username, server.panel_password
    ) as xui:
        if user.xui_client_uuid and user.server_id == server.id:
            # Клиент уже есть на этом сервере — просто продлеваем срок
            await xui.update_client_expiry(
                inbound_id=server.inbound_id,
                client_uuid=user.xui_client_uuid,
                email=email,
                expiry_ms=expiry_ms,
                limit_ip=limit_ip,
            )
            client_uuid = user.xui_client_uuid
        else:
            # Новый клиент (первая покупка или смена страны)
            client_uuid = await xui.add_client(
                inbound_id=server.inbound_id,
                email=email,
                expiry_ms=expiry_ms,
                limit_ip=limit_ip,
            )

    user.server_id = server.id
    user.xui_client_uuid = client_uuid
    user.xui_email = email

    return build_vless_link(
        client_uuid=client_uuid,
        server_ip=server.ip,
        port=server.vless_port,
        public_key=server.public_key or "",
        sni=server.sni,
        short_id=server.short_id or "",
        label=f"{server.flag} {server.country_name} VPN",
    )


def format_expiry(dt: datetime | None) -> str:
    """Форматирует дату окончания подписки для сообщений."""
    if dt is None:
        return "—"
    return dt.strftime("%d.%m.%Y")


# ── /start ───────────────────────────────────────────────────────────────────


@router.message(CommandStart())
async def cmd_start(message: Message, state: FSMContext) -> None:
    await state.clear()
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username
    )

    text = (
        "🛡 <b>Добро пожаловать в наш VPN-сервис!</b>\n\n"
        "Высокая скорость, надёжная защита и подключение "
        "в 1 клик прямо из Telegram.\n\n"
        "⚡ <b>Что умеет бот:</b>\n"
        "▫️ Оформление подписки за 30 секунд\n"
        "▫️ Мгновенная выдача ссылки для подключения\n"
        "▫️ Личный кабинет с балансом и статусом подписки\n"
        "▫️ Пошаговые инструкции для любого устройства\n\n"
        "👇 Выберите раздел в меню ниже."
    )
    await message.answer(text, reply_markup=main_menu_keyboard())


# ── Личный кабинет ───────────────────────────────────────────────────────────


@router.message(F.text == BTN_PROFILE)
async def profile_handler(message: Message) -> None:
    user = await get_or_create_user(
        message.from_user.id, message.from_user.username
    )

    if user.has_active_subscription:
        status = f"✅ Активна до <b>{format_expiry(user.subscription_expires_at)}</b>"
    else:
        status = "❌ Нет активной подписки"

    text = (
        "💳 <b>Личный кабинет</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"🆔 ID: <code>{user.telegram_id}</code>\n"
        f"💰 Баланс: <b>{user.balance:.0f} ₽</b>\n"
        f"📅 Подписка: {status}\n"
    )

    if user.has_active_subscription and user.vless_link:
        text += (
            "\n🔑 <b>Ваша ссылка для подключения:</b>\n"
            f"<code>{user.vless_link}</code>\n\n"
            "📋 Нажмите на ссылку, чтобы скопировать её, затем вставьте "
            "в приложение (v2rayTun, v2rayNG или FoXray) и включите VPN."
        )
        await message.answer(text)
    else:
        text += (
            "\n🚀 Подключите VPN за 1 минуту — выберите тариф "
            "и получите ссылку сразу после оплаты."
        )
        await message.answer(text, reply_markup=buy_vpn_inline_keyboard())


# ── Покупка VPN ──────────────────────────────────────────────────────────────


@router.message(F.text == BTN_BUY)
async def buy_handler(message: Message, state: FSMContext) -> None:
    await state.set_state(PurchaseFlow.choosing_plan)
    await message.answer(_plans_text(), reply_markup=plans_keyboard())


def _plans_text() -> str:
    return (
        "🛍 <b>Выберите тарифный план</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "⚡ <b>Стартовый</b> — 99 ₽/мес\n"
        "▫️ 1 устройство\n"
        "▫️ Идеально для телефона\n\n"
        "👨‍👩‍👧‍👦 <b>Семейный</b> — 200 ₽/мес\n"
        "▫️ До 10 устройств\n"
        "▫️ Для всей семьи и роутеров\n\n"
        "🔒 Безлимитный трафик и максимальная скорость на всех тарифах."
    )


@router.callback_query(F.data == "back_to_plans")
async def back_to_plans_handler(callback: CallbackQuery, state: FSMContext) -> None:
    await state.set_state(PurchaseFlow.choosing_plan)
    await callback.message.edit_text(_plans_text(), reply_markup=plans_keyboard())
    await callback.answer()


@router.callback_query(F.data.startswith("plan:"))
async def plan_selected_handler(callback: CallbackQuery, state: FSMContext) -> None:
    plan_id = callback.data.split(":")[1]
    plan = PLANS.get(plan_id)
    if plan is None:
        await callback.answer("Тариф не найден. Попробуйте ещё раз.", show_alert=True)
        return

    # Активные серверы = доступные страны
    async with get_session() as session:
        result = await session.execute(
            select(Server)
            .where(Server.is_active.is_(True))
            .order_by(Server.country_name)
        )
        servers = list(result.scalars().all())

    if not servers:
        await callback.answer(
            "Пока нет доступных серверов. Попробуйте позже.", show_alert=True
        )
        return

    await state.set_state(PurchaseFlow.choosing_country)
    await state.update_data(plan_id=plan_id)

    text = (
        f"⚡ Тариф <b>«{plan['title']}»</b> — {plan['price']} ₽/мес\n"
        f"▫️ {plan['devices'].capitalize()}\n\n"
        "🌍 <b>Выберите страну подключения:</b>"
    )
    await callback.message.edit_text(
        text, reply_markup=countries_keyboard(plan_id, servers)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("country:"))
async def country_selected_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    _, plan_id, server_id_str = callback.data.split(":")
    server_id = int(server_id_str)
    plan = PLANS.get(plan_id)

    async with get_session() as session:
        server = await session.get(Server, server_id)

    if plan is None or server is None or not server.is_active:
        await callback.answer(
            "Сервер недоступен. Выберите другую страну.", show_alert=True
        )
        return

    await state.set_state(PurchaseFlow.choosing_duration)
    await state.update_data(plan_id=plan_id, server_id=server_id)

    text = (
        f"⚡ Тариф <b>«{plan['title']}»</b> — {plan['price']} ₽/мес\n"
        f"🌍 Страна: <b>{server.flag} {server.country_name}</b>\n\n"
        "🗓 <b>Выберите срок подписки:</b>"
    )
    await callback.message.edit_text(
        text, reply_markup=durations_keyboard(plan_id, server_id)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("duration:"))
async def duration_selected_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    _, plan_id, server_id_str, months_str = callback.data.split(":")
    server_id = int(server_id_str)
    months = int(months_str)
    plan = PLANS.get(plan_id)
    if plan is None or months not in DURATIONS:
        await callback.answer("Что-то пошло не так. Попробуйте ещё раз.", show_alert=True)
        return

    async with get_session() as session:
        server = await session.get(Server, server_id)
    if server is None:
        await callback.answer("Сервер недоступен.", show_alert=True)
        return

    total = plan["price"] * months
    await state.set_state(PurchaseFlow.awaiting_payment)
    await state.update_data(plan_id=plan_id, server_id=server_id, months=months)

    text = (
        "🧾 <b>Ваш заказ</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Тариф: <b>«{plan['title']}»</b>\n"
        f"🌍 Страна: <b>{server.flag} {server.country_name}</b>\n"
        f"🗓 Срок: <b>{DURATIONS[months]}</b>\n"
        f"💰 К оплате: <b>{total} ₽</b>\n\n"
        "Нажмите «Перейти к оплате», а после завершения платежа — "
        "«Я оплатил(а)». Ссылка для подключения придёт мгновенно."
    )
    await callback.message.edit_text(
        text, reply_markup=payment_keyboard(plan_id, server_id, months)
    )
    await callback.answer()


@router.callback_query(F.data.startswith("paid:"))
async def payment_confirmed_handler(
    callback: CallbackQuery, state: FSMContext
) -> None:
    """
    Имитация успешного платёжного callback.

    В production здесь будет webhook платёжного провайдера,
    который подтв��рждает оплату и продлевает подписку.
    """
    _, plan_id, server_id_str, months_str = callback.data.split(":")
    server_id = int(server_id_str)
    months = int(months_str)
    plan = PLANS.get(plan_id)
    if plan is None or months not in DURATIONS:
        await callback.answer("Заказ не найден. Начните заново.", show_alert=True)
        return

    telegram_id = callback.from_user.id
    now = datetime.now(timezone.utc)

    try:
        async with get_session() as session:
            server = await session.get(Server, server_id)
            if server is None or not server.is_active or server.inbound_id is None:
                await callback.answer(
                    "Сервер временно недоступен. Обратитесь в поддержку.",
                    show_alert=True,
                )
                return

            result = await session.execute(
                select(User).where(User.telegram_id == telegram_id)
            )
            user = result.scalar_one_or_none()
            if user is None:
                user = User(
                    telegram_id=telegram_id, username=callback.from_user.username
                )
                session.add(user)
                await session.flush()

            # Продлеваем от текущей даты окончания, если подписка ещё активна
            base = (
                user.subscription_expires_at
                if user.has_active_subscription
                else now
            )
            if base.tzinfo is None:
                base = base.replace(tzinfo=timezone.utc)
            user.subscription_expires_at = base + timedelta(days=30 * months)

            # Реальная выдача: создаём/продлеваем клиента в панели 3x-ui
            user.vless_link = await issue_real_vless_link(
                user=user,
                server=server,
                expires_at=user.subscription_expires_at,
                limit_ip=plan["limit_ip"],
            )

            expires_at = user.subscription_expires_at
            vless_link = user.vless_link
            country_label = f"{server.flag} {server.country_name}"
    except XUIError:
        logger.exception("Ошибка выдачи ключа для tg-%s", telegram_id)
        await callback.answer(
            "Не удалось создать ключ на сервере. Напишите в поддержку — "
            "мы всё исправим.",
            show_alert=True,
        )
        return

    await state.clear()

    text = (
        "🎉 <b>Оплата прошла успешно!</b>\n"
        "━━━━━━━━━━━━━━━━━━\n"
        f"⚡ Тариф: <b>«{plan['title']}»</b>\n"
        f"🌍 Страна: <b>{country_label}</b>\n"
        f"📅 Подписка активна до: <b>{format_expiry(expires_at)}</b>\n\n"
        "🔑 <b>Ваша персональная ссылка для подключения:</b>\n"
        f"<code>{vless_link}</code>\n\n"
        "📋 <b>Как подключиться:</b>\n"
        "1️⃣ Нажмите на ссылку выше — она скопируется автоматически\n"
        "2️⃣ Откройте приложение v2rayTun, v2rayNG или FoXray\n"
        "3️⃣ Вставьте ссылку из буфера обмена\n"
        "4️⃣ Включите VPN и наслаждайтесь свободным интернетом 🚀\n\n"
        "❓ Нужна помощь — раздел «🧑‍💻 Поддержка» в меню."
    )
    await callback.message.edit_text(text)
    await callback.answer("✅ Подписка активирована!")


# ── Инструкция по подключению ────────────────────────────────────────────────


@router.message(F.text == BTN_GUIDES)
async def guides_handler(message: Message) -> None:
    text = (
        "🚀 <b>Инструкция по подключению</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "1️⃣ <b>Скачайте приложение</b> для вашего устройства "
        "по кнопкам ниже.\n\n"
        "2️⃣ <b>Купите подписку</b> в разделе «🛍 Купить VPN» — "
        "ссылка для подключения придёт мгновенно.\n\n"
        "3️⃣ <b>Скопируйте ссылку</b> — просто нажмите на неё "
        "в личном кабинете, и она попадёт в буфер обмена.\n\n"
        "4️⃣ <b>Импортируйте конфигурацию</b> — откройте приложение "
        "и вставьте ссылку (обычно кнопка «+» → «Импорт из буфера»).\n\n"
        "5️⃣ <b>Включите VPN</b> одним нажатием — готово! 🎉\n\n"
        "💡 Если что-то не получается — напишите в поддержку, "
        "поможем в течение нескольких минут."
    )
    await message.answer(text, reply_markup=guides_keyboard())


# ── Поддержка ────────────────────────────────────────────────────────────────


@router.message(F.text == BTN_SUPPORT)
async def support_handler(message: Message) -> None:
    text = (
        "🧑‍💻 <b>Поддержка</b>\n"
        "━━━━━━━━━━━━━━━━━━\n\n"
        "Мы на связи и отвечаем быстро.\n\n"
        "✉️ Напишите нам, если:\n"
        "▫️ Не получается подключиться\n"
        "▫️ Возникли вопросы по оплате\n"
        "▫️ Нужна помощь с настройкой на роутере\n\n"
        "Среднее время ответа — <b>до 15 минут</b>."
    )
    await message.answer(text, reply_markup=support_keyboard())


# ── Fallback ─────────────────────────────────────────────────────────────────


@router.message(F.text)
async def unknown_message_handler(message: Message) -> None:
    await message.answer(
        "🤔 Я не понял команду. Пожалуйста, воспользуйтесь меню ни��е 👇",
        reply_markup=main_menu_keyboard(),
    )
