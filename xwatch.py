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

import requests

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")
STATE_FILE = os.path.join(BASE_DIR, "state.json")

API_BASE = os.environ.get("XWATCH_API", "https://api.fxtwitter.com")

NTFY_URL = os.environ.get("NTFY_URL", "http://112.74.95.74:8858").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "xtweet-9f4bq2wp")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "").strip()

TRANSLATE = os.environ.get("XWATCH_TRANSLATE", "true").lower() in ("1", "true", "yes", "on")
MAX_PER_USER = int(os.environ.get("XWATCH_MAX_PER_USER", "3"))
MAX_AGE_HOURS = int(os.environ.get("XWATCH_MAX_AGE_HOURS", "18"))
MAX_CHARS = int(os.environ.get("XWATCH_MAX_CHARS", "1200"))
DRY_RUN = os.environ.get("XWATCH_DRY_RUN", "false").lower() in ("1", "true", "yes", "on")
TEST_MODE = os.environ.get("XWATCH_TEST", "false").lower() in ("1", "true", "yes", "on")

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
    """读取 watchlist.txt；每行一个账号，支持 @handle 与行尾 # 注释。"""
    accounts = []
    if not os.path.exists(WATCHLIST_FILE):
        log("watchlist.txt 不存在，退化为默认名单")
        return ["DeItaone", "FirstSquawk", "elonmusk"]
    with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.split("#", 1)[0].strip()
            if not line:
                continue
            line = line.lstrip("@")
            # 允许 “handle, 备注” 这种写法
            line = re.split(r"[\s,，]+", line)[0].strip()
            if line and line not in accounts:
                accounts.append(line)
    return accounts


# -------------------------------------------------------------------- 抓取 --

def fetch_statuses(session, handle, count=20):
    """取某账号最近若干条推文；抓取失败返回 None（与“空列表”区分开）。"""
    url = f"{API_BASE}/2/profile/{handle}/statuses"
    for attempt in range(3):
        try:
            r = session.get(url, params={"count": count}, timeout=20)
            if r.status_code == 404:
                log(f"@{handle}: 账号不存在或接口 404")
                return None
            r.raise_for_status()
            data = r.json()
            results = data.get("results") or []
            return [x for x in results if isinstance(x, dict) and x.get("id")]
        except Exception as e:
            log(f"@{handle}: 抓取失败({attempt + 1}/3) {e}")
            time.sleep(2 * (attempt + 1))
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


# -------------------------------------------------------------------- 推送 --

def publish(session, title, message, click=""):
    payload = {"topic": NTFY_TOPIC, "title": title, "message": message}
    if click:
        payload["click"] = click
    headers = {"Content-Type": "application/json; charset=utf-8"}
    if NTFY_TOKEN:
        headers["Authorization"] = f"Bearer {NTFY_TOKEN}"
    for attempt in range(3):
        try:
            r = session.post(
                f"{NTFY_URL}/",
                data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                headers=headers,
                timeout=20,
            )
            if r.status_code == 200:
                return True
            log(f"推送失败 HTTP {r.status_code}: {r.text[:200]}")
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

    if TEST_MODE:
        ok = publish(
            session,
            "xwatch 自检",
            "链路正常：Actions（境外抓取）→ ntfy（国内）已打通。",
            "https://github.com/dzp181/x2ding/actions",
        )
        log("测试消息发送" + ("成功" if ok else "失败"))
        return 0 if ok else 1

    accounts = load_watchlist()
    log(f"监控 {len(accounts)} 个账号: {', '.join(accounts)}")

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

    for handle in accounts:
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
            if publish(session, title, body, click):
                total_pushed += 1
                log(f"@{handle}: 已推送 {status.get('id')}")
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
