# watcher：自动回复

每个机器人一个 watcher：轻量轮询 + 有新 @ 才唤醒。两种接法：

## 有 SSH（你自己的 Muse）

1. 复制 `watch.sh.template`，填好三处占位符：机器人自己的 open_id
   （从 inbox 里实测）、SSH 主机、该槽位的 inbox 目录。
2. 挂成 hook：轮询间隔建议 10 秒（hook 最小 5 秒；不要用 cron，
   最小粒度 1 分钟）。
3. 唤醒指令用 `wake-prompt.md`，占位符换成实际值。
4. 先 dry-run：应为 silent（建基线不唤醒）；之后群里 @ 机器人，
   应唤醒、回复、验发一次通过。

脚本行为：首次运行建基线（已在 inbox 的消息记为已见，不唤醒）；
SSH 失败/输出异常记日志后 silent，不打扰；状态存 hook 自带的状态文件，
已见集合上限 1000。

## 无 SSH（朋友的 Muse）

watcher 轮询平台 API：`GET /api/v1/<bot>/inbox?since=<id>`，Bearer 令牌
从自家安全凭证取。唤醒规则与 `wake-prompt.md` 同款，只是把 SSH 读队列
换成 API 调用、把写 outbox 换成 `POST /api/v1/<bot>/outbox`。
详见 `docs/friend-brief-template.md`。

## 间隔建议

10 秒。轮询与 watcher 两边一起调，只改一边没用。再低没有意义：
回复慢的大头是唤醒后干活的时间。60 秒不是任何一边的平台限制。
