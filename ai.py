"""
ai.py — Groq LLM + memory (SQLAlchemy) + summarization
"""
from __future__ import annotations
import requests
from sqlalchemy.orm import Session

from config import (GROQ_KEY, LLM_MODEL, LLM_MAX_TOKENS,
                    FEATURE_MEMORY, MEMORY_WINDOW, MEMORY_KEEP)
from models import Message, Summary, Company


GROQ_URL = "https://api.groq.com/openai/v1/chat/completions"


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
              user_text: str) -> str:
    """
    Основной вход: принять сообщение пользователя → вернуть ответ бота.
    Память и summarization управляются флагами из config.
    """
    company_id = company.id

    # 1. Сохранить входящее сообщение
    if FEATURE_MEMORY:
        db.add(Message(phone=phone, company_id=company_id,
                       role="user", content=user_text))
        db.commit()

    # 2. Построить system-prompt
    persona    = company.persona    or "Ты — вежливый помощник."
    knowledge  = company.knowledge  or ""
    system_msg = persona
    if knowledge:
        system_msg += f"\n\nБаза знаний:\n{knowledge}"

    # 3. Собрать историю
    history: list[dict] = []
    if FEATURE_MEMORY:
        # сначала старое резюме (если есть)
        summary = db.query(Summary).filter_by(
            phone=phone, company_id=company_id).first()
        if summary:
            history.append({"role": "user",
                             "content": f"[Резюме предыдущего разговора: {summary.content}]"})
            history.append({"role": "assistant", "content": "Понял, продолжаем."})

        # последние сообщения
        recent = (db.query(Message)
                    .filter_by(phone=phone, company_id=company_id)
                    .order_by(Message.id)
                    .all())
        for m in recent:
            history.append({"role": m.role, "content": m.content})
    else:
        history.append({"role": "user", "content": user_text})

    # 4. Вызов LLM
    reply = _call_groq([{"role": "system", "content": system_msg}] + history)

    # 5. Сохранить ответ
    if FEATURE_MEMORY:
        db.add(Message(phone=phone, company_id=company_id,
                       role="assistant", content=reply))
        db.commit()

        # Проверить нужна ли компрессия
        count = (db.query(Message)
                   .filter_by(phone=phone, company_id=company_id)
                   .count())
        if count >= MEMORY_WINDOW:
            _summarize(db, phone, company_id)

    return reply
