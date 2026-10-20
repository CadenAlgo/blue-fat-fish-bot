"""本地控制台：状态、人设、话量、模型、最近对话。"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from . import brain, napcat, profiles, prompt, reminders, settings, tools, video
from .guard import GUARD
from .state import State

IMAGE_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}


def _sticker_items() -> list[dict[str, Any]]:
    items = []
    for path in tools.sticker_files():
        try:
            size = round(path.stat().st_size / 1024)
        except OSError:
            size = 0
        items.append({"name": path.name, "tag": path.stem.split("_")[0], "kb": size})
    return sorted(items, key=lambda item: item["name"])


PROVIDERS = [
    {"name": "DeepSeek", "base": "https://api.deepseek.com/v1", "hint": "deepseek-flash / deepseek-v4-pro"},
    {"name": "阶跃星辰 StepFun", "base": "https://api.stepfun.com/v1", "hint": "step-2-16k 等，模型名以控制台为准"},
    {"name": "阶跃 Step Plan（套餐版）", "base": "https://api.stepfun.com/step_plan/v1", "hint": "套餐专用入口"},
    {"name": "阿里百炼 DashScope", "base": "https://dashscope.aliyuncs.com/compatible-mode/v1", "hint": "qwen-plus / qwen-max"},
    {"name": "智谱 BigModel", "base": "https://open.bigmodel.cn/api/paas/v4", "hint": "glm-4-plus"},
    {"name": "月之暗面 Kimi", "base": "https://api.moonshot.cn/v1", "hint": "moonshot-v1-8k"},
    {"name": "硅基流动 SiliconFlow", "base": "https://api.siliconflow.cn/v1", "hint": "各种开源模型"},
    {"name": "OpenAI", "base": "https://api.openai.com/v1", "hint": "gpt-4o-mini"},
    {"name": "本地 Ollama", "base": "http://127.0.0.1:11434/v1", "hint": "qwen2.5:7b 之类"},
]


def create_app(state: State) -> FastAPI:
    app = FastAPI(title="大肥鱼控制台", docs_url=None, redoc_url=None)
    sticker_dir = settings.ROOT / "stickers"
    sticker_dir.mkdir(exist_ok=True)
    app.mount("/stickers", StaticFiles(directory=sticker_dir), name="stickers")

    @app.get("/")
    async def index() -> FileResponse:
        return FileResponse(settings.WEB_DIR / "index.html")

    @app.get("/api/state")
    async def api_state() -> dict[str, Any]:
        config = settings.load()
        api_key, base_url = settings.llm()
        return {
            "config": config,
            "persona": settings.persona(),
            "runtime": state.snapshot(),
            "llm": {
                "base_url": base_url,
                "key_masked": settings.mask(api_key),
                "key_length": len(api_key),
                "has_key": bool(api_key),
            },
            "providers": PROVIDERS,
            "profiles": profiles.listing(),
            "levels": [
                {"level": level, "name": name, "style": style}
                for level, (name, style) in enumerate(prompt.LEVELS)
            ],
            "guard": config.get("guard", {}),
            "blacklist": GUARD.entries(),
            "reminders": reminders.pending(),
            "profile": settings.profile(),
            "express": {
                "customer": config.get("express", {}).get("customer", ""),
                "has_key": bool(config.get("express", {}).get("api_key")),
            },
            "email": {
                "enabled": bool(config.get("email", {}).get("enabled")),
                "user": config.get("email", {}).get("user", ""),
                "has_password": bool(config.get("email", {}).get("password")),
            },
            "downloads": video.stats(),
            "stickers": _sticker_items(),
            "sticker": config.get("sticker", {}),
            "logs": state.recent(30),
        }

    @app.post("/api/config")
    async def api_config(patch: dict[str, Any]) -> dict[str, Any]:
        try:
            return settings.save(patch)
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/persona")
    async def api_persona(body: dict[str, Any]) -> dict[str, Any]:
        text = str(body.get("text", ""))
        if not text.strip():
            raise HTTPException(status_code=400, detail="人设不能为空")
        settings.save_persona(text)
        return {"ok": True, "length": len(text)}

    @app.post("/api/profile")
    async def api_profile(body: dict[str, Any]) -> dict[str, Any]:
        settings.save_profile(str(body.get("text", "")))
        return {"ok": True}

    @app.post("/api/express")
    async def api_express(body: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, str] = {}
        if "api_key" in body and str(body["api_key"]).strip():
            patch["api_key"] = str(body["api_key"]).strip()
        if "customer" in body:
            patch["customer"] = str(body["customer"]).strip()
        if patch:
            settings.save({"express": patch})
        return {"ok": True}

    @app.post("/api/llm")
    async def api_llm(body: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, str] = {}
        base_url = str(body.get("base_url", "")).strip()
        api_key = str(body.get("api_key", "")).strip()
        warning = ""

        if base_url:
            patch["LLM_BASE_URL"] = base_url

        if api_key:
            looks_like_address = (
                "://" in api_key
                or api_key.lower().startswith(("http", "127.", "localhost", "0.0.0.0"))
                or bool(re.match(r"^\d{1,3}(\.\d{1,3}){3}(:\d+)?$", api_key))
            )
            if looks_like_address:
                raise HTTPException(
                    status_code=400,
                    detail=(
                        "这一串看起来是网址或代理地址（比如 127.0.0.1:7897），不是 API key。"
                        "API key 一般是 sk- 开头的一长串字符，去服务商控制台重新复制。"
                    ),
                )
            if len(api_key) < 20:
                warning = f"这个 key 只有 {len(api_key)} 个字符，可能没复制全，建议核对一下。"
            patch["LLM_API_KEY"] = api_key

        if patch:
            settings.set_env(patch)
        return {"ok": True, "warning": warning, "key_masked": settings.mask(settings.llm()[0])}

    @app.post("/api/email")
    async def api_email(body: dict[str, Any]) -> dict[str, Any]:
        patch: dict[str, Any] = {}
        if "enabled" in body:
            patch["enabled"] = bool(body["enabled"])
        if "user" in body:
            patch["user"] = str(body["user"]).strip()
        if str(body.get("password", "")).strip():
            patch["password"] = str(body["password"]).strip()
        if patch:
            settings.save({"email": patch})
        return {"ok": True}

    @app.post("/api/profiles/add")
    async def api_profile_add(body: dict[str, Any]) -> dict[str, Any]:
        name = str(body.get("name", "")).strip()
        if not name:
            raise HTTPException(status_code=400, detail="给这套配置起个名字")
        api_key, base_url = settings.llm()
        item = profiles.add(
            name=name,
            base=str(body.get("base") or base_url),
            key=str(body.get("key") or api_key),
            model=str(body.get("model") or settings.load()["model"]["name"]),
            temperature=float(body.get("temperature") or 1.1),
        )
        return {"ok": True, "id": item["id"], "profiles": profiles.listing()}

    @app.post("/api/profiles/apply")
    async def api_profile_apply(body: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(body.get("id", "")).strip()
        applied = profiles.apply(profile_id)
        if applied is None:
            raise HTTPException(status_code=404, detail="没找到这套配置")
        return {"ok": True, "profile": applied, "profiles": profiles.listing()}

    @app.post("/api/profiles/delete")
    async def api_profile_delete(body: dict[str, Any]) -> dict[str, Any]:
        profile_id = str(body.get("id", "")).strip()
        return {"ok": profiles.remove(profile_id), "profiles": profiles.listing()}

    @app.post("/api/llm/clear")
    async def api_llm_clear() -> dict[str, Any]:
        settings.set_env({"LLM_API_KEY": ""})
        return {"ok": True}

    @app.post("/api/llm/test")
    async def api_llm_test() -> dict[str, Any]:
        try:
            return {"ok": True, "reply": brain.ping()}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {str(exc)[:220]}"}

    @app.post("/api/models")
    async def api_models() -> dict[str, Any]:
        try:
            return {"ok": True, "models": brain.available_models()}
        except Exception as exc:
            return {"ok": False, "message": f"{type(exc).__name__}: {exc}"}

    @app.post("/api/guard")
    async def api_guard(body: dict[str, Any]) -> dict[str, Any]:
        try:
            return settings.save({"guard": body})
        except Exception as exc:
            raise HTTPException(status_code=400, detail=str(exc)) from exc

    @app.post("/api/guard/unblock")
    async def api_unblock(body: dict[str, Any]) -> dict[str, Any]:
        user = str(body.get("user", "")).strip()
        if not user:
            raise HTTPException(status_code=400, detail="缺少 user")
        return {"ok": GUARD.unblock(user), "user": user}

    @app.post("/api/reminders/remove")
    async def api_reminder_remove(body: dict[str, Any]) -> dict[str, Any]:
        reminder_id = str(body.get("id", "")).strip()
        if not reminder_id:
            raise HTTPException(status_code=400, detail="缺少 id")
        return {"ok": reminders.remove(reminder_id), "id": reminder_id}

    @app.post("/api/stickers/upload")
    async def api_sticker_upload(
        file: UploadFile = File(...), tag: str = Form("其他")
    ) -> dict[str, Any]:
        suffix = Path(file.filename or "").suffix.lower()
        if suffix not in IMAGE_SUFFIXES:
            raise HTTPException(status_code=400, detail="只支持 png / jpg / gif / webp")

        data = await file.read()
        if len(data) > 5 * 1024 * 1024:
            raise HTTPException(status_code=400, detail="图太大了，5MB 以内")

        safe_tag = re.sub(r"[^0-9A-Za-z\u4e00-\u9fff]", "", tag or "其他")[:10] or "其他"
        folder = settings.ROOT / "stickers"
        folder.mkdir(exist_ok=True)
        index = 1
        while (folder / f"{safe_tag}_{index:02d}{suffix}").exists():
            index += 1
        target = folder / f"{safe_tag}_{index:02d}{suffix}"
        target.write_bytes(data)
        return {"ok": True, "name": target.name, "stickers": _sticker_items()}

    @app.post("/api/stickers/delete")
    async def api_sticker_delete(body: dict[str, Any]) -> dict[str, Any]:
        name = Path(str(body.get("name", ""))).name
        target = settings.ROOT / "stickers" / name
        if not name or not target.exists():
            raise HTTPException(status_code=404, detail="没找到这张图")
        target.unlink()
        return {"ok": True, "stickers": _sticker_items()}

    @app.post("/api/downloads/clean")
    async def api_downloads_clean() -> dict[str, Any]:
        return {"ok": True, "removed": video.cleanup(0)}

    @app.post("/api/napcat/restart")
    async def api_restart() -> dict[str, Any]:
        return {"ok": True, "message": napcat.restart()}

    @app.post("/api/napcat/stop")
    async def api_stop() -> dict[str, Any]:
        return {"ok": True, "message": napcat.stop()}

    return app
