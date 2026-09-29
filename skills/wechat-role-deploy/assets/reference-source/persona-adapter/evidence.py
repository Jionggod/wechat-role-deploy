"""只读跨平台证据抽样、精确引文复核与检索。"""
import argparse
from collections import Counter, defaultdict
from contextlib import closing
from datetime import datetime, timedelta, timezone
import hashlib
import json
from pathlib import Path
import re
import sqlite3

ROOT = Path(__file__).resolve().parents[1]
SOURCES = {
    "qq": ROOT.parent / "data/index.sqlite",
    "wechat": ROOT.parent.parent / "RoleHistory/data/index.sqlite",
}
TOPICS = {
    "兴趣作品": ["喜欢看", "喜欢听", "最喜欢", "柯南", "超炮", "番剧", "动漫", "live", "演唱会", "音乐", "歌曲", "游戏", "雀魂"],
    "饮食偏好": ["喜欢吃", "不爱吃", "不喜欢吃", "想吃", "好吃", "火锅", "奶茶", "咖啡", "甜", "辣"],
    "价值花钱": ["舍不得", "值得", "浪费", "攒钱", "存钱", "预算", "性价比", "太贵", "省钱", "花钱", "买不起"],
    "工作学习": ["工作", "上班", "加班", "同事", "老板", "学习", "考试", "毕业", "面试", "实习", "作业"],
    "边界自主": ["不想去", "不想过去", "我不想", "我不愿意", "我拒绝", "别管", "不要逼", "不舒服", "尊重", "介意"],
    "关系关心": ["担心你", "注意安全", "要注意", "记得吃", "早点睡", "陪你", "陪我", "在乎", "想你", "别熬夜"],
    "冲突修复": ["生气", "吵架", "对不起", "道歉", "误会", "委屈", "冷静", "不理", "讲道理", "沟通"],
    "情绪应对": ["压力", "焦虑", "难受", "害怕", "烦躁", "累了", "开心", "放松", "不开心", "安全感"],
    "社交生活": ["朋友", "社交", "聚会", "一个人", "独处", "出去玩", "不出门", "旅游", "散步", "成都", "公园"],
    "审美表达": ["好看", "可爱", "丑", "颜色", "穿搭", "衣服", "口红", "拍照", "猫", "狗", "表情包"],
    "自我原则": ["我觉得", "我认为", "我希望", "我这个人", "我就是", "对我来说", "我一直", "以前", "现在更", "底线"],
    "共同记忆": ["上次我们", "我们上次", "记不记得", "还记得", "一起去", "那天", "见面", "我们去", "以后一起"],
}


def instant(value):
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone(timedelta(hours=8)))


def bounded_context(rows, index, radius=3, minutes=30):
    center = rows[index]
    return [r for r in rows[max(0, index-radius):index+radius+1]
            if r.get("peer") == center.get("peer")
            and abs((instant(r["time"]) - instant(center["time"])).total_seconds()) <= minutes*60]


def connect(platform):
    db = sqlite3.connect(SOURCES[platform].as_uri() + "?mode=ro", uri=True)
    db.row_factory = sqlite3.Row
    return db


def get_message(platform, message_id):
    with closing(connect(platform)) as db:
        fields = "id,time,sender_role,text" + (",peer,kind" if platform == "qq" else ",type AS kind")
        row = db.execute(f"SELECT {fields} FROM messages WHERE id=?", (message_id,)).fetchone()
        if row is None:
            raise ValueError(f"消息不存在：{platform}:{message_id}")
        return dict(row)


def source_media(platform, message_id):
    """只按消息/元素关联，不能凭相邻文字或同名文件猜媒体归属。"""
    with closing(connect(platform)) as db:
        if platform == "qq":
            sql = """SELECT e.id AS element_id,e.kind,l.asset_id,l.variant,l.status,a.archive_path
                     FROM elements e LEFT JOIN media_links l ON e.id=l.element_id
                     LEFT JOIN assets a ON l.asset_id=a.id WHERE e.message_id=? AND e.kind!='text'"""
        else:
            sql = """SELECT '' AS element_id,l.kind,l.asset_id,'' AS variant,l.status,a.archive_path
                     FROM media_links l LEFT JOIN assets a ON l.asset_id=a.id WHERE l.message_id=?"""
        links, seen = [], set()
        for row in db.execute(sql, (message_id,)):
            item = dict(row, platform=platform, message_id=message_id)
            key = (item["element_id"], item["asset_id"], item["variant"], item["status"])
            if key in seen:
                continue
            seen.add(key)
            item["exists"] = bool(item["archive_path"] and Path(item["archive_path"]).is_file())
            item["annotations"] = [dict(a) for a in db.execute(
                "SELECT kind,text,reviewed,engine FROM annotations WHERE asset_id=? AND reviewed=1", (item["asset_id"],))] if item["asset_id"] else []
            item["notice"] = "语境邻接不等于内容标注；实际呈现前仍须查看原件，动画首帧不代表全片。"
            links.append(item)
        return links


def verify_card(card, getter=get_message):
    errors = []
    for ev in card.get("evidence", []):
        try:
            row = getter(ev["platform"], ev["id"])
            if row["sender_role"] != "target" or row["text"] != ev["quote"]:
                errors.append(f"{card['id']} 引文或说话人不符：{ev['id']}")
        except (ValueError, KeyError) as exc:
            errors.append(f"{card['id']}：{exc}")
    if not card.get("evidence"):
        errors.append(f"{card['id']} 缺少证据")
    return errors


def scan():
    selected = []
    counts = {}
    for platform in SOURCES:
        with closing(connect(platform)) as db:
            if platform == "qq":
                sql = "SELECT id,time,sender_role,text,peer,kind FROM messages ORDER BY peer,timestamp,id"
            else:
                sql = "SELECT id,time,sender_role,text,'' AS peer,type AS kind FROM messages ORDER BY position"
            rows = [dict(r, platform=platform) for r in db.execute(sql)]
        counts[platform] = {"messages": len(rows), "target_text_by_year": dict(Counter(
            r["time"][:4] for r in rows if r["sender_role"] == "target" and r["kind"] in ("text", "文本")))}
        groups = defaultdict(list)
        for index, row in enumerate(rows):
            if row["sender_role"] != "target" or row["kind"] not in ("text", "文本"):
                continue
            txt = row["text"] or ""
            if not 8 <= len(txt) <= 240 or re.search(r"https?://|<msg|转发", txt):
                continue
            for topic, words in TOPICS.items():
                hit = [w for w in words if w.casefold() in txt.casefold()]
                if hit:
                    score = len(hit)*3 + min(len(txt), 60)/30 + 2*bool(re.search(r"我(不|喜欢|觉得|想|认为|希望)", txt))
                    groups[(topic, row["time"][:4])].append((score, index))
        for (topic, year), candidates in sorted(groups.items()):
            used_days = set()
            for score, index in sorted(candidates, key=lambda pair: (-pair[0], rows[pair[1]]["time"])):
                row = rows[index]
                if row["time"][:10] in used_days:
                    continue
                selected.append(dict(row, topic=topic, candidate_count=len(candidates), context=bounded_context(rows, index)))
                used_days.add(row["time"][:10])
                if len(used_days) == 3:
                    break
    for i, row in enumerate(selected, 1):
        row["ref"] = f"E{i:03}"
        row["quote_sha256"] = hashlib.sha256(row["text"].encode()).hexdigest()
    output = {"notice": "全库文字筛选，不是全库语义审阅；候选需人工审核。", "counts": counts, "candidates": selected}
    dest = ROOT / "data"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "candidates.json").write_text(json.dumps(output, ensure_ascii=False, indent=2), encoding="utf-8")
    for topic in TOPICS:
        chunks = []
        for row in selected:
            if row["topic"] != topic:
                continue
            lines = [f"\n{row['ref']} {row['platform']} {row['time']} | {row['text']}"]
            lines += [f"  {'*' if ctx['id'] == row['id'] else ' '} {ctx['time']} {ctx['sender_role']}: {ctx['text']}" for ctx in row["context"]]
            chunks.append("\n".join(lines))
        (dest / f"review-{topic}.txt").write_text("\n".join(chunks), encoding="utf-8")
    print(json.dumps({"candidates": len(selected), "counts": counts}, ensure_ascii=False))


def build():
    candidates = json.loads((ROOT / "data/candidates.json").read_text(encoding="utf-8"))["candidates"]
    by_ref = {e["ref"]: e for e in candidates}
    drafts = json.loads((ROOT / "data/card-drafts.json").read_text(encoding="utf-8"))
    cards = []
    for draft in drafts:
        card = dict(draft, evidence=[])
        for ref in draft["refs"]:
            row = by_ref[ref]
            card["evidence"].append({"ref": ref, "platform": row["platform"], "id": row["id"],
                "time": row["time"], "quote": row["text"], "quote_sha256": row["quote_sha256"], "context": row["context"]})
        errors = verify_card(card)
        if errors:
            raise ValueError("\n".join(errors))
        card["truth_layer"] = "historical_interpretation"
        card["media_refs"] = []
        seen_messages = set()
        for ev in card["evidence"]:
            for ctx in ev["context"]:
                key = (ctx["platform"], ctx["id"])
                if key in seen_messages:
                    continue
                seen_messages.add(key)
                for link in source_media(*key):
                    card["media_refs"].append(dict(link, sender_role=ctx["sender_role"], time=ctx["time"], relation="context_neighbor_not_same_message" if ctx["id"] != ev["id"] else "same_message"))
        cards.append(card)
    (ROOT / "data/cards.json").write_text(json.dumps(cards, ensure_ascii=False, indent=2), encoding="utf-8")
    lines = ["# AI角色 · 人格与记忆证据卡\n", "历史结论与模拟生活分开；以下是逐项审核后的解释，不是模型权重训练。完整引文、ID、上下文和媒体映射见 data/cards.json。\n"]
    for card in cards:
        lines += [f"## {card['id']} · {card['title']}\n", card["summary"]+"\n", f"对话应用：{card['application']}\n", f"限定：{card['limits']}\n", f"证据强度：{card['confidence']}；事件类型：{card.get('event_status', 'personality_interpretation')}\n"]
        for ev in card["evidence"]:
            lines += [f"- {ev['ref']} / {ev['platform']} / {ev['time']} / `{ev['id']}`：{ev['quote'].replace(chr(10), ' / ')}"]
        lines.append("")
    (ROOT / "docs/persona-evidence.md").write_text("\n".join(lines), encoding="utf-8")
    print(json.dumps({"cards": len(cards), "verified_quotes": sum(len(c["evidence"]) for c in cards)}, ensure_ascii=False))


def query(text, limit=5):
    cards = json.loads((ROOT / "data/cards.json").read_text(encoding="utf-8"))
    words = [w for w in re.split(r"[\s，,、]+", text) if w]
    ranked = sorted(cards, key=lambda c: sum(w.casefold() in json.dumps(c, ensure_ascii=False).casefold() for w in words), reverse=True)
    result = [c for c in ranked if any(w.casefold() in json.dumps(c, ensure_ascii=False).casefold() for w in words)]
    return result[:limit]


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["scan", "build", "verify", "query"])
    parser.add_argument("--query", default="")
    parser.add_argument("--limit", type=int, default=5)
    args = parser.parse_args()
    if args.action == "scan":
        scan()
    elif args.action == "build":
        build()
    elif args.action == "verify":
        cards = json.loads((ROOT / "data/cards.json").read_text(encoding="utf-8"))
        errors = [e for c in cards for e in verify_card(c)]
        print(json.dumps({"cards": len(cards), "errors": errors}, ensure_ascii=False))
        raise SystemExit(bool(errors))
    else:
        print(json.dumps(query(args.query, args.limit), ensure_ascii=False, indent=2))
