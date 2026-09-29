"""微信交互走Mobilerun；ADB用于设置/诊断，scrcpy仅管理物理屏幕。"""

import base64
import json
import os
import re
import subprocess
import time
from pathlib import Path

from bridge_state import UncertainState, appended, atomic_json, now_cst

PREFIX = "com.tencent.mm:id/"
SELECT_TO_SPEAK = "com.google.android.marvin.talkback/com.google.android.accessibility.selecttospeak.SelectToSpeakService"
SETTINGS = [("secure", "enabled_accessibility_services"), ("secure", "default_input_method"),
            ("secure", "enabled_input_methods"), ("global", "stay_on_while_plugged_in")]


def walk(node):
    if not isinstance(node, dict):
        return
    yield node
    for child in node.get("children", []):
        yield from walk(child)


def find(ui, rid, text=None):
    return [node for node in walk(ui.get("a11y_tree", {})) if node.get("resourceId") == PREFIX+rid
            and (text is None or node.get("text") == text) and node.get("isVisibleToUser", True)]


def messages(ui):
    result = []
    for row in find(ui, "bn1"):
        descendants = list(walk(row))
        avatars = [n for n in descendants if n.get("resourceId") == PREFIX+"bk1"]
        if len(avatars) != 1:
            continue  # 顶部裁切行或系统行不能当作完整新收件。
        avatar = avatars[0]
        bounds = avatar.get("boundsInScreen", {})
        direction = "in" if bounds.get("right", 9999) < 540 else "out" if bounds.get("left", 0) > 540 else "unknown"
        bodies = [n for n in descendants if n.get("resourceId") == PREFIX+"bkl"]
        kind, text = ("text", bodies[0].get("text", "")) if len(bodies) == 1 else ("media", "")
        labels = [n.get("text") or n.get("contentDescription") for n in descendants
                  if n.get("resourceId") not in (PREFIX+"bk1", PREFIX+"br1")
                  and (n.get("text") or n.get("contentDescription"))]
        voices=[n for n in descendants if n.get("resourceId")==PREFIX+"brp"]
        if len(voices)==1:
            kind,text="media",""
            duration=re.search(r"语音\d+秒",voices[0]["contentDescription"])
            seconds=[n.get("text","").strip().rstrip('"') for n in bodies if re.fullmatch(r'\d+"',n.get("text","").strip())]
            labels=[duration.group(0) if duration else "语音"+seconds[0]+"秒" if len(seconds)==1 else "语音"]
        if kind == "text":
            body_bounds = bodies[0].get("boundsInScreen", {})
            if (direction == "in" and bounds.get("right", 9999) > body_bounds.get("left", -1)) or (
                    direction == "out" and body_bounds.get("right", 9999) > bounds.get("left", -1)):
                direction = "unknown"
        item = {"direction": direction, "kind": kind, "text": text,
                "sender": avatar.get("contentDescription", ""), "media_labels": labels if kind != "text" else []}
        item["signature"] = json.dumps(item, ensure_ascii=False, sort_keys=True)
        result.append(item)
    return result


def sequence(ui):
    return [row["signature"] for row in messages(ui)]


class Phone:
    def __init__(self, config, runtime):
        self.config, self.runtime = config, Path(runtime)
        self.device = None
        self.screen_power = None
        self.recovery_needed = False
        self.transport_id = None

    def adb_command(self, *args):
        return subprocess.run([self.config["adb_path"], *args],
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=20,
            check=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).stdout.strip()

    def adb(self, *args):
        target = self.config.get("adb_target") or self.config["device_serial"]
        return self.adb_command("-s", target, *args)

    def connect_device(self):
        os.environ["PATH"] = str(Path(self.config["adb_path"]).parent)+os.pathsep+os.environ["PATH"]
        from mobilerun_core import Mobilerun
        target = self.config.get("adb_target") or self.config["device_serial"]
        self.device = Mobilerun().connect(target, backend="local-android-adb", portal_mode="required")
        self.capabilities = self.device.capabilities
        self.recovery_needed = True

    def ensure_transport(self, recover=False):
        """仅在轮询边界重建已绑定LAN通道，不重放中断的手机动作。"""
        target = self.config.get("adb_target")
        if not target:
            return
        if (self.runtime/"stop.json").exists():
            raise UncertainState("已设置停止标志，不恢复局域网连接")

        def epoch():
            listing = self.adb_command("devices", "-l")
            for line in listing.splitlines():
                fields = line.split()
                if len(fields) >= 2 and fields[:2] == [target, "device"]:
                    return next((field.split(":", 1)[1] for field in fields if field.startswith("transport_id:")), None)
            return None

        observed = epoch()
        disconnected = observed is None
        if observed is None and recover:
            self.adb_command("connect", target)
            observed = epoch()
        if observed is None:
            raise UncertainState("局域网设备不可用，等待下一轮重连；不重试发送")
        if observed == self.transport_id and not disconnected:
            return
        if not recover:
            raise UncertainState("局域网连接已变化，等待下一轮核对绑定会话；中断本次操作")
        serial = self.adb("shell", "getprop", "ro.serialno")
        if serial != self.config["device_serial"]:
            raise UncertainState("局域网设备硬件身份不符，停止手机操作")
        if self.screen_power:
            self.screen_power.close()
        self.connect_device()
        self.transport_id = observed
        atomic_json(self.runtime/"transport.json", {"status":"connected", "at":now_cst().isoformat(),
            "adb_target":target, "device_serial":serial, "transport_id":observed, "model_calls":0})

    def setup(self):
        if self.config.get("adb_target"):
            self.ensure_transport(recover=True)
        backup = self.runtime / "phone-settings.json"
        if not backup.exists():
            atomic_json(backup, {f"{ns}/{key}": self.adb("shell", "settings", "get", ns, key) for ns, key in SETTINGS})
        enabled = self.adb("shell", "settings", "get", "secure", "enabled_accessibility_services")
        services = [] if enabled in ("", "null") else enabled.split(":")
        if SELECT_TO_SPEAK not in services:
            self.adb("shell", "settings", "put", "secure", "enabled_accessibility_services", ":".join(services+[SELECT_TO_SPEAK]))
        self.adb("shell", "settings", "put", "global", "stay_on_while_plugged_in", "3")
        if self.adb("shell", "settings", "get", "secure", "touch_exploration_enabled") != "0":
            raise UncertainState("触摸探索已开启，不能继续常规操作")
        if not self.device:
            self.connect_device()
        if self.config.get("screen_off_enabled"):
            from screen_power import ScreenPower
            self.screen_power = ScreenPower(self.config,self.runtime,self.adb)
            self.screen_power.wake = self.wake_if_unlocked
            self.screen_power.ensure()

    def restore(self):
        if self.screen_power:
            self.screen_power.close()
        path = self.runtime/"phone-settings.json"
        if path.exists():
            if (self.config.get("adb_target") and
                    self.adb("shell", "getprop", "ro.serialno") != self.config["device_serial"]):
                raise UncertainState("局域网设备硬件身份不符，不恢复手机设置")
            saved = json.loads(path.read_text(encoding="utf-8"))
            for entry, value in saved.items():
                ns, key = entry.split("/", 1)
                if value == "null":
                    self.adb("shell", "settings", "delete", ns, key)
                else:
                    self.adb("shell", "settings", "put", ns, key, value)

    def require_unlocked(self):
        policy = self.adb("shell", "dumpsys", "window", "policy")
        section = policy.partition("KeyguardServiceDelegate")[2]
        for key in ("showing", "secure", "inputRestricted", "mSimSecure"):
            values = re.findall(r"(?m)^\s*"+key+r"=(true|false)\s*$", section)
            if values != ["false"]:
                raise UncertainState("存在安全锁屏或锁屏状态不明，需要在手机上解锁；不自动绕过")

    def wake_if_unlocked(self):
        if (self.runtime/"stop.json").exists():
            raise UncertainState("已设置停止标志，不自动唤醒")
        self.require_unlocked()
        self.device.key("wakeup")  # 明确唤醒，不能使用会反向休眠的power切换键。
        deadline = time.monotonic()+5
        while "mWakefulness=Awake" not in self.adb("shell", "dumpsys", "power"):
            if time.monotonic() >= deadline:
                raise UncertainState("唤醒后系统仍在休眠，停止恢复")
            time.sleep(.25)
        self.require_unlocked()
        self.recovery_needed = True
        atomic_json(self.runtime/"phone-recovery.json", {"status":"awake_verified", "at":now_cst().isoformat()})

    def observe(self, recover=False):
        self.ensure_transport(recover=recover)
        if self.screen_power:
            self.screen_power.ensure()
        if self.recovery_needed:
            if not recover:
                raise UncertainState("手机已唤醒，等待下一轮核对绑定会话；中断本次操作")
            if (self.runtime/"stop.json").exists():
                raise UncertainState("已设置停止标志，不恢复会话")
            self.require_unlocked()
            self.recovery_needed = False
            try:
                self.device.start_app("com.tencent.mm")
                time.sleep(.5)
                state = self.verify_identity()
            except Exception:
                self.recovery_needed = True
                raise
            atomic_json(self.runtime/"phone-recovery.json", {"status":"identity_verified", "at":now_cst().isoformat(),
                        "contact_wechat_id":self.config["contact_wechat_id"]})
            return state
        state = self.device.ui()
        if state.dead_tree or not state.nodes:
            raise UncertainState("微信无障碍树不可用；检查解锁及无障碍服务")
        if state.get("phone_state", {}).get("package_name") != "com.tencent.mm":
            raise UncertainState("微信不在前台，暂停手机操作")
        return state

    def tap(self, state, rid, text=None):
        nodes = find(state, rid, text)
        if len(nodes) != 1:
            raise UncertainState(f"控件{rid}不是唯一可见节点")
        node = nodes[0]
        box = node["boundsInScreen"]
        root = state["a11y_tree"]["boundsInScreen"]
        x, y = (box["left"]+box["right"])//2, (box["top"]+box["bottom"])//2
        if not (root["left"] <= x < root["right"] and root["top"] <= y < root["bottom"]):
            raise UncertainState("控件不在真实屏幕内")
        # SDK screen_size误用2147高；实际root为2400，使用已观测bounds。
        self.device.tap(x, y)
        time.sleep(.5)
        return self.observe()

    def verify_identity(self):
        if self.recovery_needed:
            return self.observe(recover=True)
        state = self.observe()
        if state.get("phone_state", {}).get("activity_name") == "com.tencent.mm.ui.LauncherUI":
            state = self.tap(state, "jha")
            if not find(state, "d98"):
                raise UncertainState("没有进入微信本地搜索页")
            self.device.type(self.config["contact_name"], clear=True, stealth=False)
            time.sleep(.5)
            state = self.observe()
            state = self.tap(state, "odf", self.config["contact_name"])
        if state.get("phone_state", {}).get("activity_name") != "com.tencent.mm.ui.chatting.ChattingUI":
            raise UncertainState("当前不是私聊页，不能确定发送对象")
        state = self.tap(state, "fq")
        if state.get("phone_state", {}).get("activity_name") != "com.tencent.mm.ui.SingleChatInfoUI":
            raise UncertainState("不是单聊信息页，停止发送")
        state = self.tap(state, "m7b", self.config["contact_name"])
        identifiers = find(state, "cff")
        if len(identifiers) != 1 or identifiers[0].get("text", "").replace(" ", "") != "微信号:"+self.config["contact_wechat_id"]:
            raise UncertainState("资料页微信号不符，停止发送")
        if not find(state, "cf8", self.config["contact_name"]):
            raise UncertainState("资料页备注不符")
        self.identity_evidence = state
        state = self.tap(state, "o3b", "发消息")
        if not find(state, "bkk"):
            raise UncertainState("回到私聊后没有输入框")
        return state

    def send(self, reply, expected_sequence, before_click, audit):
        state = self.verify_identity()
        audit.retain("identity.json", self.identity_evidence)
        if appended(expected_sequence, sequence(state)):
            raise UncertainState("生成期间收到新消息，不能发送过期草稿")
        fields = find(state, "bkk")
        if len(fields) != 1 or fields[0].get("text"):
            raise UncertainState("输入框已有草稿，不覆盖")
        state = self.tap(state, "bkk")
        self.device.type(reply, clear=False, stealth=False)
        state = self.observe()
        if len(find(state, "bkk", reply)) != 1:
            raise UncertainState("输入内容回读不符，不点击发送")
        if appended(expected_sequence, sequence(state)):
            self.device.clear_input()
            self.observe()
            raise UncertainState("输入期间收到新消息，已取消发送")
        audit.retain("draft.json", state)
        try:
            before_click()  # 落盘后才允许唯一发送动作；未知结果不重试。
        except UncertainState:
            # 输入期间跨过静默边界：只清理本次已回读确认的草稿，不点发送。
            self.device.clear_input()
            self.observe()
            raise
        state = self.tap(state, "bql", "发送")
        if state.get("phone_state", {}).get("keyboard_visible"):
            self.device.key("back")
        state = self.observe()
        added = appended(expected_sequence, sequence(state))
        sent = [json.loads(value) for value in added]
        if not any(row["direction"] == "out" and row["kind"] == "text" and row["text"] == reply for row in sent):
            raise UncertainState("未能确认新右侧气泡；保持sending，禁止自动重发")
        if any(node.get("text") for node in find(state, "bkk")):
            raise UncertainState("发送后输入框非空，结果需要检查")
        audit.retain("sent.json", state)
        audit.retain("sent.png", base64.b64decode(self.device.screenshot()))
        return state
