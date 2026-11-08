# dsh-videokit 内部契约（开发文档，非面向用户）

本文件是三个半边之间的接口契约。**任何一方改动都要先改这里**。

组件与写权：

| 文件 | 归属 | 说明 |
|---|---|---|
| `package.json` / `cordis.patch.yml` / `config.js` / `index.js` / `locale/*` / `icon.svg` / `README.md` | Lead | host 半边 + 清单 |
| `scripts/videokit.py` / `test/*` | engine | 全部重活（Python） |
| `client.js` | ui | 设置页「视频理解」栏 |

---

## 1. 配置文件 `~/.dsh/dsh-videokit.json`

Windows 下 `~` = `%USERPROFILE%`。键名与环境变量 `VIDEOKIT_CONFIG` 可覆盖路径。

```json
{
  "pythonPath": "D:/anaconda3/python.exe",
  "ffmpeg": "D:/ffmpeg/bin/ffmpeg.exe",
  "ffprobe": "D:/ffmpeg/bin/ffprobe.exe",
  "workdir": "C:/Users/me/videokit",
  "asr":    { "base": "", "key": "", "model": "", "auth": "api-key", "chunkMinutes": 15 },
  "vision": { "base": "", "key": "", "model": "", "auth": "bearer",  "maxFrames": 200 },
  "llm":    { "base": "", "key": "", "model": "", "auth": "bearer",  "maxTokens": 2048 }
}
```

- `auth` 取值只有 `"api-key"`（请求头 `api-key: <key>`，小米 MiMo）与 `"bearer"`（`Authorization: Bearer <key>`）。
- 三组端点全部按 **OpenAI 兼容** 实现：
  - `base` 规范化：去掉尾部 `/`；如果结尾不是 `/chat/completions` 就拼上 `/chat/completions`。
  - ASR: `POST {base}/chat/completions`，body `messages[0].content = [{"type":"input_audio","input_audio":{"data":"data:audio/mpeg;base64,<b64>"}}]`（MiMo 形状）。
  - Vision: `POST {base}/chat/completions`，content = `[{"type":"image_url","image_url":{"url":"data:image/jpeg;base64,<b64>"}},{"type":"text","text":<prompt>}]`。
  - LLM: `POST {base}/chat/completions`，content 为纯字符串。
- key **明文**存本地（与 dsh 现有凭据文件一致）；任何回传前端的地方必须脱敏。

脱敏规则：key 长度 ≥ 8 时 `key.slice(0,3) + '****' + key.slice(-4)`，否则 `'****'`。

Python 侧只读取 `~/.dsh/dsh-videokit.json`（可用 `--config` 或 `VIDEOKIT_CONFIG` 覆盖），不读环境变量里的 key。

---

## 2. Python 引擎 CLI 契约

入口：`<plugin>/scripts/videokit.py`，由 host 用配置里的 `pythonPath` 执行。
所有命令都接受可选 `--workdir DIR`、`--config FILE`；**路径参数一律绝对路径**（host 侧已 `resolve()`）。

### 通用约定

- stdout = 给人/给模型看的文本（中文），stderr 只在异常时用。
- 退出码：`0` 成功；`1` 致命错误（视频不可读、ffmpeg 缺失、参数错）；`2` 仅"可选依赖缺失且已给出安装提示"（如 yt-dlp 不存在）。
- 每行以 `›` 或 `·` 起头的进度行可自由使用；关键结果行必须**出现在 stdout 里**（模型要读）。
- Windows 下必须 `sys.stdout.reconfigure(encoding="utf-8", errors="replace")`。
- 控制台进度行不要刷屏：单条命令超过 200 行输出时自行截断到末尾。

### 2.1 `probe <video> [--out DIR]`

- `<video>` 可以是本地路径或 `http(s)://` 链接。是链接时先下载到 `<workdir>/cache/<sha1[:12]>/` 再 probe。
- 输出目录 `DIR` 默认 `<workdir>/<视频文件名 stem>`。
- 写 `DIR/probe.json`，字段：`{path, sizeBytes, durationSec, width, height, fps, bitrateKbps, videoCodec, audioCodec, audioChannels, audioSampleRate, chapters: [{t0,t1,title}], hasAudio}`
- stdout 必含这些行（模型要读）：
  ```
  文件: <绝对路径>
  时长: 00:45:12 (2712.0 s)
  画面: 1920x1080 @ 29.97 fps, h264
  音频: aac 2ch 44100Hz
  章节: 3
  工作目录: <DIR 绝对路径>
  ```

### 2.2 `download <url> --out DIR [--p N] [--all-pages]`

- B 站链接（`bilibili.com` / `b23.tv`）：`x/web-interface/view` 取 **pages** →
  `x/player/playurl?fnval=16` 取 dash 音视频流 → 下载 → ffmpeg `-c copy` 合流。
  UA 与 Referer 必须带（B站对缺 UA 的请求直接 403）。
- **分 P 必须走 pages，不能用 `data.cid`**：`data.cid` 是 P1 的 cid，直接拿它下载会让
  61 个分 P 的视频**只下到第一个**，而且没有任何报错。
  - 单 P 视频：保存为 `<DIR>/<bvid>.mp4`（向后兼容）。
  - 多 P 视频：保存为 `<DIR>/<bvid>_p<NN>.mp4`。
  - `--p N` 只下一个分 P；不给则取 URL 里的 `?p=`，再不给就是 P1。
  - `--all-pages` 下载全部分 P，**已存在且非空的直接跳过**（断点续传）。
- 每次下完一个分 P 就原子写一次 `<DIR>/manifest.json`：
  `{bvid, total, pages:[{p,cid,duration,title,path,sizeBytes}]}`。中断重跑靠它续上。
- 其他站点：`yt-dlp`（`shutil.which('yt-dlp')` 或配置 `ytdlp` 字段）存在才用；不存在则 stderr/stdout 输出明确中文安装提示（`pip install -U yt-dlp`）并退出码 2。
- 成功时：单个分 P 输出 `已下载: <绝对路径> (<MB>)`；多个分 P 输出
  `已下载 N 个分 P，共 <MB> → <DIR>` + `清单: <manifest>`，有失败则退出码 1。

### 2.3 `index <video> --out DIR [--density 1.0] [--tail-ratio 0.2] [--max-frames 200] [--vision auto|none] [--asr auto|none] [--resume]`

重活。产物目录：

```
DIR/
├── probe.json
├── audio/part_000.mp3 ...            # 16kHz 单声道 64kbps
├── asr/part_000.json                 # {index,t0,t1,text,segments?,error?,model}
├── frames/f000_t0000.0.jpg           # ≤960 宽，jpeg
├── frames/f000_t0000.0.json          # {t,path,text,scene:float|none,error?,model,srcSize,srcMtime}
├── timeline.json
├── chapters.json
├── index.json
└── index.log
```

**音频分段**（纯函数，测试要直接 import）：
```python
def plan_chunks(duration_sec, chunk_seconds):   # -> [(t0,t1), ...]  覆盖到 duration，最后一段不短于 5s（合并进上一段）
def estimate_b64_mb(seconds, kbps=64):          # -> float, base64 后大小估算
```
默认 `chunkMinutes=5`（300 s）。若 `estimate_b64_mb(chunk_seconds) > 9.5`，自动把 chunk_seconds 减半直到达标（下限 120 s）。

**ASR 输出塌缩检测（必须有）**：长音频上模型会"悄悄跳读" —— 返回成功、
`finish_reason=stop`、没有错误码，但正文只覆盖开头和结尾，中间整段丢失。
实测 MiMo 对同一段口播音频（起点都是 0）：

| 段长 | 字数 | 字/秒 | 判定 |
|---|---|---|---|
| 300 s | 1529 | 5.10 | ✓ 正常 |
| 450 s | 2255 | 5.01 | ✓ 正常 |
| 572 s | 1229 | 2.15 | △ 轻度省略 |
| 600 s | 146 | 0.24 | ✗ 塌缩 |
| 750 s | 1238 | 1.65 | ✗ 塌缩 |
| 900 s | 371 | 0.41 | ✗ 塌缩 |

所以每段转写后算 `len(text) / (t1 - t0)`，**低于 `MIN_CHARS_PER_SEC = 3.0` 或高于
`MAX_CHARS_PER_SEC = 8.0`** 就**把音频对半切开重转**（递归，深度上限 2、段长下限 150 s），
两半的结果按顺序拼回。切完仍不合格就保留原结果，不让重试把结果变差。
阈值取 3.0 是因为正常口播在 4~6 字/秒；误判只多花一次调用，漏判则整段内容凭空消失。

**反向（膨胀）也必须卡**：实测同一段 300 s 数学课音频，整段回 3195 字（10.6 字/秒），
切成两个 150 s 分别转只有 902 + 862 = 1764 字 —— 那次是**复读/扩写**，凭空多出内容。
所以上界 8.0 字/秒同样要触发对半重转。两个方向都会污染时间线和摘要。

**画面候选**（纯函数，测试要直接 import）：
```python
def elect_frames(candidates, max_frames, duration_sec, tail_ratio):
    # candidates: [{'t': float, 'score': float, 'tail': bool}]  (score=场景分，uniform 帧 score=0)
    # 返回按 t 升序的候选列表
```
选举规则（必须实现一致）：
1. 永远保留首帧（t≈0）与尾帧（t≈duration）。
2. 场景帧按 `score` 降序优先，其次尾部帧，最后中段均匀帧。
3. 总数不超过 `max_frames`。

**抽帧来源**：
- 场景帧：`ffmpeg -i v -vf "select='gt(scene,0.3)',showinfo,metadata=print,scale=960:-2" -vsync vfr tmp/scene_%05d.jpg`。
  时间戳只从 `Parsed_showinfo` 行取 `pts_time:`（`metadata=print` 也会打 `pts_time`，别混用）；
  场景分从 `lavfi.scene_score` 行按顺序取，与帧序号一一对应。
- 均匀帧：基准间隔 `uniform = 120 / density` 秒（density 1.0 → 每 2 分钟一帧）。落在末尾 `tail_ratio` 区间的均匀帧间隔减半（×2 密度）。
- 抽取：对入选时间点 `ffmpeg -ss <t> -i <video> -frames:v 1 -vf scale=960:-2 -q:v 3 <out>.jpg`（`-ss` 放 `-i` 前，快）。
  若 `t` 落在最后一帧之后抽不出文件，把 `-ss` 往回退 0.1 s 重试。
- 文件名：`f{序号:03d}_t{t:06.1f}.jpg`，`t` 保留 1 位小数、左补零到 6 字符（例如 `f003_t0053.2.jpg`、`f000_t0000.0.jpg`）。

**VLM 描述 prompt**（固定，改这里就改契约）：
```
你在看一段视频里的某一帧。请严格按下面两行输出，不要多余客套：
画面：<这一帧发生了什么，主体、动作、场景，≤120字>
文字：<逐字抄录画面上所有可见文字（界面、标题、字幕、数字、弹幕）；确实没有就写 无文字>
```
- 并发 4，每帧最多重试 2 次（指数退避 1s/3s），单次超时 120 s。
- 失败帧写 `error` 字段，不中断整体。
- 复用：`asr/*.json` 与 `frames/*.json` 已存在且（帧的 `srcSize`/`srcMtime` 未变）时跳过，除非 `--resume` 未给则重算？ —— **统一：`--resume` 给了才跳过；不给就覆盖重算**。

**ASR 文本 → 事件**：优先用响应里的 `segments`（`[{start,end,text}]`，单位秒，相对本段）；没有就按标点把整段文本切句，并在 `[t0,t1]` 内按字符数比例分配时间戳，标记 `estimated: true`。

**`timeline.json`**：
```json
{ "version": 1, "video": "<abs>", "durationSec": 2712.0, "events": [
  { "t0": 12.5, "t1": 21.0, "kind": "speech", "text": "……", "source": "asr", "estimated": false },
  { "t0": 53.2, "t1": 53.2, "kind": "scene", "text": "……", "source": "vlm", "frame": "frames/f003_t0053.2.jpg" },
  { "t0": 53.2, "t1": 53.2, "kind": "ocr",   "text": "……", "source": "vlm", "frame": "frames/f003_t0053.2.jpg" }
], "stats": { "speech": 120, "scene": 40, "ocr": 32 } }
```
`events` 按 `t0` 升序；`ocr` 事件仅在"文字："非空且不是"无文字"时产生。

**`chapters.json`**：`[{index,t0,t1,title,summary}]`，`index` **1 起始**。
- 切分：语音事件之间静音间隔 ≥ 20 s 处断开；单章最长 300 s 强制切断；`t0`/`t1` 夹到 `[0, duration]`。
- `title`：该章第一条 speech 文本去掉首尾标点，取前 24 字；超过 24 字时**在 12 字之后的最后一个
  句读处收尾**（硬切会切出"人强烈的表达欲与倾"这种半截标题）。没有 speech 就用该章第一条 scene
  文本，再没有就 `"画面片段 N"`。
- `summary`：该章前 2 条事件文本拼接，截断 160 字。

**`index.json`**：`{video, durationSec, workdir, probe, audio:{parts,chunkSeconds,coveredSec}, asr:{ok,failed,model,parts:[{i,t0,t1,chars,error?}]}, vision:{frames,described,failed,model}, timeline:{events,stats}, chapters:{count}, startedAt, finishedAt}`

**`--vision none`**：只抽帧、不写 `frames/*.json` 的 `text`（可以只写 `{"t":..,"path":..,"skipped":true}`），`timeline.json` 里没有 scene/ocr 事件。
**`--asr none`**：不调 ASR，`timeline.json` 里没有 speech 事件。

**端点未配置时**：`--asr auto`（默认）遇到 `asr.base` 或 `asr.key` 为空 → 跳过 ASR 并在 stdout 打
`· 语音识别未配置，已跳过（设置 → 视频理解 → 语音识别）`，**不报错退出**。vision 同理。

stdout 结束时必须打印（模型要读）：
```
索引完成: <DIR>
时长: 00:45:12 | 语音 120 条 / 画面 40 帧 / 文字 32 条 | 章节 9
时间线: <DIR>/timeline.json
```

### 2.4 `timeline <DIR> [--from S] [--to S] [--grep TEXT] [--tail N] [--kind speech|scene|ocr] [--limit N]`

- `S` 支持 `90`、`1:30`、`01:02:03` 三种写法。
- 输出行：`[mm:ss] 🗣 正文` / `[mm:ss] 🎬 正文` / `[mm:ss] 🔤 正文`（`mm:ss` 超过 1 小时用 `h:mm:ss`）。
- 头部一行按**全量**计数（不是过滤后的），只在输出被裁剪时才追加本次条数，避免 `--tail 6` 让人误判视频只有 6 条：
  `时间线 <DIR> · 共 N 条（语音 x / 画面 y / 文字 z）[ · 本次输出 M 条]`
- 默认 `--limit 200`，截断时尾部提示 `…（本次匹配 N 条，只显示前 M 条，用 --limit 调整）`。
- `--tail N` 表示**只取时间线最后 N 条**（与 `--from/--to` 互斥时以 `--tail` 为准）。

### 2.5 `frames <video> --out DIR --from S --to S [--fps N]`

- 定点高密度抽帧，**不调 VLM**。`--fps` 默认 1.0（间隔 `1/fps` 秒）。
- 文件名 `t{t:06.1f}.jpg`（撞名时加 `_2` 后缀）；写 `DIR/frames.json` = `{video, from, to, fps, count, files:[{t,path}]}`。
- stdout：`抽帧完成: N 张 → <DIR>` + 前 10 个绝对路径 + `清单: <DIR>/frames.json`。

### 2.6 `summarize <DIR> [--mode mapreduce|tail-first] [--question Q] [--block-chars 6000] [--parallel 4]`

- 用 `timeline.json` 的 speech/scene 事件拼块；每块 ≤ `block-chars` 字符（按事件边界切，不切断单条）。
- **画面描述在汇总阶段要截断并打标**：`scene` 事件只取前 `SCENE_SUMMARY_CHARS = 80` 字，行前缀加
  `（画面）`；`speech` 用全文、不加前缀。原因：一帧描述 100~200 字，120 帧就是 17k 字，
  是转写正文（7.6k 字）的两倍多 —— 不截断的话块预算被美术描述吃掉，模型抽出来的全是
  "一名穿红衣的少年站在雪地里"，旁白的论证反而进不去。
- map：每块一次 LLM 调用，system 固定为
  `你是视频内容的结构化抽取器。只输出事实，不写评论、不写总结性套话。`
  user：
  ```
  下面是一段视频的第 <i>/<n> 块内容，每行以 [时间] 开头。
  <块正文>

  请抽 3-5 条事实，每条一行，必须以 [mm:ss] 开头，只写素材里出现的内容。
  取材优先级：旁白讲的观点、论证、结论 > 屏幕上出现的文字 > 画面。带（画面）标记的行只在它承载信息（字幕、图表、标题、明显的叙事画面）时才采用，不要逐帧做美术描述。
  ```
  （有 `--question` 时在末尾追加 `只关注与这个问题相关的内容：<question>`）
- reduce：一次 LLM 调用，system
  `你是去重合并器。只做去重与按时间排序，禁止删除、禁止压缩、禁止改写事实。`
  user：`以下是同一段视频各块抽出的全部事实，请去掉完全重复的条目并按时间排序后原样输出。\n<全部事实>`
- `tail-first`：reduce 输入把**尾部块的事实放最前面**（map 的调用顺序不变）。
- 输出 `DIR/summary.json`：`{mode, question, blockCount, blocks:[{i,t0,t1,chars,facts:[...]}], facts:[...], reduce:{text,error?}, model, createdAt}`；
  另写 `DIR/summary.md`（`## 关键结论` + 事实列表）。stdout 打印 `facts` 全部内容 + `摘要: <DIR>/summary.md`。
- `llm.base`/`llm.key` 为空 → 退出码 1，stdout：
  `LLM 端点未配置。请在 设置 → 视频理解 → 文本汇总 里填写接口地址、API key、模型名。`

### 2.7 `report <DIR> [--out FILE]`

写 markdown（默认 `DIR/report.md`），小节顺序：
1. `# <视频文件名> 视频理解报告`
2. `## 概要` —— 时长、分辨率、语音条数/画面帧数/章节数、若 `summary.json` 存在则列出前 10 条 facts
3. `## 基本信息` —— 表格
4. `## 章节时间线` —— `| # | 时间 | 标题 | 摘要 |`
5. `## 关键结论（来自 map-reduce）` —— `summary.json.facts` 全量；没有则写 `（未运行 summarize）`
6. `## 尾部事实清单` —— timeline 最后 10% 时间段的 speech/scene 事件原文
7. `## 关键帧` —— 前 30 个 `frames/*.jpg` 的**绝对路径**列表
8. `## 端点与统计` —— asr/vision/llm 的 base+model（**不含 key**）、各阶段失败数
stdout：`报告已生成: <绝对路径>`

### 2.8 `index-series <DIR> [--max-frames 60] [--tail-ratio 0.2] [--density 1.0] [--vision auto|none] [--asr auto|none] [--only 1,2,61] [--resume]`

**30 小时的课不是"一个视频"，是 61 个视频。** 不要让一个 index 目录吞下 30 小时：
时间线会长到读不动，`maxFrames` 摊到 30 小时等于每 9 分钟一帧（画面信息全丢），
任何一步失败都要从头再来。

- 逐个分 P 调用 `index`，每集一套独立产物：`<DIR>/<label>/`（label 来自 manifest，
  如 `BV1QeNc6iE84_p01`）。
- `--max-frames` 是**每个 P 的上限**，默认 60（不是整系列的总数）。
- 单集失败只记一笔，不中断整轮；`--resume` 时若 `<DIR>/<label>/index.json` 存在就跳过
  （index.json 只在 `index` 跑完时写，所以它的存在就等于"这集完成了"）。
- `--only` 只跑指定分 P，逗号分隔。
- stdout 结尾：`系列索引完成：新增 N / 跳过 N / 失败 N（共 M 个分 P）`；有失败退出码 1，
  并列出失败的分 P 号（重跑同一条命令即可续上）。

### 2.9 `series-report <DIR> [--out FILE] [--summarize]`

把各分 P 的索引聚合成系列报告（`<DIR>/series.md` + `<DIR>/series.json`）：
总览（分 P 数 / 已索引数 / 总时长 / 语音与章节合计 / 未索引的分 P）、
课程大纲（`--summarize` 时逐集 LLM 摘要再加一次归纳）、分 P 一览表（P / 时长 / 章节数 /
标题 / 摘要）。

- 逐集摘要**必须缓存**到 `<DIR>/.series-cache/<label>.json`：30 小时课程是 61 次 LLM 调用，
  重跑不能重花。
- 没有 `manifest.json` 时退化为扫目录里的 mp4（用 `_pNN` 后缀推分 P 号）。
- 一个分 P 都没索引 → 退出码 1，提示先跑 `index-series`。
- `--summarize` 但 LLM 未配置 → 退出码 1（只出统计表就不需要 `--summarize`）。

### 2.10 `selftest`

纯本地断言，不联网：
1. `plan_chunks(90*60, 900)` 返回 6 段。
2. 每段 `estimate_b64_mb` < 10。
3. `elect_frames` 对 300 个候选 + `max_frames=200` 返回 200 条且含首尾帧。
4. `parse_time('1:30')==90`、`parse_time('01:02:03')==3723`。
失败退出码 1，成功打印 `selftest ok`。

---

## 3. host 半边的九个工具 → CLI 映射

host 用 `execFile(python, [script, ...argv], {timeout, maxBuffer: 32MB, cwd: workdir})`，
把 `{code, stdout, stderr, killed}` 用 `fmtRun()` 拼成一段文本返回（与 `dsh-swfkit/index.js` 同款）。

| 工具 | 参数 | argv |
|---|---|---|
| `video_probe` | `video` | `probe <video> --out <workdir>/<stem>` |
| `video_download` | `url`, `out?`, `p?`, `allPages?` | `download <url> --out <out or workdir/downloads> [--p N] [--all-pages]` |
| `video_index` | `video, out?, density?, tailRatio?, maxFrames?, vision?, asr?, resume?` | `index <video> --out <out or workdir/<stem>> [--density ..] [--tail-ratio ..] [--max-frames ..] [--vision none] [--asr none] [--resume]` |
| `video_index_series` | `dir, maxFrames?, tailRatio?, density?, vision?, asr?, only?, resume?` | `index-series <dir> [--max-frames 60] ... [--only 1,2,61] [--resume]` |
| `video_timeline` | `dir, from?, to?, grep?, tail?, kind?, limit?` | `timeline <dir> [...]` |
| `video_frames` | `video, out, from, to, fps?` | `frames <video> --out <out> --from .. --to .. [--fps ..]` |
| `video_summarize` | `dir, mode?, question?` | `summarize <dir> [--mode ..] [--question ..]` |
| `video_report` | `dir, out?` | `report <dir> [--out ..]` |
| `video_series_report` | `dir, out?, summarize?` | `series-report <dir> [--out ..] [--summarize]` |

host 另外会把配置里的 `--workdir`、`--config` 传给 python（`--workdir <workdir>` 仅在 CLI 未显式带 `--out` 时需要）。

---

## 4. `/videokit-api` HTTP 契约（host 提供，client 消费）

可选注入 `webServer`。`kind:'exact'`，path `/videokit-api`。

### GET

```json
{
  "ok": true,
  "config": { "...": "全部字段，key 已脱敏" },
  "defaults": { "...": "默认值，同结构（key 为空）" },
  "detected": { "python": "<path|null>", "ffmpeg": "<path|null>", "ffprobe": "<path|null>", "ytdlp": "<path|null>" }
}
```

### POST（JSON）

```json
{ "op": "save", "patch": { "workdir": "...", "asr": { "base": "...", "key": "...", "model": "...", "auth": "api-key" } } }
→ { "ok": true, "config": { ... 脱敏 ... } }
```
- `patch` 里 `key` 等于当前脱敏串（或为空串）时**保持原 key 不变**。
- `patch.<section>.key === "__clear__"` 时**清空**该组的 key（设置页「清除」按钮用它）。
- 只覆盖 patch 里出现的字段（深合并一层：顶层字段直接覆盖，`asr/vision/llm` 内部按字段合并）。
  所以 patch 里不带 `vision.maxFrames` / `llm.maxTokens` 时，这两项原样保留，不会被重置。

```json
{ "op": "clear", "section": "asr" }
→ { "ok": true, "config": { ... 脱敏 ... } }
```
`op:'clear'` 是「清空某一组 key」的便捷入口，等价于 `save` 带 `{asr:{key:'__clear__'}}`。

```json
{ "op": "test", "section": "asr|vision|llm", "draft": { "base": "...", "key": "...", "model": "...", "auth": "..." } }
→ { "ok": true, "latencyMs": 812, "model": "mimo-v2.5-asr", "error": null }
```
- `draft` 缺省时用已保存配置；`draft.key` 为空或是脱敏串时用已保存 key。
- 测试请求（最小代价）：
  - **asr**：构造 0.5 s 全零 16 kHz 单声道 16-bit PCM WAV（44 字节头 + 16000 字节数据），base64 后按 §1 的 `input_audio` 形状发一次。
  - **vision**：发一张 16×16 纯灰 PNG（data URL），prompt `"用两个字回答：灰或彩。"`。
  - **llm**：`messages=[{role:'user',content:'hi'}]`，`max_tokens:1`。
- 失败返回 `{ok:false, latencyMs, model, error:"HTTP 401 — <响应体前 300 字>"}`；HTTP 状态码仍为 200（前端只看 `ok`）。

```json
{ "op": "detect" }
→ { "ok": true, "detected": { ... } }
```
探测顺序：`pythonPath` 配置 → `python` / `python3` / `py` on PATH → 已知 conda 路径；ffmpeg/ffprobe 同理（配置 → `VIDEOKIT_FFMPEG`/`VIDEOKIT_FFPROBE` → PATH → 常见安装位置）。

---

## 5. client 半边（设置页）

- 文件：`client.js`，`window.__ModuleLoader__.load({ id: 'dsh-videokit', factory })`。
- `factory(require)` 里 `const React = require('react')`，用 `React.createElement`（不要 JSX，不要构建步骤）。
- 返回 `{ inject: ['slots'], apply(ctx) { ... } }`。
- `apply` 内：
  ```js
  ctx.slots.inject('settings.section', () => ctx.slots.register(
    { name: 'settings.section', id: 'videokit', order: 26, label: '视频理解' },
    () => React.createElement(VideoKitPanel),
  ))
  ```
- 面板组件无 props 依赖（可接收 `{close}` 忽略）。
- 数据全部走 `fetch('/videokit-api')`。
- 样式：注入一个 `<style>`（在 `apply` 里 `document.head.appendChild`，并用 `ctx.effect(() => () => el.remove())` 清理），CSS 类名前缀 `vk-`，颜色只用 `var(--dsw-alias-*)` 变量。

界面结构（必须四张卡片）：

1. **语音识别**（`asr`）：接口地址 / API Key（`type=password`，已保存时 placeholder 显示脱敏串）/ 模型名 / 认证方式下拉（`api-key`｜`Bearer`）/ 每段分钟数 —— 按钮「测试连接」「保存」
2. **画面理解**（`vision`）：接口地址 / API Key / 模型名 / 认证方式下拉 —— 「测试连接」「保存」
3. **文本汇总**（`llm`）：接口地址 / API Key / 模型名 / 认证方式下拉 —— 「测试连接」「保存」
4. **全局**：python 路径 / ffmpeg 路径 / ffprobe 路径 / 工作目录 + 「自动探测」+「保存」

每张卡片测试结果就地显示：成功 `✓ 812ms · mimo-v2.5-asr`，失败 `✗ <error>`（红色 `var(--dsw-alias-status-danger)`，若该变量不存在就退化成 `#d9534f`）。
