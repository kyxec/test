import os
from dotenv import load_dotenv

load_dotenv()

# ── Обязательные ──────────────────────────────────────────────────
GROQ_KEY    = os.environ["GROQ_KEY"]
WA_VERIFY   = os.environ["WA_VERIFY"]

# ── Feature flags (true/false в .env) ─────────────────────────────
def _flag(name: str, default: bool = False) -> bool:
    return os.getenv(name, str(default)).lower() in ("1", "true", "yes")

FEATURE_MEMORY   = _flag("FEATURE_MEMORY",   True)   # память клиента + summary
FEATURE_FUNNEL   = _flag("FEATURE_FUNNEL",   True)   # воронка / стадии клиента
FEATURE_CRM      = _flag("FEATURE_CRM",      False)  # отправка в CRM
FEATURE_SPAM     = _flag("FEATURE_SPAM",     True)   # защита от спама
FEATURE_HANDOFF  = _flag("FEATURE_HANDOFF",  True)   # передача менеджеру

# ── Параметры памяти ───────────────────────────────────────────────
MEMORY_WINDOW    = int(os.getenv("MEMORY_WINDOW",   "20"))  # сообщений до summary
MEMORY_KEEP      = int(os.getenv("MEMORY_KEEP",     "10"))  # сообщений после summary

# ── Спам-защита ────────────────────────────────────────────────────
SPAM_MAX_PER_MIN = int(os.getenv("SPAM_MAX_PER_MIN", "5"))

# ── CRM (заполнить когда понадобится) ──────────────────────────────
CRM_WEBHOOK_URL  = os.getenv("CRM_WEBHOOK_URL", "")

# ── LLM ───────────────────────────────────────────────────────────
LLM_MODEL        = os.getenv("LLM_MODEL", "llama-3.3-70b-versatile")
LLM_MAX_TOKENS   = int(os.getenv("LLM_MAX_TOKENS", "500"))
