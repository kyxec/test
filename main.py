"""
main.py — FastAPI webhook + Super-Admin (/admin) + Company Portal (/portal)
"""
from __future__ import annotations
import os, logging, secrets, hashlib
from datetime import datetime, timedelta
from dotenv import load_dotenv

load_dotenv()

from fastapi import FastAPI, Request, Response, Depends, HTTPException, Form
from fastapi.responses import HTMLResponse, RedirectResponse, JSONResponse
from fastapi.templating import Jinja2Templates
import requests as _req
from sqlalchemy.orm import Session
from sqlalchemy import func

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

@app.get("/healthz")
async def healthz():
    return {"ok": True}

ADMIN_PASS = os.getenv("ADMIN_PASSWORD", "admin123")
_super_sessions: set[str] = set()
_co_sessions: dict[str, str] = {}   # token → company_id


# ── helpers ───────────────────────────────────────────────────────

def _hash_pwd(pwd: str) -> str:
    return hashlib.sha256(pwd.encode()).hexdigest()

def _verify_pwd(pwd: str, hashed: str) -> bool:
    return hashlib.sha256(pwd.encode()).hexdigest() == hashed


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


def get_company_by_phone_id(db: Session, phone_id: str) -> Company | None:
    return db.query(Company).filter_by(id=phone_id, active=True).first()


def _is_super(request: Request) -> bool:
    return request.cookies.get("admin_token") in _super_sessions


def _get_co_company(request: Request, db: Session) -> Company | None:
    token = request.cookies.get("co_token")
    if not token:
        return None
    company_id = _co_sessions.get(token)
    if not company_id:
        return None
    return db.query(Company).filter_by(id=company_id, active=True).first()


def get_stats(db: Session, company_id: str) -> dict:
    total_clients = db.query(Client).filter_by(company_id=company_id).count()
    blocked  = db.query(Client).filter_by(company_id=company_id, blocked=True).count()
    handoff  = db.query(Client).filter_by(company_id=company_id, handoff=True).count()
    total_msgs = db.query(Message).filter_by(company_id=company_id).count()
    today = datetime.utcnow().date()
    msgs_today = db.query(Message).filter(
        Message.company_id == company_id,
        func.date(Message.created_at) == today
    ).count()
    chart_labels, chart_data = [], []
    for i in range(6, -1, -1):
        d = (datetime.utcnow() - timedelta(days=i)).date()
        cnt = db.query(Message).filter(
            Message.company_id == company_id,
            Message.role == "user",
            func.date(Message.created_at) == d
        ).count()
        chart_labels.append(d.strftime("%d.%m"))
        chart_data.append(cnt)
    stages: dict[str, int] = {}
    for c in db.query(Client).filter_by(company_id=company_id).all():
        stages[c.stage] = stages.get(c.stage, 0) + 1
    return {
        "total_clients": total_clients, "blocked": blocked, "handoff": handoff,
        "total_msgs": total_msgs, "msgs_today": msgs_today,
        "chart_labels": chart_labels, "chart_data": chart_data, "stages": stages,
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
        log.info("[handoff] %s — пропущено (у менеджера)", phone)
        return {"ok": True}

    if FEATURE_HANDOFF and text.lower() in ("!человек", "!manager", "!human"):
        client.handoff = True
        db.commit()
        send_wa(phone, "Соединяю с менеджером, ожидайте.",
                company.wa_phone_id, company.wa_token)
        return {"ok": True}

    try:
        reply = get_reply(db, phone, company, text)
        log.info("[reply] %s → %s", phone, reply[:80])
    except Exception as e:
        log.error("[get_reply] Ошибка: %s", e)
        return {"ok": True}
    send_wa(phone, reply, company.wa_phone_id, company.wa_token)

    if FEATURE_FUNNEL and client.stage == "new":
        client.stage = "qualifying"
        db.commit()
        push_lead(phone, company.id, client.stage)

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
        token = secrets.token_hex(32)
        _super_sessions.add(token)
        r = RedirectResponse("/admin", status_code=302)
        r.set_cookie("admin_token", token, httponly=True)
        return r
    return templates.TemplateResponse(request=request, name="admin_login.html", context={"error": "Неверный пароль"})


@app.get("/admin/logout")
async def admin_logout(request: Request):
    _super_sessions.discard(request.cookies.get("admin_token", ""))
    r = RedirectResponse("/admin/login", status_code=302)
    r.delete_cookie("admin_token")
    return r


@app.get("/admin", response_class=HTMLResponse)
async def admin_index(request: Request, db: Session = Depends(get_db)):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    companies = db.query(Company).all()
    stats = {c.id: {
        "clients":  db.query(Client).filter_by(company_id=c.id).count(),
        "messages": db.query(Message).filter_by(company_id=c.id).count(),
    } for c in companies}
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
        persona: str = Form(""), knowledge: str = Form(""), active: str = Form("off"),
        login: str = Form(""), password: str = Form("")):
    if not _is_super(request):
        return RedirectResponse("/admin/login", status_code=302)
    if db.query(Company).filter_by(id=wa_phone_id).first():
        return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": None, "error": "Phone ID уже существует"})
    if login and db.query(Company).filter_by(login=login).first():
        return templates.TemplateResponse(request=request, name="super_company_form.html", context={"company": None, "error": "Логин уже занят"})
    co = Company(id=wa_phone_id, name=name, wa_phone_id=wa_phone_id, wa_token=wa_token,
                 persona=persona, knowledge=knowledge, active=(active == "on"),
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
        persona: str = Form(""), knowledge: str = Form(""), active: str = Form("off"),
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
    company.persona = persona
    company.knowledge = knowledge
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
        token = secrets.token_hex(32)
        _co_sessions[token] = company.id
        r = RedirectResponse("/portal", status_code=302)
        r.set_cookie("co_token", token, httponly=True)
        return r
    return templates.TemplateResponse(request=request, name="co_login.html", context={"error": "Неверный логин или пароль"})


@app.get("/portal/logout")
async def co_logout(request: Request):
    _co_sessions.pop(request.cookies.get("co_token", ""), None)
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
    return templates.TemplateResponse(request=request, name="co_dashboard.html", context={"company": company, "stats": stats, "recent_clients": recent, "active_page": "dashboard"})


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
        new_password: str = Form(""), current_password: str = Form("")):
    company = _get_co_company(request, db)
    if not company:
        return RedirectResponse("/portal/login", status_code=302)
    if new_password:
        if not _verify_pwd(current_password, company.password_hash or ""):
            return templates.TemplateResponse(request=request, name="co_settings.html", context={"company": company, "error": "Неверный текущий пароль", "success": None, "active_page": "settings"})
        company.password_hash = _hash_pwd(new_password)
    company.name = name
    company.wa_token = wa_token
    company.persona = persona
    company.knowledge = knowledge
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


@app.post("/portal/ai-suggest")
async def co_ai_suggest(request: Request, db: Session = Depends(get_db),
                         description: str = Form(...)):
    company = _get_co_company(request, db)
    if not company:
        return JSONResponse({"error": "Unauthorized"}, status_code=401)
    try:
        raw = _call_groq([
            {"role": "system",
             "content": "Ты эксперт по настройке AI-чатботов для бизнеса. Отвечай строго в нужном формате."},
            {"role": "user",
             "content": (
                 f"Создай для бизнеса:\n\n{description}\n\n"
                 "Строго в формате:\n"
                 "PERSONA:\n<3-5 предложений — системный промпт для AI-ассистента "
                 "от первого лица, приветливо>\n"
                 "KNOWLEDGE:\n<структурированная база знаний: режим работы, услуги, "
                 "цены, FAQ — всё что можно вывести из описания>"
             )},
        ])
        parts = raw.split("KNOWLEDGE:")
        persona = parts[0].replace("PERSONA:", "").strip()
        knowledge = parts[1].strip() if len(parts) > 1 else ""
        return JSONResponse({"persona": persona, "knowledge": knowledge})
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
