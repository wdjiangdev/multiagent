# 飞书 API 实测笔记

2026-11-13 真机联调验证过的事实，写代码和排障时不用重猜。
飞书与 Lark 是不同部署、同一权限模型；Lark 也一样需要中转。

## 身份与 token

- 机器人身份必须用 `tenant_access_token`：`POST
  https://open.feishu.cn/open-apis/auth/v3/tenant_access_token/internal`，
  body 为 App ID + App Secret。token 最长 2 小时。
- 用户 token（user_access_token）发出来的是用户本人的消息，成不了机器人。
- token 可在内存里缓存、提前 5 分钟刷新：每应用约 2 小时才 1 次换 token
  请求，轮询加快不增加换 token 次数。

## 消息接口

- 拉历史：`GET /open-apis/im/v1/messages`，`container_id_type=chat`。
  **`start_time` 参数是秒，消息的 `create_time` 是毫秒**，换算错了会漏
  消息或重复拉。`sort_type=ByCreateTimeAsc` 时分页配合 watermark。
- 发送：`POST /open-apis/im/v1/messages?receive_id_type=chat_id`。
- 回复：`POST /open-apis/im/v1/messages/:message_id/reply`，
  回复的 `parent_id`/`root_id` 指向原消息。
- 列群：`GET /open-apis/im/v1/chats`，最小权限 `im:chat:readonly`，
  缺了报 `99991672`。
- @ 格式：文本前拼 `<at user_id="open_id">名字</at>` 标签。

## ID 与字段（实测）

- **open_id 因 app 而异**：同一条消息，在不同应用拉到的 mentions 里，
  同一人的 open_id 各不相同；sender id 也一样。跨视角 ID 绝不能混用。
  @ 对方用自己视角的 ID，飞书侧会解析（已实测：A 用 A 视角的 B 的 ID
  发出，B 的 inbox 里显示为 B 视角的自己，mention 完整）。
- sender：用户消息 `id_type=open_id`；机器人消息 `id_type=app_id`，
  id 为该机器人的 App ID（`cli_...`）。
- `mentions` 字段恒在：普通消息为数组，系统消息为空数组。
- 机器人间 @ 要进轮询历史，必须开
  `im:message.group_at_msg.include_bot:readonly`（仅自建应用）。

## 权限清单（7 项）

`im:message`、`im:message:send_as_bot`、`im:message.group_msg`、
`im:message.group_at_msg:readonly`、
`im:message.group_at_msg.include_bot:readonly`、
`im:message.p2p_msg:readonly`、`im:chat:readonly`。

## 限流体感

轮询每应用每分钟个位数请求（10 秒一轮约 6 次），离限流很远。
走官方接口 + 应用身份，不牵连个人账号。飞书没有最小轮询间隔一说。
