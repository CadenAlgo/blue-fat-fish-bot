"""定时提醒：落到 JSON，主循环到点推送。"""

from __future__ import annotations

import datetime
import json
import time
import uuid
from typing import Any

from . import settings

FILE = settings.DATA_DIR / "reminders.json"

MODES = ("none", "daily", "weekly", "weekdays")


def _load() -> list[dict[str, Any]]:
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    try:
        FILE.parent.mkdir(parents=True, exist_ok=True)
        FILE.write_text(
            json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8"
        )
    except OSError:
        pass


def add(
    user: str,
    kind: str,
    group: Any,
    due: float,
    text: str,
    repeat: str = "none",
) -> dict[str, Any]:
    items = _load()
    item = {
        "id": uuid.uuid4().hex[:8],
        "user": str(user),
        "kind": kind,
        "group": group,
        "due": float(due),
        "text": text.strip()[:200],
        "repeat": repeat if repeat in MODES else "none",
        "attempts": 0,
        "created": time.time(),
    }
    items.append(item)
    _save(items)
    return item


def pending() -> list[dict[str, Any]]:
    items = _load()
    items.sort(key=lambda item: item.get("due", 0))
    for item in items:
        item["due_text"] = time.strftime("%m-%d %H:%M", time.localtime(item["due"]))
    return items


def due(now: float | None = None) -> list[dict[str, Any]]:
    now = time.time() if now is None else now
    return [item for item in _load() if item.get("due", 0) <= now]


def advance(item: dict[str, Any]) -> None:
    items = _load()
    keep: list[dict[str, Any]] = []
    for current in items:
        if current.get("id") != item.get("id"):
            keep.append(current)
            continue

        mode = current.get("repeat")
        due = float(current.get("due", 0))
        if mode == "daily":
            current["due"] = due + 86400
        elif mode == "weekly":
            current["due"] = due + 7 * 86400
        elif mode == "weekdays":
            moment = datetime.datetime.fromtimestamp(due) + datetime.timedelta(days=1)
            while moment.weekday() >= 5:  # 跳过周六周日
                moment += datetime.timedelta(days=1)
            current["due"] = moment.timestamp()
        else:
            continue  # 一次性的，用完就删

        current["attempts"] = 0
        keep.append(current)
    _save(keep)


def fail(item: dict[str, Any]) -> None:
    items = _load()
    keep: list[dict[str, Any]] = []
    for current in items:
        if current.get("id") != item.get("id"):
            keep.append(current)
            continue
        attempts = int(current.get("attempts", 0)) + 1
        if attempts < 3:
            current["attempts"] = attempts
            current["due"] = time.time() + 60
            keep.append(current)
        else:
            print(f"[提醒] 连续失败，放弃：{current.get('text')}")
    _save(keep)


def remove(reminder_id: str) -> bool:
    items = _load()
    keep = [item for item in items if item.get("id") != reminder_id]
    if len(keep) == len(items):
        return False
    _save(keep)
    return True
