#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
avatars.py —— 给监控名单里的 X 账号补头像，压成 80x80 PNG 存进仓库 avatars/。

严格遵守的几条约束（因为它跑在监控流程里）：
    * 只写 avatars/ 目录，绝不碰 watchlist.txt / state.json / last_id.json；
    * 任何异常都在内部吞掉，退出码永远是 0 —— 头像挂了也绝不影响抓取和推送；
    * 图片已经有了就永远跳过：全部就绪后每轮 0 次请求、0 秒；
    * 抓失败的记进 _index.json，满 3 次不再重试，避免每 10 分钟空跑；
    * 想全部重抓（比如谁换了头像）：XWATCH_REFRESH_AVATARS=1 python avatars.py

单独跑，不在 xwatch.py 里：这样监控主流程一行都不用改。
"""

import io
import json
import os
import re
import sys
import time
from datetime import datetime, timezone

import requests

try:                       # Pillow 只用来把各色头像统一成 80px JPEG
    from PIL import Image
except Exception:          # 没装就当没有这个功能，不影响任何东西
    Image = None

import xwatch

AVATAR_DIR = os.path.join(xwatch.BASE_DIR, "avatars")
INDEX_FILE = os.path.join(AVATAR_DIR, "_index.json")

SIZE = 80
MAX_TRIES = 3
BUDGET = 60                # 整段最多占 60 秒，超了就下轮再说
TIMEOUT = 6
REFRESH = os.environ.get("XWATCH_REFRESH_AVATARS", "false").lower() in ("1", "true", "yes", "on")


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def safe_name(handle):
    """文件名只留 X 合法字符，避免云端条目里混进奇怪符号"""
    return re.sub(r"[^a-z0-9_]", "", (handle or "").lower())


def load_index():
    try:
        with open(INDEX_FILE, "r", encoding="utf-8") as f:
            return json.load(f) or {}
    except Exception:
        return {}


def save_index(index):
    os.makedirs(AVATAR_DIR, exist_ok=True)
    tmp = INDEX_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(index, f, indent=2, sort_keys=True, ensure_ascii=False)
        f.write("\n")
    os.replace(tmp, INDEX_FILE)


def avatar_url(session, handle):
    """fxtwitter 的 user 接口，顺便把 48px 的 _normal 换成 200px"""
    r = session.get(f"{xwatch.API_BASE}/{handle}", timeout=TIMEOUT)
    if r.status_code != 200:
        return ""
    try:
        user = (r.json() or {}).get("user") or {}
    except Exception:
        return ""
    url = user.get("avatar_url") or ""
    return re.sub(r"_(normal|bigger|mini)(\.\w+)$", r"_200x200\2", url)


def download(session, url):
    if not url:
        return None
    r = session.get(url, timeout=TIMEOUT, headers={"Referer": "https://x.com/"})
    if r.status_code != 200 or not r.content:
        return None
    if not r.headers.get("Content-Type", "").startswith("image/"):
        return None
    return r.content


def normalize(raw):
    """统一成 80x80 的 JPEG（居中裁方）——照片用 PNG 太占仓库，JPEG 只有 1/4 大"""
    if Image is None or not raw:
        return None
    try:
        im = Image.open(io.BytesIO(raw))
        im.seek(0)
        w, h = im.size
        side = min(w, h)
        im = im.crop(((w - side) // 2, (h - side) // 2, (w + side) // 2, (h + side) // 2))
        im = im.convert("RGBA").resize((SIZE, SIZE), Image.LANCZOS)
        flat = Image.new("RGB", (SIZE, SIZE), (255, 255, 255))   # 透明头像垫白底
        flat.paste(im, mask=im.split()[3])
        buf = io.BytesIO()
        flat.save(buf, format="JPEG", quality=88, optimize=True, progressive=True)
        return buf.getvalue()
    except Exception:
        return None


def pick_accounts(session):
    accounts = xwatch.load_watchlist() or []
    try:
        seen = {h.lower() for h, _ in accounts}
        extra = [(h, t) for h, t in (xwatch.load_cloud_watch(session) or []) if h.lower() not in seen]
        accounts += extra
    except Exception as e:
        xwatch.log(f"头像：云端名单读不到，只处理仓库名单（{e}）")
    return accounts


def main():
    try:
        if xwatch.DRY_RUN or xwatch.TEST_MODE not in ("false", "0", "", "no", "off"):
            xwatch.log("头像：dry-run / 测试模式，跳过")
            return 0
        if Image is None:
            xwatch.log("头像：环境里没有 Pillow，跳过（不影响抓取推送）")
            return 0

        session = requests.Session()
        session.headers.update({"User-Agent": xwatch.UA})

        accounts = pick_accounts(session)
        os.makedirs(AVATAR_DIR, exist_ok=True)
        index = load_index()
        started = time.time()
        tried = added = 0

        for handle, _topic in accounts:
            key = safe_name(handle)
            if not key:
                continue
            path = os.path.join(AVATAR_DIR, key + ".jpg")
            rec = index.get(key) or {}

            if os.path.exists(path) and not REFRESH:
                continue                                    # 已经有图：永远不再抓
            if not REFRESH and int(rec.get("tries") or 0) >= MAX_TRIES:
                continue                                    # 抓 3 次都没成，放弃
            if time.time() - started > BUDGET:
                xwatch.log("头像：本轮预算用完，剩下的下轮再说")
                break

            tried += 1
            png = None
            try:
                png = normalize(download(session, avatar_url(session, handle)))
            except Exception as e:
                xwatch.log(f"头像：@{handle} 抓取异常（{e}）")

            if png:
                try:
                    with open(path, "wb") as f:
                        f.write(png)
                    index[key] = {"ok": True, "at": now_iso(), "tries": 0}
                    added += 1
                    xwatch.log(f"头像：@{handle} 已存 {len(png) // 1024}KB")
                except Exception as e:
                    xwatch.log(f"头像：@{handle} 写文件失败（{e}）")
            else:
                tries = int(rec.get("tries") or 0) + 1
                index[key] = {"ok": False, "at": now_iso(), "tries": tries}
                xwatch.log(f"头像：@{handle} 没抓到（第 {tries}/{MAX_TRIES} 次）")
            time.sleep(0.8)                                 # 别把接口打太急（0.4 会被限流）

        if tried:
            save_index(index)
        have = len([k for k, v in index.items() if (v or {}).get("ok")])
        xwatch.log(f"头像：本轮尝试 {tried}，新增 {added}，已就绪 {have}/{len(accounts)}")
        return 0
    except Exception as e:
        xwatch.log(f"头像：出错但不影响监控（{e}）")
        return 0


if __name__ == "__main__":
    sys.exit(main())
