#!/usr/bin/env python3
"""飞书多机器人平台：单管理员后台 + 每机器人队列 API。

由单机器人配置页（feishu-config-ui app.py）扩展而来，登录/锁仓/CSRF/会话、
占位拒绝、空字段保留、下拉 /app.js 三点逻辑与已修复版一致。

路径（容器内，环境变量可覆盖，仅用于本地测试）：
- SLOTS_DIR: 槽位根目录，/slots/slotN/{config,state,queues}，slot1 挂载既有
  /opt/docker/feishu-relay，slot2..5 挂载 /opt/docker/feishu-platform/slots/slotN。
- DATA_DIR: 后台数据（auth/sessions/bots/tokens），持久卷。
- SETUP_TOKEN_DIR: 首次设置令牌明文文件落点（宿主机项目目录）。

安全要点：
- 管理员会话与 API 令牌完全分离；API 只认 Bearer 令牌，不认会话 Cookie。
- 令牌只存 SHA-256 哈希，生成时仅显示一次，绝不回显、绝不记日志。
- API 响应绝不含密钥/配置值；错 token、错 bot、非法路径一律 404，不泄露存在性。
- 一切路径拼接先过白名单正则，防 ../ 穿越。
"""
import os
import re
import json
import secrets
import hashlib
import hmac
import html
import time
import shutil
import threading
import http.server
import socketserver
import urllib.parse
from pathlib import Path
from http import cookies

# ============== 配置与路径 ==============
SLOTS_DIR = Path(os.environ.get("SLOTS_DIR", "/slots"))
DATA_DIR = Path(os.environ.get("DATA_DIR", "/data"))
SETUP_TOKEN_DIR = Path(os.environ.get("SETUP_TOKEN_DIR", "/setup-token-dir"))
UI_PORT = int(os.environ.get("UI_PORT", "8000"))

SLOT_NAMES = ["slot1", "slot2", "slot3", "slot4", "slot5"]

AUTH_FILE = DATA_DIR / "auth.json"  # {hash, salt}
SESSION_FILE = DATA_DIR / "sessions.json"  # {token: {expiry, csrf_token}}
SETUP_TOKEN_FILE = DATA_DIR / "setup_token_hash.json"
SETUP_FLAG_FILE = DATA_DIR / "setup_done.flag"
SETUP_TOKEN_TEXT = SETUP_TOKEN_DIR / "setup-token.txt"
BOTS_FILE = DATA_DIR / "bots.json"  # {slug: {name, slot, history_allowed, created_at}}
TOKENS_FILE = DATA_DIR / "tokens.json"  # {token_id: {hash, bot, created_at, prefix}}

PLACEHOLDER_APP_ID = "REPLACE_ME_APP_ID"
PLACEHOLDER_APP_SECRET = "REPLACE_ME_APP_SECRET"
PLACEHOLDER_CHAT_ID = "oc_xxx_REPLACE_ME"

SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
MSG_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")
MENTION_ID_RE = re.compile(r"^[A-Za-z0-9_-]+$")

API_MAX_BODY = 32 * 1024
FORM_MAX_BODY = 10 * 1024
OUTBOX_RATE_LIMIT = 30  # 每机器人每分钟最多 POST 次数
OUTBOX_RATE_WINDOW = 60

# 线程安全：数据文件写 + 限流状态共用一把锁（ThreadingHTTPServer）
LOCK = threading.RLock()
LOGIN_ATTEMPTS = {}  # {ip: [count, lock_until]}
OUTBOX_RATE = {}  # {bot: [timestamps]}

# ============== 工具函数 ==============

def read_json(path, default=None):
    if not path.exists():
        return default
    try:
        with open(path, "r") as f:
            return json.load(f)
    except Exception:
        return default


def write_json_atomic(path, data):
    tmp = path.with_suffix(".tmp")
    fd = os.open(tmp, os.O_CREAT | os.O_WRONLY | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(data, f)
        f.flush()
        os.fsync(f.fileno())
    tmp.replace(path)


def hash_password(password, salt=None):
    if salt is None:
        salt = secrets.token_bytes(16)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return pwd_hash.hex(), salt.hex()


def verify_password(password, pwd_hash_hex, salt_hex):
    salt = bytes.fromhex(salt_hex)
    pwd_hash = hashlib.pbkdf2_hmac('sha256', password.encode(), salt, 100000)
    return pwd_hash.hex() == pwd_hash_hex


def get_session(token):
    sessions = read_json(SESSION_FILE, {})
    if token in sessions:
        if sessions[token]["expiry"] > time.time():
            return sessions[token]
        else:
            with LOCK:
                sessions = read_json(SESSION_FILE, {})
                if token in sessions:
                    del sessions[token]
                    write_json_atomic(SESSION_FILE, sessions)
    return None


def create_session():
    token = secrets.token_urlsafe(32)
    expiry = time.time() + 3600 * 24  # 24h
    with LOCK:
        sessions = read_json(SESSION_FILE, {})
        csrf_token = secrets.token_urlsafe(32)
        sessions[token] = {"expiry": expiry, "csrf_token": csrf_token}
        write_json_atomic(SESSION_FILE, sessions)
    return token, csrf_token


# ============== 机器人注册表与槽位 ==============

def load_bots():
    bots = read_json(BOTS_FILE, None)
    return bots if isinstance(bots, dict) else {}


def save_bots(bots):
    with LOCK:
        write_json_atomic(BOTS_FILE, bots)


def ensure_bots():
    """首次启动预置 President(slot1) 与 Secretary(slot2)，其余槽位空闲。"""
    if BOTS_FILE.exists():
        return
    now = int(time.time())
    bots = {
        "president": {"name": "President", "slot": "slot1",
                      "history_allowed": False, "created_at": now},
        "secretary": {"name": "Secretary", "slot": "slot2",
                      "history_allowed": False, "created_at": now},
    }
    save_bots(bots)
    print("[ui] bots registry seeded: president->slot1, secretary->slot2")


def slot_dir(bot):
    return SLOTS_DIR / bot["slot"]


def bot_config_file(bot):
    return slot_dir(bot) / "config" / "config.json"


def bot_state_dir(bot):
    return slot_dir(bot) / "state"


def bot_queues_dir(bot):
    return slot_dir(bot) / "queues"


def free_slot(bots):
    used = {b["slot"] for b in bots.values()}
    for s in SLOT_NAMES:
        if s not in used:
            return s
    return None


def slugify(name):
    slug = name.strip().lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug


def config_filled(config):
    return bool(
        config.get("app_id") and config.get("app_id") != PLACEHOLDER_APP_ID
        and config.get("app_secret") and config.get("app_secret") != PLACEHOLDER_APP_SECRET
        and config.get("chat_id") and config.get("chat_id") != PLACEHOLDER_CHAT_ID
    )


# ============== API 令牌 ==============

def load_tokens():
    tokens = read_json(TOKENS_FILE, None)
    return tokens if isinstance(tokens, dict) else {}


def save_tokens(tokens):
    with LOCK:
        write_json_atomic(TOKENS_FILE, tokens)


def token_bot(presented):
    """Bearer 令牌 -> 所属机器人 slug；无效返回 None。"""
    if not presented:
        return None
    digest = hashlib.sha256(presented.encode()).hexdigest()
    for rec in load_tokens().values():
        if hmac.compare_digest(digest, rec.get("hash", "")):
            return rec.get("bot")
    return None


def check_outbox_rate(bot):
    """滑动窗口限流；返回 True 表示放行，False 表示超限。"""
    now = time.time()
    with LOCK:
        stamps = [t for t in OUTBOX_RATE.get(bot, []) if now - t < OUTBOX_RATE_WINDOW]
        if len(stamps) >= OUTBOX_RATE_LIMIT:
            OUTBOX_RATE[bot] = stamps
            return False
        stamps.append(now)
        OUTBOX_RATE[bot] = stamps
        return True


# ============== Web 处理 ==============

class PlatformHandler(http.server.BaseHTTPRequestHandler):
    def log_request(self, code='-', size='-'):
        if isinstance(self.path, str) and self.path.startswith("/setup/"):
            masked_path = "/setup/***"
            masked_requestline = self.requestline.replace(self.path, masked_path)
            self.log_message('"%s" %s %s', masked_requestline, str(code), str(size))
        else:
            super().log_request(code, size)

    # ---------- 路由 ----------

    def do_GET(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path
        query = urllib.parse.parse_qs(url.query)

        # 队列 API 优先：不走会话/CSRF，只认 Bearer
        if path.startswith("/api/v1/"):
            return self.handle_api_get(path, query)

        # 首次设置流程
        if not SETUP_FLAG_FILE.exists() and not AUTH_FILE.exists():
            if path.startswith("/setup/"):
                token = path[7:]
                if self.verify_setup_token(token):
                    return self.render_setup_page()
                else:
                    return self.send_error_page(403, "Invalid or expired setup token.")
            elif path == "/":
                return self.send_redirect("/setup-hint")
            elif path == "/setup-hint":
                return self.render_setup_hint()

        if path == "/app.js":
            return self.render_app_js()

        session = self.get_current_session()
        if not session:
            if path == "/login":
                return self.render_login_page()
            else:
                return self.send_redirect("/login")

        if path == "/":
            return self.send_redirect("/bots")
        elif path == "/config":
            return self.send_redirect("/bots")
        elif path == "/bots":
            return self.render_bots_page(session)
        elif path == "/bots/new":
            return self.render_bot_new_page(session)
        elif path == "/logout":
            return self.handle_logout()

        m = re.match(r"^/bots/([A-Za-z0-9_-]+)/(edit|status|tokens|delete)$", path)
        if m:
            slug, action = m.group(1), m.group(2)
            bots = load_bots()
            if slug not in bots:
                return self.send_error(404)
            return self.send_redirect(f"/bots/{slug}")

        m = re.match(r"^/bots/([A-Za-z0-9_-]+)$", path)
        if m:
            slug = m.group(1)
            bots = load_bots()
            if slug not in bots:
                return self.send_error(404)
            return self.render_bot_detail_page(session, slug, bots[slug])

        self.send_error(404)

    def do_POST(self):
        url = urllib.parse.urlparse(self.path)
        path = url.path

        if path.startswith("/api/v1/"):
            length = int(self.headers.get('Content-Length', 0) or 0)
            if length > API_MAX_BODY:
                return self.send_json(413, {"error": "payload too large"})
            body = self.rfile.read(length) if length > 0 else b""
            return self.handle_api_post(path, body)

        length = int(self.headers.get('Content-Length', 0) or 0)
        if length > FORM_MAX_BODY:
            return self.send_error(413)
        body = self.rfile.read(length).decode(errors="replace")
        params = urllib.parse.parse_qs(body)

        # 首次设置提交
        if not SETUP_FLAG_FILE.exists() and not AUTH_FILE.exists():
            if path == "/setup/submit":
                return self.handle_setup_submit(params)

        # 登录提交
        if path == "/login":
            return self.handle_login_submit(params)

        # 鉴权后的操作
        session = self.get_current_session()
        if not session:
            return self.send_redirect("/login")

        # CSRF 校验
        token_in_body = params.get("csrf_token", [""])[0]
        if token_in_body != session.get("csrf_token"):
            return self.send_error_page(403, "CSRF token mismatch.")

        if path == "/bots/new":
            return self.handle_bot_create(session, params)

        m = re.match(r"^/bots/([A-Za-z0-9_-]+)/(save|tokens/generate|tokens/rotate|tokens/revoke|delete)$", path)
        if m:
            slug, action = m.group(1), m.group(2)
            bots = load_bots()
            if slug not in bots:
                return self.send_error(404)
            if action == "save":
                return self.handle_bot_save(session, slug, bots[slug], params)
            elif action == "tokens/generate":
                return self.handle_token_generate(session, slug, bots[slug], rotate=False, params=params)
            elif action == "tokens/rotate":
                return self.handle_token_generate(session, slug, bots[slug], rotate=True, params=params)
            elif action == "tokens/revoke":
                return self.handle_token_revoke(session, slug, bots[slug], params)
            elif action == "delete":
                return self.handle_bot_delete(session, slug, bots[slug], params)

        self.send_error(404)

    # ---------- 会话与设置 ----------

    def get_current_session(self):
        cookie_str = self.headers.get('Cookie', '')
        c = cookies.SimpleCookie(cookie_str)
        if "session" in c:
            return get_session(c["session"].value)
        return None

    def verify_setup_token(self, token):
        st = read_json(SETUP_TOKEN_FILE)
        if not st:
            return False
        if time.time() > st["expiry"]:
            return False
        h = hashlib.sha256(token.encode()).hexdigest()
        return hmac.compare_digest(h, st["hash"])

    def handle_setup_submit(self, params):
        token = params.get("token", [""])[0]
        if not self.verify_setup_token(token):
            return self.send_error_page(403, "Invalid token.")
        password = params.get("password", [""])[0]
        if len(password) < 8:
            return self.send_error_page(400, "Password too short.")

        pwd_hash, salt = hash_password(password)
        write_json_atomic(AUTH_FILE, {"hash": pwd_hash, "salt": salt})
        write_json_atomic(SETUP_FLAG_FILE, {"done_at": int(time.time())})
        if SETUP_TOKEN_FILE.exists():
            SETUP_TOKEN_FILE.unlink()
        if SETUP_TOKEN_TEXT.exists():
            try:
                SETUP_TOKEN_TEXT.unlink()
            except Exception:
                pass

        print("[ui] setup completed")
        return self.send_redirect("/login")

    def handle_login_submit(self, params):
        ip = self.client_address[0]
        now = time.time()
        with LOCK:
            attempt = LOGIN_ATTEMPTS.get(ip, [0, 0])
            if attempt[1] > now:
                return self.send_error_page(403, f"Locked. Try again in {int(attempt[1]-now)}s.")

        password = params.get("password", [""])[0]
        auth = read_json(AUTH_FILE)

        if auth and verify_password(password, auth["hash"], auth["salt"]):
            with LOCK:
                LOGIN_ATTEMPTS[ip] = [0, 0]
            token, csrf = create_session()
            self.send_response(302)
            self.send_header("Location", "/bots")
            self.send_header("Set-Cookie", f"session={token}; HttpOnly; Secure; SameSite=Lax; Path=/")
            self.end_headers()
            print(f"[ui] login success: {ip}")
        else:
            with LOCK:
                attempt = LOGIN_ATTEMPTS.get(ip, [0, 0])
                attempt[0] += 1
                if attempt[0] >= 5:
                    attempt[1] = now + 900  # 15 min
                LOGIN_ATTEMPTS[ip] = attempt
            print(f"[ui] login failed: {ip}, count={attempt[0]}")
            return self.send_error_page(401, "Invalid password.")

    def handle_logout(self):
        cookie_str = self.headers.get('Cookie', '')
        c = cookies.SimpleCookie(cookie_str)
        if "session" in c:
            token = c["session"].value
            with LOCK:
                sessions = read_json(SESSION_FILE, {})
                if token in sessions:
                    del sessions[token]
                    write_json_atomic(SESSION_FILE, sessions)
        self.send_response(302)
        self.send_header("Location", "/login")
        self.send_header("Set-Cookie", "session=; Max-Age=0; Path=/")
        self.end_headers()

    # ---------- 机器人管理 ----------

    def handle_bot_create(self, session, params):
        name = params.get("name", [""])[0].strip()
        if not name or len(name) > 64:
            return self.send_error_page(400, "Bot name is required (max 64 chars).")
        slug = slugify(name)
        if not slug or not SLUG_RE.match(slug):
            return self.send_error_page(400, "Bot name must contain letters/digits to form a valid ID.")
        bots = load_bots()
        if slug in bots:
            return self.send_error_page(400, f"Bot ID '{html.escape(slug)}' already exists. Rename the bot.")
        slot = free_slot(bots)
        if not slot:
            return self.send_error_page(400, "No free slots. Ask Muse to add more slots via SSH.")

        # 新机器人无已有真实值：三项必填，占位符拒绝（与已修复逻辑一致）
        submitted_app_id = params.get("app_id", [""])[0].strip()
        submitted_app_secret = params.get("app_secret", [""])[0].strip()
        submitted_chat_id = params.get("chat_id", [""])[0].strip()
        for value, placeholder, field in (
            (submitted_app_id, PLACEHOLDER_APP_ID, "App ID"),
            (submitted_app_secret, PLACEHOLDER_APP_SECRET, "App Secret"),
            (submitted_chat_id, PLACEHOLDER_CHAT_ID, "Chat ID"),
        ):
            if not value:
                return self.send_error_page(400, field + " is required.")
            if value == placeholder:
                return self.send_error_page(400, field + " is still a placeholder value. Please enter a real value.")

        bot = {"name": name, "slot": slot,
               "history_allowed": "history_allowed" in params,
               "created_at": int(time.time())}
        config = {"app_id": submitted_app_id, "app_secret": submitted_app_secret,
                  "chat_id": submitted_chat_id, "poll_interval": 10}
        cfg_file = SLOTS_DIR / slot / "config" / "config.json"
        cfg_file.parent.mkdir(parents=True, exist_ok=True)
        with LOCK:
            write_json_atomic(cfg_file, config)
        bots[slug] = bot
        save_bots(bots)
        print(f"[ui] bot created: {slug} -> {slot}")
        return self.render_bot_detail_page(session, slug, bot, msg=f"Bot {name} created in {slot}.")

    def handle_bot_save(self, session, slug, bot, params):
        submitted_app_id = params.get("app_id", [""])[0].strip()
        submitted_app_secret = params.get("app_secret", [""])[0].strip()
        submitted_chat_id = params.get("chat_id", [""])[0].strip()

        cfg_file = bot_config_file(bot)
        existing_config = read_json(cfg_file, {})

        def _pick(submitted, existing, placeholder, field):
            # 空提交 = 保留已有真实值；占位符提交 = 拒绝（绝不把占位符存成"成功"）
            if submitted:
                if submitted == placeholder:
                    return None, field + " is still a placeholder value. Please enter a real value."
                return submitted, None
            if existing and existing != placeholder:
                return existing, None
            return None, field + " is required (no saved value yet)."

        app_id, err = _pick(submitted_app_id, existing_config.get("app_id", ""), PLACEHOLDER_APP_ID, "App ID")
        if err:
            return self.send_error_page(400, err)
        app_secret, err = _pick(submitted_app_secret, existing_config.get("app_secret", ""), PLACEHOLDER_APP_SECRET, "App Secret")
        if err:
            return self.send_error_page(400, err)
        chat_id, err = _pick(submitted_chat_id, existing_config.get("chat_id", ""), PLACEHOLDER_CHAT_ID, "Chat ID")
        if err:
            return self.send_error_page(400, err + " Please select a group from the dropdown or type an oc_ chat ID.")

        # 保留现有 poll_interval
        poll_interval = existing_config.get("poll_interval", 10)

        config = {
            "app_id": app_id,
            "app_secret": app_secret,
            "chat_id": chat_id,
            "poll_interval": poll_interval
        }
        with LOCK:
            write_json_atomic(cfg_file, config)
        bots = load_bots()
        if slug in bots:
            bots[slug]["history_allowed"] = "history_allowed" in params
            save_bots(bots)

        print(f"[ui] config saved: {slug}")
        return self.render_bot_detail_page(session, slug, load_bots().get(slug, bot),
                                           msg="Config saved successfully.")

    def handle_bot_delete(self, session, slug, bot, params):
        typed = params.get("confirm_name", [""])[0].strip()
        if typed != bot["name"]:
            return self.send_error_page(400, "Name mismatch. Type the exact bot name to confirm deletion.")
        slot_path = slot_dir(bot)
        # 归档 state/queues 内容（不删，留待恢复）；配置写回占位符，容器空转。
        ts = int(time.time())
        archive = slot_path / "_archive" / f"{slug}-{ts}"
        try:
            for sub in ("state", "queues"):
                src = slot_path / sub
                if src.exists():
                    dst = archive / sub
                    dst.mkdir(parents=True, exist_ok=True)
                    for child in list(src.iterdir()):
                        shutil.move(str(child), str(dst / child.name))
            (slot_path / "state").mkdir(parents=True, exist_ok=True)
            write_json_atomic(slot_path / "state" / "disabled.json",
                              {"disabled_bot": slug, "disabled_at": ts})
            for sub in ("inbox", "outbox", "sent", "failed"):
                (slot_path / "queues" / sub).mkdir(parents=True, exist_ok=True)
        except Exception as e:
            print(f"[ui] archive failed on delete {slug}: {e}")
            return self.send_error_page(500, "Archive failed, deletion aborted. Nothing was changed.")
        write_json_atomic(bot_config_file(bot), {
            "app_id": PLACEHOLDER_APP_ID,
            "app_secret": PLACEHOLDER_APP_SECRET,
            "chat_id": PLACEHOLDER_CHAT_ID,
            "poll_interval": 10,
        })
        with LOCK:
            tokens = load_tokens()
            tokens = {tid: rec for tid, rec in tokens.items() if rec.get("bot") != slug}
            save_tokens(tokens)
        bots = load_bots()
        bots.pop(slug, None)
        save_bots(bots)
        print(f"[ui] bot deleted: {slug} (archived to {archive})")
        return self.render_bots_page(session, msg=f"Bot {bot['name']} deleted. Slot {bot['slot']} is free now.")

    # ---------- API 令牌管理 ----------

    def handle_token_generate(self, session, slug, bot, rotate, params=None):
        token = "fshu_" + secrets.token_urlsafe(32)
        digest = hashlib.sha256(token.encode()).hexdigest()
        token_id = secrets.token_hex(8)
        with LOCK:
            tokens = load_tokens()
            if rotate:
                tokens = {tid: rec for tid, rec in tokens.items() if rec.get("bot") != slug}
            tokens[token_id] = {"hash": digest, "bot": slug,
                                "created_at": int(time.time()),
                                "prefix": token[:12]}
            save_tokens(tokens)
        print(f"[ui] token {'rotated' if rotate else 'generated'}: {slug} id={token_id}")
        return self.render_bot_detail_page(session, slug, bot, flash_token=(token, rotate))

    def handle_token_revoke(self, session, slug, bot, params):
        token_id = params.get("token_id", [""])[0].strip()
        with LOCK:
            tokens = load_tokens()
            rec = tokens.get(token_id)
            if not rec or rec.get("bot") != slug:
                return self.send_error_page(404, "Token not found.")
            del tokens[token_id]
            save_tokens(tokens)
        print(f"[ui] token revoked: {slug} id={token_id}")
        return self.render_bot_detail_page(session, slug, bot, msg="Token revoked.")

    # ---------- 队列 API ----------

    def _api_auth(self, slug):
        """返回 (bot记录, 403/404原因)；鉴权失败一律按 404 处理。"""
        if not SLUG_RE.match(slug):
            return None
        bots = load_bots()
        bot = bots.get(slug)
        if not bot:
            return None
        auth = self.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return None
        if token_bot(auth[7:].strip()) != slug:
            return None
        return bot

    def handle_api_get(self, path, query):
        m = re.match(r"^/api/v1/([A-Za-z0-9_-]+)/(inbox|health|settings)$", path)
        m_one = re.match(r"^/api/v1/([A-Za-z0-9_-]+)/inbox/([A-Za-z0-9_-]+)$", path)
        if m_one:
            slug, mid = m_one.group(1), m_one.group(2)
            bot = self._api_auth(slug)
            if not bot or not MSG_ID_RE.match(mid):
                return self.send_json(404, {"error": "not_found"})
            fpath = bot_queues_dir(bot) / "inbox" / f"{mid}.json"
            rec = read_json(fpath, None)
            if rec is None:
                return self.send_json(404, {"error": "not_found"})
            return self.send_json(200, rec)
        if not m:
            return self.send_json(404, {"error": "not_found"})
        slug, action = m.group(1), m.group(2)
        bot = self._api_auth(slug)
        if not bot:
            return self.send_json(404, {"error": "not_found"})
        if action == "health":
            return self.send_json(200, read_json(bot_state_dir(bot) / "health.json", {}))
        if action == "settings":
            return self.send_json(200, {
                "bot": slug,
                "name": bot.get("name", slug),
                "history_allowed": bool(bot.get("history_allowed", False)),
            })
        # inbox 列表
        inbox_dir = bot_queues_dir(bot) / "inbox"
        records = []
        if inbox_dir.exists():
            for fpath in inbox_dir.glob("*.json"):
                rec = read_json(fpath, None)
                if isinstance(rec, dict) and rec.get("message_id"):
                    records.append(rec)
        def _ct(r):
            try:
                return int(r.get("create_time") or 0)
            except (ValueError, TypeError):
                return 0
        records.sort(key=lambda r: (_ct(r), r.get("message_id", "")))
        since = query.get("since", [""])[0]
        if since:
            idx = next((i for i, r in enumerate(records) if r.get("message_id") == since), None)
            if idx is not None:
                records = records[idx + 1:]
        try:
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            limit = 50
        limit = max(1, min(200, limit))
        records = records[:limit]
        print(f"[api] inbox list: {slug} count={len(records)}")
        return self.send_json(200, {"messages": records, "count": len(records)})

    def handle_api_post(self, path, body):
        m = re.match(r"^/api/v1/([A-Za-z0-9_-]+)/outbox$", path)
        if not m:
            return self.send_json(404, {"error": "not_found"})
        slug = m.group(1)
        bot = self._api_auth(slug)
        if not bot:
            return self.send_json(404, {"error": "not_found"})
        if not check_outbox_rate(slug):
            return self.send_json(429, {"error": "rate_limited"})
        try:
            job = json.loads(body.decode("utf-8"))
        except Exception:
            return self.send_json(400, {"error": "invalid json"})
        if not isinstance(job, dict):
            return self.send_json(400, {"error": "job must be an object"})
        text = job.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 8000:
            return self.send_json(400, {"error": "text is required (1..8000 chars)"})
        mentions = job.get("mentions", [])
        if mentions is None:
            mentions = []
        if not isinstance(mentions, list):
            return self.send_json(400, {"error": "mentions must be a list"})
        clean_mentions = []
        for mm in mentions:
            if not isinstance(mm, dict):
                return self.send_json(400, {"error": "mention must be an object"})
            mid = mm.get("id", "")
            if mid != "all" and not (isinstance(mid, str) and MENTION_ID_RE.match(mid)):
                return self.send_json(400, {"error": "invalid mention id"})
            name = mm.get("name", "")
            if not isinstance(name, str) or len(name) > 100:
                return self.send_json(400, {"error": "invalid mention name"})
            clean_mentions.append({"id": mid, "name": name})
        reply_to = job.get("reply_to_message_id")
        if reply_to is not None and not (isinstance(reply_to, str) and MSG_ID_RE.match(reply_to)):
            return self.send_json(400, {"error": "invalid reply_to_message_id"})
        reply_in_thread = job.get("reply_in_thread", False)
        if not isinstance(reply_in_thread, bool):
            return self.send_json(400, {"error": "reply_in_thread must be boolean"})

        clean_job = {"text": text, "mentions": clean_mentions}
        if reply_to:
            clean_job["reply_to_message_id"] = reply_to
        if reply_in_thread:
            clean_job["reply_in_thread"] = True

        outbox_dir = bot_queues_dir(bot) / "outbox"
        outbox_dir.mkdir(parents=True, exist_ok=True)
        fname = f"job-{int(time.time())}-{secrets.token_hex(6)}.json"
        write_json_atomic(outbox_dir / fname, clean_job)
        print(f"[api] outbox queued: {slug} file={fname} len={len(text)}")
        return self.send_json(202, {"ok": True, "job": fname})

    # ---------- 渲染 ----------

    def send_redirect(self, location):
        self.send_response(302)
        self.send_header("Location", location)
        self.end_headers()

    def send_html(self, content, code=200):
        self.send_response(code)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'")
        self.end_headers()
        self.wfile.write(content.encode())

    def send_json(self, code, obj):
        data = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(data)

    def send_error_page(self, code, msg):
        content = self._page_head(f"Error {code}") + f"<h1>Error {code}</h1><p>{html.escape(str(msg))}</p><a href='/'>Back</a></body></html>"
        self.send_html(content, code)

    def render_setup_hint(self):
        content = self._page_head("Setup Required") + "<h1>Setup Required</h1><p>Please check <code>setup-token.txt</code> on the server for the setup link.</p></body></html>"
        self.send_html(content)

    def render_setup_page(self):
        token = urllib.parse.urlparse(self.path).path[7:]
        html_token = html.escape(token)
        content = self._page_head("Initial Setup") + f"""
        <h1>Set Admin Password</h1>
        <form method="POST" action="/setup/submit">
            <input type="hidden" name="token" value="{html_token}">
            <p>Password (min 8 chars):<br><input type="password" name="password" minlength="8" required></p>
            <button type="submit">Set Password</button>
        </form>
        </body></html>
        """
        self.send_html(content)

    def render_login_page(self):
        content = self._page_head("Login") + """
        <h1>Feishu Bots Platform - Login</h1>
        <form method="POST" action="/login">
            <p>Password:<br><input type="password" name="password" required></p>
            <button type="submit">Login</button>
        </form>
        </body></html>
        """
        self.send_html(content)

    def render_app_js(self):
        # 静态同步脚本（无任何数据）：每个 .chat-form 内，下拉选择 -> chat_id 输入框；
        # 提交前再同步一次。必须走独立文件：页面 CSP 禁止内联事件处理。
        js = """
document.addEventListener('DOMContentLoaded', function () {
  document.querySelectorAll('form.chat-form').forEach(function (form) {
    var sel = form.querySelector('select.chat-select');
    var input = form.querySelector('input.chat-id-input');
    if (!sel || !input) return;
    function sync() { if (sel.value) { input.value = sel.value; } }
    sel.addEventListener('change', sync);
    form.addEventListener('submit', sync);
    sync();
  });
});
"""
        self.send_response(200)
        self.send_header("Content-Type", "application/javascript; charset=utf-8")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(js.encode())

    def _page_head(self, title):
        return f"""<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width, initial-scale=1"><title>{html.escape(title)}</title>
        <style>
            * {{ box-sizing: border-box; }}
            body {{ font-family: system-ui, -apple-system, "Segoe UI", Roboto, "Helvetica Neue", Arial, sans-serif; color: #1e293b; background: #f1f5f9; max-width: 960px; margin: 0 auto; padding: 24px 20px 48px; font-size: 15px; line-height: 1.55; }}
            h1 {{ font-size: 24px; letter-spacing: -0.02em; margin: 8px 0 16px; }}
            h3 {{ font-size: 17px; margin: 20px 0 8px; }}
            a {{ color: #2563eb; text-decoration: none; }} a:hover {{ text-decoration: underline; }}
            .topnav {{ background: #1e293b; border-radius: 8px; padding: 10px 16px; margin: 0 0 20px; }}
            .topnav a {{ color: #e2e8f0; margin-right: 18px; font-weight: 500; }} .topnav a:hover {{ color: #fff; }}
            table {{ border-collapse: collapse; width: 100%; background: #fff; border-radius: 8px; overflow: hidden; box-shadow: 0 1px 3px rgba(15,23,42,.08); }}
            th {{ background: #eef2f7; font-weight: 600; }}
            th, td {{ border: 1px solid #e2e8f0; padding: 8px 10px; text-align: left; font-size: 14px; }}
            tbody tr:hover {{ background: #f8fafc; }}
            input[type=text], input[type=password], select {{ padding: 8px 10px; border: 1px solid #cbd5e1; border-radius: 6px; font-size: 14px; width: 100%; max-width: 440px; background: #fff; }}
            input[type=checkbox] {{ accent-color: #2563eb; width: 16px; height: 16px; vertical-align: middle; }}
            button {{ background: #2563eb; color: #fff; border: none; border-radius: 6px; padding: 8px 18px; font-size: 14px; font-weight: 500; cursor: pointer; }} button:hover {{ background: #1d4ed8; }}
            .btnlink {{ display: inline-block; background: #2563eb; color: #fff; border-radius: 6px; padding: 8px 18px; font-size: 14px; font-weight: 500; }} .btnlink:hover {{ background: #1d4ed8; color: #fff; text-decoration: none; }}
            form p {{ margin: 12px 0; }}
            .status {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 8px; padding: 14px 16px; margin-bottom: 20px; box-shadow: 0 1px 3px rgba(15,23,42,.08); }}
            .status p {{ margin: 6px 0; }}
            .error {{ color: #dc2626; }} .ok {{ color: #16a34a; }}
            .msg {{ background: #e0f2fe; border: 1px solid #7dd3fc; color: #0c4a6e; border-radius: 6px; padding: 10px 12px; }}
            pre {{ white-space: pre-wrap; word-break: break-all; background: #0f172a; color: #e2e8f0; padding: 12px 14px; border-radius: 8px; font-size: 14px; }}
            code {{ background: #eef2f7; padding: 2px 5px; border-radius: 4px; font-size: 13px; }}
            small {{ color: #64748b; }}
            hr {{ border: none; border-top: 1px solid #e2e8f0; margin: 24px 0; }}
            h2 {{ font-size: 19px; margin: 0 0 10px; }}
            .module {{ background: #fff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 18px 20px; margin: 22px 0; box-shadow: 0 1px 3px rgba(15,23,42,.08); }}
            .module h3 {{ margin-top: 22px; }}
            .jumpnav {{ color: #64748b; }}
            .jumpnav a {{ margin-right: 12px; }}
            .danger {{ border: 1px solid #fecaca; background: #fef2f2; border-radius: 8px; padding: 12px 14px; }}
            @media (max-width: 600px) {{ body {{ padding: 16px 12px 40px; }} th, td {{ padding: 6px; font-size: 13px; }} }}
        </style></head><body>
        <p class="topnav"><a href="/bots">Bots</a><a href="/bots/new">Add bot</a><a href="/logout">Logout</a></p>
        """

    def _bot_module(self, session, slug, bot, flash_token=None):
        config = read_json(bot_config_file(bot), {})
        health = read_json(bot_state_dir(bot) / "health.json", {})
        qdir = bot_queues_dir(bot)
        counts = {}
        for sub in ("inbox", "outbox", "sent", "failed"):
            d = qdir / sub
            counts[sub] = len(list(d.glob("*.json"))) if d.exists() else 0
        app_id = config.get("app_id", "")
        if app_id and app_id != PLACEHOLDER_APP_ID:
            masked_app_id = app_id[:6] + "..." + app_id[-4:] if len(app_id) > 10 else app_id
        else:
            masked_app_id = "Not Set"
        has_secret = "Yes" if config.get("app_secret") and config.get("app_secret") != PLACEHOLDER_APP_SECRET else "No"
        _cid = config.get("chat_id", "")
        chat_id_value = _cid if _cid and _cid != PLACEHOLDER_CHAT_ID else ""
        checked = "checked" if bot.get("history_allowed") else ""
        records = []
        inbox_dir = qdir / "inbox"
        if inbox_dir.exists():
            for fpath in inbox_dir.glob("*.json"):
                rec = read_json(fpath, None)
                if isinstance(rec, dict):
                    records.append(rec)
        def _ct(r):
            try:
                return int(r.get("create_time") or 0)
            except (ValueError, TypeError):
                return 0
        records.sort(key=_ct, reverse=True)
        inbox_rows = ""
        for rec in records[:5]:
            sender = rec.get("sender", {}) or {}
            mentions = rec.get("mentions", []) or []
            names = ", ".join(str(m.get("name", "")) for m in mentions if isinstance(m, dict))
            content_txt = json.dumps(rec.get("content", {}), ensure_ascii=False)[:150]
            ct = _ct(rec)
            ct_s = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ct // 1000)) if ct else "-"
            inbox_rows += (
                f"<tr><td>{ct_s}</td>"
                f"<td><code>{html.escape(str(rec.get('message_id', '')))}</code></td>"
                f"<td>{html.escape(str(sender.get('id', '')))} ({html.escape(str(sender.get('sender_type', '')))})</td>"
                f"<td>{html.escape(names)}</td>"
                f"<td>{html.escape(content_txt)}</td></tr>"
            )
        return f"""
        <section class="module" id="bot-{html.escape(slug)}">
        <h2>{html.escape(bot.get('name', slug))} <small>(<code>{html.escape(slug)}</code>, {html.escape(bot['slot'])})</small></h2>
        {self._flash_token_block(flash_token) if flash_token else ''}
        <div class="status">
            <p>Token OK: <span class="{'ok' if health.get('token_ok') else 'error'}">{html.escape(str(health.get('token_ok', 'Unknown')))}</span>
            &nbsp; Last Error: <span class="error">{html.escape(str(health.get('last_error', 'None')))}</span></p>
            <p>Last Poll: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(health.get('last_poll_time', 0)))}</p>
            <p>Queues: inbox={counts['inbox']} outbox={counts['outbox']} sent={counts['sent']} failed={counts['failed']}</p>
            <p>App ID: {html.escape(masked_app_id)} &nbsp; App Secret Filled: {has_secret} &nbsp; History allowed: {html.escape(str(bool(bot.get('history_allowed', False))))}</p>
        </div>
        <h3>Configuration</h3>
        <form class="chat-form" method="POST" action="/bots/{html.escape(slug)}/save">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>App ID:<br><input type="text" name="app_id" placeholder="cli_... (leave blank to keep current)"></p>
            <p>App Secret:<br><input type="password" name="app_secret" placeholder="Leave blank to keep current secret"></p>
            <p>Chat ID (Group):<br>
                <select class="chat-select">
                    <option value="">-- Select or Input Below --</option>
                    {self._chat_options(bot, config.get("chat_id", ""))}
                </select><br>
                <input type="text" class="chat-id-input" name="chat_id" value="{html.escape(chat_id_value)}" placeholder="oc_...">
            </p>
            <p><label><input type="checkbox" name="history_allowed" {checked}> Allow viewing history records</label>
            <br><small>Checked = this bot may read group history and its own Muse memory and answer history questions in the group. Default off. Checking the box for someone else's bot requires that bot owner's own consent. Private chats of other users are never accessible either way.</small></p>
            <button type="submit">Save Configuration</button>
        </form>
        {self._tokens_section(session, slug, bot)}
        <h3>Recent inbox messages (newest first, up to 5)</h3>
        <table><tr><th>Time (UTC)</th><th>Message ID</th><th>Sender</th><th>Mentions</th><th>Content</th></tr>
        {inbox_rows}</table>
        <div class="danger">
        <h3>Delete bot</h3>
        <p>Writes the placeholder config back to {html.escape(bot['slot'])} (the relay idles), revokes all API tokens, and archives state/queues. Type the exact bot name to confirm.</p>
        <form method="POST" action="/bots/{html.escape(slug)}/delete">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>Bot name:<br><input type="text" name="confirm_name" required></p>
            <button type="submit">Delete bot</button>
        </form>
        </div>
        </section>
        """

    def _add_bot_module(self, session, bots):
        slot = free_slot(bots)
        if not slot:
            return """
        <section class="module" id="add-bot">
        <h2>Add bot</h2>
        <p>No free slots. Ask Muse to add more slots via SSH.</p>
        </section>
        """
        pseudo_bot = {"slot": slot}
        return f"""
        <section class="module" id="add-bot">
        <h2>Add bot (slot: {html.escape(slot)})</h2>
        <form class="chat-form" method="POST" action="/bots/new">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>Bot name:<br><input type="text" name="name" required maxlength="64" placeholder="e.g. Secretary"></p>
            <p>App ID:<br><input type="text" name="app_id" required placeholder="cli_..."></p>
            <p>App Secret:<br><input type="password" name="app_secret" required></p>
            <p>Chat ID (Group):<br>
                <select class="chat-select">
                    <option value="">-- Select or Input Below --</option>
                    {self._chat_options(pseudo_bot, "")}
                </select><br>
                <input type="text" class="chat-id-input" name="chat_id" required placeholder="oc_...">
            </p>
            <p><label><input type="checkbox" name="history_allowed"> Allow viewing history records</label>
            <br><small>Checked = this bot may read group history and its own Muse memory and answer history questions in the group. Default off. Checking the box for someone else's bot requires that bot owner's own consent.</small></p>
            <button type="submit">Create bot</button>
        </form>
        </section>
        """

    def render_bot_detail_page(self, session, slug, bot, msg="", flash_token=None):
        content = self._page_head(f"{bot.get('name', slug)}") + f"""
        <p><a href="/bots">← All bots</a></p>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        {self._bot_module(session, slug, bot, flash_token)}
        <script src="/app.js"></script>
        </body></html>
        """
        self.send_html(content)

    def render_bots_page(self, session, msg=""):
        bots = load_bots()
        rows = ""
        for slug in sorted(bots):
            bot = bots[slug]
            config = read_json(bot_config_file(bot), {})
            health = read_json(bot_state_dir(bot) / "health.json", {})
            filled = "Yes" if config_filled(config) else "No"
            token_ok = health.get("token_ok", "Unknown")
            last_err = health.get("last_error", "")
            last_poll = health.get("last_poll_time", 0)
            last_poll_s = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(last_poll)) if last_poll else "-"
            hist = "Yes" if bot.get("history_allowed") else "No"
            rows += (
                f"<tr><td>{html.escape(bot.get('name', slug))}</td>"
                f"<td><code>{html.escape(slug)}</code></td>"
                f"<td>{html.escape(bot['slot'])}</td>"
                f"<td>{filled}</td>"
                f"<td>{html.escape(str(token_ok))}</td>"
                f"<td>{html.escape(str(last_err))}</td>"
                f"<td>{last_poll_s}</td>"
                f"<td>{hist}</td>"
                f"<td><a href='/bots/{html.escape(slug)}'>Open →</a></td></tr>"
            )
        free = [s for s in SLOT_NAMES if s not in {b["slot"] for b in bots.values()}]
        content = self._page_head("Feishu Bots") + f"""
        <h1>Feishu Bots Platform</h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        <p>Free slots: {html.escape(", ".join(free) if free else "none")} (total {len(SLOT_NAMES)})</p>
        <table><tr><th>Name</th><th>ID</th><th>Slot</th><th>Config</th><th>Token OK</th>
        <th>Last Error</th><th>Last Poll (UTC)</th><th>History</th><th></th></tr>
        {rows}</table>
        <p><a class="btnlink" href="/bots/new">Add bot</a></p>
        </body></html>
        """
        self.send_html(content)

    def _chat_options(self, bot, current_chat_id):
        chats = read_json(bot_state_dir(bot) / "chats.json", [])
        opts = ""
        for c in chats:
            if not isinstance(c, dict):
                continue
            cid = c.get("chat_id", "")
            name = c.get("name", "")
            sel = "selected" if cid and cid == current_chat_id else ""
            opts += f"<option value='{html.escape(cid)}' {sel}>{html.escape(name)} ({html.escape(cid)})</option>"
        return opts

    def render_bot_new_page(self, session, msg=""):
        bots = load_bots()
        slot = free_slot(bots)
        if not slot:
            return self.send_error_page(400, "No free slots.")
        pseudo_bot = {"slot": slot}
        content = self._page_head("Add bot") + f"""
        <h1>Add bot (slot: {html.escape(slot)})</h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        <form class="chat-form" method="POST" action="/bots/new">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>Bot name:<br><input type="text" name="name" required maxlength="64" placeholder="e.g. Secretary"></p>
            <p>App ID:<br><input type="text" name="app_id" required placeholder="cli_..."></p>
            <p>App Secret:<br><input type="password" name="app_secret" required></p>
            <p>Chat ID (Group):<br>
                <select class="chat-select">
                    <option value="">-- Select or Input Below --</option>
                    {self._chat_options(pseudo_bot, "")}
                </select><br>
                <input type="text" class="chat-id-input" name="chat_id" required placeholder="oc_...">
            </p>
            <p><label><input type="checkbox" name="history_allowed"> Allow viewing history records</label>
            <br><small>Checked = this bot may read group history and its own Muse memory and answer history questions in the group. Default off. Checking the box for someone else's bot requires that bot owner's own consent.</small></p>
            <button type="submit">Create bot</button>
        </form>
        <script src="/app.js"></script>
        </body></html>
        """
        self.send_html(content)

    def _flash_token_block(self, flash_token):
        token, rotate = flash_token
        return (f"<p><b>Token {'rotated' if rotate else 'generated'} — copy it now. "
                f"It is shown only once and will never be displayed again.</b></p>"
                f"<pre>{html.escape(token)}</pre>"
                f"<p>Send it to the bot owner privately. The owner's Muse must store it "
                f"in secure credential storage, never paste it into chat or the Feishu group.</p>")

    def _tokens_section(self, session, slug, bot):
        tokens = load_tokens()
        rows = ""
        for tid, rec in sorted(tokens.items(), key=lambda kv: kv[1].get("created_at", 0)):
            if rec.get("bot") != slug:
                continue
            created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rec.get("created_at", 0)))
            rows += (
                f"<tr><td><code>{html.escape(rec.get('prefix', ''))}...</code></td>"
                f"<td>{created}</td>"
                f"<td><form method='POST' action='/bots/{html.escape(slug)}/tokens/revoke' style='display:inline'>"
                f"<input type='hidden' name='csrf_token' value='{html.escape(session['csrf_token'])}'>"
                f"<input type='hidden' name='token_id' value='{html.escape(tid)}'>"
                f"<input type='hidden' name='return_to' value='edit'>"
                f"<button type='submit'>Revoke</button></form></td></tr>"
            )
        return f"""
        <hr>
        <h3>API tokens</h3>
        <p>Each bot has its own API token. Give the token to the bot owner's Muse privately
        (it goes into that Muse's secure credential storage, never into chat). A token only
        accesses this bot's queues.</p>
        <table><tr><th>Prefix</th><th>Created (UTC)</th><th>Action</th></tr>{rows}</table>
        <p>
        <form method="POST" action="/bots/{html.escape(slug)}/tokens/generate" style="display:inline">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <input type="hidden" name="return_to" value="edit">
            <button type="submit">Generate new token</button>
        </form>
        <form method="POST" action="/bots/{html.escape(slug)}/tokens/rotate" style="display:inline">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <input type="hidden" name="return_to" value="edit">
            <button type="submit">Rotate (revoke all old, generate new)</button>
        </form>
        </p>
        """

    # 以下单机器人分页面已废弃：两级结构（2026-11-13）后首页只列基础信息，
    # 点进机器人详情页（render_bot_detail_page + _bot_module）集中全部设置，
    # 加机器人走 render_bot_new_page。旧分页面路由一律重定向到详情页。函数
    # 保留仅作备用，不再被调用。
    def render_bot_edit_page(self, session, slug, bot, msg="", flash_token=None):
        config = read_json(bot_config_file(bot), {})
        health = read_json(bot_state_dir(bot) / "health.json", {})

        app_id = config.get("app_id", "")
        if app_id and app_id != PLACEHOLDER_APP_ID:
            masked_app_id = app_id[:6] + "..." + app_id[-4:] if len(app_id) > 10 else app_id
        else:
            masked_app_id = "Not Set"
        has_secret = "Yes" if config.get("app_secret") and config.get("app_secret") != PLACEHOLDER_APP_SECRET else "No"

        # 占位 chat_id 不预填进输入框（否则看起来像已填、提交即"成功"存占位符）
        _cid = config.get("chat_id", "")
        chat_id_value = _cid if _cid and _cid != PLACEHOLDER_CHAT_ID else ""
        checked = "checked" if bot.get("history_allowed") else ""

        content = self._page_head(f"Edit {bot.get('name', slug)}") + f"""
        <h1>Edit bot: {html.escape(bot.get('name', slug))} <small>(<code>{html.escape(slug)}</code>, {html.escape(bot['slot'])})</small></h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        {self._flash_token_block(flash_token) if flash_token else ''}
        <div class="status">
            <h3>Status</h3>
            <p>Token OK: <span class="{'ok' if health.get('token_ok') else 'error'}">{html.escape(str(health.get('token_ok', 'Unknown')))}</span></p>
            <p>Last Error: <span class="error">{html.escape(str(health.get('last_error', 'None')))}</span></p>
            <p>Last Poll: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(health.get('last_poll_time', 0)))}</p>
            <hr>
            <p>App ID: {html.escape(masked_app_id)}</p>
            <p>App Secret Filled: {has_secret}</p>
        </div>

        <form class="chat-form" method="POST" action="/bots/{html.escape(slug)}/save">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>App ID:<br><input type="text" name="app_id" placeholder="cli_... (leave blank to keep current)"></p>
            <p>App Secret:<br><input type="password" name="app_secret" placeholder="Leave blank to keep current secret"></p>
            <p>Chat ID (Group):<br>
                <select class="chat-select">
                    <option value="">-- Select or Input Below --</option>
                    {self._chat_options(bot, config.get("chat_id", ""))}
                </select><br>
                <input type="text" class="chat-id-input" name="chat_id" value="{html.escape(chat_id_value)}" placeholder="oc_...">
            </p>
            <p><label><input type="checkbox" name="history_allowed" {checked}> Allow viewing history records</label>
            <br><small>Checked = this bot may read group history and its own Muse memory and answer history questions in the group. Default off. Checking the box for someone else's bot requires that bot owner's own consent. Private chats of other users are never accessible either way.</small></p>
            <button type="submit">Save Configuration</button>
        </form>
        {self._tokens_section(session, slug, bot)}
        <script src="/app.js"></script>
        </body></html>
        """
        self.send_html(content)

    def render_bot_status_page(self, session, slug, bot, msg=""):
        health = read_json(bot_state_dir(bot) / "health.json", {})
        qdir = bot_queues_dir(bot)
        counts = {}
        for sub in ("inbox", "outbox", "sent", "failed"):
            d = qdir / sub
            counts[sub] = len(list(d.glob("*.json"))) if d.exists() else 0
        # 最近 20 条 inbox（时间倒序）
        records = []
        inbox_dir = qdir / "inbox"
        if inbox_dir.exists():
            for fpath in inbox_dir.glob("*.json"):
                rec = read_json(fpath, None)
                if isinstance(rec, dict):
                    records.append(rec)
        def _ct(r):
            try:
                return int(r.get("create_time") or 0)
            except (ValueError, TypeError):
                return 0
        records.sort(key=_ct, reverse=True)
        rows = ""
        for rec in records[:20]:
            sender = rec.get("sender", {}) or {}
            mentions = rec.get("mentions", []) or []
            names = ", ".join(str(m.get("name", "")) for m in mentions if isinstance(m, dict))
            content_txt = json.dumps(rec.get("content", {}), ensure_ascii=False)[:300]
            ct = _ct(rec)
            ct_s = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(ct // 1000)) if ct else "-"
            rows += (
                f"<tr><td>{ct_s}</td>"
                f"<td><code>{html.escape(str(rec.get('message_id', '')))}</code></td>"
                f"<td>{html.escape(str(sender.get('id', '')))} ({html.escape(str(sender.get('sender_type', '')))})</td>"
                f"<td>{html.escape(names)}</td>"
                f"<td>{html.escape(content_txt)}</td></tr>"
            )
        content = self._page_head(f"Status {bot.get('name', slug)}") + f"""
        <h1>Status: {html.escape(bot.get('name', slug))}</h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        <div class="status">
            <p>Token OK: {html.escape(str(health.get('token_ok', 'Unknown')))}</p>
            <p>Last Error: {html.escape(str(health.get('last_error', 'None')))}</p>
            <p>Last Poll: {time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(health.get('last_poll_time', 0)))}</p>
            <p>Queues: inbox={counts['inbox']} outbox={counts['outbox']} sent={counts['sent']} failed={counts['failed']}</p>
            <p>History allowed: {html.escape(str(bool(bot.get('history_allowed', False))))}</p>
        </div>
        <h3>Recent inbox messages (newest first)</h3>
        <table><tr><th>Time (UTC)</th><th>Message ID</th><th>Sender</th><th>Mentions</th><th>Content</th></tr>
        {rows}</table>
        </body></html>
        """
        self.send_html(content)

    def render_bot_tokens_page(self, session, slug, bot, msg=""):
        tokens = load_tokens()
        rows = ""
        for tid, rec in sorted(tokens.items(), key=lambda kv: kv[1].get("created_at", 0)):
            if rec.get("bot") != slug:
                continue
            created = time.strftime('%Y-%m-%d %H:%M:%S', time.localtime(rec.get("created_at", 0)))
            rows += (
                f"<tr><td><code>{html.escape(rec.get('prefix', ''))}...</code></td>"
                f"<td>{created}</td>"
                f"<td><form method='POST' action='/bots/{html.escape(slug)}/tokens/revoke' style='display:inline'>"
                f"<input type='hidden' name='csrf_token' value='{html.escape(session['csrf_token'])}'>"
                f"<input type='hidden' name='token_id' value='{html.escape(tid)}'>"
                f"<button type='submit'>Revoke</button></form></td></tr>"
            )
        content = self._page_head(f"Tokens {bot.get('name', slug)}") + f"""
        <h1>API tokens: {html.escape(bot.get('name', slug))}</h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        <p>Each bot has its own API token. Give the token to the bot owner's Muse privately
        (it goes into that Muse's secure credential storage, never into chat). A token only
        accesses this bot's queues.</p>
        <table><tr><th>Prefix</th><th>Created (UTC)</th><th>Action</th></tr>{rows}</table>
        <p>
        <form method="POST" action="/bots/{html.escape(slug)}/tokens/generate" style="display:inline">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <button type="submit">Generate new token</button>
        </form>
        <form method="POST" action="/bots/{html.escape(slug)}/tokens/rotate" style="display:inline">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <button type="submit">Rotate (revoke all old, generate new)</button>
        </form>
        </p>
        </body></html>
        """
        self.send_html(content)

    def render_token_once_page(self, session, slug, bot, token, rotate):
        content = self._page_head("Token generated") + f"""
        <h1>Token {'rotated' if rotate else 'generated'}: {html.escape(bot.get('name', slug))}</h1>
        <p><b>Copy it now. It is shown only once and will never be displayed again.</b></p>
        <pre>{html.escape(token)}</pre>
        <p>Send it to the bot owner privately. The owner's Muse must store it in secure
        credential storage, never paste it into chat or the Feishu group.</p>
        <p><a href="/bots/{html.escape(slug)}/tokens">Back to tokens</a></p>
        </body></html>
        """
        self.send_html(content)

    def render_bot_delete_page(self, session, slug, bot, msg=""):
        content = self._page_head(f"Delete {bot.get('name', slug)}") + f"""
        <h1>Delete bot: {html.escape(bot.get('name', slug))}</h1>
        {f'<p class="msg">{html.escape(msg)}</p>' if msg else ''}
        <p><b>Warning:</b> this writes the placeholder config back to {html.escape(bot['slot'])}
        (the relay idles), revokes all API tokens for this bot, and archives the slot's
        state/queues. The slot becomes free for another bot. Type the exact bot name to confirm.</p>
        <form method="POST" action="/bots/{html.escape(slug)}/delete">
            <input type="hidden" name="csrf_token" value="{html.escape(session['csrf_token'])}">
            <p>Bot name:<br><input type="text" name="confirm_name" required></p>
            <button type="submit">Delete bot</button>
        </form>
        </body></html>
        """
        self.send_html(content)


class ThreadingServer(socketserver.ThreadingMixIn, socketserver.TCPServer):
    daemon_threads = True
    allow_reuse_address = True


# ============== 启动与初始化 ==============

def init_setup_token():
    if AUTH_FILE.exists():
        return
    if not SETUP_TOKEN_FILE.exists():
        token = secrets.token_urlsafe(32)
        h = hashlib.sha256(token.encode()).hexdigest()
        expiry = time.time() + 7200  # 2h
        write_json_atomic(SETUP_TOKEN_FILE, {"hash": h, "expiry": expiry})

        # 写给用户看的明文令牌
        try:
            SETUP_TOKEN_DIR.mkdir(parents=True, exist_ok=True)
            with open(SETUP_TOKEN_TEXT, "w") as f:
                f.write(token)
            os.chmod(SETUP_TOKEN_TEXT, 0o600)
        except Exception:
            pass
        print("[ui] setup token generated")


if __name__ == "__main__":
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if SESSION_FILE.exists():
        try:
            SESSION_FILE.unlink()
        except Exception:
            pass
    ensure_bots()
    init_setup_token()
    server = ThreadingServer(("0.0.0.0", UI_PORT), PlatformHandler)
    print(f"Platform UI starting on 0.0.0.0:{UI_PORT}")
    server.serve_forever()
