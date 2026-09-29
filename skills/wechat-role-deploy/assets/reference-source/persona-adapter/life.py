"""用户知情的本地虚构生活：真实时钟/天气、持久状态、独立历史来源。"""
import argparse
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import sqlite3
from urllib.error import URLError
from urllib.request import urlopen

ROOT = Path(__file__).resolve().parents[1]
CST = timezone(timedelta(hours=8), "Asia/Shanghai")
WORLD = json.loads((ROOT / "data/world.json").read_text(encoding="utf-8"))
PROJECTS = {
    "quiet_playlist": {"title": "整理一份安静的歌单", "basis": ["music-instrumental", "music-reset"],
                       "stages": ["想清楚想要的听感", "收集候选方向", "比较松弛和节奏感", "调整顺序", "回顾保留的理由"]},
    "dance_routine": {"title": "把一小段动作练得顺一点", "basis": ["dance-recent"],
                      "stages": ["先把动作顺序理清", "慢速练衔接", "找容易卡住的地方", "连起来回顾"]},
    "cozy_corner": {"title": "把桌边收拾得舒服一点", "basis": ["cozy-home", "clear-goals"],
                    "stages": ["分开常用与暂不用的东西", "腾出桌边空间", "整理线材与小物", "检查拿取是否顺手"]},
}


def aware(dt):
    if dt.tzinfo is None:
        raise ValueError("时间必须包含时区")
    return dt.astimezone(CST)


def now_cst():
    return datetime.now(CST)


def parse(value):
    return aware(datetime.fromisoformat(value))


def validate_weather(payload, now):
    """模型时次、时区、单位均核对，拒绝旧值、未来值和畸形响应。"""
    result = {"status": "unknown", "truth_layer": "real_environment", "source": "Open-Meteo",
              "source_url": WORLD["weather"]["endpoint"], "retrieved_at": aware(now).isoformat()}
    try:
        current = payload["current"]
        units = payload["current_units"]
        stamp = datetime.fromisoformat(current["time"])
        observed = stamp.replace(tzinfo=CST) if stamp.tzinfo is None else aware(stamp)
        age = (aware(now)-observed).total_seconds()
        if payload["timezone"] != "Asia/Shanghai" or payload["utc_offset_seconds"] != 28800:
            raise ValueError("天气时区不符")
        if not -300 <= age <= 90*60:
            raise ValueError("天气时次过期或来自未来")
        if units["temperature_2m"] != "°C" or units["precipitation"] != "mm" or units["weather_code"] != "wmo code":
            raise ValueError("天气单位不符")
        temperature, rain, code = float(current["temperature_2m"]), float(current["precipitation"]), int(current["weather_code"])
        if not (-60 <= temperature <= 60 and 0 <= rain <= 300 and 0 <= code <= 99):
            raise ValueError("天气值超出可接受范围")
        result.update(status="available", observed_at=observed.isoformat(), temperature_c=temperature,
                      precipitation_mm=rain, weather_code=code, expires_at=(observed+timedelta(minutes=90)).isoformat(),
                      nature="天气模型的当前时次，不是角色现场观察", returned_grid={"latitude": payload.get("latitude"), "longitude": payload.get("longitude")})
    except (KeyError, TypeError, ValueError) as exc:
        result["reason"] = str(exc)
    return result


def fresh_weather(weather, now):
    if weather and weather.get("status") == "available":
        if -300 <= (now-parse(weather["observed_at"])).total_seconds() <= 5400:
            return weather
    return {"status": "unknown", "reason": "没有有效的当前天气；不推断晴雨", "truth_layer": "real_environment"}


class Life:
    def __init__(self, path=ROOT / "runtime/life.sqlite"):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.db() as db:
            db.executescript("""
                CREATE TABLE IF NOT EXISTS kv(key TEXT PRIMARY KEY,value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY,at TEXT,kind TEXT,body TEXT);
                CREATE TABLE IF NOT EXISTS turns(id TEXT PRIMARY KEY,at TEXT,user TEXT,reply TEXT,digest TEXT,proactive INTEGER);
                CREATE TABLE IF NOT EXISTS loops(id TEXT PRIMARY KEY,title TEXT,status TEXT,created_at TEXT,origin TEXT);
            """)

    @contextmanager
    def db(self):
        db = sqlite3.connect(self.path, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def read(db, key, default=None):
        row = db.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def save(db, key, value):
        db.execute("INSERT INTO kv VALUES (?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                   (key, json.dumps(value, ensure_ascii=False)))

    @staticmethod
    def event(db, at, kind, body):
        db.execute("INSERT INTO events(at,kind,body) VALUES (?,?,?)", (at, kind, json.dumps(body, ensure_ascii=False)))

    def weather(self, now, refresh=False):
        with self.db() as db:
            cached = self.read(db, "weather")
        valid = fresh_weather(cached, now)
        if valid["status"] == "available" or not refresh:
            return valid
        try:
            with urlopen(WORLD["weather"]["endpoint"], timeout=15) as response:
                payload = json.load(response)
            result = validate_weather(payload, now)
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            result = {"status": "unknown", "reason": f"天气获取失败：{type(exc).__name__}", "retrieved_at": now.isoformat(), "truth_layer": "real_environment"}
        with self.db() as db:
            self.save(db, "weather", result)
        return result

    @staticmethod
    def activity(start, minutes, kind, description, reason, **extra):
        return dict(start=start.isoformat(), end=(start+timedelta(minutes=minutes)).isoformat(), kind=kind,
                    description=description, reason=reason, truth_layer="fictional_life", details=[], **extra)

    def choose(self, state, now):
        h = now.hour + now.minute/60
        if h < 7.5 or h >= 23:
            wake = now.replace(hour=7, minute=30, second=0, microsecond=0)
            if h >= 23:
                wake += timedelta(days=1)
            return self.activity(now, (wake-now).total_seconds()/60, "rest", "睡眠/安静时段", "保留夜间休息，不靠熬夜制造陪伴感", energy_delta=25)
        if state["energy"] < 30:
            return self.activity(now, 35, "rest", "休息一会儿", "上个活动消耗了精力，先恢复再安排", energy_delta=18)
        if 12 <= h < 13.5 or 18 <= h < 19.5:
            return self.activity(now, 40, "meal", "准备并吃一顿简单的饭", "到了饭点，先把饭吃好", energy_delta=12, basis=["food-recovery", "food-family"])
        if 7.5 <= h < 9:
            return self.activity(now, 35, "routine", "洗漱、早餐，整理今天要做的事", "先把日常安排理顺", energy_delta=8, basis=["backup-plans"])
        if now.weekday() < 5 and (9 <= h < 12 or 14 <= h < 17):
            boundary = 12 if h < 12 else 17
            minutes = min(50, max(5, int((boundary-h)*60)))
            return self.activity(now, minutes, "personal_admin", "处理个人文档与待办，留意还有哪一项没收尾",
                "模拟的昼间责任时段；不是推断真人目前单位、工时或国家调休安排", energy_delta=-9, basis=["clear-goals", "backup-plans"])
        projects = state["projects"]
        available = [key for key, value in projects.items() if value["progress"] < len(value["stages"])]
        if not available:
            return self.activity(now, 40, "rest", "留一点自由时间，回顾最近做完的小事", "已有项目做完，不自动捏造新成果", energy_delta=8)
        seed = int(hashlib.sha256(f"{now.date()}:{now.hour}:{state['completed_activities']}".encode()).hexdigest()[:8], 16)
        key = available[seed % len(available)]
        if h >= 21.5 and key == "dance_routine":
            key = next((k for k in available if k != "dance_routine"), key)
        duration = 25 if key == "dance_routine" else 40 + (seed % 3)*10
        duration = min(duration, max(5, int((23-h)*60)))
        progress = projects[key]["progress"]
        return self.activity(now, duration, "project", projects[key]["stages"][progress],
            f"接着做『{projects[key]['title']}』的第{progress+1}小步，不从头重来", project=key,
            energy_delta=-12 if key == "dance_routine" else -6, basis=projects[key]["basis"])

    def step(self, now, weather=None):
        now = aware(now)
        weather = fresh_weather(weather, now)
        with self.db() as db:
            state = self.read(db, "state")
            if state is None:
                state = {"truth_layer": "fictional_life", "created_at": now.isoformat(), "last_advanced_at": now.isoformat(),
                         "location": "home", "energy": 65, "active": None, "completed_activities": 0, "pending_visit": None,
                         "projects": {k: dict(v, progress=0, total=len(v["stages"]), completed_stages=[]) for k, v in PROJECTS.items()},
                         "plan_changes": [], "gap_notice": None, "last_turn_at": None,
                         "feeling": {"label": "平静", "reason": "模拟生活刚开始，尚未发生新对话", "at": now.isoformat()}}
                self.event(db, now.isoformat(), "initialized", {"notice": "虚构生活从此刻开始，不编造此前经历"})
            if now < parse(state["last_advanced_at"]):
                raise ValueError("系统时间倒退，拒绝改写已有生活状态")
            # 仅补全已经启动的活动/往返链，不生成未调用期间的满日经历。
            while state["active"] and parse(state["active"]["end"]) <= now:
                active = state["active"]
                end = parse(active["end"])
                state["completed_activities"] += 1
                state["energy"] = max(0, min(100, state["energy"] + active.get("energy_delta", 0)))
                if active.get("project") and not active.get("interrupted"):
                    project = state["projects"][active["project"]]
                    project["completed_stages"].append({"at": active["end"], "stage": active["description"], "details": active["details"], "truth_layer": "fictional_life"})
                    project["progress"] += 1
                self.event(db, active["end"], "interrupted_simulation" if active.get("interrupted") else "completed_simulation", active)
                state["active"] = None
                if active["kind"] == "travel":
                    state["location"] = active["destination"]
                    if active["destination"] != "home":
                        state["active"] = self.activity(end, 45, "visit", "在东郊记忆周边公共街区走走", "这是已计划的模拟出行，不编造演出、店铺营业和购买", energy_delta=-8)
                elif active["kind"] == "visit":
                    state["active"] = self.activity(end, 60, "travel", "从公共街区返程", "为往返预留时间，不能瞬移回家", destination="home", energy_delta=-5)
                    state["location"] = "in_transit"
                if state["active"]:
                    self.event(db, state["active"]["start"], "started_simulation", state["active"])
                elif (now-end).total_seconds() > 3600:
                    state["gap_notice"] = f"{end.isoformat()} 至 {now.isoformat()} 未逐时模拟；没有补造期间的人物、见闻或成果。"
            if state["active"] is None:
                pending = state["pending_visit"]
                if pending and parse(pending["start"]) <= now:
                    safe = weather["status"] == "available" and weather.get("precipitation_mm", 1) == 0 and weather.get("weather_code", 99) < 45 and 8 <= weather.get("temperature_c", 0) <= 33
                    if pending["require_weather"] and not safe or not 9 <= now.hour <= 17:
                        state["plan_changes"].append({"at": now.isoformat(), "reason": "天气未核实/不适宜或出发时段已过，推迟户外计划", "plan": pending})
                        state["plan_changes"] = state["plan_changes"][-20:]
                    else:
                        state["active"] = self.activity(now, 60, "travel", "从模拟住处前往东郊记忆周边", "留足60分钟模拟交通预算，不声称查过实时路线", destination=pending["destination"], energy_delta=-5)
                        state["location"] = "in_transit"
                    state["pending_visit"] = None
                if state["active"] is None:
                    state["active"] = self.choose(state, now)
                if state["pending_visit"]:
                    start = parse(state["pending_visit"]["start"])
                    if now < start < parse(state["active"]["end"]):
                        active = state["active"]
                        fraction = (start-now).total_seconds() / (parse(active["end"])-now).total_seconds()
                        active["energy_delta"] = round(active.get("energy_delta", 0)*fraction)
                        active["interrupted"] = True
                        state["active"]["end"] = start.isoformat()
                        state["active"]["description"] += "（出门前的一小步）"
                self.event(db, now.isoformat(), "started_simulation", state["active"])
            if (now-parse(state["feeling"]["at"])).total_seconds() > 3*3600:
                state["feeling"] = {"label": "有点累" if state["energy"] < 35 else "平静", "reason": "随经过的时间缓和；保留事件记忆，不把旧情绪永久冻结", "at": now.isoformat()}
            state["last_advanced_at"] = now.isoformat()
            self.save(db, "state", state)
            return state

    def plan_visit(self, destination, start, require_weather=True):
        start = aware(start)
        if destination != "east_memory" or not 9 <= start.hour <= 17:
            raise ValueError("仅支持已核实公共地点的白天模拟出行")
        with self.db() as db:
            state = self.read(db, "state")
            if state is None or start <= parse(state["last_advanced_at"]):
                raise ValueError("先初始化生活，只能规划未来出发")
            if state["pending_visit"]:
                raise ValueError("已有待出发计划，不能静默覆盖")
            state["pending_visit"] = {"start": start.isoformat(), "destination": destination, "require_weather": require_weather,
                                      "truth_layer": "fictional_plan", "status": "planned_not_completed"}
            if state["active"] and parse(state["active"]["end"]) > start:
                raise ValueError("出发与当前活动冲突，请安排当前活动结束之后")
            self.save(db, "state", state)
            self.event(db, state["last_advanced_at"], "planned_visit", state["pending_visit"])

    def add_project(self, project_id, title, stages, basis):
        if not project_id or not title or not stages or not basis:
            raise ValueError("新项目需要ID、标题、步骤和人格卡依据")
        known = {c["id"] for c in json.loads((ROOT / "data/cards.json").read_text(encoding="utf-8"))}
        if any(card not in known for card in basis):
            raise ValueError("新项目引用的人格卡不存在")
        with self.db() as db:
            state = self.read(db, "state")
            if state is None:
                raise ValueError("先获取生活上下文")
            if project_id in state["projects"]:
                raise ValueError("项目ID已存在，不能覆盖过去进度")
            if sum(p["progress"] < p["total"] for p in state["projects"].values()) >= 5:
                raise ValueError("已有五个未完项目，先推进或收尾，不不断堆新剧情")
            state["projects"][project_id] = dict(title=title, stages=stages, basis=basis, progress=0, total=len(stages), completed_stages=[])
            self.save(db, "state", state)
            self.event(db, state["last_advanced_at"], "new_fictional_project", {"id": project_id, "title": title, "basis": basis})

    def record(self, turn_id, now, user, reply, topic="", details=None, proactive=False, feeling=None, feeling_reason=None):
        now = aware(now)
        content_hash = hashlib.sha256(json.dumps([user, reply, topic, details, proactive, feeling, feeling_reason], ensure_ascii=False).encode()).hexdigest()
        if feeling and (feeling not in ("平静", "开心", "有点累", "有点烦", "惦记", "期待") or not feeling_reason):
            raise ValueError("模拟心情需要有效标签和具体原因")
        with self.db() as db:
            old = db.execute("SELECT digest FROM turns WHERE id=?", (turn_id,)).fetchone()
            if old:
                if old[0] != content_hash:
                    raise ValueError("同一轮ID内容发生变化，拒绝覆盖")
                return
            state = self.read(db, "state")
            if state is None:
                raise ValueError("聊天前先获取生活上下文")
            if now < parse(state["last_advanced_at"]) or state["last_turn_at"] and now < parse(state["last_turn_at"]):
                raise ValueError("对话时间倒退")
            if proactive:
                eligible, _ = self.eligibility(db, now)
                if not eligible:
                    raise ValueError("主动话题处于冷却、安静时段或已达当日上限")
            db.execute("INSERT INTO turns(id,at,user,reply,digest,proactive) VALUES (?,?,?,?,?,?)", (turn_id, now.isoformat(), user, reply, content_hash, int(proactive)))
            if topic:
                loop_id = hashlib.sha256(topic.encode()).hexdigest()[:16]
                db.execute("INSERT OR IGNORE INTO loops VALUES (?,?,?,?,?)", (loop_id, topic, "open", now.isoformat(), "current_ai_conversation"))
            if details:
                if now >= parse(state["active"]["end"]):
                    raise ValueError("活动已到结束时间，先刷新上下文再补细节")
                state["active"]["details"].extend(details)
            if feeling:
                state["feeling"] = {"label": feeling, "reason": feeling_reason, "at": now.isoformat()}
            state["last_turn_at"] = now.isoformat()
            self.save(db, "state", state)
            self.event(db, now.isoformat(), "ai_conversation", {"turn_id": turn_id, "notice": "不是历史真人语料", "details": details or []})

    def close_loop(self, loop_id):
        with self.db() as db:
            row = db.execute("UPDATE loops SET status='closed' WHERE id=? AND status='open'", (loop_id,))
            if row.rowcount != 1:
                raise ValueError("没有这个未完话题，或它已经关闭")

    @staticmethod
    def eligibility(db, now):
        latest = db.execute("SELECT at,proactive FROM turns ORDER BY at DESC LIMIT 1").fetchone()
        cool = 60 + int(hashlib.sha256(str(now.date()).encode()).hexdigest()[:2], 16) % 31
        if not 9 <= now.hour < 22:
            return False, "安静时段"
        if latest and (now-parse(latest["at"])).total_seconds() < cool*60:
            return False, f"最近对话后的{cool}分钟冷却"
        if latest and latest["proactive"]:
            return False, "上条主动话题尚未得到用户回应，不追发"
        count = db.execute("SELECT COUNT(*) FROM turns WHERE proactive=1 AND substr(at,1,10)=?", (str(now.date()),)).fetchone()[0]
        return (count < 2, "当日最多两次，只提供候选，不自动外发")

    def context(self, now, weather=None):
        state = self.step(now, weather)
        with self.db() as db:
            eligible, reason = self.eligibility(db, now)
            turns = [dict(r) for r in db.execute("SELECT id,at,user,reply FROM turns ORDER BY at DESC LIMIT 6")][::-1]
            loops = [dict(r) for r in db.execute("SELECT * FROM loops WHERE status='open' ORDER BY created_at")]
            recent = [dict(r, body=json.loads(r["body"])) for r in db.execute("SELECT * FROM events WHERE kind IN ('completed_simulation','interrupted_simulation') ORDER BY id DESC LIMIT 4")][::-1]
        return {"real_environment": {"city": "成都", "now": now.isoformat(), "timezone": "Asia/Shanghai", "weekday": "一二三四五六日"[now.weekday()], "weather": fresh_weather(weather, now)},
                "fictional_life": state, "recent_fictional_events": recent, "conversation_memory": turns, "open_loops": loops,
                "initiative": {"eligible": eligible, "reason": reason, "motive": loops[0]["title"] if loops else state["active"]["reason"], "automatic_send": False},
                "truth_contract": "环境是实查数据；生活是用户批准的虚构；往事只能查历史卡与原始库；本轮对话另存。"}


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("action", choices=["context", "record", "close-loop", "plan-visit", "new-project"])
    p.add_argument("--refresh-world", action="store_true")
    p.add_argument("--query", default="")
    p.add_argument("--turn-id")
    p.add_argument("--user", default="")
    p.add_argument("--reply", default="")
    p.add_argument("--topic", default="")
    p.add_argument("--detail", action="append")
    p.add_argument("--proactive", action="store_true")
    p.add_argument("--loop-id")
    p.add_argument("--start")
    p.add_argument("--project-id")
    p.add_argument("--title")
    p.add_argument("--stage", action="append")
    p.add_argument("--basis", action="append")
    p.add_argument("--feeling")
    p.add_argument("--feeling-reason")
    p.add_argument("--output", type=Path)
    a = p.parse_args()
    life, now = Life(), now_cst()
    if a.action == "context":
        result = life.context(now, life.weather(now, a.refresh_world))
        if a.query:
            from evidence import query
            result["historical_cards"] = [dict({k: v for k, v in c.items() if k not in ("evidence", "media_refs")},
                evidence=[{k: v for k, v in e.items() if k != "context"} for e in c["evidence"]],
                media_ref_count=len(c.get("media_refs", [])), media_lookup="需要媒体时用evidence.py query取得确切映射") for c in query(a.query, 4)]
    elif a.action == "record":
        if not a.turn_id or not a.reply:
            p.error("record需要--turn-id和--reply")
        life.record(a.turn_id, now, a.user, a.reply, a.topic, a.detail, a.proactive, a.feeling, a.feeling_reason)
        result = {"recorded": a.turn_id, "truth_layer": "current_ai_conversation"}
    elif a.action == "close-loop":
        life.close_loop(a.loop_id)
        result = {"closed": a.loop_id}
    elif a.action == "new-project":
        life.add_project(a.project_id, a.title, a.stage, a.basis)
        result = {"new_fictional_project": a.project_id}
    else:
        if not a.start:
            p.error("plan-visit需要带时区的--start")
        life.plan_visit("east_memory", parse(a.start))
        result = {"planned": a.start, "truth_layer": "fictional_plan", "not_sent": True}
    rendered = json.dumps(result, ensure_ascii=False, indent=2)
    if a.output:
        a.output.parent.mkdir(parents=True, exist_ok=True)
        a.output.write_text(rendered, encoding="utf-8")
    print(json.dumps({"saved": str(a.output)}, ensure_ascii=False) if a.output else rendered)
