"""视频解析下载：B站、抖音、快手等，走 yt-dlp。

下载到 data/downloads，发完给 QQ 后按配置延时删除。
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

from . import settings

HOSTS = (
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "xiaohongshu.com",
    "xhslink.com",
    "weibo.com",
    "weibo.cn",
    "youtube.com",
    "youtu.be",
    "twitter.com",
    "x.com",
    "instagram.com",
)

# 国内站点必须直连：走代理会被当成境外 IP，直接 geo 拦截
DOMESTIC = (
    "bilibili.com",
    "b23.tv",
    "douyin.com",
    "iesdouyin.com",
    "kuaishou.com",
    "xiaohongshu.com",
    "xhslink.com",
    "weibo.com",
    "weibo.cn",
)

PROXY_KEYS = (
    "HTTP_PROXY",
    "HTTPS_PROXY",
    "ALL_PROXY",
    "http_proxy",
    "https_proxy",
    "all_proxy",
)

URL_RE = re.compile(r"https?://[^\s<>\"'，。！？、）\)]+")


def _host(url: str) -> str:
    return urlparse(url).netloc.lower()


def _matches(host: str, domains: tuple[str, ...]) -> bool:
    return any(host == item or host.endswith("." + item) for item in domains)


def download_dir() -> Path:
    path = settings.DATA_DIR / "downloads"
    path.mkdir(parents=True, exist_ok=True)
    return path


def files() -> list[Path]:
    directory = settings.DATA_DIR / "downloads"
    if not directory.exists():
        return []
    try:
        return [item for item in directory.iterdir() if item.is_file()]
    except OSError:
        return []


def stats() -> dict[str, object]:
    items = files()
    total = 0
    for item in items:
        try:
            total += item.stat().st_size
        except OSError:
            pass
    return {
        "path": str(settings.DATA_DIR / "downloads"),
        "count": len(items),
        "mb": round(total / 1024 / 1024, 1),
    }


def cleanup(max_age: int = 0) -> int:
    """删掉下载目录里的文件；max_age=0 表示全删。返回删了几个。"""
    now = time.time()
    removed = 0
    for item in files():
        try:
            if max_age <= 0 or now - item.stat().st_mtime > max_age:
                item.unlink(missing_ok=True)
                removed += 1
        except OSError:
            continue
    return removed


def find_url(text: str) -> str | None:
    for match in URL_RE.finditer(text or ""):
        url = match.group(0)
        if _matches(_host(url), HOSTS):
            return url
    return None


def _ffmpeg() -> str:
    try:
        import imageio_ffmpeg

        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return ""


def _resolve_short(url: str) -> str:
    """b23.tv 这类短链要先解析成真正的视频页，否则 yt-dlp 会把落地页当视频下。"""
    if "b23.tv" not in _host(url):
        return url

    try:
        import httpx

        response = httpx.get(url, timeout=20.0, follow_redirects=True, headers={"User-Agent": "Mozilla/5.0"})
    except Exception as exc:
        raise RuntimeError(f"短链打不开：{type(exc).__name__}") from exc

    final = str(response.url)
    if "bilibili.com/video" in final:
        return final

    match = re.search(r"https?://(?:www\.)?bilibili\.com/video/[A-Za-z0-9]+", response.text)
    if match:
        return match.group(0)

    if '"code":-404' in response.text or "啥都木有" in response.text:
        raise RuntimeError("这个短链已经失效了（视频可能被删），换个完整链接试试")
    raise RuntimeError("短链没解析出视频地址")


def download(url: str, timeout: int = 900) -> tuple[Path, str]:
    """返回 (文件路径, 标题)。失败抛 RuntimeError。"""
    config = settings.load().get("video", {})
    if not config.get("enabled", True):
        raise RuntimeError("视频下载没开")

    url = _resolve_short(url)

    target = download_dir() / "%(title).70B.%(ext)s"
    cmd = [
        sys.executable,
        "-m",
        "yt_dlp",
        "--no-playlist",
        "--no-warnings",
        "--no-progress",
        "--no-part",
        "-f",
        "bv*+ba/b",
        "-S",
        "res:1080,codec:h264",  # 优先 1080P 的 H.264，手机兼容性好
        "--merge-output-format",
        "mp4",
        "-o",
        str(target),
        "--print",
        "after_move:filepath",
        url,
    ]
    ffmpeg = _ffmpeg()
    if ffmpeg:
        cmd[4:4] = ["--ffmpeg-location", ffmpeg]

    cookies_file = str(config.get("cookies_file") or "").strip()
    cookies_browser = str(config.get("cookies_browser") or "").strip()
    if cookies_file and Path(cookies_file).exists():
        cmd[4:4] = ["--cookies", str(Path(cookies_file))]
    elif cookies_browser:
        cmd[4:4] = ["--cookies-from-browser", cookies_browser]

    env = os.environ.copy()
    # 强制子进程用 UTF-8 输出，否则中文文件名会以 GBK 写进管道，被我们按 UTF-8 读成乱码
    env["PYTHONIOENCODING"] = "utf-8"
    env["PYTHONUTF8"] = "1"
    if _matches(_host(url), DOMESTIC):
        for key in PROXY_KEYS:
            env.pop(key, None)

    started = time.time()
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("下载超时了") from exc

    if result.returncode != 0:
        lines = [
            line.strip()
            for line in (result.stderr or result.stdout or "").splitlines()
            if line.strip()
        ]
        reason = lines[-1][:140] if lines else "yt-dlp 失败"
        raise RuntimeError(reason)

    paths = [line.strip() for line in (result.stdout or "").splitlines() if line.strip()]

    path: Path | None = None
    for line in reversed(paths):
        candidate = Path(line)
        if candidate.exists():
            path = candidate
            break

    if path is None:
        # 路径可能被编码搞坏，退一步找下载目录里刚生成的文件
        fresh = [
            item
            for item in files()
            if item.stat().st_mtime >= started - 5 and item.suffix.lower() != ".part"
        ]
        if fresh:
            path = max(fresh, key=lambda item: item.stat().st_mtime)

    if path is None:
        raise RuntimeError("文件没下下来（yt-dlp 说成功，但找不到产物）")

    # 有些链接会被当成网页下载下来，产物根本不是视频
    if path.suffix.lower() in (".unknown_video", ".part", ".json", ".html", ".htm"):
        path.unlink(missing_ok=True)
        raise RuntimeError("这个链接没解析出视频（可能视频已删或需要登录）")
    if path.stat().st_size < 100 * 1024:
        size_kb = path.stat().st_size / 1024
        path.unlink(missing_ok=True)
        raise RuntimeError(f"下出来的东西只有 {size_kb:.0f}KB，不像视频，已丢弃")

    limit = float(config.get("max_mb", 800)) * 1024 * 1024
    if path.stat().st_size > limit:
        size = path.stat().st_size / 1024 / 1024
        path.unlink(missing_ok=True)
        raise RuntimeError(f"文件 {size:.0f}MB，超过上限了")

    return path, path.stem
