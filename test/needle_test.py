#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""尾部召回验收（零网络，mock LLM）。

构造一个 45 分钟的时间线：事件每 30 s 一条铺满 2700 s，其中 t=2400（第 40 分钟）、
t=2450、t=2650 三条含 NEEDLE。然后用 `--block-chars 800` 把时间线切成 ≥4 块，
分别跑 `summarize --mode mapreduce` 与 `--mode tail-first`，验证：

  1. 两种模式的 summary.json.facts 里都出现 NEEDLE（尾部 5 分钟的内容不能被吞掉）；
  2. tail-first 的 reduce 请求里，最后一个块的第一条事实排在第一个块的事实之前 ——
     也就是说"尾部优先"真的体现在发出去的 prompt 上，而不是只在本地排了个序；
     mapreduce 则是正序。

任何一条不满足就打印对比并以退出码 1 结束。
"""

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "videokit.py")
MOCK = os.path.join(HERE, "mock_llm.py")
PYTHON = sys.executable

DURATION = 2700.0          # 45 分钟
EVENT_STEP = 30.0          # 每 30 s 一条
NEEDLE_TIMES = (2400.0, 2450.0, 2650.0)
BLOCK_CHARS = 800          # 刻意调小，确保切出 ≥4 块
NEEDLE = "NEEDLE"


class CheckFailed(AssertionError):
    pass


def expect(condition, message, detail=None):
    if condition:
        print("  [OK] %s" % message)
        return
    raise CheckFailed("%s%s" % (message, ("\n诊断:\n" + str(detail)) if detail else ""))


def tail(text, lines=20):
    rows = (text or "").strip().splitlines()
    return "\n".join(rows[-lines:])


# ---------------------------------------------------------------------------
# 造索引目录
# ---------------------------------------------------------------------------


def filler(index):
    return (
        "这是第%02d条事件，它的作用只是把时间线铺满，让分块逻辑切出足够多的块，"
        "本身不含任何关键信息。" % index
    )


def build_index_dir(out_dir, log_dir):
    """写 probe.json / index.json / timeline.json —— summarize 只读 timeline.json，
    另外两个是为了让目录看起来像一个真实的索引产物。"""
    os.makedirs(out_dir, exist_ok=True)
    events = []
    position = 0
    moment = 0.0
    while moment < DURATION:
        text = filler(position)
        if any(abs(moment - needle) < 1e-6 for needle in NEEDLE_TIMES):
            text = "%s 关键线索出现在 %s。" % (NEEDLE, fmt_mmss(moment))
        events.append({"t0": round(moment, 3), "t1": round(moment + EVENT_STEP * 0.6, 3),
                       "kind": "speech", "text": text, "source": "asr", "estimated": True})
        position += 1
        moment += EVENT_STEP
    # 2450 / 2650 不落在 30 s 网格上，单独补两条
    for needle in NEEDLE_TIMES:
        if not any(abs(event["t0"] - needle) < 1e-6 for event in events):
            events.append({"t0": needle, "t1": needle, "kind": "scene",
                           "text": "%s 补充线索在 %s，来自画面。" % (NEEDLE, fmt_mmss(needle)),
                           "source": "vlm", "frame": "frames/f000_t0000.0.jpg"})
    events.sort(key=lambda event: (event["t0"], event["kind"]))
    stats = {"speech": 0, "scene": 0, "ocr": 0}
    for event in events:
        stats[event["kind"]] = stats.get(event["kind"], 0) + 1

    timeline = {"version": 1, "video": os.path.join(out_dir, "fake.mp4"),
                "durationSec": DURATION, "events": events, "stats": stats}
    write_json(os.path.join(out_dir, "timeline.json"), timeline)
    write_json(os.path.join(out_dir, "probe.json"), {
        "path": os.path.join(out_dir, "fake.mp4"), "sizeBytes": 1024 * 1024,
        "durationSec": DURATION, "width": 1280, "height": 720, "fps": 25.0,
        "bitrateKbps": 1200, "videoCodec": "h264", "audioCodec": "aac",
        "audioChannels": 2, "audioSampleRate": 44100, "chapters": [], "hasAudio": True,
    })
    write_json(os.path.join(out_dir, "index.json"), {
        "video": os.path.join(out_dir, "fake.mp4"), "durationSec": DURATION, "workdir": out_dir,
        "audio": {"parts": 3, "chunkSeconds": 900.0, "coveredSec": DURATION},
        "asr": {"ok": 3, "failed": 0, "model": "mock-asr", "parts": []},
        "vision": {"frames": 0, "described": 0, "failed": 0, "model": ""},
        "timeline": {"events": len(events), "stats": stats},
        "chapters": {"count": 1},
        "startedAt": "2026-01-01T00:00:00+08:00", "finishedAt": "2026-01-01T00:01:00+08:00",
    })
    return timeline


def fmt_mmss(seconds):
    total = int(round(seconds))
    return "%02d:%02d" % (total // 60, total % 60)


def write_json(path, obj):
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(obj, handle, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# mock 进程
# ---------------------------------------------------------------------------


def start_mock(log_dir):
    os.makedirs(log_dir, exist_ok=True)
    process = subprocess.Popen(
        [PYTHON, MOCK, "--port", "0", "--logdir", log_dir],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE,
        encoding="utf-8", errors="replace", cwd=ROOT,
    )
    deadline = time.time() + 30
    while time.time() < deadline:
        line = process.stdout.readline()
        if not line:
            if process.poll() is not None:
                raise CheckFailed("mock_llm 启动即退出: %s" % tail(process.stderr.read()))
            continue
        if line.startswith("PORT="):
            return process, int(line.strip().split("=", 1)[1])
    process.kill()
    raise CheckFailed("mock_llm 30 秒内没有打印 PORT=")


def stop_mock(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


# ---------------------------------------------------------------------------
# 跑一次 summarize 并收集证据
# ---------------------------------------------------------------------------


def read_log(path):
    if not os.path.isfile(path):
        return []
    entries = []
    with open(path, "r", encoding="utf-8") as handle:
        for line in handle:
            if line.strip():
                entries.append(json.loads(line))
    return entries


def run_summarize(index_dir, config_path, workdir, mode):
    command = [PYTHON, SCRIPT, "summarize", index_dir, "--mode", mode,
               "--block-chars", str(BLOCK_CHARS), "--config", config_path, "--workdir", workdir]
    result = subprocess.run(command, capture_output=True, encoding="utf-8", errors="replace",
                            timeout=300, cwd=ROOT)
    if result.returncode != 0:
        raise CheckFailed("summarize --mode %s 失败（退出码 %d）\nstdout 尾部:\n%s\nstderr 尾部:\n%s"
                          % (mode, result.returncode, tail(result.stdout), tail(result.stderr)))
    return result


def reduce_user_text(entry):
    """从 requests.jsonl 的一条记录里取出 reduce 的 user 内容。"""
    for message in reversed((entry.get("request") or {}).get("messages") or []):
        if message.get("role") == "user":
            return str(message.get("content") or "")
    return ""


def position_of(text, needle):
    index = text.find(needle)
    if index < 0:
        raise CheckFailed("reduce 请求里找不到这段事实: %r" % needle)
    return index


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    parser = argparse.ArgumentParser(description="dsh-videokit 尾部召回验收（零网络）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    args = parser.parse_args()

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    work_root = tempfile.mkdtemp(prefix="videokit-needle-")
    print("临时目录: %s" % work_root)
    mock = None
    try:
        index_dir = os.path.join(work_root, "index")
        log_dir = os.path.join(work_root, "log")
        timeline = build_index_dir(index_dir, log_dir)
        needled = [event for event in timeline["events"] if NEEDLE in event["text"]]
        print("时间线: %d 条事件 / %.0f s；含 %s 的事件 %d 条"
              % (len(timeline["events"]), DURATION, NEEDLE, len(needled)))
        if len(needled) < 3:
            raise CheckFailed("造数据失败：只放了 %d 条含 %s 的事件" % (len(needled), NEEDLE))

        mock, port = start_mock(log_dir)
        print("mock LLM: http://127.0.0.1:%d/v1" % port)
        config_path = os.path.join(work_root, "dsh-videokit.json")
        workdir = os.path.join(work_root, "work")
        os.makedirs(workdir, exist_ok=True)
        write_json(config_path, {
            "workdir": workdir,
            "asr": {"base": "", "key": "", "model": "", "auth": "api-key", "chunkMinutes": 15},
            "vision": {"base": "", "key": "", "model": "", "auth": "bearer", "maxFrames": 200},
            "llm": {"base": "http://127.0.0.1:%d/v1" % port, "key": "sk-test-needle-123456",
                    "model": "mock-llm", "auth": "bearer", "maxTokens": 1024},
        })
        requests_log = os.path.join(log_dir, "requests.jsonl")
        evidence = {}

        for mode in ("mapreduce", "tail-first"):
            print("\n=== summarize --mode %s --block-chars %d ===" % (mode, BLOCK_CHARS))
            before = len(read_log(requests_log))
            result = run_summarize(index_dir, config_path, workdir, mode)
            summary = json.load(open(os.path.join(index_dir, "summary.json"), encoding="utf-8-sig"))
            del result  # stdout 只在失败时才有用
            new_entries = read_log(requests_log)[before:]
            reduces = [entry for entry in new_entries if entry.get("kind") == "reduce"]
            expect(len(reduces) == 1, "%s：mock 收到 1 次 reduce 请求（实际 %d）" % (mode, len(reduces)))
            facts = summary.get("facts") or []
            expect(any(NEEDLE in fact for fact in facts),
                   "%s：facts 里出现 %s（共 %d 条事实）" % (mode, NEEDLE, len(facts)),
                   detail="尾部三条 NEEDLE 事件被吞掉了；facts 前 5 条:\n%s" % "\n".join(facts[:5]))
            block_count = int(summary.get("blockCount") or 0)
            expect(block_count >= 4, "%s：时间线切成 ≥4 块（实际 %d）" % (mode, block_count),
                   detail="调小 --block-chars 或加长事件文本")
            blocks = summary.get("blocks") or []
            expect(len(blocks) == block_count, "%s：blocks 数量与 blockCount 一致" % mode)
            first_fact = (blocks[0].get("facts") or [None])[0]
            last_fact = (blocks[-1].get("facts") or [None])[0]
            expect(bool(first_fact) and bool(last_fact), "%s：首块与末块都有事实" % mode)
            expect(first_fact != last_fact, "%s：首块第一条与末块第一条不是同一条事实" % mode,
                   detail="首块=%r 末块=%r" % (first_fact, last_fact))
            reduce_text = reduce_user_text(reduces[0])
            evidence[mode] = {
                "blocks": blocks, "facts": facts, "reduce_text": reduce_text,
                "first_fact": first_fact, "last_fact": last_fact,
            }
            expect(NEEDLE in reduce_text, "%s：reduce 请求正文里带着 %s" % (mode, NEEDLE))

        # ---- 顺序断言 ----
        print("\n=== reduce 请求里的块顺序 ===")
        mapreduce = evidence["mapreduce"]
        tail_first = evidence["tail-first"]
        mr_first = position_of(mapreduce["reduce_text"], mapreduce["first_fact"])
        mr_last = position_of(mapreduce["reduce_text"], mapreduce["last_fact"])
        tf_first = position_of(tail_first["reduce_text"], tail_first["first_fact"])
        tf_last = position_of(tail_first["reduce_text"], tail_first["last_fact"])

        print("  mapreduce 首块第一条事实的位置: %d" % mr_first)
        print("  mapreduce 末块第一条事实的位置: %d" % mr_last)
        print("  tail-first 首块第一条事实的位置: %d" % tf_first)
        print("  tail-first 末块第一条事实的位置: %d" % tf_last)

        expect(mr_first < mr_last, "mapreduce：首块事实排在末块事实之前（正序）",
               detail="首块位置 %d 未小于末块位置 %d" % (mr_first, mr_last))
        expect(tf_last < tf_first, "tail-first：末块第一条事实排在首块事实之前（尾部优先）",
               detail="末块位置 %d 未小于首块位置 %d —— reduce 没有真的尾部优先" % (tf_last, tf_first))

        print("\ntail-first reduce 请求开头 400 字：")
        print(tail_first["reduce_text"][:400])
        print("\n全部通过")
        return 0
    except CheckFailed as exc:
        sys.stderr.write("\n[失败] %s\n" % exc)
        return 1
    except subprocess.TimeoutExpired as exc:
        sys.stderr.write("\n[失败] 命令超时: %s\n" % exc)
        return 1
    finally:
        stop_mock(mock)
        if args.keep:
            print("保留临时目录: %s" % work_root)
        else:
            shutil.rmtree(work_root, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
