#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
把 X 监控当天的推文推送，从 ntfy 缓存归档并生成可导入 ima 的 Markdown。

产物：ima_export/<账号>/<YYYY-MM-DD>.md
  - 一人一个账号文件夹（文件夹在 ima 端手工建，本脚本只生成本地同名目录）
  - 每天一个文件，文件名用北京时间日期；条目按时间倒序、编号 01 / 02 / 03
  - 正文就是监控推送的内容（译文 + 原文），不含订阅地址 / topic / 链接等元信息

数据源：自建 ntfy 的 48h 消息缓存（一人一主题 x-<handle>）。
  - 不碰 xwatch.py / state.json / workflow，不改任何推送行为
  - 消息按 ntfy 的 message id 去重落 archive/x_tweets.jsonl，缓存过期也不丢
  - 只归档「启动日」及之后的消息（START_DATE 记录在 .sync.json，首次运行写入当天），
    严格不补历史

ima 只能经 WorkBuddy 的 ima 连接器写入：本脚本产出文件 → WorkBuddy 自动化上传。
零第三方依赖，只用标准库。

用法：
  python3 export_ima.py                       # 抓取 + 归档 + 生成当天/各账号 md
  python3 export_ima.py --sync-status         # stdout 只输出一行 JSON：待上传文件数组
  python3 export_ima.py --commit <fp>         # 上传成功后记录某个文件的指纹
"""

import argparse
import datetime as dt
import hashlib
import json
import os
import re
import subprocess
import sys
import urllib.parse
import urllib.request

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
ARCHIVE_JSONL = os.path.join(BASE_DIR, "archive", "x_tweets.jsonl")
OUT_DIR = os.path.join(BASE_DIR, "ima_export")
SYNC_STATE = os.path.join(OUT_DIR, ".sync.json")
PENDING = os.path.join(OUT_DIR, ".pending.json")

NTFY_URL = os.environ.get("NTFY_URL", "http://112.74.95.74:8858").rstrip("/")
DEFAULT_TOPIC = os.environ.get("NTFY_TOPIC", "xtweet-9f4bq2wp")
WATCHLIST_URL = "https://raw.githubusercontent.com/dzp181/x2ding/main/watchlist.txt"
WATCHLIST_FILE = os.path.join(BASE_DIR, "watchlist.txt")

CST = dt.timezone(dt.timedelta(hours=8))
UA = "Mozilla/5.0 (compatible; xwatch-ima-export/1.0)"


def log(msg):
    print(f"[{dt.datetime.now(CST).strftime('%H:%M:%S')}] {msg}", file=sys.stderr, flush=True)


def today():
    return dt.datetime.now(CST).strftime("%Y-%m-%d")


def read_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def http_get(url, timeout=20):
    # 沙箱里 Python socket 直连不通、urllib 走代理也超时，但 curl 能通（走本机代理），
    # 所以统一用 curl 子进程。ntfy 的 /json 默认是长轮询流，加 poll=1 让它发完即断。
    p = subprocess.run(
        ["curl", "-s", "--max-time", str(timeout), url],
        capture_output=True, text=True,
    )
    return p.stdout


# ------------------------------------------------------------ 账号 / 主题 --

def load_topics():
    """
    从远端最新 watchlist.txt 取 (handle, topic) 列表，本地文件兜底。
    返回 {topic: handle}：topic 用小写 handle 反推文件夹名。
    """
    text = ""
    try:
        text = http_get(WATCHLIST_URL + "?t=" + str(int(dt.datetime.now().timestamp())))
    except Exception as e:
        log(f"远端名单读取失败，用本地文件兜底: {e}")
        try:
            with open(WATCHLIST_FILE, encoding="utf-8") as f:
                text = f.read()
        except OSError:
            text = ""

    mapping = {}
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line or line.startswith("!") or line.startswith("#!"):
            continue
        parts = [p for p in re.split(r"[\s,，]+", line) if p]
        if not parts:
            continue
        handle = parts[0].lstrip("@")
        topic = parts[1] if len(parts) > 1 else DEFAULT_TOPIC
        mapping[topic.lower()] = handle
    return mapping


def folder_name(topic, handle):
    """ima 文件夹名 = 账号 handle（小写）。独立主题优先用主题名，默认主题用标题里的 handle。"""
    t = (topic or "").lower()
    if t and t != DEFAULT_TOPIC.lower() and t.startswith("x-"):
        return t[2:]
    return (handle or "").lower() or "other"


# ---------------------------------------------------------------- 归档 -----


def load_archive():
    items = {}
    try:
        with open(ARCHIVE_JSONL, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    it = json.loads(ln)
                    if it.get("mid"):
                        items[str(it["mid"])] = it
                except (ValueError, json.JSONDecodeError):
                    continue
    except OSError:
        pass
    return items


def parse_ts(ts):
    try:
        return int(ts)
    except (TypeError, ValueError):
        return 0


def cst_day(ts):
    return dt.datetime.fromtimestamp(ts, CST).strftime("%Y-%m-%d")


def clean_body(msg):
    """
    ntfy message 形如：译文\n\n原文：xxx\n\n— 11-18 13:26 CST
    去掉末尾的时间戳行，正文（译文+原文）原样保留。
    """
    text = (msg or "").rstrip()
    text = re.sub(r"\n*—\s*\d{2}-\d{2}\s+\d{2}:\d{2}\s+CST\s*$", "", text)
    return text.strip()


def display_name(title, handle):
    """从 title『Name (@handle)』里取显示名；[转推] 前缀去掉。"""
    s = (title or "").strip()
    s = re.sub(r"^\[转推\]\s*", "", s)
    m = re.match(r"^(.*?)\s*\(@[^)]+\)$", s)
    name = m.group(1).strip() if m else s
    return name or handle


def parse_message_time(msg, fallback_ts):
    """优先用正文末尾『— MM-DD HH:MM CST』，失败退回 ntfy 推送时间。"""
    m = re.search(r"—\s*(\d{2})-(\d{2})\s+(\d{2}:\d{2})\s+CST\s*$", (msg or "").rstrip())
    if m:
        return f"{m.group(1)}-{m.group(2)}", m.group(3)
    d = dt.datetime.fromtimestamp(fallback_ts, CST)
    return d.strftime("%m-%d"), d.strftime("%H:%M")


def collect(topics, archive, start_date):
    """并发拉各主题 ntfy 缓存，新消息按 mid 去重落归档。只留启动日及之后。"""
    from concurrent.futures import ThreadPoolExecutor

    # 从「启动日 0 点」起取（不是当天）：某轮漏跑时，次日能从 48h 缓存里补回来；
    # mid 去重保证多取不会重复。poll=1 让 ntfy 发完即断。
    try:
        d0 = dt.datetime.strptime(start_date, "%Y-%m-%d").replace(tzinfo=CST)
    except (TypeError, ValueError):
        d0 = dt.datetime.now(CST).replace(hour=0, minute=0, second=0, microsecond=0)
    since_ts = int(d0.timestamp())

    def fetch(topic):
        url = (f"{NTFY_URL}/{urllib.parse.quote(topic)}/json"
               f"?since={since_ts}&poll=1")
        try:
            return topic, http_get(url, timeout=15)
        except Exception as e:
            log(f"主题 {topic} 拉取失败（跳过）: {e}")
            return topic, ""

    results = {}
    with ThreadPoolExecutor(max_workers=6) as pool:
        for topic, raw in pool.map(fetch, topics):
            results[topic] = raw

    added = 0
    for topic, handle in topics.items():
        raw = results.get(topic) or ""
        for ln in raw.splitlines():
            if not ln.strip():
                continue
            try:
                m = json.loads(ln)
            except (ValueError, json.JSONDecodeError):
                continue
            if m.get("event") != "message":
                continue
            mid = str(m.get("id") or "")
            ts = parse_ts(m.get("time"))
            if not mid or not ts or mid in archive:
                continue
            day = cst_day(ts)
            if start_date and day < start_date:
                continue
            folder = folder_name(topic, handle)
            if not folder or folder == "other":
                continue
            title = m.get("title") or ""
            msg = m.get("message") or ""
            day_s, hm = parse_message_time(msg, ts)
            archive[mid] = {
                "mid": mid,
                "topic": topic,
                "folder": folder,
                "handle": handle,
                "name": display_name(title, handle),
                "ts": ts,
                "date": day,
                "day_s": day_s,
                "hm": hm,
                "text": clean_body(msg),
            }
            added += 1
    if added:
        os.makedirs(os.path.dirname(ARCHIVE_JSONL), exist_ok=True)
        tmp = ARCHIVE_JSONL + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            for mid in sorted(archive, key=lambda k: archive[k].get("ts", 0)):
                f.write(json.dumps(archive[mid], ensure_ascii=False) + "\n")
        os.replace(tmp, ARCHIVE_JSONL)
    log(f"归档新增 {added} 条，累计 {len(archive)} 条")
    return added


# ------------------------------------------------------------ 生成 md ------


def build_files(archive, committed):
    """按 (folder, date) 分组生成 md，返回需要上传的文件列表。"""
    groups = {}
    for it in archive.values():
        key = (it.get("folder") or "other", it.get("date") or "")
        if not key[1]:
            continue
        groups.setdefault(key, []).append(it)

    os.makedirs(OUT_DIR, exist_ok=True)
    out = []
    for (folder, day), items in groups.items():
        # 按推文自身时间倒序，使编号 01/02… 与展示时间严格一致（不是用推送时间）
        items.sort(
            key=lambda x: ((x.get("day_s") or "") + " " + (x.get("hm") or "")
                           + " " + x.get("mid", "")),
            reverse=True,
        )
        handle = items[0].get("handle") or folder

        lines = [f"# @{handle} · {day}", ""]
        for i, it in enumerate(items, 1):
            hm = it.get("hm") or ""
            day_s = it.get("day_s") or ""
            # 跨天推文（昨天发布、今天推送）带上月日，避免时:分看起来像排序错误
            stamp = f"{day_s} {hm}" if day_s and day_s.replace("-", "") != day[5:].replace("-", "") else hm
            lines.append(f"## {i:02d} · {stamp}")
            lines.append("")
            text = (it.get("text") or "").strip() or "（无正文）"
            lines.append(text)
            lines += ["", "---", ""]

        fp = hashlib.sha256(
            json.dumps(
                [[it.get("mid"), it.get("text"), it.get("hm")] for it in items],
                ensure_ascii=False,
            ).encode("utf-8")
        ).hexdigest()[:16]

        fdir = os.path.join(OUT_DIR, folder)
        os.makedirs(fdir, exist_ok=True)
        path = os.path.join(fdir, f"{day}.md")
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            f.write("\n".join(lines).rstrip() + "\n")
        os.replace(tmp, path)

        out.append({
            "folder": folder,
            "date": day,
            "file_name": f"{day}.md",
            "path": path,
            "file_size": os.path.getsize(path),
            "count": len(items),
            "fingerprint": fp,
            "changed": fp not in committed,
        })
    out.sort(key=lambda x: (x["date"], x["folder"]), reverse=True)
    return out


# ---------------------------------------------------------------- main -----


def main():
    ap = argparse.ArgumentParser(description="X 推文推送 → ima Markdown（当天起、按账号/日期）")
    ap.add_argument("--sync-status", action="store_true",
                    help="stdout 只输出一行 JSON：待上传文件数组")
    ap.add_argument("--commit", default=None, metavar="FINGERPRINT",
                    help="上传成功后记录某文件的指纹")
    args = ap.parse_args()

    state = read_json(SYNC_STATE)
    committed = state.get("committed") or {}

    if args.commit:
        pend = read_json(PENDING)
        info = pend.get(args.commit)
        if not info:
            print(f"[ima] 提交失败：找不到待同步指纹 {args.commit}", file=sys.stderr)
            return 1
        committed[args.commit] = info
        state["committed"] = committed
        save_json(SYNC_STATE, state)
        pend.pop(args.commit, None)
        save_json(PENDING, pend)
        log(f"已记录 fp={args.commit} {info.get('folder')}/{info.get('file_name')}")
        return 0

    # 启动日：首次运行写入当天，只归档当天及之后，不补历史
    start_date = state.get("start_date")
    if not start_date:
        start_date = today()
        state["start_date"] = start_date
        save_json(SYNC_STATE, state)
        log(f"首次运行，归档起始日设为 {start_date}（不补历史）")

    topics = load_topics()
    log(f"账号主题 {len(topics)} 个")
    archive = load_archive()
    collect(topics, archive, start_date)
    files = build_files(archive, committed)

    pending = read_json(PENDING)
    for f in files:
        if f["changed"]:
            pending[f["fingerprint"]] = {
                k: f[k] for k in ("folder", "date", "file_name", "path", "file_size", "count")
            }
    # 已提交的指纹从 pending 清掉
    for fp in list(pending):
        if fp in committed:
            pending.pop(fp, None)
    save_json(PENDING, pending)

    todo = [f for f in files if f["changed"]]
    if args.sync_status:
        print(json.dumps(todo, ensure_ascii=False))
    else:
        log(f"生成 {len(files)} 个文件，其中 {len(todo)} 个待上传")
        for f in todo:
            log(f"  {f['folder']}/{f['file_name']}  {f['count']} 条  {f['file_size']:,} 字节")
    return 0


if __name__ == "__main__":
    sys.exit(main())
