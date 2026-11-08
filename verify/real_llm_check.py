#!/usr/bin/env python3
"""真实模型验收：把 needle（结论句）埋在第 40 分钟，用**真实 DeepSeek**跑
mapreduce 与 tail-first 两种汇总，两种都必须召回 needle。

这条测试与引擎自带的 mock 测试是两条独立证据：
  - mock 测试验的是"管线把尾部内容喂进了汇总器"（机械性质，零网络）
  - 这条验的是"真实模型在给定预算下确实保住了尾部事实"（语义性质，要联网、要花钱）

**需要联网，会用 ~/.dsh/.credentials.yaml 里的 DEEPSEEK_API_KEY。**
没有这个 key 就跳过（退出码 0 并说明跳过原因），不算失败。

用法:
  python verify/real_llm_check.py [--keep]
"""

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
SCRIPT = os.path.join(ROOT, "scripts", "videokit.py")
SEED = os.path.join(HERE, "seed_index.py")
CREDS = os.path.join(os.path.expanduser("~"), ".dsh", ".credentials.yaml")

PY = "python"


def log(msg):
    print(msg, flush=True)


def read_deepseek_key():
    """从 dsh 凭据文件里取 DEEPSEEK_API_KEY（不引 yaml 依赖，凭据文件结构固定）。"""
    try:
        with open(CREDS, "r", encoding="utf-8") as f:
            text = f.read()
    except OSError:
        return None
    m = re.search(r"^\s*DEEPSEEK_API_KEY:\s*(\S+)\s*$", text, re.M)
    return m.group(1) if m else None


def write_config(path, key):
    cfg = {
        "workdir": os.path.join(tempfile.gettempdir(), "videokit-real"),
        "llm": {"base": "https://api.deepseek.com", "key": key,
                "model": "deepseek-chat", "auth": "bearer"},
        "asr": {"base": "", "key": "", "model": "", "auth": "api-key", "chunkMinutes": 15},
        "vision": {"base": "", "key": "", "model": "", "auth": "bearer", "maxFrames": 200},
    }
    with open(path, "w", encoding="utf-8") as f:
        json.dump(cfg, f, ensure_ascii=False, indent=2)


def run(argv, timeout=900):
    log("  $ " + " ".join(argv[:3]) + " ...")
    r = subprocess.run([PY, SCRIPT] + argv, capture_output=True, text=True,
                       encoding="utf-8", errors="replace", timeout=timeout)
    if r.returncode != 0:
        log(r.stdout[-3000:])
        log(r.stderr[-3000:])
        raise AssertionError("命令失败 exit=%d: %s" % (r.returncode, " ".join(argv[:3])))
    return r.stdout


def load_facts(dir_path):
    with open(os.path.join(dir_path, "summary.json"), "r", encoding="utf-8") as f:
        return json.load(f)


def main():
    if sys.platform == "win32":
        try:
            sys.stdout.reconfigure(encoding="utf-8", errors="replace")
        except Exception:
            pass
    ap = argparse.ArgumentParser()
    ap.add_argument("--keep", action="store_true", help="保留临时目录以便检查")
    args = ap.parse_args()

    key = read_deepseek_key()
    if not key:
        log("跳过：%s 里没有 DEEPSEEK_API_KEY" % CREDS)
        return 0

    tmp = tempfile.mkdtemp(prefix="vk-real-")
    cfg = os.path.join(tmp, "config.json")
    idx = os.path.join(tmp, "idx")
    write_config(cfg, key)
    try:
        log("1) 造 45 分钟假索引（needle 在第 40 分钟）")
        subprocess.run([PY, SEED, "--out", idx, "--minutes", "45", "--needle-at-min", "40"],
                       check=True, capture_output=True, text=True, encoding="utf-8",
                       errors="replace")

        results = {}
        for mode in ("mapreduce", "tail-first"):
            log("2) summarize --mode %s （真实 DeepSeek）" % mode)
            out = run(["summarize", idx, "--mode", mode, "--block-chars", "800",
                       "--config", cfg, "--workdir", tmp])
            data = load_facts(idx)
            facts = data.get("facts") or []
            text = "\n".join(facts)
            # 真实模型不会原样保留怪异 token（实测它把 "NEEDLE" 这个词洗掉了，
            # 但结论本身留着）。所以断言只看语义核心：同一行里既有"答案/结论"
            # 又有 "42"。填充文本里没有这两个词、也没有 ASCII 数字，所以这条
            # 断言不会被 [42:30] 这类时间戳污染。
            core = [ln for ln in facts if "42" in ln and ("答案" in ln or "结论" in ln)]
            hit = bool(core)
            at_40 = "[40:00]" in text or "40:00" in text
            results[mode] = {"blocks": data.get("blockCount"), "facts": len(facts),
                             "hit": hit, "core": core}
            log("   块数=%s 事实数=%d 召回结论=%s 带 40:00 时间戳=%s"
                % (data.get("blockCount"), len(facts), hit, at_40))
            for ln in core:
                log("   ★ " + ln[:160])
            log("   facts 前 3 条：")
            for line in facts[:3]:
                log("     " + str(line)[:120])
            log("   summarize stdout 尾部：")
            for line in out.strip().splitlines()[-4:]:
                log("     " + line[:140])
            if not hit:
                log("   !! 尾部结论没被召回，全部 facts：")
                for line in facts:
                    log("     " + str(line)[:200])
            log("   尾部时段是否出现在 facts 里：%s" % at_40)

        log("3) report")
        out = run(["report", idx, "--config", cfg, "--workdir", tmp])
        md_path = os.path.join(idx, "report.md")
        with open(md_path, "r", encoding="utf-8") as f:
            md = f.read()
        for need in ("## 章节时间线", "## 尾部事实清单", "## 关键结论"):
            if need not in md:
                raise AssertionError("report.md 缺少小节: " + need)
        log("   报告 %d 字符，小节齐全" % len(md))
        log("   " + out.strip().splitlines()[-1][:160])

        failures = [m for m, r in results.items() if not r["hit"]]
        if failures:
            raise AssertionError("以下模式未召回尾部 needle: " + ", ".join(failures))
        log("\nREAL-LLM CHECK OK（两种模式都召回了第 40 分钟的结论）")
        return 0
    finally:
        if args.keep:
            log("临时目录保留: " + tmp)
        else:
            shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
