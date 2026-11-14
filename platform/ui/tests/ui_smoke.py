#!/usr/bin/env python3
"""Local smoke test for the two-level platform UI (temp dirs, no prod data).

Run: python3 tests/ui_smoke.py   (from platform-ui-src/)
"""
import json, os, re, shutil, socket, subprocess, sys, tempfile, time, urllib.error, urllib.parse, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "app.py")
BASE = tempfile.mkdtemp(prefix="ui-smoke-")
DATA = os.path.join(BASE, "data"); SLOTS = os.path.join(BASE, "slots"); SETUPD = os.path.join(BASE, "setup")
for d in (DATA, SLOTS, SETUPD):
    os.makedirs(d)
for n in range(1, 6):
    for sub in ("config", "state", "queues/inbox", "queues/outbox", "queues/sent", "queues/failed"):
        os.makedirs(os.path.join(SLOTS, f"slot{n}", sub))
PORT = 18799
env = dict(os.environ, DATA_DIR=DATA, SLOTS_DIR=SLOTS, SETUP_TOKEN_DIR=SETUPD, UI_PORT=str(PORT))
proc = subprocess.Popen([sys.executable, APP], env=env, stdout=subprocess.PIPE,
                        stderr=subprocess.STDOUT, text=True)
results = []
def check(name, ok, extra=""):
    results.append((name, ok))
    print(("PASS " if ok else "FAIL ") + name + ((" :: " + extra) if extra and not ok else ""))

def wait_port():
    for _ in range(50):
        try:
            socket.create_connection(("127.0.0.1", PORT), 0.5).close()
            return True
        except OSError:
            time.sleep(0.2)
    return False

class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None

op = urllib.request.build_opener()
op_nr = urllib.request.build_opener(NoRedirect())
SESS = {"cookie": None}

def _hdrs(headers):
    h = dict(headers or {})
    if SESS["cookie"]:
        h["Cookie"] = SESS["cookie"]
    return h

def get(path, opener=op, headers=None):
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", headers=_hdrs(headers))
    try:
        r = opener.open(req, timeout=10)
        return r.status, r.headers, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")

def post(path, fields, headers=None, opener=op):
    data = urllib.parse.urlencode(fields).encode()
    req = urllib.request.Request(f"http://127.0.0.1:{PORT}{path}", data=data, headers=_hdrs(headers),
                                 method="POST")
    try:
        r = opener.open(req, timeout=10)
        return r.status, r.headers, r.read().decode(errors="replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers, e.read().decode(errors="replace")

def csrf(html):
    m = re.search(r'name="csrf_token" value="([^"]+)"', html)
    return m.group(1) if m else ""

try:
    assert wait_port(), "server did not start"
    # 1. login page styled
    s, h, b = get("/login")
    check("login 200 + styled", s == 200 and "topnav" in b and "system-ui" in b, f"status={s}")
    # 2. setup
    raw = open(os.path.join(SETUPD, "setup-token.txt")).read().strip()
    token = raw.rsplit("/", 1)[-1] if "://" in raw else raw
    s, h, b = post("/setup/submit", {"token": token, "password": "TestPass123"}, opener=op_nr)
    check("setup submit -> 302 login", s in (301, 302) and h.get("Location") == "/login", f"status={s} loc={h.get('Location')}")
    # 3. login (capture Secure cookie manually: jar won't send it over plain http)
    s, h, b = post("/login", {"password": "TestPass123"}, opener=op_nr)
    sc = h.get("Set-Cookie", "")
    m = re.search(r"session=([^;]+)", sc)
    if m:
        SESS["cookie"] = "session=" + m.group(1)
    check("login sets session cookie", s in (301, 302) and bool(m), f"status={s} cookie={bool(m)}")
    s, h, b = get("/bots")
    check("login lands on bots page", s == 200 and "Feishu Bots Platform" in b, f"status={s}")
    # 4. overview homepage: basic info only, no modules
    check("overview: table + links, no modules", s == 200 and 'id="bot-' not in b and "/bots/president" in b
          and "Free slots" in b and "topnav" in b and "viewport" in b, f"status={s}")
    # 5. /bots/new is a real form page
    s, h, b = get("/bots/new")
    check("/bots/new renders add form", s == 200 and "Create bot" in b and "chat-select" in b, f"status={s}")
    # 6. create bot -> detail page
    c = csrf(b)
    s, h, b = post("/bots/new", {"csrf_token": c, "name": "TestBot", "app_id": "cli_test123",
                                 "app_secret": "sec_test", "chat_id": "oc_test123"})
    check("create bot renders detail page", s == 200 and 'id="bot-testbot"' in b and "API tokens" in b
          and "Recent inbox" in b and "Delete bot" in b and "Configuration" in b and "All bots" in b, f"status={s}")
    # 6b. detail page directly
    s, h, b = get("/bots/testbot")
    check("GET detail page 200", s == 200 and 'id="bot-testbot"' in b and "API tokens" in b, f"status={s}")
    # 7. legacy routes redirect to detail page
    s, h, b = get("/bots/testbot/edit", opener=op_nr)
    check("legacy /edit redirects to detail", s in (301, 302) and h.get("Location", "").endswith("/bots/testbot"), f"status={s} loc={h.get('Location')}")
    s, h, b = get("/bots/testbot/tokens", opener=op_nr)
    check("legacy /tokens redirects to detail", s in (301, 302) and h.get("Location", "").endswith("/bots/testbot"), f"status={s} loc={h.get('Location')}")
    s, h, b = get("/bots/nonexistent", opener=op_nr)
    check("unknown slug -> 404", s == 404, f"status={s}")
    # 8. save with blanks preserves values (csrf from detail page)
    s, h, b = get("/bots/testbot")
    s, h, b = post("/bots/testbot/save", {"csrf_token": csrf(b), "app_id": "", "app_secret": "",
                                          "chat_id": "oc_test123", "history_allowed": "on"})
    bots = json.load(open(os.path.join(DATA, "bots.json")))
    slot = bots["testbot"]["slot"]
    cfg = json.load(open(os.path.join(SLOTS, slot, "config", "config.json")))
    check("save keeps blank fields + msg", s == 200 and "Config saved successfully." in b
          and cfg.get("app_id") == "cli_test123" and cfg.get("app_secret") == "sec_test", f"status={s} cfg={cfg}")
    # 9. generate token -> flash inside detail page
    s, h, b = get("/bots/testbot")
    s, h, b = post("/bots/testbot/tokens/generate", {"csrf_token": csrf(b)})
    m = re.search(r"<pre>(fshu_[^<]+)</pre>", b)
    check("generate shows token once in detail", s == 200 and m and "shown only once" in b
          and 'id="bot-testbot"' in b, f"status={s}")
    real_token = m.group(1) if m else ""
    # 10. one-time: GET detail page must not contain the token
    s, h, b = get("/bots/testbot")
    check("token gone on reload, prefix row present", real_token not in b and "fshu_" in b, "")
    # 11. API positive with real token
    s, h, b = get("/api/v1/testbot/health", headers={"Authorization": f"Bearer {real_token}"})
    check("API health with real token 200", s == 200, f"status={s} body={b[:80]}")
    s, h, b = get("/api/v1/testbot/inbox", headers={"Authorization": f"Bearer {real_token}"})
    check("API inbox with real token 200", s == 200, f"status={s} body={b[:80]}")
    # 11b. sent endpoint: seed fixtures, list/single/pagination/auth
    bots = json.load(open(os.path.join(DATA, "bots.json")))
    slot = bots["testbot"]["slot"]
    sent_dir = os.path.join(SLOTS, slot, "queues", "sent")
    fix1 = {"message_id": "om_sent1", "sent_at": 1000,
            "original": {"text": "first reply", "mentions": [], "reply_to_message_id": "om_q1"},
            "response": {"code": 0, "data": {"secret": "must-not-leak"}}}
    fix2 = {"message_id": "om_sent2", "sent_at": 2000,
            "original": {"text": "second reply", "mentions": [{"id": "ou_x", "name": ""}],
                         "reply_to_message_id": None},
            "response": {"code": 0}}
    json.dump(fix1, open(os.path.join(sent_dir, "job-1000-a.json"), "w"))
    json.dump(fix2, open(os.path.join(sent_dir, "job-2000-b.json"), "w"))
    s, h, b = get("/api/v1/testbot/sent", headers={"Authorization": f"Bearer {real_token}"})
    d = json.loads(b) if s == 200 else {}
    check("API sent list 200 + shaped, no response blob", s == 200 and d.get("count") == 2
          and d["messages"][0]["message_id"] == "om_sent1"
          and d["messages"][1]["text"] == "second reply"
          and "response" not in b and "must-not-leak" not in b, f"status={s} body={b[:120]}")
    s, h, b = get("/api/v1/testbot/sent?since=om_sent1&limit=1", headers={"Authorization": f"Bearer {real_token}"})
    d = json.loads(b) if s == 200 else {}
    check("API sent since+limit", s == 200 and d.get("count") == 1
          and d["messages"][0]["message_id"] == "om_sent2", f"status={s} body={b[:80]}")
    s, h, b = get("/api/v1/testbot/sent/om_sent1", headers={"Authorization": f"Bearer {real_token}"})
    d = json.loads(b) if s == 200 else {}
    check("API sent single 200", s == 200 and d.get("message_id") == "om_sent1"
          and d.get("reply_to_message_id") == "om_q1", f"status={s} body={b[:80]}")
    s, h, b = get("/api/v1/testbot/sent/om_nope", headers={"Authorization": f"Bearer {real_token}"})
    check("API sent unknown -> 404", s == 404, f"status={s}")
    s, h, b = get("/api/v1/otherbot/sent", headers={"Authorization": f"Bearer {real_token}"})
    check("API sent wrong bot -> 404", s == 404, f"status={s}")
    s, h, b = get("/api/v1/testbot/sent")
    check("API sent no token -> 404", s == 404, f"status={s}")
    s, h, b = get("/api/v1/otherbot/health", headers={"Authorization": f"Bearer {real_token}"})
    check("API wrong bot -> 404", s == 404, f"status={s}")
    # 12. revoke
    s, h, b = get("/bots/testbot")
    m2 = re.search(r"tokens/revoke'[^>]*>.*?name='token_id' value='([0-9a-f]+)'", b, re.S)
    tid = m2.group(1) if m2 else ""
    s, h, b = post("/bots/testbot/tokens/revoke", {"csrf_token": csrf(b), "token_id": tid})
    s2, _, _ = get("/api/v1/testbot/health", headers={"Authorization": f"Bearer {real_token}"})
    check("revoke + token dead", s == 200 and "Token revoked." in b and s2 == 404, f"status={s} api={s2} tid={tid}")
    # 13. API no-token -> 404
    s, h, b = get("/api/v1/testbot/health")
    check("API no token -> 404", s == 404, f"status={s}")
    # 14. app.js multi-form
    s, h, b = get("/app.js")
    check("app.js serves multi-form sync", s == 200 and "querySelectorAll('form.chat-form')" in b, f"status={s}")
    # 15. delete -> back to overview
    s, h, b = get("/bots/testbot")
    s, h, b = post("/bots/testbot/delete", {"csrf_token": csrf(b), "confirm_name": "TestBot"})
    check("delete lands on overview", s == 200 and 'id="bot-testbot"' not in b and "Feishu Bots Platform" in b, f"status={s}")
    s, h, b = get("/bots")
    check("bot gone from overview", "/bots/testbot" not in b, "")
finally:
    proc.terminate()
    try:
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
    shutil.rmtree(BASE, ignore_errors=True)

failed = [n for n, ok in results if not ok]
print(f"\n{len(results) - len(failed)}/{len(results)} passed")
if failed:
    print("FAILED:", failed)
    sys.exit(1)
