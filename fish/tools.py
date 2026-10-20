"""工具：天气、联网搜索、读网页。模型通过 JSON Schema 调用它们。"""

from __future__ import annotations

import hashlib
import html as html_lib
import json
import random
import re
import time
from datetime import datetime, timedelta
from html.parser import HTMLParser
from pathlib import Path
from typing import Any

import httpx

from . import reminders, settings

UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)
TIMEOUT = httpx.Timeout(25.0)

# ---------------------------------------------------------------------------
# 天气
# ---------------------------------------------------------------------------

WEATHER_TEXT = {
    0: "晴", 1: "晴间多云", 2: "多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    56: "冻毛毛雨", 57: "强冻毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    66: "冻雨", 67: "强冻雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    77: "雪粒",
    80: "阵雨", 81: "强阵雨", 82: "暴雨",
    85: "阵雪", 86: "强阵雪",
    95: "雷阵雨", 96: "雷阵雨带冰雹", 99: "强雷暴带冰雹",
}

PROVINCES = (
    "北京", "天津", "上海", "重庆", "河北", "山西", "辽宁", "吉林", "黑龙江",
    "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南", "湖北", "湖南",
    "广东", "海南", "四川", "贵州", "云南", "陕西", "甘肃", "青海", "台湾",
    "内蒙古", "广西", "西藏", "宁夏", "新疆", "香港", "澳门",
)

GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


def _http(method: str, url: str, **kwargs: Any) -> httpx.Response:
    """优先走系统代理；代理挂了就直连再试一次。"""
    try:
        return httpx.request(method, url, timeout=TIMEOUT, follow_redirects=True, **kwargs)
    except httpx.TransportError:
        return httpx.request(
            method, url, timeout=TIMEOUT, follow_redirects=True, trust_env=False, **kwargs
        )


def _request(url: str, **kwargs: Any) -> httpx.Response:
    return _http("GET", url, **kwargs)


def _at(seq: Any, index: int) -> Any:
    return seq[index] if isinstance(seq, list) and index < len(seq) else None


def _sky(code: Any) -> str:
    return WEATHER_TEXT.get(code, "看不太懂的天")


def _city_variants(city: str) -> list[str]:
    """地名库里写法不统一，同一座城市可能只认「佛山」或只认「佛山市」，多试几种。"""
    city = city.strip()
    variants = [city]

    trimmed = city
    for suffix in ("特别行政区", "自治区", "省", "市"):
        if trimmed.endswith(suffix) and len(trimmed) > len(suffix):
            trimmed = trimmed[: -len(suffix)]
            variants.append(trimmed)
            break

    for province in PROVINCES:
        if city.startswith(province) and len(city) > len(province):
            rest = city[len(province) :].lstrip("省市区 ")
            if rest:
                variants.append(rest)
                variants.append(rest + "市")
            break

    variants.append(trimmed + "市")

    result: list[str] = []
    for item in variants:
        item = item.strip()
        if item and item not in result:
            result.append(item)
    return result[:4]


def _lookup_places(city: str) -> list[dict[str, Any]]:
    merged: list[dict[str, Any]] = []
    seen: set[tuple[Any, ...]] = set()

    for variant in _city_variants(city):
        try:
            results = (
                _request(
                    GEOCODE_URL,
                    params={"name": variant, "count": 8, "language": "zh", "format": "json"},
                )
                .json()
                .get("results")
                or []
            )
        except Exception:
            continue
        for item in results:
            key = (item.get("name"), item.get("admin1"))
            if key in seen:
                continue
            seen.add(key)
            merged.append(item)

    if not merged:
        return []

    chinese = [item for item in merged if item.get("country_code") == "CN"]
    pool = chinese or merged

    for province in PROVINCES:
        if city.startswith(province):
            matched = [
                item for item in pool if str(item.get("admin1") or "").startswith(province)
            ]
            if matched:
                pool = matched
            break

    pool.sort(key=lambda item: item.get("population") or 0, reverse=True)
    return pool


def get_weather(city: str, days: int = 1) -> str:
    try:
        days = max(1, min(int(days), 3))
    except (TypeError, ValueError):
        days = 1

    candidates = _lookup_places(city)
    if not candidates:
        return f"没查到「{city}」这个地方，换个说法试试。"

    place = candidates[0]
    try:
        data = _request(
            "https://api.open-meteo.com/v1/forecast",
            params={
                "latitude": place["latitude"],
                "longitude": place["longitude"],
                "current": (
                    "temperature_2m,apparent_temperature,"
                    "relative_humidity_2m,weather_code,wind_speed_10m"
                ),
                "daily": (
                    "weather_code,temperature_2m_max,"
                    "temperature_2m_min,precipitation_probability_max"
                ),
                "timezone": "auto",
                "forecast_days": days,
            },
        ).json()
    except Exception as exc:
        return f"天气接口连不上：{exc}"

    name = place.get("name") or city
    region = place.get("admin1") or ""
    current = data.get("current") or {}

    lines = [
        f"{name}{f'（{region}）' if region else ''}：{current.get('temperature_2m')}°C，"
        f"{_sky(current.get('weather_code'))}，"
        f"体感 {current.get('apparent_temperature')}°C，"
        f"湿度 {current.get('relative_humidity_2m')}%，"
        f"风速 {current.get('wind_speed_10m')} km/h"
    ]

    daily = data.get("daily") or {}
    for index, day in enumerate(daily.get("time") or []):
        low = _at(daily.get("temperature_2m_min"), index)
        high = _at(daily.get("temperature_2m_max"), index)
        rain = _at(daily.get("precipitation_probability_max"), index)
        lines.append(
            f"{day}：{_sky(_at(daily.get('weather_code'), index))}，"
            f"{low}~{high}°C，降水概率 {rain}%"
        )

    others = [
        f"{item.get('name')}（{item.get('admin1') or item.get('country') or '未知'}）"
        for item in candidates[1:4]
    ]
    if others:
        lines.append("（同名地点还有：" + "、".join(others) + "；要的不是这个就说清楚省份）")

    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 联网搜索
# ---------------------------------------------------------------------------

def _clean(fragment: str) -> str:
    text = re.sub(r"<[^>]+>", " ", fragment)
    text = html_lib.unescape(text)
    return re.sub(r"\s+", " ", text).strip()


def _bing(query: str, count: int) -> list[dict[str, str]]:
    response = _request(
        "https://cn.bing.com/search",
        params={"q": query, "setlang": "zh-Hans", "count": count},
        headers={"User-Agent": UA, "Accept-Language": "zh-CN,zh;q=0.9"},
    )
    response.raise_for_status()
    page = response.text

    results: list[dict[str, str]] = []
    for block in re.findall(r'<li class="b_algo.*?</li>', page, re.S):
        heading = re.search(r"<h2[^>]*>(.*?)</h2>", block, re.S)
        if not heading:
            continue
        link = re.search(r'href="([^"]+)"[^>]*>(.*?)</a>', heading.group(1), re.S)
        if not link:
            continue
        url = html_lib.unescape(link.group(1))
        if url.startswith("/"):
            continue
        snippet = ""
        para = re.search(r"<p[^>]*>(.*?)</p>", block, re.S)
        if para:
            snippet = _clean(para.group(1))
        results.append(
            {"title": _clean(link.group(2)), "url": url, "snippet": snippet[:180]}
        )
        if len(results) >= count:
            break
    return results


def _duckduckgo(query: str, count: int) -> list[dict[str, str]]:
    response = _request(
        "https://html.duckduckgo.com/html/",
        params={"q": query},
        headers={"User-Agent": UA},
    )
    response.raise_for_status()
    page = response.text

    results: list[dict[str, str]] = []
    links = re.findall(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', page, re.S)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</a>', page, re.S)
    for index, (url, title) in enumerate(links[:count]):
        results.append(
            {
                "title": _clean(title),
                "url": html_lib.unescape(url),
                "snippet": _clean(snippets[index])[:180] if index < len(snippets) else "",
            }
        )
    return results


def _auth() -> tuple[str, dict[str, str]]:
    api_key, base_url = settings.llm()
    if not api_key:
        raise RuntimeError("没有 API key")
    return base_url.rstrip("/"), {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }


def _deepseek_only() -> tuple[str, dict[str, str]]:
    """官方搜索/抓取只有 DeepSeek 有；换成别家就跳过，交给 Bing 兜底。"""
    base_url, headers = _auth()
    if "deepseek" not in base_url:
        raise RuntimeError("当前接口没有内置搜索")
    return base_url, headers


def _official_search(query: str, count: int) -> list[dict[str, Any]]:
    """DeepSeek 自研搜索引擎：POST {base}/tools/search。"""
    base_url, headers = _deepseek_only()
    response = _http(
        "POST",
        f"{base_url}/tools/search",
        json={"query": query, "count": count},
        headers=headers,
    )
    if response.status_code == 402:
        raise RuntimeError("账户余额不足")
    response.raise_for_status()
    return response.json().get("results") or []


def _format_official(query: str, results: list[dict[str, Any]]) -> str:
    lines = [f"「{query}」的搜索结果（DeepSeek 搜索引擎）："]
    for index, item in enumerate(results, 1):
        head = f"{index}. {item.get('title') or '（无标题）'}"
        meta: list[str] = []
        if item.get("site_name"):
            meta.append(str(item["site_name"]))
        if item.get("date"):
            meta.append(str(item["date"])[:10])
        if item.get("authority") not in (None, -1):
            meta.append(f"权威度{item['authority']}")
        if meta:
            head += "（" + " · ".join(meta) + "）"
        lines.append(head)
        body = str(item.get("summary") or item.get("snippet") or "").replace("\n", " ")
        if body:
            lines.append("   " + body[:400])
        lines.append(f"   {item.get('url')}")
    lines.append("（要细节就 fetch_page 打开对应链接）")
    return "\n".join(lines)


def web_search(query: str, count: int = 5) -> str:
    try:
        count = max(1, min(int(count), 8))
    except (TypeError, ValueError):
        count = 5

    try:
        official = _official_search(query, count)
    except Exception:
        official = []
    if official:
        return _format_official(query, official)

    # 官方接口不可用时退回自己抓
    results: list[dict[str, str]] = []
    errors: list[str] = []
    for name, engine in (("bing", _bing), ("duckduckgo", _duckduckgo)):
        try:
            results = engine(query, count)
        except Exception as exc:
            errors.append(f"{name}: {type(exc).__name__}")
            continue
        if results:
            break

    if not results:
        return f"没搜到「{query}」的结果。" + (f"（{'，'.join(errors)}）" if errors else "")

    lines = [f"「{query}」的搜索结果："]
    for index, item in enumerate(results, 1):
        lines.append(f"{index}. {item['title']}")
        if item["snippet"]:
            lines.append(f"   {item['snippet']}")
        lines.append(f"   {item['url']}")
    lines.append("（需要细节就 fetch_page 打开对应链接）")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 读网页
# ---------------------------------------------------------------------------

class _TextExtractor(HTMLParser):
    SKIP = {"script", "style", "noscript", "iframe", "svg"}
    BREAK = {"p", "br", "div", "li", "tr", "h1", "h2", "h3", "h4", "section", "article"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self._skip = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag in self.SKIP:
            self._skip += 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_endtag(self, tag: str) -> None:
        if tag in self.SKIP and self._skip:
            self._skip -= 1
        elif tag in self.BREAK:
            self.parts.append("\n")

    def handle_data(self, data: str) -> None:
        if not self._skip:
            self.parts.append(data)

    def text(self) -> str:
        raw = "".join(self.parts)
        lines = [re.sub(r"[ \t\u3000]+", " ", line).strip() for line in raw.splitlines()]
        return "\n".join(line for line in lines if line)


def _official_fetch(url: str) -> tuple[str, str]:
    """DeepSeek 抓取接口：POST {base}/tools/fetch，返回 Markdown 正文。"""
    base_url, headers = _deepseek_only()
    response = _http(
        "POST", f"{base_url}/tools/fetch", json={"url": url}, headers=headers
    )
    response.raise_for_status()
    data = response.json()
    return str(data.get("title") or ""), str(data.get("content") or "")


def fetch_page(url: str, max_chars: int = 3000) -> str:
    if not url.startswith(("http://", "https://")):
        return "只支持 http/https 链接。"
    try:
        max_chars = max(500, min(int(max_chars), 8000))
    except (TypeError, ValueError):
        max_chars = 3000

    try:
        title, content = _official_fetch(url)
    except Exception:
        title, content = "", ""

    if content.strip():
        text = content.strip()
        if len(text) > max_chars:
            text = text[:max_chars] + "\n……（太长截断了）"
        return (f"《{title}》\n" if title else "") + text

    try:
        response = _request(url, headers={"User-Agent": UA})
        response.raise_for_status()
    except Exception as exc:
        return f"打不开这个网页：{type(exc).__name__}"

    content_type = response.headers.get("content-type", "")
    if "html" not in content_type and "text" not in content_type:
        return f"这个链接不是网页（{content_type or '未知类型'}），读不了。"

    parser = _TextExtractor()
    try:
        parser.feed(response.text)
    except Exception:
        pass
    text = parser.text() or response.text
    text = re.sub(r"\n{3,}", "\n\n", text)
    if len(text) > max_chars:
        text = text[:max_chars] + "\n……（后面还有，内容太长截断了）"
    return text or "这个页面没读出正文。"


# ---------------------------------------------------------------------------
# 快递
# ---------------------------------------------------------------------------

EXPRESS_COMPANIES = {
    "shunfeng": "顺丰",
    "yuantong": "圆通",
    "zhongtong": "中通",
    "yunda": "韵达",
    "shentong": "申通",
    "jd": "京东",
    "jtexpress": "极兔",
    "ems": "EMS",
    "youzhengguonei": "邮政",
    "debangkuaidi": "德邦",
}

EXPRESS_STATE = {
    "0": "在途",
    "1": "已揽收",
    "2": "疑难件",
    "3": "已签收",
    "4": "已退签",
    "5": "派送中",
    "6": "退回中",
    "7": "转投中",
    "8": "清关中",
    "10": "待清关",
    "11": "清关中",
    "12": "已清关",
    "13": "清关异常",
    "14": "拒签",
}

_express_cache: dict[str, tuple[float, str]] = {}


def _express_candidates(number: str) -> list[str]:
    head = number.upper()
    first: list[str] = []
    if head.startswith("SF"):
        first.append("shunfeng")
    elif head.startswith("YT"):
        first.append("yuantong")
    elif head.startswith("JD"):
        first.append("jd")
    elif head.startswith("1Z"):
        first.append("ups")
    elif head.startswith(("EA", "EB", "EQ", "EN")):
        first.append("ems")
    elif head.startswith(("773", "778", "768", "680", "781")):
        first.append("zhongtong")
    elif head.startswith(("43", "45", "46")):
        first.append("yunda")
    elif head.startswith(("98", "77", "66", "55", "88")):
        first.append("shentong")
    elif head.startswith(("DP", "JDV")):
        first.append("debangkuaidi")

    return first + [code for code in EXPRESS_COMPANIES if code not in first]


def _express_official(number: str, company: str) -> dict[str, Any] | None:
    """填了快递100 的 key 就走官方接口，没填就返回 None。"""
    config = settings.load().get("express", {})
    api_key = str(config.get("api_key") or "").strip()
    customer = str(config.get("customer") or "").strip()
    if not api_key or not customer:
        return None

    param = json.dumps({"com": company, "num": number, "resultv2": "1"}, ensure_ascii=False)
    sign = hashlib.md5((param + api_key + customer).encode("utf-8")).hexdigest().upper()
    try:
        response = _http(
            "POST",
            "https://poll.kuaidi100.com/poll/query.do",
            data={"customer": customer, "sign": sign, "param": param},
        )
        data = response.json()
    except Exception:
        return None
    if str(data.get("status")) != "200" or not data.get("data"):
        return None
    return data


def _express_query(number: str, company: str) -> dict[str, Any] | None:
    official = _express_official(number, company)
    if official:
        return official

    response = _request(
        "https://www.kuaidi100.com/query",
        params={"type": company, "postid": number, "temp": str(time.time())},
        headers={"User-Agent": UA, "Referer": "https://www.kuaidi100.com/"},
    )
    response.raise_for_status()
    try:
        data = response.json()
    except Exception:
        return None
    if str(data.get("status")) != "200" or not data.get("data"):
        return None
    return data


def _format_express(number: str, company: str, data: dict[str, Any]) -> str:
    name = EXPRESS_COMPANIES.get(company, company)
    state = EXPRESS_STATE.get(str(data.get("state")), "状态未知")
    lines = [f"{name} {number}：{state}"]
    for event in data.get("data", [])[:5]:
        stamp = str(event.get("time", ""))[5:16]
        context = str(event.get("context", "")).strip()
        if context:
            lines.append(f"{stamp} {context}")
    return "\n".join(lines)


def get_express(number: str, carrier: str = "") -> str:
    number = "".join(str(number).split())
    if not number:
        return "单号是空的。"

    key = f"{number}|{carrier}"
    cached = _express_cache.get(key)
    if cached and time.time() - cached[0] < 300:
        return cached[1]

    companies = [carrier.strip()] if carrier.strip() else _express_candidates(number)[:10]

    for company in companies:
        try:
            data = _express_query(number, company)
        except Exception:
            continue
        if not data:
            continue
        text = _format_express(number, company, data)
        _express_cache[key] = (time.time(), text)
        return text

    return (
        f"没查到 {number} 的物流。可能是单号还没录入，或者快递公司猜错了——"
        "告诉我是哪家（顺丰/中通/圆通/韵达/申通/京东/极兔/EMS/邮政/德邦）。"
    )


# ---------------------------------------------------------------------------
# 定时提醒
# ---------------------------------------------------------------------------

def _parse_when(raw: str) -> float | None:
    text = str(raw).strip().replace("T", " ")
    now = datetime.now()

    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except ValueError:
            continue

    try:
        moment = datetime.strptime(text, "%m-%d %H:%M").replace(year=now.year)
        if moment < now:
            moment = moment.replace(year=now.year + 1)
        return moment.timestamp()
    except ValueError:
        pass

    try:
        moment = datetime.strptime(text, "%H:%M").replace(
            year=now.year, month=now.month, day=now.day
        )
        if moment <= now:
            moment = moment + timedelta(days=1)
        return moment.timestamp()
    except ValueError:
        return None


def set_reminder(
    when: str,
    what: str,
    repeat: str = "none",
    weekday: int = 0,
    ctx: dict[str, Any] | None = None,
) -> str:
    if not ctx or not ctx.get("user"):
        return "现在没拿到你是谁，定不了提醒。"

    due = _parse_when(when)
    if due is None:
        return "时间没看懂。用「2026-10-21 07:00」这种格式。"

    now = time.time()
    if due <= now + 30:
        return "这个时间已经过了，往后定一点。"
    if due > now + 400 * 86400:
        return "定得太远了，最多一年内。"

    daily = str(repeat).lower()
    if daily in ("weekly", "每周"):
        mode = "weekly"
    elif daily in ("weekdays", "工作日"):
        mode = "weekdays"
    elif daily in ("daily", "每天", "每日"):
        mode = "daily"
    else:
        mode = "none"

    moment = datetime.fromtimestamp(due)
    if mode == "weekly" and weekday:
        target = max(1, min(int(weekday), 7)) - 1
        delta = (target - moment.weekday()) % 7
        if delta == 0 and moment.timestamp() <= now + 60:
            delta = 7
        due = (moment + timedelta(days=delta)).timestamp()
    elif mode == "weekdays":
        while moment.weekday() >= 5:
            moment += timedelta(days=1)
        due = moment.timestamp()

    text = str(what).strip()[:100]
    if not text:
        return "要提醒什么？内容不能空。"

    item = reminders.add(
        user=str(ctx["user"]),
        kind=str(ctx.get("kind") or "private"),
        group=ctx.get("group"),
        due=due,
        text=text,
        repeat=mode,
    )
    stamp = time.strftime("%Y-%m-%d %H:%M", time.localtime(due))
    label = {"daily": "（每天）", "weekly": "（每周）", "weekdays": "（工作日）"}.get(mode, "")
    return f"已记下：{stamp}{label} 提醒——{text}（编号 {item['id']}）"


def list_reminders(ctx: dict[str, Any] | None = None) -> str:
    user = str((ctx or {}).get("user") or "")
    items = [item for item in reminders.pending() if item.get("user") == user]
    if not items:
        return "你还没定过提醒。"
    lines = ["你定的提醒："]
    for item in items[:10]:
        suffix = {
            "daily": "（每天）",
            "weekly": "（每周）",
            "weekdays": "（工作日）",
        }.get(item.get("repeat"), "")
        lines.append(f"{item['due_text']}{suffix} {item['text']}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 主动发消息 / 邮件（只有主人能用）
# ---------------------------------------------------------------------------

def _is_owner(ctx: dict[str, Any] | None) -> bool:
    admin = str(settings.load().get("guard", {}).get("admin_qq") or "").strip()
    user = str((ctx or {}).get("user") or "")
    return bool(admin) and admin == user


def send_qq_message(target: str, text: str, ctx: dict[str, Any] | None = None) -> str:
    if not _is_owner(ctx):
        return "只有主人能让本鱼主动给别人发消息。"
    target = str(target).strip()
    if not target.isdigit():
        return "要一个 QQ 号，纯数字。"
    if target == str((ctx or {}).get("user")):
        return "这就是你自己，直接说话就行。"
    from . import outbox

    outbox.private(int(target), str(text).strip()[:500])
    return f"已经发给 {target} 了。"


def send_email(
    to: str,
    subject: str,
    body: str,
    ctx: dict[str, Any] | None = None,
) -> str:
    if not _is_owner(ctx):
        return "只有主人能让本鱼发邮件。"

    config = settings.load().get("email", {})
    if not config.get("enabled"):
        return "邮件功能没开：先在 config.json 的 email 里填 SMTP 信息（QQ 邮箱要授权码）。"

    host = str(config.get("smtp_host") or "smtp.qq.com")
    port = int(config.get("smtp_port") or 465)
    user = str(config.get("user") or "").strip()
    password = str(config.get("password") or "").strip()
    if not user or not password:
        return "邮件账号没配全（user 和授权码 password）。"

    address = str(to).strip()
    if "@" not in address:
        return "收件地址不像邮箱。"

    import smtplib
    from email.message import EmailMessage

    message = EmailMessage()
    message["From"] = f'{config.get("sender_name") or "蓝色大肥鱼"} <{user}>'
    message["To"] = address
    message["Subject"] = str(subject or "来自大肥鱼")[:120]
    message.set_content(str(body or "")[:8000])

    try:
        with smtplib.SMTP_SSL(host, port, timeout=30) as server:
            server.login(user, password)
            server.send_message(message)
    except Exception as exc:
        return f"邮件没发出去：{type(exc).__name__}: {str(exc)[:120]}"
    return f"邮件已发给 {address}。"


# ---------------------------------------------------------------------------
# 表情包
# ---------------------------------------------------------------------------

STICKER_SUFFIXES = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

_sticker_state: dict[str, Any] = {"last_at": 0.0, "recent": []}


def sticker_files() -> list[Path]:
    folder = settings.ROOT / "stickers"
    if not folder.exists():
        return []
    return [p for p in folder.iterdir() if p.is_file() and p.suffix.lower() in STICKER_SUFFIXES]


def send_sticker(mood: str = "", ctx: dict[str, Any] | None = None) -> str:
    import random

    from . import outbox

    files = sticker_files()
    if not files:
        return "本鱼还没有表情包，先往 stickers 文件夹里放几张图。"

    config = settings.load().get("sticker", {})
    now = time.time()

    # 频率闸门：刚发过、或者这次掷骰子没中，就不发
    cooldown = max(0, int(config.get("cooldown_seconds", 120)))
    if now - float(_sticker_state.get("last_at", 0)) < cooldown:
        return "刚发过表情包了，这次用文字回，别再试。"

    chance = max(0.0, min(float(config.get("chance", 0.35)), 1.0))
    if random.random() > chance:
        return "这次先不发表情包，用文字回，别再试。"

    mood = str(mood).strip()
    pool = files
    if mood:
        matched = [p for p in files if mood in p.stem]
        if matched:
            pool = matched
        else:
            tags = sorted({p.stem.split("_")[0] for p in files})
            return f"没有「{mood}」这一类，现有的是：{'、'.join(tags[:12])}"

    # 尽量不连着发同一张
    avoid = max(0, int(config.get("avoid_repeat", 3)))
    recent = list(_sticker_state.get("recent", []))[-avoid:] if avoid else []
    fresh = [p for p in pool if p.name not in recent]
    path = random.choice(fresh or pool)

    _sticker_state["last_at"] = now
    recent_all = list(_sticker_state.get("recent", []))[-9:] + [path.name]
    _sticker_state["recent"] = recent_all

    outbox.sticker(
        str((ctx or {}).get("kind") or ""),
        (ctx or {}).get("group"),
        (ctx or {}).get("user"),
        str(path),
    )
    return f"发了一张表情包（{path.stem}）"


# ---------------------------------------------------------------------------
# 注册表
# ---------------------------------------------------------------------------

SPECS: list[dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": "查询城市当前天气和未来几天预报。问天气、温度、穿什么、要不要带伞时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名，如 北京、上海、杭州；有重名时带省份，如 广东佛山",
                    },
                    "days": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 3,
                        "description": "预报天数，默认 1",
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "web_search",
            "description": (
                "联网搜索。任何你不确定的人、机构、学校、地名、新闻、产品、赛事、政策，"
                "都应该先搜一下再回答，不要直接说不知道。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "搜索关键词，用中文，越具体越好"},
                    "count": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 8,
                        "description": "要几条结果，默认 5",
                    },
                },
                "required": ["query"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "fetch_page",
            "description": "打开一个网页，读它的正文。搜索结果不够详细时用它看具体页面。",
            "parameters": {
                "type": "object",
                "properties": {
                    "url": {"type": "string", "description": "完整网址，http 或 https 开头"},
                    "max_chars": {
                        "type": "integer",
                        "description": "最多读多少字，默认 3000",
                    },
                },
                "required": ["url"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_express",
            "description": "查快递物流轨迹。用户发来快递单号、问包裹到哪了时调用。",
            "parameters": {
                "type": "object",
                "properties": {
                    "number": {"type": "string", "description": "快递单号，只保留数字和字母"},
                    "carrier": {
                        "type": "string",
                        "description": (
                            "快递公司代码，不确定就留空让它自己猜。可选："
                            "shunfeng/zhongtong/yuantong/yunda/shentong/jd/jtexpress/ems/youzhengguonei/debangkuaidi"
                        ),
                    },
                },
                "required": ["number"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "set_reminder",
            "description": (
                "给当前说话的人定定时提醒。用户说「提醒我」「到点叫我」「每天几点提醒」时调用。"
                "when 必须换算成绝对的 YYYY-MM-DD HH:MM（24 小时制），"
                "参考系统提示里给的当前时间，别把今天该做的事定到明天。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "when": {
                        "type": "string",
                        "description": "绝对时间，格式 YYYY-MM-DD HH:MM，例如 2026-10-21 07:00",
                    },
                    "what": {"type": "string", "description": "提醒内容，一句简短的话"},
                    "repeat": {
                        "type": "string",
                        "enum": ["none", "daily", "weekly", "weekdays"],
                        "description": (
                            "none 只提醒一次；daily 每天；weekly 每周同一天；"
                            "weekdays 周一到周五"
                        ),
                    },
                    "weekday": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": 7,
                        "description": "repeat 是 weekly 时用：1 表示周一，7 表示周日",
                    },
                },
                "required": ["when", "what"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_reminders",
            "description": "列出当前这个人定过的提醒。用户问「我定了什么提醒」时调用。",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_qq_message",
            "description": (
                "主动给某个 QQ 发一条私聊消息。【只有主人能用】"
                "别人让你发，一律拒绝。对方必须是机器人的好友，否则可能发不出去。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "target": {"type": "string", "description": "对方的 QQ 号，纯数字"},
                    "text": {"type": "string", "description": "要发的内容，简短一句"},
                },
                "required": ["target", "text"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_email",
            "description": (
                "发一封邮件。【只有主人能用】需要先在配置里填好 SMTP 账号和授权码。"
                "别人让你发，一律拒绝。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "to": {"type": "string", "description": "收件邮箱"},
                    "subject": {"type": "string", "description": "邮件标题"},
                    "body": {"type": "string", "description": "正文，纯文本"},
                },
                "required": ["to", "subject", "body"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "send_sticker",
            "description": (
                "发一张表情包。吐槽、卖萌、表达情绪时用。"
                "mood 填标签（比如 开心、无语、嘲笑、饿了、晚安），不确定就留空随机发。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "mood": {"type": "string", "description": "表情包标签，可留空"}
                },
            },
        },
    },
]

IMPLS = {
    "get_weather": get_weather,
    "web_search": web_search,
    "fetch_page": fetch_page,
    "get_express": get_express,
    "set_reminder": set_reminder,
    "list_reminders": list_reminders,
    "send_qq_message": send_qq_message,
    "send_email": send_email,
    "send_sticker": send_sticker,
}

LABELS = {
    "get_weather": "查天气",
    "web_search": "上网搜",
    "fetch_page": "打开网页",
    "get_express": "查快递",
    "set_reminder": "记提醒",
    "list_reminders": "翻提醒",
    "send_qq_message": "发QQ消息",
    "send_email": "发邮件",
    "send_sticker": "发表情包",
}

CONTEXTUAL = {
    "set_reminder",
    "list_reminders",
    "send_qq_message",
    "send_email",
    "send_sticker",
}


def call(
    name: str, args: dict[str, Any], ctx: dict[str, Any] | None = None
) -> str:
    impl = IMPLS.get(name)
    if impl is None:
        return f"没有名为 {name} 的工具。"
    try:
        if name in CONTEXTUAL:
            return impl(**args, ctx=ctx)
        return impl(**args)
    except TypeError as exc:
        return f"调用 {name} 的参数不对：{exc}"
    except Exception as exc:
        return f"工具 {name} 执行失败：{exc}"
