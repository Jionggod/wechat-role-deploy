"""只读素材核验、场景候选与逐帧审阅材料。"""

import hashlib
import json
import math
from pathlib import Path

from timeline import local


class MediaCatalog:
    def __init__(self, path: Path):
        self.path=Path(path)
        self.assets={e["id"]:e for e in json.loads(self.path.read_text(encoding="utf-8"))["assets"]} if self.path.exists() else {}

    def resolve(self, key: str) -> dict:
        """Resolve an allowlisted ID, never a model-supplied path."""
        entry=self.assets.get(key)
        if not entry or not entry.get("reviewed") or entry.get("sender_role")!="target" or not entry.get("source_messages"):
            raise ValueError("媒体未审阅、来源不符或没有原始消息")
        if entry.get("review_scope") not in ("static_full_image","full_animation","historical_preview"):
            raise ValueError("媒体审阅范围不足")
        if entry.get("frames",1)>1 and entry["review_scope"]!="full_animation":
            raise ValueError("不能把首帧当完整动画")
        path=Path(entry["path"])
        if not path.is_file():
            raise ValueError("媒体文件缺失")
        with path.open("rb") as stream:
            digest=hashlib.file_digest(stream,"sha256").hexdigest()
        if digest!=entry["sha256"]:
            raise ValueError("媒体哈希与审阅时不同")
        return entry.copy()

    def candidates(self, incoming: str, feeling: str, request_id: str, now, recent: list[dict]) -> list[dict]:
        """Use burst-level opportunities, scene filtering and repeat suppression."""
        explicit=any(word in incoming for word in ("表情","图片","照片","那张图"))
        previous=[r for r in recent if r.get("media_id")]
        if not explicit:
            if previous and (local(now)-local(previous[-1]["at"])).total_seconds()<900:
                return []
            # 候选机会以近年发言段约15%为参考；模型仍可选择不发送。
            if int(hashlib.sha256(request_id.encode()).hexdigest()[:8],16)/0xffffffff>=.15:
                return []
        used={r["media_id"] for r in previous[-3:]}
        serious=any(word in incoming for word in ("去世","住院","受伤","分手","抑郁","自杀"))
        ranked=[]
        for key in self.assets:
            try:
                entry=self.resolve(key)
            except (ValueError,OSError):
                continue
            if key in used and not explicit:
                continue
            tags=entry.get("tags",[])
            if serious and "安慰" not in tags:
                continue
            if entry.get("sensitive"):
                continue
            if entry["kind"]=="image" and not any(tag in incoming for tag in tags) and not explicit:
                continue
            if "表情" in incoming and entry["kind"]!="sticker":
                continue
            if any(word in incoming for word in ("照片","那张图")) and entry["kind"]!="image":
                continue
            score=sum(3 for tag in tags if tag in incoming)+sum(1 for tag in tags if tag in feeling)
            ranked.append((score,entry.get("uses",0),entry))
        ranked.sort(key=lambda r:(r[0],r[1]),reverse=True)
        return [{k:e.get(k) for k in ("id","kind","description","tags","usage_notes","source_messages","review_scope")}
                for _,_,e in ranked[:8]]

    def validate_choice(self, key: str, offered: list[str], reply: str) -> dict | None:
        """Require a historically framed caption for non-sticker photographs."""
        if not key:
            return None
        if key not in offered:
            raise ValueError("模型选择了本轮候选之外的媒体")
        entry=self.resolve(key)
        if entry["kind"]=="image" and not any(word in reply for word in ("以前","之前","旧图","旧照片","当时","历史")):
            raise ValueError("普通图片必须明确为历史素材，不能冒充今天拍摄")
        return entry


def review_sheets(path: Path, output: Path) -> dict:
    """Render every frame into labelled review sheets without modifying the source."""
    from PIL import Image, ImageDraw
    output=Path(output)
    output.mkdir(parents=True,exist_ok=True)
    sheets=[]
    with Image.open(path) as source:
        count=getattr(source,"n_frames",1)
        for offset in range(0,count,48):
            n=min(48,count-offset)
            sheet=Image.new("RGB",(8*144,math.ceil(n/8)*164),"white")
            draw=ImageDraw.Draw(sheet)
            for index in range(offset,offset+n):
                source.seek(index)
                frame=source.convert("RGBA")
                frame.thumbnail((140,140))
                x,y=((index-offset)%8)*144,((index-offset)//8)*164
                sheet.paste(frame,(x,y+20),frame)
                draw.text((x+2,y+2),f"{index+1}/{count}",fill="black")
            dest=output/f"{Path(path).stem}-{offset//48+1}.jpg"
            sheet.save(dest,quality=94)
            sheets.append(str(dest))
    return {"source":str(path),"frames":count,"frame_indices":list(range(count)),"sheets":sheets}
