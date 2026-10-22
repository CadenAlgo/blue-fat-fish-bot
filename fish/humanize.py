"""拟人化：延迟、分段、作息、跳过、主动开口。

核心目标不是"装人"，而是别让行为呈现出机器特征：
固定间隔、秒回、一次一大段、24 小时在线。
"""

from __future__ import annotations

import random
import re
from datetime import datetime

from . import settings


def config() -> dict:
    return settings.load().get("humanize", {})


def enabled() -> bool:
    return bool(config().get("enabled", True))


def _minutes(text: str) -> int | None:
    raw = str(text or "").strip()
    if not raw:
        return None
    match = re.match(r"^(\d{1,2}):(\d{2})$", raw)
    if not match:
        return None
    hour, minute = int(match.group(1)), int(match.group(2))
    if hour > 23 or minute > 59:
        return None
    return hour * 60 + minute


def in_sleep_window(now: datetime | None = None) -> bool:
    cfg = config()
    start = _minutes(cfg.get("sleep_start", ""))
    end = _minutes(cfg.get("sleep_end", ""))
    if start is None or end is None or start == end:
        return False
    now = now or datetime.now()
    current = now.hour * 60 + now.minute
    if start < end:
        return start <= current < end
    return current >= start or current < end


def read_delay() -> float:
    cfg = config()
    low = float(cfg.get("read_delay_min", 3))
    high = max(low, float(cfg.get("read_delay_max", 12)))
    return random.uniform(low, high)


def typing_time(text: str) -> float:
    """按字数估一个打字时间，带抖动。"""
    cfg = config()
    per_char = float(cfg.get("typing_per_char", 0.12))
    low = float(cfg.get("typing_min", 2))
    high = max(low, float(cfg.get("typing_max", 15)))
    base = max(low, min(high, len(text) * per_char))
    return base * random.uniform(0.8, 1.3)


def split_reply(text: str) -> list[str]:
    """把一条长回复拆成几段，模拟边想边打。"""
    text = (text or "").strip()
    if not text:
        return []
    if not config().get("split_replies", True):
        return [text]

    max_parts = max(1, int(config().get("split_max", 3)))
    pieces = [part.strip() for part in re.findall(r"[^。！？!?…~\n]+[。！？!?…~]*", text)]
    pieces = [part for part in pieces if part]
    if len(pieces) <= 1:
        return [text]

    while len(pieces) > max_parts:
        index = min(
            range(len(pieces) - 1),
            key=lambda i: len(pieces[i]) + len(pieces[i + 1]),
        )
        pieces[index : index + 2] = [pieces[index] + pieces[index + 1]]
    return pieces


def gap() -> float:
    cfg = config()
    low = float(cfg.get("split_gap_min", 2))
    high = max(low, float(cfg.get("split_gap_max", 8)))
    return random.uniform(low, high)


def should_skip() -> bool:
    chance = float(config().get("skip_chance", 0.12))
    return random.random() < chance


def should_chime_in() -> bool:
    """群里没人 @ 它时，要不要插一句。"""
    chance = float(config().get("group_reply_chance", 0.25))
    return random.random() < chance


def proactive_due(now: datetime | None = None) -> bool:
    cfg = config()
    if not cfg.get("proactive", False):
        return False
    if in_sleep_window(now):
        return False
    chance = float(cfg.get("proactive_chance", 0.25))
    return random.random() < chance
