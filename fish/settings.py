"""配置层：密钥进 .env，行为进 config.json，运行时进 data/。

开源时只需要交出 config.example / .env.example，真实密钥永远不进仓库。
"""

from __future__ import annotations

import json
import os
import threading
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from dotenv import dotenv_values, set_key


def _sanitize_no_proxy() -> None:
    """代理软件常往 NO_PROXY 里写 [::1]，HTTP 库解析会报错，先剔掉。"""
    for name in ("NO_PROXY", "no_proxy"):
        raw = os.environ.get(name)
        if not raw:
            continue
        kept = [
            item.strip()
            for item in raw.split(",")
            if item.strip() and ":" not in item and "[" not in item
        ]
        os.environ[name] = ",".join(kept)


_sanitize_no_proxy()

ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = ROOT / "config.json"
ENV_FILE = ROOT / ".env"
PERSONA_FILE = ROOT / "persona.md"
PROFILE_FILE = ROOT / "profile.md"
DATA_DIR = ROOT / "data"
WEB_DIR = ROOT / "web"

DEFAULTS: dict[str, Any] = {
    "bot": {
        "names": ["大肥鱼", "鱼鱼"],
        "group_mode": "at",          # at 只回 @ / keyword 带名字就回 / all 全接
        "chattiness": 3,             # 0 哑巴 … 5 话痨
        "reply_chunk": 300,          # 单条消息上限，超了拆开
        "quote_reply": False,
    },
    "model": {
        "name": "deepseek-flash",
        "temperature": 1.1,
        "max_steps": 6,
        "max_history": 12,
        "profile_id": "",
    },
    "search": {
        "enabled": True,
        "max_results": 5,
        "fetch_chars": 3000,
    },
    "guard": {
        "enabled": True,
        "window_seconds": 60,
        "max_messages": 20,
        "block_seconds": 3600,
        "admin_qq": "",
        "notify_admin": True,
        "notice_to_user": "喵，你刷得太密了，本鱼先躲 {minutes} 分钟，消停点再来。",
    },
    "reminder": {
        "message": "喵——到点了，本鱼来催你：{text}",
    },
    "sticker": {
        "chance": 0.35,
        "cooldown_seconds": 120,
        "avoid_repeat": 3,
    },
    "humanize": {
        "enabled": True,
        "read_delay_min": 3,
        "read_delay_max": 12,
        "typing_per_char": 0.12,
        "typing_min": 2,
        "typing_max": 15,
        "split_replies": True,
        "split_max": 3,
        "split_gap_min": 2,
        "split_gap_max": 8,
        "skip_chance": 0.12,
        "sleep_start": "01:30",
        "sleep_end": "08:30",
        "group_reply_chance": 0.25,
        "proactive": True,
        "proactive_chance": 0.25,
        "proactive_interval_minutes": 60,
        "proactive_daily_max": 3,
    },
    "express": {
        "api_key": "",
        "customer": "",
    },
    "video": {
        "enabled": True,
        "max_mb": 800,
        "keep_seconds": 180,
        "cookies_file": "",
        "cookies_browser": "",
    },
    "email": {
        "enabled": False,
        "smtp_host": "smtp.qq.com",
        "smtp_port": 465,
        "user": "",
        "password": "",
        "sender_name": "蓝色大肥鱼",
    },
    "server": {
        "onebot_port": 3001,
        "webui_port": 8765,
        "open_browser": True,
    },
    "napcat": {
        "dir": "../napcat/shell",
        "auto_start": True,
    },
}

_lock = threading.RLock()
_cache: dict[str, Any] | None = None
_cache_mtime: float = -1.0


# ---------------------------------------------------------------------------
# config.json
# ---------------------------------------------------------------------------

def _merge(target: dict[str, Any], patch: dict[str, Any]) -> dict[str, Any]:
    for key, value in patch.items():
        if isinstance(value, dict) and isinstance(target.get(key), dict):
            _merge(target[key], value)
        else:
            target[key] = value
    return target


def _write(config: dict[str, Any]) -> None:
    CONFIG_FILE.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )


def ensure() -> None:
    """首次运行把该建的都建好。"""
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if not CONFIG_FILE.exists():
        _write(json.loads(json.dumps(DEFAULTS)))
    if not PERSONA_FILE.exists():
        PERSONA_FILE.write_text("# 蓝色大肥鱼\n\n（在这里写人设）\n", encoding="utf-8")


def load() -> dict[str, Any]:
    """读配置；文件变了就重新读，改了立刻生效。"""
    global _cache, _cache_mtime
    with _lock:
        mtime = CONFIG_FILE.stat().st_mtime if CONFIG_FILE.exists() else -1.0
        if _cache is not None and mtime == _cache_mtime:
            return _cache

        raw: dict[str, Any] = {}
        if CONFIG_FILE.exists():
            try:
                raw = json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                raw = {}
        config = _merge(json.loads(json.dumps(DEFAULTS)), raw)
        _cache = config
        _cache_mtime = mtime
        return config


def save(patch: dict[str, Any]) -> dict[str, Any]:
    global _cache, _cache_mtime
    with _lock:
        config = _merge(load(), patch)
        _write(config)
        _cache = config
        _cache_mtime = CONFIG_FILE.stat().st_mtime
        return config


# ---------------------------------------------------------------------------
# .env（密钥）
# ---------------------------------------------------------------------------

def env() -> dict[str, str]:
    if not ENV_FILE.exists():
        return {}
    return {key: value for key, value in dotenv_values(ENV_FILE).items() if value}


def set_env(values: dict[str, str]) -> None:
    ENV_FILE.touch(exist_ok=True)
    for key, value in values.items():
        set_key(str(ENV_FILE), key, value or "")


def normalize_base(url: str) -> str:
    """把 base_url 收拾成 SDK 能用的样子：补协议、去尾斜杠、补 /v1。"""
    raw = (url or "").strip()
    if not raw:
        return "https://api.deepseek.com/v1"
    if not raw.startswith(("http://", "https://")):
        raw = "https://" + raw
    raw = raw.rstrip("/")

    for suffix in ("/chat/completions", "/completions", "/models"):
        if raw.endswith(suffix):
            raw = raw[: -len(suffix)]

    parsed = urlparse(raw)
    if parsed.path in ("", "/"):
        raw = raw + "/v1"
    return raw


def llm() -> tuple[str, str]:
    """返回 (api_key, base_url)。兼容老的 DEEPSEEK_* 写法。"""
    values = env()
    key = values.get("LLM_API_KEY") or values.get("DEEPSEEK_API_KEY") or ""
    base = (
        values.get("LLM_BASE_URL")
        or values.get("DEEPSEEK_BASE_URL")
        or "https://api.deepseek.com"
    )
    return key.strip(), normalize_base(base)


def mask(secret: str) -> str:
    if not secret:
        return ""
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:4]}{'*' * 8}{secret[-4:]}"


# ---------------------------------------------------------------------------
# persona.md
# ---------------------------------------------------------------------------

def persona() -> str:
    with _lock:
        if not PERSONA_FILE.exists():
            return ""
        return PERSONA_FILE.read_text(encoding="utf-8")


def profile() -> str:
    with _lock:
        if not PROFILE_FILE.exists():
            return ""
        return PROFILE_FILE.read_text(encoding="utf-8")


def save_persona(text: str) -> None:
    with _lock:
        PERSONA_FILE.write_text(text, encoding="utf-8")


def save_profile(text: str) -> None:
    with _lock:
        PROFILE_FILE.write_text(text, encoding="utf-8")
