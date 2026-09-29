"""哈希命名媒体准备与微信相册选择，原件不转码。"""

import hashlib
from pathlib import Path

from bridge_state import UncertainState


def stage(phone, asset, audit):
    """Prepare one allowlisted asset on shared storage; never delete phone files."""
    path=Path(asset["path"])
    digest=hashlib.sha256(path.read_bytes()).hexdigest()
    if digest!=asset["sha256"] or path.suffix.lower() not in (".jpg",".jpeg",".png",".gif"):
        raise ValueError("素材内容或格式与已审阅记录不符")
    folder="/sdcard/Pictures/RoleAI"
    filename=digest+path.suffix.lower()
    target=folder+"/"+filename
    phone.adb("shell","mkdir","-p",folder)
    # ADB仅作资产部署；所有微信界面操作仍通过Mobilerun。
    phone.adb("push",str(path),target)
    remote=phone.adb("shell","sha256sum",target).split()[0]
    if remote!=digest:
        raise UncertainState("手机素材校验不一致")
    phone.adb("shell","touch",target)
    phone.adb("shell","am","broadcast","-a","android.intent.action.MEDIA_SCANNER_SCAN_FILE","-d","file://"+target)
    audit.retain("staged.json",{"media_id":asset["id"],"path":target,"bytes":path.stat().st_size,
                                "sha256":remote,"frames":asset.get("frames",1),"original_unchanged":True})
    return filename
