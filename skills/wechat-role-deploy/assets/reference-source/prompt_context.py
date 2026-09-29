"""仅精简模型输入副本；不删记忆，不改审计与校验上下文。"""

from copy import deepcopy


def compact_context(context: dict) -> dict:
    """Remove exact aliases while keeping every distinct value intact."""
    result = deepcopy(context)
    timeline = result.get("life_timeline")
    if not isinstance(timeline, dict):
        return result
    life = result.get("fictional_life", {})
    for key in ("active", "feeling"):
        if key in timeline and key in life and timeline[key] == life[key]:
            del timeline[key]
    if "recent_fictional_events" in result and result["recent_fictional_events"] == timeline.get("recent_events"):
        del result["recent_fictional_events"]
    if "open_loops" in result and result["open_loops"] == timeline.get("open_loops"):
        del timeline["open_loops"]
    memory = timeline.get("conversation_memory")
    merged = result.get("conversation_memory")
    if isinstance(memory, list) and isinstance(merged, list) and (not memory or merged[-len(memory):] == memory):
        del timeline["conversation_memory"]
    return result
