"""新生活上下文，生产与预览共用；从不推进旧生活数据库。"""

from contextlib import closing
import json
from pathlib import Path
import sqlite3
from urllib.parse import urlencode
from urllib.request import urlopen

from bridge_state import atomic_json
from life_plan import ensure_schedule, apply_conversation_plan
from timeline import local

CITY_COORDS={"成都":(30.67,104.06),"重庆":(29.56,106.55),"北京":(39.90,116.40),"广州":(23.13,113.26),"上海":(31.23,121.47)}


def weather_context(root, city, now, module, refresh=False):
    """Reuse the existing time/unit validator with a city-bound cache."""
    if city not in CITY_COORDS:
        return {"status":"unknown","reason":"交通途中或城市未确定，不套用成都天气"}
    cache=Path(root)/"runtime"/f"weather-{city}.json"
    saved=json.loads(cache.read_text(encoding="utf-8")) if cache.exists() else None
    if saved and saved.get("city")==city and module.fresh_weather(saved,now)["status"]=="available":
        return saved
    if not refresh:
        return {"status":"unknown","city":city,"reason":"没有当前有效天气，本次不联网"}
    lat,lon=CITY_COORDS[city]
    url="https://api.open-meteo.com/v1/forecast?"+urlencode(dict(latitude=lat,longitude=lon,
        current="temperature_2m,precipitation,weather_code,is_day",timezone="Asia/Shanghai"))
    try:
        with urlopen(url,timeout=15) as response:
            payload=json.load(response)
        result=module.validate_weather(payload,now)
        if abs(float(payload.get("latitude",999))-lat)>.5 or abs(float(payload.get("longitude",999))-lon)>.5:
            raise ValueError("返回天气网格与城市不符")
    except (OSError,ValueError,TypeError) as exc:
        result={"status":"unknown","reason":f"天气获取失败：{type(exc).__name__}"}
    result.update(city=city,source_url=url,retrieved_at=now.isoformat(),truth_layer="real_environment")
    atomic_json(cache,result)
    return result


def legacy_turns(config):
    """Read previous AI conversations without changing historical life state."""
    path=Path(config["persona_root"])/"runtime/life.sqlite"
    if not path.exists():
        return []
    with closing(sqlite3.connect(path.as_uri()+"?mode=ro",uri=True)) as db:
        db.row_factory=sqlite3.Row
        return [dict(row) for row in db.execute("SELECT id,at,user,reply,proactive FROM turns ORDER BY at DESC LIMIT 6")][::-1]


def build_context(bridge, now, module):
    """Assemble separated truth layers and only reviewed media candidates."""
    timeline=bridge.timeline(now)
    ensure_schedule(timeline,now)
    if bridge.config.get("coaching_enabled"):
        from coaching_scenes import ensure
        ensure(timeline,now,bridge.config)
    pending=bridge.state.data["pending"]
    change=apply_conversation_plan(timeline,pending["user"],now)
    state=timeline.snapshot(now)
    active=state["active"]
    city=active["city"] if active and active["kind"]!="travel" else "途中未知"
    profile={"role_summary":"兼顾产品与市场相关工作","responsibilities":["产品需求","市场方案","培训资料","展会和直播准备"],
             "truth_layer":"fictional_life","basis":["work-2025-roles","work-2024-training"]}
    from run import ROOT
    facts=json.loads((ROOT/"data/user-facts.private.json").read_text(encoding="utf-8"))
    workplace={**facts["workplace"],"source":"用户2026-09-27提供，未独立核验","not_live_location":True}
    media=bridge.media().candidates(pending["user"],state["feeling"]["label"],pending["id"],now,state["conversation_memory"])
    if not bridge.config.get("outgoing_media_enabled",False):
        media=[]
    # 模型只看少量语境摘要和出处，不把完整家人/第三人原话塞入素材候选。
    for item in media:
        item["source_messages"]=[{"id":s["id"],"time":s["time"]} for s in item["source_messages"]]
    history=legacy_turns(bridge.config)
    context={"real_environment":{"now":now.isoformat(),"timezone":"Asia/Shanghai","city":city,
                  "weather":weather_context(bridge.root,city,now,module,bridge.config.get("refresh_world",True))},
             "fictional_life":{"truth_layer":"fictional_life","active":active,"work_profile":profile,
                  "location":active["place"] if active else "unknown","feeling":state["feeling"]},
             "life_timeline":state,"recent_fictional_events":state["recent_events"],"plan_change":change,
             "trip_plan":[e for e in timeline.events(now) if e.get("trip_id") and local(e["end"])>now],
             "conversation_memory":history+state["conversation_memory"],"open_loops":state["open_loops"],
             "user_provided_workplace":workplace,"media_candidates":media,
             "legacy_notice":"旧AI回复仅维持对话连续性，未核验的旧动作等不能成为新事件依据。"}
    timeline.export(bridge.root/"虚构记忆",now)
    if bridge.config.get("coaching_enabled"):
        from coaching_scenes import public_context, role_context, settings
        context["life_situations"] = role_context(timeline,now,pending["kind"])
        context = public_context(context, settings(timeline)["paused"])
    return context
