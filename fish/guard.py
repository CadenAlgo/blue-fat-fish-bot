"""防刷屏：一个人短时间内发太多，就自动拉黑，并通知管理员。"""

from __future__ import annotations

import json
import time
from collections import defaultdict, deque
from typing import Any

from . import settings

FILE = settings.DATA_DIR / "blacklist.json"


class Guard:
    def __init__(self) -> None:
        self.hits: dict[str, deque[float]] = defaultdict(deque)
        self.blocked: dict[str, float] = {}
        self._load()

    # -- 持久化 ----------------------------------------------------------

    def _load(self) -> None:
        try:
            raw = json.loads(FILE.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return
        if isinstance(raw, dict):
            self.blocked = {}
            for user, until in raw.items():
                try:
                    self.blocked[str(user)] = float(until)
                except (TypeError, ValueError):
                    continue

    def _save(self) -> None:
        try:
            FILE.parent.mkdir(parents=True, exist_ok=True)
            FILE.write_text(
                json.dumps(self.blocked, ensure_ascii=False, indent=2), encoding="utf-8"
            )
        except OSError:
            pass

    # -- 判定 ------------------------------------------------------------

    @staticmethod
    def _config() -> dict[str, Any]:
        return settings.load().get("guard", {})

    def is_blocked(self, user: str) -> bool:
        until = self.blocked.get(user)
        if until is None:
            return False
        if until <= time.time():
            self.blocked.pop(user, None)
            self._save()
            return False
        return True

    def check(self, user: str) -> str | None:
        """放行返回 None；已拉黑返回 'blocked'；刚触发拉黑返回 'newly_blocked'。"""
        config = self._config()
        if not config.get("enabled", True):
            return None

        admin = str(config.get("admin_qq") or "").strip()
        if admin and user == admin:
            return None  # 管理员永远放行

        if self.is_blocked(user):
            return "blocked"

        now = time.time()
        window = max(5, int(config.get("window_seconds", 60)))
        limit = max(3, int(config.get("max_messages", 20)))

        hits = self.hits[user]
        hits.append(now)
        while hits and now - hits[0] > window:
            hits.popleft()

        if len(hits) > limit:
            block_for = max(60, int(config.get("block_seconds", 3600)))
            self.blocked[user] = now + block_for
            self._save()
            hits.clear()
            return "newly_blocked"
        return None

    # -- 管理 ------------------------------------------------------------

    def unblock(self, user: str) -> bool:
        existed = self.blocked.pop(str(user), None) is not None
        if existed:
            self._save()
        return existed

    def entries(self) -> list[dict[str, Any]]:
        now = time.time()
        items = []
        for user, until in sorted(self.blocked.items(), key=lambda kv: kv[1]):
            items.append(
                {
                    "user": user,
                    "until": time.strftime("%H:%M:%S", time.localtime(until)),
                    "remaining_minutes": max(0, int((until - now) // 60)),
                }
            )
        return items


GUARD = Guard()
