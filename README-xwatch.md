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
| `watchlist.txt` | 监控名单，一行一个账号，`#` 注释，改完提交即生效 |
| `xwatch.py` | 抓取 + 组包 + 推送，纯 requests，无浏览器依赖 |
| `state.json` | 每个账号最后推送的推文 ID，workflow 每轮自动提交回仓库 |
| `.github/workflows/xwatch.yml` | 定时（10 分钟）+ 手动触发 |

## 消息格式

一条推文 = 一条通知：标题是「作者 (@handle)」，正文是推文全文（英文另附中文翻译），
末尾一行 CST 时间，推文链接放在 `click` 字段里，正文不出现网址（避免 iOS 折行乱版）。

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
