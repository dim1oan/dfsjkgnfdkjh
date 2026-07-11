"""
Точка входа бота.

Запуск:
    python bot.py
"""

import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.enums import ParseMode
from aiogram.fsm.storage.memory import MemoryStorage

from admin import router as admin_router
from config import settings
from database import init_db
from handlers import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
)
logger = logging.getLogger(__name__)


async def main() -> None:
    """Инициализация БД, бота и запуск long polling."""
    await init_db()
    logger.info("База данных инициализирована")

    # Если задан PROXY_URL — подключаемся к Telegram API через прокси.
    # Для socks5 требуется пакет aiohttp-socks (pip install aiohttp-socks).
    session = None
    if settings.proxy_url:
        session = AiohttpSession(proxy=settings.proxy_url)
        logger.info("Использую прокси: %s", settings.proxy_url)

    bot = Bot(
        token=settings.bot_token,
        session=session,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )
    dp = Dispatcher(storage=MemoryStorage())
    # Админ-роутер первым: его команды не должны попадать в общий fallback
    dp.include_router(admin_router)
    dp.include_router(router)

    # Сбрасываем накопившиеся апдейты, чтобы не отвечать на старые сообщения
    await bot.delete_webhook(drop_pending_updates=True)

    logger.info("Бот запущен. Начинаю polling…")
    try:
        await dp.start_polling(bot)
    finally:
        await bot.session.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except (KeyboardInterrupt, SystemExit):
        logger.info("Бот остановлен")
