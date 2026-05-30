"""
ai.py — Groq LLM + memory (SQLAlchemy) + summarization
Один LLM-вызов возвращает reply + lead_score + intent (экономия токенов).
"""
from __future__ import annotations
import json, re, logging
import requests
from sqlalchemy.orm import Session

from config import (GROQ_KEY, LLM_MODEL, LLM_MAX_TOKENS,
                    FEATURE_MEMORY, MEMORY_WINDOW, MEMORY_KEEP)
from models import Message, Summary, Company

log = logging.getLogger(__name__)
GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"

# Структура ответа бота: чтобы извлечь из JSON не нужен отдельный вызов
_REPLY_FORMAT = """
ВАЖНО: отвечай СТРОГО в формате JSON (без markdown, без ```):
{"reply":"<твой ответ клиенту>","score":<число 1-10>,"intent":"<одно слово>"}

score — насколько горячий лид (1=холодный, 10=готов купить прямо сейчас).
intent — одно из: greeting / question / price / booking / complaint / objection / ready_to_buy / other
"""

_STRICT_SUFFIX = """
ПРАВИЛО БЕЗОПАСНОСТИ: отвечай ТОЛЬКО на основе базы знаний выше.
Если информации нет — reply должен быть: "Уточню у менеджера и вернусь к вам ✋"
Тогда intent="no_info" и score оставь текущий.
ЗАПРЕЩЕНО придумывать цены, даты, адреса, условия."""


# ── helpers ───────────────────────────────────────────────────────

def _call_groq(messages: list[dict]) -> str:
    resp = requests.post(
        GROQ_URL,
        headers={"Authorization": f"Bearer {GROQ_KEY}",
                 "Content-Type": "application/json"},
        json={"model": LLM_MODEL, "max_tokens": LLM_MAX_TOKENS,
              "messages": messages},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()["choices"][0]["message"]["content"].strip()


def _parse_bot_response(raw: str) -> tuple[str, int, str]:
    """Распарсить JSON-ответ бота. Fallback — вернуть текст как reply."""
    # Бывает что LLM обворачивает в ```json ... ```, чистим
    cleaned = re.sub(r"```[a-z]*\n?", "", raw).strip()
    try:
        data = json.loads(cleaned)
        reply  = str(data.get("reply", raw))
        score  = max(1, min(10, int(data.get("score", 1))))
        intent = str(data.get("intent", "other"))
        return reply, score, intent
    except Exception:
        log.warning("[ai] Не удалось распарсить JSON: %s", raw[:120])
        return raw, 1, "other"


def _summarize(db: Session, phone: str, company_id: str) -> None:
    """Сжать старые сообщения в одно резюме."""
    msgs = (db.query(Message)
              .filter_by(phone=phone, company_id=company_id)
              .order_by(Message.id)
              .all())
    if len(msgs) < MEMORY_WINDOW:
        return

    # берём всё кроме последних MEMORY_KEEP — это и сожмём
    to_compress = msgs[:-MEMORY_KEEP]
    history_text = "\n".join(f"{m.role}: {m.content}" for m in to_compress)

    summary_text = _call_groq([
        {"role": "system",
         "content": "Кратко перескажи эту переписку в 3–5 предложениях на русском."},
        {"role": "user", "content": history_text},
    ])

    # сохранить / обновить summary
    row = db.query(Summary).filter_by(phone=phone, company_id=company_id).first()
    if row:
        row.content = summary_text
    else:
        db.add(Summary(phone=phone, company_id=company_id, content=summary_text))

    # удалить сжатые сообщения
    for m in to_compress:
        db.delete(m)

    db.commit()


# ── public API ────────────────────────────────────────────────────

def get_reply(db: Session, phone: str, company: Company,
              user_text: str) -> tuple[str, int, str]:
    """
    Принять сообщение → вернуть (reply, lead_score, intent).
    Один LLM-вызов: ответ + аналитика одновременно.
    """
    company_id = company.id

    # 1. Сохранить входящее
    if FEATURE_MEMORY:
        db.add(Message(phone=phone, company_id=company_id,
                       role="user", content=user_text))
        db.commit()

    # 2. System prompt: персона + база знаний + строгий режим + формат
    persona   = company.persona   or "Ты — вежливый и полезный помощник компании."
    knowledge = company.knowledge or ""
    system_msg = persona
    if knowledge:
        system_msg += f"\n\nБаза знаний:\n{knowledge}"
    if getattr(company, "strict_mode", True):
        system_msg += _STRICT_SUFFIX
    system_msg += _REPLY_FORMAT

    # 3. История (sliding window + summary)
    history: list[dict] = []
    if FEATURE_MEMORY:
        summary = db.query(Summary).filter_by(
            phone=phone, company_id=company_id).first()
        if summary:
            history.append({"role": "user",
                             "content": f"[Резюме предыдущего разговора: {summary.content}]"})
            history.append({"role": "assistant", "content": "Понял, продолжаем."})
        recent = (db.query(Message)
                    .filter_by(phone=phone, company_id=company_id)
                    .order_by(Message.id).all())
        for m in recent:
            history.append({"role": m.role, "content": m.content})
    else:
        history.append({"role": "user", "content": user_text})

    # 4. Вызов LLM
    raw = _call_groq([{"role": "system", "content": system_msg}] + history)
    reply, score, intent = _parse_bot_response(raw)

    # 5. Сохранить ответ
    if FEATURE_MEMORY:
        db.add(Message(phone=phone, company_id=company_id,
                       role="assistant", content=reply))
        db.commit()
        count = (db.query(Message)
                   .filter_by(phone=phone, company_id=company_id).count())
        if count >= MEMORY_WINDOW:
            _summarize(db, phone, company_id)

    return reply, score, intent
