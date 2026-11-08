#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""dsh-videokit 的 Python 引擎：ffmpeg/ffprobe 抽音频抽帧、OpenAI 兼容端点调用、时间线与报告。

只用标准库。契约见仓库根目录 SPEC.md（§2 是 CLI 契约，任何命令/字段改动都要先改那里）。

设计取舍：
  * 所有路径在入口处立刻 resolve 成绝对路径 —— host 用 execFile 跑我们，cwd 是配置里的
    workdir 而不是插件目录，相对路径随时会漂。
  * HTTP 一律走 chat_post()，它保证「不抛异常」：重活是多阶段并发的，一处网络抖动不该炸掉
    整个索引，失败只记 error 字段继续。
  * 进度行进缓冲区、命令结束时一次性 flush —— SPEC 要求单命令输出超过 200 行时截断到末尾，
    流式打印做不到这一点。关键结果行走 say() 立刻输出。
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import time
import traceback
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

IS_WIN = os.name == "nt"

# 二进制定位顺序：配置文件 > 环境变量 > shutil.which()（PATH）> 下面这些常见安装位置。
# 刻意不放任何个人目录 —— 装到非常规位置的用户设 VIDEOKIT_FFMPEG / VIDEOKIT_FFPROBE 即可。
KNOWN_FFMPEG = os.environ.get("VIDEOKIT_FFMPEG") or ""
KNOWN_FFPROBE = os.environ.get("VIDEOKIT_FFPROBE") or ""
DEFAULT_WORKDIR = os.path.join(os.path.expanduser("~"), "videokit")

DEFAULT_CONFIG = {
    "pythonPath": "",
    "ffmpeg": "",
    "ffprobe": "",
    "workdir": DEFAULT_WORKDIR,
    "asr": {"base": "", "key": "", "model": "mimo-v2.5-asr", "auth": "api-key", "chunkMinutes": 5},
    "vision": {"base": "", "key": "", "model": "glm-4.1v-thinking-flash", "auth": "bearer", "maxFrames": 200},
    "llm": {"base": "", "key": "", "model": "deepseek-chat", "auth": "bearer", "maxTokens": 2048},
}
SECTIONS = ("asr", "vision", "llm")

# SPEC §2.3 固定 prompt —— 改这里就等于改契约。
VLM_PROMPT = (
    "你在看一段视频里的某一帧。请严格按下面两行输出，不要多余客套：\n"
    "画面：<这一帧发生了什么，主体、动作、场景，≤120字>\n"
    "文字：<逐字抄录画面上所有可见文字（界面、标题、字幕、数字、弹幕）；确实没有就写 无文字>"
)
MAP_SYSTEM = "你是视频内容的结构化抽取器。只输出事实，不写评论、不写总结性套话。"
REDUCE_SYSTEM = "你是去重合并器。只做去重与按时间排序，禁止删除、禁止压缩、禁止改写事实。"

B64_MB_LIMIT = 9.5          # 单段音频 base64 后的软上限（服务端普遍 ~10MB body）
MIN_CHUNK_SECONDS = 120.0   # 自动减半的下限，避免碎片化请求
MIN_TAIL_SECONDS = 5.0      # plan_chunks 的末段合并阈值
SCENE_THRESHOLD = 0.3       # SPEC 指定的场景分阈值
PROGRESS_CAP = 200          # SPEC：单条命令进度行超过 200 行时截断到末尾
MAX_TIMELINE_CHARS = 300    # 单行时间线正文上限
SCENE_SUMMARY_CHARS = 80    # 汇总阶段画面描述只取前 N 字当佐证（见 summarize_rows）

# ---- ASR 输出塌缩检测 ------------------------------------------------------
# 实测 MiMo（同一段 16k 单声道口播音频，起点都是 0，内容应单调增长）：
#     300 s → 1529 字（5.10 字/秒）✓      450 s → 2255 字（5.01 字/秒）✓
#     572 s → 1229 字（2.15 字/秒）△      600 s →  146 字（0.24 字/秒）✗
#     750 s → 1238 字（1.65 字/秒）✗      900 s →  371 字（0.41 字/秒）✗
# 塌缩的响应 finish_reason 仍是 stop、没有错误码，只是模型悄悄跳读、直接跳到
# 片尾台词 —— 唯一能发现的信号就是"字数 / 音频时长"。
# 阈值取 3.0（正常中文口播 4~6 字/秒）：能抓住 572 s 那种"半个字也没少太多"的
# 轻度省略，又不会误切 300/450 s 的正常段。误判的代价只是多切一刀重转，
# 而漏判的代价是整段内容凭空消失。
#
# 反向也会出事：实测同一段 300 s 数学课音频，整段回了 3195 字（10.6 字/秒），
# 而切成两个 150 s 分别转只有 902 + 862 = 1764 字 —— 那次是**膨胀/复读**。
# 所以两头都要卡：低于 MIN 或高于 MAX 都判可疑，一律对半重转。
MIN_CHARS_PER_SEC = 3.0
MAX_CHARS_PER_SEC = 8.0     # 中文口播的上界（含标点）；超过就是复读/扩写
ASR_SPLIT_FLOOR = 150.0     # 对半重试的最小段长（秒），避免无限细分
MAX_ASR_DEPTH = 2           # 对半重试的最大递归深度（最坏 7 次调用/段，有界）
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120 Safari/537.36")
BILI_HEADERS = {"User-Agent": UA, "Referer": "https://www.bilibili.com/"}
KIND_ICON = {"speech": "🗣", "scene": "🎬", "ocr": "🔤"}
FACT_RE = re.compile(r"^\[(\d{1,2}):(\d{2})(?::(\d{2}))?\]")


# ---------------------------------------------------------------------------
# 错误与输出
# ---------------------------------------------------------------------------


class FatalError(RuntimeError):
    """致命错误：入口捕获后打中文到 stderr 并退出码 1。"""


class MissingDependency(FatalError):
    """可选依赖缺失且已给出安装提示 → SPEC 要求退出码 2（而不是 1）。"""


_PROGRESS = []
_FLUSHED = False


def note(msg, mark="·"):
    """进度行：进缓冲区，命令结束时统一输出（超过上限只留末尾）。"""
    _PROGRESS.append("%s %s" % (mark, msg))


def arrow(msg):
    note(msg, mark="›")


def say(msg=""):
    """关键结果行：立刻输出 —— 模型要读的内容不能被截断规则吞掉。"""
    print(msg)
    sys.stdout.flush()


def flush_progress():
    """把进度缓冲刷到 stdout；超过上限时只保留末尾（SPEC §2 通用约定）。"""
    global _FLUSHED
    if _FLUSHED or not _PROGRESS:
        return
    _FLUSHED = True
    lines = _PROGRESS
    if len(lines) > PROGRESS_CAP:
        say("· …（进度共 %d 行，已截断只保留末尾 %d 行）" % (len(lines), PROGRESS_CAP))
        lines = lines[-PROGRESS_CAP:]
    for line in lines:
        print(line)
    sys.stdout.flush()


def reconfigure_stdio():
    """Windows 控制台默认 GBK，中文与 emoji 会炸；显式换成 utf-8 + replace。"""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass


# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------


def deep_merge(base, patch):
    """一层深合并：顶层标量直接覆盖，asr/vision/llm 内部按字段合并。"""
    out = json.loads(json.dumps(base))  # 纯数据，深拷贝最省事
    for key, value in (patch or {}).items() if isinstance(patch, dict) else ():
        if value is None:
            continue
        if key in SECTIONS and isinstance(value, dict):
            merged = dict(out.get(key) or {})
            merged.update({k: v for k, v in value.items() if v is not None})
            out[key] = merged
        elif key in out and not isinstance(out[key], dict):
            out[key] = value
    return out


def config_path(cli_value):
    return cli_value or os.environ.get("VIDEOKIT_CONFIG") or os.path.join(
        os.path.expanduser("~"), ".dsh", "dsh-videokit.json")


def read_config(path):
    """读配置。文件不存在 / JSON 损坏 / 带 BOM 都退化成内置默认值（绝不抛）。"""
    cfg = deep_merge(DEFAULT_CONFIG, {})
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:  # utf-8-sig 顺带吃掉 BOM
            cfg = deep_merge(cfg, json.load(handle))
    except FileNotFoundError:
        pass
    except Exception as exc:  # 坏配置不该阻断管线，顶多让端点看起来没配
        note("配置文件读取失败，已使用默认值：%s（%s）" % (path, exc))
    for name in SECTIONS:
        cfg[name] = deep_merge(DEFAULT_CONFIG[name], cfg.get(name) or {})
        try:
            if name == "asr":
                cfg[name]["chunkMinutes"] = float(cfg[name].get("chunkMinutes") or 5)
            elif name == "vision":
                cfg[name]["maxFrames"] = int(cfg[name].get("maxFrames") or 200)
            else:
                cfg[name]["maxTokens"] = int(cfg[name].get("maxTokens") or 2048)
        except (TypeError, ValueError):
            cfg[name] = deep_merge(DEFAULT_CONFIG[name], {})
    return cfg


def endpoint_ready(section):
    return bool(str(section.get("base") or "").strip() and str(section.get("key") or "").strip())


def resolve_workdir(cfg, cli_value):
    return os.path.abspath(os.path.expanduser(str(cli_value or cfg.get("workdir") or DEFAULT_WORKDIR)))


def find_binary(cfg, name):
    """ffmpeg/ffprobe 定位：配置 → PATH → 已知绝对路径。"""
    configured = str(cfg.get(name) or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    found = shutil.which(name) or shutil.which(name + (".exe" if IS_WIN else ""))
    if found:
        return found
    known = KNOWN_FFMPEG if name == "ffmpeg" else KNOWN_FFPROBE
    return known if os.path.isfile(known) else None


def require_binary(cfg, name):
    path = find_binary(cfg, name)
    if not path:
        raise FatalError("找不到 %s。请先安装 ffmpeg，然后在 设置 → 视频理解 → 全局 里填写 %s 的完整路径。"
                         % (name, name))
    return path


# ---------------------------------------------------------------------------
# 小工具
# ---------------------------------------------------------------------------


def now_iso():
    return datetime.now().astimezone().isoformat(timespec="seconds")


def write_text_atomic(path, text):
    """先写 .tmp 再 os.replace —— 中途被杀不会留下半截 JSON。"""
    directory = os.path.dirname(os.path.abspath(path))
    if directory:
        os.makedirs(directory, exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8", newline="\n") as handle:
        handle.write(text)
    os.replace(tmp, path)


def write_json(path, obj):
    write_text_atomic(path, json.dumps(obj, ensure_ascii=False, indent=2) + "\n")


def load_json(path, default=None):
    try:
        with open(path, "r", encoding="utf-8-sig") as handle:
            return json.load(handle)
    except Exception:
        return default


def oneline(text, limit=MAX_TIMELINE_CHARS):
    """压成单行：换行/制表/多空格归一，超长截断。"""
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[: limit - 1] + "…"


def fmt_hms(seconds):
    """00:45:12 —— probe 行用。"""
    total = max(0, int(round(float(seconds or 0))))
    return "%02d:%02d:%02d" % (total // 3600, (total % 3600) // 60, total % 60)


def fmt_mmss(seconds):
    """mm:ss，超过 1 小时用 h:mm:ss —— 时间线行用。"""
    total = max(0, int(round(float(seconds or 0))))
    hours, rest = divmod(total, 3600)
    return "%d:%02d:%02d" % (hours, rest // 60, rest % 60) if hours else "%02d:%02d" % (rest // 60, rest % 60)


def fmt_fps(value):
    number = float(value or 0)
    return ("%.2f" % number).rstrip("0").rstrip(".") if number else "0"


def fmt_mb(num_bytes):
    return "%.1f MB" % (float(num_bytes or 0) / (1024 * 1024))


def clamp(value, low, high):
    return max(low, min(high, value))


def parse_time(value):
    """把 90 / 1:30 / 01:02:03 / 90.5 解析成秒（SPEC §2.4 的三种写法）。"""
    if isinstance(value, bool):
        raise ValueError("无法解析时间: %r" % (value,))
    if isinstance(value, (int, float)):
        return float(value)
    text = str(value or "").strip()
    if not text:
        raise ValueError("时间不能为空")
    negative = text.startswith("-")
    parts = (text[1:] if negative else text).split(":")
    if len(parts) > 3:
        raise ValueError("无法解析时间: %r" % (value,))
    total = 0.0
    for part in parts:
        try:
            total = total * 60.0 + float(part.strip())
        except ValueError:
            raise ValueError("无法解析时间: %r" % (value,))
    return -total if negative else total


def parse_time_arg(value, what="时间"):
    """parse_time 的 CLI 版本：解析失败变成中文 FatalError（退出码 1）。"""
    try:
        return parse_time(value)
    except ValueError as exc:
        raise FatalError("%s 格式不对：%s（支持 90 / 1:30 / 01:02:03）" % (what, exc))


def frame_stem(index, seconds):
    """f003_t0053.2 —— SPEC 正文写 {t:08.1f}，但同一节的示例 f003_t0053.2 / f000_t0000.0
    都是 6 字符宽度、两处示例互相一致，这里按示例走 06.1f（报告里已说明）。"""
    return "f%03d_t%06.1f" % (index, max(0.0, float(seconds)))


def is_url(value):
    return bool(re.match(r"^(https?|ftp)://", str(value or "").strip(), re.I))


# ---------------------------------------------------------------------------
# 纯函数（测试直接 import —— 不要往里塞 I/O）
# ---------------------------------------------------------------------------


def estimate_b64_mb(seconds, kbps=64):
    """base64 之后的体积估算（MB）：原始字节 × 4/3。"""
    return float(seconds) * float(kbps) * 1000.0 / 8.0 * 4.0 / 3.0 / (1024.0 * 1024.0)


def pick_chunk_seconds(base_seconds):
    """base64 超过 9.5MB 就把分段时长减半，下限 120 s（SPEC §2.3）。

    用户显式配了比 120 s 更短的分段（chunkMinutes < 2）时按用户的值来 —— 那是他故意的。
    """
    seconds = float(base_seconds or 0)
    if seconds <= 0:
        return MIN_CHUNK_SECONDS
    if seconds <= MIN_CHUNK_SECONDS:
        return seconds
    while seconds > MIN_CHUNK_SECONDS and estimate_b64_mb(seconds) > B64_MB_LIMIT:
        seconds /= 2.0
    return seconds


def plan_chunks(duration_sec, chunk_seconds):
    """切成覆盖 [0, duration] 的连续段；末段短于 5 s 时并进上一段。"""
    duration = max(0.0, float(duration_sec))
    chunk = max(1.0, float(chunk_seconds))
    if duration <= 0:
        return []
    spans = []
    cursor = 0.0
    while cursor < duration - 1e-6:
        end = min(duration, cursor + chunk)
        spans.append([cursor, end])
        cursor = end
    if len(spans) >= 2 and (spans[-1][1] - spans[-1][0]) < MIN_TAIL_SECONDS:
        spans[-2][1] = spans[-1][1]
        spans.pop()
    return [(round(a, 3), round(b, 3)) for a, b in spans]


def uniform_candidates(duration_sec, density, tail_ratio):
    """均匀帧候选：基准间隔 120/density 秒，末尾 tail_ratio 区间间隔减半。"""
    duration = max(0.0, float(duration_sec))
    if duration <= 0:
        return []
    step = 120.0 / max(0.05, float(density or 1.0))
    tail_from = duration * (1.0 - clamp(float(tail_ratio or 0.0), 0.0, 1.0))
    out = []
    cursor = 0.0
    while cursor < tail_from - 1e-6:
        out.append({"t": round(cursor, 1), "score": 0.0, "tail": False})
        cursor += step
    cursor = tail_from
    while cursor <= duration + 1e-6:
        out.append({"t": round(min(cursor, duration), 1), "score": 0.0, "tail": True})
        cursor += step / 2.0
    return out


def elect_frames(candidates, max_frames, duration_sec, tail_ratio):
    """从候选里选出不超过 max_frames 帧，按 t 升序返回。

    优先级：首帧与尾帧 → 场景帧（score 降序）→ 尾部帧 → 中段均匀帧。
    """
    budget = max(1, int(max_frames))
    duration = max(0.0, float(duration_sec))
    tail_from = duration * (1.0 - clamp(float(tail_ratio or 0.0), 0.0, 1.0))
    items = []
    for candidate in candidates or []:
        try:
            seconds = float(candidate.get("t"))
        except (TypeError, ValueError):
            continue
        items.append({"t": max(0.0, seconds), "score": float(candidate.get("score") or 0.0),
                      "tail": bool(candidate.get("tail", seconds >= tail_from))})
    if not items and duration <= 0:
        return []
    items.sort(key=lambda c: c["t"])

    picked = {}

    def add(item):
        key = round(float(item["t"]), 1)
        if key not in picked and len(picked) < budget:
            picked[key] = {"t": key, "score": float(item.get("score") or 0.0), "tail": bool(item.get("tail"))}

    # 1) 首尾必留（候选里没有就补虚拟点，SPEC 要求"永远保留"）
    near_head = [c for c in items if abs(c["t"]) < 0.5]
    near_tail = [c for c in items if abs(c["t"] - duration) < 1.0]
    add(min(near_head, key=lambda c: abs(c["t"])) if near_head else {"t": 0.0, "score": 0.0})
    add(min(near_tail, key=lambda c: abs(c["t"] - duration)) if near_tail
        else {"t": duration, "score": 0.0, "tail": True})
    # 2) 场景帧按分数降序（同分按时间），3) 尾部帧，4) 剩下的中段均匀帧
    for item in sorted([c for c in items if c["score"] > 0], key=lambda c: (-c["score"], c["t"])):
        add(item)
    for item in [c for c in items if c["tail"]]:
        add(item)
    for item in items:
        add(item)
    return sorted(picked.values(), key=lambda c: c["t"])


# ---------------------------------------------------------------------------
# ffmpeg / ffprobe
# ---------------------------------------------------------------------------


def run_tool(args, timeout=600, what="ffmpeg"):
    """跑外部命令（一律 list + utf-8/replace，中文路径才不会炸）。"""
    try:
        proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except FileNotFoundError:
        raise FatalError("找不到可执行文件: %s" % args[0])
    except subprocess.TimeoutExpired:
        raise FatalError("%s 超时（>%ss）: %s" % (what, timeout, args[0]))
    except OSError as exc:
        raise FatalError("%s 启动失败: %s" % (what, exc))
    if proc.returncode != 0:
        tail = (proc.stderr or "").strip().splitlines()[-6:]
        raise FatalError("%s 失败（退出码 %d）：\n%s" % (what, proc.returncode, "\n".join(tail)))
    return proc


def parse_fraction(text):
    """ffprobe 的 '25/1' / '29.97' → float。"""
    raw = str(text or "").strip()
    if "/" in raw:
        num, _, den = raw.partition("/")
        try:
            return float(num) / float(den) if float(den) else 0.0
        except ValueError:
            return 0.0
    try:
        return float(raw)
    except ValueError:
        return 0.0


def ffprobe_json(ffprobe, path, timeout=180):
    """-show_format -show_streams -show_chapters；解析失败退化成只取时长。"""
    args = [ffprobe, "-v", "error", "-print_format", "json",
            "-show_format", "-show_streams", "-show_chapters", path]
    try:
        return json.loads(run_tool(args, timeout=timeout, what="ffprobe").stdout or "{}")
    except (FatalError, ValueError):
        # 老 ffprobe 可能不认 -show_chapters，或者 JSON 被截断
        try:
            proc = run_tool([ffprobe, "-v", "error", "-show_entries", "format=duration",
                             "-of", "default=nw=1:nk=1", path], timeout=timeout, what="ffprobe")
            return {"format": {"duration": (proc.stdout or "").strip()}}
        except FatalError as exc:
            raise FatalError("ffprobe 无法读取这个视频：%s" % exc)


def probe_video(video_path, cfg):
    """把 ffprobe 的一坨东西压成 SPEC §2.1 的 probe.json 形状。"""
    data = ffprobe_json(require_binary(cfg, "ffprobe"), video_path)
    fmt = data.get("format") or {}
    streams = data.get("streams") or []
    try:
        duration = float(fmt.get("duration") or 0.0)
    except (TypeError, ValueError):
        duration = 0.0
    video_stream = next((s for s in streams if s.get("codec_type") == "video"
                         and not (s.get("disposition") or {}).get("attached_pic")), {})
    audio_stream = next((s for s in streams if s.get("codec_type") == "audio"), None)
    bitrate_kbps = 0
    try:
        bitrate_kbps = int(round(int(fmt.get("bit_rate") or 0) / 1000))
    except (TypeError, ValueError):
        pass
    if not bitrate_kbps and duration > 0:
        try:
            size = int(fmt.get("size") or 0)
            bitrate_kbps = int(round(size * 8 / duration / 1000)) if size else 0
        except (TypeError, ValueError):
            bitrate_kbps = 0
    chapters = []
    for chapter in data.get("chapters") or []:
        try:
            t0 = float(chapter.get("start_time") or 0.0)
        except (TypeError, ValueError):
            continue
        chapters.append({"t0": round(t0, 3),
                         "t1": round(float(chapter.get("end_time") or t0), 3),
                         "title": str((chapter.get("tags") or {}).get("title") or "").strip()})
    return {
        "path": video_path,
        "sizeBytes": os.path.getsize(video_path) if os.path.isfile(video_path) else 0,
        "durationSec": round(duration, 3),
        "width": int(video_stream.get("width") or 0),
        "height": int(video_stream.get("height") or 0),
        "fps": round(parse_fraction(video_stream.get("avg_frame_rate")) or parse_fraction(
            video_stream.get("r_frame_rate")), 3),
        "bitrateKbps": bitrate_kbps,
        "videoCodec": str(video_stream.get("codec_name") or ""),
        "audioCodec": str((audio_stream or {}).get("codec_name") or ""),
        "audioChannels": int((audio_stream or {}).get("channels") or 0),
        "audioSampleRate": int((audio_stream or {}).get("sample_rate") or 0),
        "chapters": chapters,
        "hasAudio": audio_stream is not None,
    }


def print_probe(probe, out_dir, emit=note):
    """SPEC §2.1 要求 stdout 必含的六行。"""
    emit("文件: %s" % probe["path"])
    emit("时长: %s (%.1f s)" % (fmt_hms(probe["durationSec"]), probe["durationSec"]))
    emit("画面: %dx%d @ %s fps, %s" % (probe["width"], probe["height"],
                                      fmt_fps(probe["fps"]), probe["videoCodec"] or "未知"))
    if probe["hasAudio"]:
        emit("音频: %s %dch %dHz" % (probe["audioCodec"], probe["audioChannels"], probe["audioSampleRate"]))
    else:
        emit("音频: 无")
    emit("章节: %d" % len(probe["chapters"]))
    emit("工作目录: %s" % out_dir)


def extract_audio_parts(ffmpeg, video_path, spans, audio_dir):
    """按 spans 切 16kHz 单声道 64kbps mp3；返回 [(i, t0, t1, path)]。"""
    os.makedirs(audio_dir, exist_ok=True)
    parts = []
    for index, (t0, t1) in enumerate(spans):
        path = os.path.join(audio_dir, "part_%03d.mp3" % index)
        base = [ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.3f" % t0,
                "-t", "%.3f" % max(0.05, t1 - t0), "-i", video_path,
                "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k"]
        try:
            # 显式点 libmp3lame：不同 ffmpeg 构建的默认 mp3 编码器不一样，显式更可预测
            run_tool(base + ["-c:a", "libmp3lame", path], timeout=900, what="ffmpeg(音频分段)")
        except FatalError:
            run_tool(base + [path], timeout=900, what="ffmpeg(音频分段)")
        if not os.path.isfile(path) or os.path.getsize(path) == 0:
            raise FatalError("音频分段为空: %s" % path)
        parts.append((index, t0, t1, path))
    return parts


def scene_candidates(ffmpeg, video_path, tmp_dir, timeout=3600):
    """场景检测抽帧：select+showinfo 出文件，showinfo 的 pts_time 就是帧时间。

    额外串了 metadata=print 拿真实 scene 分数（elect_frames 要按分数排序）；
    它不改帧，只往 stderr 多打一行 lavfi.scene_score。
    """
    os.makedirs(tmp_dir, exist_ok=True)
    for name in os.listdir(tmp_dir):
        if name.startswith("scene_") and name.endswith(".jpg"):
            try:
                os.remove(os.path.join(tmp_dir, name))
            except OSError:
                pass
    args = [ffmpeg, "-hide_banner", "-loglevel", "info", "-y", "-i", video_path,
            "-vf", "select='gt(scene,%s)',showinfo,metadata=print,scale=960:-2" % SCENE_THRESHOLD,
            "-vsync", "vfr", "-q:v", "3", os.path.join(tmp_dir, "scene_%05d.jpg")]
    try:
        proc = subprocess.run(args, capture_output=True, encoding="utf-8", errors="replace", timeout=timeout)
    except subprocess.TimeoutExpired:
        note("场景检测超时，已跳过场景帧（可降低密度或加大 timeout 重试）")
        return []
    # metadata=print 也会打 pts_time，所以时间戳只从 showinfo 行取，分数单独按顺序取；
    # 两条流都是一帧一行，位置一一对应。
    times, scores = [], []
    for line in (getattr(proc, "stderr", "") or "").splitlines():
        if "Parsed_showinfo" in line:
            found = re.search(r"pts_time:([0-9]+(?:\.[0-9]+)?)", line)
            if found:
                times.append(float(found.group(1)))
            continue
        found = re.search(r"lavfi\.scene_score=([0-9.]+)", line)
        if found:
            scores.append(float(found.group(1)))
    files = sorted(os.path.join(tmp_dir, n) for n in os.listdir(tmp_dir)
                   if n.startswith("scene_") and n.endswith(".jpg"))
    return [{"t": round(times[i] if i < len(times) else float(i), 3),
             "score": scores[i] if i < len(scores) else 1.0, "path": path}
            for i, path in enumerate(files)]


def extract_one_frame(ffmpeg, video_path, seconds, dest, timeout=180):
    """单帧定点抽取（-ss 放 -i 前面，快）。"""
    run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.3f" % seconds,
              "-i", video_path, "-frames:v", "1", "-vf", "scale=960:-2", "-q:v", "3", dest],
             timeout=timeout, what="ffmpeg(抽帧)")
    return os.path.isfile(dest) and os.path.getsize(dest) > 0


# ---------------------------------------------------------------------------
# HTTP：OpenAI 兼容端点
# ---------------------------------------------------------------------------


def chat_endpoint(base):
    """base 规范化：去尾部斜杠，不以 /chat/completions 结尾就补上（SPEC §1）。"""
    text = str(base or "").strip().rstrip("/")
    if not text:
        return ""
    return text if re.search(r"/chat/completions$", text, re.I) else text + "/chat/completions"


def auth_headers(section):
    key = str(section.get("key") or "")
    headers = {"Content-Type": "application/json", "Accept": "application/json"}
    if str(section.get("auth") or "bearer").strip().lower() == "api-key":
        headers["api-key"] = key
    else:
        headers["Authorization"] = "Bearer " + key
    return headers


def chat_post(section, payload, timeout=120, retries=2, purpose="llm"):
    """POST 到 OpenAI 兼容端点，返回 (ok, data, error)。绝不把异常抛给调用方。

    只对 5xx / 超时 / 网络错误重试（1s、3s 退避）；4xx 是调用方自己写错了，重试没意义。
    """
    base = str((section or {}).get("base") or "").strip()
    if not base:
        return False, None, "接口地址为空"
    url = chat_endpoint(base)
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    headers = auth_headers(section or {})
    delays = [1.0, 3.0]
    last_error = ""
    for attempt in range(int(retries) + 1):
        request = urllib.request.Request(url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                raw = response.read().decode("utf-8", "replace")
            try:
                return True, json.loads(raw), ""
            except ValueError:
                return False, None, "响应不是 JSON：%s" % raw[:200]
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read().decode("utf-8", "replace")
            except Exception:
                detail = str(exc)
            message = "HTTP %d — %s" % (exc.code, oneline(detail, 300))
            if 400 <= exc.code < 500:
                return False, None, message
            last_error = message
        except Exception as exc:  # URLError / socket.timeout / 连接被重置
            last_error = "%s: %s" % (type(exc).__name__,
                                     oneline(str(getattr(exc, "reason", None) or exc), 240))
        if attempt < retries:
            time.sleep(delays[min(attempt, len(delays) - 1)])
    return False, None, last_error or ("%s 请求失败" % purpose)


def message_text(data):
    """统一取 choices[0].message.content（可能是字符串，也可能是分片数组）。"""
    try:
        content = data["choices"][0]["message"]["content"]
    except (KeyError, IndexError, TypeError):
        return ""
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return "".join(str(p.get("text") or "") if isinstance(p, dict) else str(p) for p in content).strip()
    return ""


def extract_segments(data):
    """从响应里找 segments（[{start,end,text}]，单位秒，相对本段）。"""
    if not isinstance(data, dict):
        return None
    pools = [data]
    choices = data.get("choices") or []
    if choices and isinstance(choices[0], dict):
        pools.append(choices[0])
        if isinstance(choices[0].get("message"), dict):
            pools.append(choices[0]["message"])
    for pool in pools:
        segments = pool.get("segments")
        if not isinstance(segments, list):
            continue
        cleaned = []
        for segment in segments:
            text = str((segment or {}).get("text") or "").strip() if isinstance(segment, dict) else ""
            if not text:
                continue
            try:
                start = float(segment.get("start", segment.get("t0", 0.0)) or 0.0)
                end = float(segment.get("end", segment.get("t1", start)) or start)
            except (TypeError, ValueError):
                continue
            cleaned.append({"start": start, "end": max(start, end), "text": text})
        if cleaned:
            return cleaned
    return None


def asr_transcribe(section, audio_path, timeout=180):
    """调 ASR：先 MiMo 的 data URL 形状；4xx 且提示格式问题时退到 OpenAI 裸 b64 形状。"""
    try:
        with open(audio_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError as exc:
        return None, "音频读取失败: %s" % exc
    model = str(section.get("model") or "")
    primary = {"model": model, "messages": [{"role": "user", "content": [
        {"type": "input_audio", "input_audio": {"data": "data:audio/mpeg;base64," + encoded}}]}]}
    ok, data, error = chat_post(section, primary, timeout=timeout, purpose="asr")
    if ok:
        return data, None
    # 服务端只认 OpenAI gpt-4o-audio 的 {data, format} 时，MiMo 形状会 4xx 并抱怨 input_audio/format
    if error.startswith("HTTP 4") and ("input_audio" in error or "format" in error):
        arrow("ASR 形状不被接受（%s），改用裸 base64 + format 重试" % oneline(error, 120))
        fallback = {"model": model, "messages": [{"role": "user", "content": [
            {"type": "input_audio", "input_audio": {"data": encoded, "format": "mp3"}},
            {"type": "text", "text": "请转写这段音频，只输出文字。"}]}]}
        ok2, data2, error2 = chat_post(section, fallback, timeout=timeout, purpose="asr")
        if ok2:
            return data2, None
        return None, "%s / 兼容形状也失败: %s" % (error, error2)
    return None, error


def vlm_describe(section, jpg_path, timeout=120):
    """调 VLM 描述一帧，返回 (text, error)。"""
    try:
        with open(jpg_path, "rb") as handle:
            encoded = base64.b64encode(handle.read()).decode("ascii")
    except OSError as exc:
        return None, "帧读取失败: %s" % exc
    payload = {"model": str(section.get("model") or ""), "max_tokens": 512, "messages": [{
        "role": "user", "content": [
            {"type": "image_url", "image_url": {"url": "data:image/jpeg;base64," + encoded}},
            {"type": "text", "text": VLM_PROMPT}]}]}
    ok, data, error = chat_post(section, payload, timeout=timeout, purpose="vision")
    if not ok:
        return None, error
    text = message_text(data).strip()
    return (text, None) if text else (None, "响应为空")


def llm_chat(section, system, user, timeout=240):
    """一次文本补全，返回 (text, error)。"""
    payload = {"model": str(section.get("model") or ""),
               "max_tokens": int(section.get("maxTokens") or 2048),
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    ok, data, error = chat_post(section, payload, timeout=timeout, purpose="llm")
    if not ok:
        return None, error
    text = message_text(data).strip()
    return (text, None) if text else (None, "响应为空")


# ---------------------------------------------------------------------------
# 下载（B站 / yt-dlp / 直链）—— 零网络测试不覆盖，靠契约保证形状
# ---------------------------------------------------------------------------


def http_get(url, headers=None, timeout=60):
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return response.read()


def http_download(url, dest, headers=None, timeout=1800):
    """流式下载到 dest（先 .part 再改名，断掉不会留下半截可用文件）。"""
    part = dest + ".part"
    request = urllib.request.Request(url, headers=headers or {}, method="GET")
    with urllib.request.urlopen(request, timeout=timeout) as response, open(part, "wb") as handle:
        shutil.copyfileobj(response, handle, length=1024 * 512)
    os.replace(part, dest)
    return dest


def bilibili_bvid(url):
    """从 URL / b23.tv 短链里拿 BV 号（短链要跟一次重定向）。"""
    match = re.search(r"(BV[0-9A-Za-z]{10})", url)
    if match:
        return match.group(1)
    if "b23.tv" in url:
        request = urllib.request.Request(url, headers=BILI_HEADERS, method="GET")
        with urllib.request.urlopen(request, timeout=30) as response:
            match = re.search(r"(BV[0-9A-Za-z]{10})", response.geturl() or "")
            if match:
                return match.group(1)
    return None


def bilibili_pages(url):
    """解析 B站视频的分 P 清单，返回 (bvid, [page dict])。

    page dict: {p, cid, duration, title, path?}。单 P 视频也返回长度 1 的列表，
    调用方不需要区分两种形状。分 P 视频如果不看 pages，直接用 view 接口的 data.cid，
    拿到的是 **P1 的 cid** —— 那就是"61 个分 P 只下到第一个"的成因。
    """
    bvid = bilibili_bvid(url)
    if not bvid:
        raise FatalError("没能从链接里认出 B站 BV 号：%s" % url)
    view = json.loads(http_get("https://api.bilibili.com/x/web-interface/view?bvid=%s" % bvid,
                               BILI_HEADERS).decode("utf-8", "replace"))
    data = view.get("data") or {}
    raw = data.get("pages") or []
    if not raw:  # 极少数视频没有 pages 字段，按单 P 处理
        cid = data.get("cid")
        if not cid:
            raise FatalError("B站接口没有返回 cid（可能是付费/受限视频）：%s" % bvid)
        return bvid, [{"p": 1, "cid": cid, "duration": data.get("duration") or 0,
                       "title": data.get("title") or ""}]
    pages = [{"p": int(item.get("page") or position + 1),
              "cid": item.get("cid"),
              "duration": item.get("duration") or 0,
              "title": item.get("part") or ""}
             for position, item in enumerate(raw)]
    pages.sort(key=lambda item: item["p"])
    return bvid, pages


def bilibili_page_from_url(url):
    """URL 里的 ?p=N（没有就 1）。"""
    query = urllib.parse.parse_qs(urllib.parse.urlparse(url).query or "")
    try:
        return max(1, int((query.get("p") or ["1"])[0]))
    except (TypeError, ValueError):
        return 1


def download_bilibili_page(bvid, page, out_dir, cfg, label):
    """下载一个分 P（dash 音视频流 → ffmpeg -c copy 合流）。已存在则跳过。"""
    target = os.path.join(out_dir, "%s.mp4" % label)
    if os.path.isfile(target) and os.path.getsize(target) > 0:
        note("已存在，跳过: %s" % os.path.basename(target))
        return target
    cid = page["cid"]
    play = json.loads(http_get(
        "https://api.bilibili.com/x/player/playurl?bvid=%s&cid=%s&fnval=16&qn=80&fourk=1" % (bvid, cid),
        BILI_HEADERS).decode("utf-8", "replace"))
    dash = (play.get("data") or {}).get("dash") or {}
    videos = sorted(dash.get("video") or [], key=lambda s: (s.get("height") or 0, s.get("bandwidth") or 0))
    audios = sorted(dash.get("audio") or [], key=lambda s: s.get("bandwidth") or 0)
    if not videos or not audios:
        raise FatalError("B站 dash 流里没有音视频轨道：%s cid=%s" % (bvid, cid))
    os.makedirs(out_dir, exist_ok=True)
    video_part = os.path.join(out_dir, "%s_video.m4s" % label)
    audio_part = os.path.join(out_dir, "%s_audio.m4s" % label)
    note("下载视频流 %s…" % oneline(videos[-1].get("baseUrl") or videos[-1].get("base_url"), 80))
    http_download(videos[-1].get("baseUrl") or videos[-1].get("base_url"), video_part, BILI_HEADERS)
    noted_audio = False
    for audio in reversed(audios):  # 从高码率往低试，失败就换一档
        try:
            if not noted_audio:
                note("下载音频流…")
                noted_audio = True
            http_download(audio.get("baseUrl") or audio.get("base_url"), audio_part, BILI_HEADERS)
            break
        except Exception as exc:
            note("音频流失败，换下一档：%s" % oneline(str(exc), 120))
    else:
        raise FatalError("所有音频流都下载失败：%s cid=%s" % (bvid, cid))
    run_tool([require_binary(cfg, "ffmpeg"), "-hide_banner", "-loglevel", "error", "-y",
              "-i", video_part, "-i", audio_part, "-c", "copy", target],
             timeout=1800, what="ffmpeg(合流)")
    for part in (video_part, audio_part):
        try:
            os.remove(part)
        except OSError:
            pass
    return target


def download_bilibili(url, out_dir, cfg):
    """下载单个分 P（cache_download / 老调用点用）。"""
    bvid, pages = bilibili_pages(url)
    want = bilibili_page_from_url(url)
    page = next((item for item in pages if item["p"] == want), pages[0])
    label = bvid if len(pages) == 1 else "%s_p%02d" % (bvid, page["p"])
    return download_bilibili_page(bvid, page, out_dir, cfg, label)


def find_ytdlp(cfg):
    configured = str(cfg.get("ytdlp") or "").strip()
    if configured and os.path.isfile(configured):
        return configured
    for name in ("yt-dlp", "yt-dlp.exe"):
        found = shutil.which(name)
        if found:
            return found
    return None


def download_ytdlp(url, out_dir, cfg):
    ytdlp = find_ytdlp(cfg)
    if not ytdlp:
        raise MissingDependency(
            "没有可用的下载器：这个站点需要 yt-dlp。请先安装（pip install -U yt-dlp），"
            "或把 yt-dlp.exe 的路径填进 设置 → 视频理解 → 全局。")
    os.makedirs(out_dir, exist_ok=True)
    before = set(os.listdir(out_dir))
    run_tool([ytdlp, "--no-playlist", "-o", os.path.join(out_dir, "%(title).80s.%(ext)s"), url],
             timeout=1800, what="yt-dlp")
    candidates = [os.path.join(out_dir, name) for name in os.listdir(out_dir) if name not in before
                  and os.path.splitext(name)[1].lower() in (".mp4", ".mkv", ".webm", ".flv", ".m4a", ".mov")]
    if not candidates:
        raise FatalError("yt-dlp 跑完但没找到新下载的媒体文件：%s" % out_dir)
    return max(candidates, key=os.path.getsize)


def cache_download(url, cfg, workdir):
    """URL → 本地文件：B站 / yt-dlp / 直链，落到 <workdir>/cache/<sha1[:12]>/。"""
    cache_dir = os.path.join(workdir, "cache", hashlib.sha1(url.encode("utf-8")).hexdigest()[:12])
    os.makedirs(cache_dir, exist_ok=True)
    media = (".mp4", ".mkv", ".webm", ".flv", ".mov", ".m4a", ".mp3")
    existing = [os.path.join(cache_dir, name) for name in os.listdir(cache_dir)
                if os.path.splitext(name)[1].lower() in media and not name.endswith(".part")]
    if existing:
        note("命中下载缓存: %s" % max(existing, key=os.path.getsize))
        return max(existing, key=os.path.getsize)
    if "bilibili.com" in url or "b23.tv" in url:
        return download_bilibili(url, cache_dir, cfg)
    if find_ytdlp(cfg):
        return download_ytdlp(url, cache_dir, cfg)
    name = os.path.basename(urllib.parse.urlparse(url).path) or "download.mp4"
    if os.path.splitext(name)[1].lower() in media:  # 直链兜底
        note("直链下载: %s" % oneline(url, 100))
        return http_download(url, os.path.join(cache_dir, name), {"User-Agent": UA})
    raise MissingDependency("这个链接没有匹配的下载器。B站链接可直接用；"
                            "其他站点请安装 yt-dlp（pip install -U yt-dlp）。")


# ---------------------------------------------------------------------------
# index 各阶段
# ---------------------------------------------------------------------------


def cut_audio(ffmpeg, src, start, duration, dest):
    """从一段音频里切出 [start, start+duration)。重编码而不是 -c copy，
    因为 mp3 的 copy 切点会偏移，而这里切点要跟时间戳严格对齐。"""
    run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
              "-ss", "%.3f" % max(0.0, start), "-t", "%.3f" % max(0.1, duration),
              "-i", src, "-vn", "-ac", "1", "-ar", "16000", "-b:a", "64k", dest],
             timeout=600, what="ffmpeg(切音频)")
    return dest


def asr_suspect(text, duration):
    """判断一次 ASR 结果跟音频时长是否"不匹配"，返回 'short' / 'long' / None。

    两个方向都会坏：短了是模型跳读（中间整段消失），长了是复读/扩写（凭空多出内容）。
    只有字数这个信号能发现，服务端两种情况下都返回 200 且 finish_reason=stop。
    """
    if not text or duration < ASR_SPLIT_FLOOR * 2:
        return None
    per_second = len(text) / duration
    if per_second < MIN_CHARS_PER_SEC:
        return "short"
    if per_second > MAX_CHARS_PER_SEC:
        return "long"
    return None


def transcribe_repaired(section, ffmpeg, audio_path, duration, scratch_dir,
                        depth=0, rel_offset=0.0):
    """转写一段音频；若结果与时长严重不匹配，对半切开重转并把结果拼回来。

    这是为了对抗"服务端悄悄跳读"这种无错误码的失败模式（见 MIN_CHARS_PER_SEC 注释）。
    切开重转是幂等的：两半各自都通过同一个检测，所以不会退化成更差的覆盖。
    @returns (text, segments, error, splits) —— segments 的时间相对**原始段**起点。
    """
    data, error = asr_transcribe(section, audio_path)
    text = message_text(data).strip() if data else ""
    segments = extract_segments(data) if data else None
    if segments:
        segments = [dict(seg, start=float(seg.get("start", 0.0)) + rel_offset,
                         end=float(seg.get("end", 0.0)) + rel_offset) for seg in segments]

    reason = asr_suspect(text, duration)
    if error or not reason or depth >= MAX_ASR_DEPTH:
        return text, segments, error, 0

    half = duration / 2.0
    if half < ASR_SPLIT_FLOOR:
        return text, segments, error, 0
    note("ASR 结果可疑（%.0f s 有 %d 字 = %.1f 字/秒，%s），对半重转"
         % (duration, len(text), len(text) / duration,
            "偏少，判定被跳读" if reason == "short" else "偏多，判定复读/扩写"))
    texts, merged, splits = [], [], 1
    for k in range(2):
        start = k * half
        span = half if k == 0 else max(0.1, duration - half)
        piece = os.path.join(scratch_dir, "split_d%d_%d_%03d.mp3" % (depth, k, len(texts)))
        try:
            cut_audio(ffmpeg, audio_path, start, span, piece)
        except FatalError as exc:
            note("切音频失败，保留原结果：%s" % oneline(str(exc), 160))
            return text, segments, error, splits
        sub_text, sub_segments, sub_error, sub_splits = transcribe_repaired(
            section, ffmpeg, piece, span, scratch_dir, depth + 1, rel_offset + start)
        splits += sub_splits
        if sub_text:
            texts.append(sub_text)
        if sub_segments:
            merged.extend(sub_segments)
        if sub_error and not sub_text:
            return text, segments, error, splits  # 重试反而更差 → 保留原结果
        try:
            os.remove(piece)
        except OSError:
            pass
    if not texts:
        return text, segments, error, splits
    return "".join(texts), (merged or None), None, splits


def stage_asr(section, parts, out_dir, resume, ffmpeg, workers=2):
    """并发调 ASR；单段失败只记 error。返回 asr/part_*.json 的记录列表。"""
    asr_dir = os.path.join(out_dir, "asr")
    os.makedirs(asr_dir, exist_ok=True)
    scratch = os.path.join(out_dir, ".asr-split")
    os.makedirs(scratch, exist_ok=True)
    model = str(section.get("model") or "")

    def work(part):
        index, t0, t1, audio_path = part
        meta_path = os.path.join(asr_dir, "part_%03d.json" % index)
        if resume and os.path.isfile(meta_path):
            cached = load_json(meta_path)
            if isinstance(cached, dict):
                arrow("ASR 段 %03d 复用已有结果" % index)
                return cached
        text, segments, error, splits = transcribe_repaired(
            section, ffmpeg, audio_path, max(0.1, t1 - t0), scratch)
        record = {"index": index, "t0": round(t0, 3), "t1": round(t1, 3), "text": text, "model": model}
        if segments:
            record["segments"] = segments
        if splits:
            record["splits"] = splits
        if error or not text:
            record["error"] = error or "转写结果为空"
        write_json(meta_path, record)
        if record.get("error"):
            note("ASR 段 %03d 失败: %s" % (index, oneline(record["error"], 160)))
        else:
            arrow("ASR 段 %03d 完成（%d 字%s）"
                  % (index, len(text), "，对半重转 %d 次" % splits if splits else ""))
        return record

    try:
        with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
            return list(pool.map(work, parts))
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


def stage_describe(section, frames, frames_dir, resume, workers=4, src_size=0, src_mtime=0.0):
    """并发描述帧；单帧失败只记 error。帧 json 与 jpg 同名。

    section=None 表示 --vision none / 端点未配置：只写 {t,path,skipped}，不调模型。
    """
    model = str(section.get("model") or "") if section else ""

    def work(frame):
        stem = os.path.splitext(os.path.basename(frame["path"]))[0]
        meta_path = os.path.join(frames_dir, stem + ".json")
        base = {"t": round(float(frame["t"]), 3), "path": frame["path"],
                "srcSize": int(src_size), "srcMtime": round(float(src_mtime), 3)}
        if resume and os.path.isfile(meta_path):
            cached = load_json(meta_path)
            if isinstance(cached, dict) and not cached.get("skipped") and (
                    int(cached.get("srcSize") or 0) == int(src_size)
                    and abs(float(cached.get("srcMtime") or 0) - float(src_mtime)) < 1e-3):
                arrow("帧 %s 复用已有描述" % stem)
                return cached
        if not section:
            record = dict(base, skipped=True)
            write_json(meta_path, record)
            return record
        text, error = vlm_describe(section, frame["path"])
        record = dict(base, text=text or "", scene=float(frame.get("score") or 0.0), model=model)
        if error:
            record["error"] = error
            note("帧 %s 描述失败: %s" % (stem, oneline(error, 160)))
        else:
            arrow("帧 %s 描述完成" % stem)
        write_json(meta_path, record)
        return record

    if not frames:
        return []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        return list(pool.map(work, frames))


def parse_vlm_text(text):
    """从固定两行 prompt 的回答里取「画面：」「文字：」。"""
    scene, ocr = "", ""
    for line in str(text or "").splitlines():
        item = line.strip()
        if item.startswith("画面：") or item.startswith("画面:"):
            scene = item[3:].strip()
        elif item.startswith("文字：") or item.startswith("文字:"):
            ocr = item[3:].strip()
    if not scene and not ocr:
        scene = str(text or "").strip()[:300]
    return scene, ocr


def is_no_text(value):
    """「无文字」的常见变体都当没有文字。"""
    cleaned = re.sub(r"[\s。．.,，:：;；!！?？]", "", str(value or ""))
    return cleaned in ("", "无文字", "無文字", "没有文字", "无", "無", "无任何文字",
                       "没有可见文字", "无字幕", "none")


def split_sentences(text):
    return [piece.strip() for piece in re.findall(r"[^。！？!?；;\n]+[。！？!?；;]?", str(text or ""))
            if piece.strip()]


def estimate_speech_events(record, duration):
    """ASR 一段 → speech 事件。有 segments 用之，否则按字符比例在段内分配时间（标 estimated）。"""
    t0 = float(record.get("t0") or 0.0)
    span = max(0.0, float(record.get("t1") or t0) - t0)
    events = []
    if record.get("segments"):
        for segment in record["segments"]:
            start = clamp(t0 + float(segment["start"]), 0.0, duration)
            end = clamp(t0 + float(segment["end"]), start, duration)
            events.append({"t0": round(start, 3), "t1": round(end, 3), "kind": "speech",
                           "text": str(segment["text"]), "source": "asr", "estimated": False})
        return events
    sentences = split_sentences(record.get("text"))
    total_chars = float(sum(len(s) for s in sentences) or 1)
    cursor = 0.0
    for sentence in sentences:
        share = span * (len(sentence) / total_chars)
        start = clamp(t0 + cursor, 0.0, duration)
        end = clamp(t0 + cursor + share, start, duration)
        cursor += share
        events.append({"t0": round(start, 3), "t1": round(end, 3), "kind": "speech",
                       "text": sentence, "source": "asr", "estimated": True})
    return events


def build_timeline(video_path, duration, asr_records, frame_records):
    """ASR 段 + 帧描述 → SPEC §2.3 的 timeline 事件流（按 t0 升序）。"""
    events = []
    for record in asr_records or []:
        if not record.get("skipped") and record.get("text"):
            events.extend(estimate_speech_events(record, duration))
    for record in frame_records or []:
        if record.get("skipped") or not record.get("text"):
            continue
        seconds = clamp(float(record.get("t") or 0.0), 0.0, duration)
        rel = "frames/" + os.path.basename(record["path"])
        scene_text, ocr_text = parse_vlm_text(record.get("text"))
        if scene_text:
            events.append({"t0": round(seconds, 3), "t1": round(seconds, 3), "kind": "scene",
                           "text": scene_text, "source": "vlm", "frame": rel})
        if ocr_text and not is_no_text(ocr_text):
            events.append({"t0": round(seconds, 3), "t1": round(seconds, 3), "kind": "ocr",
                           "text": ocr_text, "source": "vlm", "frame": rel})
    events.sort(key=lambda e: (e["t0"], {"speech": 0, "scene": 1, "ocr": 2}.get(e["kind"], 3)))
    stats = {"speech": 0, "scene": 0, "ocr": 0}
    for event in events:
        stats[event["kind"]] = stats.get(event["kind"], 0) + 1
    return {"version": 1, "video": video_path, "durationSec": round(float(duration), 3),
            "events": events, "stats": stats}


def chapter_title(events, fallback, limit=24):
    """该章第一条 speech 的前若干字；没有就用 scene；再没有就占位。

    标题优先在标点处收尾 —— 硬切 24 字会切出"人强烈的表达欲与倾"这种半截子标题。
    """
    for kind in ("speech", "scene"):
        for event in events:
            if event["kind"] == kind and event.get("text"):
                text = re.sub(r"^[\s，。、；：！？,.!?;:\"'“”‘’（）()\[\]【】-]+", "", event["text"]).strip()
                if not text:
                    continue
                if len(text) <= limit:
                    return text
                head = text[:limit]
                # 12 字以后找最后一个句读，避免把标题切在词中间
                cut = max(head.rfind(p) for p in "。！？；，、")
                if cut >= 12:
                    return head[:cut]
                return head
    return fallback


def build_chapters(events, duration):
    """静音间隔 ≥20s 断开、单章最长 300s 强制切断（SPEC §2.3）。"""
    duration = max(0.0, float(duration))
    base = [e for e in events if e["kind"] == "speech"] or \
           [e for e in events if e["kind"] in ("scene", "ocr")]
    if not base:
        return [{"index": 1, "t0": 0.0, "t1": round(duration, 3),
                 "title": "画面片段 1", "summary": ""}] if duration > 0 else []
    base.sort(key=lambda e: e["t0"])
    groups = [[base[0]]]
    for previous, current in zip(base, base[1:]):
        gap = float(current["t0"]) - float(previous["t1"])
        span = float(current["t1"]) - float(groups[-1][0]["t0"])
        if gap >= 20.0 or span > 300.0:
            groups.append([current])
        else:
            groups[-1].append(current)
    chapters = []
    for position, group in enumerate(groups):
        t0 = clamp(float(group[0]["t0"]), 0.0, duration)
        t1 = clamp(max(float(item["t1"]) for item in group), t0, duration)
        chapters.append({
            "index": position + 1, "t0": round(t0, 3), "t1": round(t1, 3),
            "title": chapter_title(group, "画面片段 %d" % (position + 1)),
            "summary": oneline(" ".join(item["text"] for item in group if item.get("text")), 160),
        })
    return chapters


# ---------------------------------------------------------------------------
# 子命令：probe / download
# ---------------------------------------------------------------------------


def resolve_out_dir(args, local_video, workdir):
    """--out 优先；否则 <workdir>/<视频文件名 stem>（SPEC §2.1 / §3）。"""
    if getattr(args, "out", None):
        return os.path.abspath(os.path.expanduser(str(args.out)))
    stem = os.path.splitext(os.path.basename(local_video))[0] or "video"
    return os.path.join(workdir, re.sub(r'[<>:"/\\|?*]', "_", stem))


def materialize_video(video_arg, cfg, workdir):
    """本地路径直接返回绝对路径；URL 先下载到 <workdir>/cache 再返回本地路径。"""
    raw = str(video_arg or "").strip()
    if not raw:
        raise FatalError("视频路径为空")
    if is_url(raw):
        arrow("检测到链接，先下载…")
        return cache_download(raw, cfg, workdir)
    path = os.path.abspath(os.path.expanduser(raw))
    if not os.path.isfile(path):
        raise FatalError("找不到视频文件: %s" % path)
    return path


def cmd_probe(args, cfg, workdir):
    local = materialize_video(args.video, cfg, workdir)
    out_dir = resolve_out_dir(args, local, workdir)
    os.makedirs(out_dir, exist_ok=True)
    probe = probe_video(local, cfg)
    if probe["durationSec"] <= 0:
        raise FatalError("这个视频的时长是 0，可能已损坏或不是媒体文件: %s" % local)
    write_json(os.path.join(out_dir, "probe.json"), probe)
    print_probe(probe, out_dir, emit=say)
    return 0


def cmd_download(args, cfg, workdir):
    url = str(args.url or "").strip()
    if not is_url(url):
        raise FatalError("这不是一个 http(s) 链接: %s" % url)
    out_dir = os.path.abspath(os.path.expanduser(str(args.out or os.path.join(workdir, "downloads"))))
    if not ("bilibili.com" in url or "b23.tv" in url):
        target = download_ytdlp(url, out_dir, cfg)
        say("已下载: %s (%s)" % (target, fmt_mb(os.path.getsize(target))))
        return 0

    bvid, pages = bilibili_pages(url)
    multi = len(pages) > 1
    if getattr(args, "all_pages", False):
        wanted = pages
    else:
        number = getattr(args, "page", None) or bilibili_page_from_url(url)
        wanted = [item for item in pages if item["p"] == number] or pages[:1]
    os.makedirs(out_dir, exist_ok=True)
    note("BV%s 共 %d 个分 P%s" % (bvid[2:], len(pages), "，本次下载全部" if len(wanted) > 1 else "，本次下载 P%d" % wanted[0]["p"]))

    manifest_path = os.path.join(out_dir, "manifest.json")
    manifest = load_json(manifest_path) if os.path.isfile(manifest_path) else {}
    if not isinstance(manifest, dict):
        manifest = {}
    manifest.update({"bvid": bvid, "total": len(pages),
                     "pages": manifest.get("pages") or []})
    known = {entry.get("p"): entry for entry in manifest["pages"] if isinstance(entry, dict)}

    saved, failed = [], []
    for position, page in enumerate(wanted, 1):
        label = "%s_p%02d" % (bvid, page["p"]) if multi else bvid
        arrow("P %d/%d %s  %s（%s）"
              % (position, len(wanted), label, oneline(page["title"], 40), fmt_mmss(page["duration"])))
        try:
            target = download_bilibili_page(bvid, page, out_dir, cfg, label)
            saved.append(target)
            known[page["p"]] = {"p": page["p"], "cid": page["cid"], "duration": page["duration"],
                                "title": page["title"], "path": os.path.basename(target),
                                "sizeBytes": os.path.getsize(target)}
            manifest["pages"] = [known[key] for key in sorted(known)]
            write_json(manifest_path, manifest)  # 每下完一个就落盘，中断不丢进度
        except (FatalError, MissingDependency, OSError) as exc:
            failed.append(page["p"])
            note("P%d 失败: %s" % (page["p"], oneline(str(exc), 200)))

    if len(saved) == 1:
        say("已下载: %s (%s)" % (saved[0], fmt_mb(os.path.getsize(saved[0]))))
    else:
        total_mb = sum(os.path.getsize(path) for path in saved) / 1e6
        say("已下载 %d 个分 P，共 %.1f MB → %s" % (len(saved), total_mb, out_dir))
        say("清单: %s" % manifest_path)
        if failed:
            say("失败 %d 个分 P: %s（重跑同一条命令会跳过已完成的）"
                % (len(failed), ", ".join("P%d" % number for number in failed)))
            return 1
    return 0


# ---------------------------------------------------------------------------
# 子命令：index
# ---------------------------------------------------------------------------


def cmd_index(args, cfg, workdir):
    started_at = now_iso()
    ffmpeg = require_binary(cfg, "ffmpeg")
    local = materialize_video(args.video, cfg, workdir)
    out_dir = resolve_out_dir(args, local, workdir)
    os.makedirs(out_dir, exist_ok=True)
    arrow("索引开始: %s" % local)

    probe = probe_video(local, cfg)
    if probe["durationSec"] <= 0:
        raise FatalError("视频时长为 0，无法建索引: %s" % local)
    write_json(os.path.join(out_dir, "probe.json"), probe)
    duration = float(probe["durationSec"])
    print_probe(probe, out_dir, emit=note)
    src_size = probe["sizeBytes"]
    src_mtime = os.path.getmtime(local) if os.path.isfile(local) else 0.0

    # ---- 1. 音频分段（失败只跳过 ASR，不影响抽帧） ----
    asr_section = cfg["asr"]
    asr_on = args.asr != "none"
    audio_parts, chunk_seconds = [], 0.0
    if asr_on and not endpoint_ready(asr_section):
        note("语音识别未配置，已跳过（设置 → 视频理解 → 语音识别）")
        asr_on = False
    if asr_on and not probe["hasAudio"]:
        note("视频没有音轨，已跳过语音识别")
        asr_on = False
    if asr_on:
        chunk_seconds = min(pick_chunk_seconds(float(asr_section.get("chunkMinutes") or 5) * 60.0),
                            max(5.0, duration))
        spans = plan_chunks(duration, chunk_seconds)
        note("音频分段: %d 段（每段 %.0f s）" % (len(spans), chunk_seconds))
        try:
            audio_parts = extract_audio_parts(ffmpeg, local, spans, os.path.join(out_dir, "audio"))
            for _, t0, t1, path in audio_parts:
                arrow("音频 %s (%.0fs-%.0fs, %s)"
                      % (os.path.basename(path), t0, t1, fmt_mb(os.path.getsize(path))))
        except FatalError as exc:
            note("音频提取失败，已跳过语音识别：%s" % oneline(str(exc), 240))
            asr_on, audio_parts = False, []

    # ---- 2. ASR ----
    asr_records = []
    if asr_on and audio_parts:
        arrow("语音转写中（并发 2）…")
        asr_records = stage_asr(asr_section, audio_parts, out_dir, args.resume, ffmpeg, workers=2)

    # ---- 3. 抽帧 ----
    arrow("场景检测抽帧…")
    scene_frames = scene_candidates(ffmpeg, local, os.path.join(out_dir, ".scene"))
    tail_ratio = clamp(args.tail_ratio, 0.0, 1.0)
    note("场景帧 %d 个" % len(scene_frames))
    candidates = [{"t": s["t"], "score": s["score"], "tail": s["t"] >= duration * (1.0 - tail_ratio)}
                  for s in scene_frames]
    candidates.extend(uniform_candidates(duration, args.density, args.tail_ratio))
    chosen = elect_frames(candidates, args.max_frames, duration, args.tail_ratio)
    note("入选帧 %d 个（上限 %d）" % (len(chosen), int(args.max_frames)))
    scene_by_key = {round(float(item["t"]), 1): item["path"] for item in scene_frames}
    frames_dir = os.path.join(out_dir, "frames")
    os.makedirs(frames_dir, exist_ok=True)
    kept_frames = []
    for position, candidate in enumerate(chosen):
        dest = os.path.join(frames_dir, frame_stem(position, candidate["t"]) + ".jpg")
        if args.resume and os.path.isfile(dest) and os.path.getsize(dest) > 0:
            kept_frames.append({"t": candidate["t"], "path": dest, "score": candidate["score"]})
            continue
        source = scene_by_key.get(round(float(candidate["t"]), 1))
        try:
            if source and os.path.isfile(source):
                shutil.copyfile(source, dest)  # 场景帧已经编码过，省一次解码
            else:
                # 尾帧的时间点常落在最后一帧之后，-ss 过去会什么都不产出。
                # 只退 0.1 s 在真实素材上不够（实测 212.3 s 的视频退到 212.2 仍然空），
                # 所以按 0 / .1 / .3 / .6 / 1.2 / 2.5 逐级回退，抽到就停。
                target = float(candidate["t"])
                for back in (0.0, 0.1, 0.3, 0.6, 1.2, 2.5):
                    if extract_one_frame(ffmpeg, local, max(0.0, target - back), dest):
                        break
        except FatalError as exc:
            note("抽帧失败 t=%.1fs: %s" % (candidate["t"], oneline(str(exc), 160)))
            continue
        if not os.path.isfile(dest) or os.path.getsize(dest) == 0:
            note("抽帧无输出，已跳过 t=%.1fs" % candidate["t"])
            continue
        kept_frames.append({"t": candidate["t"], "path": dest, "score": candidate["score"]})
    shutil.rmtree(os.path.join(out_dir, ".scene"), ignore_errors=True)

    # ---- 4. VLM 描述 ----
    vision_section = cfg["vision"]
    vision_on = args.vision != "none"
    if vision_on and not endpoint_ready(vision_section):
        note("画面理解未配置，已跳过（设置 → 视频理解 → 画面理解）")
        vision_on = False
    if vision_on:
        arrow("画面描述中（并发 4，共 %d 帧）…" % len(kept_frames))
    frame_records = stage_describe(vision_section if vision_on else None, kept_frames, frames_dir,
                                   args.resume, workers=4, src_size=src_size, src_mtime=src_mtime)

    # ---- 5. 时间线 / 章节 / index.json ----
    timeline = build_timeline(local, duration, asr_records, frame_records)
    write_json(os.path.join(out_dir, "timeline.json"), timeline)
    chapters = build_chapters(timeline["events"], duration)
    write_json(os.path.join(out_dir, "chapters.json"), chapters)

    asr_failed = sum(1 for record in asr_records if record.get("error"))
    described = sum(1 for record in frame_records if record.get("text"))
    described_failed = sum(1 for record in frame_records if record.get("error"))
    index_doc = {
        "video": local, "durationSec": round(duration, 3), "workdir": out_dir, "probe": probe,
        "audio": {"parts": len(audio_parts), "chunkSeconds": round(chunk_seconds, 1),
                  "coveredSec": round(sum(t1 - t0 for _, t0, t1, _ in audio_parts), 1)},
        "asr": {"ok": len(asr_records) - asr_failed, "failed": asr_failed,
                "model": str(asr_section.get("model") or "") if asr_records else "",
                "parts": [{"i": r.get("index"), "t0": r.get("t0"), "t1": r.get("t1"),
                           "chars": len(r.get("text") or ""),
                           **({"error": r["error"]} if r.get("error") else {})} for r in asr_records]},
        "vision": {"frames": len(frame_records), "described": described, "failed": described_failed,
                   "model": str(vision_section.get("model") or "") if vision_on else ""},
        "timeline": {"events": len(timeline["events"]), "stats": dict(timeline["stats"])},
        "chapters": {"count": len(chapters)},
        "startedAt": started_at, "finishedAt": now_iso(),
    }
    write_json(os.path.join(out_dir, "index.json"), index_doc)
    flush_progress()  # 先把进度行输出，再打三行关键结果
    write_text_atomic(os.path.join(out_dir, "index.log"), "\n".join(_PROGRESS) + "\n")

    stats = timeline["stats"]
    say("索引完成: %s" % out_dir)
    say("时长: %s | 语音 %d 条 / 画面 %d 帧 / 文字 %d 条 | 章节 %d"
        % (fmt_hms(duration), stats.get("speech", 0), len(frame_records), stats.get("ocr", 0), len(chapters)))
    say("时间线: %s" % os.path.join(out_dir, "timeline.json"))
    return 0


# ---------------------------------------------------------------------------
# 子命令：timeline
# ---------------------------------------------------------------------------


def load_timeline(out_dir):
    path = os.path.join(out_dir, "timeline.json")
    data = load_json(path)
    if not isinstance(data, dict):
        raise FatalError("没找到可用的时间线: %s（先跑 index）" % path)
    return data


def cmd_timeline(args, cfg, workdir):
    out_dir = os.path.abspath(os.path.expanduser(str(args.dir)))
    events = [e for e in (load_timeline(out_dir).get("events") or []) if isinstance(e, dict)]
    selected = events
    if args.kind:
        selected = [e for e in selected if e.get("kind") == args.kind]
    if args.grep:
        needle = args.grep.lower()
        selected = [e for e in selected if needle in str(e.get("text") or "").lower()]
    if args.tail is not None:  # --tail 优先于 --from/--to（SPEC §2.4）
        selected = selected[-max(0, int(args.tail)):] if int(args.tail) else []
    else:
        if args.start is not None:
            low = parse_time_arg(args.start, "--from")
            selected = [e for e in selected if float(e.get("t0") or 0.0) >= low]
        if args.end is not None:
            high = parse_time_arg(args.end, "--to")
            selected = [e for e in selected if float(e.get("t0") or 0.0) <= high]

    stats = {"speech": 0, "scene": 0, "ocr": 0}
    for event in events:  # 计数按**全量**算，否则 --tail 6 会说"共 6 条"，让人误判视频只有这么点内容
        stats[event.get("kind")] = stats.get(event.get("kind"), 0) + 1
    limit = 200 if args.limit is None else int(args.limit)
    shown = selected if limit <= 0 else selected[:limit]

    head = ("时间线 %s · 共 %d 条（语音 %d / 画面 %d / 文字 %d）"
            % (out_dir, len(events), stats["speech"], stats["scene"], stats["ocr"]))
    if len(shown) != len(events):
        head += " · 本次输出 %d 条" % len(shown)
    say(head)
    for event in shown:
        say("[%s] %s %s" % (fmt_mmss(event.get("t0")), KIND_ICON.get(event.get("kind"), "·"),
                            oneline(event.get("text"))))
    if len(selected) > len(shown):
        say("…（本次匹配 %d 条，只显示前 %d 条，用 --limit 调整）" % (len(selected), len(shown)))
    return 0


# ---------------------------------------------------------------------------
# 子命令：frames
# ---------------------------------------------------------------------------


def cmd_frames(args, cfg, workdir):
    ffmpeg = require_binary(cfg, "ffmpeg")
    local = str(args.video or "")
    local = cache_download(local, cfg, workdir) if is_url(local) \
        else os.path.abspath(os.path.expanduser(local))
    if not os.path.isfile(local):
        raise FatalError("找不到视频文件: %s" % local)
    start = parse_time_arg(args.start, "--from")
    end = parse_time_arg(args.end, "--to")
    if end <= start:
        raise FatalError("--to (%s) 必须大于 --from (%s)" % (args.end, args.start))
    fps = float(args.fps or 1.0)
    if fps <= 0:
        raise FatalError("--fps 必须大于 0")
    out_dir = os.path.abspath(os.path.expanduser(str(args.out)))
    tmp_dir = os.path.join(out_dir, ".tmp_frames")
    os.makedirs(tmp_dir, exist_ok=True)
    for name in os.listdir(tmp_dir):
        try:
            os.remove(os.path.join(tmp_dir, name))
        except OSError:
            pass
    # 一趟 ffmpeg 把整段抽成 %05d，再按序号改名成 t<秒>：比逐点 -ss 快一个数量级
    run_tool([ffmpeg, "-hide_banner", "-loglevel", "error", "-y", "-ss", "%.3f" % start,
              "-i", local, "-t", "%.3f" % (end - start),
              "-vf", "fps=%g,scale=960:-2" % fps, "-q:v", "3",
              os.path.join(tmp_dir, "f_%05d.jpg")], timeout=1800, what="ffmpeg(批量抽帧)")
    rows = []
    for position, name in enumerate(sorted(n for n in os.listdir(tmp_dir) if n.endswith(".jpg"))):
        seconds = start + position / fps
        dest = os.path.join(out_dir, "t%06.1f.jpg" % seconds)
        suffix = 1
        while os.path.exists(dest):  # fps > 10 时 1 位小数会撞名
            suffix += 1
            dest = os.path.join(out_dir, "t%06.1f_%d.jpg" % (seconds, suffix))
        os.replace(os.path.join(tmp_dir, name), dest)
        rows.append({"t": round(seconds, 3), "path": dest})
    shutil.rmtree(tmp_dir, ignore_errors=True)
    write_json(os.path.join(out_dir, "frames.json"),
               {"video": local, "from": start, "to": end, "fps": fps, "count": len(rows), "files": rows})
    say("抽帧完成: %d 张 → %s" % (len(rows), out_dir))
    for row in rows[:10]:
        say(row["path"])
    say("清单: %s" % os.path.join(out_dir, "frames.json"))
    return 0


# ---------------------------------------------------------------------------
# 子命令：summarize
# ---------------------------------------------------------------------------


def extract_facts(text):
    """只认以 [mm:ss] / [h:mm:ss] 开头的行 —— 契约规定事实必须带时间戳。"""
    return [item for item in (line.strip().lstrip("-*•").strip() for line in str(text or "").splitlines())
            if FACT_RE.match(item)]


def plan_blocks(rows, block_chars):
    """按事件边界拼块：每块不超过 block_chars 字符，单条超长行自己一块（不切断单条）。"""
    limit = max(200, int(block_chars))
    blocks, current = [], {"lines": [], "chars": 0, "t0": 0.0, "t1": 0.0}
    for t0, line in rows:
        if current["lines"] and current["chars"] + len(line) + 1 > limit:
            blocks.append(current)
            current = {"lines": [], "chars": 0, "t0": t0, "t1": t0}
        if not current["lines"]:
            current["t0"] = t0
        current["lines"].append(line)
        current["chars"] += len(line) + 1
        current["t1"] = t0
    if current["lines"]:
        blocks.append(current)
    for position, block in enumerate(blocks):
        block["i"], block["n"] = position, len(blocks)
    return blocks


def summarize_rows(events):
    """把时间线折成"喂给 map"的行。

    画面描述在汇总阶段只是佐证，所以截到前 SCENE_SUMMARY_CHARS 字并打上（画面）标记：
    实测 115 帧描述 ≈ 17k 字，是转写正文 7.6k 字的两倍多，不截断的话块预算会被美术
    描述吃掉，模型抽出来全是"一名穿红衣的少年站在雪地里"，旁白的论证反而进不去。
    """
    rows = []
    for event in events:
        kind = event.get("kind")
        if kind not in ("speech", "scene"):
            continue
        text = oneline(event.get("text"))
        if not text:
            continue
        t0 = float(event.get("t0") or 0.0)
        if kind == "scene":
            rows.append((t0, "[%s] （画面）%s" % (fmt_mmss(t0), text[:SCENE_SUMMARY_CHARS])))
        else:
            rows.append((t0, "[%s] %s" % (fmt_mmss(t0), text)))
    return rows


def cmd_summarize(args, cfg, workdir):
    out_dir = os.path.abspath(os.path.expanduser(str(args.dir)))
    data = load_timeline(out_dir)
    llm_section = cfg["llm"]
    if not endpoint_ready(llm_section):
        say("LLM 端点未配置。请在 设置 → 视频理解 → 文本汇总 里填写接口地址、API key、模型名。")
        return 1
    rows = summarize_rows(data.get("events") or [])
    if not rows:
        raise FatalError("时间线里没有可汇总的语音/画面事件: %s" % os.path.join(out_dir, "timeline.json"))

    mode = args.mode
    blocks = plan_blocks(rows, args.block_chars)
    question = str(args.question or "").strip()
    arrow("切成 %d 块，开始 map（并发 %d）…" % (len(blocks), max(1, int(args.parallel))))

    def run_map(block):
        user = ("下面是一段视频的第 %d/%d 块内容，每行以 [时间] 开头。\n%s\n\n"
                "请抽 3-5 条事实，每条一行，必须以 [mm:ss] 开头，只写素材里出现的内容。\n"
                "取材优先级：旁白讲的观点、论证、结论 > 屏幕上出现的文字 > 画面。"
                "带（画面）标记的行只在它承载信息（字幕、图表、标题、明显的叙事画面）时才采用，"
                "不要逐帧做美术描述。"
                % (block["i"] + 1, block["n"], "\n".join(block["lines"])))
        if question:
            user += "\n只关注与这个问题相关的内容：%s" % question
        text, error = llm_chat(llm_section, MAP_SYSTEM, user)
        record = {"i": block["i"], "t0": round(block["t0"], 3), "t1": round(block["t1"], 3),
                  "chars": block["chars"], "facts": extract_facts(text) if text else []}
        if error:
            record["error"] = error
            note("map 块 %d/%d 失败: %s" % (block["i"] + 1, block["n"], oneline(error, 160)))
        else:
            arrow("map 块 %d/%d 完成（%d 条事实）" % (block["i"] + 1, block["n"], len(record["facts"])))
        return record

    with ThreadPoolExecutor(max_workers=max(1, int(args.parallel))) as pool:
        mapped = list(pool.map(run_map, blocks))

    # tail-first 只反转"喂给 reduce 的顺序"，map 的调用顺序不变（SPEC §2.6）
    ordered = mapped if mode == "mapreduce" else list(reversed(mapped))
    all_facts = [fact for record in ordered for fact in record["facts"]]
    arrow("reduce 开始（%d 条事实，%s）…" % (len(all_facts), mode))
    reduce_text, reduce_error = llm_chat(
        llm_section, REDUCE_SYSTEM,
        "以下是同一段视频各块抽出的全部事实，请去掉完全重复的条目并按时间排序后原样输出。\n"
        + "\n".join(all_facts), timeout=300)
    facts = extract_facts(reduce_text) if reduce_text else []
    if not facts:
        facts = all_facts  # reduce 挂了也别丢内容，facts 保留 map 的全量
        reduce_error = reduce_error or "reduce 输出里没有带时间戳的事实"
    if reduce_error:
        note("reduce 失败: %s" % oneline(reduce_error, 200))

    summary = {
        "mode": mode, "question": question, "blockCount": len(blocks),
        "blocks": [{k: v for k, v in record.items() if k != "n"} for record in mapped],
        "facts": facts,
        "reduce": {"text": reduce_text or "", **({"error": reduce_error} if reduce_error else {})},
        "model": str(llm_section.get("model") or ""),
        "createdAt": now_iso(),
    }
    write_json(os.path.join(out_dir, "summary.json"), summary)

    lines = ["# 视频摘要", "",
             "- 模式: %s · 块数: %d · 模型: %s" % (mode, len(blocks), summary["model"] or "(未指定)")]
    if question:
        lines.append("- 关注问题: %s" % question)
    lines.extend(["", "## 关键结论", ""])
    lines.extend(["- %s" % fact for fact in facts] or ["- （无）"])
    write_text_atomic(os.path.join(out_dir, "summary.md"), "\n".join(lines) + "\n")

    flush_progress()
    for fact in facts:
        say(fact)
    say("摘要: %s" % os.path.join(out_dir, "summary.md"))
    return 0


# ---------------------------------------------------------------------------
# 子命令：report
# ---------------------------------------------------------------------------


def cmd_report(args, cfg, workdir):
    out_dir = os.path.abspath(os.path.expanduser(str(args.dir)))
    probe = load_json(os.path.join(out_dir, "probe.json")) or {}
    index_doc = load_json(os.path.join(out_dir, "index.json")) or {}
    timeline = load_json(os.path.join(out_dir, "timeline.json")) or {}
    chapters = load_json(os.path.join(out_dir, "chapters.json")) or []
    summary = load_json(os.path.join(out_dir, "summary.json")) or {}
    if not probe and not timeline:
        raise FatalError("这个目录里没有 probe.json / timeline.json，先跑 index: %s" % out_dir)

    video_path = probe.get("path") or timeline.get("video") or index_doc.get("video") or ""
    duration = float(probe.get("durationSec") or timeline.get("durationSec")
                     or index_doc.get("durationSec") or 0.0)
    events = timeline.get("events") or []
    stats = timeline.get("stats") if isinstance(timeline.get("stats"), dict) else {}
    frames_dir = os.path.join(out_dir, "frames")
    frame_files = sorted(os.path.join(frames_dir, name) for name in os.listdir(frames_dir)
                         if name.lower().endswith(".jpg")) if os.path.isdir(frames_dir) else []
    facts = summary.get("facts") or []
    asr_index = index_doc.get("asr") or {}
    vision_index = index_doc.get("vision") or {}

    lines = ["# %s 视频理解报告" % (os.path.basename(video_path) or os.path.basename(out_dir)), "", "## 概要", ""]
    lines.append("- 时长: %s（%.1f s）" % (fmt_hms(duration), duration))
    lines.append("- 分辨率: %dx%d @ %s fps" % (int(probe.get("width") or 0),
                                             int(probe.get("height") or 0), fmt_fps(probe.get("fps"))))
    lines.append("- 语音 %d 条 / 画面 %d 帧 / 章节 %d"
                 % (stats.get("speech", 0), stats.get("scene", 0), len(chapters)))
    if facts:
        lines.append("- 前 10 条结论（map-reduce）:")
        lines.extend("  - %s" % fact for fact in facts[:10])
    lines.extend(["", "## 基本信息", "", "| 项 | 值 |", "|---|---|"])
    rows = [
        ("文件", video_path or "(未知)"),
        ("时长", "%s (%.1f s)" % (fmt_hms(duration), duration)),
        ("画面", "%dx%d @ %s fps, %s" % (int(probe.get("width") or 0), int(probe.get("height") or 0),
                                       fmt_fps(probe.get("fps")), probe.get("videoCodec") or "未知")),
        ("音频", "%s %dch %dHz" % (probe.get("audioCodec") or "未知",
                                 int(probe.get("audioChannels") or 0),
                                 int(probe.get("audioSampleRate") or 0)) if probe.get("hasAudio") else "无"),
        ("大小", fmt_mb(probe.get("sizeBytes"))),
        ("工作目录", out_dir),
    ]
    lines.extend("| %s | %s |" % (name, value) for name, value in rows)

    lines.extend(["", "## 章节时间线", ""])
    if chapters:
        lines.extend(["| # | 时间 | 标题 | 摘要 |", "|---|---|---|---|"])
        lines.extend("| %s | %s - %s | %s | %s |" % (
            chapter.get("index", ""), fmt_mmss(chapter.get("t0")), fmt_mmss(chapter.get("t1")),
            str(chapter.get("title") or "").replace("|", "\\|"),
            str(chapter.get("summary") or "").replace("|", "\\|")) for chapter in chapters)
    else:
        lines.append("（没有切出章节）")

    lines.extend(["", "## 关键结论（来自 map-reduce）", ""])
    lines.extend(["- %s" % fact for fact in facts] or ["（未运行 summarize）"])

    lines.extend(["", "## 尾部事实清单", ""])
    tail_events = [e for e in events if e.get("kind") in ("speech", "scene")
                   and float(e.get("t0") or 0) >= duration * 0.9]
    lines.extend(["- [%s] %s" % (fmt_mmss(e.get("t0")), oneline(e.get("text"), 240)) for e in tail_events]
                 or ["（尾部 10% 时间区间内没有语音/画面事件，时间线总长 %s）" % fmt_hms(duration)])

    lines.extend(["", "## 关键帧", ""])
    lines.extend(["- %s" % path for path in frame_files[:30]] or ["（没有帧图）"])

    # 端点只写 base + model，绝不落 key
    lines.extend(["", "## 端点与统计", "", "| 阶段 | 接口地址 | 模型 | 失败数 |", "|---|---|---|---|"])
    for label, section, model, failed in (
            ("语音识别", cfg["asr"], asr_index.get("model") or cfg["asr"].get("model"), asr_index.get("failed", 0)),
            ("画面理解", cfg["vision"], vision_index.get("model") or cfg["vision"].get("model"),
             vision_index.get("failed", 0)),
            ("文本汇总", cfg["llm"], summary.get("model") or cfg["llm"].get("model"),
             1 if (summary.get("reduce") or {}).get("error") else 0)):
        lines.append("| %s | %s | %s | %s |" % (label, section.get("base") or "(未配置)", model or "-", failed))
    lines.extend(["", "- 章节数: %d · 时间线事件: %d · 帧: %d"
                  % (len(chapters), len(events), len(frame_files))])
    if summary.get("mode"):
        lines.append("- 汇总模式: %s · 块数: %s" % (summary.get("mode"), summary.get("blockCount")))

    target = os.path.abspath(os.path.expanduser(str(args.out))) if args.out \
        else os.path.join(out_dir, "report.md")
    write_text_atomic(target, "\n".join(lines) + "\n")
    say("报告已生成: %s" % target)
    return 0


# ---------------------------------------------------------------------------
# 子命令：selftest
# ---------------------------------------------------------------------------


def cmd_selftest(args, cfg, workdir):
    """SPEC §2.8 的四条断言（test/selftest.py 里有更全的同款检查）。"""
    checks = [
        ("plan_chunks(90*60, 900) 返回 6 段",
         lambda: _assert(len(plan_chunks(90 * 60, 900)) == 6, "期望 6 段，实际 %r" % (plan_chunks(90 * 60, 900),))),
        ("每段 estimate_b64_mb < 10",
         lambda: _assert(all(estimate_b64_mb(t1 - t0) < 10.0 for t0, t1 in plan_chunks(90 * 60, 900)),
                         "有分段的 base64 估算超过 10MB")),
        ("elect_frames 300 候选 / max 200",
         lambda: _assert(len(elect_frames([{"t": float(i * 12), "score": float(i % 7), "tail": i > 250}
                                           for i in range(300)], 200, 3600.0, 0.2)) == 200,
                         "期望 200 条")),
        ("elect_frames 含首尾帧", lambda: _assert_first_last()),
        ("parse_time 三种写法",
         lambda: _assert(parse_time("1:30") == 90 and parse_time("01:02:03") == 3723 and parse_time("90") == 90,
                         "parse_time 解析不符合预期")),
        ("末段短于 5s 时合并", lambda: _assert(plan_chunks(602, 600) == [(0.0, 602.0)], "602s/600s 应合并成 1 段")),
    ]
    failed = 0
    for name, func in checks:
        try:
            func()
        except AssertionError as exc:
            failed += 1
            sys.stderr.write("[失败] %s：%s\n" % (name, exc))
        except Exception as exc:
            failed += 1
            sys.stderr.write("[失败] %s：%s: %s\n%s" % (name, type(exc).__name__, exc, traceback.format_exc()))
    if failed:
        sys.stderr.write("selftest 失败 %d/%d\n" % (failed, len(checks)))
        return 1
    say("selftest ok")
    return 0


def _assert(condition, message):
    if not condition:
        raise AssertionError(message)


def _assert_first_last():
    picked = elect_frames([{"t": float(i * 12), "score": float(i % 7), "tail": i > 250} for i in range(300)],
                          200, 3600.0, 0.2)
    times = [item["t"] for item in picked]
    _assert(any(abs(t) < 0.5 for t in times), "缺少首帧")
    _assert(any(abs(t - 3600.0) < 1.0 for t in times), "缺少尾帧")
    _assert(times == sorted(times), "未按 t 升序")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def add_globals(parser):
    """host 把 --config/--workdir 追加在子命令参数最后，所以必须挂在每个 subparser 上。"""
    parser.add_argument("--config", default=None, help="配置文件路径（默认 ~/.dsh/dsh-videokit.json）")
    parser.add_argument("--workdir", default=None, help="工作目录")


# ---------------------------------------------------------------------------
# 分 P 系列：一个 30 小时的课程不是"一个视频"，是 61 个视频
# ---------------------------------------------------------------------------
# 设计判断：不要让一个 index 目录吞下 30 小时。timeline 会长到没法读、
# maxFrames 的预算摊到 30 小时等于每 9 分钟一帧（画面信息全丢）、
# 任何一步失败都要从头再来。所以按 P 建索引（<root>/<P01>/），
# 系列级只做聚合（series-report），两层各司其职。


def series_entries(root):
    """列出系列里的分 P：优先读 manifest.json（下载时写的），否则扫 mp4。"""
    entries = []
    manifest_path = os.path.join(root, "manifest.json")
    if os.path.isfile(manifest_path):
        manifest = load_json(manifest_path)
        for item in (manifest or {}).get("pages") or []:
            if not isinstance(item, dict) or not item.get("path"):
                continue
            path = os.path.join(root, item["path"])
            if not os.path.isfile(path):
                continue
            label = os.path.splitext(item["path"])[0]
            entries.append({"p": int(item.get("p") or len(entries) + 1), "label": label,
                            "path": path, "title": item.get("title") or "",
                            "duration": float(item.get("duration") or 0)})
    if entries:
        return entries
    for name in sorted(os.listdir(root)):
        path = os.path.join(root, name)
        stem, ext = os.path.splitext(name)
        if os.path.isfile(path) and ext.lower() in (".mp4", ".mkv", ".webm", ".flv", ".mov"):
            match = re.search(r"_p(\d+)$", stem)
            entries.append({"p": int(match.group(1)) if match else len(entries) + 1,
                            "label": stem, "path": path, "title": "", "duration": 0.0})
    entries.sort(key=lambda item: item["p"])
    return entries


def cmd_index_series(args, cfg, workdir):
    """逐 P 建索引：<root>/<P01>/ 各一套产物。失败不中断，已完成的自动跳过。"""
    root = os.path.abspath(os.path.expanduser(str(args.dir)))
    entries = series_entries(root)
    if not entries:
        raise FatalError("这个目录里没有可索引的分 P（找不到 manifest.json，也没有 mp4）：%s" % root)
    if args.only:
        wanted = {int(item) for item in str(args.only).replace("，", ",").split(",") if item.strip().isdigit()}
        entries = [item for item in entries if item["p"] in wanted]
    note("系列 %s：%d 个分 P" % (root, len(entries)))
    done = skipped = failed = 0
    failed_pages = []
    for position, entry in enumerate(entries, 1):
        out_dir = os.path.join(root, entry["label"])
        head = "P %d/%d %s" % (position, len(entries), entry["label"])
        if args.resume and os.path.isfile(os.path.join(out_dir, "index.json")):
            arrow("%s 已完成，跳过" % head)
            skipped += 1
            continue
        arrow("%s 建索引…" % head)
        child = argparse.Namespace(
            video=entry["path"], out=out_dir, density=args.density, tail_ratio=args.tail_ratio,
            max_frames=args.max_frames, vision=args.vision, asr=args.asr, resume=args.resume,
            config=args.config, workdir=workdir)
        try:
            if cmd_index(child, cfg, workdir) == 0:
                done += 1
            else:
                failed += 1
                failed_pages.append(entry["p"])
        except (FatalError, MissingDependency, OSError) as exc:
            failed += 1
            failed_pages.append(entry["p"])
            note("%s 失败: %s" % (head, oneline(str(exc), 200)))
    say("系列索引完成：新增 %d / 跳过 %d / 失败 %d（共 %d 个分 P）"
        % (done, skipped, failed, len(entries)))
    if failed_pages:
        say("失败的分 P: %s（重跑同一条命令会跳过已完成的）"
            % ", ".join("P%d" % number for number in failed_pages))
        return 1
    return 0


SERIES_SUMMARY_SYSTEM = "你是课程内容的编辑。只写素材里能看出来的东西，不编造。"


def series_page_brief(cfg, entry, out_dir, cache_dir):
    """一集的 2-3 句摘要 + 关键词。有缓存就复用（LLM 调用很贵，30 小时更是）。"""
    cache_path = os.path.join(cache_dir, "%s.json" % entry["label"])
    if os.path.isfile(cache_path):
        cached = load_json(cache_path)
        if isinstance(cached, dict) and cached.get("brief"):
            return cached
    chapters = load_json(os.path.join(out_dir, "chapters.json")) or []
    if not isinstance(chapters, list):
        chapters = []
    lines = []
    for chapter in chapters[:40]:
        lines.append("[%s] %s" % (fmt_mmss(chapter.get("t0")), oneline(chapter.get("title"), 60)))
    if not lines:
        return {"label": entry["label"], "p": entry["p"], "title": entry["title"], "brief": "", "error": "无章节"}
    user = ("下面是一集网课的分段标题（有则用，没有就跳过）。\n"
            "%s\n\n请输出两行：\n摘要：用 2-3 句说明这一集讲了什么\n关键词：3 个，用、分隔"
            % "\n".join(lines))
    text, error = llm_chat(cfg["llm"], SERIES_SUMMARY_SYSTEM, user, timeout=180)
    record = {"label": entry["label"], "p": entry["p"], "title": entry["title"]}
    if error or not text:
        record["error"] = error or "空响应"
    else:
        brief, keywords = "", ""
        for line in text.splitlines():
            if line.startswith("摘要："):
                brief = line[3:].strip()
            elif line.startswith("关键词："):
                keywords = line[4:].strip()
        record["brief"] = brief or oneline(text, 120)
        record["keywords"] = keywords
    write_json(cache_path, record)
    return record


def cmd_series_report(args, cfg, workdir):
    """把各 P 的索引聚合成一份系列报告（可选 LLM 摘要 + 课程大纲）。"""
    root = os.path.abspath(os.path.expanduser(str(args.dir)))
    entries = series_entries(root)
    if not entries:
        raise FatalError("这个目录里没有分 P：%s" % root)
    rows, missing = [], []
    for entry in entries:
        out_dir = os.path.join(root, entry["label"])
        index = load_json(os.path.join(out_dir, "index.json")) or {}
        if not isinstance(index, dict) or not index:
            missing.append(entry["p"])
        rows.append({"p": entry["p"], "label": entry["label"], "title": entry["title"],
                     "duration": float(index.get("durationSec") or entry["duration"] or 0),
                     "chapters": (index.get("chapters") or {}).get("count") or 0,
                     "speech": (index.get("timeline") or {}).get("stats", {}).get("speech") or 0,
                     "indexed": bool(index)})
    done = [row for row in rows if row["indexed"]]
    note("系列 %s：%d 个分 P，已索引 %d 个" % (root, len(rows), len(done)))
    if not done:
        raise FatalError("一个分 P 都还没建索引：先跑 index-series %s" % root)

    briefs = []
    if args.summarize:
        if not endpoint_ready(cfg["llm"]):
            raise FatalError("LLM 端点未配置，无法生成每集摘要（去掉 --summarize 只出统计表）")
        cache_dir = os.path.join(root, ".series-cache")
        os.makedirs(cache_dir, exist_ok=True)
        arrow("逐集摘要中（%d 集，并发 4）…" % len(done))

        def work(row):
            return series_page_brief(cfg, {"label": row["label"], "p": row["p"], "title": row["title"]},
                                     os.path.join(root, row["label"]), cache_dir)

        with ThreadPoolExecutor(max_workers=4) as pool:
            briefs = list(pool.map(work, done))
        ok = sum(1 for item in briefs if item.get("brief"))
        arrow("逐集摘要完成：%d/%d" % (ok, len(briefs)))

    outline = ""
    if args.summarize and briefs:
        packed = "\n".join("P%d %s｜%s" % (item["p"], (item.get("title") or "")[:30], item.get("brief") or "")
                           for item in briefs if item.get("brief"))
        user = ("下面是一门网课各集的摘要（共 %d 集）。\n%s\n\n"
                "请输出一段课程大纲：先写一句整门课讲什么，再按 3-8 个板块列出覆盖的集号与板块主题。"
                "只写素材里能看出来的东西。" % (len(briefs), packed))
        text, error = llm_chat(cfg["llm"], SERIES_SUMMARY_SYSTEM, user, timeout=300)
        if error:
            note("课程大纲生成失败: %s" % oneline(error, 160))
        else:
            outline = text or ""

    total_seconds = sum(row["duration"] for row in rows)
    out_path = os.path.abspath(os.path.expanduser(str(args.out))) if args.out \
        else os.path.join(root, "series.md")
    lines = ["# %s 系列视频理解报告" % (os.path.basename(root)), "",
             "## 总览", "",
             "- 分 P: %d 个（已索引 %d）" % (len(rows), len(done)),
             "- 总时长: %s（%.1f 小时）" % (fmt_mmss(total_seconds), total_seconds / 3600.0),
             "- 已索引时长: %s" % fmt_mmss(sum(row["duration"] for row in done)),
             "- 语音事件合计: %d 条 / 章节合计: %d 个"
             % (sum(row["speech"] for row in done), sum(row["chapters"] for row in done))]
    if missing:
        lines.append("- 尚未索引: %s" % ", ".join("P%d" % number for number in missing[:20])
                     + (" …" if len(missing) > 20 else ""))
    if outline:
        lines += ["", "## 课程大纲（LLM 归纳）", "", outline.strip()]
    lines += ["", "## 分 P 一览", "",
              "| P | 时长 | 章节 | 标题 | 摘要 |", "|---|---|---|---|---|"]
    brief_by_p = {item["p"]: item for item in briefs}
    for row in rows:
        brief = (brief_by_p.get(row["p"]) or {}).get("brief") or ""
        lines.append("| %d | %s | %s | %s | %s |"
                     % (row["p"], fmt_mmss(row["duration"]),
                        row["chapters"] if row["indexed"] else "未索引",
                        oneline(row["title"], 40).replace("|", "／"),
                        oneline(brief, 120).replace("|", "／")))
    with open(out_path, "w", encoding="utf-8") as handle:
        handle.write("\n".join(lines) + "\n")
    write_json(os.path.join(root, "series.json"),
               {"root": root, "totalPages": len(rows), "indexed": len(done),
                "totalSeconds": total_seconds, "outline": outline, "pages": rows,
                "briefs": briefs, "missing": missing})
    say("系列报告已生成: %s" % out_path)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="videokit.py",
                                     description="dsh-videokit 视频理解引擎（ffmpeg + OpenAI 兼容端点）")
    sub = parser.add_subparsers(dest="command")
    p = sub.add_parser("probe", help="探测视频并写 probe.json")
    p.add_argument("video")
    p.add_argument("--out", default=None)
    add_globals(p)
    p = sub.add_parser("download", help="下载在线视频")
    p.add_argument("url")
    p.add_argument("--out", default=None)
    p.add_argument("--p", dest="page", type=int, default=None, help="只下第 N 个分 P（默认取 URL 里的 ?p=）")
    p.add_argument("--all-pages", dest="all_pages", action="store_true", help="下载全部分 P（可断点续传）")
    add_globals(p)
    p = sub.add_parser("index", help="建索引（音频转写 + 抽帧 + VLM + 时间线）")
    p.add_argument("video")
    p.add_argument("--out", default=None)
    p.add_argument("--density", type=float, default=1.0)
    p.add_argument("--tail-ratio", dest="tail_ratio", type=float, default=0.2)
    p.add_argument("--max-frames", dest="max_frames", type=int, default=200)
    p.add_argument("--vision", choices=("auto", "none"), default="auto")
    p.add_argument("--asr", choices=("auto", "none"), default="auto")
    p.add_argument("--resume", action="store_true")
    add_globals(p)
    p = sub.add_parser("timeline", help="紧凑打印时间线")
    p.add_argument("dir")
    p.add_argument("--from", dest="start", default=None, help="起始时间：90 / 1:30 / 01:02:03")
    p.add_argument("--to", dest="end", default=None, help="结束时间，写法同上")
    p.add_argument("--grep", default=None, help="按正文关键词过滤（不区分大小写）")
    p.add_argument("--tail", type=int, default=None, help="只取最后 N 条")
    p.add_argument("--kind", choices=("speech", "scene", "ocr"), default=None)
    p.add_argument("--limit", type=int, default=200)
    add_globals(p)
    p = sub.add_parser("frames", help="定点高密度抽帧")
    p.add_argument("video")
    p.add_argument("--out", required=True)
    p.add_argument("--from", dest="start", required=True)
    p.add_argument("--to", dest="end", required=True)
    p.add_argument("--fps", type=float, default=1.0)
    add_globals(p)
    p = sub.add_parser("summarize", help="map-reduce / tail-first 汇总")
    p.add_argument("dir")
    p.add_argument("--mode", choices=("mapreduce", "tail-first"), default="mapreduce")
    p.add_argument("--question", default=None)
    p.add_argument("--block-chars", dest="block_chars", type=int, default=6000)
    p.add_argument("--parallel", type=int, default=4)
    add_globals(p)
    p = sub.add_parser("report", help="生成 markdown 报告")
    p.add_argument("dir")
    p.add_argument("--out", default=None)
    add_globals(p)
    p = sub.add_parser("index-series", help="逐分 P 建索引（30 小时课程用这个）")
    p.add_argument("dir", help="download --all-pages 的输出目录")
    p.add_argument("--density", type=float, default=1.0)
    p.add_argument("--tail-ratio", dest="tail_ratio", type=float, default=0.2)
    p.add_argument("--max-frames", dest="max_frames", type=int, default=60)
    p.add_argument("--vision", choices=("auto", "none"), default="auto")
    p.add_argument("--asr", choices=("auto", "none"), default="auto")
    p.add_argument("--resume", action="store_true")
    p.add_argument("--only", default=None, help="只处理这些分 P，逗号分隔，如 1,2,61")
    add_globals(p)
    p = sub.add_parser("series-report", help="把各分 P 的索引聚合成系列报告")
    p.add_argument("dir")
    p.add_argument("--out", default=None)
    p.add_argument("--summarize", action="store_true", help="逐集 LLM 摘要 + 课程大纲（要 llm 端点）")
    add_globals(p)
    p = sub.add_parser("selftest", help="纯本地自检")
    add_globals(p)
    return parser


HANDLERS = {"probe": cmd_probe, "download": cmd_download, "index": cmd_index,
            "timeline": cmd_timeline, "frames": cmd_frames, "summarize": cmd_summarize,
            "report": cmd_report, "index-series": cmd_index_series,
            "series-report": cmd_series_report, "selftest": cmd_selftest}


def main(argv=None):
    reconfigure_stdio()
    parser = build_parser()
    raw = list(sys.argv[1:] if argv is None else argv)
    if not raw:
        parser.print_help()
        return 1
    try:
        args = parser.parse_args(raw)
    except SystemExit as exc:  # argparse 自己的 -h / 参数错
        return int(exc.code or 0)
    if not args.command:
        parser.print_help()
        return 1
    try:
        cfg = read_config(config_path(args.config))
        code = HANDLERS[args.command](args, cfg, resolve_workdir(cfg, args.workdir))
    except MissingDependency as exc:
        flush_progress()
        sys.stderr.write("缺少可选依赖: %s\n" % exc)
        return 2
    except FatalError as exc:
        flush_progress()
        sys.stderr.write("错误: %s\n" % exc)
        return 1
    except KeyboardInterrupt:
        flush_progress()
        sys.stderr.write("已中断\n")
        return 130
    except Exception as exc:  # 兜底：未预期异常也给中文 + 退出码 1，而不是裸 traceback 崩掉 host
        flush_progress()
        sys.stderr.write("内部错误: %s: %s\n%s" % (type(exc).__name__, exc, traceback.format_exc()))
        return 1
    finally:
        flush_progress()
    return 0 if code is None else int(code)


if __name__ == "__main__":
    sys.exit(main())
