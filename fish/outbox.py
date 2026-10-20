"""发件箱：工具里只登记要发什么，真正发送由 QQ 通道在回复后统一做。"""

from __future__ import annotations

from typing import Any

_pending: list[tuple[str, dict[str, Any]]] = []


def private(user_id: int, text: str) -> None:
    _pending.append(("send_private_msg", {"user_id": int(user_id), "message": text}))


def group(group_id: int, text: str) -> None:
    _pending.append(("send_group_msg", {"group_id": int(group_id), "message": text}))


def sticker(kind: str, group_id: Any, user_id: Any, path: str) -> None:
    """发表情包：OneBot 的 image 段，file 用 file:/// 绝对路径。"""
    uri = "file:///" + str(path).replace("\\", "/").lstrip("/")
    segment = [{"type": "image", "data": {"file": uri}}]
    if kind == "group" and group_id:
        _pending.append(("send_group_msg", {"group_id": int(group_id), "message": segment}))
    elif user_id:
        _pending.append(("send_private_msg", {"user_id": int(user_id), "message": segment}))


def drain() -> list[tuple[str, dict[str, Any]]]:
    items = list(_pending)
    _pending.clear()
    return items
