"""真实验证与发送证据，保留输入摘要、哈希与耗时。"""

import hashlib
import json
import time
import uuid
from pathlib import Path

from bridge_state import atomic_json, now_cst


class Audit:
    def __init__(self, root, kind, inputs):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=True)
        self.started = now_cst()
        self.tick = time.monotonic()
        self.id = f"{self.started:%Y%m%dT%H%M%S}-{kind}-{uuid.uuid4().hex[:8]}"
        self.kind, self.inputs, self.files = kind, inputs, []

    def __enter__(self):
        return self

    def retain(self, name, data):
        raw = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False, indent=2, default=str).encode()
        path = self.root / f"{self.id}-{name}"
        with path.open("xb") as stream:
            stream.write(raw)
        self.files.append({"path": str(path), "bytes": len(raw), "sha256": hashlib.sha256(raw).hexdigest()})
        return path

    def __exit__(self, error_type, error, traceback):
        if error:
            self.retain("error.json", {"error": str(error)})
        row = {"taskId": self.id, "taskType": self.kind,
               "inputSummary": json.dumps(self.inputs, ensure_ascii=False, default=str)[:300],
               "inputSha256": hashlib.sha256(json.dumps(self.inputs, sort_keys=True, default=str).encode()).hexdigest(),
               "outputFiles": self.files, "startedAt": self.started.isoformat(),
               "completedAt": now_cst().isoformat(), "durationSeconds": time.monotonic()-self.tick,
               "points": None if self.kind == "model" else 0,
               "pointsNote": "Codex订阅额度不可折算点数；token见生成结果" if self.kind == "model" else "本机操作，无外部计费点数",
               "result": "FAIL" if error else "PASS"}
        with (self.root/"audit.jsonl").open("a", encoding="utf-8") as stream:
            stream.write(json.dumps(row, ensure_ascii=False)+"\n")
        totals = {}
        for line in (self.root/"audit.jsonl").read_text(encoding="utf-8").splitlines():
            entry = json.loads(line)
            total = totals.setdefault(entry["taskType"], {"totalTasks": 0, "totalSeconds": 0,
                "knownPoints": 0, "unknownPointTasks": 0, "failures": 0})
            total["totalTasks"] += 1
            total["totalSeconds"] += entry["durationSeconds"]
            total["knownPoints"] += entry["points"] or 0
            total["unknownPointTasks"] += int(entry["points"] is None)
            total["failures"] += int(entry["result"] == "FAIL")
        for total in totals.values():
            total["pointsPerSecond"] = (None if total["unknownPointTasks"] or not total["totalSeconds"]
                                        else total["knownPoints"]/total["totalSeconds"])
        atomic_json(self.root/"summary.json", totals)
