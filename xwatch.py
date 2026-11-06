#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
xwatch —— 监控 X(Twitter) 账号的新推文，并推送到自建 ntfy。

链路设计（无网络死角）：
    GitHub Actions（境外 runner，直连 api.fxtwitter.com 抓取）
        -> 组装纯文本消息
        -> POST 到国内服务器上的 ntfy（112.74.95.74:8858）
        -> iPhone ntfy 客户端（走 ntfy.sh 上游转发 APNs 即时到达）

设计要点：
    * 不依赖 Playwright / Nitter（实例大面积失效），直接用 fxtwitter 公开 JSON 接口；
    * 一条推文 = 一条通知，正文是推文原文（+可选中文翻译），网址放在 click 字段里，
      不出现在正文里，避免 iOS 上折行成一堆乱码；
    * 状态存 state.json（每轮抓取后由 workflow 提交回仓库），只推“没见过的”推文；
    * 推送失败不写状态，下一轮自动重试，不丢消息。
"""

import html
import json
import os
import re
import sys
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

API_BASE = os.environ.get("XWATCH_API", "https://api.fxtwitter.com")

NTFY_URL = os.environ.get("NTFY_URL", "http://112.74.95.74:8858").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "xtweet-9f4bq2wp")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()

# 网页上「用户新增」的监控存在云表 xw_watch 里（普通用户没有 GitHub 写权限）。
# 匿名只读，且只放开 handle/topic 两列；读不到就当没有，绝不影响仓库名单的抓取。
CLOUD_WATCH_API = os.environ.get(
    "XWATCH_CLOUD_API",
    "https://x-watch-config.app.workbuddy.host/.cloud/database/rest",
).rstrip("/")
CLOUD_WATCH_KEY = os.environ.get(
    "XWATCH_CLOUD_KEY",
    "wbpk_p56SiaYcfk2CtvuBd3PEnm_eKfaZDWUQqUFWx4wGihkEQoW3FbSP0nQ",
).strip()

TRANSLATE = os.environ.get("XWATCH_TRANSLATE", "true").lower() in ("1", "true", "yes", "on")
MAX_PER_USER = int(os.environ.get("XWATCH_MAX_PER_USER", "3"))
MAX_AGE_HOURS = int(os.environ.get("XWATCH_MAX_AGE_HOURS", "18"))
MAX_CHARS = int(os.environ.get("XWATCH_MAX_CHARS", "1200"))
DRY_RUN = os.environ.get("XWATCH_DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
TEST_MODE = os.environ.get("XWATCH_TEST", "false").strip().lower()  # false / true / image

UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
)


def log(msg):
    print(f"[{datetime.now(timezone.utc).strftime('%H:%M:%S')}Z] {msg}", flush=True)


def sid(item):
    try:
        return int(item.get("id"))
    except (TypeError, ValueError):
        return 0


# ---------------------------------------------------------------- watchlist --

def load_watchlist():
    """
    读取 watchlist.txt。每行 `handle [topic]`：
        elonmusk   x-elonmusk      -> 只推到 x-elonmusk 这个主题（一个人一个主题）
        DeItaone                   -> 没写主题就走默认主题 NTFY_TOPIC（多人混在一起）
    # 开头或行尾是注释。
    """
    accounts = []
    seen = set()
    if not os.path.exists(WATCHLIST_FILE):
        log("watchlist.txt 不存在，退化为默认名单")
        return [(h, "") for h in ("DeItaone", "FirstSquawk", "elonmusk")]
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            parts = [p for p in re.split(r"[\s,，]+", line) if p]
            handle = parts[0].lstrip("@")
            topic = parts[1] if len(parts) > 1 else ""
            if handle and handle.lower() not in seen:
                seen.add(handle.lower())
                accounts.append((handle, topic))
    return accounts


def load_cloud_watch(session):
    """
    网页上「用户新增」的监控。存于云表 xw_watch，匿名只读 handle/topic。
    读失败一律返回空 —— 云端挂了也不能拖垮仓库名单的抓取。
    """
    if not CLOUD_WATCH_API or not CLOUD_WATCH_KEY:
        return []
    try:
        r = session.get(
            f"{CLOUD_WATCH_API}/xw_watch",
            params={"select": "handle,topic", "enabled": "is.true", "status": "eq.approved"},
            headers={"x-wb-webapp-access-key": CLOUD_WATCH_KEY},
            timeout=15,
        )
        if r.status_code != 200:
            log(f"云端新增名单跳过：HTTP {r.status_code}")
            return []
        rows = r.json() or []
    except Exception as e:
        log(f"云端新增名单跳过：{e}")
        return []
    out = []
    for x in rows:
        if not isinstance(x, dict):
            continue
        h = str(x.get("handle") or "").strip().lstrip("@")
        if h:
            out.append((h, str(x.get("topic") or "").strip()))
    return out


# -------------------------------------------------------------------- 抓取 --
def fetch_statuses(session, handle, count=20, attempts=5):
    """
    取某账号最近若干条推文；彻底失败返回 None（与“真的没有推文”区分开）。

    坑：fxtwitter 的 /2/profile 接口在缓存冷的时候会先返 404 + 空 results，
       隔几秒再请求就 200 了 —— 所以 404 当瞬时故障重试，不能直接放弃。
    """
    url = f"{API_BASE}/2/profile/{handle}/statuses"
    last_err = ""
    for attempt in range(attempts):
        try:
            r = session.get(url, params={"count": count}, timeout=20)
            if r.status_code == 200:
                data = r.json()
                results = [x for x in (data.get("results") or []) if isinstance(x, dict) and x.get("id")]
                if results:
                    return results
                last_err = "200 但结果为空"
            elif r.status_code == 404:
                last_err = "404（多为缓存冷，重试）"
            else:
                last_err = f"HTTP {r.status_code}"
                if r.status_code in (401, 403):
                    break
        except Exception as e:
            last_err = str(e)
        if attempt < attempts - 1:
            time.sleep(2 * (attempt + 1))
    log(f"@{handle}: 抓取失败 — {last_err}")
    return None


# -------------------------------------------------------------------- 文本 --

TCO_RE = re.compile(r"https?://t\.co/\S+")
MULTI_NL_RE = re.compile(r"\n{3,}")


def clean_text(status):
    text = status.get("text") or ""
    text = TCO_RE.sub("", text)
    text = html.unescape(text)
    text = MULTI_NL_RE.sub("\n\n", text).strip()
    if not text and (status.get("media") or status.get("embed_card")):
        text = "（这条推文只有图片/视频，没有文字）"
    if len(text) > MAX_CHARS:
        text = text[:MAX_CHARS].rstrip() + "…"
    return text


def mostly_ascii(text):
    if len(text) < 24:
        return False
    letters = [c for c in text if not c.isspace()]
    if not letters:
        return False
    return sum(1 for c in letters if ord(c) < 128) / len(letters) > 0.85


def translate(session, text):
    """走 Google 免费 gtx 接口；失败返回空串，不影响主流程。"""
    try:
        r = session.get(
            "https://translate.googleapis.com/translate_a/single",
            params={"client": "gtx", "sl": "auto", "tl": "zh-CN", "dt": "t", "q": text},
            timeout=15,
        )
        r.raise_for_status()
        data = r.json()
        parts = [seg[0] for seg in (data[0] or []) if seg and seg[0]]
        return "".join(parts).strip()
    except Exception as e:
        log(f"翻译失败（忽略）: {e}")
        return ""


def build_message(session, status):
    author = status.get("author") or {}
    handle = author.get("screen_name", "")
    name = author.get("name") or handle
    text = clean_text(status)

    body = text
    if TRANSLATE and mostly_ascii(text):
        zh = translate(session, text)
        if zh:
            body = f"{zh}\n\n原文：{text}"

    title = f"{name} (@{handle})"
    if status.get("reposted_by"):
        title = f"[转推] {title}"

    try:
        ts = int(status.get("created_timestamp") or 0)
    except (TypeError, ValueError):
        ts = 0
    if ts:
        cst = datetime.fromtimestamp(ts, timezone.utc) + timedelta(hours=8)
        body = f"{body}\n\n— {cst.strftime('%m-%d %H:%M')} CST"

    return title, body, status.get("url", "")


# -------------------------------------------------------------------- 图片 --

def pick_image(status):
    """取推文里第一张能当封面的图：优先真图，其次视频封面。"""
    media = (status.get("media") or {}).get("all") or []
    for item in media:
        if item.get("type") == "photo" and item.get("url"):
            return item["url"]
    for item in media:
        if item.get("thumbnail_url"):
            return item["thumbnail_url"]
    return ""


def proxied_image(url):
    """
    pbs.twimg.com 在国内直连打不开，走 wsrv.nl 图床代理。

    注意：wsrv 的 url 参数必须**整条编码**（带 https:// 且 percent-encode），
    否则它转去抓 twimg 时会拿到 520，自己返回 404 —— 图就挂了。
    """
    if not url:
        return ""
    if "pbs.twimg.com" not in url:
        return url
    base = url.split("?")[0]
    if not re.search(r"\.(jpg|jpeg|png|webp|gif)$", base, re.I):
        base += "?format=jpg&name=large"
    return f"https://wsrv.nl/?url={quote(base, safe='')}&w=1200&output=jpg&n=-1"


def image_ok(session, img_url):
    """发之前先确认这张图真能下下来：200 + image/* + 文件头是图片。"""
    try:
        r = session.get(img_url, timeout=20, stream=True)
        ct = (r.headers.get("content-type") or "").lower()
        head = r.raw.read(16)
        r.close()
        if r.status_code != 200 or not ct.startswith("image/"):
            return False
        return head[:3] == b"\xff\xd8\xff" or head[:4] == b"\x89PNG" or head[:4] == b"RIFF" or head[:6] in (b"GIF87a", b"GIF89a")
    except Exception as e:
        log(f"图片预检异常: {e}")
        return False


def ready_image(session, status, tries=2):
    """
    挑图 → 换代理地址 → 预检。
    宁可只发文字，也不能发一张点开是 404 的图（图片由手机侧的 wsrv 直连下载，
    服务端不会替我们校验，所以必须在发之前自己先下一遍）。
    """
    raw = pick_image(status)
    if not raw:
        return ""
    img = proxied_image(raw)
    for i in range(tries):
        if image_ok(session, img):
            return img
        if i + 1 < tries:
            img = f"{img}&ts={int(time.time())}"   # 换个缓存键再来一次
    log(f"图片预检失败，本条只发文字: {raw[:70]}")
    return ""


# -------------------------------------------------------------------- 推送 --

def publish(session, title, message, click="", attach="", topic=""):
    payload = {"topic": topic or NTFY_TOPIC, "title": title, "message": message}
    if click:
        payload["click"] = click
    if attach:
        payload["attach"] = attach
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    for attempt in range(3):
        try:
            r = session.post(
                f"{NTFY_URL}/",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=30,
            )
            if r.status_code == 200:
                return True
            log(f"推送失败 HTTP {r.status_code}: {r.text[:200]}")
            # 附件拉取失败时退回纯文字，别把整条消息丢掉
            if attach and r.status_code in (400, 413, 415, 500):
                log("带图推送失败，降级为纯文字重试")
                return publish(session, title, message, click, "", topic)
            if r.status_code in (401, 403):
                return False  # 鉴权问题，重试无意义
        except Exception as e:
            log(f"推送异常({attempt + 1}/3): {e}")
        time.sleep(2 * (attempt + 1))
    return False


# -------------------------------------------------------------------- 主流程 --

def main():
    if not NTFY_TOKEN and not DRY_RUN:
        log("缺少 NTFY_TOKEN，无法发布（仓库 Secrets 里配置 NTFY_TOKEN）")
        return 1

    session = requests.Session()
    session.headers.update({"User-Agent": UA})

    if TEST_MODE in ("true", "1", "yes", "on"):
        ok = publish(
            session,
            "xwatch 自检",
            "链路正常：Actions（境外抓取）→ ntfy（国内）已打通。",
            "https://github.com/dzp181/x2ding/actions",
        )
        log("测试消息发送" + ("成功" if ok else "失败"))
        return 0 if ok else 1

    if TEST_MODE == "image":
        # 从监控名单里挑一条“最新且带图”的推文，强制推一次（忽略 state），用来看图片效果
        watch = load_watchlist()
        names = [h for h, _ in watch]
        watch += [(h, "") for h in ("nvidia", "Tesla", "OpenAI") if h not in names]
        for handle, topic in watch:
            results = fetch_statuses(session, handle, 20)
            if not results:
                continue
            for status in sorted(results, key=sid, reverse=True)[:8]:
                img = ready_image(session, status)
                if not img:
                    continue
                title, body, click = build_message(session, status)
                log(f"图片样例来源 @{handle} {status.get('id')} -> {img[:80]}")
                ok = publish(session, title, body, click, img, topic)
                log("图片消息发送" + ("成功" if ok else "失败"))
                return 0 if ok else 1
        log("名单里最近 8 条都没图片，没发出去")
        return 1

    accounts = load_watchlist()
    # 网页上用户新增、且管理员已批准的，追加在后面；同账号以仓库那份为准
    seen = {h.lower() for h, _ in accounts}
    extra = [(h, t) for h, t in load_cloud_watch(session) if h.lower() not in seen]
    if extra:
        log(f"云端已批准 {len(extra)} 个：" + ", ".join(h for h, _ in extra))
    accounts += extra
    log("监控 " + ", ".join(f"{h}→{t or NTFY_TOPIC}" for h, t in accounts))

    state = {}
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                state = json.load(f) or {}
        except Exception as e:
            log(f"state.json 读取失败，按空状态处理: {e}")
            state = {}

    changed = False
    total_pushed = 0
    cutoff = datetime.now(timezone.utc).replace(tzinfo=None) - timedelta(hours=MAX_AGE_HOURS)

    for handle, topic in accounts:
        key = handle.lower()
        results = fetch_statuses(session, handle)
        if results is None:
            continue  # 抓取失败：保留旧状态，下一轮再来
        if not results:
            log(f"@{handle}: 没有可见推文")
            continue

        results.sort(key=sid, reverse=True)  # 新 -> 旧
        newest_id = sid(results[0])
        last_seen = int(state.get(key, 0) or 0)
        first_time = key not in state

        pending = [r for r in results if sid(r) > last_seen]
        # 首次见到该账号只推最新一条，其余记为已读，避免刷一屏历史
        pending = pending[:1] if first_time else pending[:MAX_PER_USER]

        if not pending:
            log(f"@{handle}: 无新推文（最新 {newest_id}）")
            continue

        ok_all = True
        for status in sorted(pending, key=sid):  # 时间顺序：旧 -> 新
            ts = int(status.get("created_timestamp") or 0)
            if ts and datetime.fromtimestamp(ts, timezone.utc).replace(tzinfo=None) < cutoff:
                continue
            title, body, click = build_message(session, status)
            if DRY_RUN:
                log(f"[DRY] {title}\n{body}\n")
                continue
            if publish(session, title, body, click, ready_image(session, status), topic):
                total_pushed += 1
                log(f"@{handle}: 已推送 {status.get('id')} → {topic or NTFY_TOPIC}")
                time.sleep(1)
            else:
                log(f"@{handle}: {status.get('id')} 推送失败，保留状态下轮重试")
                ok_all = False
                break

        if ok_all:
            state[key] = str(newest_id)
            changed = True

    if changed and not DRY_RUN:
        with open(STATE_FILE, "w", encoding="utf-8") as f:
            json.dump(state, f, indent=2, sort_keys=True, ensure_ascii=False)
            f.write("\n")
        log("state.json 已更新")

    log(f"本轮结束，推送 {total_pushed} 条")
    return 0


if __name__ == "__main__":
    sys.exit(main())
