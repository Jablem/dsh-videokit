# dsh-videokit —— 长视频理解插件

给 [DeepSeek Harness](https://github.com/deepseek-ai/deepseek-harness)（dsh）用的插件：
把半小时以上的视频**从头读到尾**，而不是均匀抽几帧猜内容。

老路子（每 N 秒抽一帧 + 只做 ASR + 链式 refine 摘要）在长视频上系统性失效，
失效点是两个：**预算按时间等分**（结尾的内容被稀释）和**链式摘要对末尾的衰减**。
这个插件换三件事：

1. **音频全长无损覆盖**：整条音轨切成 **5 分钟**一段，逐段送用户的 ASR 端点，
   每段保留起止时间 —— 这是时间线的骨架，不是摘要的原料。
   每段转写完会算"字数 / 音频时长"，出界就**把音频对半切开重转**
   （见下文「两个必须自己防的坑」）。
2. **画面按需加密**：ffmpeg 场景切分取代表帧，再按 `density` 补均匀帧，
   **末尾 `tailRatio` 区间密度 ×2**（结论通常在最后），总帧数受 `maxFrames` 硬约束。
3. **结构化时间线 + map-reduce**：产物是 `timeline.json`（事件数组，带 t0/t1/kind/source），
   汇总时每块独立抽事实，归并阶段**只去重排序、不二次压缩**，尾部再单独给一块预算。
   画面描述在汇总阶段只取前 80 字当佐证（否则 120 帧的美术描述会把旁白的论证挤出块预算）。

模型全部由你配：语音识别、画面理解、文本汇总三组端点，各自填接口地址 / API Key / 模型名 /
认证方式。插件里没有写死任何厂商。

## 安装

```bash
git clone https://github.com/Jablem/dsh-videokit.git
dsh plugin --profile <你的 profile> add ./dsh-videokit
```

也可以把上面的 `./dsh-videokit` 换成绝对路径。插件管理器里的「本地目录安装」等价：
加一条 `link:` 依赖 + 往 `dsh.profile.bundles` 里加一行。

装完工具立即可用；**设置页要刷新一次页面**才有「视频理解」这一栏。
依赖只有 `ffmpeg` / `ffprobe`（必须）和 `yt-dlp`（只在下载 YouTube 等站点时需要）。

## 配置

设置 → **视频理解**。四张卡片：

| 卡片 | 填什么 | 默认占位 |
|---|---|---|
| 语音识别 | OpenAI 兼容的**音频对话**端点（`input_audio` 块） | `https://api.xiaomimimo.com/v1` / `mimo-v2.5-asr` / auth=`api-key` |
| 画面理解 | 视觉模型端点（`image_url` 块） | `https://open.bigmodel.cn/api/paas/v4` / `glm-4.1v-thinking-flash` / auth=`Bearer` |
| 文本汇总 | 文本模型端点 | `https://api.deepseek.com` / `deepseek-chat` / auth=`Bearer` |
| 全局 | python / ffmpeg / ffprobe 路径、工作目录 | 留空则自动探测 |

每张卡片有「测试连接」：真的发一次最小请求（ASR 发 0.5 秒静音 wav、视觉发 16×16 纯灰 PNG、
文本发 `hi`），显示 `✓ 812 ms · mimo-v2.5-asr` 或红色的错误原因。

配置落在 `~/.dsh/dsh-videokit.json`，**key 明文存本地**（与 dsh 自己的凭据文件一致），
回传前端时一律脱敏成 `sk-****abcd`。改完配置不用重启 dsh —— 每次工具调用实时读盘。

路径探测优先级：设置页配置 > 环境变量（`VIDEOKIT_PYTHON` / `VIDEOKIT_FFMPEG` /
`VIDEOKIT_FFPROBE` / `VIDEOKIT_CONFIG`）> PATH > 常见安装位置。

## 九个工具

| 工具 | 用途 |
|---|---|
| `video_probe` | 时长 / 分辨率 / fps / 音轨 / 章节，写 `probe.json`。支持本地路径与 http(s) 链接 |
| `video_download` | B站走 playurl 取 dash 流再 ffmpeg 合流；YouTube 等走 yt-dlp（没装会明确提示）。分 P 视频默认只下一个 P，`allPages=true` 下全部 |
| `video_index` | 重活：音频分段转写 + 场景/尾部加密抽帧 + VLM 描述 → `timeline.json` / `chapters.json` |
| `video_index_series` | 逐分 P 建索引（30 小时课程用这个，别用 `video_index` 吞成一坨） |
| `video_timeline` | 紧凑读时间线，每行 `[mm:ss] 正文`；`tail=40` 直接看结尾 |
| `video_frames` | 在某区间按 fps 定点加密抽帧（不调模型），返回图片绝对路径给主模型自己读图 |
| `video_summarize` | `mapreduce` / `tail-first` 两种汇总 |
| `video_report` | 出单集 markdown 报告 |
| `video_series_report` | 把各分 P 的索引聚合成系列报告（总览 + 课程大纲 + 分 P 一览） |

典型用法：

```
video_probe   → 看清时长再决定密度
video_index   → density=1.0 tailRatio=0.25 maxFrames=200
video_timeline tail=40            # 先看结尾，不要从头扫
video_frames  from=39:00 to=41:00 fps=1
video_summarize mode=tail-first   # 问"结论是什么"时用这个
video_report
```

### 分 P / 30 小时课程

一个 61 集、30 小时的课程**不要**当成一个视频喂进去：时间线会长到读不动，
`maxFrames` 摊到 30 小时等于每 9 分钟一帧（画面信息全丢），中途失败还要从头再来。
按 P 建索引，系列级只做聚合：

```
video_download      url=... allPages=true out=~/videokit/series      # 可断点续传
video_index_series  dir=~/videokit/series resume=true maxFrames=60   # 每集一套独立产物
video_series_report dir=~/videokit/series summarize=true             # 总览 + 课程大纲
```

- 只挑几集看：`video_index_series only="1,2,61"`，或直接 `video_download p=7` + `video_index`。
- 单集还是用 `video_timeline` / `video_summarize` / `video_report`，跟单视频一模一样。
- `--resume` 是可续的：已完成的分 P（有 `index.json`）自动跳过，失败的分 P 会列在结尾。
- 粗略成本（30 小时 / 61 集）：下载约 2~7 GB（取决于 h264 / hevc）；ASR 约 370~400 次调用；
  视觉按每集 60 帧是 3660 次调用 —— **视觉这一项是大头**，`maxFrames` 降到 30 能砍一半。
  全部跑完大约 4~8 小时，取决于视觉端点延迟；先跑 `only="1"` 试水再放全量。

## 两个必须自己防的坑

这两个都不是"用错 API"，而是**厂商会静默给你错的东西**，没有报错码可以抓。
插件已经内置了检测，但如果你换成别的模型，值得知道原理。

### 1. ASR 长音频会双向静默出错

MiMo `mimo-v2.5-asr` 对同一段 16k 单声道口播音频（起点都是 0，内容本应单调增长）：

| 段长 | 返回字数 | 字/秒 | |
|---|---|---|---|
| 300 s | 1529 | 5.10 | 正常 |
| 450 s | 2255 | 5.01 | 正常 |
| 572 s | 1229 | 2.15 | 轻度省略 |
| 600 s | 146 | 0.24 | 塌缩 |
| 750 s | 1238 | 1.65 | 塌缩 |
| 900 s | 371 | 0.41 | 塌缩，结尾直接跳到片尾台词 |

900 s 的 tail 与"600~900 s 单独转写"的 tail 完全一致 —— 跳过中间、直接读到结尾。
`finish_reason` 仍是 `stop`，`audio_tokens` 显示音频确实完整上传。

反过来也会发生：同一段 300 s 数学课音频整段回 3195 字（10.6 字/秒），
切成两个 150 s 分别转只有 902 + 862 = **1764 字** —— 那次是**复读/扩写**。

**判据只有一个：字数 / 音频时长。** 正常中文口播 4~6 字/秒，本插件取
`[3.0, 8.0]` 区间，出界就把音频对半切开重转（递归，深度上限 2、段长下限 150 s）。
实测一段 24.5 分钟视频总字数 4740 → 7588、一段 34.7 分钟课程里同时抓到一次膨胀
和一次跳读，修完各段稳定在 4.9~5.9 字/秒。

### 2. B站分 P 视频不能只看 `data.cid`

`x/web-interface/view` 返回的 `data.cid` 是 **P1 的 cid**。直接拿它下载，
61 集的视频会安安静静只下第一集、**没有任何报错**。必须读 `pages` 全清单
（本插件已按这个实现，见 `download --p N` / `--all-pages`）。

## 产物目录

```
<workdir>/<视频名>/
├── probe.json          # 时长/流信息/章节
├── audio/part_000.mp3  # 16kHz 单声道 64kbps，5 分钟一段
├── asr/part_000.json   # {t0,t1,text,segments?,splits?,error?}
├── frames/             # 关键帧 jpg + 同名 .json（VLM 描述）
├── timeline.json       # 事件流：speech / scene / ocr
├── chapters.json       # 按静音段 + 场景切换聚类的章节
├── index.json          # 各阶段统计
├── summary.json        # summarize 产物
└── report.md           # report 产物
```

## 命令行（不经过 dsh 也能用）

```bash
python scripts/videokit.py probe  <video> [--out DIR]
python scripts/videokit.py download <url> --out DIR [--p 3] [--all-pages]
python scripts/videokit.py index  <video> --out DIR [--density 1.0] [--tail-ratio 0.2] \
                                  [--max-frames 200] [--vision auto|none] [--asr auto|none] [--resume]
python scripts/videokit.py index-series <DIR> [--max-frames 60] [--only 1,2,61] [--resume]
python scripts/videokit.py series-report <DIR> [--out series.md] [--summarize]
python scripts/videokit.py timeline DIR [--from 1:30] [--to 5:00] [--grep 关键词] [--tail 40] [--kind speech]
python scripts/videokit.py frames <video> --out DIR --from 39:00 --to 41:00 [--fps 1]
python scripts/videokit.py summarize DIR [--mode mapreduce|tail-first] [--question "..."]
python scripts/videokit.py report DIR [--out report.md]
python scripts/videokit.py selftest
```

所有子命令都接受 `--config <json>` 与 `--workdir <dir>`。

## 测试

大部分测试是**零网络**的：真实端点由本地 mock 顶替。

| 命令 | 验什么 | 要什么 |
|---|---|---|
| `python test/selftest.py` | 分块/选举/时间解析/配置退化的纯函数断言 | 无 |
| `python test/make_fixture.py` | 用 ffmpeg testsrc 生成 90 秒假视频 | ffmpeg |
| `python test/e2e_mock_test.py` | 全链路（probe→index→timeline→frames→summarize→report） | ffmpeg |
| `python test/needle_test.py` | 结论埋在第 40 分钟，两种 mode 的归并都必须召回 | 无 |
| `node verify/apply_check.mjs` | host 半边装配（9 个工具、参数、execute→render 往返、配置脱敏往返） | 无 |
| `node dev/render_check.mjs` | 设置页组件的离线渲染与交互（伪 window + SSR） | 一对版本匹配的 react/react-dom |
| `python verify/real_llm_check.py` | **真实** DeepSeek：第 40 分钟的结论两种模式都要召回 | 网络 + `~/.dsh/.credentials.yaml` 里的 `DEEPSEEK_API_KEY` |

ffmpeg 不在 PATH 上时用环境变量指过去：

```bash
VIDEOKIT_FFMPEG=/usr/bin/ffmpeg VIDEOKIT_FFPROBE=/usr/bin/ffprobe python test/e2e_mock_test.py
```

`dev/render_check.mjs` 需要一对**版本配对**的 react / react-dom（版本不一致时 SSR 会抛
`Objects are not valid as a React child`）。它会依次尝试插件的 `node_modules`、上一级目录的
`node_modules`，都不行时用 `VIDEOKIT_REACT_DIR=/path/to/node_modules node dev/render_check.mjs`。

CI 跑前两组（静态检查 + 引擎全链路 mock），见 `.github/workflows/ci.yml`。

## 已知坑

- **改 `client.js` 要刷新页面**（client 半边按模块图加载，刷新才拿到新代码）。
  **改 `index.js` / `config.js` 不必重启整个 dsh**：插件管理里把 `dsh-videokit` 关掉再打开
  就会加载新的模块代际。实测过：不重开的话 host 仍跑旧的模块缓存，工具会报
  `output.render returned non-lossless JSON` 这类"代码明明改了却没生效"的怪错。
- **改 `scripts/videokit.py` 连插件都不用重开** —— 每次工具调用都重新起进程执行。
- `defineTool.parameters` 是 dsh 自家的 DSL（`{type, required, description}`），不是 JSON Schema。
  `output.schema: {type:'json'}` 编译后是注解式的空 schema，所以 **`execute` 的返回值必须是
  lossless JSON，且 `render` 读的字段必须真的存在** —— 返回裸字符串而 render 读 `v.text`，
  会得到 `output.render returned non-lossless JSON`，整个工具不可用。`verify/apply_check.mjs`
  里的「execute → render 往返」就是钉这条的。
- 插件进程的 cwd 不是会话 workspace。参数表面支持相对路径，但**一律给绝对路径**；
  相对路径按配置的 `workdir` 解析，不是按你的 shell 目录。`workdir` 不存在时插件会先创建
  （否则 `execFile` 的 cwd 不存在会得到"exit 1 且完全无输出"的哑失败）。
- 依赖 `webServer` 必须用 `ctx.inject(['webServer'], ...)` 可选注入：
  属性访问会抛「cannot get property without inject」，写进顶层 `inject` 会变成必需依赖，
  非 web 模式下阻塞整个插件。
- ASR 的认证头：MiMo 是 `api-key: <key>`，GLM / DeepSeek 是 `Authorization: Bearer <key>`。
  设置页的认证方式下拉就是为此存在。
- ASR 的请求形状按 MiMo 的 `input_audio`（`data:audio/mpeg;base64,...`）实现；
  若服务端返回 4xx 且提示 `input_audio` / `format`，会自动改用「裸 base64 + `format`」的
  gpt-4o-audio 形状重试一次。
- ffmpeg `-ss` 放在 `-i` **之前**是快但关键帧级；要帧级精确就放到 `-i` 之后。
  抽静帧用前者（够用且快 10 倍）；**尾帧的时间点常落在最后一帧之后**，`-ss` 会什么都不产出，
  所以抽不到时按 0 / .1 / .3 / .6 / 1.2 / 2.5 秒逐级回退（实测 212.3 s 的视频退 0.1 s 仍为空）。
  场景检测必须全解码，所以那一步是一次性扫全片。
- 场景检测的 filter 里必须加 `metadata=print` 才能拿到 `lavfi.scene_score`，但它**也会打
  `pts_time`** —— 时间戳只能从 `Parsed_showinfo` 行取，否则帧会错配。
- Windows 中文路径：Python 子进程统一 `sys.stdout.reconfigure(encoding='utf-8', errors='replace')`；
  配置文件若被记事本保存成带 BOM 的格式，host 与 python 都会先剥 BOM 再解析。
- 40 分钟视频完整索引大约 3~8 分钟（取决于 ASR/视觉端点的并发与延迟）。
  中断后用 `resume=true` 续跑，已完成的分段和帧描述不会重算。
- `test/fixture.mp4` 是 `test/make_fixture.py` 生成的素材（约 5.6 MB），不进仓库；
  `test/e2e_mock_test.py` 发现缺失会自己先生成。

## 目录

```
dsh-videokit/
├── package.json          # host + client 双半边声明
├── cordis.patch.yml      # bundle patch：插入 dsh-videokit 这一行
├── index.js              # 九个工具 + 用法提示词 + /videokit-api
├── client.js             # 设置页「视频理解」栏（无构建，运行时加载）
├── config.js             # 配置读写、脱敏、路径探测、端点连通性测试
├── scripts/videokit.py   # 全部重活（ffmpeg/ffprobe、分段、并发、HTTP）
├── locale/{en,zh}.json   # 插件清单里的标题与描述
├── icon.svg
├── test/                 # 零网络自带测试（ffmpeg 假素材 + mock 端点 + needle 验收）
├── verify/               # 装配自检（execute→render 往返）+ 真实模型验收
├── dev/render_check.mjs  # 设置页组件的离线渲染/交互检查（伪 window + SSR）
└── .github/workflows/    # CI
```

`SPEC.md` 是三方（host / client / python）的接口契约，改任一边先改它。
它是这个项目里最有用的一份文档 —— 三个半边并行开发时，靠它才没有互相猜。

## 许可

MIT
