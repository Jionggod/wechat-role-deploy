"""持久游标与主动机会；不接触手机或模型。"""

import hashlib
import json
import os
import random
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

CST = timezone(timedelta(hours=8), "Asia/Shanghai")


class UncertainState(RuntimeError):
    """身份、顺序或发送结果无法确定，必须保留状态。"""


def now_cst():
    return datetime.now(CST)


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(dir=path.parent, prefix=".writing-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


class State:
    def __init__(self, path):
        self.path = Path(path)
        self.data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.exists() else {
            "sequence": [], "pending": None, "last_dialogue": None,
            "awaiting_reply": False, "handled_slots": [], "paused": None, "counter": 0,
        }

    def save(self):
        atomic_json(self.path, self.data)


def appended(previous, current):
    """Use suffix/prefix overlap, never coordinates or text alone as an ID."""
    if previous == current:
        return []
    if not previous or not current:
        raise UncertainState("消息序列为空，不能推定新收件")
    for count in range(min(len(previous), len(current)), 0, -1):
        matches = [index for index in range(len(current)-count+1) if previous[-count:] == current[index:index+count]]
        if len(matches) > 1:
            raise UncertainState("重复内容使消息锚点不唯一，停止发送")
        if matches:
            return current[matches[0]+count:]
    raise UncertainState("消息序列没有连续锚点；可能滚动、切换会话或漏读，停止发送")


def daily_plan(now, salt, minimum=6, maximum=10, start_hour=9, end_hour=24):
    """Deterministic daily jitter survives restarts; entries are opportunities."""
    seed = hashlib.sha256(f"{now.astimezone(CST).date()}:{salt}".encode()).digest()
    rng = random.Random(seed)
    count = rng.randint(minimum, maximum)
    start = now.astimezone(CST).replace(hour=start_hour, minute=0, second=0, microsecond=0)
    width = (end_hour-start_hour) * 3600 / count
    return [(start + timedelta(seconds=int(width * (index + rng.uniform(.15, .85))))).isoformat()
            for index in range(count)]


def cooldown_minutes(now):
    return 45 + int(hashlib.sha256(str(now.astimezone(CST).date()).encode()).hexdigest()[:2], 16) % 31


def configured_plan(now, config):
    """Exclude pre-activation slots without resetting persistent counters."""
    slots = daily_plan(now, config.get("contact_wechat_id", "test"),
                       config.get("proactive_min", 6), config.get("proactive_max", 10),
                       config.get("active_start_hour", 9), config.get("active_end_hour", 24))
    effective = config.get("coaching_effective_at")
    if effective and datetime.fromisoformat(effective).astimezone(CST).date() == now.astimezone(CST).date():
        slots = [slot for slot in slots if datetime.fromisoformat(slot) > datetime.fromisoformat(effective)]
    return slots


def proactive_gate(data, now, start_hour=9, end_hour=24):
    now = now.astimezone(CST)
    if not start_hour <= now.hour < end_hour:
        return "夜间静默"
    if data.get("paused"):
        return "已暂停"
    if data.get("pending"):
        return "先处理收件或发送中的任务"
    if data.get("awaiting_reply"):
        return "上条主动话题未回应"
    if data.get("last_dialogue"):
        elapsed = (now - datetime.fromisoformat(data["last_dialogue"])).total_seconds()
        if elapsed < cooldown_minutes(now)*60:
            return "最近对话冷却"
    return "ready"
