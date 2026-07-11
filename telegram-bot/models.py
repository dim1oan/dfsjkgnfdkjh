"""
Модели базы данных (SQLAlchemy 2.0, declarative + typed mapping).

Таблица `users` спроектирована так, чтобы её можно было использовать
совместно с FastAPI веб-приложением (общая PostgreSQL-база).
"""

from datetime import datetime
from typing import Optional

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    """Базовый класс всех моделей."""


class Server(Base):
    """
    VPN-сервер с панелью 3x-ui.

    Схема «общий сервер на страну»: один сервер обслуживает много клиентов,
    новые клиенты добавляются через API 3x-ui мгновенно.
    """

    __tablename__ = "servers"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Код страны (ISO, нижний регистр): "nl", "de", "us"…
    country_code: Mapped[str] = mapped_column(String(8), index=True, nullable=False)
    # Название страны для кнопок: "Нидерланды"
    country_name: Mapped[str] = mapped_column(String(64), nullable=False)
    # Эмодзи-флаг: "🇳🇱"
    flag: Mapped[str] = mapped_column(String(8), nullable=False, default="🌍")

    # Публичный IP сервера
    ip: Mapped[str] = mapped_column(String(64), nullable=False)

    # Панель 3x-ui
    panel_url: Mapped[str] = mapped_column(String(256), nullable=False)  # http://ip:2053
    panel_username: Mapped[str] = mapped_column(String(64), nullable=False)
    panel_password: Mapped[str] = mapped_column(String(128), nullable=False)

    # ID VLESS/Reality-инбаунда в панели, куда добавляются клиенты
    inbound_id: Mapped[Optional[int]] = mapped_column(Integer, nullable=True)

    # Параметры Reality для сборки клиентских ссылок
    vless_port: Mapped[int] = mapped_column(Integer, nullable=False, default=443)
    public_key: Mapped[Optional[str]] = mapped_column(String(128), nullable=True)
    sni: Mapped[str] = mapped_column(String(128), nullable=False, default="yahoo.com")
    short_id: Mapped[Optional[str]] = mapped_column(String(32), nullable=True)

    # Доступен ли сервер для продажи
    is_active: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)

    # Лимит клиентов на сервер (для будущего балансирования)
    max_clients: Mapped[int] = mapped_column(Integer, nullable=False, default=200)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    def __repr__(self) -> str:
        return f"<Server {self.country_code} ip={self.ip} active={self.is_active}>"


class User(Base):
    """Пользователь VPN-сервиса."""

    __tablename__ = "users"

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)

    # Telegram ID — уникальный идентификатор пользователя в Telegram
    telegram_id: Mapped[int] = mapped_column(
        BigInteger, unique=True, index=True, nullable=False
    )

    username: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Баланс в рублях
    balance: Mapped[float] = mapped_column(
        Numeric(10, 2), nullable=False, default=0
    )

    # Дата окончания подписки (None — подписки нет)
    subscription_expires_at: Mapped[Optional[datetime]] = mapped_column(
        DateTime(timezone=True), nullable=True
    )

    # Персональная VLESS-ссылка подключения
    vless_link: Mapped[Optional[str]] = mapped_column(String(512), nullable=True)

    # Сервер, на котором создан клиент пользователя
    server_id: Mapped[Optional[int]] = mapped_column(
        ForeignKey("servers.id"), nullable=True
    )

    # UUID клиента в панели 3x-ui (нужен для продления/удаления)
    xui_client_uuid: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    # Email-идентификатор клиента в 3x-ui (уникален в рамках инбаунда)
    xui_email: Mapped[Optional[str]] = mapped_column(String(64), nullable=True)

    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )

    @property
    def has_active_subscription(self) -> bool:
        """Активна ли подписка на текущий момент."""
        if self.subscription_expires_at is None:
            return False
        expires = self.subscription_expires_at
        now = datetime.now(tz=expires.tzinfo) if expires.tzinfo else datetime.utcnow()
        return expires > now

    def __repr__(self) -> str:
        return f"<User telegram_id={self.telegram_id} username={self.username}>"
