# xwatch —— X 推文 → ntfy 订阅

在 `lixiansky/x2ding` 的基础上改造：**抓取放到境外（GitHub Actions），推送经国内自建 ntfy 落地**，
不依赖本机常驻、不依赖 Nitter 实例、不经过被墙节点。

```
国内服务器 crontab（每 10 分钟）── workflow_dispatch ──▶ GitHub Actions (ubuntu-latest, 境外)
                                                              └─ api.fxtwitter.com 直连抓推文 JSON
                                                                   └─ POST http://112.74.95.74:8858（Bearer token）
                                                                        └─ 国内 ntfy → ntfy.sh 上游 → APNs → iPhone
```

定时为什么不用 GitHub 自带的 `schedule`：fork 仓库的定时任务经常不触发、或者延迟十几分钟。
改成国内服务器的 crontab 调 `workflow_dispatch`（`/home/zhangsan/bin/xwatch-trigger.sh`，`*/10 * * * *`），
准时且确定；抓取和推送仍然全在 Actions 里跑，服务器只负责“敲一下”。

## 订阅

| 项 | 值 |
| --- | --- |
| 订阅地址 | `http://112.74.95.74:8858/xtweet-9f4bq2wp` |
| 主题 | `xtweet-9f4bq2wp` |
| 发布令牌（只对本主题有写权限） | `tk_k2bmpf9quio90fvdfovbhn5zmqsrm` |

iOS ntfy App：`Settings → Subscribe to topic`，URL 填上面的完整地址即可。

## 文件

| 文件 | 作用 |
| --- | --- |
| `watchlist.txt` | 监控名单，`账号 [主题]`；写主题=这个人单独一个主题，不写=进默认主题 |
| `xwatch.py` | 抓取 + 组包 + 推送，纯 requests，无浏览器依赖 |
| `state.json` | 每个账号最后推送的推文 ID，workflow 每轮自动提交回仓库 |
| `.github/workflows/xwatch.yml` | 定时（10 分钟）+ 手动触发 |

## 主题

默认主题 `xtweet-9f4bq2wp` 收“没指定主题”的账号；目前所有账号都走独立主题，默认主题已停用。
指定了主题的账号，推文只进自己的主题：

| 账号 | 主题 | 订阅地址 |
| --- | --- | --- |
| `elonmusk` | `x-elonmusk` | `http://112.74.95.74:8858/x-elonmusk` |
| `xy_xtony`（逍遥XTony） | `x-xtony` | `http://112.74.95.74:8858/x-xtony` |
| `aleabitoreddit`（Serenity） | `x-serenity` | `http://112.74.95.74:8858/x-serenity` |

`DeItaone` / `FirstSquawk`（原 `xtweet-9f4bq2wp` 混合主题）已于 2026-11-05 停推，名单里保留注释行便于恢复。

新加一个人单独主题：在 `watchlist.txt` 里写 `<账号> <主题>`，主题名以 `x-` 开头（写入令牌已授权 `x-*`），
提交后下一轮生效；第一次见到这个账号会自动推一条最新的做种子。


## 网页配置

`https://x-watch-config.app.workbuddy.host/?sharecode=23YTOGQN6L`

单页应用，带账号体系：邮箱注册（收验证码）→ 设置密码 → 以后用密码登录，也支持找回密码。
登录后才能看到配置页。账号数据存在 WorkBuddy 云服务，与本仓库无关。

登录后：

* **只读浏览**（默认）：能看到名单、主题、订阅地址，不需要任何 GitHub 凭据——本仓库是公开的，
  页面用匿名 GitHub API 读取。
* **编辑**：需要额外粘贴一个 Fine-grained token（只授权本仓库，`Contents: Read and write`；
  要「立即运行一轮」再加 `Actions: Read and write`），令牌只存在这台浏览器的 localStorage，
  不经过任何服务器，也不会写进云端。

所以登录是「进站门禁」，写权限仍然由 GitHub 令牌控制。改动提交后下一轮 cron 自动生效。

文件格式向后兼容：`#! handle topic` 表示停用（`xwatch.py` 把它当注释跳过）。

## 消息格式

一条推文 = 一条通知：标题是「作者 (@handle)」，正文是推文全文（英文另附中文翻译），
末尾一行 CST 时间，推文链接放在 `click` 字段里，正文不出现网址（避免 iOS 折行乱版）。

## 图片

推文带图（或视频）时，取第一张图（视频取封面）放进 `attach` 字段，iOS 通知里会显示预览。

图片地址不能直接用 `pbs.twimg.com`——国内打不开。走 `wsrv.nl` 图床代理：

```
https://wsrv.nl/?url=https%3A%2F%2Fpbs.twimg.com%2Fmedia%2Fxxx.jpg&w=1200&output=jpg&n=-1
```

**`url` 参数必须整条 percent-encode（连 `https://` 一起编码）。** 不编码的话 wsrv 去抓 twimg 会拿到
520，它自己回 404，手机端就是“图片下载失败”。这是踩过的坑：不编码时 `media/` 的普通照片全挂，
而 `amplify_video_thumb` 的视频封面却能出图，很容易误判成“偶发网络问题”。

服务端不会替我们校验附件（ntfy 只把 URL 透传给客户端，手机自己去下），所以**发送前会自己先 GET 一遍**：
状态 200 + `Content-Type: image/*` + 文件头是 JPEG/PNG/GIF/WEBP 才算通过，不通过就换一次缓存键重试，
仍失败则这条只发文字——宁可没图，也不发一张点开是 404 的图。

ImgBB 走不通（`i.ibb.co` 在服务器上 SSL 握手失败），所以没有接 ImgBB。
强制看一张图：`gh workflow run xwatch.yml -f test_image=true`。

## 可调参数

仓库 `Settings → Variables`（也可以直接写进 workflow 的 `env`）：

| 变量 | 默认 | 说明 |
| --- | --- | --- |
| `NTFY_URL` | `http://112.74.95.74:8858` | ntfy 服务地址 |
| `NTFY_TOPIC` | `xtweet-9f4bq2wp` | 主题 |
| `XWATCH_TRANSLATE` | `true` | 英文推文是否附中文翻译 |
| `XWATCH_MAX_PER_USER` | `3` | 单账号单轮最多推几条 |
| `XWATCH_MAX_AGE_HOURS` | `18` | 超过这个时长的旧推文跳过 |

Secret：`NTFY_TOKEN`（即上表的发布令牌）。

## 手动操作

```bash
# 只发自检消息（验链路）
gh workflow run xwatch.yml -R dzp181/x2ding -f test=true

# 只抓取打印，不推送
gh workflow run xwatch.yml -R dzp181/x2ding -f dry_run=true

# 看运行记录
gh run list -R dzp181/x2ding
```

## 已知边界

* 触发节奏由服务器 crontab 控制（每 10 分钟）；GitHub 侧排队通常 5–20 秒起跑；
* 服务器上的 `~/bin/xwatch-trigger.sh` 需要 `~/.x2ding.gh-token`（600），令牌失效就换一个：
  `gh auth token` 取新值，覆盖写入该文件即可；
* 国内直连打不开 `x.com`，通知里的 `click` 需要梯子；正文已经有全文，链接只是备用。
