"""模型配置档：存多套 base / key / 模型，一键切换。

密钥存在 data/profiles.json（不进仓库），面板只回显打码后的值。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any

from . import settings

FILE = settings.DATA_DIR / "profiles.json"


def _load() -> list[dict[str, Any]]:
    try:
        raw = json.loads(FILE.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return []
    return raw if isinstance(raw, list) else []


def _save(items: list[dict[str, Any]]) -> None:
    FILE.parent.mkdir(parents=True, exist_ok=True)
    FILE.write_text(json.dumps(items, ensure_ascii=False, indent=2), encoding="utf-8")


def listing() -> list[dict[str, Any]]:
    config = settings.load()
    active = str(config.get("model", {}).get("profile_id") or "")
    result = []
    for item in _load():
        result.append(
            {
                "id": item.get("id"),
                "name": item.get("name"),
                "base": item.get("base"),
                "model": item.get("model"),
                "temperature": item.get("temperature", 1.1),
                "key_masked": settings.mask(str(item.get("key") or "")),
                "has_key": bool(item.get("key")),
                "active": item.get("id") == active,
            }
        )
    return result


def add(
    name: str,
    base: str,
    key: str,
    model: str,
    temperature: float = 1.1,
) -> dict[str, Any]:
    items = _load()
    item = {
        "id": uuid.uuid4().hex[:8],
        "name": (name or "未命名").strip()[:40],
        "base": base.strip(),
        "key": key.strip(),
        "model": model.strip(),
        "temperature": float(temperature),
        "created": time.time(),
    }
    items.append(item)
    _save(items)
    return item


def remove(profile_id: str) -> bool:
    items = _load()
    keep = [item for item in items if item.get("id") != profile_id]
    if len(keep) == len(items):
        return False
    _save(keep)
    return True


def apply(profile_id: str) -> dict[str, Any] | None:
    """把某一套写进 .env 和 config.json，立刻生效。"""
    for item in _load():
        if item.get("id") != profile_id:
            continue
        settings.set_env(
            {
                "LLM_BASE_URL": str(item.get("base") or ""),
                "LLM_API_KEY": str(item.get("key") or ""),
            }
        )
        settings.save(
            {
                "model": {
                    "name": str(item.get("model") or ""),
                    "temperature": float(item.get("temperature") or 1.1),
                    "profile_id": profile_id,
                }
            }
        )
        return {
            "id": item.get("id"),
            "name": item.get("name"),
            "base": settings.normalize_base(str(item.get("base") or "")),
            "model": item.get("model"),
            "temperature": item.get("temperature"),
            "key_masked": settings.mask(str(item.get("key") or "")),
        }
    return None
