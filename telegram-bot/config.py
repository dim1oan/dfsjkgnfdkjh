"""
Конфигурация бота.

Загружает переменные окружения из файла `.env` с помощью Pydantic Settings.
Обязательные переменные:
    BOT_TOKEN    — токен Telegram-бота от @BotFather
    DATABASE_URL — строка подключения к БД (async-драйвер)
"""

from functools import lru_cache
from pathlib import Path

from pydantic_settings import BaseSettings, SettingsConfigDict

# Путь к .env рядом с этим файлом — работает независимо от того,
# из какой директории запускается бот.
_ENV_FILE = Path(__file__).resolve().parent / ".env"


class Settings(BaseSettings):
    """Настройки приложения, читаемые из окружения / .env."""

    model_config = SettingsConfigDict(
        env_file=_ENV_FILE,
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # Токен бота (получить у @BotFather)
    bot_token: str

    # Async-URL базы данных.
    # PostgreSQL: postgresql+asyncpg://user:password@host:5432/vpn_db
    # SQLite:     sqlite+aiosqlite:///./vpn_bot.db
    database_url: str = "sqlite+aiosqlite:///./vpn_bot.db"

    # Прокси для подключения к Telegram API (опционально).
    # Нужен, если api.telegram.org недоступен напрямую.
    # Примеры:
    #   socks5://127.0.0.1:10808   (v2rayN)
    #   socks5://127.0.0.1:2080    (Nekoray/NekoBox)
    #   socks5://user:pass@host:1080
    proxy_url: str | None = None

    # Контакт поддержки (username без @)
    support_username: str = "vpn_support"

    # Telegram ID администраторов (через запятую в .env: ADMIN_IDS=123,456).
    # Только им доступны команды /servers, /add_server и т.п.
    admin_ids: str = ""

    # ── HostVDS (OpenStack API) — для автоаренды серверов ────────────────────
    # Данные из панели HostVDS: раздел API / OpenStack credentials.
    hostvds_auth_url: str = "https://api.hostvds.com:5000/v3"
    hostvds_username: str | None = None
    hostvds_password: str | None = None
    hostvds_project_name: str | None = None
    hostvds_domain_name: str = "default"

    # Тариф (flavor) и образ ОС для новых серверов — самые дешёвые значения
    # смотрите в панели HostVDS или через API (списки flavors/images).
    hostvds_flavor: str = "v1.nano"
    hostvds_image: str = "Ubuntu 24.04"

    # ── Учётные данные панелей 3x-ui на новых серверах ───────────────────────
    # Эти логин/пароль будут установлены на каждый новый сервер при провижининге
    xui_panel_port: int = 2053
    xui_panel_username: str = "admin"
    xui_panel_password: str | None = None

    # SNI (маскировка Reality) для новых инбаундов
    vless_sni: str = "yahoo.com"

    @property
    def admin_id_list(self) -> list[int]:
        """ADMIN_IDS из .env в виде списка чисел."""
        return [int(x) for x in self.admin_ids.replace(" ", "").split(",") if x]


@lru_cache
def get_settings() -> Settings:
    """Кешированный доступ к настройкам."""
    return Settings()


settings = get_settings()
