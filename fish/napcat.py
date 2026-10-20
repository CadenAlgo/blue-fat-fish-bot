"""NapCat 进程管理：找账号、写连接配置、快速登录、盯二维码。"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path

from . import settings
from .state import State

CREATE_NEW_CONSOLE = 0x00000010
CLIENT_NAME = "erhua"

_process: subprocess.Popen | None = None
_patched = False


def napcat_dir() -> Path:
    raw = str(settings.load()["napcat"]["dir"])
    path = Path(raw)
    if not path.is_absolute():
        path = (settings.ROOT / raw).resolve()
    return path


def config_dir() -> Path:
    return napcat_dir() / "napcat" / "config"


def onebot_config() -> Path | None:
    directory = config_dir()
    if not directory.exists():
        return None
    files = sorted(directory.glob("onebot11_*.json"))
    return files[0] if files else None


def account() -> str:
    path = onebot_config()
    if path is None:
        return ""
    return path.stem.split("_", 1)[1] if "_" in path.stem else ""


def ensure_client(port: int) -> bool:
    """让 NapCat 把事件推到我们这里；返回是否真的改了文件。"""
    path = onebot_config()
    if path is None:
        return False

    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False

    network = data.setdefault("network", {})
    url = f"ws://127.0.0.1:{port}"
    original = list(network.get("websocketClients") or [])

    same_url = [c for c in original if c.get("url") == url]
    if (
        len(same_url) == 1
        and same_url[0].get("name") == CLIENT_NAME
        and same_url[0].get("enable")
    ):
        return False

    # 同一个地址只留一条，别留历史遗留的旧名字
    clients = [c for c in original if c.get("url") != url]
    clients.append(
        {
            "name": CLIENT_NAME,
            "enable": True,
            "url": url,
            "reportSelfMessage": False,
            "messagePostFormat": "array",
            "token": "",
            "debug": False,
            "heartInterval": 30000,
            "reconnectInterval": 3000,
        }
    )
    network["websocketClients"] = clients

    with path.open("w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False, indent=2)
    return True


def is_running() -> bool:
    """看有没有带 napcat 字样的 node 进程（WebUI 只监听 IPv6，不能靠端口判断）。"""
    if _process is not None and _process.poll() is None:
        return True

    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -like '*napcat*' } | "
        "Measure-Object | Select-Object -ExpandProperty Count"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=25,
        )
    except Exception:
        return False
    return result.stdout.strip() not in ("", "0")


def qrcode_path() -> Path:
    return napcat_dir() / "napcat" / "cache" / "qrcode.png"


def start() -> str:
    global _process
    bat = napcat_dir() / "napcat.bat"
    if not bat.exists():
        return f"找不到 NapCat：{bat}"

    if is_running():
        return "NapCat 已经在运行"

    args = ["cmd", "/c", str(bat)]
    uin = account()
    if uin:
        args += ["-q", uin]

    _process = subprocess.Popen(
        args,
        cwd=str(napcat_dir()),
        creationflags=CREATE_NEW_CONSOLE,
    )
    return "NapCat 启动中" + (f"（快速登录 {uin}）" if uin else "（需要扫码）")


def _kill_all() -> int:
    """按命令行匹配，杀掉所有 NapCat 的 node 进程（包括不是本次启动的）。"""
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name='node.exe'\" | "
        "Where-Object { $_.CommandLine -like '*napcat*' } | "
        "ForEach-Object { Stop-Process -Id $_.ProcessId -Force -ErrorAction SilentlyContinue; "
        "$_.ProcessId }"
    )
    try:
        result = subprocess.run(
            ["powershell", "-NoProfile", "-Command", script],
            capture_output=True,
            text=True,
            timeout=40,
        )
    except Exception:
        return 0
    return len([line for line in (result.stdout or "").splitlines() if line.strip().isdigit()])


def stop() -> str:
    global _process
    if _process is not None and _process.poll() is None:
        subprocess.run(
            ["taskkill", "/PID", str(_process.pid), "/T", "/F"],
            capture_output=True,
            check=False,
        )
        _process = None

    killed = _kill_all()
    time.sleep(1.5)
    return f"NapCat 已停止（清掉 {killed} 个进程）" if killed else "NapCat 已经停了"


def restart() -> str:
    ensure_client(int(settings.load()["server"]["onebot_port"]))
    stop()
    killed = _kill_all()
    if killed:
        print(f"[NapCat] 又清掉 {killed} 个残留进程")
    time.sleep(2.5)
    return start()


async def watch(state: State) -> None:
    """盯着二维码：只有这次启动新生成的、而且是真需要登录时才弹出来。"""
    global _patched
    started = time.time()
    last_mtime = started

    while True:
        await asyncio.sleep(2)

        path = qrcode_path()
        try:
            mtime = path.stat().st_mtime
        except OSError:
            mtime = 0.0

        # 必须是这次启动之后新写的图，且还没登录，才值得弹
        if mtime > max(started, last_mtime) and not state.napcat_online:
            last_mtime = mtime
            try:
                os.startfile(str(path))  # type: ignore[attr-defined]
                print(f"[二维码] 需要登录，已打开：{path}")
            except Exception:
                print(f"[二维码] 需要登录，图片在：{path}")

        # 首次登录会新建配置文件：补上连接配置，等下次重启生效
        if not _patched and not state.napcat_online and onebot_config() is not None:
            _patched = True
            port = int(settings.load()["server"]["onebot_port"])
            if ensure_client(port):
                print("[配置] 已写入连接配置，重启 NapCat 后生效（控制台有按钮）")
