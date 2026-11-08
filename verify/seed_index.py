#!/usr/bin/env python3
"""造一个"假索引目录"：不碰 ffmpeg、不调 ASR/视觉，只写 SPEC §2.3 规定的
timeline.json / chapters.json / index.json / probe.json。

用途（Lead 侧独立验收，不依赖引擎的 test/ 目录）：
  - 给 summarize / report 提供输入
  - 把 needle（结论句）埋在 45 分钟视频的第 40 分钟，用于真实模型的尾部召回测试

用法:
  python verify/seed_index.py --out <DIR> [--minutes 45] [--needle-at-min 40]
"""

import argparse
import json
import os
import sys

NEEDLE_TEXT = ("NEEDLE 这一段是全部内容里唯一的结论：主持人明确宣布最终答案是 42，"
               "并且这个数字只在这里出现过一次。")
FILLER = [
    "主持人在介绍今天的流程安排。",
    "画面切到工作台，工作人员开始整理材料。",
    "有人提出问题，讨论了一下时间安排。",
    "镜头拉近，展示了一张流程表。",
    "旁白在解释背景信息，节奏比较慢。",
    "两位嘉宾交换了一下意见。",
    "画面短暂黑屏后切回现场。",
    "主持人重复了一遍刚才的要点。",
]

CN_DIGITS = "零一二三四五六七八九"


def cn_num(n):
    """0..99 → 中文数字。用它的唯一目的：让每条填充文本互不相同（便于模型
    去重时不会把整段内容压成一条），同时**不引入任何 ASCII 数字**，否则
    "42" 这个召回断言会被 [42:30] 这类时间戳污染。"""
    if n < 10:
        return CN_DIGITS[n]
    tens, ones = divmod(n, 10)
    head = "" if tens == 1 else CN_DIGITS[tens]
    return head + "十" + (CN_DIGITS[ones] if ones else "")


def mmss(seconds):
    s = int(seconds)
    return "%d:%02d" % (s // 60, s % 60)


def build(args):
    duration = args.minutes * 60
    needle_at = args.needle_at_min * 60
    events = []
    step = 30
    t = 0.0
    filler_i = 0
    while t < duration:
        t0 = float(t)
        t1 = min(duration, t0 + step - 2)  # 留 2 秒静音，制造章节边界
        if abs(t0 - needle_at) < step / 2:
            text = NEEDLE_TEXT
        else:
            # 填充文本里不出现任何 ASCII 数字（时间戳由 block 拼装时另加），
            # 否则 "42" 这种召回断言会被 "42:30" 自身污染，测试就失去意义了。
            text = "%s这一段的主题是第%s分钟的细节。" % (
                FILLER[filler_i % len(FILLER)], cn_num(int(t0) // 60))
            filler_i += 1
        events.append({"t0": t0, "t1": t1, "kind": "speech", "text": text,
                       "source": "asr", "estimated": False})
        # 每 5 分钟配一条画面描述，模拟场景帧
        if int(t0) % 300 == 0:
            events.append({"t0": t0, "t1": t0, "kind": "scene",
                           "text": "画面：现场全景，白板上有流程图。", "source": "vlm",
                           "frame": "frames/f%03d_t%08.1f.jpg" % (len(events) // 9, t0)})
        t += step
    events.sort(key=lambda e: e["t0"])

    # 章节：按静音边界粗切（每 5 分钟一章）
    chapters = []
    by_end = duration
    start = 0.0
    idx = 0
    while start < by_end:
        end = min(by_end, start + 300)
        first = next((e for e in events if start <= e["t0"] < end), None)
        chapters.append({
            "index": idx + 1,  # 与引擎一致：chapters[].index 1 起始
            "t0": start,
            "t1": end,
            "title": (first["text"][:24] if first else "画面片段 %d" % idx),
            "summary": (first["text"][:160] if first else ""),
        })
        idx += 1
        start = end

    stats = {"speech": 0, "scene": 0, "ocr": 0}
    for e in events:
        stats[e["kind"]] = stats.get(e["kind"], 0) + 1

    probe = {
        "path": os.path.abspath(os.path.join(args.out, "synthetic.mp4")),
        "sizeBytes": 0, "durationSec": float(duration), "width": 1920, "height": 1080,
        "fps": 30.0, "bitrateKbps": 2400, "videoCodec": "h264", "audioCodec": "aac",
        "audioChannels": 2, "audioSampleRate": 44100, "chapters": [], "hasAudio": True,
    }
    # 关键帧占位文件，让 report 的"关键帧"小节有东西可列
    frames_dir = os.path.join(args.out, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    frame_paths = []
    for e in events:
        if e["kind"] == "scene":
            rel = e["frame"]
            abs_path = os.path.join(args.out, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(abs_path), exist_ok=True)
            with open(abs_path, "wb") as f:
                f.write(b"\xff\xd8\xff\xd9")  # 最小 jpeg 骨架，只占位
            frame_paths.append(rel)

    timeline = {"version": 1, "video": probe["path"], "durationSec": float(duration),
                "events": events, "stats": stats}
    index = {
        "video": probe["path"], "durationSec": float(duration),
        "workdir": os.path.abspath(args.out), "probe": probe,
        "audio": {"parts": 3, "chunkSeconds": 900, "coveredSec": float(duration)},
        "asr": {"ok": True, "failed": 0, "model": "synthetic", "parts": []},
        "vision": {"frames": len(frame_paths), "described": len(frame_paths),
                   "failed": 0, "model": "synthetic"},
        "timeline": {"events": len(events), "stats": stats},
        "chapters": {"count": len(chapters)},
        "startedAt": "2026-11-08T00:00:00", "finishedAt": "2026-11-08T00:00:00",
    }

    os.makedirs(args.out, exist_ok=True)
    for name, obj in (("probe.json", probe), ("timeline.json", timeline),
                      ("chapters.json", chapters), ("index.json", index)):
        with open(os.path.join(args.out, name), "w", encoding="utf-8") as f:
            json.dump(obj, f, ensure_ascii=False, indent=1)
    return len(events), len(chapters), needle_at


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", required=True)
    ap.add_argument("--minutes", type=int, default=45)
    ap.add_argument("--needle-at-min", type=int, default=40)
    args = ap.parse_args()
    args.out = os.path.abspath(args.out)
    n_events, n_chapters, needle_at = build(args)
    print("已生成假索引: %s" % args.out)
    print("事件 %d 条 / 章节 %d 个 / needle 埋点 %s" % (n_events, n_chapters, mmss(needle_at)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
