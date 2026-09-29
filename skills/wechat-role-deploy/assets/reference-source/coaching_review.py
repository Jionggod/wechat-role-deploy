"""每日复盘证据打包：只读桥接账本，不联系手机、不调用模型。"""

import argparse
from contextlib import closing
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path
import sqlite3

from bridge_state import atomic_json, now_cst
from timeline import local

ROOT = Path(__file__).resolve().parent


def window(now: datetime) -> tuple[datetime, datetime]:
    """Return the most recently closed half-open 23:30 window."""
    now = local(now)
    end = now.replace(hour=23, minute=30, second=0, microsecond=0)
    if end > now:
        end -= timedelta(days=1)
    return end-timedelta(days=1), end


def normalize(rows: list[dict], start: datetime, end: datetime) -> tuple[list[dict], list[str]]:
    """Preserve real repeats; exclude drafts and separate inferred old merges."""
    messages, notes = [], []
    received = {row["id"]: row for row in rows if row["status"] == "received"}
    sent_ids = {row["id"] for row in rows if row["status"] == "sent"}
    previous = None
    seen_media = set()
    for row in sorted(rows, key=lambda r: (r["at"], r.get("seq", 0))):
        at, status = local(row["at"]), row["status"]
        if status == "received":
            media = [item for item in row.get("incoming_media", []) if item["id"] not in seen_media]
            seen_media.update(item["id"] for item in media)
            text = row.get("delta_user", row["user"])
            if "delta_user" not in row and previous and previous["id"] not in sent_ids:
                prefix = previous["user"]+"\n"
                if row["user"].startswith(prefix) and 0 <= (at-local(previous["at"])).total_seconds() <= 300:
                    text = row["user"][len(prefix):]
                    if start <= at < end:
                        notes.append(f"{row['id']}：旧版累计收件按连续前缀推定拆分，原始批次仍保留，不能作精确时间证据")
            previous = row
            if start <= at < end:
                messages.append({"id": row["id"]+":received", "turn_id": row["id"], "at": row["at"],
                                 "speaker": "user", "text": text, "time_basis": "脚本观察时间",
                                 "incoming_media": media,
                                 "supersedes": row.get("supersedes"), "source_status": status})
        elif status == "sent":
            previous = None
            if start <= at < end:
                if row["id"] not in received and row.get("user") and not row.get("proactive"):
                    notes.append(f"{row['id']}：缺独立收件记录；用户文字仅来自已发送事务，不能确定用户原发送时间")
                    messages.append({"id": row["id"]+":input-context", "turn_id": row["id"], "at": row["at"],
                                     "speaker": "user", "text": row["user"], "time_basis": "原时间未知，仅作该回复的上下文"})
                messages.append({"id": row["id"]+":sent", "turn_id": row["id"], "at": row["at"], "speaker": "ai",
                                 "text": row["reply"], "time_basis": "发送确认时间", "source_status": status,
                                 "prior_window_input": row.get("user", "") if row["id"] in received and local(received[row["id"]]["at"]) < start else "",
                                 "event_ids": row.get("event_ids", []), "scene_ids": row.get("scene_ids", []),
                                 "media_id": row.get("media_id", ""), "media_understanding": row.get("media_understanding", []),
                                 "incoming_media": row.get("incoming_media", []), "proactive": row.get("proactive", False)})
        elif status in {"sending", "uncertain", "partial"} and start <= at < end:
            notes.append(f"{row['id']}：{status}，不当作已成功发送")
    return messages, notes


def prepare(root: Path = ROOT, now: datetime | None = None) -> dict:
    """Write a complete local evidence package, reusing a finished report."""
    root, now = Path(root), local(now or now_cst())
    start, end = window(now)
    config_path = root/"config.json"
    if config_path.exists():
        config = json.loads(config_path.read_text(encoding="utf-8"))
        if config.get("coaching_effective_at") and end < local(config["coaching_effective_at"]):
            return {"status":"not_due","reason":"首个复盘窗口尚未闭合","next_window_end":(end+timedelta(days=1)).isoformat()}
    folder = root/"沟通复盘"/end.strftime("%Y/%m/%d")
    report = folder/"复盘.md"
    if report.exists() and report.stat().st_size:
        return {"status": "already_reviewed", "window_end": end.isoformat(), "report_path": str(report)}
    manifest = folder/"manifest.json"
    if manifest.exists():
        return json.loads(manifest.read_text(encoding="utf-8"))
    path = root/"runtime/life-v3.sqlite"
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro", uri=True)) as db:
        rows = [{**json.loads(body), "seq": seq} for seq, body in db.execute(
            "SELECT seq,body FROM dialogues WHERE at<? ORDER BY at,seq", (end.isoformat(),))]
        initialized = db.execute("SELECT value FROM metadata WHERE key='initialized_at'").fetchone()
        scene_versions = [json.loads(body) for (body,) in db.execute(
            "SELECT body FROM event_versions WHERE recorded_at<? ORDER BY seq", (end.isoformat(),))]
    messages, notes = normalize(rows, start, end)
    latest = {event["id"]: event for event in scene_versions}
    scenes = [event["_coaching"] for event in latest.values() if event.get("_coaching") and not event.get("cancelled")
              and local(event["start"]) < end and local(event["end"]) > start]
    if initialized and local(initialized[0]) > start:
        notes.append("账本在窗口开始后才初始化，不能视为完整一天")
    pending_path = root/"runtime/state.json"
    if pending_path.exists():
        pending = json.loads(pending_path.read_text(encoding="utf-8")).get("pending")
        if pending and pending.get("status") in {"sending", "uncertain"}:
            notes.append(f"当前队列存在未确认发送：{pending['id']}；不补发、不当作已说出")
    folder.mkdir(parents=True, exist_ok=True)
    artifacts = []
    def save(name: str, raw: str) -> Path:
        target = folder/name
        target.write_text(raw, encoding="utf-8")
        data = raw.encode("utf-8")
        artifacts.append({"path": str(target), "bytes": len(data), "sha256": hashlib.sha256(data).hexdigest()})
        return target
    raw = "".join(json.dumps(message, ensure_ascii=False)+"\n" for message in messages)
    save("对话证据.jsonl", raw)
    save("原始批次.jsonl", "".join(json.dumps(row, ensure_ascii=False)+"\n" for row in rows if start <= local(row["at"]) < end))
    save("设计情境.json", json.dumps(scenes, ensure_ascii=False, indent=2))
    # 分块只为有限读取；完整内容不丢弃，大单条会分成连续文本片段。
    chunks = [raw[index:index+12000] for index in range(0, len(raw), 12000)]
    chunk_paths = [str(save(f"片段-{index+1:03}.txt", chunk)) for index, chunk in enumerate(chunks)]
    history = sorted((root/"沟通复盘").glob("*/*/*/复盘.md"))
    history = [str(p) for p in history if p.parent < folder][-7:]
    summary = {"status": "ready", "window_start": start.isoformat(), "window_end": end.isoformat(),
               "timezone": "Asia/Shanghai", "prepared_at": now.isoformat(),
               "delay_seconds": max(0, (now-end).total_seconds()), "count": len(messages),
               "speaker_mapping": {"user": "已绑定用户/YOUR_CONTACT_WECHAT_ID", "ai": "AIAI角色，不是历史真人"},
               "time_notice": "观察/发送确认时间不等于每条微信原始时间；无秒级回复延迟推断",
               "notes": notes, "chunks": chunk_paths, "scenes_path": str(folder/"设计情境.json"),
               "recent_reports": history, "report_path": str(report), "manifest_path": str(manifest),
               "artifacts": artifacts, "model_calls": 0}
    atomic_json(manifest, summary)
    return summary


def main() -> None:
    """Prepare evidence or change the opt-out flag without operating WeChat."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["prepare", "pause", "resume"])
    parser.add_argument("--root", type=Path, default=ROOT)
    parser.add_argument("--now", help="带时区的验收时刻；常规调度不传")
    args = parser.parse_args()
    now = local(args.now) if args.now else now_cst()
    if args.action == "prepare":
        from audit import Audit
        with Audit(args.root/"evidence/coaching", "review_prepare", {"at": now.isoformat()}) as audit:
            result = prepare(args.root, now)
            audit.retain("manifest.json", result)
    else:
        from coaching_scenes import control
        from timeline import Timeline
        result = control(Timeline(args.root/"runtime/life-v3.sqlite", now),
                         "暂停压力测试" if args.action == "pause" else "恢复压力测试", now)
    print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
