"""QQ 通道：OneBot 11 反向 WebSocket，收消息、调大脑、发回复。"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import random
import time
from collections import OrderedDict, deque
from typing import Any

import httpx
import websockets
from websockets.exceptions import ConnectionClosed

from . import brain, humanize, outbox, reminders, settings, tools, video
from .guard import GUARD
from .state import State

MAX_CHATS = 100
MAX_IMAGE_BYTES = 4 * 1024 * 1024


def _download_image(url: str) -> str | None:
    """把 QQ 图片下回来，转成 data URI 给模型看。"""
    if not url.startswith(("http://", "https://")):
        return None
    try:
        response = httpx.get(url, timeout=25.0, follow_redirects=True)
        response.raise_for_status()
    except Exception:
        return None

    data = response.content
    if not data or len(data) > MAX_IMAGE_BYTES:
        return None

    content_type = (response.headers.get("content-type") or "image/jpeg").split(";")[0].strip()
    if not content_type.startswith("image/"):
        content_type = "image/jpeg"
    return f"data:{content_type};base64," + base64.b64encode(data).decode()


class OneBotServer:
    def __init__(self, state: State) -> None:
        self.state = state
        self.histories: "OrderedDict[str, list[dict[str, Any]]]" = OrderedDict()
        self.locks: dict[str, asyncio.Lock] = {}
        self._tasks: set[asyncio.Task] = set()
        self._send_lock = asyncio.Lock()
        self._connections = 0
        self._recent_ids: deque[str] = deque(maxlen=200)
        self._recent_text: dict[str, float] = {}
        self._socket: Any = None
        self._active: dict[str, dict[str, Any]] = {}
        self._proactive_last = 0.0
        self._proactive_date = ""
        self._proactive_count = 0

    # -- 连接 ------------------------------------------------------------

    async def run(self) -> None:
        config = settings.load()
        port = int(config["server"]["onebot_port"])
        async with websockets.serve(
            self._handler,
            "127.0.0.1",
            port,
            ping_interval=30,
            ping_timeout=90,
            max_size=8 * 1024 * 1024,
        ):
            print(f"QQ 通道已就绪：ws://127.0.0.1:{port}（等 NapCat 连过来）")
            asyncio.create_task(self._reminder_loop())
            asyncio.create_task(self._proactive_loop())
            await asyncio.Future()

    async def _handler(self, socket: Any) -> None:
        self._socket = socket
        self._connections += 1
        self.state.napcat_online = True
        print(f"[连接] NapCat 连上了（当前 {self._connections} 条）")
        try:
            async for raw in socket:
                try:
                    payload = json.loads(raw)
                except json.JSONDecodeError:
                    continue
                if "post_type" not in payload:
                    continue  # 动作回执，不用管
                task = asyncio.create_task(self._handle(socket, payload))
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except ConnectionClosed:
            pass
        finally:
            self._connections = max(0, self._connections - 1)
            self.state.napcat_online = self._connections > 0
            if self._socket is socket:
                self._socket = None
            print(f"[断开] 一条连接关闭（剩 {self._connections} 条）")

    async def _send(self, socket: Any, action: str, params: dict[str, Any]) -> None:
        async with self._send_lock:
            await socket.send(
                json.dumps({"action": action, "params": params}, ensure_ascii=False)
            )

    # -- 消息 ------------------------------------------------------------

    def _history(self, key: str) -> list[dict[str, Any]]:
        if key not in self.histories:
            self.histories[key] = []
            while len(self.histories) > MAX_CHATS:
                self.histories.popitem(last=False)
        self.histories.move_to_end(key)
        return self.histories[key]

    def _lock(self, key: str) -> asyncio.Lock:
        lock = self.locks.get(key)
        if lock is None:
            lock = asyncio.Lock()
            self.locks[key] = lock
        return lock

    @staticmethod
    def _split(text: str, size: int) -> list[str]:
        text = text.strip()
        if not text:
            return []
        if len(text) <= size:
            return [text]
        return [text[i : i + size] for i in range(0, len(text), size)]

    def _read(
        self, event: dict[str, Any]
    ) -> tuple[str, list[str], bool]:
        """返回 (纯文本, 图片地址列表, 有没有点名叫它)。"""
        self_id = str(event.get("self_id", ""))
        mentioned = False
        texts: list[str] = []
        images: list[str] = []

        message = event.get("message")
        if isinstance(message, str):
            texts.append(message)
        else:
            for segment in message or []:
                kind = segment.get("type")
                data = segment.get("data") or {}
                if kind == "text":
                    texts.append(str(data.get("text", "")))
                elif kind == "at":
                    qq = str(data.get("qq", ""))
                    if qq == self_id:
                        mentioned = True
                    else:
                        texts.append(f"@{data.get('name') or qq} ")
                elif kind in ("image", "mface"):
                    url = str(data.get("url") or "")
                    if url:
                        images.append(url)
                    else:
                        texts.append("[图片]")
                elif kind == "face":
                    texts.append("[表情]")
                elif kind == "record":
                    texts.append("[语音]")

        text = "".join(texts).strip()
        for name in settings.load()["bot"]["names"]:
            if text.startswith(name):
                mentioned = True
                text = text[len(name) :].lstrip("，,：: 、")
                break
        return text, images, mentioned

    async def _build_content(self, text: str, images: list[str]) -> Any:
        if not images:
            return text

        parts: list[dict[str, Any]] = [{"type": "text", "text": text or "看看这个"}]
        for url in images[:3]:
            data = await asyncio.to_thread(_download_image, url)
            if data:
                parts.append({"type": "image_url", "image_url": {"url": data}})
        if len(parts) == 1:
            parts[0]["text"] = (text + " " if text else "") + "（图片没下载下来）"
        return parts

    async def _notify_admin(
        self, socket: Any, user_id: str, where: str, text: str
    ) -> None:
        config = settings.load().get("guard", {})
        if not config.get("notify_admin", True):
            return
        admin = str(config.get("admin_qq") or "").strip()
        if not admin.isdigit():
            return

        seconds = int(config.get("block_seconds", 3600))
        message = (
            f"[风控] QQ {user_id} 在{where}刷屏，已拉黑 {seconds} 秒。\n"
            f"最近一句：{(text or '[图片]')[:80]}"
        )
        try:
            await self._send(
                socket, "send_private_msg", {"user_id": int(admin), "message": message}
            )
            print(f"[风控] 已通知管理员 {admin}")
        except Exception as exc:
            print(f"[风控] 通知管理员失败：{type(exc).__name__}: {exc}")

    async def _handle(self, socket: Any, event: dict[str, Any]) -> None:
        if event.get("post_type") != "message":
            return

        # 万一有两条连接同时投递，同一条消息只处理一次
        message_id = str(event.get("message_id") or "")
        if message_id:
            if message_id in self._recent_ids:
                return
            self._recent_ids.append(message_id)

        self_id = str(event.get("self_id", ""))
        user_id = str(event.get("user_id", ""))
        if self_id:
            self.state.bot_uin = self_id
        if user_id and user_id == self_id:
            return

        text, images, mentioned = self._read(event)
        if not text and not images:
            return

        # 对方开了勿扰/自动回复：这种消息一律不理，否则两边会互相触发死循环
        stripped = text.lstrip(" \t\r\n")
        if stripped.startswith(("[自动回复]", "【自动回复】", "[自動回覆]")):
            print("[跳过] 自动回复消息")
            return

        # 兜底去重：同一个人 3 秒内发来一模一样的内容，只处理一次
        marker = f"{user_id}:{text or ('img' + str(len(images)))}"
        now = time.monotonic()
        last = self._recent_text.get(marker)
        if last is not None and now - last < 3.0:
            print(f"[跳过] 重复投递：{text or '[图片]'}")
            return
        self._recent_text[marker] = now
        if len(self._recent_text) > 300:
            self._recent_text.clear()

        config = settings.load()
        names = config["bot"]["names"]
        mode = str(config["bot"]["group_mode"])
        message_type = event.get("message_type")

        if message_type == "group":
            if mode == "at" and not mentioned:
                return
            if mode == "keyword" and not mentioned and not any(n in text for n in names):
                return
            if mode == "occasional" and not mentioned and not humanize.should_chime_in():
                return
            key = f"group:{event.get('group_id')}"
            action = "send_group_msg"
            params: dict[str, Any] = {"group_id": event.get("group_id")}
            scene, where = "qq_group", "群"
        elif message_type == "private":
            key = f"private:{user_id}"
            action = "send_private_msg"
            params = {"user_id": event.get("user_id")}
            scene, where = "qq_private", "私聊"
        else:
            return

        self._remember(key, message_type, event)

        # 拟人：睡觉时段不理人；有时候看到了也懒得回
        if humanize.enabled():
            if humanize.in_sleep_window():
                print("[拟人] 作息时间里，不回")
                return
            if humanize.should_skip():
                print("[拟人] 这次装作没看见")
                return

        # 防刷屏：同一账号短时间发太多，直接拉黑并通知管理员
        verdict = GUARD.check(user_id)
        if verdict == "blocked":
            print(f"[拦截] {user_id} 还在黑名单里，跳过")
            return
        if verdict == "newly_blocked":
            print(f"[拉黑] {user_id} 刷屏，已加入黑名单")
            guard_config = settings.load().get("guard", {})
            await self._notify_admin(socket, user_id, where, text)
            minutes = max(1, int(guard_config.get("block_seconds", 3600)) // 60)
            template = str(guard_config.get("notice_to_user") or "")
            notice = template.replace("{minutes}", str(minutes)) if template else ""
            if notice:
                with contextlib.suppress(Exception):
                    await self._send(socket, action, {**params, "message": notice})
            return

        # 视频链接：直接交给下载器，不烧模型额度
        video_url = video.find_url(text)
        if video_url and settings.load().get("video", {}).get("enabled", True):
            await self._send(
                socket,
                action,
                {**params, "message": "收到，本鱼去下这个视频，等我一两分钟……"},
            )
            task = asyncio.create_task(
                self._deliver_video(socket, action, dict(params), video_url)
            )
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
            self.state.log(where, user_id, text or "[链接]", "[视频下载中]")
            return

        content = await self._build_content(text, images)
        history = self._history(key)
        shown = text or "[图片]"
        print(f"[{where}] {user_id}: {shown}")

        # 拟人：先"看到"，过一会儿才回
        if humanize.enabled():
            await asyncio.sleep(humanize.read_delay())

        async with self._lock(key):
            history.append({"role": "user", "content": content})
            ctx = {
                "user": user_id,
                "kind": "group" if message_type == "group" else "private",
                "group": event.get("group_id") if message_type == "group" else None,
            }
            try:
                answer = await asyncio.to_thread(
                    brain.chat,
                    history,
                    scene,
                    lambda name: print(f"  · {tools.LABELS.get(name, name)}……"),
                    ctx,
                )
            except Exception as exc:
                history.pop()
                print(f"  ! 出错：{type(exc).__name__}: {exc}")
                answer = "喵……本鱼这边卡了一下，等下再问？"
            else:
                history.append({"role": "assistant", "content": answer})
                limit = max(2, int(config["model"]["max_history"])) * 2
                del history[:-limit]

            print(f"  -> {answer}")
            self.state.log(where, user_id, shown, answer)

            # 工具登记的主动消息（给某人发 QQ、发邮件之外的动作）在这里真正发出去
            for queued_action, queued_params in outbox.drain():
                with contextlib.suppress(Exception):
                    await self._send(socket, queued_action, queued_params)

            chunk_size = max(50, int(config["bot"]["reply_chunk"]))
            parts = humanize.split_reply(answer) if humanize.enabled() else [answer]
            messages: list[str] = []
            for part in parts:
                messages.extend(self._split(part, chunk_size))

            for index, piece in enumerate(messages):
                if not piece:
                    continue
                if humanize.enabled():
                    await asyncio.sleep(humanize.typing_time(piece))
                await self._send(socket, action, {**params, "message": piece})
                if humanize.enabled() and index < len(messages) - 1:
                    await asyncio.sleep(humanize.gap())

    def _remember(self, key: str, message_type: str, event: dict[str, Any]) -> None:
        """记下最近活跃的会话，供"主动开口"挑目标。"""
        target = event.get("group_id") if message_type == "group" else event.get("user_id")
        if target is None:
            return
        self._active[key] = {
            "kind": "group" if message_type == "group" else "private",
            "id": target,
            "scene": "qq_group" if message_type == "group" else "qq_private",
            "last": time.time(),
        }
        if len(self._active) > 100:
            oldest = min(self._active, key=lambda k: self._active[k]["last"])
            self._active.pop(oldest, None)

    async def _proactive_loop(self) -> None:
        while True:
            await asyncio.sleep(60)
            try:
                await self._maybe_proactive()
            except Exception as exc:
                print(f"[主动] 出错：{type(exc).__name__}: {exc}")

    async def _maybe_proactive(self) -> None:
        config = settings.load().get("humanize", {})
        if not config.get("proactive", False) or not config.get("enabled", True):
            return
        if self._socket is None:
            return

        today = time.strftime("%Y-%m-%d")
        if self._proactive_date != today:
            self._proactive_date = today
            self._proactive_count = 0
        if self._proactive_count >= int(config.get("proactive_daily_max", 3)):
            return

        interval = max(5, int(config.get("proactive_interval_minutes", 60))) * 60
        if time.time() - self._proactive_last < interval:
            return
        self._proactive_last = time.time()

        if not humanize.proactive_due():
            return

        now = time.time()
        pool = [item for item in self._active.values() if now - item["last"] < 6 * 3600]
        if not pool:
            return
        target = random.choice(pool)

        clock = time.strftime("%H:%M")
        hint = (
            f"（现在是 {clock}，{('群里' if target['kind'] == 'group' else '私聊里')}暂时没人说话。"
            "你闲着，想主动开一句口，像真人那样随口问一句：吃了没、在干嘛、还不睡啊之类。"
            "只回一句，别解释，别用括号写动作。）"
        )

        try:
            line = await asyncio.to_thread(
                brain.chat,
                [{"role": "user", "content": hint}],
                str(target["scene"]),
                None,
                {"user": "", "kind": target["kind"], "group": target.get("id")},
            )
        except Exception as exc:
            print(f"[主动] 生成失败：{type(exc).__name__}")
            return

        line = (line or "").strip()
        if not line:
            return

        await asyncio.sleep(humanize.read_delay() + humanize.typing_time(line))
        if target["kind"] == "group":
            await self._send(
                self._socket,
                "send_group_msg",
                {"group_id": target["id"], "message": line},
            )
        else:
            await self._send(
                self._socket,
                "send_private_msg",
                {"user_id": target["id"], "message": line},
            )
        self._proactive_count += 1
        self.state.log("主动", str(target["id"]), "（她主动开口）", line)
        print(f"[主动] {line}")

    # -- 定时提醒 --------------------------------------------------------

    async def _reminder_loop(self) -> None:
        while True:
            await asyncio.sleep(15)
            try:
                for item in reminders.due():
                    await self._fire(item)
            except Exception as exc:
                print(f"[提醒] 循环出错：{type(exc).__name__}: {exc}")

    async def _fire(self, item: dict[str, Any]) -> None:
        template = str(
            settings.load().get("reminder", {}).get("message")
            or "到点了：{text}"
        )
        text = template.replace("{text}", str(item.get("text") or ""))
        try:
            if item.get("kind") == "group" and item.get("group"):
                await self.push(
                    "send_group_msg",
                    {"group_id": int(item["group"]), "message": text},
                )
            else:
                await self.push(
                    "send_private_msg",
                    {"user_id": int(item["user"]), "message": text},
                )
            reminders.advance(item)
            print(f"[提醒] 已推送：{item.get('text')}")
        except Exception as exc:
            print(f"[提醒] 推送失败：{type(exc).__name__}: {exc}")
            reminders.fail(item)

    async def push(self, action: str, params: dict[str, Any]) -> None:
        socket = self._socket
        if socket is None:
            raise RuntimeError("NapCat 没连上")
        await self._send(socket, action, params)

    # -- 视频下载 --------------------------------------------------------

    async def _deliver_video(
        self, socket: Any, action: str, params: dict[str, Any], url: str
    ) -> None:
        print(f"[视频] 开始下载：{url}")
        try:
            path, title = await asyncio.to_thread(video.download, url)
        except Exception as exc:
            print(f"[视频] 失败：{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await self._send(
                    socket, action, {**params, "message": f"没下下来：{exc}"}
                )
            return

        size_mb = path.stat().st_size / 1024 / 1024
        print(f"[视频] 下好了：{path.name}（{size_mb:.1f}MB）")

        try:
            if action == "send_group_msg":
                await self._send(
                    socket,
                    "upload_group_file",
                    {
                        "group_id": params["group_id"],
                        "file": str(path),
                        "name": path.name,
                    },
                )
            else:
                await self._send(
                    socket,
                    "upload_private_file",
                    {
                        "user_id": params["user_id"],
                        "file": str(path),
                        "name": path.name,
                    },
                )
            await self._send(
                socket,
                action,
                {
                    **params,
                    "message": f"《{title}》下好了，{size_mb:.1f}MB。本地文件本鱼过会儿就删。",
                },
            )
        except Exception as exc:
            print(f"[视频] 发送失败：{type(exc).__name__}: {exc}")
            with contextlib.suppress(Exception):
                await self._send(
                    socket, action, {**params, "message": f"下好了但发不出去：{exc}"}
                )
        finally:
            keep = int(settings.load().get("video", {}).get("keep_seconds", 180))
            asyncio.create_task(self._cleanup_file(path, keep))

    async def _cleanup_file(self, path: Any, delay: int) -> None:
        await asyncio.sleep(max(10, delay))
        try:
            path.unlink(missing_ok=True)
            print(f"[视频] 本地文件已删除：{path.name}")
        except OSError:
            pass
