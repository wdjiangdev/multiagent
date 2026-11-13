#!/usr/bin/env python3
"""
飞书中转 Relay - 单应用轮询中转骨架
只用飞书官方 OpenAPI + 标准库 urllib/json，无第三方依赖。

API 文档来源（必须按文档核实，代码注释写文档 URL）：
- tenant_access_token: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal
- 历史消息: https://open.feishu.cn/document/server-docs/im-v1/message/list
- 获取单条消息: https://open.feishu.cn/document/server-docs/im-v1/message/get
- 发送消息: https://open.feishu.cn/document/server-docs/im-v1/message/create
- 回复消息: https://open.feishu.cn/document/server-docs/im-v1/message/reply
- 列出群: https://open.feishu.cn/document/server-docs/group/chat/list
- @ 格式: https://open.feishu.cn/document/server-docs/im-v1/faq (搜索"机器人发送消息时，如何实现 @所有人、@指定人")

OPEN QUESTION 记录（核实不清的细节，绝不编造）：
1. sender.id_type 取值：文档写 "open_id" | "app_id"，实测是否可能返回 "union_id" / "user_id"？兜底：双重判断中只用 sender_type=="app" 且 id==app_id。
2. mentions 结构在接收消息里是否必现？文档有示例但未明确必填。兜底：mentions 缺省视为空列表。
3. list-chats 需要的权限：文档列出 im:chat / im:chat:readonly / im:chat:read，实测哪个最小通过？兜底：三个都在注释里写上，运行时报错再补。
4. 历史消息分页 page_token 与 watermark 配合的最佳实践？文档说 sort_type=ByCreateTimeAsc 时 page_token 固定。兜底：首轮不用 page_token，后续若 has_more 则存 page_token 到 watermark.json。
5. 回复消息 reply_in_thread 默认 false，文档说"如果要回复的消息已经是话题形式的消息，则默认以话题形式进行回复"。兜底：outbox 不传则 false，显式传 true 时带上。
6. 引用回复的 parent_id：文档称消息对象带 parent_id（被引用消息 id）。兜底：process_inbox 仅在 parent_id 存在时拉父消息；若 list 接口实测不返回 parent_id，则 quoted 恒为 None，需另查。
"""

import argparse
import json
import os
import sys
import time
import urllib.request
import urllib.error
import urllib.parse
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

# ============== 常量与路径 ==============
BASE_DIR = Path("/app")
CONFIG_DIR = BASE_DIR / "config"
STATE_DIR = BASE_DIR / "state"
QUEUES_DIR = BASE_DIR / "queues"
INBOX_DIR = QUEUES_DIR / "inbox"
OUTBOX_DIR = QUEUES_DIR / "outbox"
SENT_DIR = QUEUES_DIR / "sent"
FAILED_DIR = QUEUES_DIR / "failed"

CONFIG_FILE = CONFIG_DIR / "config.json"
WATERMARK_FILE = STATE_DIR / "watermark.json"
SEEN_FILE = STATE_DIR / "seen.json"
HEALTH_FILE = STATE_DIR / "health.json"

# 飞书官方 API 基址
FEISHU_BASE = "https://open.feishu.cn/open-apis"

# 默认轮询间隔（秒），可被 config.json 或环境变量覆盖
DEFAULT_POLL_INTERVAL = 60

# 占位符检测
PLACEHOLDER_APP_ID = "REPLACE_ME_APP_ID"
PLACEHOLDER_APP_SECRET = "REPLACE_ME_APP_SECRET"
PLACEHOLDER_CHAT_ID = "oc_xxx_REPLACE_ME"

# ============== 工具函数 ==============

def read_json(path: Path, default: Any = None) -> Any:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return default
    except json.JSONDecodeError:
        return default


def write_json_atomic(path: Path, data: Any) -> None:
    """原子写入：写临时文件再 rename，避免读到半截 JSON"""
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    tmp.replace(path)


def http_request(
    method: str,
    url: str,
    headers: Optional[Dict[str, str]] = None,
    data: Optional[bytes] = None,
    timeout: int = 15,
) -> Tuple[int, Dict[str, Any]]:
    """统一 HTTP 请求，返回 (status_code, json_body)"""
    req = urllib.request.Request(url, data=data, method=method)
    if headers:
        for k, v in headers.items():
            req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8")
            return resp.status, json.loads(body) if body else {}
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8")
        try:
            return e.code, json.loads(body)
        except json.JSONDecodeError:
            return e.code, {"code": e.code, "msg": body}
    except urllib.error.URLError as e:
        return 0, {"code": -1, "msg": f"network error: {e}"}


def convert_create_time_ms_to_start_time_s(create_time_ms: Any) -> Optional[int]:
    """把毫秒级 create_time 转换为秒级 start_time（官方文档示例：1608594809）"""
    if not create_time_ms:
        return None
    try:
        val = int(create_time_ms)
        return val // 1000 if val > 0 else None
    except (ValueError, TypeError):
        return None


def is_placeholder_config(config: Dict[str, Any]) -> bool:
    """检测是否为占位配置（凭据或chat_id占位）"""
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")
    chat_id = config.get("chat_id", "")
    return (
        app_id == PLACEHOLDER_APP_ID
        or app_secret == PLACEHOLDER_APP_SECRET
        or chat_id == PLACEHOLDER_CHAT_ID
        or not app_id
        or not app_secret
        or not chat_id
    )

def is_credentials_placeholder(config: Dict[str, Any]) -> bool:
    """检测凭据是否占位"""
    app_id = config.get("app_id", "")
    app_secret = config.get("app_secret", "")
    return (
        app_id == PLACEHOLDER_APP_ID
        or app_secret == PLACEHOLDER_APP_SECRET
        or not app_id
        or not app_secret
    )

def is_chat_id_missing(config: Dict[str, Any]) -> bool:
    """检测 chat_id 是否缺失/占位"""
    chat_id = config.get("chat_id", "")
    return chat_id == PLACEHOLDER_CHAT_ID or not chat_id


# ============== Token 管理 ==============

class TokenManager:
    """tenant_access_token 内存缓存 + 提前 5 分钟刷新"""
    # Doc: https://open.feishu.cn/document/server-docs/authentication-management/access-token/tenant_access_token_internal

    def __init__(self, app_id: str, app_secret: str):
        self.app_id = app_id
        self.app_secret = app_secret
        self._token: Optional[str] = None
        self._expire_at: float = 0  # 绝对时间戳（秒）

    def get_token(self) -> Optional[str]:
        now = time.time()
        if self._token and now < self._expire_at - 300:  # 提前 5 分钟刷新
            return self._token
        return self._refresh()

    def _refresh(self) -> Optional[str]:
        url = f"{FEISHU_BASE}/auth/v3/tenant_access_token/internal"
        payload = json.dumps({"app_id": self.app_id, "app_secret": self.app_secret}).encode()
        headers = {"Content-Type": "application/json; charset=utf-8"}
        code, resp = http_request("POST", url, headers=headers, data=payload)
        if code == 200 and resp.get("code") == 0:
            self._token = resp.get("tenant_access_token")
            expire = resp.get("expire", 7200)
            self._expire_at = time.time() + expire
            return self._token
        else:
            self._token = None
            self._expire_at = 0
            return None


# ============== 状态持久化 ==============

def load_watermark() -> Dict[str, Any]:
    """watermark.json: {last_create_time: int, last_message_id: str, page_token: str}"""
    return read_json(WATERMARK_FILE, {"last_create_time": 0, "last_message_id": "", "page_token": ""})


def save_watermark(wm: Dict[str, Any]) -> None:
    write_json_atomic(WATERMARK_FILE, wm)


def load_seen() -> List[str]:
    """seen.json: 最近 1000 个 message_id 去重"""
    seen = read_json(SEEN_FILE, [])
    if not isinstance(seen, list):
        return []
    return seen[-1000:]


def save_seen(seen: List[str]) -> None:
    write_json_atomic(SEEN_FILE, seen[-1000:])


def add_seen(message_id: str) -> None:
    seen = load_seen()
    if message_id not in seen:
        seen.append(message_id)
        save_seen(seen)


def is_seen(message_id: str) -> bool:
    return message_id in load_seen()


def update_health(
    token_ok: bool,
    last_error: str = "",
    inbox_count: int = 0,
    outbox_pending: int = 0,
) -> None:
    health = {
        "last_poll_time": int(time.time()),
        "token_ok": token_ok,
        "last_error": last_error,
        "inbox_count": inbox_count,
        "outbox_pending": outbox_pending,
    }
    write_json_atomic(HEALTH_FILE, health)


# ============== 飞书 API 调用 ==============

def fetch_messages(
    token: str, chat_id: str, watermark: Dict[str, Any], page_size: int = 50
) -> Tuple[List[Dict[str, Any]], Dict[str, Any]]:
    """
    拉取群历史消息（增量）
    Doc: https://open.feishu.cn/document/server-docs/im-v1/message/list
    返回: (新消息列表, 更新后的 watermark)
    """
    url = f"{FEISHU_BASE}/im/v1/messages"
    params = {
        "container_id_type": "chat",
        "container_id": chat_id,
        "sort_type": "ByCreateTimeAsc",
        "page_size": str(page_size),
    }
    # 续传 page_token
    if watermark.get("page_token"):
        params["page_token"] = watermark["page_token"]
    # 首轮或无 page_token 时，用 start_time 做增量锚点（秒级，来自毫秒 watermark 转换）
    elif watermark.get("last_create_time"):
        start_time_s = convert_create_time_ms_to_start_time_s(watermark["last_create_time"])
        if start_time_s is not None:
            params["start_time"] = str(start_time_s)

    query = urllib.parse.urlencode(params)
    full_url = f"{url}?{query}"
    headers = {"Authorization": f"Bearer {token}"}

    code, resp = http_request("GET", full_url, headers=headers)
    if code != 200 or resp.get("code") != 0:
        raise RuntimeError(f"fetch_messages failed: code={code}, resp={resp}")

    data = resp.get("data", {})
    items = data.get("items", [])
    has_more = data.get("has_more", False)
    page_token = data.get("page_token", "")

    new_wm = watermark.copy()
    if items:
        last = items[-1]
        new_wm["last_create_time"] = int(last.get("create_time", 0))
        new_wm["last_message_id"] = last.get("message_id", "")
    if has_more:
        new_wm["page_token"] = page_token
    else:
        new_wm["page_token"] = ""

    return items, new_wm


def get_message(token: str, message_id: str) -> Dict[str, Any]:
    """
    获取单条消息详情（用于取被引用消息的原文）
    Doc: https://open.feishu.cn/document/server-docs/im-v1/message/get
    返回: 消息对象 dict（无消息时抛 RuntimeError）
    """
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}"
    headers = {"Authorization": f"Bearer {token}"}
    code, resp = http_request("GET", url, headers=headers)
    if code != 200 or resp.get("code") != 0:
        raise RuntimeError(f"get_message failed: code={code}, resp={resp}")
    data = resp.get("data", {})
    items = data.get("items", [])
    if not items:
        raise RuntimeError(f"get_message empty: message_id={message_id}")
    return items[0]


def fetch_quoted_message(token: str, msg: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    """
    若 msg 是引用回复（带 parent_id），拉取被引用消息的原文，返回与 inbox 记录同形的 quoted 记录。
    失败时返回 None（不阻塞轮询）；非引用消息返回 None。
    引用回复的 parent_id 即被引用消息的 message_id；root_id 为话题根消息（有则一并记录）。
    """
    parent_id = msg.get("parent_id")
    if not parent_id:
        return None
    try:
        parent = get_message(token, parent_id)
    except Exception as e:
        print(f"[relay] fetch quoted {parent_id} failed: {e}", file=sys.stderr)
        return None
    body = parent.get("body", {})
    content_str = body.get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {"raw": content_str}
    return {
        "message_id": parent.get("message_id"),
        "sender": {
            "id": parent.get("sender", {}).get("id"),
            "id_type": parent.get("sender", {}).get("id_type"),
            "sender_type": parent.get("sender", {}).get("sender_type"),
        },
        "content": content,
        "create_time": parent.get("create_time"),
    }


def send_message(
    token: str, chat_id: str, text: str, mentions: Optional[List[Dict[str, Any]]] = None
) -> Dict[str, Any]:
    """
    发送文本消息到群聊
    Doc: https://open.feishu.cn/document/server-docs/im-v1/message/create
    @ 格式参考: https://open.feishu.cn/document/server-docs/im-v1/faq
    - 文本消息用 <at user_id="open_id">name</at>
    - @所有人用 <at user_id="all">所有人</at>
    """
    url = f"{FEISHU_BASE}/im/v1/messages?receive_id_type=chat_id"
    # 构造 content：text 类型，支持 @ 标签
    content_text = text
    if mentions:
        for m in mentions:
            uid = m.get("id", "")
            name = m.get("name", "")
            if uid == "all":
                content_text = f'<at user_id="all">所有人</at> {content_text}'
            else:
                content_text = f'<at user_id="{uid}">{name}</at> {content_text}'

    payload = {
        "receive_id": chat_id,
        "msg_type": "text",
        "content": json.dumps({"text": content_text}, ensure_ascii=False),
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    code, resp = http_request("POST", url, headers=headers, data=data)
    return {"code": code, "resp": resp}


def reply_message(
    token: str,
    message_id: str,
    text: str,
    mentions: Optional[List[Dict[str, Any]]] = None,
    reply_in_thread: bool = False,
) -> Dict[str, Any]:
    """
    回复指定消息
    Doc: https://open.feishu.cn/document/server-docs/im-v1/message/reply
    """
    url = f"{FEISHU_BASE}/im/v1/messages/{message_id}/reply"
    content_text = text
    if mentions:
        for m in mentions:
            uid = m.get("id", "")
            name = m.get("name", "")
            if uid == "all":
                content_text = f'<at user_id="all">所有人</at> {content_text}'
            else:
                content_text = f'<at user_id="{uid}">{name}</at> {content_text}'

    payload = {
        "msg_type": "text",
        "content": json.dumps({"text": content_text}, ensure_ascii=False),
        "reply_in_thread": reply_in_thread,
    }
    data = json.dumps(payload).encode("utf-8")
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json; charset=utf-8",
    }
    code, resp = http_request("POST", url, headers=headers, data=data)
    return {"code": code, "resp": resp}


def list_chats(token: str) -> List[Dict[str, Any]]:
    """
    列出机器人所在群
    Doc: https://open.feishu.cn/document/server-docs/group/chat/list
    权限：im:chat / im:chat:readonly / im:chat:read（三选一，实测以最小通过为准）
    OPEN QUESTION: 实测最小权限是哪个？
    """
    url = f"{FEISHU_BASE}/im/v1/chats"
    params = {"page_size": "100", "sort_type": "ByCreateTimeAsc"}
    all_items = []
    page_token = ""
    while True:
        if page_token:
            params["page_token"] = page_token
        query = urllib.parse.urlencode(params)
        full_url = f"{url}?{query}"
        headers = {"Authorization": f"Bearer {token}"}
        code, resp = http_request("GET", full_url, headers=headers)
        if code != 200 or resp.get("code") != 0:
            raise RuntimeError(f"list_chats failed: code={code}, resp={resp}")
        data = resp.get("data", {})
        items = data.get("items", [])
        all_items.extend(items)
        if data.get("has_more"):
            page_token = data.get("page_token", "")
        else:
            break
    return all_items


# ============== 核心逻辑 ==============

def check_and_reload_config(config_file: Path, current_mtime: float, current_config: Dict[str, Any], token_mgr: Optional[TokenManager]) -> Tuple[float, Dict[str, Any], Optional[TokenManager], bool]:
    """
    检查 config.json 的 mtime，若改变则重新读取与校验。
    返回: (new_mtime, new_config, new_token_mgr, reloaded_flag)
    """
    try:
        mtime = config_file.stat().st_mtime
    except OSError:
        return current_mtime, current_config, token_mgr, False

    if mtime == current_mtime:
        return current_mtime, current_config, token_mgr, False

    new_config = read_json(config_file, {})
    if not new_config:
        return current_mtime, current_config, token_mgr, False

    # 配置热重载
    app_id = new_config.get("app_id", "")
    app_secret = new_config.get("app_secret", "")
    chat_id = new_config.get("chat_id", "")

    has_app_id = bool(app_id and app_id != PLACEHOLDER_APP_ID)
    has_app_secret = bool(app_secret and app_secret != PLACEHOLDER_APP_SECRET)
    has_chat_id = bool(chat_id and chat_id != PLACEHOLDER_CHAT_ID)

    print(f"[relay] config reloaded: has_app_id={has_app_id}, has_app_secret={has_app_secret}, has_chat_id={has_chat_id}")

    new_token_mgr = None
    if has_app_id and has_app_secret:
        new_token_mgr = TokenManager(app_id, app_secret)

    # 尝试群发现（若凭据真实但 chat_id 仍为占位符）
    if new_token_mgr and is_chat_id_missing(new_config):
        try_discover_chats(new_token_mgr)

    return mtime, new_config, new_token_mgr, True


DISCOVER_MIN_INTERVAL_S = 600  # 群发现最小间隔（秒）：缺 chat_id 时最多每 10 分钟调一次 list-chats
_last_discover_attempt_mono: float = 0.0


def try_discover_chats(token_mgr: TokenManager) -> None:
    """
    当凭据真实但 chat_id 为占位符时，调 list-chats 并写 state/chats.json
    Doc: https://open.feishu.cn/document/server-docs/group/chat/list
    权限：im:chat / im:chat:readonly / im:chat:read（三选一）
    节流：距上次尝试不足 DISCOVER_MIN_INTERVAL_S 时直接跳过，避免每轮都调 API。
    失败时不覆盖已有的 chats.json，错误 code+message 记入日志。
    """
    global _last_discover_attempt_mono
    now_mono = time.monotonic()
    if now_mono - _last_discover_attempt_mono < DISCOVER_MIN_INTERVAL_S:
        return
    _last_discover_attempt_mono = now_mono
    token = token_mgr.get_token()
    if not token:
        return
    try:
        chats = list_chats(token)
        out = []
        for c in chats:
            out.append({
                "chat_id": c.get("chat_id"),
                "name": c.get("name"),
            })
        write_json_atomic(STATE_DIR / "chats.json", out)
        print(f"[relay] discovered {len(out)} chats, saved to state/chats.json")
    except Exception as e:
        print(f"[relay] try_discover_chats failed: {e} (existing chats.json kept, will retry)", file=sys.stderr)


def is_self_message(msg: Dict[str, Any], app_id: str, sent_ids: Optional[set] = None) -> bool:
    """
    判断是否为自己发的消息（双重判断）
    1. sender.sender_type == "app" 且 sender.id == app_id
    2. message_id 在已发送集合 sent_ids 中（运行时内存维护）
    OPEN QUESTION: sender.id_type 是否可能为 union_id/user_id？文档仅列 open_id/app_id。
    兜底：仅用 sender_type=="app" 且 id==app_id，以及 sent_ids 内存校验。
    """
    mid = msg.get("message_id")
    if sent_ids and mid and mid in sent_ids:
        return True
    sender = msg.get("sender", {})
    if sender.get("sender_type") == "app" and sender.get("id") == app_id:
        return True
    return False


def extract_inbox_record(msg: Dict[str, Any], quoted: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """从飞书消息提取 inbox 记录（不截断）。
    引用回复时 quoted 为被引用消息的同形记录；无引用时为 None。"""
    body = msg.get("body", {})
    content_str = body.get("content", "{}")
    try:
        content = json.loads(content_str)
    except json.JSONDecodeError:
        content = {"raw": content_str}

    mentions = msg.get("mentions", [])
    # mentions 结构：[{key, id, id_type, name, tenant_key}]
    mentions_out = []
    for m in mentions:
        mentions_out.append({
            "key": m.get("key"),
            "id": m.get("id"),
            "id_type": m.get("id_type"),
            "name": m.get("name"),
        })

    return {
        "message_id": msg.get("message_id"),
        "sender": {
            "id": msg.get("sender", {}).get("id"),
            "id_type": msg.get("sender", {}).get("id_type"),
            "sender_type": msg.get("sender", {}).get("sender_type"),
        },
        "mentions": mentions_out,
        "content": content,
        "create_time": msg.get("create_time"),
        "parent_id": msg.get("parent_id"),
        "root_id": msg.get("root_id"),
        "quoted": quoted,
    }


def write_inbox(record: Dict[str, Any]) -> None:
    """写入 inbox/（原子重命名）"""
    mid = record["message_id"]
    path = INBOX_DIR / f"{mid}.json"
    write_json_atomic(path, record)


def process_inbox(token: str, config: Dict[str, Any], app_id: str, sent_ids: Optional[set] = None) -> int:
    """轮询写入 inbox，返回新消息数"""
    chat_id = config["chat_id"]
    watermark = load_watermark()
    items, new_wm = fetch_messages(token, chat_id, watermark)
    save_watermark(new_wm)

    new_count = 0
    for msg in items:
        mid = msg.get("message_id")
        if not mid:
            continue
        if is_seen(mid):
            continue
        if is_self_message(msg, app_id, sent_ids=sent_ids):
            add_seen(mid)
            continue
        quoted = fetch_quoted_message(token, msg) if msg.get("parent_id") else None
        record = extract_inbox_record(msg, quoted=quoted)
        write_inbox(record)
        add_seen(mid)
        new_count += 1
    return new_count


def process_outbox(token: str, config: Dict[str, Any], dry_run: bool, sent_ids: set) -> int:
    """处理 outbox 待发队列，返回成功发送数"""
    sent_count = 0
    for entry in sorted(OUTBOX_DIR.glob("*.json")):
        try:
            with open(entry, "r", encoding="utf-8") as f:
                job = json.load(f)
        except (json.JSONDecodeError, OSError) as e:
            # 损坏文件移入 failed 并记日志
            print(f"[relay] outbox file corrupt {entry.name}: {e}", file=sys.stderr)
            move_to_failed(entry, f"corrupt json: {e}")
            continue

        text = job.get("text", "")
        mentions = job.get("mentions")  # list of {id, name}
        reply_to = job.get("reply_to_message_id")
        reply_in_thread = job.get("reply_in_thread", False)

        if dry_run:
            # DRY_RUN: 直接生成假 message_id 移入 sent/
            fake_mid = f"om_dryrun_{int(time.time() * 1000)}"
            move_to_sent(entry, fake_mid, job)
            sent_ids.add(fake_mid)
            sent_count += 1
            print(f"[relay] [DRY_RUN] sent outbox {entry.name} -> message_id {fake_mid}")
            continue

        try:
            if reply_to:
                result = reply_message(token, reply_to, text, mentions, reply_in_thread)
            else:
                result = send_message(token, config["chat_id"], text, mentions)

            code = result["code"]
            resp = result["resp"]
            if code == 200 and resp.get("code") == 0:
                data = resp.get("data", {})
                new_mid = data.get("message_id", "")
                if new_mid:
                    sent_ids.add(new_mid)
                move_to_sent(entry, new_mid, job, resp)
                sent_count += 1
            else:
                # 可重试错误（频控、临时网络）保留在 outbox，下轮重试
                # 230020: 消息包含敏感信息/触发风控限制
                # 230049: 发送消息频率超过限制（频控）
                # 11232: 触发 API 频控限制
                # 11233: 触发租户 API 频控限制
                # 99991400: 系统繁忙/临时错误
                # 标注：上述重试错误码集合来自常规错误码列表，尚未在飞书官方 OpenAPI 最新文档中获得逐一页面确认（OPEN QUESTION）。
                # 永久性错误（权限、参数错）移入 failed
                err_code = resp.get("code")
                if err_code in (230020, 230049, 11232, 11233, 99991400):
                    print(f"[relay] outbox retryable error for {entry.name}: code={code}, err_code={err_code}, msg={resp.get('msg')}", file=sys.stderr)
                else:
                    print(f"[relay] outbox permanent error for {entry.name}: code={code}, resp={resp}", file=sys.stderr)
                    move_to_failed(entry, f"api error: code={code}, resp={resp}")
        except Exception as e:
            # 网络异常等保留重试
            print(f"[relay] outbox network/unexpected exception for {entry.name}: {e}", file=sys.stderr)
    return sent_count


def move_to_sent(src: Path, message_id: str, job: Dict, resp: Optional[Dict] = None) -> None:
    """outbox -> sent/（附上返回的 message_id）"""
    dst = SENT_DIR / src.name
    record = {
        "original": job,
        "message_id": message_id,
        "sent_at": int(time.time()),
        "response": resp,
    }
    write_json_atomic(dst, record)
    try:
        src.unlink()
    except OSError:
        pass


def move_to_failed(src: Path, error: str) -> None:
    """outbox -> failed/（附错误文件）"""
    dst = FAILED_DIR / src.name
    record = {
        "original": read_json(src, {}),
        "error": error,
        "failed_at": int(time.time()),
    }
    write_json_atomic(dst, record)
    try:
        src.unlink()
    except OSError:
        pass


def run_list_chats(config: Dict[str, Any]) -> int:
    """list-chats 命令：打印 chat_id + 群名，仅需凭据真实"""
    if is_credentials_placeholder(config):
        print("ERROR: config.json 凭据占位，请先填入真实 app_id/app_secret", file=sys.stderr)
        return 1
    token_mgr = TokenManager(config["app_id"], config["app_secret"])
    token = token_mgr.get_token()
    if not token:
        print("ERROR: 获取 tenant_access_token 失败", file=sys.stderr)
        return 1
    try:
        chats = list_chats(token)
        for c in chats:
            print(f"{c.get('chat_id')}  {c.get('name')}")
        return 0
    except Exception as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1


def run_loop(config: Dict[str, Any], dry_run: bool = False, once: bool = False) -> int:
    """主循环"""
    poll_interval = config.get("poll_interval", DEFAULT_POLL_INTERVAL)
    if isinstance(poll_interval, str):
        poll_interval = int(poll_interval)

    sent_ids = set()  # 运行时内存：本次运行发送成功的 message_id，用于防自回环

    print(f"[relay] starting loop: dry_run={dry_run}, once={once}, poll_interval={poll_interval}s")

    config_mtime = 0.0
    try:
        config_mtime = CONFIG_FILE.stat().st_mtime
    except OSError:
        pass

    app_id = config.get("app_id", "")
    token_mgr: Optional[TokenManager] = None
    if not is_credentials_placeholder(config):
        token_mgr = TokenManager(config.get("app_id", ""), config.get("app_secret", ""))

    while True:
        loop_start = time.time()

        # 1. 检查配置热重载
        config_mtime, config, token_mgr, reloaded = check_and_reload_config(CONFIG_FILE, config_mtime, config, token_mgr)
        app_id = config.get("app_id", "")
        poll_interval = config.get("poll_interval", DEFAULT_POLL_INTERVAL)
        if isinstance(poll_interval, str):
            poll_interval = int(poll_interval)

        # 2. 占位配置处理
        if is_credentials_placeholder(config):
            if dry_run:
                outbox_done = process_outbox(token="", config=config, dry_run=True, sent_ids=sent_ids)
                outbox_pending = len(list(OUTBOX_DIR.glob("*.json")))
                update_health(token_ok=False, last_error="credentials placeholder (DRY_RUN)", inbox_count=0, outbox_pending=outbox_pending)
                print(f"[relay] [DRY_RUN placeholder] outbox sent {outbox_done}, pending {outbox_pending}")
            else:
                update_health(token_ok=False, last_error="credentials placeholder", inbox_count=0, outbox_pending=len(list(OUTBOX_DIR.glob("*.json"))))
                print("[relay] WARN: config.json 凭据占位，跳过飞书 API 调用，仅按间隔空转并记录日志")

            if once:
                break
            elapsed = time.time() - loop_start
            sleep_s = max(1, poll_interval - int(elapsed))
            time.sleep(sleep_s)
            continue

        # 3. 真实配置处理（含 chat_id 缺失状态）
        if not token_mgr:
            token_mgr = TokenManager(config.get("app_id", ""), config.get("app_secret", ""))

        token = token_mgr.get_token()
        token_ok = token is not None

        if token:
            if is_chat_id_missing(config):
                # 凭据真实但 chat_id 缺失：不轮询 inbox、不真发 outbox，只做群发现
                if dry_run:
                    # DRY_RUN 保留：仅模拟移队列，不调飞书 API
                    outbox_done = process_outbox(token="", config=config, dry_run=True, sent_ids=sent_ids)
                    print(f"[relay] [DRY_RUN waiting for chat_id] outbox sent {outbox_done}")
                update_health(token_ok=True, last_error="waiting for chat_id", inbox_count=0, outbox_pending=len(list(OUTBOX_DIR.glob("*.json"))))
                print("[relay] WARN: chat_id 缺失，正在尝试群发现...")
                try_discover_chats(token_mgr)
            else:
                # 完整运行
                try:
                    inbox_new = process_inbox(token, config, app_id, sent_ids=sent_ids)
                    outbox_done = process_outbox(token, config, dry_run, sent_ids)
                    update_health(
                        token_ok=True,
                        last_error="",
                        inbox_count=inbox_new,
                        outbox_pending=len(list(OUTBOX_DIR.glob("*.json"))),
                    )
                    print(f"[relay] poll ok: inbox+{inbox_new}, outbox sent {outbox_done}")
                except Exception as e:
                    update_health(token_ok=True, last_error=str(e), inbox_count=0, outbox_pending=0)
                    print(f"[relay] poll error: {e}")
        else:
            update_health(token_ok=False, last_error="token refresh failed", inbox_count=0, outbox_pending=0)
            print("[relay] WARN: token unavailable, will retry next poll")

        if once:
            break

        elapsed = time.time() - loop_start
        sleep_s = max(1, poll_interval - int(elapsed))
        time.sleep(sleep_s)
    return 0


# ============== 自测模式 ==============

def run_self_test() -> int:
    """
    容器内无网络自测：队列写读移动、去重、watermark、@ 渲染、占位检测
    使用临时目录，不污染生产 state/queues
    """
    import tempfile
    import shutil

    print("[self-test] starting...")
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        # 临时替换全局路径
        global CONFIG_DIR, STATE_DIR, QUEUES_DIR, INBOX_DIR, OUTBOX_DIR, SENT_DIR, FAILED_DIR
        global CONFIG_FILE, WATERMARK_FILE, SEEN_FILE, HEALTH_FILE
        global _last_discover_attempt_mono
        old = (CONFIG_DIR, STATE_DIR, QUEUES_DIR, INBOX_DIR, OUTBOX_DIR, SENT_DIR, FAILED_DIR,
               CONFIG_FILE, WATERMARK_FILE, SEEN_FILE, HEALTH_FILE)
        CONFIG_DIR = tmp / "config"
        STATE_DIR = tmp / "state"
        QUEUES_DIR = tmp / "queues"
        INBOX_DIR = QUEUES_DIR / "inbox"
        OUTBOX_DIR = QUEUES_DIR / "outbox"
        SENT_DIR = QUEUES_DIR / "sent"
        FAILED_DIR = QUEUES_DIR / "failed"
        CONFIG_FILE = CONFIG_DIR / "config.json"
        WATERMARK_FILE = STATE_DIR / "watermark.json"
        SEEN_FILE = STATE_DIR / "seen.json"
        HEALTH_FILE = STATE_DIR / "health.json"
        for d in (CONFIG_DIR, STATE_DIR, INBOX_DIR, OUTBOX_DIR, SENT_DIR, FAILED_DIR):
            d.mkdir(parents=True, exist_ok=True)

        try:
            # 1. 占位配置检测
            ph_config = {"app_id": PLACEHOLDER_APP_ID, "app_secret": PLACEHOLDER_APP_SECRET, "chat_id": PLACEHOLDER_CHAT_ID}
            assert is_placeholder_config(ph_config), "placeholder detection failed"
            real_config = {"app_id": "cli_test", "app_secret": "secret", "chat_id": "oc_test"}
            assert not is_placeholder_config(real_config), "real config falsely detected as placeholder"
            print("  [ok] placeholder detection")

            # 1.5 D1 start_time 毫秒转秒换算
            assert convert_create_time_ms_to_start_time_s("1608594809123") == 1608594809
            assert convert_create_time_ms_to_start_time_s(1608594809123) == 1608594809
            assert convert_create_time_ms_to_start_time_s(0) is None
            assert convert_create_time_ms_to_start_time_s(None) is None
            print("  [ok] D1 start_time ms to s conversion")

            # 2. watermark 读写
            wm = load_watermark()
            assert wm["last_create_time"] == 0
            wm["last_create_time"] = 1234567890
            wm["last_message_id"] = "om_test"
            save_watermark(wm)
            wm2 = load_watermark()
            assert wm2["last_create_time"] == 1234567890
            assert wm2["last_message_id"] == "om_test"
            print("  [ok] watermark read/write")

            # 3. seen 去重（最多 1000）
            for i in range(1005):
                add_seen(f"om_{i}")
            seen = load_seen()
            assert len(seen) == 1000
            assert is_seen("om_5")
            assert not is_seen("om_9999")
            print("  [ok] seen dedup (max 1000)")

            # 4. inbox 写入
            rec = {
                "message_id": "om_111",
                "sender": {"id": "ou_123", "id_type": "open_id", "sender_type": "user"},
                "mentions": [{"key": "@_user_1", "id": "ou_456", "id_type": "open_id", "name": "Bob"}],
                "content": {"text": "hello"},
                "create_time": "1234567890123",
            }
            write_inbox(rec)
            assert (INBOX_DIR / "om_111.json").exists()
            print("  [ok] inbox write")

            # 5. outbox -> sent (DRY_RUN 模拟，包含占位配置下 DRY_RUN 连通测试)
            job = {"text": "reply", "mentions": [{"id": "ou_456", "name": "Bob"}], "reply_to_message_id": "om_111"}
            write_json_atomic(OUTBOX_DIR / "job1.json", job)
            sent_ids = set()
            process_outbox("dummy_token", ph_config, dry_run=True, sent_ids=sent_ids)
            assert len(list(SENT_DIR.glob("*.json"))) == 1
            sent_rec = read_json(list(SENT_DIR.glob("*.json"))[0])
            assert sent_rec["message_id"].startswith("om_dryrun_")
            assert sent_rec["original"]["text"] == "reply"
            print("  [ok] outbox -> sent (DRY_RUN with placeholder config)")

            # 6. @ 渲染（文本消息格式）
            # 仅测试 content 字符串构造逻辑
            text = "hello"
            mentions = [{"id": "ou_123", "name": "Alice"}, {"id": "all", "name": "所有人"}]
            # 模拟 send_message 里的构造
            content_text = text
            for m in mentions:
                uid = m.get("id", "")
                name = m.get("name", "")
                if uid == "all":
                    content_text = f'<at user_id="all">所有人</at> {content_text}'
                else:
                    content_text = f'<at user_id="{uid}">{name}</at> {content_text}'
            assert '<at user_id="ou_123">Alice</at>' in content_text
            assert '<at user_id="all">所有人</at>' in content_text
            print("  [ok] @ mention rendering")

            # 7. 防自回环：is_self_message
            msg_app = {"sender": {"id": "cli_test", "id_type": "app_id", "sender_type": "app"}, "message_id": "om_x"}
            msg_user = {"sender": {"id": "ou_123", "id_type": "open_id", "sender_type": "user"}, "message_id": "om_y"}
            assert is_self_message(msg_app, "cli_test")
            assert not is_self_message(msg_user, "cli_test")
            print("  [ok] self-message detection")

            # 8. health 写入
            update_health(token_ok=True, last_error="", inbox_count=5, outbox_pending=2)
            health = read_json(HEALTH_FILE)
            assert health["token_ok"] is True
            assert health["inbox_count"] == 5
            print("  [ok] health write")

            # 9. 热重载测试
            cfg_file = tmp / "config" / "config.json"
            write_json_atomic(cfg_file, ph_config)
            mtime0 = cfg_file.stat().st_mtime
            mtime1, cfg1, tm1, reloaded1 = check_and_reload_config(cfg_file, mtime0, ph_config, None)
            assert not reloaded1, "should not reload when mtime unchanged"

            time.sleep(0.01)
            new_cfg = {"app_id": "cli_new", "app_secret": "sec_new", "chat_id": "oc_new"}
            write_json_atomic(cfg_file, new_cfg)
            mtime2, cfg2, tm2, reloaded2 = check_and_reload_config(cfg_file, mtime0, ph_config, None)
            assert reloaded2, "should reload on mtime change"
            assert cfg2["app_id"] == "cli_new"
            assert tm2 is not None and tm2.app_id == "cli_new"
            print("  [ok] config hot reload")

            # 10. 占位与真实配置动态切换
            mtime3, cfg3, tm3, reloaded3 = check_and_reload_config(cfg_file, mtime2, cfg2, tm2)
            assert not reloaded3
            time.sleep(0.01)
            write_json_atomic(cfg_file, ph_config)
            mtime4, cfg4, tm4, reloaded4 = check_and_reload_config(cfg_file, mtime2, cfg2, tm2)
            assert reloaded4
            assert is_placeholder_config(cfg4)
            assert tm4 is None
            print("  [ok] placeholder <-> real config dynamic switch")

            # 11. D5 三种配置状态
            creds_real_no_chat = {"app_id": "cli_test", "app_secret": "secret", "chat_id": PLACEHOLDER_CHAT_ID}
            assert is_credentials_placeholder(ph_config) and is_chat_id_missing(ph_config)
            assert not is_credentials_placeholder(creds_real_no_chat) and is_chat_id_missing(creds_real_no_chat)
            assert is_placeholder_config(creds_real_no_chat), "legacy helper still flags missing chat_id"
            assert not is_credentials_placeholder(real_config) and not is_chat_id_missing(real_config)
            # 11a. run_list_chats 门：凭据占位拒跑且不建 TokenManager；凭据真实+缺 chat_id 放行（mock 网络）
            import io
            import contextlib
            _orig_tm = TokenManager
            _orig_lc = list_chats
            tm_calls = []
            class _FakeTM:
                def __init__(self, app_id, app_secret):
                    tm_calls.append((app_id, app_secret))
                def get_token(self):
                    return "tok_fake"
            def _fake_list_chats(token):
                assert token == "tok_fake"
                return [{"chat_id": "oc_mock", "name": "MockGroup"}]
            try:
                globals()["TokenManager"] = _FakeTM
                globals()["list_chats"] = _fake_list_chats
                assert run_list_chats(ph_config) == 1
                assert tm_calls == [], "placeholder creds must not construct TokenManager"
                buf = io.StringIO()
                with contextlib.redirect_stdout(buf):
                    assert run_list_chats(creds_real_no_chat) == 0
                assert "oc_mock" in buf.getvalue() and "MockGroup" in buf.getvalue()
            finally:
                globals()["TokenManager"] = _orig_tm
                globals()["list_chats"] = _orig_lc
            # 11b. 热重载：凭据真实+缺 chat_id 时建 token_mgr 并触发一次群发现（mock 发现函数）
            discover_calls = []
            _orig_discover = try_discover_chats
            try:
                globals()["try_discover_chats"] = lambda tm: discover_calls.append(tm)
                time.sleep(0.01)
                write_json_atomic(cfg_file, creds_real_no_chat)
                mtime5, cfg5, tm5, reloaded5 = check_and_reload_config(cfg_file, mtime4, cfg4, tm4)
                assert reloaded5 and tm5 is not None and tm5.app_id == "cli_test"
                assert len(discover_calls) == 1, "reload to creds-real+missing-chat must trigger discovery"
            finally:
                globals()["try_discover_chats"] = _orig_discover
            print("  [ok] D5 three config states (helpers, list-chats gate, reload discovery)")

            # 12. 群发现节流 + 失败不覆盖 chats.json
            _orig_lc2 = list_chats
            lc_calls = []
            def _counting_list_chats(token):
                lc_calls.append(token)
                return [{"chat_id": "oc_a", "name": "GroupA"}]
            class _FakeTM2:
                def get_token(self):
                    return "tok_fake"
            try:
                globals()["list_chats"] = _counting_list_chats
                _last_discover_attempt_mono = 0.0
                try_discover_chats(_FakeTM2())
                try_discover_chats(_FakeTM2())
                assert len(lc_calls) == 1, "second discovery within interval must be throttled"
                chats_saved = read_json(STATE_DIR / "chats.json")
                assert chats_saved == [{"chat_id": "oc_a", "name": "GroupA"}]
                # 节流过期后再次调用应放行
                _last_discover_attempt_mono = time.monotonic() - DISCOVER_MIN_INTERVAL_S - 1
                try_discover_chats(_FakeTM2())
                assert len(lc_calls) == 2
                # 失败时不覆盖已有 chats.json、不抛异常
                def _failing_list_chats(token):
                    raise RuntimeError("list_chats failed: code=99991401, resp={'code': 99991401}")
                globals()["list_chats"] = _failing_list_chats
                _last_discover_attempt_mono = 0.0
                try_discover_chats(_FakeTM2())  # must not raise
                assert read_json(STATE_DIR / "chats.json") == [{"chat_id": "oc_a", "name": "GroupA"}]
            finally:
                globals()["list_chats"] = _orig_lc2
                _last_discover_attempt_mono = 0.0
            print("  [ok] discovery throttle + failure keeps chats.json")

            # 13. 引用回复透传：extract_inbox_record 带 parent_id/root_id/quoted；
            #     fetch_quoted_message 拉父消息（mock get_message），失败返回 None 不抛
            msg_reply = {
                "message_id": "om_reply",
                "sender": {"id": "ou_123", "id_type": "open_id", "sender_type": "user"},
                "mentions": [],
                "body": {"msg_type": "text", "content": json.dumps({"text": "针对这条回复"}, ensure_ascii=False)},
                "create_time": "1794575415754",
                "parent_id": "om_parent",
                "root_id": "om_parent",
            }
            rec_plain = extract_inbox_record(msg_reply)
            assert rec_plain["parent_id"] == "om_parent"
            assert rec_plain["root_id"] == "om_parent"
            assert rec_plain["quoted"] is None
            _orig_gm = get_message
            gm_calls = []
            def _fake_get_message(token, message_id):
                gm_calls.append((token, message_id))
                return {
                    "message_id": "om_parent",
                    "sender": {"id": "ou_999", "id_type": "open_id", "sender_type": "user"},
                    "body": {"msg_type": "text", "content": json.dumps({"text": "被引用的原文"}, ensure_ascii=False)},
                    "create_time": "1794575300000",
                }
            try:
                globals()["get_message"] = _fake_get_message
                quoted = fetch_quoted_message("tok_fake", msg_reply)
                assert quoted is not None
                assert quoted["message_id"] == "om_parent"
                assert quoted["sender"]["id"] == "ou_999"
                assert quoted["content"] == {"text": "被引用的原文"}
                assert gm_calls == [("tok_fake", "om_parent")]
                rec_quoted = extract_inbox_record(msg_reply, quoted=quoted)
                assert rec_quoted["quoted"]["content"] == {"text": "被引用的原文"}
                # 非引用消息不调 get_message
                gm_calls.clear()
                assert fetch_quoted_message("tok_fake", {"message_id": "om_x"}) is None
                assert gm_calls == []
                # 父消息拉取失败返回 None、不抛异常
                def _failing_get_message(token, message_id):
                    raise RuntimeError("get_message failed: code=400")
                globals()["get_message"] = _failing_get_message
                assert fetch_quoted_message("tok_fake", msg_reply) is None
            finally:
                globals()["get_message"] = _orig_gm
            print("  [ok] quoted reply passthrough (parent_id/root_id/quoted, fetch failure tolerated)")

            print("[self-test] ALL PASSED")
            return 0
        finally:
            # 恢复全局路径
            (CONFIG_DIR, STATE_DIR, QUEUES_DIR, INBOX_DIR, OUTBOX_DIR, SENT_DIR, FAILED_DIR,
             CONFIG_FILE, WATERMARK_FILE, SEEN_FILE, HEALTH_FILE) = old


# ============== CLI 入口 ==============

def main():
    parser = argparse.ArgumentParser(description="飞书中转 Relay")
    parser.add_argument("--self-test", action="store_true", help="容器内无网络自测")
    parser.add_argument("--list-chats", action="store_true", help="列出机器人所在群（需真实配置）")
    parser.add_argument("--once", action="store_true", help="跑一轮即退出（调试用）")
    parser.add_argument("--dry-run", action="store_true", help="DRY_RUN 模式：outbox 不调飞书，直接移入 sent/")
    args = parser.parse_args()

    # 环境变量 DRY_RUN=1 也可触发
    dry_run = args.dry_run or os.environ.get("DRY_RUN") == "1"

    if args.self_test:
        return run_self_test()

    # 读取配置
    config = read_json(CONFIG_FILE, {})
    if not config:
        print("ERROR: config.json 不存在或为空", file=sys.stderr)
        return 1

    if args.list_chats:
        return run_list_chats(config)

    return run_loop(config, dry_run=dry_run, once=args.once)


if __name__ == "__main__":
    sys.exit(main())