#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""端到端测试（零网络）：mock LLM + test/fixture.mp4 走完 probe → index → timeline → frames → summarize → report。

全部走 assert，任何一步失败就打印该步的 stdout/stderr 尾部并以退出码 1 结束。
临时目录默认建在 %TEMP%，跑完删掉（--keep 留着排查）。

用法：python test/e2e_mock_test.py [--keep] [--verbose]
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
FIXTURE = os.path.join(HERE, "fixture.mp4")
MOCK = os.path.join(HERE, "mock_llm.py")
MAKE_FIXTURE = os.path.join(HERE, "make_fixture.py")
PYTHON = sys.executable

KNOWN_FFMPEG = os.environ.get("VIDEOKIT_FFMPEG") or ""
KNOWN_FFPROBE = os.environ.get("VIDEOKIT_FFPROBE") or ""

VERBOSE = False


# ---------------------------------------------------------------------------
# 基础设施
# ---------------------------------------------------------------------------


class StepFailed(AssertionError):
    pass


def step(title):
    """打印一行步骤标题（跑完才算数）。"""
    print("\n=== %s ===" % title)


def ok(message):
    print("  [OK] %s" % message)


def expect(condition, message, detail=None):
    if condition:
        ok(message)
        return
    raise StepFailed("%s%s" % (message, ("\n诊断:\n" + str(detail)) if detail else ""))


def find_binary(name, known):
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    return known if os.path.isfile(known) else None


def tail(text, lines=20):
    rows = (text or "").strip().splitlines()
    return "\n".join(rows[-lines:])


def run_cli(argv, timeout=900):
    command = [PYTHON, SCRIPT] + list(argv)
    if VERBOSE:
        print("  $ %s" % " ".join(command))
    result = subprocess.run(
        command, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout, cwd=ROOT
    )
    return result


def run_cli_ok(argv, timeout=900):
    result = run_cli(argv, timeout)
    if result.returncode != 0:
        raise StepFailed(
            "命令失败（退出码 %d）: %s\nstdout 尾部:\n%s\nstderr 尾部:\n%s"
            % (result.returncode, " ".join(argv), tail(result.stdout), tail(result.stderr))
        )
    if VERBOSE:
        print(tail(result.stdout, 40))
    return result


def start_mock(log_dir):
    """拉起 mock_llm，读第一行 PORT=<n>。"""
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
                raise StepFailed("mock_llm 启动即退出: %s" % tail(process.stderr.read()))
            continue
        if line.startswith("PORT="):
            return process, int(line.strip().split("=", 1)[1])
    process.kill()
    raise StepFailed("mock_llm 30 秒内没有打印 PORT=")


def stop_mock(process):
    if process is None or process.poll() is not None:
        return
    process.terminate()
    try:
        process.wait(timeout=10)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)


def write_config(path, port, workdir):
    config = {
        "pythonPath": PYTHON,
        "ffmpeg": find_binary("ffmpeg", KNOWN_FFMPEG) or "",
        "ffprobe": find_binary("ffprobe", KNOWN_FFPROBE) or "",
        "workdir": workdir,
        "asr": {"base": "http://127.0.0.1:%d/v1" % port, "key": "sk-test-e2e-1234567",
                "model": "mock-asr", "auth": "bearer", "chunkMinutes": 1},
        "vision": {"base": "http://127.0.0.1:%d/v1" % port, "key": "sk-test-e2e-1234567",
                   "model": "mock-vlm", "auth": "bearer", "maxFrames": 200},
        "llm": {"base": "http://127.0.0.1:%d/v1" % port, "key": "sk-test-e2e-1234567",
                "model": "mock-llm", "auth": "bearer", "maxTokens": 1024},
    }
    with open(path, "w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
    return config


def load_json(path):
    with open(path, "r", encoding="utf-8-sig") as handle:
        return json.load(handle)


def count_files(directory, suffix):
    if not os.path.isdir(directory):
        return 0
    return len([name for name in os.listdir(directory) if name.lower().endswith(suffix)])


# ---------------------------------------------------------------------------
# 各步骤
# ---------------------------------------------------------------------------


def check_probe(config_path, workdir, out_dir):
    step("probe fixture")
    result = run_cli_ok(["probe", FIXTURE, "--out", out_dir, "--config", config_path, "--workdir", workdir])
    probe = load_json(os.path.join(out_dir, "probe.json"))
    duration = float(probe.get("durationSec") or 0)
    expect(abs(duration - 90.0) <= 1.5, "probe.json durationSec ≈ 90（实际 %.2f）" % duration)
    expect(int(probe.get("width")) == 640 and int(probe.get("height")) == 360,
           "分辨率 640x360（实际 %sx%s）" % (probe.get("width"), probe.get("height")))
    expect(bool(probe.get("hasAudio")), "有音轨")
    for line in ("文件:", "时长:", "画面:", "音频:", "章节:", "工作目录:"):
        expect(line in result.stdout, "stdout 含 %r 行" % line)
    return probe


def check_index(config_path, workdir, out_dir, probe):
    step("index fixture --max-frames 12")
    result = run_cli_ok(["index", FIXTURE, "--out", out_dir, "--max-frames", "12",
                         "--config", config_path, "--workdir", workdir])

    timeline_path = os.path.join(out_dir, "timeline.json")
    expect(os.path.isfile(timeline_path), "timeline.json 已生成")
    timeline = load_json(timeline_path)
    events = timeline.get("events") or []
    expect(len(events) > 0, "timeline.events 非空（%d 条）" % len(events))

    times = [float(event.get("t0") or 0.0) for event in events]
    expect(all(b >= a - 1e-9 for a, b in zip(times, times[1:])), "events 的 t0 单调不减",
           "前 10 个 t0: %r" % (times[:10],))

    chapters = load_json(os.path.join(out_dir, "chapters.json"))
    expect(isinstance(chapters, list) and len(chapters) > 0, "chapters.json 非空（%d 章）" % len(chapters))

    asr_count = count_files(os.path.join(out_dir, "asr"), ".json")
    expect(asr_count == 2, "asr/ 恰有 2 个 json（chunkMinutes=1 → 90s 切两段，实际 %d）" % asr_count)

    frame_count = count_files(os.path.join(out_dir, "frames"), ".jpg")
    expect(3 < frame_count <= 12, "frames/ 的 jpg 数量在 (3, 12]（实际 %d）" % frame_count)

    frame_meta = count_files(os.path.join(out_dir, "frames"), ".json")
    expect(frame_meta == frame_count, "每张帧图都有同名 json（%d vs %d）" % (frame_meta, frame_count))

    index_doc = load_json(os.path.join(out_dir, "index.json"))
    expect(isinstance(index_doc, dict) and index_doc.get("durationSec"), "index.json 可解析且有 durationSec")
    expect((index_doc.get("asr") or {}).get("failed") == 0,
           "ASR 无失败段（failed=%r）" % (index_doc.get("asr") or {}).get("failed"))
    expect((index_doc.get("vision") or {}).get("described") == frame_count,
           "所有帧都拿到了描述（described=%r，帧=%d）"
           % ((index_doc.get("vision") or {}).get("described"), frame_count))

    stats = timeline.get("stats") or {}
    expect(stats.get("speech", 0) > 0, "有语音事件（%d 条）" % stats.get("speech", 0))
    expect(stats.get("scene", 0) > 0, "有画面事件（%d 条）" % stats.get("scene", 0))
    expect(stats.get("ocr", 0) > 0, "有 OCR 事件（%d 条，mock 每 3 帧回一次 NEEDLE-OCR）" % stats.get("ocr", 0))

    for line in ("索引完成:", "时长:", "时间线:"):
        expect(line in result.stdout, "stdout 含 %r 行" % line)
    return timeline


def check_timeline(config_path, workdir, out_dir):
    step("timeline <dir> --tail 5")
    result = run_cli_ok(["timeline", out_dir, "--tail", "5", "--config", config_path, "--workdir", workdir])
    lines = [line for line in result.stdout.splitlines() if line.strip()]
    body = [line for line in lines if line.startswith("[")]
    expect(1 <= len(body) <= 5, "事件行数在 1..5 之间（实际 %d）" % len(body))
    expect(all(line.startswith("[") for line in body), "事件行都以 '[' 开头")
    expect(any("时间线" in line for line in lines), "有 SPEC 要求的头部行")
    for line in body:
        print("    " + line)


def check_frames(config_path, workdir, out_dir):
    step("frames <video> --from 10 --to 14")
    result = run_cli_ok(["frames", FIXTURE, "--out", out_dir, "--from", "10", "--to", "14",
                         "--config", config_path, "--workdir", workdir])
    images = count_files(out_dir, ".jpg")
    expect(images >= 4, "抽到 ≥4 张图（实际 %d）" % images)
    manifest = load_json(os.path.join(out_dir, "frames.json"))
    expect(int(manifest.get("count") or 0) == images, "frames.json 的 count 与实际文件数一致")
    expect(len(manifest.get("files") or []) == images, "frames.json files 长度一致")
    expect("抽帧完成:" in result.stdout and "清单:" in result.stdout, "stdout 含抽帧完成/清单行")
    return images


def check_summarize(config_path, workdir, out_dir):
    step("summarize --mode mapreduce")
    result = run_cli_ok(["summarize", out_dir, "--mode", "mapreduce",
                         "--config", config_path, "--workdir", workdir])
    summary = load_json(os.path.join(out_dir, "summary.json"))
    facts = summary.get("facts") or []
    expect(len(facts) > 0, "summary.json 的 facts 非空（%d 条）" % len(facts))
    expect(int(summary.get("blockCount") or 0) >= 1, "blockCount ≥1（%r）" % summary.get("blockCount"))
    expect(not (summary.get("reduce") or {}).get("error"), "reduce 没有 error")
    expect(os.path.isfile(os.path.join(out_dir, "summary.md")), "summary.md 已生成")
    expect("摘要:" in result.stdout, "stdout 含 摘要: 行")
    for fact in facts:
        print("    " + fact)
    return summary


def check_report(config_path, workdir, out_dir):
    step("report <dir>")
    result = run_cli_ok(["report", out_dir, "--config", config_path, "--workdir", workdir])
    report_path = os.path.join(out_dir, "report.md")
    expect(os.path.isfile(report_path), "report.md 已生成")
    with open(report_path, "r", encoding="utf-8") as handle:
        text = handle.read()
    for heading in ("## 章节时间线", "## 尾部事实清单"):
        expect(heading in text, "报告含 %r" % heading)
    for heading in ("## 概要", "## 基本信息", "## 关键结论（来自 map-reduce）", "## 关键帧", "## 端点与统计"):
        expect(heading in text, "报告含 %r" % heading)
    expect("报告已生成:" in result.stdout, "stdout 含 报告已生成: 行")
    expect("mock-asr" in text and "mock-vlm" in text and "mock-llm" in text, "端点表里有三个模型名")
    expect("sk-test-e2e-1234567" not in text, "报告里没有泄漏 API key")
    return report_path


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------


def main():
    global VERBOSE
    parser = argparse.ArgumentParser(description="dsh-videokit 端到端测试（零网络）")
    parser.add_argument("--keep", action="store_true", help="保留临时目录")
    parser.add_argument("--verbose", action="store_true", help="打印每条命令的完整输出")
    args = parser.parse_args()
    VERBOSE = args.verbose

    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass

    if not os.path.isfile(FIXTURE):
        print("素材不存在，先生成：python test/make_fixture.py")
        if subprocess.run([PYTHON, MAKE_FIXTURE], cwd=ROOT).returncode != 0:
            sys.stderr.write("生成素材失败\n")
            return 1

    work_root = tempfile.mkdtemp(prefix="videokit-e2e-")
    print("临时目录: %s" % work_root)
    mock = None
    started = time.time()
    try:
        mock, port = start_mock(os.path.join(work_root, "log"))
        print("mock LLM: http://127.0.0.1:%d/v1" % port)
        config_path = os.path.join(work_root, "dsh-videokit.json")
        workdir = os.path.join(work_root, "work")
        os.makedirs(workdir, exist_ok=True)
        write_config(config_path, port, workdir)

        probe_out = os.path.join(work_root, "probe-out")
        index_out = os.path.join(work_root, "index-out")
        frames_out = os.path.join(work_root, "frames-out")

        check_probe(config_path, workdir, probe_out)
        check_index(config_path, workdir, index_out, None)
        check_timeline(config_path, workdir, index_out)
        check_frames(config_path, workdir, frames_out)
        check_summarize(config_path, workdir, index_out)
        check_report(config_path, workdir, index_out)

        requests_log = os.path.join(work_root, "log", "requests.jsonl")
        with open(requests_log, "r", encoding="utf-8") as handle:
            entries = [json.loads(line) for line in handle if line.strip()]
        kinds = {}
        for entry in entries:
            kinds[entry.get("kind")] = kinds.get(entry.get("kind"), 0) + 1
        step("mock 请求统计")
        print("    %s" % json.dumps(kinds, ensure_ascii=False))
        expect(kinds.get("asr", 0) == 2, "mock 收到 2 次 ASR 请求（实际 %d）" % kinds.get("asr", 0))
        expect(all(entry.get("auth") == "bearer" for entry in entries), "所有请求都带了 Bearer 头")

        print("\n全部通过，用时 %.1f s" % (time.time() - started))
        return 0
    except StepFailed as exc:
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
