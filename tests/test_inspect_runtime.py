"""新skill的只读检查器；不访问生产手机或模型。"""

from datetime import datetime, timedelta, timezone
import importlib.util
import json
from pathlib import Path
import tempfile
import unittest

SCRIPT = Path(__file__).resolve().parents[1]/"skills/wechat-role-deploy/scripts/inspect_runtime.py"
NOW = datetime(2026, 9, 29, 15, 0, tzinfo=timezone(timedelta(hours=8)))


class InspectRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.assertTrue(SCRIPT.is_file(), "尚无不输出聊天的只读状态检查器")
        spec = importlib.util.spec_from_file_location("inspect_runtime", SCRIPT)
        self.module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(self.module)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/"runtime").mkdir()
        self.write("config.json", {"send_enabled":True,"screen_off_enabled":True})
        self.write("runtime/status.json", {"status":"listening","pid":123,"at":NOW.isoformat()})
        self.write("runtime/state.json", {"pending":None,"paused":None,"secret_chat":"不应输出"})
        self.write("runtime/local-runtime-status.json", {"status":"running","pid":124,"at":NOW.isoformat()})
        self.write("runtime/screen-power.json", {"status":"screen_off","owner_pid":123,
            "panel":"Off","awake":True,"at":NOW.isoformat()})

    def write(self, name, data):
        (self.root/name).write_text(json.dumps(data,ensure_ascii=False), encoding="utf-8")

    def inspect(self):
        return self.module.inspect(self.root, NOW)

    def test_active_is_read_only_and_does_not_expose_chat(self):
        before = {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob("*") if p.is_file()}
        result = self.inspect()
        self.assertEqual(result["status"], "active")
        self.assertNotIn("不应输出", json.dumps(result,ensure_ascii=False))
        self.assertEqual(before, {p.relative_to(self.root):p.read_bytes() for p in self.root.rglob("*") if p.is_file()})

    def test_old_clock_is_not_active(self):
        self.write("runtime/local-runtime-status.json", {"status":"running","pid":124,
            "at":(NOW-timedelta(minutes=5)).isoformat()})
        self.assertEqual(self.inspect()["status"], "attention")

    def test_user_stop_is_preserved(self):
        self.write("runtime/stop.json", {"reason":"用户暂停"})
        self.assertEqual(self.inspect()["status"], "paused")
        self.assertTrue((self.root/"runtime/stop.json").exists())

    def test_uncertain_part_blocks_even_if_status_is_listening(self):
        self.write("runtime/state.json", {"pending":{"id":"job-example","status":"drafted",
            "user":"不应输出","parts":[{"status":"sending"}]}})
        result = self.inspect()
        self.assertEqual(result["status"], "attention")
        self.assertEqual(result["task_id"], "job-example")
        self.assertNotIn("不应输出", json.dumps(result,ensure_ascii=False))

    def test_malformed_or_missing_status_is_not_active(self):
        (self.root/"runtime/status.json").write_text("broken",encoding="utf-8")
        self.assertEqual(self.inspect()["status"], "attention")

    def test_future_heartbeat_is_not_active(self):
        self.write("runtime/status.json", {"status":"listening","pid":123,
            "at":(NOW+timedelta(minutes=5)).isoformat()})
        self.assertEqual(self.inspect()["status"], "attention")


if __name__ == "__main__":
    unittest.main()
