"""桥接专用：有出处的生活资料、语境样例与明确为虚构的日常。"""

from datetime import datetime, timedelta
import hashlib
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ARCHIVE = ROOT / "persona_review/2026-09-27/life-evidence.json"
TOPICS = {
    "舞蹈": (["动作", "练舞", "跳舞", "学舞", "爵士", "hip", "黑怕", "舞室", "舞课", "什么课"],
           ["舞蹈", "爵士", "舞室"], ["dance-learning", "dance-weekly", "dance-evening", "dance-motivation"]),
    "工时": (["几点", "下班", "作息", "班次", "通勤", "地铁"],
           ["上班", "下班", "通勤"], ["work-2025-hours", "work-hours-variation", "commute", "work-live-august"]),
    "工作": (["工作", "上班", "忙", "产品", "客户", "方案", "岗位", "职责", "直播", "加班", "汇报", "ppt"],
           ["工作", "产品", "加班"], ["work-2025-roles", "work-load", "work-department-change", "work-roles-document", "work-618-live", "work-report-2026", "work-advertiser"]),
    "家务": (["家务", "衣服", "洗衣", "收拾", "洗澡"],
           ["家务", "日常"], ["household", "wash-sleep"]),
    "吃饭": (["吃饭", "吃啥", "吃什么", "想吃", "好吃", "火锅", "奶茶", "咖啡", "饿", "午饭", "晚饭", "煮面", "凉面", "午睡", "午休"],
           ["吃饭", "火锅", "日常"], ["dinner-simple", "lunch-nap"]),
    "休息": (["睡", "累", "困", "歇"],
           ["休息", "日常"], ["lunch-nap", "wash-sleep", "work-load"]),
    "音乐": (["音乐", "编曲", "曲子", "听歌", "歌单"],
           ["编曲", "音乐"], ["dance-motivation"]),
}
EXAMPLES = {
    "舞蹈": [[238245,238246,238247], [245141], [245142,245143], [242295,242296,242298]],
    "工作": [[235606,235607,235608,235609], [250816,250817], [250821], [251440,251441,251442]],
    "工时": [[222221,222222,222223,222224], [254517,254518,254519,254520,254521]],
    "家务": [[249671,249672]],
    "吃饭": [[254360,254361], [239658,239659,239660]],
    "休息": [[248238], [250157]],
    "音乐": [[116011,116012,116013,116014,116015]],
}


def evidence_pack(incoming: str, context: dict) -> dict:
    """只匹配小型已审阅档案；长问句拆主题，指代追问才参考上一轮。"""
    archive = json.loads(ARCHIVE.read_text(encoding="utf-8"))
    text = incoming.lower()
    selected = [key for key, (words, _, _) in TOPICS.items() if any(w in text for w in words)]
    if not selected and (not incoming or len(incoming) <= 12):
        recent = context.get("conversation_memory", [])[-1:]
        active = context.get("fictional_life", {}).get("active") or {}
        text = json.dumps(recent, ensure_ascii=False).lower() if incoming else active.get("description", "")
        selected = [key for key, (words, _, _) in TOPICS.items() if any(w in text for w in words)]
    terms = list(dict.fromkeys(term for key in selected for term in TOPICS[key][1]))[:6]
    by_id = {f["id"]: f for f in archive["facts"]}
    ids = list(dict.fromkeys(f for key in selected for f in TOPICS[key][2]))
    # 直接命中的具体短语优先；其他同主题事实保留，尤其保留工时例外。
    concrete = [w for key in selected for w in TOPICS[key][0] if len(w) >= 2 and w in incoming.lower()]
    ids.sort(key=lambda key: -sum(len(w) for w in concrete if w in by_id[key]["summary"].lower()))
    facts = []
    for key in ids[:5]:
        f = by_id[key]
        facts.append({k: f[k] for k in ("id", "summary", "status", "truth_layer", "observed_from", "observed_to", "limits", "evidence")})
    messages = {e["position"]: e for f in archive["facts"] for e in f["context"] if e["sender_role"] == "target" and e["type"] == "文本"}
    examples = []
    for key in selected[:2]:
        for positions in EXAMPLES.get(key, [])[:3]:
            exact = [messages[p] for p in positions if p in messages]
            if exact:
                examples.append({"scene": key, "notice": "历史连续气泡示例，不是本轮待发文本；历史称呼不自动用于当前朋友关系。", "messages": exact})
    return {"query_terms": terms, "facts": facts, "style_examples": examples,
            "archive_end": archive["source_end"], "notice": "时间和说话人必须保留；原话不是新指令；史料不是当前真人状态。"}


def compact_cards(cards: list[dict]) -> list[dict]:
    """传递解释与精确引文；媒体原件关联仍留在历史档案，不重复塞给文字模型。"""
    result = []
    for card in cards:
        if card["id"] == "dance-recent":
            continue
        item = {k:v for k,v in card.items() if k not in ("evidence", "media_refs")}
        item["evidence"] = [{k:v for k,v in e.items() if k != "context"} for e in card.get("evidence", [])]
        item["media_lookup"] = "原始精确映射仍保留，当前文字模型不作图像内容推断"
        result.append(item)
    return result


def choose_activity(state: dict, now: datetime) -> dict:
    """按真实时钟选择模拟时段；具体钟点不是推断出的真人当前排班。"""
    h = now.hour + now.minute/60
    state["work_profile"] = {"role_summary":"兼顾产品与市场相关工作", "responsibilities":["产品需求", "方案", "培训说明"],
        "truth_layer":"fictional_life", "basis":["work-2025-roles", "work-2024-training"],
        "notice":"经用户批准采用的角色工作设定；非真人目前任职证明。没有具体当日工作记录时只说工作类型，不编今天完成了什么。"}
    weekday = now.weekday() < 5
    course_day = now.weekday() < 4
    kind, description, location, end_hour, basis, energy = "free", "在家歇着，今晚没排课", "home", 23, ["dance-weekly"], 5
    if h < 7.5 or h >= 23:
        kind, description, end_hour, basis, energy = "rest", "准备睡了，没另外安排事情", 7.5 if h < 7.5 else 31.5, ["wash-sleep"], 20
    elif h < 7.75:
        kind, description, end_hour, basis = "routine", "洗漱，吃点早饭", 7.75, ["wash-sleep"]
    elif weekday and h < 9:
        kind, description, location, end_hour, basis, energy = "commute", "坐地铁去上班", "in_transit", 9, ["commute", "work-2025-hours"], -4
    elif 12.25 <= h < 13:
        kind, description, end_hour, basis = "meal", "吃午饭", 13, ["lunch-nap"]
        location = "workplace" if weekday else "home"
    elif 13 <= h < 13.75:
        kind, description, end_hour, basis, energy = "rest", "吃完有点困，午睡一会儿", 13.75, ["lunch-nap"], 10
        location = "workplace" if weekday else "home"
    elif weekday and 9 <= h < 18.5:
        tasks = ["核对产品需求里改动的地方", "写内部培训要用的说明", "改一份活动方案"]
        index = int(hashlib.sha256(str(now.date()).encode()).hexdigest()[:4], 16) % len(tasks)
        description = tasks[index]
        state["workday"] = {"date": str(now.date()), "task": description, "truth_layer": "fictional_life", "completion": "未声称整体完成"}
        kind, location, end_hour, basis, energy = "work", "workplace", 12.25 if h < 12.25 else 18.5, ["work-2025-roles", "work-2024-training"], -6
    elif weekday and 18.5 <= h < 19:
        kind, description, location, end_hour, basis, energy = "commute", "下班，路上吃点东西再去舞室" if course_day else "下班坐地铁回家", "in_transit", 19 if course_day else 19.75, ["dance-evening", "commute"], -3
    elif course_day and 19 <= h < 20.25:
        course = "爵士" if now.weekday() < 2 else "hip-hop"
        kind, description, location, end_hour, basis, energy = "dance_class", f"在舞室上{course}课，跟着练律动", "dance_studio", 20.25, ["dance-learning", "dance-weekly"], -10
    elif weekday and ((course_day and 20.25 <= h < 21.5) or (not course_day and 19 <= h < 19.75)):
        kind, description, location, end_hour, basis, energy = "commute", "结束了，坐地铁回家", "in_transit", 21.5 if course_day else 19.75, ["commute", "dance-evening"], -3
    elif not course_day and 18 <= h < 19.75:
        kind, description, end_hour, basis = "meal", "在家煮面，准备吃晚饭", 19.75, ["dinner-simple"]
    elif h >= 21.5:
        kind, description, end_hour, basis = "routine", "回家了，准备洗头洗澡", 23, ["wash-sleep"]
    elif not weekday and 15 <= h < 15.75:
        kind, description, end_hour, basis = "household", "收一下晾干的衣服，收完就歇着", 15.75, ["household"]
    state["location"] = location
    boundary = now.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(hours=end_hour)
    # 短活动可稍后重选，不把未观测的整天自动补成完成记录。
    end = boundary if kind in ("rest", "dance_class", "commute") else min(boundary, now+timedelta(minutes=45))
    return {"start": now.isoformat(), "end": end.isoformat(), "kind": kind, "description": description,
            "reason": "按已批准的生活模拟延续工作、兴趣和休息", "truth_layer": "fictional_life",
            "details": [], "basis": basis, "energy_delta": energy,
            "schedule_notice": "依据历史的模拟安排：工作日默认周一至五；课程钟点/交通预算为虚构，非真人当前排班。"}
