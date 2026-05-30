from datetime import datetime, timezone
import os
from sqlalchemy import create_engine, Column, String, Text, DateTime, Integer, Boolean, ForeignKey, text
from sqlalchemy.orm import declarative_base, sessionmaker

Base = declarative_base()

# Railway даёт DATABASE_URL=postgresql://..., локально падаем на SQLite
_db_url = os.getenv("DATABASE_URL", "sqlite:///bot.db")
# Railway старые планы отдают postgres://, SQLAlchemy требует postgresql://
if _db_url.startswith("postgres://"):
    _db_url = _db_url.replace("postgres://", "postgresql://", 1)

_connect_args = {"check_same_thread": False} if _db_url.startswith("sqlite") else {}
engine = create_engine(_db_url, connect_args=_connect_args)
Session = sessionmaker(bind=engine)


class Company(Base):
    """Одна запись = одна компания-клиент (у каждой свой WhatsApp номер)."""
    __tablename__ = "companies"

    id            = Column(String, primary_key=True)   # = phone_number_id из Meta
    name          = Column(String, nullable=False)
    wa_phone_id   = Column(String, default="")
    wa_token      = Column(Text, nullable=False)
    persona       = Column(Text, default="")
    knowledge     = Column(Text, default="")
    active        = Column(Boolean, default=True)
    login         = Column(String, unique=True, nullable=True)   # логин владельца
    password_hash = Column(String, nullable=True)                # SHA-256
    # Уведомления и поведение бота
    tg_token      = Column(String, default="")   # Telegram Bot Token для алертов
    tg_chat_id    = Column(String, default="")   # Telegram chat_id менеджера
    hot_score     = Column(Integer, default=8)   # порог горячего лида (1-10)
    strict_mode   = Column(Boolean, default=True) # бот молчит о том чего не знает


class Client(Base):
    """Один клиент = (phone, company_id) — уникальная пара."""
    __tablename__ = "clients"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    phone       = Column(String, nullable=False)
    company_id  = Column(String, ForeignKey("companies.id"), nullable=False)
    name        = Column(String, default="")
    stage       = Column(String, default="new")       # new / qualifying / qualified / customer
    crm_id      = Column(String, default="")          # ID в CRM после передачи
    blocked     = Column(Boolean, default=False)      # спам-блок
    handoff     = Column(Boolean, default=False)      # передан менеджеру — бот молчит
    lead_score  = Column(Integer, default=0)          # 1-10 горячесть лида
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Message(Base):
    """История переписки."""
    __tablename__ = "messages"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    phone       = Column(String, nullable=False)
    company_id  = Column(String, ForeignKey("companies.id"), nullable=False)
    role        = Column(String, nullable=False)      # "user" | "assistant"
    content     = Column(Text, nullable=False)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


class Summary(Base):
    """Сжатая память — заменяет старые сообщения чтобы не раздувать контекст."""
    __tablename__ = "summaries"

    id          = Column(Integer, primary_key=True, autoincrement=True)
    phone       = Column(String, nullable=False)
    company_id  = Column(String, ForeignKey("companies.id"), nullable=False)
    content     = Column(Text, nullable=False)
    created_at  = Column(DateTime, default=lambda: datetime.now(timezone.utc))


def init_db():
    Base.metadata.create_all(engine)
    is_pg = not _db_url.startswith("sqlite")
    # Миграция: добавляем новые колонки если их нет
    # Каждая попытка — отдельная транзакция, чтобы ошибка одной не ломала следующие
    migrations = [
        # (table, column, sqlite_ddl, pg_ddl)
        ("companies", "login",         "TEXT",            "TEXT"),
        ("companies", "password_hash", "TEXT",            "TEXT"),
        ("companies", "tg_token",      "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
        ("companies", "tg_chat_id",    "TEXT DEFAULT ''", "TEXT DEFAULT ''"),
        ("companies", "hot_score",     "INTEGER DEFAULT 8",  "INTEGER DEFAULT 8"),
        ("companies", "strict_mode",   "BOOLEAN DEFAULT 1",  "BOOLEAN DEFAULT TRUE"),
        ("clients",   "lead_score",    "INTEGER DEFAULT 0",  "INTEGER DEFAULT 0"),
    ]
    for table, col, sqlite_ddl, pg_ddl in migrations:
        ddl = pg_ddl if is_pg else sqlite_ddl
        with engine.connect() as conn:
            try:
                conn.execute(text(f"ALTER TABLE {table} ADD COLUMN {col} {ddl}"))
                conn.commit()
            except Exception:
                # Колонка уже существует — это нормально, игнорируем
                conn.rollback()


def get_db():
    """FastAPI dependency — сессия на один запрос."""
    db = Session()
    try:
        yield db
    finally:
        db.close()
