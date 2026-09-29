"""仅管理已绑定真机的物理屏幕；不锁屏、不发送输入、不采集音视频。"""

import os
from pathlib import Path
import re
import subprocess
import time

from bridge_state import atomic_json, now_cst


class ScreenPower:
    def __init__(self, config, runtime, adb):
        self.config, self.runtime, self.adb = config, Path(runtime), adb
        self.child = None
        self.next_check = 0
        self.wake = None

    def state(self):
        surface = self.adb("shell","dumpsys","SurfaceFlinger")
        system = self.adb("shell","dumpsys","power")
        panels = re.findall(r"powerMode=(\w+)\s*\(\d+\)",surface)
        return {"panel":panels[0] if len(panels)==1 else "Unknown", "awake":"mWakefulness=Awake" in system}

    def publish(self, status, **extra):
        atomic_json(self.runtime/"screen-power.json", {"status":status,"at":now_cst().isoformat(),
            "pid":self.child.pid if self.child else None,"owner_pid":os.getpid(),
            "serial":self.config["device_serial"],
            "adb_target":self.config.get("adb_target") or self.config["device_serial"],"model_calls":0,**extra})

    def ensure(self):
        if self.child and self.child.poll() is None and time.monotonic()<self.next_check:
            return
        try:
            observed=self.state()
            if not observed["awake"]:
                self.close()
                if self.wake is None:
                    raise RuntimeError("手机未保持唤醒，未配置安全恢复入口")
                self.wake()
                observed=self.state()
                if not observed["awake"]:
                    raise RuntimeError("安全恢复后仍未确认系统唤醒")
            if self.child and self.child.poll() is None:
                if observed["panel"]=="Off":
                    self.publish("screen_off",**observed)
                    self.next_check=time.monotonic()+60
                    return
                self.close()
            exe=Path(self.config["scrcpy_path"])
            if not exe.is_file():
                raise RuntimeError("已校验的scrcpy程序不存在")
            self.runtime.mkdir(parents=True,exist_ok=True)
            with (self.runtime/"screen-power.log").open("ab") as output:
                self.child=subprocess.Popen([str(exe),"--serial="+(self.config.get("adb_target") or self.config["device_serial"]),
                    "--no-video","--no-audio","--no-window","--no-clipboard-autosync","--turn-screen-off","--no-power-on"],
                    cwd=exe.parent,env={**os.environ,"ADB":self.config["adb_path"]},stdin=subprocess.DEVNULL,
                    stdout=output,stderr=output,creationflags=subprocess.CREATE_NO_WINDOW)
            deadline=time.monotonic()+12
            while time.monotonic()<deadline:
                if self.child.poll() is not None:
                    raise RuntimeError("屏幕助手提前退出，详情见screen-power.log")
                observed=self.state()
                if not observed["awake"]:
                    raise RuntimeError("手机未保持唤醒，不自动解锁")
                if observed["panel"]=="Off":
                    self.publish("screen_off",**observed)
                    self.next_check=time.monotonic()+60
                    return
                time.sleep(.25)
            raise RuntimeError("未确认物理面板关闭，不继续手机操作")
        except Exception as exc:
            self.close()
            self.publish("error",error=str(exc))
            raise

    def close(self):
        if self.child:
            if self.child.poll() is None:
                self.child.terminate()
                self.child.wait(timeout=10)
            self.child=None
            self.next_check=0
            self.publish("stopped")
