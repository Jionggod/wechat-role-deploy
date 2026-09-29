"""脚本规划自然生活细节；不调用模型、不预写争吵或要求用户服从。"""

from copy import deepcopy
from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

from timeline import Timeline, local

ROOT = Path(__file__).resolve().parent
CONTROL_KEY = "coaching_control"


def settings(timeline: Timeline) -> dict:
    """Read persistent opt-out state."""
    with timeline.db() as db:
        row = db.execute("SELECT value FROM metadata WHERE key=?", (CONTROL_KEY,)).fetchone()
    return json.loads(row[0]) if row else {"paused": False}


def records(timeline: Timeline) -> list[dict]:
    """Recover provenance directly from atomic event revisions."""
    found, latest = {}, {}
    with timeline.db() as db:
        for row in db.execute("SELECT body FROM event_versions ORDER BY seq"):
            event = json.loads(row[0])
            latest[event["id"]] = event
            scene = event.get("_coaching")
            if scene:
                found.setdefault(scene["id"], scene)
    return [{**scene, "cancelled": latest[scene["parent_id"]].get("_coaching", {}).get("id") != key
             or latest[scene["parent_id"]].get("cancelled", False)} for key, scene in found.items()]


def control(timeline: Timeline, text: str, now: datetime) -> dict | None:
    """Only current explicit user commands change persistent training state."""
    message = text.strip().strip("。！! ")
    stop = message in {"暂停压力测试", "停止压力测试", "停止测试", "不要再给我压力", "压力测试让我不舒服"}
    resume = message == "恢复压力测试"
    if not stop and not resume:
        return None
    result = {"paused": stop, "at": local(now).isoformat(), "reason": message}
    with timeline.db() as db:
        db.execute("INSERT OR REPLACE INTO metadata VALUES (?,?)", (CONTROL_KEY, json.dumps(result, ensure_ascii=False)))
    # 标志立即生效；未来情境由下一次ensure在日程锁内撤销，已发生事实不变。
    return result


def ensure(timeline: Timeline, now: datetime, config: dict) -> None:
    """Enrich at most one future interval per day, with bounded pressure."""
    if not config.get("coaching_enabled"):
        return
    now = local(now)
    patterns = json.loads((ROOT/"data/coaching-patterns.json").read_text(encoding="utf-8"))["patterns"]
    source = {row["id"]: row["source_ids"] for row in patterns}
    with timeline.schedule_lock():
        paused = settings(timeline)["paused"]
        events = timeline.events(now)
        if paused:
            for event in events:
                scene = event.get("_coaching")
                if scene and scene["pressure"] and local(event["start"]) > now:
                    restored = {**event, **scene["original"]}
                    restored.pop("_coaching", None)
                    timeline.replace_future([event["id"]], [restored], now, "用户暂停压力情境；恢复原有未来安排")
            return
        existing = records(timeline)
        occupied = {row["start"][:10] for row in existing}
        for event in events:
            start = local(event["start"])
            if start <= now or start.date().isoformat() in occupied or not 9 <= start.hour < 23:
                continue
            if event["kind"] not in {"work", "free"} or event.get("_coaching"):
                continue
            if event["kind"] == "work" and not event.get("task_id"):
                continue
            seed = int(hashlib.sha256((str(start.date())+config.get("contact_wechat_id", "")).encode()).hexdigest()[:8], 16)
            # 每日稳定选择；不因回应好坏改变难度或再抽一次。
            if seed % 5 == 0:
                occupied.add(str(start.date()))
                continue
            pressure = seed % 2 == 0
            recent = [r for r in existing if r["pressure"] and not r.get("cancelled", False)
                      and start-timedelta(days=7) < local(r["start"]) <= start]
            if pressure and len(recent) >= 3:
                pressure = False
            crisis = (pressure and event["kind"] == "work" and event.get("task_stage", 1) >= 1
                      and not any(r["crisis"] for r in recent))
            if crisis:
                detail = "复核本阶段材料时发现两处流程说明前后不一致，先标记并核对；未确认修好，不涉及真实产品发布"
                pattern, feeling = "changed_plan", "有点烦但能着手处理"
            elif pressure and event["kind"] == "work":
                detail = "本阶段资料还要核清，先整理未确认点，不赶着给结论；没有新增截止时间或他人催促"
                pattern, feeling = "availability", "有点累，想把手头理顺"
            elif pressure:
                detail = "在家整理自己的待办，在休息与收尾事情之间取舍，没有对他人的新约定"
                pattern, feeling = "family_boundary", "想留点自己的时间"
            elif event["kind"] == "work":
                detail = "把本阶段已确认内容和待核问题分开放，方便之后接着做；不代表整个任务已经完成"
                pattern, feeling = "respect", "平静，按自己的节奏做事"
            else:
                detail = "在家放松，给自己的兴趣留点空闲；没有额外外出安排，也没有约定对方必须陪伴"
                pattern, feeling = "family_boundary", "放松"
            scene = {"id": "scene-"+event["id"], "parent_id": event["id"], "start": event["start"], "end": event["end"],
                     "recorded_at": now.isoformat(), "source_ids": source[pattern], "pattern": pattern,
                     "pressure": pressure, "crisis": crisis, "detail": detail,
                     "original": {key: event.get(key, "") for key in ("description", "feeling", "feeling_reason")},
                     "exercise": "理解与支持、清楚表达、尊重边界；不要求发生冲突",
                     "closure": "该活动结束即停止主动设置压力；实际问题是否解决另看记录，未回应不追问"}
            replacement = {**event, "description": event["description"]+"；"+detail,
                           "feeling": feeling, "feeling_reason": detail, "_coaching": scene}
            timeline.replace_future([event["id"]], [replacement], now, "未来生活细节，历史模式仅作沟通机会参考")
            occupied.add(str(start.date()))
            existing.append(scene)


def role_context(timeline: Timeline, now: datetime, kind: str) -> dict:
    """Expose life facts, not scores, target answers or training labels."""
    now = local(now)
    paused = settings(timeline)["paused"]
    rows = [r for r in records(timeline) if not r["cancelled"] and local(r["start"]) <= now < local(r["end"])
            and not (paused and r["pressure"])]
    mentioned = set()
    if kind == "proactive":
        with timeline.db() as db:
            for (body,) in db.execute("SELECT body FROM dialogues WHERE status='sent' AND at<=?", (now.isoformat(),)):
                mentioned.update(json.loads(body).get("scene_ids", []))
        rows = [r for r in rows if r["id"] not in mentioned]
    return {"situations": [{"id": r["id"], "event_id": r["parent_id"], "detail": r["detail"]} for r in rows],
            "avoid_reopening": sorted(mentioned),
            "boundary": "自然交流，不预设争吵；不要求对方安慰、认错或给正确答案；不凭空加码。"
                        +( "用户暂停了压力情境，普通聊天继续，不主动制造或重提烦恼。" if paused else "")}


def public_context(context: dict, paused: bool) -> dict:
    """Strip planning metadata from every event copy sent to the model."""
    result = deepcopy(context)
    suppressed = set(result.get("life_situations", {}).get("avoid_reopening", []))
    def clean(value):
        if isinstance(value, dict):
            scene = value.pop("_coaching", None)
            if scene and ((paused and scene["pressure"]) or scene["id"] in suppressed):
                value.update(scene["original"])
            for child in value.values():
                clean(child)
        elif isinstance(value, list):
            for child in value:
                clean(child)
    clean(result)
    return result


def can_send(timeline: Timeline, ids: list[str], now: datetime) -> bool:
    """Recheck opt-out and event validity immediately before sending."""
    if not ids:
        return True
    paused = settings(timeline)["paused"]
    available = {r["id"]: r for r in records(timeline)}
    return all(key in available and not available[key]["cancelled"]
               and local(available[key]["start"]) <= local(now) < local(available[key]["end"])
               and not (paused and available[key]["pressure"]) for key in ids)
