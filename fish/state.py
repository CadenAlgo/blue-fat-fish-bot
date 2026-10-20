"""运行时状态：在线标记、最近对话、统计。"""

from __future__ import annotations

import json
import threading
import time
from collections import deque
from typing import Any

from . import settings


class State:
    def __init__(self) -> None:
        self.started_at = time.time()
        self.napcat_online = False
        self.napcat_uin = ""
        self.bot_uin = ""
        self.messages_in = 0
        self.messages_out = 0
        self.logs: deque[dict[str, Any]] = deque(maxlen=200)
        self._lock = threading.Lock()
        self._log_file = settings.DATA_DIR / "chat.jsonl"
        self._load_recent()

    def _load_recent(self) -> None:
        """重启后把上次的对话读回面板，别白花花一片。"""
        try:
            if not self._log_file.exists():
                return
            for line in self._log_file.read_text(encoding="utf-8").splitlines()[-30:]:
                try:
                    self.logs.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        except OSError:
            pass

    def log(self, scene: str, user: str, text: str, reply: str = "") -> None:
        entry = {
            "t": time.strftime("%H:%M:%S"),
            "scene": scene,
            "user": user,
            "text": text[:500],
            "reply": reply[:500],
        }
        with self._lock:
            self.logs.append(entry)
            self.messages_in += 1
            if reply:
                self.messages_out += 1
            try:
                self._log_file.parent.mkdir(parents=True, exist_ok=True)
                with self._log_file.open("a", encoding="utf-8") as handle:
                    handle.write(json.dumps(entry, ensure_ascii=False) + "\n")
            except OSError:
                pass

    def recent(self, limit: int = 50) -> list[dict[str, Any]]:
        with self._lock:
            return list(self.logs)[-limit:]

    def snapshot(self) -> dict[str, Any]:
        return {
            "uptime": int(time.time() - self.started_at),
            "napcat_online": self.napcat_online,
            "napcat_uin": self.napcat_uin,
            "bot_uin": self.bot_uin,
            "messages_in": self.messages_in,
            "messages_out": self.messages_out,
        }


STATE = State()
