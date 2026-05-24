"""
crm.py — интеграция с CRM.
Когда FEATURE_CRM=false — просто логируем, не отправляем.
Когда FEATURE_CRM=true  — POST на CRM_WEBHOOK_URL.
"""
from __future__ import annotations
import logging
import requests

from config import FEATURE_CRM, CRM_WEBHOOK_URL

log = logging.getLogger(__name__)


def push_lead(phone: str, company_id: str, stage: str, summary: str = "") -> None:
    """Отправить/обновить лид в CRM."""
    if not FEATURE_CRM:
        log.debug("[CRM stub] phone=%s stage=%s", phone, stage)
        return

    if not CRM_WEBHOOK_URL:
        log.warning("FEATURE_CRM=true, но CRM_WEBHOOK_URL не задан")
        return

    payload = {
        "phone":      phone,
        "company_id": company_id,
        "stage":      stage,
        "summary":    summary,
    }
    try:
        r = requests.post(CRM_WEBHOOK_URL, json=payload, timeout=10)
        r.raise_for_status()
        log.info("[CRM] lead pushed: %s → %s", phone, stage)
    except Exception as exc:
        log.error("[CRM] error: %s", exc)
