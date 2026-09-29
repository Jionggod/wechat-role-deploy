"""真机微信串行桥接：监听、草稿、确认发送、原生调度触发。"""

import argparse
from contextlib import contextmanager
import hashlib
import importlib.util
import json
import logging
import msvcrt
import os
from pathlib import Path
import sys
import time
import uuid

from audit import Audit
from bridge_state import State, UncertainState, appended, atomic_json, daily_plan, configured_plan, now_cst, proactive_gate
from model_client import ModelClient, ModelError
from phone import Phone, messages, sequence
from persona_context import choose_activity, compact_cards, evidence_pack
from timeline import Timeline, local
from media_catalog import MediaCatalog
from delivery import build_parts, next_part

ROOT = Path(__file__).resolve().parent
STOP_WORDS = {"停止", "退出", "停止自动回复", "暂停机器人", "不要再发消息", "停止主动推送"}


@contextmanager
def single_instance(runtime):
    runtime.mkdir(parents=True, exist_ok=True)
    with (runtime/"process.lock").open("a+b") as handle:
        handle.seek(0, 2)
        if handle.tell() == 0:
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise RuntimeError("已有监听进程，拒绝重复启动") from exc
        try:
            yield
        finally:
            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)


class Bridge:
    def __init__(self, root, config, phone=None, model=None):
        self.root, self.config = Path(root), config
        self.runtime = self.root/"runtime"
        self.evidence = self.root/"evidence"
        self.state = State(self.runtime/"state.json")
        self.phone = phone
        self.model = model

    def status(self, status, **extra):
        atomic_json(self.runtime/"status.json", {"pid": os.getpid(), "at": now_cst().isoformat(), "status": status,
            "primary": self.config.get("primary_model"), "fallback": self.config.get("fallback_model"),
            "effort": self.config.get("reasoning_effort"), "pending": self.state.data.get("pending"),
            "last_dialogue": self.state.data.get("last_dialogue"), "awaiting_reply": self.state.data.get("awaiting_reply"),
            "daily_slots": configured_plan(now_cst(), self.config),
            "coaching_enabled": self.config.get("coaching_enabled",False), **extra})

    def check_pending(self):
        pending = self.state.data.get("pending")
        if pending and pending["status"] == "sending":
            raise UncertainState("存在发送结果未确认的任务，禁止重发；需要核对气泡")
        if pending and pending.get("parts"):
            next_part(pending["parts"])

    def timeline(self, now=None):
        return Timeline(self.runtime/"life-v3.sqlite", now or now_cst())

    def media(self):
        return MediaCatalog(ROOT/"data/media/catalog.json")

    def ingest(self, rows):
        if not rows:
            raise UncertainState("没有可用的完整消息行")
        data = self.state.data
        current = [row["signature"] for row in rows]
        if not data["sequence"]:
            anchors = [index for index, row in enumerate(rows) if row["direction"] == "out"
                       and row["text"] == self.config["baseline_text"]]
            if len(anchors) != 1:
                raise UncertainState("找不到已发送AI说明消息，不能把历史消息当新消息")
            data["sequence"] = current[:anchors[0]+1]
            data["last_dialogue"] = self.config["baseline_at"]
            self.state.save()
        added = appended(data["sequence"], current)
        if not added:
            return False
        new = [json.loads(row) for row in added]
        data["sequence"] = current
        incoming = [row for row in new if row["direction"] == "in"]
        if incoming:
            data["awaiting_reply"] = False
            data["last_dialogue"] = now_cst().isoformat()
            if data.get("pending") and data["pending"]["kind"] == "proactive" and data["pending"]["status"] != "sending":
                data["pending"] = None
            if any(row["kind"] != "text" for row in incoming):
                data["media_notice"] = {"at": now_cst().isoformat(), "items": incoming}
            text = "\n".join(row["text"] for row in incoming if row["kind"] == "text")
            delta_user = text
            if self.config.get("coaching_enabled"):
                from coaching_scenes import control
                for row in incoming:
                    if row["kind"] == "text":
                        control(self.timeline(),row["text"],now_cst())
            if any(row["text"].strip() in STOP_WORDS for row in incoming):
                data["paused"] = "用户通过微信要求停止"
                self.state.save()
                raise UncertainState(data["paused"])
            media_in=[(len(current)-len(added)+i,row) for i,row in enumerate(new)
                      if row["direction"]=="in" and row["kind"]!="text"] if self.config.get("incoming_media_enabled") else []
            if text or media_in:
                old = data.get("pending")
                if old and old["status"] in ("sending", "sent"):
                    raise UncertainState("上一发送事务尚未完成")
                data["counter"] += 1
                text = (old["user"]+"\n" if old and old["kind"] == "passive" else "")+text
                data["pending"] = {"id": "phone-"+uuid.uuid4().hex, "kind": "passive", "user": text,
                    "status": "queued", "created_at": now_cst().isoformat()}
                pending=data["pending"]
                pending["incoming_media"]=(old.get("incoming_media",[]) if old and old["kind"]=="passive" else [])+[
                    {"id":pending["id"]+f":in:{i}","anchor":current[:index+1],"labels":row.get("media_labels",[]),"status":"pending"}
                    for i,(index,row) in enumerate(media_in)]
                if self.config.get("life_v3"):
                    self.timeline().record_dialogue(pending["id"],now_cst(),text,"","received",incoming_media=pending["incoming_media"],
                        delta_user=delta_user,supersedes=old["id"] if old and old["kind"]=="passive" else None)
        else:
            data["last_dialogue"] = now_cst().isoformat()
        self.state.save()
        return bool(incoming)

    def persona(self):
        path = Path(self.config["persona_root"])/"scripts/life.py"
        spec = importlib.util.spec_from_file_location("role_bridge_life", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        config = self.config
        class BridgeLife(module.Life):
            def choose(self, state, now):
                return choose_activity(state, now)

            @staticmethod
            def eligibility(db, now):
                latest = db.execute("SELECT at,proactive FROM turns ORDER BY at DESC LIMIT 1").fetchone()
                gate = proactive_gate({"last_dialogue": latest["at"] if latest else None,
                                       "awaiting_reply": bool(latest and latest["proactive"])}, now,
                                      config.get("active_start_hour",9),config.get("active_end_hour",24))
                count = db.execute("SELECT count(*) FROM turns WHERE proactive=1 AND substr(at,1,10)=?", (str(now.date()),)).fetchone()[0]
                limit = len(daily_plan(now, config["contact_wechat_id"], config["proactive_min"], config["proactive_max"],
                                      config.get("active_start_hour",9),config.get("active_end_hour",24)))
                return gate == "ready" and count < limit, gate if gate != "ready" else f"本桥接当日上限{limit}次"
        path = config.get("persona_runtime_db")
        if not path and (self.config.get("life_v3") or self.root != ROOT):
            path = self.runtime/"legacy-isolated.sqlite"
        return (BridgeLife(path) if path else BridgeLife()), module

    def draft(self):
        pending = self.state.data["pending"]
        inputs=pending.get("incoming_media",[])
        if any(item.get("status")=="pending" for item in inputs):
            from incoming_media import collect
            if not self.phone:
                raise UncertainState("新收件媒体尚未采集，隔离预览不能猜测图片")
            with Audit(self.evidence,"incoming_media",{"event_id":pending["id"]}) as audit:
                collect(self.phone,inputs,audit,self.state.save)
        from incoming_media import model_media
        images,media_context=model_media(inputs)
        life, module = self.persona()
        now = now_cst()
        if self.config.get("life_v3"):
            from v3_context import build_context
            context = build_context(self, now, module)
        else:
            context = life.context(now, life.weather(now, refresh=self.config.get("refresh_world", True)))
        script_dir = str(Path(self.config["persona_root"])/"scripts")
        if script_dir not in sys.path:
            sys.path.insert(0, script_dir)
        from evidence import query
        retrieval_text=pending["user"]+"\n"+"\n".join(item.get("text","") for item in inputs)
        pack = evidence_pack(retrieval_text.strip(), context)
        context["persona_evidence"] = pack
        context["historical_cards"] = compact_cards(query(" ".join(pack["query_terms"]), 3))
        context["incoming_media"]=media_context
        # 旧项目及完整循环日志留在库中，但不继续把任务清单塞进新回复。
        context["fictional_life"].pop("projects", None)
        instructions = (ROOT/"persona_profile.md").read_text(encoding="utf-8")
        pending["scene_ids"] = [row["id"] for row in context.get("life_situations",{}).get("situations",[])]
        payload = {"kind": pending["kind"], "incoming": pending["user"], "context": context,
            "instruction": "先回应来信。若是proactive，围绕一个当前真实可核实话题或已有模拟生活动因自然发起话题。"}
        from prompt_context import compact_context
        original_prompt = json.dumps(payload, ensure_ascii=False)
        prompt = json.dumps({**payload, "context": compact_context(context)}, ensure_ascii=False, separators=(",", ":"))
        with Audit(self.evidence, "model", {"event_id": pending["id"], "kind": pending["kind"],
                                            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
                                            "image_sha256":[hashlib.sha256(p.read_bytes()).hexdigest() for p in images],
                                            "instructions_sha256": hashlib.sha256(instructions.encode()).hexdigest()}) as audit:
            audit.retain("prompt-size.json", {"before_chars":len(original_prompt), "after_chars":len(prompt),
                "before_bytes":len(original_prompt.encode()), "after_bytes":len(prompt.encode()),
                "notice":"同一完整上下文与去重副本的大小比较，不是实际token或费用降幅"})
            audit.retain("request.json", {"prompt": prompt, "instructions": instructions})
            audit.retain("context.json", context)
            result = (self.model.generate(prompt, instructions, images=images) if images
                      else self.model.generate(prompt, instructions))
            audit.retain("result.json", result)
            if self.config.get("coaching_enabled") and result.get("pause_pressure") is True:
                from coaching_scenes import control
                control(self.timeline(now),"暂停压力测试",now)
                pending["scene_ids"] = []
            if len(result["reply"]) > self.config["max_reply_chars"]:
                raise ModelError("回复过长，保留任务不发送")
            if self.config.get("life_v3"):
                offered = [item["id"] for item in context["media_candidates"]]
                self.media().validate_choice(result.get("media_id", ""), offered, result["reply"])
                available = context["life_timeline"]["recent_events"]+context["life_timeline"]["upcoming"]+context.get("trip_plan",[])
                if context["fictional_life"]["active"]:
                    available.append(context["fictional_life"]["active"])
                if any(key not in {e["id"] for e in available} for key in result.get("event_ids", [])):
                    raise ValueError("模型引用不存在的生活事件")
                pending["parts"] = build_parts(pending["id"], result)
                active = context["fictional_life"]["active"]
                pending["draft_valid_until"] = active["end"] if active else None
                pending["draft_revision"]=pending.get("draft_revision",0)+1
                understanding=result.get("media_understanding",[])
                expected_ids={item["id"] for item in inputs}
                if {row.get("id") for row in understanding}!=expected_ids or len(understanding)!=len(expected_ids):
                    raise ValueError("模型媒体理解没有逐一对应收件消息")
                for item in inputs:
                    if item.get("source")=="wechat_transcription_capture":
                        row=next(r for r in understanding if r["id"]==item["id"])
                        item.update(text=row.get("transcript",""),text_source="model_read_wechat_transcript")
                self.timeline(now).record_dialogue(f"{pending['id']}:draft:{pending['draft_revision']}",now,pending["user"],result["reply"],"drafted",
                    parent_id=pending["id"],
                    incoming_media=inputs,media_understanding=understanding,
                    media_id=result.get("media_id", ""),event_ids=result.get("event_ids", []),scene_ids=pending.get("scene_ids",[]))
        pending.update(status="drafted", draft=result)
        self.state.save()
        return result

    def finish_memory(self):
        pending = self.state.data["pending"]
        # 发送已经按桥接策略验收；记忆保存不用再次以新的时钟判断发送冷却。
        result = pending["draft"]
        if self.config.get("life_v3"):
            stamp = local(pending["sent_at"])
            timeline = self.timeline(stamp)
            timeline.record_dialogue(pending["id"],stamp,pending["user"],result["reply"],"sent",
                topic=result.get("topic", ""),media_id=result.get("media_id", ""),event_ids=result.get("event_ids", []),
                feeling=result.get("feeling", ""),feeling_reason=result.get("feeling_reason", ""),
                proactive=pending["kind"]=="proactive",parts=pending.get("parts", []),
                incoming_media=pending.get("incoming_media",[]),media_understanding=result.get("media_understanding",[]),
                scene_ids=pending.get("scene_ids",[]))
            timeline.export(self.root/"虚构记忆",now_cst())
        else:
            life, _ = self.persona()
            life.record(pending["id"], now_cst(), pending["user"], result["reply"], result.get("topic", ""),
                        proactive=pending["kind"] == "proactive")
        self.state.data["last_completed"] = pending
        self.state.data["pending"] = None
        self.state.save()

    def proactive_request(self):
        path = self.runtime/"proactive-request.json"
        if not path.exists():
            return
        request = json.loads(path.read_text(encoding="utf-8"))
        if request["id"] == self.state.data.get("last_proactive_request"):
            return
        data, now = self.state.data, now_cst()
        data["last_proactive_request"] = request["id"]
        gate = proactive_gate(data, now, self.config.get("active_start_hour",9),self.config.get("active_end_hour",24))
        slots = configured_plan(now, self.config)
        limit = len(daily_plan(now, self.config["contact_wechat_id"], self.config["proactive_min"], self.config["proactive_max"],
                              self.config.get("active_start_hour",9),self.config.get("active_end_hour",24)))
        if data.get("proactive_sent", {}).get(str(now.date()), 0) >= limit:
            gate = "当日主动上限"
        if gate == "ready":
            gate = self.activity_gate(now)
        from datetime import datetime
        due = [slot for slot in slots if 0 <= (now-datetime.fromisoformat(slot)).total_seconds() <= 1200
               and slot not in data["handled_slots"]]
        if request.get("now"):
            due = ["manual:"+request["id"]]
        if gate == "ready" and due:
            data["handled_slots"].extend(due)
            data["pending"] = {"id": "proactive-"+uuid.uuid4().hex, "kind": "proactive", "user": "",
                               "status": "queued", "created_at": now.isoformat()}
        data["last_proactive_check"] = {"at": now.isoformat(), "gate": gate, "due": due}
        self.state.save()

    def activity_gate(self, now):
        if not (self.config.get("life_v3") and self.config.get("coaching_enabled")):
            return "ready"
        active = self.timeline(now).snapshot(now)["active"]
        if not active or active["kind"] in {"sleep","commute","travel","dance"}:
            return "当前生活活动不适合主动聊天"
        return "ready"

    def scene_guard(self, pending, now):
        if self.config.get("coaching_enabled") and pending.get("scene_ids"):
            from coaching_scenes import can_send
            if not can_send(self.timeline(now),pending["scene_ids"],now):
                pending.update(status="queued",scene_ids=[])
                pending.pop("draft",None)
                pending.pop("parts",None)
                self.state.save()
                return False
        return True

    def tick(self, send=False):
        self.check_pending()
        if self.state.data.get("paused"):
            raise UncertainState(self.state.data["paused"])
        pending = self.state.data.get("pending")
        if pending and pending["status"] == "sent":
            self.finish_memory()
        state = self.phone.observe(recover=True)
        rows = messages(state)
        for row in rows:
            if row["direction"] == "unknown" or row["sender"] not in (
                    self.config["contact_name"]+"头像", self.config["self_name"]+"头像"):
                raise UncertainState("界面消息发送者不符合绑定身份")
        self.ingest(rows)
        self.proactive_request()
        pending = self.state.data.get("pending")
        if not pending:
            self.status("listening")
            return
        if pending["status"] == "queued":
            self.status("generating")
            self.draft()
        if not send:
            self.status("drafted")
            return
        if (self.runtime/"stop.json").exists():
            raise UncertainState("已设置暂停标志，不发送已有草稿")
        # 模型计算过程中可能有新消息，先更新队列而不是发送旧草稿。
        if self.ingest(messages(self.phone.observe())):
            self.status("queued")
            return
        pending = self.state.data["pending"]
        if not self.scene_guard(pending,now_cst()):
            return
        if pending.get("draft_valid_until") and now_cst()>=local(pending["draft_valid_until"]):
            pending["status"]="queued"
            self.state.save()
            return
        if pending["draft"].get("media_id"):
            raise UncertainState("图片发出通道尚未完成真机验收；保留完整草稿，不先发送配文")
        if pending["kind"] == "proactive":
            gate = proactive_gate({**self.state.data, "pending": None}, now_cst(),
                                  self.config.get("active_start_hour",9),self.config.get("active_end_hour",24))
            if gate == "ready":
                gate = self.activity_gate(now_cst())
            if self.config.get("life_v3"):
                eligible,reason=True,"由桥接队列执行冷却和次数限制"
            else:
                life, _ = self.persona()
                with life.db() as db:
                    eligible, reason = life.eligibility(db, now_cst())
            if gate != "ready" or not eligible:
                self.state.data["cancelled_proactive"] = {"id": pending["id"], "reason": gate if gate != "ready" else reason}
                self.state.data["pending"] = None
                self.state.save()
                return
        def before_click():
            if not self.scene_guard(pending,now_cst()):
                raise UncertainState("情境已暂停或失效；取消旧草稿，普通聊天继续")
            if pending["kind"] == "proactive":
                gate = proactive_gate({**self.state.data,"pending":None},now_cst(),
                                      self.config.get("active_start_hour",9),self.config.get("active_end_hour",24))
                if gate != "ready":
                    raise UncertainState(gate)
                if self.activity_gate(now_cst()) != "ready":
                    raise UncertainState("当前生活活动不适合主动聊天")
            pending["status"] = "sending"
            self.state.save()
        with Audit(self.evidence, "send", {"event_id": pending["id"], "kind": pending["kind"],
                                           "recipient": self.config["contact_wechat_id"]}) as audit:
            state = self.phone.send(pending["draft"]["reply"], self.state.data["sequence"], before_click, audit)
            pending["status"] = "sent"
            pending["sent_at"] = now_cst().isoformat()
            for part in pending.get("parts",[]):
                part.update(status="sent",sent_at=pending["sent_at"])
            # 仅确认本次发送；不吞掉点击期间新到的收件，完成事务后统一入队。
            self.state.data["last_dialogue"] = pending["sent_at"]
            self.state.data["awaiting_reply"] = pending["kind"] == "proactive"
            if pending["kind"] == "proactive":
                counts = self.state.data.setdefault("proactive_sent", {})
                day = str(now_cst().date())
                counts[day] = counts.get(day, 0)+1
            self.state.save()
        self.finish_memory()
        self.ingest(messages(state))
        self.status("listening")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=["probe", "once", "run", "status", "stop", "proactive-check", "model-test"])
    parser.add_argument("--send", action="store_true")
    parser.add_argument("--now", action="store_true", help="主动检查立即提供一次机会，仍受静默/冷却/未回复/日上限约束")
    args = parser.parse_args()
    config = json.loads((ROOT/"config.json").read_text(encoding="utf-8"))
    runtime = ROOT/"runtime"
    runtime.mkdir(exist_ok=True)
    if args.action == "status":
        print((runtime/"status.json").read_text(encoding="utf-8") if (runtime/"status.json").exists() else "未启动")
        return
    if args.action in ("stop", "proactive-check"):
        atomic_json(runtime/f"{'stop' if args.action == 'stop' else 'proactive-request'}.json",
                    {"id": uuid.uuid4().hex, "at": now_cst().isoformat(), "now": args.now})
        print("请求已保存；以status实际状态为准")
        return
    if args.action == "model-test":
        with Audit(ROOT/"evidence", "model", {"test": "Astra优先短对话"}) as audit:
            result = ModelClient(config, ROOT).generate("对方说：在用AI编曲。输出一句自然的短回复。",
                "你是已明确告知为AI的AI角色，只输出JSON reply和topic；不调用任何工具，不读取文件。")
            audit.retain("result.json", result)
            print(json.dumps(result, ensure_ascii=False))
        return
    with single_instance(runtime):
        if (runtime/"stop.json").exists():
            raise RuntimeError("已设置停止标志；恢复需明确处理该标志")
        phone = Phone(config, runtime)
        bridge = Bridge(ROOT, config, phone, ModelClient(config, ROOT))
        try:
            with Audit(ROOT/"evidence", "phone_start", {"device": config["device_serial"]}) as audit:
                bridge.check_pending()
                if bridge.state.data.get("paused"):
                    raise UncertainState(bridge.state.data["paused"])
                phone.setup()
                state = phone.observe(recover=True)
                audit.retain("identity.json", phone.identity_evidence)
                audit.retain("snapshot.json", state)
                bridge.check_pending()
                if bridge.state.data.get("pending", {}) and bridge.state.data["pending"]["status"] == "sent":
                    bridge.finish_memory()
                bridge.ingest(messages(state))
                bridge.status("ready")
                print(json.dumps({"status": "ready", "messages": messages(state)}, ensure_ascii=False), flush=True)
            if args.action == "probe":
                return
            while not (runtime/"stop.json").exists():
                try:
                    bridge.tick(send=args.send and config["send_enabled"])
                except Exception as exc:
                    logging.exception("桥接本轮未完成，保留持久状态")
                    bridge.status("blocked", error=str(exc))
                    if args.action == "once":
                        raise
                    if bridge.state.data.get("paused"):
                        break
                    time.sleep(20 if isinstance(exc, UncertainState) else 60)
                if args.action == "once":
                    break
                time.sleep(config["poll_seconds"])
        finally:
            try:
                phone.restore()
            finally:
                bridge.status("stopped")


if __name__ == "__main__":
    sys.stdout.reconfigure(encoding="utf-8")
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    main()
