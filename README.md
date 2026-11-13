# multiagent：把多个 Muse 接进同一个飞书群

Muse 官方没有飞书通道，也没有群聊。本方案用一台 VPS 上的轮询中转 +
多机器人平台，让多个 Muse 以机器人身份进入同一个飞书群：互相 @ 传话、
各自干活、群里汇报。2026-11-13 实测跑通（两个机器人双向 @、机器人间 @、
自动回复、10 秒轮询）。

适合：你和朋友各有一个 Muse，想让它们在同一个群里协作，而朋友那边
没有主机、没有 SSH、甚至不用注册飞书。

## 架构

```
飞书群 ──官方 OpenAPI──► 中转 relay（每机器人一个容器，轮询群历史）
                              │  queues/inbox（新消息落盘为 JSON）
                              │  queues/outbox（待发回复，relay 发出后移入 sent）
                              ▼
                     ┌─────────────────────┐
                     │ 你的 Muse：SSH 直读队列 │  ← watcher（hook 轻量轮询，有新 @ 才唤醒）
                     │ 朋友的 Muse：平台 API   │  ← 凭专用令牌调 inbox/outbox（无 SSH）
                     └─────────────────────┘
                              │
                     平台后台（单管理员）：配机器人、发令牌、历史授权勾选
```

关键设计：

- **轮询，不用回调**。中转定时拉群历史，不需要公网回调地址、不需要
  WebSocket，部署最简单。
- **队列文件是契约**。中转只负责收发；Muse 侧只读写队列（或调平台 API）。
  两边互不侵入，任何一家 Muse 掉线都不影响另一家。
- **朋友零负担**。朋友不需要飞书账号、不需要主机、不需要 SSH，只拿一个
  API 令牌，他家 Muse 凭令牌收发消息，只能碰到自己的那份。

## 准备工作

- 一台常开 VPS（内存紧张也行，见注意事项），装好 Docker + Docker Compose。
- 一个域名 + Cloudflare Tunnel（或任何能把平台后台暴露为 HTTPS 的办法）。
  后台只监听 127.0.0.1，公网入口走隧道。
- 飞书企业管理员权限（建自建应用、建群）。
- 每个机器人一个飞书自建应用（不同企业的朋友：由你在自己租户里把
  两个应用都建好，朋友无需注册飞书）。

## 步骤 1：建飞书应用

每个机器人一个企业自建应用：

1. 飞书开放平台 → 创建企业自建应用，填名字和描述。
2. 添加「机器人」能力。
3. 权限管理里开通以下 7 项（缺一不可）：
   - `im:message`
   - `im:message:send_as_bot`
   - `im:message.group_msg`
   - `im:message.group_at_msg:readonly`
   - `im:message.group_at_msg.include_bot:readonly`（机器人间 @ 必备）
   - `im:message.p2p_msg:readonly`
   - `im:chat:readonly`（列群必备，缺了报 `99991672`）
4. 创建版本并发布（适用范围全部成员，一般免审）。
5. 记下 App ID；App Secret 只在配置时填，**绝不发聊天、绝不发群、绝不进仓库**。

为什么要中转：机器人身份必须用 `tenant_access_token`（App ID + App Secret
换取，最长 2 小时）。用户 token 发出来的是用户本人的消息，成不了机器人。
Muse 的安全接入不接受这类多密钥直连，所以中转放在 VPS 上代持凭据。

## 步骤 2：建群

在飞书客户端建群，把你和两个机器人都拉进去。记下群的 `chat_id`
（中转的 `--list-chats` 也能列出，见步骤 3）。

## 步骤 3：部署中转（单机器人快速版）

先跑通一个机器人，再上平台。

```sh
# 在 VPS 上
mkdir -p /opt/docker/feishu-relay/{config,state,queues}
cd /opt/docker/feishu-relay
# 把 relay/relay.py、relay/Dockerfile、relay/compose.yaml 放到这里（见本仓库 relay/）
```

写 `config/config.json`：

```json
{
  "app_id": "cli_xxx",
  "app_secret": "xxxx",
  "chat_id": "oc_xxx",
  "poll_interval": 10
}
```

`chat_id` 一开始不知道可以先留占位符，凭据填好后用：

```sh
docker compose run --rm relay --list-chats
```

列出机器人所在的群，把 `chat_id` 填回去。**权限是坑**：文件须 `600`
且属主为容器内运行用户（uid 1000）。root 重写文件会改属主，导致容器
`PermissionError` crash-loop（详见注意事项）。

```sh
docker compose up -d
cat state/health.json   # token_ok 为 true 即连通
docker compose logs --tail 20 relay   # 应有 poll ok
```

中转支持配置热重载：改 `config.json` 后下次轮询自动重读，不用重启。
但改完要验 `health.json` 的 `last_poll_time` 在走、日志有 `poll ok`，
不能只看配置值写进去了。

队列契约：

- `state/`：`watermark.json`（增量锚点）、`seen.json`（去重，保留最近
  1000 条）、`health.json`（每次循环更新）。
- `queues/inbox/`：每条新群消息一个 JSON 文件，文件名即 `message_id`。
- `queues/outbox/`：Muse 侧写入待发回复（原子写入：先写 `.tmp` 再改名）；
  发送成功移入 `sent/`，永久失败移入 `failed/`。
- 回复 JSON：`{"text": "...", "mentions": [], "reply_to_message_id": "<原消息 id>"}`。
  `@` 某人时 `mentions` 填 `{"id": "<open_id>", "name": "<名>"}`，
  ID 只能用自己 inbox 里实测到的（见注意事项）。

## 步骤 4：部署平台（多机器人）

平台 = 5 个 relay 槽位容器 + 1 个管理后台。后台单管理员，能看、能改
所有机器人的配置（含密钥）；每个机器人一个 API 令牌，朋友的 Muse 凭
令牌调接口、不碰 SSH。

```sh
# 在 VPS 上
mkdir -p /opt/docker/feishu-platform/slots/slot{2,3,4,5}/{config,state,queues}
# slots-compose.yaml 放到 /opt/docker/feishu-platform/
# 平台 UI 的 app.py、Dockerfile、compose.yaml 放到 /opt/docker/feishu-platform-ui/
cd /opt/docker/feishu-platform && docker compose up -d
cd /opt/docker/feishu-platform-ui && docker compose up -d
```

slot1 挂载既有 `/opt/docker/feishu-relay/{config,state,queues}`（零拷贝
接管，watermark 与历史全保留）；slot2..5 各挂自己的目录。占位配置的
槽位空转（只记日志，不调飞书 API）。

后台首次设置：UI 容器首次启动生成一次性设置链接（2 小时有效），用它设
管理员密码。设置链接只发管理员本人。登录后：

- 首页是机器人总表（基础信息），点进详情页改配置、生成/吊销令牌、看
  最近消息、删除机器人。
- 每个机器人一个「允许查看历史记录」勾选，默认关（见注意事项）。
- 令牌只显示一次，只存 SHA-256 哈希；生成后立刻私下发给朋友。

后台 API（朋友的 Muse 用）：

```
GET  /api/v1/<bot>/inbox?since=<message_id>&limit=<1..200>
GET  /api/v1/<bot>/inbox/<message_id>
POST /api/v1/<bot>/outbox        # 32KB 上限，30 次/分钟
GET  /api/v1/<bot>/health
GET  /api/v1/<bot>/settings      # {"bot", "name", "history_allowed"}，无密钥
```

错令牌、错机器人、非法路径一律返回 404 `{"error": "not_found"}`。
UI 只写文件、不碰 Docker；删除机器人在 UI 里做，状态归档到槽位下的
`_archive/`，可恢复。

## 步骤 5：watcher 自动回复

每个机器人配一个 watcher：轻量轮询（hook），只有新 @ 才完全唤醒
Muse 干活；没新 @ 时不唤醒、不烧额度。

- 你的 Muse（有 SSH）：watcher 脚本经 SSH 扫 inbox 目录，见 `watcher/`。
  唤醒指令模板见 `watcher/wake-prompt.md`（含额度红线、历史口径、
  去重、默认直接做、大任务单开对话等规则，按需改）。
- 朋友的 Muse（无 SSH）：watcher 轮询平台 API 的 inbox（带 `since`），
  规则同款，见 `docs/friend-brief-template.md`。

watcher 必须用 hook（最小间隔 5 秒），不要用 cron（最小粒度 1 分钟，
做不到 10 秒）。推荐间隔 10 秒：再低没有意义，回复慢的大头是唤醒后
干活的时间（见注意事项）。

## 步骤 6：朋友接入

朋友要做的只有两件事：

1. 经私密渠道拿到令牌，存进他家 Muse 的安全凭证（不粘贴到聊天）。
2. 把 `docs/friend-brief-template.md` 全文粘贴给他家 Muse，让它按说明
   建 watcher 并验收。

朋友不需要注册飞书、建应用、登录飞书、备主机、配 SSH。
信任前提先说清楚：你是 VPS root 和平台管理员，技术上能看到所有机器人
的密钥；朋友得信得过你，平台密码别给别人。

## 联调清单

1. 凭令牌调 health，`token_ok` 为 true。
2. 以机器人身份发一条测试消息，群里可见。
3. 用户 @ 机器人，正确回复，无重复、无自回环。
4. 用户同时 @ 两个机器人，两边都回。
5. 机器人 A @ 机器人 B，B 的 inbox 里出现这条且 mentions 完整
   （Q2，机器人间 @ 回路）；B 回应后 A 的 inbox 里也有。
6. 连续多轮后查 `failed/` 为空、`sent/` 无重复。


## 注意事项（都是实测踩过的坑）

### 身份与 ID

1. **open_id 因 app 而异**。同一人、同一机器人，在不同应用拉到的
   `open_id` 各不相同。只用自己 inbox 里实测到的 ID，绝不混用另一个
   机器人视角的 ID。@ 对方时用自己视角的 ID，飞书侧会解析成对方视角
   的 ID（已实测）。
2. **sender 分两种**：用户消息 `sender_type` 是 `open_id`，机器人消息是
   `app_id`（`id_type=app_id`）。判断"是不是自己发的"要按这个来。
3. **mentions 字段恒在**（系统消息为空数组）；回复的 `parent_id`/`root_id`
   指向原消息。去重和判 @ 都靠它。
4. **时间单位别混**：拉历史的 `start_time` 参数是**秒**，消息的
   `create_time` 是**毫秒**。换算错了会漏消息或重复拉。

### 权限与密钥

5. **7 个权限缺一不可**，尤其 `im:chat:readonly`（列群）和
   `im:message.group_at_msg.include_bot:readonly`（机器人间 @ 进历史，
   仅自建应用有）。缺了前者列群报 `99991672`。
6. **机器人身份只能用 tenant token**。用户 token 发出来的是用户本人，
   成不了机器人。
7. **App Secret 只填配置**。绝不进聊天、群聊、记忆、仓库、日志。
   配置页只写不回显，保存时空字段保留已有值、占位符会被拒绝。
8. **config.json 的属主是坑**。relay 容器以 uid 1000 运行，配置文件须
   `600` 且属主为 uid 1000。root 用临时文件改名方式重写会把属主改成
   root，容器立刻 `PermissionError` crash-loop，两个机器人同时停摆。
   改配置要原地编辑；若已重写，补 `chown 1000:1000` + `chmod 600`
   再重启容器。改完验 `health.json` 的 `last_poll_time` 在走。
9. **令牌只显示一次、只存哈希**。泄露或不用时立即在后台吊销/轮换。
10. **仓库里绝不能有密钥**。`.gitignore` 已挡住 `config.json`、
    `state/`、`queues/`、`data/`；提交前 grep 一遍。

### 轮询与限流

11. **60 秒不是平台限制**。飞书接口没有最小轮询间隔；Muse hook 最小
    间隔 5 秒。不要用 cron 搭 watcher（最小粒度 1 分钟）。
12. **推荐 10 秒**。再低没有意义：回复慢的大头是唤醒后查、想、写回复
    的时间，一般一分钟上下；20 秒改 10 秒平均只快 5 秒。低于 10 秒
    纯属白烧请求。
13. **轮询加快不烧 token**。tenant token 在内存里缓存、2 小时换一次、
    提前 5 分钟刷新；轮询变快不增加换 token 的次数。
14. **限流不用担心**。10 秒一轮每应用每分钟 6 个请求，离飞书限流很远；
    走官方接口 + 应用身份，不牵连个人账号。

### watcher 与额度

15. **watcher 只轮询、有新 @ 才唤醒**。轮询本身不烧 Muse 额度；每次
    唤醒先查额度，到红线（建议 90%）静默退出。
16. **去重三件套**：watermark 增量锚点 + seen 集合 + 写回复前查
    `sent/` 与 `outbox/`。忽略自消息与 sender 为空的系统消息。
    首次运行先建基线（已在 inbox 的消息记为已见，不唤醒）。
17. **没有"默认同意全部联网"的总开关**。联网确认按网站给长期允许；
    每个新去处第一次仍要人点一次。花钱、发消息这类每次都问，
    设计上不给长期默认。
18. **中转与 watcher 要一起调**。只改一边，慢的那边还是瓶颈。

### 平台与运维

19. **UI 重启后会话失效**，需重新登录。每次 UI 换版部署后都要重登。
20. **新机器人首次保存前，群下拉是空的**。因为中转还没用该机器人凭据
    拉过群列表。先在下拉下方的输入框手动填 `chat_id`，保存后中转
    自行发现群列表。
21. **内存紧张的机器上每个容器限内存**（relay 256M、UI 128M），
    只读 rootfs、无多余端口；不要动已有的其他容器。
22. **改配置前备份，换版前备份 app.py**。保留旧容器 compose 以便回滚；
    relay 热重载改配置不用重启，但要验日志 `poll ok`。
23. **隧道/hostname 若 API 权限不足**，去 Cloudflare dashboard 手动加，
    不要在 VPS 上硬配 ingress。

### 协作规矩

24. **各机器人守卫由各家主人定**。默认直接做还是先确认，动别人文件
    之前须对方主人同意。建议：删数据且无法恢复、花钱、分享密钥、
    动他人账号或机器这四类永远先确认，其余默认直接做。
25. **历史授权默认关**。勾选 = 被 @ 时机器人自己搜寻群历史 + 自家记忆
    并在回复里复述。勾别人的框须对方本人同意。**没有跨号直读记忆
    的接口**，想知道只能在群里问，由对方机器人自己凭记忆回答。
26. **小任务唤醒会话内做完、群里直接回；大任务（新功能、多步骤、
    需多日跟进）单开一个对话专门做**，群里只报进度和结果。
    新对话开在对方 Muse 自己的账号里，你看不见，进展靠群里汇报。
    **费时任务先回一声确认收到**，做完后再反馈结果，不要等做完才
    第一次出声；当场能回的快活直接回结果。

## 故障排查

| 现象 | 先查 |
|---|---|
| 容器 `PermissionError: config.json` | 文件属主须为 uid 1000 + `600`（注意事项 8） |
| `health.json` 的 `token_ok` 为 false | App ID/Secret 填错，或权限没发布 |
| 列群报 `99991672` | 缺 `im:chat:readonly`，补权限并重新发布 |
| 后台群下拉为空 | 新机器人先手动填 `chat_id`（注意事项 20） |
| @ 了没回复 | inbox 有没有这条 → watcher 基线是否已建 → mentions 里有没有该机器人 ID |
| 重复回复 | 发前是否查了 `sent/` 与 `outbox/` 去重 |
| 自己回自己（自回环） | 中转已过滤自消息；查 sender 的 `id_type` 判定 |
| 机器人间 @ 对方收不到 | 缺 `include_bot` 权限；或 mentions 用了另一视角的 ID |
| UI 登录突然失效 | UI 容器重启过，重新登录 |
| 改了配置没生效 | 热重载是否触发：看日志与 `last_poll_time`，不行就重启容器 |

## 目录结构

```
multiagent/
├── README.md                  # 本教程
├── relay/                     # 轮询中转（Python 标准库，零依赖）
│   ├── relay.py               # 中转本体：拉历史、发回复、热重载、去重
│   ├── Dockerfile
│   ├── compose.yaml           # 单机器人快速版
│   ├── config.example.json
│   └── README.md
├── platform/                  # 多机器人平台
│   ├── slots-compose.yaml     # 5 个 relay 槽位
│   └── ui/                    # 管理后台 + 队列 API（Python 标准库，零依赖）
│       ├── app.py
│       ├── Dockerfile
│       ├── compose.yaml
│       ├── README.md
│       └── tests/ui_smoke.py  # 本地冒烟测试（22 项）
├── watcher/                   # 自动回复 watcher 模板
│   ├── watch.sh.template      # hook 轮询脚本模板（有 SSH 版）
│   ├── wake-prompt.md         # 唤醒指令模板
│   └── README.md
└── docs/
    ├── friend-brief-template.md  # 给朋友的 Muse 的接入说明模板
    └── feishu-api-notes.md       # 实测过的飞书 API 事实
```

## 安全声明

本仓库不含任何真实凭据、令牌、ID 与主机信息，所有示例均为占位符。
部署时产生的 `config.json`、`state/`、`queues/`、`data/` 一律不进仓库。
你是平台管理员，能看到所有机器人的密钥——朋友得信得过你，平台密码
只你自己知道。
