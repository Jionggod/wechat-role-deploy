"""文字和媒体的逐项发送事务，发送不确定时不自动重试。"""

from bridge_state import UncertainState


def build_parts(event_id: str, draft: dict) -> list[dict]:
    parts=[]
    if draft.get("reply","").strip():
        parts.append({"id":event_id+":text","kind":"text","text":draft["reply"],"status":"queued"})
    if draft.get("media_id"):
        parts.append({"id":event_id+":media","kind":"media","media_id":draft["media_id"],"status":"queued"})
    if not parts:
        raise ValueError("没有可发送内容")
    return parts


def next_part(parts: list[dict]) -> dict | None:
    if any(p["status"] in ("sending","uncertain") for p in parts):
        raise UncertainState("存在发送结果不确定的媒体/文字项，禁止重发")
    return next((p for p in parts if p["status"]=="queued"),None)
