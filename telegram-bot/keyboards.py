"""
Клавиатуры бота.

Reply-клавиатура — главное меню.
Inline-клавиатуры — тарифы, сроки, оплата, инструкции.
"""

from aiogram.types import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
)
from aiogram.utils.keyboard import InlineKeyboardBuilder

from config import settings

# ── Тексты кнопок главного меню ──────────────────────────────────────────────

BTN_PROFILE = "💳 Личный кабинет"
BTN_BUY = "🛍 Купить VPN"
BTN_GUIDES = "🚀 Инструкция по подключению"
BTN_SUPPORT = "🧑‍💻 Поддержка"

# ── Тарифы ───────────────────────────────────────────────────────────────────

PLANS: dict[str, dict] = {
    "start": {
        "title": "Стартовый",
        "price": 99,
        "devices": "1 устройство",
        "description": "Идеально для телефона",
        "limit_ip": 1,
    },
    "family": {
        "title": "Семейный",
        "price": 200,
        "devices": "до 10 устройств",
        "description": "Для всей семьи и роутеров",
        "limit_ip": 10,
    },
}

DURATIONS: dict[int, str] = {
    1: "1 месяц",
    3: "3 месяца",
    6: "6 месяцев",
}


def main_menu_keyboard() -> ReplyKeyboardMarkup:
    """Постоянное главное меню (Reply Keyboard)."""
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text=BTN_PROFILE), KeyboardButton(text=BTN_BUY)],
            [KeyboardButton(text=BTN_GUIDES)],
            [KeyboardButton(text=BTN_SUPPORT)],
        ],
        resize_keyboard=True,
        input_field_placeholder="Выберите раздел меню…",
    )


def plans_keyboard() -> InlineKeyboardMarkup:
    """Выбор тарифного плана."""
    builder = InlineKeyboardBuilder()
    for plan_id, plan in PLANS.items():
        builder.row(
            InlineKeyboardButton(
                text=f"⚡ {plan['title']} — {plan['price']} ₽/мес",
                callback_data=f"plan:{plan_id}",
            )
        )
    return builder.as_markup()


def countries_keyboard(plan_id: str, servers: list) -> InlineKeyboardMarkup:
    """
    Выбор страны подключения.

    servers — список активных объектов Server из БД.
    """
    builder = InlineKeyboardBuilder()
    for server in servers:
        builder.row(
            InlineKeyboardButton(
                text=f"{server.flag} {server.country_name}",
                callback_data=f"country:{plan_id}:{server.id}",
            )
        )
    builder.row(
        InlineKeyboardButton(text="◀️ Назад к тарифам", callback_data="back_to_plans")
    )
    return builder.as_markup()


def durations_keyboard(plan_id: str, server_id: int) -> InlineKeyboardMarkup:
    """Выбор срока подписки для выбранного тарифа и страны."""
    builder = InlineKeyboardBuilder()
    price = PLANS[plan_id]["price"]
    for months, label in DURATIONS.items():
        total = price * months
        builder.row(
            InlineKeyboardButton(
                text=f"🗓 {label} — {total} ₽",
                callback_data=f"duration:{plan_id}:{server_id}:{months}",
            )
        )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад к странам", callback_data=f"plan:{plan_id}"
        )
    )
    return builder.as_markup()


def payment_keyboard(plan_id: str, server_id: int, months: int) -> InlineKeyboardMarkup:
    """Кнопка оплаты (mock) и подтверждение платежа."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="💳 Перейти к оплате",
            url="https://example.com/payment/mock",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="✅ Я оплатил(а)",
            callback_data=f"paid:{plan_id}:{server_id}:{months}",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="◀️ Назад", callback_data=f"country:{plan_id}:{server_id}"
        )
    )
    return builder.as_markup()


def guides_keyboard() -> InlineKeyboardMarkup:
    """Ссылки на приложения для подключения."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="📱 v2rayTun (iOS)",
            url="https://apps.apple.com/app/v2raytun/id6476628951",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🤖 v2rayNG (Android)",
            url="https://play.google.com/store/apps/details?id=com.v2ray.ang",
        )
    )
    builder.row(
        InlineKeyboardButton(
            text="🦊 FoXray (iOS / macOS)",
            url="https://apps.apple.com/app/foxray/id6448898396",
        )
    )
    return builder.as_markup()


def support_keyboard() -> InlineKeyboardMarkup:
    """Кнопка связи с поддержкой."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(
            text="✉️ Написать в поддержку",
            url=f"https://t.me/{settings.support_username}",
        )
    )
    return builder.as_markup()


def buy_vpn_inline_keyboard() -> InlineKeyboardMarkup:
    """Быстрая кнопка «Купить VPN» из личного кабинета."""
    builder = InlineKeyboardBuilder()
    builder.row(
        InlineKeyboardButton(text="🛍 Выбрать тариф", callback_data="back_to_plans")
    )
    return builder.as_markup()
