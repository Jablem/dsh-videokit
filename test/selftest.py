#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""纯函数自检（零网络、零 ffmpeg）。

scripts/videokit.py 目录不是包，所以用 importlib.util.spec_from_file_location 直接加载模块，
然后对 SPEC §2.8 的四条断言 + 几条边界回归逐条跑。全部通过打印 `selftest ok` 并退出 0。

等价于 `python scripts/videokit.py selftest`，但这里能额外覆盖模块级 API 形状
（CLI 那侧的 selftest 只跑内建检查）。
"""

import importlib.util
import os
import sys
import traceback

# 插件目录是本地目录链接安装的，别在里面留 __pycache__
sys.dont_write_bytecode = True

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "videokit.py")


def load_engine():
    """把 videokit.py 当孤立模块加载（它本身不做 import 时副作用）。"""
    spec = importlib.util.spec_from_file_location("videokit_engine", SCRIPT)
    if spec is None or spec.loader is None:
        raise RuntimeError("无法加载引擎脚本: %s" % SCRIPT)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def build_checks(engine):
    plan_chunks = engine.plan_chunks
    estimate_b64_mb = engine.estimate_b64_mb
    elect_frames = engine.elect_frames
    parse_time = engine.parse_time
    checks = []

    def check(name):
        def decorator(func):
            checks.append((name, func))
            return func
        return decorator

    # ---- SPEC §2.8 第 1、2 条 ----
    @check("plan_chunks(90*60, 900) 恰好 6 段，每段 900 s")
    def _():
        spans = plan_chunks(90 * 60, 900)
        assert len(spans) == 6, "期望 6 段，实际 %d 段: %r" % (len(spans), spans)
        assert spans[0] == (0.0, 900.0), "首段应为 (0, 900): %r" % (spans[0],)
        assert spans[-1] == (4500.0, 5400.0), "末段应为 (4500, 5400): %r" % (spans[-1],)
        assert all(abs((b - a) - 900.0) < 1e-6 for a, b in spans), "每段应恰好 900 s: %r" % (spans,)

    @check("6 段的 estimate_b64_mb 全部 < 10 MB")
    def _():
        for t0, t1 in plan_chunks(90 * 60, 900):
            size = estimate_b64_mb(t1 - t0)
            assert size < 10.0, "段 %.0f-%.0f 的 base64 估算 %.3f MB 超过 10 MB" % (t0, t1, size)
        # 反向确认公式不是恒真：2 小时必须越过 9.5 MB 的自动减半阈值
        assert estimate_b64_mb(7200) > 9.5, "estimate_b64_mb(7200) 应超过 9.5 MB"

    # ---- SPEC §2.8 第 3 条 ----
    @check("elect_frames(300 候选, max_frames=200) → 200 条且含首尾帧")
    def _():
        candidates = [
            {"t": float(i) * 12.0, "score": float(i % 7), "tail": i >= 250}
            for i in range(300)
        ]
        picked = elect_frames(candidates, 200, 3600.0, 0.2)
        assert len(picked) == 200, "期望 200 条，实际 %d 条" % len(picked)
        times = [item["t"] for item in picked]
        assert times == sorted(times), "返回值必须按 t 升序"
        assert any(abs(t) < 0.5 for t in times), "必须保留首帧（t≈0）"
        assert any(abs(t - 3600.0) < 1.0 for t in times), "必须保留尾帧（t≈duration）"
        assert len(set(times)) == len(times), "不应有重复时间点"

    # ---- SPEC §2.8 第 4 条 ----
    @check("parse_time 三种写法：90 / 1:30 / 01:02:03")
    def _():
        assert parse_time("90") == 90, "parse_time('90') 应为 90，实际 %r" % (parse_time("90"),)
        assert parse_time("1:30") == 90, "parse_time('1:30') 应为 90，实际 %r" % (parse_time("1:30"),)
        assert parse_time("01:02:03") == 3723, "parse_time('01:02:03') 应为 3723，实际 %r" % (
            parse_time("01:02:03"),)
        assert parse_time("0") == 0 and parse_time("0:00:00") == 0

    # ---- 额外的边界回归 ----
    @check("末段短于 5 s 时合并进上一段；正好 5 s 不合并")
    def _():
        merged = plan_chunks(602, 600)
        assert merged == [(0.0, 602.0)], "602s / 600s 应合并成 1 段，实际 %r" % (merged,)
        kept = plan_chunks(605, 600)
        assert len(kept) == 2, "末段正好 5 s 不该合并，实际 %r" % (kept,)
        assert plan_chunks(0, 900) == [], "时长 0 应返回空列表"
        assert plan_chunks(90, 900) == [(0.0, 90.0)], "短于一段的视频应返回 1 段"

    @check("elect_frames 在候选稀缺时仍然补出首尾帧")
    def _():
        picked = elect_frames([{"t": 30.0, "score": 0.5, "tail": False}], 10, 90.0, 0.2)
        times = [item["t"] for item in picked]
        assert len(times) == 3, "应补出首帧与尾帧再带上场景帧，实际 %r" % (times,)
        assert times[0] == 0.0 and abs(times[-1] - 90.0) < 1e-6, "首尾帧丢了: %r" % (times,)

        tight = elect_frames(
            [{"t": 0.0, "score": 0.0}, {"t": 45.0, "score": 0.9}, {"t": 90.0, "score": 0.0}],
            max_frames=2, duration_sec=90.0, tail_ratio=0.2,
        )
        tight_times = [item["t"] for item in tight]
        assert len(tight_times) == 2 and tight_times[0] == 0.0 and tight_times[-1] == 90.0, (
            "max_frames=2 时首尾优先，实际 %r" % (tight_times,))

    @check("elect_frames 场景帧按分数优先于均匀帧")
    def _():
        candidates = [{"t": float(i) * 10.0, "score": 0.0, "tail": False} for i in range(20)]
        candidates.append({"t": 155.0, "score": 0.98, "tail": False})
        picked = elect_frames(candidates, 5, 200.0, 0.2)
        assert any(abs(item["t"] - 155.0) < 0.5 for item in picked), "高分场景帧应被选中: %r" % (
            [item["t"] for item in picked],)

    @check("chat_endpoint / auth_headers 按 SPEC §1 规范化")
    def _():
        assert engine.chat_endpoint("https://x/v1/") == "https://x/v1/chat/completions"
        assert engine.chat_endpoint("https://x/v1") == "https://x/v1/chat/completions"
        assert engine.chat_endpoint("https://x/v1/chat/completions") == "https://x/v1/chat/completions"
        assert engine.chat_endpoint("") == ""
        assert engine.auth_headers({"key": "k1", "auth": "api-key"}).get("api-key") == "k1"
        assert engine.auth_headers({"key": "k2", "auth": "bearer"})["Authorization"] == "Bearer k2"

    @check("read_config 在文件缺失 / JSON 损坏时退化成默认值")
    def _():
        import tempfile

        missing = os.path.join(tempfile.gettempdir(), "videokit-does-not-exist-%d.json" % os.getpid())
        cfg = engine.read_config(missing)
        assert cfg["asr"]["chunkMinutes"] == 5, "默认 chunkMinutes 应为 5（长段会被模型跳读）"
        assert cfg["workdir"], "默认 workdir 不能为空"

        handle, broken = tempfile.mkstemp(suffix=".json")
        os.close(handle)
        with open(broken, "w", encoding="utf-8") as stream:
            stream.write("{ this is not json")
        try:
            cfg2 = engine.read_config(broken)
            assert cfg2["llm"]["maxTokens"] == 2048, "损坏的配置应退化成默认值"
        finally:
            os.remove(broken)

    return checks


def main():
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    if not os.path.isfile(SCRIPT):
        sys.stderr.write("找不到引擎脚本: %s\n" % SCRIPT)
        return 1
    engine = load_engine()
    checks = build_checks(engine)
    failed = 0
    for name, func in checks:
        try:
            func()
        except AssertionError as exc:
            failed += 1
            print("[失败] %s\n        %s" % (name, exc))
        except Exception as exc:
            failed += 1
            print("[异常] %s\n        %s: %s" % (name, type(exc).__name__, exc))
            traceback.print_exc()
        else:
            print("[通过] %s" % name)
    if failed:
        sys.stderr.write("selftest 失败 %d/%d\n" % (failed, len(checks)))
        return 1
    print("selftest ok")
    return 0


if __name__ == "__main__":
    sys.exit(main())
