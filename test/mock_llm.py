#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""OpenAI 兼容的本地 mock 端点（纯标准库，零网络）。端到端/尾部召回测试的后端。

实现 POST /v1/chat/completions（任何以 /chat/completions 结尾的路径都收），按请求形状分流：

  content 里有 input_audio  → ASR：按调用序号回 "这是第N段转写。结论将在最后给出。"
  content 里有 image_url    → 视觉：回 "画面：测试画面第N帧，灰度色块。\n文字：无文字"
                              （每第 3 帧回 "文字：NEEDLE-OCR"，制造 OCR 事件样本）
  user 里含 "去掉完全重复"   → reduce：把输入里所有 [数字:数字] 开头的行去重后原样回显（保持顺序）
  user 里含 "请抽 3-5 条事实" → map：把输入里所有含 NEEDLE 的行原样回显，外加 2 条普通事实

每个请求体压成一行 JSON 追加写 <logdir>/requests.jsonl（带 seq 与 kind），
测试脚本靠它验证 reduce 的块顺序。

--port 0 选随机空闲端口，第一行 stdout 打 `PORT=<n>` 并 flush，方便测试脚本 Popen 后读端口。
退出：收到 terminate/SIGINT 即可（也可以给 --stop-file，文件一出现就自己关）。
"""

import argparse
import json
import os
import re
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MOCK_MODEL = "mock-model"
TIMESTAMP_LINE = re.compile(r"^\[\d{1,2}:\d{2}(?::\d{2})?\]")
NORMAL_FACTS = (
    "[00:01] 普通事实一：视频里出现了一段测试画面。",
    "[00:02] 普通事实二：音频是交替的两个正弦波。",
)


class State:
    """所有请求共享的计数器与日志句柄。多线程访问一律加锁。"""

    def __init__(self, log_dir):
        self.lock = threading.Lock()
        self.seq = 0
        self.asr_count = 0
        self.frame_count = 0
        self.log_path = None
        if log_dir:
            os.makedirs(log_dir, exist_ok=True)
            self.log_path = os.path.join(log_dir, "requests.jsonl")

    def next_seq(self):
        with self.lock:
            self.seq += 1
            return self.seq

    def bump_asr(self):
        with self.lock:
            self.asr_count += 1
            return self.asr_count

    def bump_frame(self):
        with self.lock:
            self.frame_count += 1
            return self.frame_count

    def append(self, record):
        if not self.log_path:
            return
        line = json.dumps(record, ensure_ascii=False)
        with self.lock:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")
                handle.flush()


STATE = None  # type: State


# ---------------------------------------------------------------------------
# 请求分流
# ---------------------------------------------------------------------------


def flatten_content(payload):
    """把 messages 里的 content 拆成 (类型集合, 文本列表)。"""
    types = set()
    texts = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        content = message.get("content")
        if isinstance(content, str):
            texts.append(content)
        elif isinstance(content, list):
            for item in content:
                if not isinstance(item, dict):
                    continue
                item_type = str(item.get("type") or "")
                types.add(item_type)
                if item_type == "text":
                    texts.append(str(item.get("text") or ""))
    return types, texts


def classify(payload):
    types, texts = flatten_content(payload)
    joined = "\n".join(texts)
    if "input_audio" in types:
        return "asr", joined
    if "image_url" in types:
        return "vision", joined
    if "去掉完全重复" in joined:
        return "reduce", joined
    if "请抽 3-5 条事实" in joined:
        return "map", joined
    return "chat", joined


def produce_asr(text, payload):
    return "这是第%d段转写。结论将在最后给出。" % STATE.bump_asr()


def produce_vision(text, payload):
    index = STATE.bump_frame()
    ocr = "NEEDLE-OCR" if index % 3 == 0 else "无文字"
    return "画面：测试画面第%d帧，灰度色块。\n文字：%s" % (index, ocr)


def produce_map(text, payload):
    """把块里含 NEEDLE 的原始行原样回显 —— 尾部召回测试靠这几条穿透到最后。"""
    echoed = [line.strip() for line in text.splitlines() if "NEEDLE" in line and line.strip()]
    echoed = [line for line in echoed if TIMESTAMP_LINE.match(line)] or echoed
    return "\n".join(list(echoed) + list(NORMAL_FACTS))


def produce_reduce(text, payload):
    """去重后原样回显（保持输入顺序）—— 顺序本身就是被测对象，绝不能排序。"""
    seen = set()
    out = []
    for line in text.splitlines():
        item = line.strip()
        if not TIMESTAMP_LINE.match(item) or item in seen:
            continue
        seen.add(item)
        out.append(item)
    return "\n".join(out)


PRODUCERS = {
    "asr": produce_asr,
    "vision": produce_vision,
    "map": produce_map,
    "reduce": produce_reduce,
    "chat": lambda text, payload: "mock pong",
}


def completion(text):
    return {
        "id": "chatcmpl-mock",
        "object": "chat.completion",
        "created": int(time.time()),
        "model": MOCK_MODEL,
        "choices": [{"index": 0, "message": {"role": "assistant", "content": text}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 1, "completion_tokens": 1, "total_tokens": 2},
    }


# ---------------------------------------------------------------------------
# HTTP
# ---------------------------------------------------------------------------


class MockHandler(BaseHTTPRequestHandler):
    server_version = "mock-llm/1.0"
    protocol_version = "HTTP/1.1"

    def log_message(self, *args):  # 静音：测试输出只留 PORT= 那一行
        pass

    def _send(self, status, obj):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):
        self._send(200, {"ok": True, "mock": True, "asr": STATE.asr_count, "frames": STATE.frame_count})

    def do_POST(self):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except (TypeError, ValueError):
            length = 0
        raw = self.rfile.read(length).decode("utf-8", "replace") if length else ""
        try:
            payload = json.loads(raw or "{}")
        except ValueError as exc:
            self._send(400, {"error": {"message": "invalid json: %s" % exc}})
            return
        kind, text = classify(payload)
        answer = PRODUCERS[kind](text, payload)
        record = {
            "seq": STATE.next_seq(),
            "kind": kind,
            "path": self.path,
            "auth": "api-key" if self.headers.get("api-key") else
                    ("bearer" if self.headers.get("Authorization") else "none"),
            "text": answer,
            "request": payload,
        }
        STATE.append(record)
        self._send(200, completion(answer))


def watch_stop_file(path):
    while not os.path.exists(path):
        time.sleep(0.2)
    os._exit(0)  # 测试脚本用 stop-file 收尾时不必管线程


def main():
    parser = argparse.ArgumentParser(description="OpenAI 兼容 mock 端点（零网络）")
    parser.add_argument("--port", type=int, default=0, help="0 = 随机空闲端口")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--logdir", default=None, help="requests.jsonl 落盘目录")
    parser.add_argument("--stop-file", default=None, help="该文件出现即退出")
    args = parser.parse_args()

    global STATE
    STATE = State(args.logdir)
    server = ThreadingHTTPServer((args.host, args.port), MockHandler)
    server.daemon_threads = True
    port = server.server_address[1]
    print("PORT=%d" % port, flush=True)
    print("MOCK_LLM=ready logdir=%s" % (os.path.abspath(args.logdir) if args.logdir else "(none)"), flush=True)

    if args.stop_file:
        threading.Thread(target=watch_stop_file, args=(args.stop_file,), daemon=True).start()
    try:
        server.serve_forever(poll_interval=0.1)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    for _stream in (sys.stdout, sys.stderr):
        try:
            _stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    sys.exit(main())
