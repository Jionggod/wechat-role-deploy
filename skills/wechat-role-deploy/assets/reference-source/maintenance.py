"""单入口维护：不调用模型/手机，不解除暂停，不重发不确定事务。"""

from datetime import datetime
import json
from pathlib import Path
import re
import subprocess
import tempfile

from bridge_state import atomic_json, now_cst

ROOT = Path(__file__).resolve().parent


def read_json(path: Path) -> dict:
    """Read a required object; missing or malformed state must stop recovery."""
    value = json.loads(path.read_text(encoding="utf-8-sig"))
    if not isinstance(value, dict):
        raise ValueError(f"状态文件不是对象：{path.name}")
    return value


def process_ids(root: Path) -> list[int]:
    """Match the exact bridge run --send command, not an unrelated Python PID."""
    command = "$ErrorActionPreference='Stop'; [Console]::OutputEncoding=[Text.UTF8Encoding]::new(); "
    command += "@(Get-CimInstance Win32_Process | Where-Object {$_.Name -eq 'python.exe'} | Select-Object ProcessId,ParentProcessId,ExecutablePath,CommandLine) | ConvertTo-Json -Compress"
    output = subprocess.run(["powershell.exe", "-NoProfile", "-Command", command], capture_output=True,
                            text=True, encoding="utf-8", check=True, timeout=20,
                            creationflags=subprocess.CREATE_NO_WINDOW).stdout
    rows = json.loads(output or "[]")
    if isinstance(rows, dict):
        rows = [rows]
    script = re.escape(str(root/"run.py").replace("\\", "/"))
    pattern = rf'(?<!\S)"?{script}"?\s+run\s+--send(?:\s|$)'
    matched = [row for row in rows if re.search(pattern, (row.get("CommandLine") or "").replace("\\", "/"), re.I)]
    # Windows venv的Scripts/python.exe只是启动器，实际解释器子进程才写心跳。
    launchers = {row["ProcessId"] for row in matched
                 if (row.get("ExecutablePath") or "").replace("\\", "/").lower().endswith("/scripts/python.exe")
                 and any(child.get("ParentProcessId") == row["ProcessId"]
                         and child.get("ExecutablePath")
                         and child["ExecutablePath"].lower() != row["ExecutablePath"].lower()
                         for child in matched)}
    return [row["ProcessId"] for row in matched if row["ProcessId"] not in launchers]


def inspect(root: Path, pids: list[int], now: datetime) -> dict:
    """Classify disk and process evidence without changing service state."""
    runtime = root/"runtime"
    state = read_json(runtime/"state.json")
    status = read_json(runtime/"status.json")
    for pending in (state.get("pending"), status.get("pending")):
        if pending and (pending.get("status") in ("sending", "uncertain") or
                        any(p.get("status") in ("sending", "uncertain") for p in pending.get("parts", []))):
            return {"status":"attention", "reason":"发送结果不确定，禁止恢复或重发，请核对气泡", "task_id":pending.get("id")}
    if (runtime/"stop.json").exists() or status.get("status") == "stopped" or state.get("paused") == "用户通过微信要求停止":
        if pids:
            return {"status":"attention", "reason":"已要求停止但仍有监听进程；本次不启动或重发，请核对停机状态"}
        return {"status":"paused"}
    if state.get("paused"):
        return {"status":"attention", "reason":str(state["paused"])}
    if not pids:
        return {"status":"needs_start"}
    if pids != [status.get("pid")]:
        return {"status":"attention", "reason":"运行进程身份与状态PID不一致或存在多个进程"}
    at = datetime.fromisoformat(status["at"])
    if at.tzinfo is None or not -30 <= (now-at).total_seconds() <= 600:
        return {"status":"attention", "reason":"进程心跳陈旧或时间无效，保留状态，不重复启动"}
    if status.get("error") or status.get("status") == "blocked":
        return {"status":"attention", "reason":str(status.get("error") or "桥接处于阻塞状态")[:500]}
    if status.get("status") not in ("listening", "ready", "generating", "queued", "drafted"):
        return {"status":"attention", "reason":"未知进程状态，不触发主动请求"}
    return {"status":"healthy"}


def start(root: Path) -> None:
    """Delegate recovery and durable proactive requests to the existing entry."""
    # Windows后台子进程可能继承PIPE，communicate超时后仍等不来EOF；用文件接收输出。
    with tempfile.TemporaryFile() as output:
        subprocess.run(["powershell.exe", "-NoProfile", "-ExecutionPolicy", "Bypass", "-File",
                        str(root/"start.ps1"), "-CheckProactive"], stdout=output, stderr=output,
                       stdin=subprocess.DEVNULL, check=True, timeout=50,
                       creationflags=subprocess.CREATE_NO_WINDOW)


def report(root: Path, result: dict) -> dict:
    """Persist a small alert fingerprint; unchanged failures do not notify again."""
    path = root/"runtime/maintenance-last.json"
    previous = read_json(path) if path.exists() else {}
    changed = previous.get("result") != result
    notify = result["status"] == "attention" and changed
    if changed:
        atomic_json(path, {"at":now_cst().isoformat(), "result":result})
    return {**result, "notify":notify}


def check(root: Path = ROOT, now: datetime | None = None) -> dict:
    """Check once; only an allowed service can reach the startup script."""
    try:
        result = inspect(root, process_ids(root), now or now_cst())
        if result["status"] in ("healthy", "needs_start"):
            start(root)
            result = inspect(root, process_ids(root), now or now_cst())
            if result["status"] == "needs_start":
                result = {"status":"attention", "reason":"服务恢复失败：启动后没有匹配的监听进程"}
    except (OSError, ValueError, KeyError, TypeError, subprocess.SubprocessError) as exc:
        result = {"status":"attention", "reason":f"巡检/恢复失败：{type(exc).__name__}：{str(exc)[:350]}"}
    return report(root, result)


if __name__ == "__main__":
    print(json.dumps(check(), ensure_ascii=False, separators=(",", ":")))
