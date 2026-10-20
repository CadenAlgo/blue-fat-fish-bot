"""大脑：拼消息、调模型、跑工具，拿到最终回复。"""

from __future__ import annotations

import json
from typing import Any, Callable

from openai import OpenAI

from . import prompt, settings, tools

_client: OpenAI | None = None
_client_key: tuple[str, str] = ("", "")


def _client_for(api_key: str, base_url: str) -> OpenAI:
    global _client, _client_key
    if _client is None or _client_key != (api_key, base_url):
        _client = OpenAI(api_key=api_key, base_url=base_url, timeout=90.0)
        _client_key = (api_key, base_url)
    return _client


def _prepare(history: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """只让最近一条带图，老消息里的图换成占位，省 token 也省内存。"""
    prepared: list[dict[str, Any]] = []
    for index, message in enumerate(history):
        content = message.get("content")
        if isinstance(content, list):
            keep_image = index == len(history) - 1
            parts: list[dict[str, Any]] = []
            for part in content:
                if part.get("type") == "image_url" and not keep_image:
                    continue
                parts.append(part)
            if not any(part.get("type") == "text" for part in parts):
                parts.insert(0, {"type": "text", "text": "（之前发过一张图）"})
            prepared.append({"role": message["role"], "content": parts})
        else:
            prepared.append({"role": message["role"], "content": content})
    return prepared


def _parse_args(raw: str | None) -> dict[str, Any]:
    try:
        return json.loads(raw or "{}")
    except json.JSONDecodeError:
        return {}


def chat(
    history: list[dict[str, Any]],
    scene: str = "qq_group",
    on_tool: Callable[[str], None] | None = None,
    ctx: dict[str, Any] | None = None,
) -> str:
    config = settings.load()
    api_key, base_url = settings.llm()
    if not api_key:
        raise RuntimeError("还没配模型 API key")

    model = str(config["model"]["name"])
    temperature = float(config["model"]["temperature"])
    max_steps = max(1, int(config["model"]["max_steps"]))
    max_history = max(2, int(config["model"]["max_history"]))

    messages: list[dict[str, Any]] = [
        {
            "role": "system",
            "content": prompt.build(
                scene,
                user_id=str((ctx or {}).get("user") or ""),
                owner_id=str(config.get("guard", {}).get("admin_qq") or ""),
            ),
        }
    ]
    messages.extend(_prepare(history[-max_history:]))

    client = _client_for(api_key, base_url)

    for _ in range(max_steps):
        response = client.chat.completions.create(
            model=model,
            messages=messages,
            tools=tools.SPECS,
            temperature=temperature,
        )
        message = response.choices[0].message

        if not message.tool_calls:
            return (message.content or "").strip()

        messages.append(
            {
                "role": "assistant",
                "content": message.content or "",
                "tool_calls": [call.model_dump() for call in message.tool_calls],
            }
        )

        for call in message.tool_calls:
            name = call.function.name
            if on_tool:
                on_tool(name)
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": call.id,
                    "content": tools.call(
                        name, _parse_args(call.function.arguments), ctx
                    ),
                }
            )

    return "想着想着把自己绕晕了……你再说一遍？"


def available_models() -> list[str]:
    api_key, base_url = settings.llm()
    if not api_key:
        raise RuntimeError("还没配 API key")

    client = _client_for(api_key, base_url)
    try:
        return [item.id for item in client.models.list().data]
    except Exception as first:
        # 有些兼容网关不按 SDK 期望的形状返回，退回裸 HTTP 再试一次
        import httpx

        try:
            response = httpx.get(
                f"{base_url}/models",
                headers={"Authorization": f"Bearer {api_key}"},
                timeout=20.0,
            )
            if response.status_code == 200:
                data = response.json().get("data") or []
                ids = [str(item.get("id")) for item in data if item.get("id")]
                if ids:
                    return ids
            raise RuntimeError(f"HTTP {response.status_code}: {response.text[:120]}")
        except Exception:
            raise RuntimeError(f"{type(first).__name__}: {str(first)[:160]}") from None


def ping() -> str:
    """发一句最短对话，验证 key / base / 模型名是否都对。"""
    api_key, base_url = settings.llm()
    if not api_key:
        raise RuntimeError("还没配 API key")

    config = settings.load()
    model = str(config["model"]["name"])
    client = _client_for(api_key, base_url)
    response = client.chat.completions.create(
        model=model,
        messages=[{"role": "user", "content": "只回复两个字：在线"}],
        max_tokens=300,  # 思考型模型会先花 token 想，给小了正文就空了
        temperature=0,
    )
    message = response.choices[0].message
    content = (message.content or "").strip()
    if content:
        return content
    reasoning = getattr(message, "reasoning_content", None)
    if reasoning:
        return f"（只拿到了思考过程）{str(reasoning)[:80]}"
    return "（模型返回了空内容）"
