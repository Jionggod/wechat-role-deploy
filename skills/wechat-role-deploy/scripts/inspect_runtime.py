"""只读落盘摘要；不操作手机、网络或模型，不输出聊天正文。"""

import argparse
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path

CST = timezone(timedelta(hours=8), "Asia/Shanghai")


def read_object(path):
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError("状态必须为对象")
    return value


def inspect(root, now=None):
    root = Path(root).resolve()
    now = now or datetime.now(CST)
    runtime = root/"runtime"
    try:
        config = read_object(root/"config.json")
        state = read_object(runtime/"state.json")
        bridge = read_object(runtime/"status.json")
        clock = read_object(runtime/"local-runtime-status.json")
        for pending in (state.get("pending"), bridge.get("pending")):
            if pending and (pending.get("status") in {"sending", "uncertain"} or any(
                    part.get("status") in {"sending", "uncertain"} for part in pending.get("parts", []))):
                return {"status":"attention", "reason":"发送结果未确认，不恢复或重发", "task_id":pending.get("id")}
        if (any((runtime/name).exists() for name in ("stop.json", "local-runtime-stop.json")) or
                state.get("paused") or bridge.get("status") == "stopped"):
            return {"status":"paused", "reason":"存在停止或暂停状态，保持原状"}
        if not config.get("send_enabled"):
            return {"status":"paused", "reason":"发送开关未启用"}
        ages = {}
        for name, row in (("bridge", bridge), ("clock", clock)):
            stamp = datetime.fromisoformat(row["at"])
            if stamp.tzinfo is None:
                raise ValueError("心跳没有时区")
            age = (now-stamp).total_seconds()
            if not -30 <= age <= 120:
                return {"status":"attention", "reason":name+"心跳陈旧或来自未来"}
            ages[name] = round(age, 1)
        if bridge.get("status") not in {"listening", "ready", "generating", "queued", "drafted"} or bridge.get("error"):
            return {"status":"attention", "reason":"监听状态异常，查看本机status错误摘要"}
        if clock.get("status") != "running" or clock.get("error"):
            return {"status":"attention", "reason":"生活时钟未正常运行"}
        if config.get("screen_off_enabled"):
            screen = read_object(runtime/"screen-power.json")
            screen_at = datetime.fromisoformat(screen["at"])
            if (screen_at.tzinfo is None or not -30 <= (now-screen_at).total_seconds() <= 120 or
                    screen.get("status") != "screen_off" or screen.get("panel") != "Off" or
                    screen.get("awake") is not True or screen.get("owner_pid") != bridge.get("pid")):
                return {"status":"attention", "reason":"熄屏记录陈旧或与监听归属不符"}
        return {"status":"active", "bridge":bridge["status"], "clock":clock["status"],
                "heartbeat_age_seconds":ages, "model_calls":0,
                "verification_scope":"仅落盘状态；还需核对真实进程和必要的设备证据"}
    except (OSError, ValueError, KeyError, TypeError, AttributeError) as exc:
        return {"status":"attention", "reason":"状态缺失或格式无效："+type(exc).__name__}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True, help="既有部署根目录")
    args = parser.parse_args()
    result = inspect(args.root)
    print(json.dumps(result, ensure_ascii=False, separators=(",", ":")))
    raise SystemExit(0 if result["status"] in {"active", "paused"} else 2)


if __name__ == "__main__":
    main()
