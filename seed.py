# -*- coding: utf-8 -*-
"""
seed.py — добавить/обновить компанию в bot.db из данных .env
Запускать ОДИН раз перед первым стартом (или когда нужно обновить токен).

Использование:
    python seed.py
"""
import os, hashlib
from dotenv import load_dotenv

load_dotenv()

from models import init_db, Session, Company

# ── данные компании ────────────────────────────────────────────────
WA_PHONE_ID = os.environ["WA_PHONE_ID"]
WA_TOKEN    = os.environ["WA_TOKEN"]

COMPANY_NAME = "Алгоритмика"   # ← можно поменять
PORTAL_LOGIN    = os.getenv("PORTAL_LOGIN",    "algoritmika")   # логин для /portal
PORTAL_PASSWORD = os.getenv("PORTAL_PASSWORD", "portal123")     # пароль для /portal

PERSONA = """\
Ты — дружелюбный менеджер по продажам образовательного центра Алгоритмика.
Отвечай на русском, кратко и по делу.
Не придумывай информацию, которой нет в базе знаний.
Если клиент хочет записаться — предложи оставить номер телефона для обратного звонка.\
"""

KNOWLEDGE = """\
Компания: Алгоритмика
Направление: курсы программирования для детей 6–17 лет
Языки: Scratch, Python, веб-разработка, мобильные приложения
Цена: от 5 000 руб/мес
Пробное занятие: бесплатно
Сайт: algoritmika.org
Режим работы: пн–пт 9:00–18:00, сб 10:00–15:00\
"""

# ── запись в БД ───────────────────────────────────────────────────
def main():
    init_db()
    db = Session()

    existing = db.query(Company).get(WA_PHONE_ID)

    if existing:
        print(f"[seed] Компания {WA_PHONE_ID!r} уже есть — обновляю токен и активирую.")
        existing.wa_token   = WA_TOKEN
        existing.active     = True
        if not existing.login:
            existing.login = PORTAL_LOGIN
            existing.password_hash = hashlib.sha256(PORTAL_PASSWORD.encode()).hexdigest()
            print(f"[seed] Установлен портал-логин: {PORTAL_LOGIN} / {PORTAL_PASSWORD}")
    else:
        print(f"[seed] Создаю компанию {COMPANY_NAME!r} с ID={WA_PHONE_ID}")
        c = Company(
            id=WA_PHONE_ID,
            name=COMPANY_NAME,
            wa_phone_id=WA_PHONE_ID,
            wa_token=WA_TOKEN,
            persona=PERSONA,
            knowledge=KNOWLEDGE,
            active=True,
            login=PORTAL_LOGIN,
            password_hash=hashlib.sha256(PORTAL_PASSWORD.encode()).hexdigest(),
        )
        db.add(c)

    db.commit()
    db.close()

    print()
    print("=" * 50)
    print(f"  Готово! Компания '{COMPANY_NAME}' в БД.")
    print(f"  Phone Number ID : {WA_PHONE_ID}")
    print()
    print("  Супер-Администратор:")
    print("  http://localhost:8000/admin")
    print(f"  Пароль: {os.getenv('ADMIN_PASSWORD','admin123')}")
    print()
    print("  Портал владельца бизнеса:")
    print("  http://localhost:8000/portal/login")
    print(f"  Логин: {PORTAL_LOGIN} / Пароль: {PORTAL_PASSWORD}")
    print("=" * 50)


if __name__ == "__main__":
    main()
