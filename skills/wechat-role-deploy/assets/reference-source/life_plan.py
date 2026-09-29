"""有界滚动日程：真实日历、虚构任务、完整交通链。"""

from datetime import datetime, timedelta
import hashlib

from bridge_state import CST
from timeline import local

CALENDAR_SOURCE = "https://www.gov.cn/zhengce/zhengceku/202511/content_7047091.htm"
HOLIDAYS_2026 = [("01-01", "01-03"), ("02-15", "02-23"), ("04-04", "04-06"), ("05-01", "05-05"),
                 ("06-19", "06-21"), ("09-25", "09-27"), ("10-01", "10-07")]
WORK_2026 = {"01-04", "02-14", "02-28", "05-09", "09-20", "10-10"}
LOCATIONS = {"home": "成都模拟住处", "office": "成都模拟办公室", "dance": "成都模拟舞室"}
STAGES = ["整理需求与问题", "改方案初稿", "核对流程与说明", "整理培训材料", "检查方案并归档"]


def workday(day):
    """Return the simulation workday and its public-calendar evidence."""
    md = day.strftime("%m-%d")
    if day.year == 2026:
        return (md in WORK_2026 or (day.weekday() < 5 and not any(a <= md <= b for a, b in HOLIDAYS_2026))), CALENDAR_SOURCE
    return day.weekday() < 5, "该年份法定调休日历未核验；仅模拟周一至周五工作"


def segment(key, start, end, kind, text, origin, destination=None, city="成都", **extra):
    destination = destination or origin
    return dict(id=key, start=start.isoformat(), end=end.isoformat(), kind=kind, description=text,
                origin=origin, destination=destination, place="in_transit" if origin != destination else origin,
                city=city, basis=["历史工作/作息作为参考，具体日程为用户批准的虚构"],
                feeling="有点累" if kind in ("overtime", "travel") else "专注" if kind == "work" else "平静", **extra)


def day_plan(day):
    """Build contiguous intervals; tasks use a persistent work-cycle ID."""
    start = datetime.combine(day, datetime.min.time(), CST)
    def t(hour):
        return start+timedelta(hours=hour)
    working, source = workday(day)
    ordinal = sum(workday(day-timedelta(days=i))[0] for i in range((day-datetime(day.year,1,1).date()).days+1))-1
    task = f"product-market-{day.year}-{max(ordinal,0)//5}"
    stage = STAGES[max(ordinal,0)%5]
    # 班次和通勤是模拟预算，不声称已取得真人公司的排班。
    if working:
        specs = [(0,7.5,"sleep","睡觉","home","home"), (7.5,7.75,"daily","洗漱、吃点东西","home","home"),
                 (7.75,9,"commute","去办公室，预留步行与地铁时间","home","office"),
                 (9,12,"work",stage,"office","office"), (12,12.75,"meal","午饭","office","office"),
                 (12.75,13.5,"rest","午休","office","office"), (13.5,18.5,"work",stage+"，继续上午未完部分","office","office")]
        if day.weekday() < 4:
            genre = "爵士" if day.weekday() < 2 else "hip-hop"
            specs += [(18.5,19,"commute","下班去舞室","office","dance"), (19,20.25,"dance",genre+"练习；具体新动作未记录","dance","dance"),
                      (20.25,21.25,"commute","舞课后回家","dance","home"), (21.25,22,"daily","吃点东西、洗澡","home","home")]
        else:
            specs += [(18.5,19.75,"commute","下班回家","office","home"), (19.75,20.5,"meal","晚饭","home","home"),
                      (20.5,22,"free","自己的空闲时间，没有约定外出","home","home")]
        specs += [(22,23.5,"free","休息，收尾今天的事","home","home"), (23.5,24,"sleep","睡觉","home","home")]
    else:
        specs = [(0,8.5,"sleep","休息日睡觉","home","home"), (8.5,10,"daily","起床洗漱、早餐","home","home"),
                 (10,12,"free","空闲时间，暂时没有外出安排","home","home"), (12,13,"meal","午饭","home","home"),
                 (13,14,"rest","午后休息","home","home"), (14,18,"free","自己的空闲时间，可调整未发生的计划","home","home"),
                 (18,19,"meal","晚饭","home","home"), (19,22,"free","在家休息，没有额外约定","home","home"),
                 (22,23.5,"daily","洗漱和放松","home","home"), (23.5,24,"sleep","睡觉","home","home")]
    return [segment(f"day-{day}-{i}",t(a),t(b),kind,text,origin,dest, calendar_source=source,
                    **({"task_id":task,"task_stage":max(ordinal,0)%5,"task_stage_count":5} if kind=="work" else {}))
            for i,(a,b,kind,text,origin,dest) in enumerate(specs)]


def ensure_week(timeline, now):
    """Append only uncovered future days; restarting never rerolls a plan."""
    now = local(now)
    existing = timeline.events(now)
    latest = max(now, max((local(e["end"]) for e in existing), default=now))
    end = now.replace(hour=0,minute=0,second=0,microsecond=0)+timedelta(days=7)
    additions = []
    day = latest.date()
    while datetime.combine(day,datetime.min.time(),CST) < end:
        for event in day_plan(day):
            if local(event["end"]) <= latest:
                continue
            if local(event["start"]) < latest:
                event["start"] = latest.isoformat()
                event["initialization_notice"] = "仅从初始化时刻开始模拟，之前部分未模拟"
            additions.append(event)
        day += timedelta(days=1)
    if additions:
        assign_local_tasks(existing,additions)
        timeline.plan(additions, now)


def assign_local_tasks(previous, events):
    """Carry work stages across actual planned office days, excluding trips."""
    earlier=[e for e in previous if e["kind"]=="work" and not e.get("trip_id")]
    last=max(earlier,key=lambda e:e["end"]) if earlier else None
    day=last["start"][:10] if last else None
    stage=last.get("task_stage",-1) if last else -1
    task=last.get("task_id") if last else None
    for event in sorted(events,key=lambda e:e["start"]):
        if event["kind"]!="work" or event.get("trip_id"):
            continue
        current=event["start"][:10]
        if current!=day:
            stage=(stage+1)%5
            if stage==0 or task is None:
                task="product-market-"+current
            day=current
        event.update(task_id=task,task_stage=stage,task_stage_count=5,
            description=STAGES[stage]+("，继续上午未完部分" if local(event["start"]).hour>=13 else ""))


def schedule_trip(timeline, key, day, days, city, now):
    """Plan a fictional work trip with a full return chain, never a real booking."""
    now = local(now)
    if city not in ("重庆", "北京", "广州", "上海") or not 2 <= days <= 5:
        raise ValueError("模拟出差只支持已定义城市及2—5天")
    start = datetime.combine(day,datetime.min.time(),CST)
    finish = start+timedelta(days=days)
    if start <= now:
        raise ValueError("出差必须从未来完整日期开始")
    existing = timeline.events(now)
    if any(e.get("trip_id")==key for e in existing):
        return
    remove = [e["id"] for e in existing if start <= local(e["start"]) < finish]
    if any(local(e["start"]) < start < local(e["end"]) or local(e["start"]) < finish < local(e["end"]) for e in existing):
        raise ValueError("出差边界与既有跨日事件冲突")
    events = []
    for offset in range(days):
        base = start+timedelta(days=offset)
        first, last = offset==0, offset==days-1
        hotel = "trip_hotel:"+key
        venue = "trip_training:"+key
        origin = "home" if first else hotel
        specs = [(0,7.5,"sleep","休息",origin,origin),(7.5,9 if first else 8.25,"daily","早餐、整理资料与行李",origin,origin)]
        specs += ([(9,15,"travel","去"+city+"，含进出站、候车与交通预算；无真实票务",origin,hotel)] if first else
                  [(8.25,9,"commute","前往模拟区域培训场地",hotel,venue),
                   (9,12,"work","内部产品培训与流程演示，使用已准备的材料",venue,venue),(12,13,"meal","午饭",venue,venue),
                   (13,14.25,"work","整理演练发现的问题与后续修改清单",venue,venue),
                   (14.25,15,"commute","回住宿处整理资料与行李",venue,hotel)])
        if last:
            specs += [(15,21,"travel","返程成都，含交通与市内换乘预算",hotel,"home"),(21,23.5,"daily","回家吃饭、洗漱、休息","home","home"),
                      (23.5,24,"sleep","睡觉","home","home")]
        else:
            specs += [(15,18,"work","核对培训材料，整理产品演示顺序",hotel,hotel),(18,19,"meal","晚饭",hotel,hotel),
                      (19,20.5,"work","整理当天问题，留出下一步修改清单",hotel,hotel),(20.5,23,"rest","洗漱、休息",hotel,hotel),
                      (23,24,"sleep","睡觉",hotel,hotel)]
        for i,(a,b,kind,text,origin,dest) in enumerate(specs):
            events.append(segment(f"trip-{key}-{offset}-{i}",base+timedelta(hours=a),base+timedelta(hours=b),kind,text,origin,dest,
                                  city="成都" if dest=="home" and kind!="travel" else city,
                                  trip_id=key,task_id="training-"+key,trip_notice="虚构内部培训准备，不代表真实客户、酒店或展会"))
    # 出差挤掉本地工作日后，后续任务从上个真实排定阶段继续，不虚构跳过的工作已完成。
    following=[e.copy() for e in existing if local(e["start"])>=finish and e["kind"]=="work" and not e.get("trip_id")]
    assign_local_tasks([e for e in existing if local(e["end"])<=start],following)
    remove.extend(e["id"] for e in following)
    events.extend(following)
    timeline.replace_future(remove,events,now,"完整模拟出差替代原本本地安排，包括舞课")


def cancel_evening_class(timeline, day, now, reason, rest=False):
    """Replace the future evening chain, preserving the office-to-home journey."""
    start = datetime.combine(day,datetime.min.time(),CST)+timedelta(hours=18.5)
    finish = datetime.combine(day+timedelta(days=1),datetime.min.time(),CST)
    rows = [e for e in timeline.events(now) if start <= local(e["start"]) < finish]
    if not rows or rows[0]["origin"] != "office":
        raise ValueError("当晚没有可替换的办公室出发计划")
    specs = ([(18.5,19.75,"commute","取消舞课，下班回家","office","home"),(19.75,21.25,"rest","吃饭、在家休息","home","home")] if rest else
             [(18.5,20,"overtime",reason,"office","office"),(20,21.25,"commute","加班后回家","office","home")]) + [
             (21.25,23.5,"daily","吃饭、洗漱，早点收尾","home","home"),(23.5,24,"sleep","睡觉","home","home")]
    base = start.replace(hour=0,minute=0)
    suffix = hashlib.sha256(reason.encode()).hexdigest()[:8]
    timeline.replace_future([e["id"] for e in rows], [segment(f"overtime-{day}-{suffix}-{i}",base+timedelta(hours=a),base+timedelta(hours=b),
        kind,text,origin,dest) for i,(a,b,kind,text,origin,dest) in enumerate(specs)],now,reason)


def ensure_schedule(timeline, now):
    """Advance a stable schedule; trip cadence is a simulation, not a real claim."""
    with timeline.schedule_lock():
        return _ensure_schedule(timeline, now)


def _ensure_schedule(timeline, now):
    now=local(now)
    ensure_week(timeline,now)
    events=timeline.events(now)
    trips=[e for e in events if e.get("trip_id")]
    if any(local(e["end"])>now for e in trips):
        return
    with timeline.db() as db:
        initialized=local(db.execute("SELECT value FROM metadata WHERE key='initialized_at'").fetchone()[0])
    anchor=max((local(e["end"]) for e in trips),default=initialized)
    # 历史仅证明存在较密集出差月份；14—24天是显式模拟规则，并非真人出差均值。
    digest=int(hashlib.sha256(anchor.date().isoformat().encode()).hexdigest()[:8],16)
    due=anchor.date()+timedelta(days=14+digest%11)
    day=max(due,now.date()+timedelta(days=1))
    horizon=now.date()+timedelta(days=6)
    while day+timedelta(days=1)<=horizon:
        if workday(day)[0] and workday(day+timedelta(days=1))[0]:
            city=("重庆","北京","广州","上海")[digest%4]
            schedule_trip(timeline,"auto-"+day.isoformat(),day,2,city,now)
            return
        day+=timedelta(days=1)


def apply_conversation_plan(timeline, text, now):
    """Describe a chat suggestion without changing the character's schedule."""
    if not any(word in text for word in ("今晚","明晚","明天晚上")):
        return "没有明确的未来计划变更请求"
    if not any(word in text for word in ("别跳舞","不要跳舞","不去跳舞","取消舞课")):
        return "未自动调整；模糊建议不能改写日程"
    return "对方建议取消舞课；未更改日程。按角色意愿和已有安排回应，不宣称已经取消或已经回家。"
