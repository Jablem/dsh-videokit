#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""生成 test/fixture.mp4 —— 端到端测试的素材，零网络。

素材规格（SPEC 之外的自测素材，不属于契约）：
  90 s / 640x360 / 25 fps / H.264 + AAC（单声道 44.1 kHz）
  画面：testsrc2 打底，在 9/27/45/63/81 s 各来一次整帧黑场（0.4 s）—— 硬切足够大，
        场景检测一定能出帧；中段再叠一段 drawtext（有字体时），制造"画面里出现文字"的样本。
  音频：440 Hz 与 880 Hz 两个正弦交替，每 15 s 换一次。

编码器：这台机器的 ffmpeg 没编 libx264，所以优先 h264_mf（Windows MediaFoundation），
失败再退 libx264 / mpeg4。退到 mpeg4 时会打一行提示——测试逻辑不依赖编码器。

可重复运行：已存在就跳过，--force 覆盖。
"""

import argparse
import os
import shutil
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
FIXTURE = os.path.join(HERE, "fixture.mp4")

KNOWN_FFMPEG = os.environ.get("VIDEOKIT_FFMPEG") or ""
KNOWN_FFPROBE = os.environ.get("VIDEOKIT_FFPROBE") or ""
FONT_CANDIDATES = (
    "C:/Windows/Fonts/arial.ttf",
    "C:/Windows/Fonts/segoeui.ttf",
    "C:/Windows/Fonts/msyh.ttc",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
)

DURATION = 90
WIDTH, HEIGHT, FPS = 640, 360, 25
FLASHES = (9.0, 27.0, 45.0, 63.0, 81.0)
FLASH_LEN = 0.4
SINE_SEGMENT = 15
ENCODERS = ("h264_mf", "libx264", "mpeg4")
TEXT_AT = 44.0


def find_binary(name, known):
    found = shutil.which(name) or shutil.which(name + ".exe")
    if found:
        return found
    return known if os.path.isfile(known) else None


def find_font():
    for path in FONT_CANDIDATES:
        if os.path.isfile(path):
            return path
    return None


def video_filter(with_text):
    """整帧黑场是最可靠的硬切：任意源 → 全黑，scene 分数接近 1。"""
    boxes = "".join(
        "drawbox=x=0:y=0:w=%d:h=%d:color=black@1:t=fill:enable='between(t,%.2f,%.2f)',"
        % (WIDTH, HEIGHT, moment, moment + FLASH_LEN)
        for moment in FLASHES
    )
    text = ""
    if with_text:
        font = find_font()
        if font:
            # ffmpeg 的 filter 参数里 Windows 盘符的冒号必须转义
            escaped = font.replace(":", r"\:")
            text = (
                "drawtext=fontfile='%s':text='SCENE 02':fontcolor=white:fontsize=64:"
                "x=(w-tw)/2:y=(h-th)/2:box=1:boxcolor=black@0.6:boxborderw=12:"
                "enable='between(t,%.1f,%.1f)'," % (escaped, TEXT_AT, TEXT_AT + 2.0)
            )
    return "%s%sscale=%d:%d" % (boxes, text, WIDTH, HEIGHT)


def build_command(ffmpeg, encoder, with_text, target):
    audio = "[1:a][2:a][1:a][2:a][1:a][2:a]concat=n=6:v=0:a=1[a]"
    graph = "[0:v]%s[v];%s" % (video_filter(with_text), audio)
    return [
        ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
        "-f", "lavfi", "-i", "testsrc2=size=%dx%d:rate=%d:duration=%d" % (WIDTH, HEIGHT, FPS, DURATION),
        "-f", "lavfi", "-i", "sine=frequency=440:sample_rate=44100:duration=%d" % SINE_SEGMENT,
        "-f", "lavfi", "-i", "sine=frequency=880:sample_rate=44100:duration=%d" % SINE_SEGMENT,
        "-filter_complex", graph,
        "-map", "[v]", "-map", "[a]",
        "-c:v", encoder, "-pix_fmt", "yuv420p", "-r", str(FPS),
        "-c:a", "aac", "-b:a", "64k", "-ar", "44100",
        target,
    ]


def run(args, timeout=600):
    return subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)


def probe_duration(ffprobe, path):
    result = run([ffprobe, "-v", "error", "-show_entries", "format=duration",
                  "-of", "default=nw=1:nk=1", path], timeout=120)
    if result.returncode != 0:
        return None
    try:
        return float((result.stdout or "").strip().splitlines()[0])
    except (ValueError, IndexError):
        return None


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    parser = argparse.ArgumentParser(description="生成测试素材 test/fixture.mp4")
    parser.add_argument("--force", action="store_true", help="已存在也重新生成")
    parser.add_argument("--out", default=FIXTURE, help="输出路径（默认 test/fixture.mp4）")
    args = parser.parse_args()
    target = os.path.abspath(args.out)

    ffmpeg = find_binary("ffmpeg", KNOWN_FFMPEG)
    ffprobe = find_binary("ffprobe", KNOWN_FFPROBE)
    if not ffmpeg:
        sys.stderr.write("找不到 ffmpeg，无法生成素材。\n")
        return 1

    if os.path.isfile(target) and os.path.getsize(target) > 0 and not args.force:
        print("素材已存在，跳过: %s (%s)" % (target, _mb(target)))
        if ffprobe:
            print("时长: %.2f s" % (probe_duration(ffprobe, target) or 0))
        return 0

    os.makedirs(os.path.dirname(target) or ".", exist_ok=True)
    last_error = ""
    for encoder in ENCODERS:
        for with_text in (True, False):
            label = "%s%s" % (encoder, "+drawtext" if with_text else "")
            result = run(build_command(ffmpeg, encoder, with_text, target))
            if result.returncode == 0 and os.path.isfile(target) and os.path.getsize(target) > 0:
                duration = probe_duration(ffprobe, target) if ffprobe else None
                print("已生成: %s (%s)" % (target, _mb(target)))
                print("编码器: %s · 分辨率: %dx%d · %d fps · 时长: %.2f s"
                      % (label, WIDTH, HEIGHT, FPS, duration or 0))
                if encoder == "mpeg4":
                    print("· 提示：这个 ffmpeg 没有 H.264 编码器，素材退化为 mpeg4（测试逻辑不依赖编码器）")
                if not with_text:
                    print("· 提示：没找到可用字体，已跳过 drawtext")
                return 0
            last_error = (result.stderr or "").strip().splitlines()[-6:]
            last_error = "\n".join(last_error)
            if os.path.isfile(target):
                os.remove(target)  # 别留下半截文件

    sys.stderr.write("生成素材失败（所有编码器都试过了）：\n%s\n" % last_error)
    return 1


def _mb(path):
    return "%.1f MB" % (os.path.getsize(path) / (1024 * 1024))


if __name__ == "__main__":
    sys.exit(main())
