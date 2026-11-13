# relay：飞书轮询中转

单应用轮询中转：用飞书官方 OpenAPI 轮询群历史、发送回复。
只用 Python 标准库，零依赖。详见仓库根目录的教程。

## 配置

复制 `config.example.json` 为 `config/config.json`，填四项。
`chat_id` 不知道可先留占位符，凭据填好后跑 `--list-chats` 列群再填回。

**权限**：`config.json` 须 `600` 且属主为容器内运行用户（uid 1000），
否则容器 `PermissionError` 无法启动。改配置原地编辑，勿改属主。

## 运维

```sh
docker compose up -d                    # 启动
docker compose logs --tail 50 relay     # 日志（应有 poll ok）
cat state/health.json                   # token_ok 为 true 即连通
docker compose run --rm relay --self-test   # 容器内自测（无网络）
docker compose run --rm relay --once        # 只跑一轮即退出（调试用）
docker compose run --rm relay --list-chats  # 列出机器人所在的群
```

DRY_RUN 模式（不调飞书 API，outbox 文件直接移入 sent/）：

```sh
docker compose run --rm -e DRY_RUN=1 relay --once
```

## 队列契约

- `config/config.json`：配置（0600，属主 uid 1000），容器内只读挂载。
- `state/`：`watermark.json`（增量锚点）、`seen.json`（去重）、`health.json`。
- `queues/inbox/`：每条新群消息一个 JSON 文件，文件名即 message_id。
- `queues/outbox/`：待发回复；成功移入 `sent/`，永久失败移入 `failed/`。
- 回复 JSON：`{"text", "mentions?", "reply_to_message_id?"}`，原子写入。
