# platform/ui：管理后台 + 队列 API

单管理员后台：管理所有机器人（配置、令牌、历史授权），并向各家 Muse
提供队列 API。只用 Python 标准库，零依赖。

## 环境变量

| 变量 | 默认 | 说明 |
|---|---|---|
| `SLOTS_DIR` | `/slots` | 槽位根目录，`/slots/slotN/{config,state,queues}` |
| `DATA_DIR` | `/data` | 后台数据（0600）：登录、会话、机器人、令牌哈希 |
| `SETUP_TOKEN_DIR` | `/setup-token-dir` | 一次性设置链接文件目录 |
| `UI_PORT` | `8000` | 监听端口（compose 里只绑 127.0.0.1，公网走隧道） |

## 首次设置

容器首次启动生成一次性设置链接（2 小时有效），形如
`https://<你的域名>/setup/<token>`，用它设管理员密码。链接只发管理员本人。
恢复设置：删 `data/setup_token_hash.json` 后重启 UI 容器。

登录有 5 次失败锁定 15 分钟、CSRF、24 小时会话。**UI 重启后会话失效，
需重新登录。**

## 页面结构

- `/bots`：机器人总表（基础信息）+ 加机器人入口。
- `/bots/<slug>`：详情页（状态、配置、令牌、最近消息、删除）。
- `/bots/new`：加机器人。
- 旧的分页面路由 302 到详情页。

保存规则：占位符值会被拒绝；空字段保留已有值；新机器人首次保存前群
下拉为空，先手动填 `chat_id`。删除机器人会写回占位配置、吊销令牌、
把状态归档到槽位下的 `_archive/`。

## 队列 API

基址 `https://<你的域名>/api/v1`，鉴权 `Authorization: Bearer <令牌>`。
令牌只显示一次，只存 SHA-256 哈希。

```
GET  /api/v1/<bot>/inbox?since=<message_id>&limit=<1..200>
GET  /api/v1/<bot>/inbox/<message_id>
POST /api/v1/<bot>/outbox        # 32KB 上限，30 次/分钟，202 即受理
GET  /api/v1/<bot>/health
GET  /api/v1/<bot>/settings      # {"bot", "name", "history_allowed"}，无密钥
```

错令牌、错机器人、非法路径一律 404 `{"error": "not_found"}`。
API 响应与日志里绝不出现密钥。

## 本地冒烟测试

```sh
python3 tests/ui_smoke.py
```

在临时目录跑起应用（端口 18799），覆盖登录、建机器人、保存、令牌、
API 权限隔离、删除全流程，共 22 项。注意：会话 cookie 带 Secure，
测试里手动取 Set-Cookie；全新数据目录会自动预置两个示例机器人。
