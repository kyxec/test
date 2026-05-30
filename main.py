"""
main.py — FastAPI webhook + Super-Admin (/admin) + Company Portal (/portal)
"""
from __future__ import annotations
import os, logging, secrets
from datetime import datetime, timedelta, timezone
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Response, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import jwt as _jwt
import requests as _req
from passlib.context import CryptContext
from sqlalchemy.orm import Session
from sqlalchemy import func, case

from models import init_db, get_db, Company, Client, Message, Summary
from config import WA_VERIFY, FEATURE_HANDOFF, FEATURE_FUNNEL
from ai    import get_reply, _call_groq
from spam  import is_spam
from crm   import push_lead

# ── init ──────────────────────────────────────────────────────────
logging.basicConfig(level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(message)s")
log = logging.getLogger(__name__)

init_db()

app = FastAPI(title="WP Bot")
templates = Jinja2Templates(directory="admin/templates")

# Выводим в лог чтобы можно было скопировать из Railway Logs
log.info("="*60)
log.info("JWT_SECRET (скопируй в Railway Variables): %s", JWT_SECRET)
log.info("="*60)

# ── Auth ──────────────────────────────────────────────────────────
ADMIN_PASS  = os.getenv("ADMIN_PASSWORD", "admin123")
JWT_SECRET  = os.getenv("JWT_SECRET", secrets.token_hex(32))  # Railway: задать явно
JWT_ALG     = "HS256"
JWT_TTL     = 60 * 60 * 24 * 7   # 7 дней

_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def _hash_pwd(pwd: str) -> str:
    return _pwd.hash(pwd)

def _verify_pwd(pwd: str, hashed: str) -> bool:
    # обратная совместимость со старыми SHA-256 хэшами
    import hashlib
    if len(hashed) == 64:   # SHA-256 hex
        return hashlib.sha256(pwd.encode()).hexdigest() == hashed
    return _pwd.verify(pwd, hashed)

def _make_jwt(payload: dict) -> str:
    data = {**payload, "exp": datetime.now(timezone.utc) + timedelta(seconds=JWT_TTL)}
    return _jwt.encode(data, JWT_SECRET, algorithm=JWT_ALG)

def _decode_jwt(token: str) -> dict | None:
    try:
        return _jwt.decode(token, JWT_SECRET, algorithms=[JWT_ALG])
    except Exception:
        return None


@app.get("/healthz")
async def healthz():
    return {"ok": True}

@app.get("/")
async def root():
    return RedirectResponse("/admin/login", status_code=302)


# ── WhatsApp helpers ──────────────────────────────────────────────

def send_wa(phone: str, text: str,
            wa_phone_id: str, wa_token: str) -> None:
    url = f"https://graph.facebook.com/v19.0/{wa_phone_id}/messages"
    try:
        r = _req.post(url,
                      headers={"Authorization": f"Bearer {wa_token}",
                               "Content-Type": "application/json"},
                      json={"messaging_product": "whatsapp",
                            "to": phone,
                            "type": "text",
                            "text": {"body": text}},
                      timeout=15)
        if r.status_code != 200:
            log.error("[send_wa] HTTP %s → %s", r.status_code, r.text[:300])
        else:
            log.info("[send_wa] OK → %s", phone)
    except Exception as e:
        log.error("[send_wa] Исключение: %s", e)


def send_telegram(tg_token: str, chat_id: str, text: str) -> None:
    """POST сообщение в Telegram. Используется для алертов менеджеру."""
    if not tg_token or not chat_id:
        return
    try:
        _req.post(
            f"https://api.telegram.org/bot{tg_token}/sendMessage",
            json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"},
            timeout=10
        )
    except Exception as e:
        log.error("[telegram] %s", e)


def get_company_by_phone_id(db: Session, phone_id: str) -> Company | None:
    return db.query(Company).filter_by(id=phone_id, active=True).first()


def _is_super(request: Request) -> bool:
    token = request.cookies.get("admin_token", "")
    data  = _decode_jwt(token)
    return bool(data and data.get("role") == "super")


def _get_co_company(request: Request, db: Session) -> Company | None:
    token = request.cookies.get("co_token", "")
    data  = _decode_jwt(token)
    if not data or data.get("role") != "company":
        return None
    return db.query(Company).filter_by(id=data["company_id"], active=True).first()


def get_stats(db: Session, company_id: str) -> dict:
    """Все метрики за один агрегированный запрос + 7-дневный чарт."""
    today = datetime.now(timezone.utc).date()

    # Метрики клиентов — один запрос
    row = db.query(
        func.count(Client.id).label("total"),
        func.sum(case((Client.blocked == True, 1), else_=0)).label("blocked"),
        func.sum(case((Client.handoff  == True, 1), else_=0)).label("handoff"),
    ).filter(Client.company_id == company_id).one()

    # Сообщения всего + сегодня — один запрос
    msg_row = db.query(
        func.count(Message.id).label("total"),
        func.sum(case((func.date(Message.created_at) == today, 1), else_=0)).label("today"),
    ).filter(Message.company_id == company_id).one()

    # 7-дневный чарт — один запрос с group by
    seven_days = [today - timedelta(days=i) for i in range(6, -1, -1)]
    chart_rows = db.query(
        func.date(Message.created_at).label("day"),
        func.count(Message.id).label("cnt"),
    ).filter(
        Message.company_id == company_id,
        Message.role == "user",
        func.date(Message.created_at) >= seven_days[0],
    ).group_by(func.date(Message.created_at)).all()
    chart_map = {str(r.day): r.cnt for r in chart_rows}

    chart_labels = [d.strftime("%d.%m") for d in seven_days]
    chart_data   = [chart_map.get(str(d), 0) for d in seven_days]

    # Стадии — один запрос
    stage_rows = db.query(
        Client.stage, func.count(Client.id)
    ).filter(Client.company_id == company_id).group_by(Client.stage).all()
    stages = {s: cnt for s, cnt in stage_rows}

    return {
        "total_clients": row.total   or 0,
        "blocked":       row.blocked or 0,
        "handoff":       row.handoff or 0,
        "total_msgs":    msg_row.total or 0,
        "msgs_today":    msg_row.today or 0,
        "chart_labels":  chart_labels,
        "chart_data":    chart_data,
        "stages":        stages,
    }


# ─────────────────────────────────────────────────────────────────
# Webhook
# ─────────────────────────────────────────────────────────────────

@app.get("/webhook")
async def verify(request: Request):
    params = request.query_params
    if params.get("hub.verify_token") == WA_VERIFY:
        return Response(content=params.get("hub.challenge", ""), media_type="text/plain")
    raise HTTPException(status_code=403, detail="Bad token")


@app.post("/webhook")
async def webhook(request: Request, db: Session = Depends(get_db)):
    body = await request.json()

    try:
        entry   = body["entry"][0]
        change  = entry["changes"][0]["value"]
        msg_obj = change["messages"][0]
        phone   = msg_obj["from"]
        text    = msg_obj.get("text", {}).get("body", "").strip()
        wa_pid  = change["metadata"]["phone_number_id"]
    except (KeyError, IndexError):
        return {"ok": True}

    if not text:
        return {"ok": True}

    company = get_company_by_phone_id(db, wa_pid)
    if not company:
        log.warning("Неизвестный phone_number_id: %s", wa_pid)
        return {"ok": True}

    if is_spam(phone):
        log.info("[spam] %s", phone)
        return {"ok": True}

    client = db.query(Client).filter_by(
        phone=phone, company_id=company.id).first()
    if not client:
        client = Client(phone=phone, company_id=company.id, stage="new")
        db.add(client)
        db.commit()

    if FEATURE_HANDOFF and client.handoff:
        # Секретная команда сброса — для тестирования
        if text.strip().lower() in ("!сброс", "!reset", "!bot", "!старт"):
            client.handoff = False
            client.lead_score = 0
            client.stage = "new"
            db.commit()
            send_wa(phone, "🤖 Бот снова активен! Начинаем сначала.",
                    company.wa_phone_id, company.wa_token)
            log.info("[reset] %s — handoff сброшен командой", phone)
        else:
            log.info("[handoff] %s — пропущено (у менеджера)", phone)
        return {"ok": True}

    if FEATURE_HANDOFF and text.lower() in ("!человек", "!manager", "!human"):
        client.handoff = True
        db.commit()
        send_wa(phone, "Соединяю с менеджером, ожидайте.",
                company.wa_phone_id, company.wa_token)
        return {"ok": True}

    try:
        reply, score, intent = get_reply(db, phone, company, text)
        log.info("[reply] %s score=%s intent=%s → %s", phone, score, intent, reply[:80])
    except Exception as e:
        log.error("[get_reply] Ошибка: %s", e)
        return {"ok": True}

    # — Сохранить score клиенту
    client.lead_score = score
    db.commit()

    # — Авто-handoff при горячем лиде
    threshold = company.hot_score if company.hot_score is not None else 8
    if FEATURE_HANDOFF and score >= threshold and not client.handoff:
        client.handoff = True
        db.commit()
        # Сообщить клиенту
        send_wa(phone,
                "Соединяю вас с менеджером, ожидайте — он уже видит ваш запрос. 🤝",
                company.wa_phone_id, company.wa_token)
        # Алерт в Telegram
        tg_msg = (
            f"🔥 <b>Горячий лид!</b> [score: {score}/10]\n\n"
            f"📱 <code>{phone}</code>\n"
            f"💬 Намерение: <b>{intent}</b>\n"
            f"✉️ Последнее: {text[:200]}\n\n"
            f"⏩ Откройте портал чтобы ответить"
        )
        send_telegram(company.tg_token or "", company.tg_chat_id or "", tg_msg)
        log.info("[handoff-auto] %s score=%s", phone, score)
        return {"ok": True}  # бот уже ответил при передаче

    send_wa(phone, reply, company.wa_phone_id, company.wa_token)

    # — Авто-прогресс стадии воронки по score
    if FEATURE_FUNNEL:
        new_stage = client.stage
        if score >= 8 and client.stage not in ("qualified", "customer"):
            new_stage = "qualified"
        elif score >= 5 and client.stage == "new":
            new_stage = "qualifying"
        if new_stage != client.stage:
            client.stage = new_stage
            db.commit()
            push_lead(phone, company.id, new_stage)

    return {"ok": True}


# ─────────────────────────────────────────────────────────────────
# Super Admin  /admin
# ─────────────────────────────────────────────────────────────────

@app.get("/admin/login", response_class=HTMLResponse)
async def admin_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"error": None})


@app.post("/admin/login")
async def admin_login(request: Request, password: str = Form(...)):
    if password == ADMIN_PASS:
        token = _make_jwt({"role": "super"})
        r = RedirectResponse("/admin", status_code=302)
        r.set_cookie("admin_token", token, httponly=True, max_age=JWT_TTL)
        return r
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"error": "Неверный пароль"})


@app.get("/admin/logout")
async def admin_logout(request: Request):
    r = RedirectResponse("/admin/login", status_code=302)
    r.delete_cookie("admin_token")
    return r


@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    companies = db.query(Company).all()
    # Один запрос вместо N*2
    client_counts  = {r[0]: r[1] for r in db.query(Client.company_id,  func.count(Client.id)).group_by(Client.company_id).all()}
    message_counts = {r[0]: r[1] for r in db.query(Message.company_id, func.count(Message.id)).group_by(Message.company_id).all()}
    stats = {c.id: {"clients": client_counts.get(c.id, 0), "messages": message_counts.get(c.id, 0)} for c in companies}
    return templates.TemplateResponse(request=request, name="super_index.html", context={"companies": companies, "stats": stats})


@app.get("/admin/company/new", response_class=HTMLResponse)
async def admin_company_new(request: Request):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": None, "error": None})


@app.post("/admin/company/new")
async def admin_company_create(
        request: Request, db: Session = Depends(get_db),
        name: str = Form(...), wa_phone_id: str = Form(...), wa_token: str = Form(...),
        active: str = Form("off"),
        login: str = Form(""), password: str = Form("")):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    if db.query(Company).filter_by(id=wa_phone_id).first():
        return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": None, "error": "Phone ID уже существует"})
    if login and db.query(Company).filter_by(login=login).first():
        return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": None, "error": "Логин уже занят"})
    co = Company(id=wa_phone_id, name=name, wa_phone_id=wa_phone_id, wa_token=wa_token,
                 active=(active == "on"),
                 login=login or None,
                 password_hash=_hash_pwd(password) if password else None)
    db.add(co)
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.get("/admin/company/{cid}/edit", response_class=HTMLResponse)
async def admin_company_edit(cid: str, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    company = db.query(Company).filter_by(id=cid).first()
    if not company:
        raise HTTPException(404)
    return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": company, "error": None})


@app.post("/admin/company/{cid}/edit")
async def admin_company_update(
        cid: str, request: Request, db: Session = Depends(get_db),
        name: str = Form(...), wa_token: str = Form(...),
        active: str = Form("off"),
        login: str = Form(""), password: str = Form("")):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    company = db.query(Company).filter_by(id=cid).first()
    if not company:
        raise HTTPException(404)
    if login:
        dup = db.query(Company).filter(Company.login == login, Company.id != cid).first()
        if dup:
            return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": company, "error": "Логин уже занят"})
    company.name = name
    company.wa_token = wa_token
    company.active = (active == "on")
    company.login = login or None
    if password:
        company.password_hash = _hash_pwd(password)
    db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.post("/admin/company/{cid}/delete")
async def admin_company_delete(cid: str, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    company = db.query(Company).filter_by(id=cid).first()
    if company:
        db.query(Client).filter_by(company_id=cid).delete()
        db.query(Message).filter_by(company_id=cid).delete()
        db.query(Summary).filter_by(company_id=cid).delete()
        db.delete(company)
        db.commit()
    return RedirectResponse("/admin", status_code=302)


@app.get("/admin/company/{cid}/clients", response_class=HTMLResponse)
async def admin_company_clients(cid: str, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    company = db.query(Company).filter_by(id=cid).first()
    if not company:
        raise HTTPException(404)
    clients = db.query(Client).filter_by(company_id=cid).order_by(Client.created_at.desc()).all()
    return templates.TemplateResponse(request=request, name="super_clients.html", context={"company": company, "clients": clients})


@app.post("/admin/client/{cid}/handoff-off")
async def super_handoff_off(cid: int, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    c = db.query(Client).filter_by(id=cid).first()
    if c:
        c.handoff = False; db.commit()
    return RedirectResponse(request.headers.get("referer", "/admin"), status_code=302)


@app.post("/admin/client/{cid}/unblock")
async def super_unblock(cid: int, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    c = db.query(Client).filter_by(id=cid).first()
    if c:
        c.blocked = False; db.commit()
    return RedirectResponse(request.headers.get("referer", "/admin"), status_code=302)


@app.post("/admin/client/{cid}/clear-history")
async def super_clear_history(cid: int, request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    c = db.query(Client).filter_by(id=cid).first()
    if c:
        db.query(Message).filter_by(phone=c.phone, company_id=c.company_id).delete()
        db.query(Summary).filter_by(phone=c.phone, company_id=c.company_id).delete()
        db.commit()
    return RedirectResponse(request.headers.get("referer", "/admin"), status_code=302)


# ─────────────────────────────────────────────────────────────────
# Company Portal  /portal
# ─────────────────────────────────────────────────────────────────

@app.get("/portal/login", response_class=HTMLResponse)
async def co_login_page(request: Request):
    return templates.TemplateResponse(request=request, name="co_login.html", context={"error": None})


@app.post("/portal/login")
async def co_login(request: Request, db: Session = Depends(get_db),
                   login: str = Form(...), password: str = Form(...)):
    company = db.query(Company).filter_by(login=login, active=True).first()
    if company and company.password_hash and _verify_pwd(password, company.password_hash):
        # Пересохранить с bcrypt если пароль был SHA-256
        import hashlib
        if len(company.password_hash) == 64:
            company.password_hash = _hash_pwd(password)
            db.commit()
        token = _make_jwt({"role": "company", "company_id": company.id})
        r = RedirectResponse("/portal", status_code=302)
        r.set_cookie("co_token", token, httponly=True, max_age=JWT_TTL)
        return r
    return templates.TemplateResponse(request=request, name="co_login.html", context={"error": "Неверный логин или пароль"})


@app.get("/portal/logout")
async def co_logout(request: Request):
    r = RedirectResponse("/portal/login", status_code=302)
    r.delete_cookie("co_token")
    return r


@app.get("/portal", response_class=HTMLResponse)
async def co_dashboard(request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    stats = get_stats(db, company.id)
    recent = (db.query(Client).filter_by(company_id=company.id)
              .order_by(Client.created_at.desc()).limit(5).all())
    handoff_clients = (db.query(Client).filter_by(company_id=company.id, handoff=True).all())
    return templates.TemplateResponse(request=request, name="co_dashboard.html", context={"company": company, "stats": stats, "recent_clients": recent, "handoff_clients": handoff_clients, "active_page": "dashboard"})


@app.get("/portal/clients", response_class=HTMLResponse)
async def co_clients(request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    clients = (db.query(Client).filter_by(company_id=company.id)
               .order_by(Client.created_at.desc()).all())
    return templates.TemplateResponse(request=request, name="co_clients.html", context={"company": company, "clients": clients, "active_page": "clients"})


@app.get("/portal/settings", response_class=HTMLResponse)
async def co_settings(request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    return templates.TemplateResponse(request=request, name="co_settings.html", context={"company": company, "success": None, "error": None, "active_page": "settings"})


@app.post("/portal/settings")
async def co_settings_save(
        request: Request, db: Session = Depends(get_db),
        name: str = Form(...), wa_token: str = Form(...),
        persona: str = Form(""), knowledge: str = Form(""),
        tg_token: str = Form(""), tg_chat_id: str = Form(""),
        hot_score: int = Form(8), strict_mode: str = Form("off"),
        new_password: str = Form(""), current_password: str = Form("")):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    if new_password:
        if not _verify_pwd(current_password, company.password_hash or ""):
            # Вернуть форму с введёнными значениями (не из БД)
            company.name       = name
            company.wa_token   = wa_token
            company.persona    = persona
            company.knowledge  = knowledge
            company.tg_token   = tg_token
            company.tg_chat_id = tg_chat_id
            company.hot_score  = max(1, min(10, hot_score))
            company.strict_mode = (strict_mode == "on")
            db.expunge(company)  # не коммитить — только для рендера
            return templates.TemplateResponse(request=request, name="co_settings.html", context={"company": company, "error": "Неверный текущий пароль", "success": None, "active_page": "settings"})
        company.password_hash = _hash_pwd(new_password)
    company.name       = name
    company.wa_token   = wa_token
    company.persona    = persona
    company.knowledge  = knowledge
    company.tg_token   = tg_token
    company.tg_chat_id = tg_chat_id
    company.hot_score  = max(1, min(10, hot_score))
    company.strict_mode = (strict_mode == "on")
    db.commit()
    return templates.TemplateResponse(request=request, name="co_settings.html", context={"company": company, "success": "✅ Настройки сохранены!", "error": None, "active_page": "settings"})


@app.post("/portal/client/{cid}/handoff-off")
async def co_handoff_off(cid: int, request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    c = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if c:
        c.handoff = False; db.commit()
    return RedirectResponse("/portal/clients", status_code=302)


@app.post("/portal/client/{cid}/block")
async def co_block(cid: int, request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    c = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if c:
        c.blocked = True; db.commit()
    return RedirectResponse("/portal/clients", status_code=302)


@app.post("/portal/client/{cid}/unblock")
async def co_unblock(cid: int, request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    c = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if c:
        c.blocked = False; db.commit()
    return RedirectResponse("/portal/clients", status_code=302)


@app.post("/portal/client/{cid}/clear-history")
async def co_clear_history(cid: int, request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    c = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if c:
        db.query(Message).filter_by(phone=c.phone, company_id=c.company_id).delete()
        db.query(Summary).filter_by(phone=c.phone, company_id=c.company_id).delete()
        db.commit()
    return RedirectResponse("/portal/clients", status_code=302)


@app.post("/portal/client/{cid}/send")
async def co_client_send(cid: int, request: Request, db: Session = Depends(get_db),
                          text: str = Form(...)):
    """\u041cенеджер отправляет сообщение клиенту из портала."""
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    client = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if not client:
        raise HTTPException(404)
    text = text.strip()
    if text:
        send_wa(client.phone, text, company.wa_phone_id, company.wa_token)
        db.add(Message(phone=client.phone, company_id=company.id,
                       role="assistant", content=f"[\u041cенеджер] {text}"))
        db.commit()
    return RedirectResponse(f"/portal/client/{cid}/chat", status_code=302)


@app.get("/portal/client/{cid}/chat")
async def co_client_chat(cid: int, request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    client = db.query(Client).filter_by(id=cid, company_id=company.id).first()
    if not client:
        raise HTTPException(404)
    messages = (db.query(Message)
                .filter_by(phone=client.phone, company_id=company.id)
                .order_by(Message.created_at)
                .all())
    return templates.TemplateResponse(request=request, name="co_chat.html", context={
        "company": company, "client": client, "messages": messages, "active_page": "clients"
    })


@app.get("/portal/test-chat")
async def co_testchat_get(request: Request, db: Session = Depends(get_db)):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    return templates.TemplateResponse(request=request, name="co_testchat.html", context={
        "company": company, "active_page": "testchat", "history": []
    })


@app.post("/portal/test-chat")
async def co_testchat_post(request: Request, db: Session = Depends(get_db),
                            message: str = Form(...),
                            history_json: str = Form("[]")):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)

    import json as _json, re
    from ai import _REPLY_FORMAT, _STRICT_SUFFIX, _parse_bot_response

    # Восстановить историю из скрытого поля
    try:
        history: list[dict] = _json.loads(history_json)
    except Exception:
        history = []

    # Собрать контекст точно как в реальном боте
    persona   = company.persona   or "Ты — вежливый помощник компании."
    knowledge = company.knowledge or ""
    system_msg = persona
    if knowledge:
        system_msg += f"\n\nБаза знаний:\n{knowledge}"
    if getattr(company, "strict_mode", True):
        system_msg += _STRICT_SUFFIX
    system_msg += _REPLY_FORMAT

    messages_ctx = [{"role": "system", "content": system_msg}]
    for h in history[-10:]:   # последние 10 пар для экономии токенов
        messages_ctx.append({"role": h["role"], "content": h["content"]})
    messages_ctx.append({"role": "user", "content": message})

    try:
        raw = _call_groq(messages_ctx)
        ai_reply, score, intent = _parse_bot_response(raw)
    except Exception as e:
        ai_reply, score, intent = f"Ошибка: {e}", 0, "error"

    # Добавить в историю
    history.append({"role": "user",      "content": message})
    history.append({"role": "assistant", "content": ai_reply, "score": score, "intent": intent})

    return templates.TemplateResponse(request=request, name="co_testchat.html", context={
        "company": company, "active_page": "testchat",
        "history": history, "history_json": _json.dumps(history, ensure_ascii=False),
        "last_score": score, "last_intent": intent,
    })


@app.get("/portal/api/handoff-count")
async def co_handoff_count(request: Request, db: Session = Depends(get_db)):
    """Лёгкий endpoint для polling — возвращает кол-во ожидающих менеджера."""
    company = _get_co_company(request, db)
    if not company:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    count = db.query(Client).filter_by(company_id=company.id, handoff=True).count()
    return {"handoff_count": count}


@app.post("/portal/api/tg-test")
async def co_tg_test(request: Request, db: Session = Depends(get_db)):
    """Тестовое сообщение в Telegram — проверка настройки."""
    company = _get_co_company(request, db)
    if not company:
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not company.tg_token or not company.tg_chat_id:
        return JSONResponse({"error": "Заполните Telegram Bot Token и Chat ID перед тестом"}, status_code=400)
    try:
        r = _req.post(
            f"https://api.telegram.org/bot{company.tg_token}/sendMessage",
            json={"chat_id": company.tg_chat_id,
                  "text": f"✅ <b>Тест уведомлений</b>\n\n"
                          f"Бот <b>{company.name}</b> успешно подключён.\n"
                          f"Горячие лиды (score ≥ {company.hot_score or 8}) будут приходить сюда 🔥",
                  "parse_mode": "HTML"},
            timeout=10
        )
        r.raise_for_status()
        return {"ok": True}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/portal/api/tg-getid")
async def co_tg_getid(request: Request, tg_token: str):
    """Получить chat_id из последних updates Telegram бота."""
    company = _get_co_company(request, None)  # только auth check
    token = request.cookies.get("co_token", "")
    if not (_decode_jwt(token) or {}).get("role") == "company":
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    if not tg_token:
        return JSONResponse({"error": "tg_token пустой"}, status_code=400)
    try:
        r = _req.get(f"https://api.telegram.org/bot{tg_token}/getUpdates", timeout=10)
        r.raise_for_status()
        updates = r.json().get("result", [])
        if not updates:
            return JSONResponse({"error": "Сначала напишите что-нибудь вашему боту в Telegram, затем повторите"})
        chat_id = str(updates[-1]["message"]["chat"]["id"])
        return {"chat_id": chat_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


async def co_ai_suggest(request: Request, db: Session = Depends(get_db),
                         description: str = Form(...)):
    company = _get_co_company(request, db)
    if not company:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        raw = _call_groq([
            {"role": "system",
             "content": "Ты эксперт по настройке AI-чатботов. Отвечай исключительно в JSON без markdown."},
            {"role": "user",
             "content": (
                 f"Создай настройку бота для бизнеса:\n\n{description}\n\n"
                 'Формат ответа (JSON):\n'
                 '{"persona":"<системный промпт 3-5 предложений>",'
                 '"knowledge":"<база знаний: режим, услуги, цены, FAQ>"} '
             )},
        ])
        import re, json as _json
        cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
        data = _json.loads(cleaned)
        return JSONResponse({"persona": data.get("persona", ""), "knowledge": data.get("knowledge", "")})
    except Exception as e:
        log.error("[ai-suggest] %s", e, exc_info=True)
        return JSONResponse({"error": str(e)}, status_code=500)
