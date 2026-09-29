"""本机无模型时钟：持续模拟、日期轨迹、天气缓存和既有桥接维护。"""

import argparse
import importlib.util
import json
import os
from pathlib import Path
import sqlite3
import sys
import time

from bridge_state import atomic_json, configured_plan, now_cst
from life_plan import ensure_schedule
import maintenance
from timeline import Timeline, local
from v3_context import weather_context

ROOT = Path(__file__).resolve().parent


def tick(root: Path = ROOT, now=None, *, refresh_weather=True, maintain=True) -> dict:
    """Advance known simulated events; never invoke a language model here."""
    now = local(now or now_cst())
    runtime = root/"runtime"
    runtime.mkdir(parents=True, exist_ok=True)
    output = runtime/"local-runtime-status.json"
    if (runtime/"local-runtime-stop.json").exists():
        result = {"status":"stopped", "at":now.isoformat(), "pid":os.getpid(), "model_calls":0}
        atomic_json(output, result)
        return result
    timeline = Timeline(runtime/"life-v3.sqlite", now)
    # SQLite单写事务保护本地时钟，常驻入口另持单例锁。
    db = sqlite3.connect(runtime/"simulation.sqlite", timeout=2)
    try:
        db.executescript("CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT);"
                         "CREATE TABLE IF NOT EXISTS trajectory(id TEXT PRIMARY KEY,at TEXT,body TEXT);")
        db.execute("BEGIN IMMEDIATE")
        row = db.execute("SELECT value FROM kv WHERE key='clock'").fetchone()
        clock = json.loads(row[0]) if row else {}
        previous = local(clock.get("last_tick", now.isoformat()))
        if now < previous:
            raise ValueError("本机时钟回退，不倒改既有生活轨迹")
        config = json.loads((root/"config.json").read_text(encoding="utf-8"))
        ensure_schedule(timeline, now)
        if config.get("coaching_enabled"):
            from coaching_scenes import ensure
            ensure(timeline, now, config)
        snapshot = timeline.snapshot(now)
        events = timeline.events(now)
        offline = (now-previous).total_seconds() > 600

        def record(key, at, body):
            entry = {"at":at, "observed_at":now.isoformat(), "truth_layer":"fictional_life", **body}
            db.execute("INSERT OR IGNORE INTO trajectory VALUES (?,?,?)", (key, at, json.dumps(entry, ensure_ascii=False)))

        for event in events:
            for field, phase in (("start", "started"), ("end", "completed")):
                at = local(event[field])
                if previous < at <= now and local(event["recorded_at"]) <= at:
                    record(event["id"]+":"+event["recorded_at"]+":"+phase, event[field],
                           {"event_id":event["id"], "phase":phase, "description":event["description"],
                            "derived_from_saved_plan":True, "observed_after_offline_gap":offline})
        active = snapshot["active"]
        phase_key = json.dumps({"active_id":active["id"] if active else None, "feeling":snapshot["feeling"]}, sort_keys=True)
        changed = phase_key != clock.get("phase_key")
        if changed:
            record("state:"+now.isoformat(), now.isoformat(), {"phase":"state", "active":active, "feeling":snapshot["feeling"]})
        config = json.loads((root/"config.json").read_text(encoding="utf-8"))
        city = active["city"] if active and active["kind"] != "travel" else "途中未知"
        weather = clock.get("weather", {"status":"unknown", "reason":"本地模拟尚无有效天气"})
        weather_due = not clock.get("last_weather") or (now-local(clock["last_weather"])).total_seconds() >= 1800
        if refresh_weather and (weather_due or city != clock.get("city")):
            spec = importlib.util.spec_from_file_location("life_weather", Path(config["persona_root"])/"scripts/life.py")
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            weather = weather_context(root, city, now, module, refresh=True)
            clock.update(weather=weather, last_weather=now.isoformat())
        with timeline.db() as source:
            revision = [source.execute("SELECT COALESCE(MAX(seq),0) FROM "+table).fetchone()[0]
                        for table in ("event_versions", "dialogues")]
        if changed or revision != clock.get("revision"):
            timeline.export(root/"虚构记忆", now)
        days = {}
        for at, body in db.execute("SELECT at,body FROM trajectory ORDER BY at,id"):
            days.setdefault(local(at).strftime("%Y/%m/%d"), []).append(body)
        for day, lines in days.items():
            folder = root/"虚构记忆"/day
            folder.mkdir(parents=True, exist_ok=True)
            path = folder/"运行轨迹.jsonl"
            content = "\n".join(lines)+"\n"
            if not path.exists() or path.read_text(encoding="utf-8") != content:
                path.write_text(content, encoding="utf-8")
        clock.update(last_tick=now.isoformat(), phase_key=phase_key, revision=revision, city=city)
        result = {"status":"running", "at":now.isoformat(), "pid":os.getpid(), "timezone":"Asia/Shanghai", "active":active,
                  "feeling":snapshot["feeling"], "weather":weather, "next_event":snapshot["upcoming"][:1],
                  "daily_slots":configured_plan(now, config),
                  "wechat_send_paused":(runtime/"stop.json").exists(), "model_calls":0,
                  "model_calls_notice":"仅指本次生活时钟；微信具体对话由独立监听进程按需调用模型"}
        due = not clock.get("last_maintenance") or (now-local(clock["last_maintenance"])).total_seconds() >= 900
        if maintain and due:
            clock["last_maintenance"] = now.isoformat()
            clock["maintenance"] = maintenance.check(root)
        result["bridge"] = clock.get("maintenance", {"status":"not_checked"})
        db.execute("INSERT OR REPLACE INTO kv VALUES ('clock',?)", (json.dumps(clock,ensure_ascii=False),))
        db.commit()
        atomic_json(output, result)
        return result
    finally:
        db.close()


def serve(root: Path = ROOT, *, interval=60) -> None:
    """Run one local worker; preserve errors without waking a model."""
    from run import single_instance
    runtime = root/"runtime"
    with single_instance(runtime/"local-worker"):
        while True:
            try:
                if tick(root)["status"] == "stopped":
                    return
            except Exception as exc:
                error = {"status":"error", "at":now_cst().isoformat(), "pid":os.getpid(), "error":str(exc)}
                atomic_json(runtime/"local-runtime-status.json", error)
                with (runtime/"local-runtime-errors.jsonl").open("a", encoding="utf-8") as stream:
                    stream.write(json.dumps(error, ensure_ascii=False)+"\n")
            deadline = time.monotonic()+interval
            while time.monotonic() < deadline and not (runtime/"local-runtime-stop.json").exists():
                time.sleep(min(1, max(0, deadline-time.monotonic())))


def main() -> None:
    """Headless entry: errors are visible in durable status files."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["tick", "serve", "stop"], default="tick", nargs="?")
    args = parser.parse_args()
    if args.action == "stop":
        atomic_json(ROOT/"runtime/local-runtime-stop.json", {"at":now_cst().isoformat(), "reason":"用户停止本地模拟"})
        return
    if args.action == "serve":
        serve()
        return
    try:
        result = tick()
    except Exception as exc:
        atomic_json(ROOT/"runtime/local-runtime-status.json", {"status":"error", "at":now_cst().isoformat(), "error":str(exc)})
        raise
    if sys.stdout is not None:
        print(json.dumps({k:result[k] for k in ("status", "at", "model_calls")}, ensure_ascii=False))


if __name__ == "__main__":
    main()
