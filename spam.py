"""
spam.py — rate-limiting в памяти процесса.
При FEATURE_SPAM=false пропускает все сообщения.
"""
from __future__ import annotations
import time
from collections import defaultdict

from config import FEATURE_SPAM, SPAM_MAX_PER_MIN

# phone → [timestamp, ...]
_buckets: dict[str, list[float]] = defaultdict(list)


def is_spam(phone: str) -> bool:
    """True — сообщение нужно заблокировать."""
    if not FEATURE_SPAM:
        return False

    now = time.time()
    window = 60.0  # 1 минута

    # удалить старые метки
    _buckets[phone] = [t for t in _buckets[phone] if now - t < window]

    if len(_buckets[phone]) >= SPAM_MAX_PER_MIN:
        return True

    _buckets[phone].append(now)
    return False
