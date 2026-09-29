"""只处理绑定会话新增媒体；画面与原件、转写与原声明确区分。"""

import base64
import hashlib
import time
from io import BytesIO
from pathlib import Path

from bridge_state import UncertainState, now_cst
from phone import find, messages, sequence, walk, PREFIX


def anchored_index(current, anchor):
    """Find the end of a saved message prefix, never a guessed latest picture."""
    for count in range(min(len(current),len(anchor)),0,-1):
        matches=[i+count-1 for i in range(len(current)-count+1) if current[i:i+count]==anchor[-count:]]
        if len(matches)>1:
            raise UncertainState("媒体消息重复且锚点不唯一")
        if matches:
            return matches[0]
    raise UncertainState("媒体消息已离开可确认范围")


def row_for(state, anchor, sender):
    index=anchored_index(sequence(state),anchor)
    rows=[r for r in find(state,"bn1") if len([n for n in walk(r) if n.get("resourceId")==PREFIX+"bk1"])==1]
    entries=messages(state)
    if entries[index]["direction"]!="in" or entries[index]["sender"]!=sender+"头像":
        raise UncertainState("媒体来源不是绑定收件人")
    if entries[index]["signature"]!=anchor[-1]:
        raise UncertainState("媒体消息本体与锚点不符，不能用相邻消息代替")
    return rows[index]


def tap_box(phone, box, long=False):
    x,y=(box["left"]+box["right"])//2,(box["top"]+box["bottom"])//2
    if long:
        phone.device.long_press(x,y,ms=700)
    else:
        phone.device.tap(x,y)
    time.sleep(.7)
    return phone.observe()


def transcript_box(row):
    boxes=[n["boundsInScreen"] for n in walk(row) if n.get("resourceId")==PREFIX+"bru" and n.get("isVisibleToUser",True)]
    return boxes[0] if len(boxes)==1 else None


def capture_region(phone,box,audit,name):
    from PIL import Image
    raw=base64.b64decode(phone.device.screenshot())
    with Image.open(BytesIO(raw)) as screenshot:
        region=screenshot.crop((box["left"],box["top"],box["right"],box["bottom"]))
        stream=BytesIO()
        region.save(stream,format="PNG")
        raw=stream.getvalue()
    path=audit.retain(name,raw)
    return {"path":str(path),"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}


def collect(phone, items, audit, save):
    """Capture bounded new inputs, persisting each result before the next action."""
    if not items:
        return
    phone.verify_identity()
    for item in items:
        if item.get("status") in ("captured","transcribed","unknown"):
            for file in item.get("files",[]):
                if not Path(file["path"]).is_file() or hashlib.sha256(Path(file["path"]).read_bytes()).hexdigest()!=file["sha256"]:
                    raise UncertainState("已经捕获的媒体文件丢失或变化")
            continue
        state=phone.observe()
        row=row_for(state,item["anchor"],phone.config["contact_name"])
        nodes=list(walk(row))
        photos=[n for n in nodes if n.get("resourceId")==PREFIX+"bkg" and n.get("contentDescription")=="图片"]
        voices=[n for n in nodes if n.get("resourceId")==PREFIX+"brp"]
        stickers=[n for n in nodes if "表情" in n.get("contentDescription","") and n.get("resourceId")!=PREFIX+"bk1"]
        item.update(captured_at=now_cst().isoformat(),files=[])
        token=hashlib.sha256(item["id"].encode()).hexdigest()[:20]
        if len(photos)==1:
            tap_box(phone,photos[0]["boundsInScreen"])
            try:
                state=phone.observe()
                if state["phone_state"]["activity_name"]!="com.tencent.mm.ui.chatting.gallery.ImageGalleryUI":
                    raise UncertainState("新收件没有打开图片查看器")
                raw=base64.b64decode(phone.device.screenshot())
                path=audit.retain(token+"-viewer.png",raw)
                item.update(status="captured",kind="image",source="viewer_capture",
                    limitation="模型看到微信图片查看器画面，并非原始文件；不推断拍摄时间、精确地址或人物身份")
                item["files"]=[{"path":str(path),"sha256":hashlib.sha256(raw).hexdigest(),"bytes":len(raw)}]
            finally:
                if phone.observe()["phone_state"]["activity_name"]=="com.tencent.mm.ui.chatting.gallery.ImageGalleryUI":
                    phone.device.key("back")
                    time.sleep(.7)
            row_for(phone.observe(),item["anchor"],phone.config["contact_name"])
        elif len(stickers)==1:
            # 小图直接捕获气泡区域，不点击可能立即发送的表情面板。
            item.update(status="captured",kind="sticker",source="bubble_frame",
                limitation="仅所捕获的单帧；不能声称了解完整动画")
            item["files"]=[capture_region(phone,stickers[0]["boundsInScreen"],audit,token+"-sticker-frame.png")]
        elif len(voices)==1:
            box=transcript_box(row)
            options=[]
            if not box:
                state=tap_box(phone,voices[0]["boundsInScreen"],long=True)
                options=[n for n in walk(state["a11y_tree"]) if n.get("text") in ("转文字","转文字（普通话）","转文字(普通话)")]
            if box or len(options)==1:
                if not box:
                    state=tap_box(phone,options[0]["boundsInScreen"])
                initial={n.get("text","") for n in nodes}
                transcript=[]
                # 只接受原语音行中新增的转写，不把旁边其他聊天当语音文字。
                for _ in range(8):
                    time.sleep(1)
                    state=phone.observe()
                    updated=row_for(state,item["anchor"],phone.config["contact_name"])
                    box=transcript_box(updated)
                    transcript=[n["text"] for n in walk(updated) if n.get("text") and n["text"] not in initial
                                and n["text"] not in ("转换中","正在转换","转文字失败","重试")]
                    if transcript or box:
                        break
                if transcript:
                    item.update(status="transcribed",kind="voice",source="wechat_transcription",text="\n".join(transcript),
                                limitation="微信自动转写可能有误，未取得原始音频，不推断音色情绪")
                elif box:
                    item.update(status="captured",kind="voice",source="wechat_transcription_capture",
                        limitation="转写文字由微信绘制，不在无障碍文本中；模型读取转写区域画面，未直接听到音频，不推断声音或语气")
                    item["files"]=[capture_region(phone,box,audit,token+"-voice-transcript.png")]
                else:
                    item.update(status="unknown",kind="voice",reason="微信未返回可确认的转写文字")
            else:
                phone.device.key("back")
                item.update(status="unknown",kind="voice",reason="当前微信没有唯一可用的转文字菜单")
        else:
            item.update(status="unknown",kind="unknown",reason="当前媒体类型或控件不明确，不能猜测内容")
        audit.retain(token+"-mapping.json",item)
        save()


def model_media(items):
    images=[]
    summaries=[]
    for item in items:
        files=item.get("files",[])
        if item.get("status")=="captured":
            for file in files:
                path=Path(file["path"])
                if hashlib.sha256(path.read_bytes()).hexdigest()!=file["sha256"]:
                    raise UncertainState("输入媒体文件发生变化")
                images.append(path)
        summaries.append({key:item[key] for key in ("id","kind","status","source","text","limitation","reason") if key in item})
    return images,summaries
