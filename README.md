# WP Bot

WhatsApp AI-бот на FastAPI + Groq + SQLAlchemy с мульти-тенантной архитектурой.

## Структура

```
main.py        — FastAPI: webhook + /admin (супер-админ) + /portal (владелец)
models.py      — SQLAlchemy модели (SQLite локально, PostgreSQL на Railway)
ai.py          — Groq LLM + память + суммаризация
config.py      — настройки из .env
spam.py        — rate-limiting
crm.py         — заглушка интеграции с CRM
seed.py        — добавить/обновить компанию вручную (только локально)
```

## Локальный запуск

```bash
# 1. Создать .env из .env.example и заполнить
cp .env.example .env

# 2. Установить зависимости
pip install -r requirements.txt

# 3. Запустить
python -m uvicorn main:app --host 0.0.0.0 --port 8000
# или двойной клик на start.bat
```

Открыть в браузере:
- http://localhost:8000/admin/login — пароль из `ADMIN_PASSWORD`
- http://localhost:8000/portal/login — логин/пароль задаётся при создании компании

## Деплой на Railway

### 1. Создай аккаунт и проект

- Зайди на [railway.app](https://railway.app) → New Project → Deploy from GitHub repo
- Подключи GitHub, загрузи туда папку проекта

### 2. Добавь PostgreSQL

В проекте Railway: **New** → **Database** → **Add PostgreSQL**  
Railway автоматически добавит `DATABASE_URL` в переменные окружения.

### 3. Добавь переменные окружения

В Railway → твой сервис → **Variables** добавь:

```
WA_TOKEN=EAAL...
WA_PHONE_ID=1234567890123456
WA_VERIFY=любая_строка
GROQ_KEY=gsk_...
ADMIN_PASSWORD=придумай_пароль
```

`DATABASE_URL` Railway добавит сам — не трогай.

### 4. Деплой произойдёт автоматически

После деплоя Railway выдаст домен вида `your-app.up.railway.app`.

### 5. Настрой webhook в Meta

В Meta Developer Console → WhatsApp → Configuration:
- Callback URL: `https://your-app.up.railway.app/webhook`
- Verify Token: значение `WA_VERIFY` из .env

### 6. Создай первую компанию

Открой `https://your-app.up.railway.app/admin/login` и добавь компанию через супер-админку.  
`seed.py` на Railway не нужен — всё делается через UI.

## Переменные окружения

| Переменная | Обязательная | Описание |
|---|---|---|
| `WA_TOKEN` | ✅ | WhatsApp Access Token из Meta |
| `WA_PHONE_ID` | ✅ | Phone Number ID из Meta |
| `WA_VERIFY` | ✅ | Любая строка для верификации webhook |
| `GROQ_KEY` | ✅ | API ключ Groq |
| `ADMIN_PASSWORD` | ✅ | Пароль супер-админа |
| `DATABASE_URL` | Railway | Автоматически от PostgreSQL плагина |
| `FEATURE_MEMORY` | — | Память клиента (по умолчанию true) |
| `FEATURE_HANDOFF` | — | Передача менеджеру (по умолчанию true) |
