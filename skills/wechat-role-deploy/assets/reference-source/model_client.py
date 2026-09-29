"""官方 Codex stdio 客户端；失败降级与最终消息契约。"""

import json
import os
import queue
import shutil
import subprocess
import threading
import time
import tomllib
from pathlib import Path

from bridge_state import atomic_json, now_cst


class ModelError(RuntimeError):
    pass


def user_inputs(prompt, images=()):
    inputs=[{"type":"text","text":prompt}]
    for image in images:
        path=Path(image).resolve()
        if not path.is_file() or path.suffix.lower() not in (".png",".jpg",".jpeg",".webp"):
            raise ValueError("模型图片输入不存在或格式不支持")
        inputs.append({"type":"localImage","path":str(path)})
    return inputs


def with_fallback(generate, primary, fallback):
    try:
        return {**generate(primary), "model": primary, "fallback_reason": None}
    except ModelError as exc:
        reason = str(exc)
    try:
        return {**generate(fallback), "model": fallback, "fallback_reason": reason}
    except ModelError as exc:
        raise ModelError(f"主模型失败：{reason}；回退也失败：{exc}") from exc


def final_reply(items):
    final = [item["text"] for item in items if item.get("type") == "agentMessage"
             and item.get("phase") in ("final", "final_answer", None) and item.get("text")]
    if not final:
        raise ModelError("模型没有返回最终回复")
    try:
        result = json.loads(final[-1])
    except ValueError as exc:
        raise ModelError("模型最终输出不是所需JSON") from exc
    if not isinstance(result, dict) or not isinstance(result.get("reply"), str):
        raise ModelError("模型回复为空或格式无效")
    if not isinstance(result.get("media_id", ""), str) or not (result["reply"].strip() or result.get("media_id")):
        raise ModelError("文字和媒体不能同时为空")
    if not isinstance(result.get("topic", ""), str):
        raise ModelError("话题格式无效")
    return result


class Rpc:
    def __init__(self, cwd, proxy=None):
        command = shutil.which("codex")
        if not command:
            raise ModelError("找不到本机Codex程序")
        args = [command, "app-server", "--listen", "stdio://", "-c", "features.shell_tool=false",
                "-c", "features.apply_patch_freeform=false", "-c", "features.multi_agent=false",
                "-c", "web_search=live"]
        # 不加载账号上其他应用连接；不读取auth或输出配置值。
        config_path = Path.home() / ".codex" / "config.toml"
        if config_path.exists():
            with config_path.open("rb") as stream:
                names = tomllib.load(stream).get("mcp_servers", {}).keys()
            for name in names:
                args += ["-c", f"mcp_servers.{name}.enabled=false"]
        environment = os.environ.copy()
        if proxy:
            environment.update(HTTP_PROXY=proxy, HTTPS_PROXY=proxy)
            environment["NO_PROXY"] = environment.get("NO_PROXY", "")+",localhost,127.0.0.1,::1"
        self.process = subprocess.Popen(args, cwd=cwd, env=environment, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                        stderr=subprocess.DEVNULL, text=True, encoding="utf-8",
                                        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        self.messages = queue.Queue()
        self.sequence = 0
        self.events = []
        threading.Thread(target=self._read, daemon=True).start()
        self.request("initialize", {"clientInfo": {"name": "role_phone_bridge", "version": "0.1.0"},
                                    "capabilities": {"experimentalApi": True}})
        self.write({"method": "initialized", "params": {}})

    def _read(self):
        for line in self.process.stdout:
            try:
                self.messages.put(json.loads(line))
            except ValueError:
                continue
        self.messages.put({"local_error": "Codex进程已退出"})

    def write(self, data):
        self.process.stdin.write(json.dumps(data, ensure_ascii=False) + "\n")
        self.process.stdin.flush()

    def next(self, deadline):
        remaining = deadline-time.monotonic()
        if remaining <= 0:
            raise ModelError("Codex响应超时")
        try:
            message = self.messages.get(timeout=remaining)
        except queue.Empty as exc:
            raise ModelError("Codex响应超时") from exc
        if "local_error" in message:
            raise ModelError(message["local_error"])
        if "method" in message and "id" in message:
            self.write({"id": message["id"], "error": {"code": -32601, "message": "本桥接不允许交互批准或外部操作"}})
        return message

    def request(self, method, params, timeout=45):
        self.sequence += 1
        request_id = self.sequence
        self.write({"method": method, "id": request_id, "params": params})
        deadline = time.monotonic()+timeout
        while True:
            msg = self.next(deadline)
            if msg.get("id") != request_id:
                self.events.append(msg)
                continue
            if "error" in msg:
                raise ModelError(str(msg["error"]))
            return msg.get("result", {})

    def close(self):
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self.process.kill()
                self.process.wait(timeout=5)
        for stream in (self.process.stdin, self.process.stdout):
            stream.close()


class ModelClient:
    def __init__(self, config, root):
        self.config, self.root = config, root

    def models(self):
        rpc = Rpc(self.root, self.config.get("model_proxy_url"))
        try:
            result = rpc.request("model/list", {"limit": 100, "includeHidden": True})
            return [{key: entry.get(key) for key in ("model", "supportedReasoningEfforts")}
                    for entry in result.get("data", []) if entry.get("model") in
                    (self.config["primary_model"], self.config["fallback_model"])]
        finally:
            rpc.close()

    def generate(self, prompt, instructions, images=()):
        return with_fallback(lambda model: self._generate(model, prompt, instructions, images),
                             self.config["primary_model"], self.config["fallback_model"])

    def _generate(self, model, prompt, instructions, images=()):
        rpc = Rpc(self.root, self.config.get("model_proxy_url"))
        try:
            progress_path = self.root/"runtime/model-progress.json"
            atomic_json(progress_path, {"at": now_cst().isoformat(), "model": model, "stage": "模型验证"})
            catalog = rpc.request("model/list", {"limit": 100, "includeHidden": True})
            matches = [entry for entry in catalog.get("data", []) if entry.get("model") == model]
            effort = self.config["reasoning_effort"]
            if not matches or effort not in [e["reasoningEffort"] for e in matches[0].get("supportedReasoningEfforts", [])]:
                raise ModelError(f"模型或推理强度不可用：{model}/{effort}")
            thread = rpc.request("thread/start", {"model": model, "cwd": str(self.root), "ephemeral": True,
                "approvalPolicy": "never", "sandbox": "read-only", "baseInstructions": instructions,
                "config": {"project_doc_max_bytes": 0}})
            thread_id = thread["thread"]["id"]
            schema = {"type": "object", "properties": {"reply": {"type": "string"}, "topic": {"type": "string"}},
                      "required": ["reply", "topic"], "additionalProperties": False}
            if self.config.get("life_v3"):
                schema["properties"].update({"media_id":{"type":"string"}, "feeling":{"type":"string"},
                    "feeling_reason":{"type":"string"}, "event_ids":{"type":"array","items":{"type":"string"}}})
                schema["required"] = list(schema["properties"])
                if self.config.get("incoming_media_enabled"):
                    schema["properties"]["media_understanding"]={"type":"array","items":{"type":"object","properties":{
                        "id":{"type":"string"},"observed":{"type":"string"},"uncertain":{"type":"string"},"transcript":{"type":"string"}},
                        "required":["id","observed","uncertain","transcript"],"additionalProperties":False}}
                    schema["required"].append("media_understanding")
            if self.config.get("coaching_enabled"):
                schema["properties"]["pause_pressure"]={"type":"boolean"}
                schema["required"].append("pause_pressure")
            turn = rpc.request("turn/start", {"threadId": thread_id, "model": model, "effort": effort,
                "input": user_inputs(prompt, images), "outputSchema": schema})
            turn_id = turn["turn"]["id"]
            deadline = time.monotonic()+120
            items, usage = [], None
            recent = []
            while True:
                message = rpc.events.pop(0) if rpc.events else rpc.next(deadline)
                params = message.get("params", {})
                if params.get("threadId", thread_id) != thread_id:
                    continue
                method = message.get("method", "")
                recent = (recent+[method])[-15:]
                atomic_json(progress_path, {"at": now_cst().isoformat(), "model": model, "stage": method,
                    "recent_events": recent, "error": params.get("error") if method == "error" else None})
                if method == "error" and not params.get("willRetry", False):
                    raise ModelError(str(params.get("error", "模型流错误")))
                if message.get("method") == "item/completed":
                    item = params.get("item", {})
                    if item.get("type") == "agentMessage":
                        items.append(item)
                if message.get("method") == "thread/tokenUsage/updated":
                    usage = params.get("tokenUsage")
                if message.get("method") == "turn/completed" and params.get("turn", {}).get("id") == turn_id:
                    if params["turn"].get("status") != "completed":
                        raise ModelError(str(params["turn"].get("error") or params["turn"].get("status")))
                    break
            return {**final_reply(items), "usage": usage, "thread_id": thread_id, "turn_id": turn_id}
        finally:
            rpc.close()
