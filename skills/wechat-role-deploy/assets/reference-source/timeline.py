"""独立的虚构事件版本账本与实际AI对话账本。"""

from contextlib import contextmanager
from datetime import datetime, timedelta
import json
import msvcrt
from pathlib import Path
import sqlite3

from bridge_state import CST, atomic_json


def local(value):
    """Require an aware timestamp and normalize it before comparison."""
    result = datetime.fromisoformat(value) if isinstance(value, str) else value
    if result.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return result.astimezone(CST)


class Timeline:
    """Append-only revisions; historical archives are never opened for writing."""

    def __init__(self, path: Path, now: datetime):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS metadata(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS event_versions(
                    seq INTEGER PRIMARY KEY,id TEXT NOT NULL,recorded_at TEXT NOT NULL,body TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS dialogues(
                    seq INTEGER PRIMARY KEY,id TEXT NOT NULL,status TEXT NOT NULL,at TEXT NOT NULL,body TEXT NOT NULL,
                    UNIQUE(id,status));
                CREATE TABLE IF NOT EXISTS loop_closures(id TEXT PRIMARY KEY,at TEXT NOT NULL);
            """)
            db.execute("INSERT OR IGNORE INTO metadata VALUES ('initialized_at',?)", (local(now).isoformat(),))

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @contextmanager
    def schedule_lock(self):
        """Serialize schedule extension across the bridge and local clock."""
        with self.path.with_suffix(".schedule.lock").open("a+b") as handle:
            handle.seek(0, 2)
            if handle.tell() == 0:
                handle.write(b"0")
                handle.flush()
            handle.seek(0)
            try:
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            except OSError as exc:
                raise RuntimeError("日程正在更新，本次稍后再试") from exc
            try:
                yield
            finally:
                handle.seek(0)
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)

    @staticmethod
    def _latest(db, now):
        versions = {}
        for row in db.execute("SELECT body FROM event_versions WHERE recorded_at<=? ORDER BY seq", (local(now).isoformat(),)):
            item = json.loads(row[0])
            versions[item["id"]] = item
        return versions

    @staticmethod
    def _validate(items):
        ordered = sorted(items, key=lambda e: e["start"])
        for event in ordered:
            if local(event["end"]) <= local(event["start"]):
                raise ValueError("事件结束时间必须晚于开始时间")
            if not all(event.get(key) for key in ("id", "kind", "description", "origin", "destination", "place", "city", "basis")):
                raise ValueError("事件缺少地点、动因或内容")
        for first, second in zip(ordered, ordered[1:]):
            if first["end"] > second["start"]:
                raise ValueError("事件时间重叠")
            if first["end"] == second["start"] and first["destination"] != second["origin"]:
                raise ValueError("相邻事件地点不连续，需要交通事件")

    def plan(self, events: list[dict], now: datetime):
        """Persist a new plan atomically; identical IDs cannot be silently rewritten."""
        return self.replace_future([], events, now, "新增计划")

    def replace_future(self, ids: list[str], events: list[dict], now: datetime, reason: str):
        """Cancel future versions and append replacements in one transaction."""
        now = local(now)
        with self.db() as db:
            db.execute("BEGIN IMMEDIATE")
            latest = self._latest(db, now)
            originals = []
            for key in ids:
                old = latest.get(key)
                if not old or old.get("cancelled"):
                    raise ValueError("找不到待修改事件")
                if local(old["start"]) < now:
                    raise ValueError("已开始事件不能被改写")
                originals.append(old)
            prepared = []
            for raw in events:
                item = {**raw, "start": local(raw["start"]).isoformat(), "end": local(raw["end"]).isoformat(),
                        "recorded_at": now.isoformat(), "truth_layer": "fictional_life", "timezone": "Asia/Shanghai",
                        "revision_reason": reason, "cancelled": False}
                if local(item["start"]) < now:
                    raise ValueError("不能回填新增过去经历")
                if item["id"] in latest and item["id"] not in ids:
                    raise ValueError("事件ID已存在，使用显式修订")
                prepared.append(item)
            if len({e["id"] for e in prepared}) != len(prepared):
                raise ValueError("事件ID重复")
            remaining = [e for key, e in latest.items() if key not in ids and not e.get("cancelled")]
            self._validate(remaining+prepared)
            for old in originals:
                item = {**old, "cancelled": True, "recorded_at": now.isoformat(), "revision_reason": reason}
                db.execute("INSERT INTO event_versions(id,recorded_at,body) VALUES (?,?,?)",
                           (item["id"], now.isoformat(), json.dumps(item, ensure_ascii=False)))
            for item in prepared:
                db.execute("INSERT INTO event_versions(id,recorded_at,body) VALUES (?,?,?)",
                           (item["id"], now.isoformat(), json.dumps(item, ensure_ascii=False)))

    def events(self, now: datetime, include_cancelled=False):
        """Return the plan known by that time, with interval-derived phase."""
        now = local(now)
        with self.db() as db:
            items = list(self._latest(db, now).values())
        for item in items:
            item["status"] = ("cancelled" if item.get("cancelled") else "planned" if now < local(item["start"])
                              else "completed" if now >= local(item["end"]) else "ongoing")
        return sorted([e for e in items if include_cancelled or not e.get("cancelled")], key=lambda e: e["start"])

    def record_dialogue(self, key: str, now: datetime, user: str, reply: str, status: str, **extra):
        """Record draft/uncertain/sent separately; retries are idempotent."""
        if status not in ("received", "drafted", "sending", "uncertain", "sent", "cancelled", "partial"):
            raise ValueError("未知对话发送状态")
        body = dict(id=key, at=local(now).isoformat(), user=user, reply=reply, status=status,
                    truth_layer="ai_conversation", **extra)
        with self.db() as db:
            old = db.execute("SELECT body FROM dialogues WHERE id=? AND status=?", (key, status)).fetchone()
            if old:
                previous = json.loads(old[0])
                if any(previous.get(k) != body.get(k) for k in ("user", "reply", "media_id", "event_ids")):
                    raise ValueError("相同发送状态不能修改原文或媒体")
                return
            db.execute("INSERT INTO dialogues(id,status,at,body) VALUES (?,?,?,?)",
                       (key, status, body["at"], json.dumps(body, ensure_ascii=False)))

    def close_loop(self, key: str, now: datetime):
        """Close a known conversation commitment without deleting its history."""
        with self.db() as db:
            db.execute("INSERT OR IGNORE INTO loop_closures VALUES (?,?)", (key, local(now).isoformat()))

    def snapshot(self, now: datetime):
        """Derive a consistent state for any timestamp; gaps stay unknown."""
        now = local(now)
        events = self.events(now)
        active = next((e for e in events if e["status"] == "ongoing"), None)
        with self.db() as db:
            sent = [json.loads(r[0]) for r in db.execute(
                "SELECT body FROM dialogues WHERE status='sent' AND at<=? ORDER BY at", (now.isoformat(),))]
            closed = {r[0] for r in db.execute("SELECT id FROM loop_closures WHERE at<=?", (now.isoformat(),))}
            initialized = db.execute("SELECT value FROM metadata WHERE key='initialized_at'").fetchone()[0]
        mood = {"label": "平静", "reason": "当前没有额外情绪事件", "source": "simulation"}
        if active:
            mood = {"label": active.get("feeling", "平静"), "reason": active.get("feeling_reason", active["description"]),
                    "event_id": active["id"], "source": "simulation"}
        if sent and sent[-1].get("feeling") and 0 <= (now-local(sent[-1]["at"])).total_seconds() < 7200:
            mood = {"label": sent[-1]["feeling"], "reason": sent[-1].get("feeling_reason", "本轮对话"),
                    "turn_id": sent[-1]["id"], "source": "ai_conversation", "decays_after_minutes": 120}
        return {"truth_layer": "fictional_life", "at": now.isoformat(), "initialized_at": initialized,
                "active": active, "feeling": mood,
                "upcoming": [e for e in events if e["status"] == "planned"][:12],
                "recent_events": [e for e in events if e["status"] == "completed"][-6:],
                "conversation_memory": sent[-8:],
                "open_loops": [{"id": t["id"], "title": t["topic"], "at": t["at"]} for t in sent
                               if t.get("topic") and t["id"] not in closed][-12:],
                "gap_notice": None if active else "此时没有已记录的模拟活动，不补造经历"}

    def export(self, root: Path, now: datetime):
        """Rebuild date-partitioned human-readable exports from both ledgers."""
        groups = {}
        with self.db() as db:
            versions = [json.loads(r[0]) for r in db.execute("SELECT body FROM event_versions ORDER BY recorded_at,seq")]
            dialogues = [json.loads(r[0]) for r in db.execute("SELECT body FROM dialogues ORDER BY at,seq")]
        for item in versions:
            # 设计标签留给独立复盘入口，客观事件导出只呈现生活事实。
            item = {key: value for key, value in item.items() if key != "_coaching"}
            first = local(item["start"]).date()
            last = (local(item["end"])-timedelta(microseconds=1)).date()
            day = first
            while day <= last:
                groups.setdefault(day, {"客观事件": [], "对话记忆": []})["客观事件"].append(
                    {**item, "continuation": day != first})
                day += timedelta(days=1)
        for item in dialogues:
            groups.setdefault(local(item["at"]).date(), {"客观事件": [], "对话记忆": []})["对话记忆"].append(item)
        for day, ledgers in groups.items():
            folder = Path(root)/f"{day:%Y/%m/%d}"
            folder.mkdir(parents=True, exist_ok=True)
            lines = [f"# {day} 虚构记忆", "", "客观事件属于AI模拟世界；对话记忆区分草稿和发送结果。", ""]
            for name, rows in ledgers.items():
                rows.sort(key=lambda r: (r.get("start", r.get("at")), r.get("recorded_at", r.get("at"))))
                (folder/f"{name}.jsonl").write_text("".join(json.dumps(r, ensure_ascii=False)+"\n" for r in rows), encoding="utf-8")
                lines += [f"## {name}", ""]
                for row in rows:
                    lines.append(f"- {row.get('start', row.get('at'))} | {row['id']} | "+
                                 (f"{row['description']} → {row['destination']}（修订：{row['revision_reason']}，取消：{row['cancelled']}）"
                                  if name == "客观事件" else f"{row['status']} | 用户：{row['user']} | AI：{row['reply']}"))
                    if name=="对话记忆":
                        for media in row.get("incoming_media",[]):
                            lines.append(f"  - 收件媒体 {media['id']}：{media.get('source',media.get('status'))}")
                            if media.get("text"):
                                lines.append(f"    - 自动转写：{media['text']}")
                            for file in media.get("files",[]):
                                lines.append(f"    - [查看捕获文件]({Path(file['path']).as_posix()})；SHA256：{file['sha256']}")
                        for understanding in row.get("media_understanding",[]):
                            lines.append(f"  - 媒体理解 {understanding['id']}：{understanding['observed']}；未知：{understanding['uncertain']}")
                lines.append("")
            (folder/"记忆.md").write_text("\n".join(lines), encoding="utf-8")
        atomic_json(Path(root)/"索引.json", {"updated_at": local(now).isoformat(), "source_db": str(self.path),
                                         "dates": [str(day) for day in sorted(groups)]})
