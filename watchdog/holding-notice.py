#!/usr/bin/env python3
"""兜底提醒：Trump 的 Muse 侧（watcher/VM）停摆时，@Trump 的消息长
时间无人回复，VPS 上的这个脚本替 Trump 在群里发一条不带引用的
独立提醒，避免群里长时间静默。

设计要点：
- 只读队列目录，不碰 relay 配置与容器。
- 提醒不带 reply_to_message_id：Muse 恢复后唤醒会话的去重按
  reply_to 查 sent/，独立提醒不会吞掉正式回复。
- 发件人是真人才 @，发件人是机器人时不带 mentions（防互相叫醒）。
- 提醒文件经 root 写进 outbox 后必须 chown 1000:1000 + chmod 644，
  否则容器内 uid 1000 的 relay 读不到（2026-11-13 实踩）。
- 幂等：已提醒过的 message_id 记入 state 文件，不重复提醒；一次
  运行最多发一条提醒，覆盖本轮全部积压消息。
- 多机器人：每个 slot 跑一个实例，传各自的队列目录与本 slot 视角
  的机器人 ID（--bot-open-id/--bot-app-id），state 文件各用各的。
"""
import argparse
import glob
import json
import os
import sys
import time
import uuid

# 部署时填本机器人视角的真实 ID（从本 slot 的 inbox 记录里实测，
# open_id 因 app 而异，绝不用另一机器人视角的 ID；多机器人时用
# --bot-open-id/--bot-app-id 给每个实例传各自的 ID）
DEFAULT_BOT_OPEN_ID = "ou_xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"  # 本机器人 open_id
DEFAULT_BOT_APP_ID = "cli_xxxxxxxxxxxxxxxx"  # 本机器人 app_id，跳过自身消息
NOTICE_TEXT = "我这边暂时没法回复，消息已收到，恢复后会第一时间回复。"


def load_json(path):
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)


def atomic_write_json(path, obj, mode=0o600):
    tmp = "%s.tmp-%s" % (path, uuid.uuid4().hex[:8])
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.chmod(tmp, mode)
    os.replace(tmp, path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--inbox", default="/opt/docker/feishu-relay/queues/inbox")
    ap.add_argument("--outbox", default="/opt/docker/feishu-relay/queues/outbox")
    ap.add_argument("--sent", default="/opt/docker/feishu-relay/queues/sent")
    ap.add_argument("--state", default="/opt/docker/feishu-relay/state/holding-notices.json")
    ap.add_argument("--bot-open-id", default=DEFAULT_BOT_OPEN_ID,
                    help="本 slot 视角的机器人 open_id（mentions 匹配用）")
    ap.add_argument("--bot-app-id", default=DEFAULT_BOT_APP_ID,
                    help="本机器人 app_id（跳过自身消息用）")
    ap.add_argument("--threshold", type=int, default=600, help="超过多少秒未回复则提醒")
    ap.add_argument("--max-age", type=int, default=21600, help="只看最近多少秒内的消息")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    now = time.time()

    replied = set()
    for p in glob.glob(os.path.join(args.sent, "*.json")):
        try:
            rid = load_json(p).get("original", {}).get("reply_to_message_id")
        except Exception:
            continue
        if rid:
            replied.add(rid)

    pending = set()
    for p in glob.glob(os.path.join(args.outbox, "*.json")):
        try:
            rid = load_json(p).get("reply_to_message_id")
        except Exception:
            continue
        if rid:
            pending.add(rid)

    noticed = []
    if os.path.exists(args.state):
        try:
            noticed = load_json(args.state)
            if not isinstance(noticed, list):
                noticed = []
        except Exception:
            noticed = []
    noticed_set = set(noticed)

    candidates = []  # (create_time_s, message_id, sender_id, sender_id_type)
    for p in sorted(glob.glob(os.path.join(args.inbox, "*.json"))):
        try:
            r = load_json(p)
        except Exception:
            continue
        mid = r.get("message_id") or os.path.basename(p)[:-5]
        if not mid or mid in replied or mid in pending or mid in noticed_set:
            continue
        sender = r.get("sender") or {}
        if not sender.get("id") or sender.get("id") == args.bot_app_id:
            continue
        mentions = r.get("mentions") or []
        at = any(isinstance(m, dict) and m.get("id") == args.bot_open_id for m in mentions)
        if not at:
            continue
        try:
            ct = int(str(r.get("create_time", "0"))) / 1000.0
        except ValueError:
            continue
        age = now - ct
        if args.threshold < age < args.max_age:
            candidates.append((ct, mid, sender["id"], sender.get("id_type", "")))

    if not candidates:
        return 0

    candidates.sort()
    humans = []
    for _, _, sid, id_type in candidates:
        if id_type == "open_id" and sid not in humans:
            humans.append(sid)
    mentions = [{"id": sid, "name": ""} for sid in humans[:3]]
    job = {"text": NOTICE_TEXT, "mentions": mentions}

    if args.dry_run:
        print("[dry-run] would notify for %d message(s): %s" % (
            len(candidates), [c[1] for c in candidates]))
        print("[dry-run] job: %s" % json.dumps(job, ensure_ascii=False))
        return 0

    fname = "holding-%d-%s.json" % (int(now), uuid.uuid4().hex[:6])
    path = os.path.join(args.outbox, fname)
    atomic_write_json(path, job, mode=0o644)
    try:
        os.chown(path, 1000, 1000)
    except OSError as e:
        os.unlink(path)
        print("[holding-notice] chown failed, job removed: %s" % e, file=sys.stderr)
        return 1
    noticed.extend(c[1] for c in candidates)
    atomic_write_json(args.state, noticed[-500:])
    print("[holding-notice] notified for %d message(s), job %s" % (len(candidates), fname))
    return 0


if __name__ == "__main__":
    sys.exit(main())
