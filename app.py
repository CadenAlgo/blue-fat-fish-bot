"""大肥鱼启动器：QQ 通道 + 本地控制台 + NapCat 托管。

运行：python app.py（或双击 启动大肥鱼.bat）
"""

from __future__ import annotations

import asyncio
import contextlib
import socket
import sys
import time
from pathlib import Path

import uvicorn

from fish import napcat, onebot, settings, video, webui
from fish.state import STATE


class Tee:
    """控制台和日志文件同时写一份。"""

    def __init__(self, *streams: object) -> None:
        self.streams = streams

    def write(self, data: str) -> None:
        for stream in self.streams:
            try:
                stream.write(data)  # type: ignore[union-attr]
                stream.flush()  # type: ignore[union-attr]
            except Exception:
                pass

    def flush(self) -> None:
        for stream in self.streams:
            try:
                stream.flush()  # type: ignore[union-attr]
            except Exception:
                pass

    def isatty(self) -> bool:
        try:
            return bool(self.streams[0].isatty())  # type: ignore[union-attr]
        except Exception:
            return False

    def fileno(self) -> int:
        return self.streams[0].fileno()  # type: ignore[union-attr]

    @property
    def encoding(self) -> str:
        try:
            return str(self.streams[0].encoding)  # type: ignore[union-attr]
        except Exception:
            return "utf-8"


def port_busy(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) == 0


async def _open_browser_later(url: str, delay: float) -> None:
    """等端口真的起来了再开浏览器，免得一打开就是"无法连接"。"""
    await asyncio.sleep(delay)
    with contextlib.suppress(Exception):
        import webbrowser

        webbrowser.open(url)


def setup_log() -> Path:
    log_path = settings.DATA_DIR / "app.log"
    settings.DATA_DIR.mkdir(parents=True, exist_ok=True)
    handle = log_path.open("a", encoding="utf-8")
    handle.write(f"\n===== {time.strftime('%Y-%m-%d %H:%M:%S')} 启动 =====\n")
    handle.flush()
    sys.stdout = Tee(sys.__stdout__, handle)  # type: ignore[assignment]
    sys.stderr = Tee(sys.__stderr__, handle)  # type: ignore[assignment]
    return log_path


async def main() -> None:
    settings.ensure()
    leftover = video.cleanup(max_age=3600)
    if leftover:
        print(f"[清理] 上次残留的 {leftover} 个下载文件已删掉")
    config = settings.load()
    onebot_port = int(config["server"]["onebot_port"])
    web_port = int(config["server"]["webui_port"])

    # 先确认端口没被占，再决定要不要拉 NapCat，避免起出第二个 QQ
    if port_busy(onebot_port) or port_busy(web_port):
        print("[启动失败] 已经有一个大肥鱼在运行（端口被占用）。")
        print("要重新开始，先双击 关闭大肥鱼.bat，再启动。")
        return

    napcat.ensure_client(onebot_port)
    if config["napcat"].get("auto_start", True):
        print("[NapCat]", napcat.start())

    server = onebot.OneBotServer(STATE)
    app = webui.create_app(STATE)

    uv = uvicorn.Server(
        uvicorn.Config(app, host="127.0.0.1", port=web_port, log_level="warning")
    )
    uv.install_signal_handlers = lambda: None  # type: ignore[method-assign]

    url = f"http://127.0.0.1:{web_port}"
    print(f"[控制台] {url}（马上会自动打开浏览器）")
    if config["server"].get("open_browser"):
        asyncio.create_task(_open_browser_later(url, 2.5))

    try:
        await asyncio.gather(server.run(), uv.serve(), napcat.watch(STATE))
    except OSError as exc:
        print(f"[启动失败] 端口被占用：{exc}")
        print("先双击 关闭大肥鱼.bat，再重新启动。")


if __name__ == "__main__":
    setup_log()
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(main())
    print("收工。")
