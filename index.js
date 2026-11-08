/**
 * dsh-videokit —— dsh 长视频理解插件（Host 半边）。
 *
 * 七个工具：
 *   video_probe      —— 时长/分辨率/音轨/章节，先跑它再决定密度与分块
 *   video_download   —— B站 / yt-dlp 取流到本地
 *   video_index      —— 重活：音频全长 ASR + 场景/尾部加密抽帧 + VLM + 时间线
 *   video_timeline   —— 只读查询时间线（--tail 直接看结尾）
 *   video_frames     —— 定点高密度抽帧（结论在某处时二次细读）
 *   video_summarize  —— map-reduce / tail-first 汇总
 *   video_report     —— 出 markdown 报告
 *
 * 重活全在 scripts/videokit.py（Python：ffmpeg/ffprobe、分段、并发、HTTP）。
 * 端点与路径不写死：读 ~/.dsh/dsh-videokit.json（设置页写入），每次调用实时重读。
 * 可选 webServer 时注册 /videokit-api（设置页的数据面）。
 */
import { defineTool } from '@deepseek-ai/dsh-tools'
import { execFile } from 'node:child_process'
import { existsSync, mkdirSync } from 'node:fs'
import { basename, dirname, extname, isAbsolute, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  CLEAR, applyPatch, defaultsCopy, detectBinaries, readSettings, redact,
  resolveRuntime, settingsPath, testEndpoint,
} from './config.js'

export const name = 'dsh-videokit'
export const inject = ['tools', 'systemPrompt']

const HERE = dirname(fileURLToPath(import.meta.url))
const SCRIPT = join(HERE, 'scripts', 'videokit.py')

const USAGE_PROMPT = '长视频理解（videokit）：要"读完一整个长视频并讲清叙事"时用它，不要自己均匀抽帧。\n' +
  '- 顺序：先 video_probe 看时长/音轨，再 video_index 建索引（音频全长转写 + 场景/尾部加密抽帧）。\n' +
  '- 半小时以上的视频：video_index 默认 tailRatio 0.2，建议显式给 0.25；maxFrames 按预算给 120~240。\n' +
  '- 问"结论/最后怎么样"：先 video_timeline tail=40 看结尾，不要从头扫。\n' +
  '- 定位到某段后要细看画面：video_frames 在该区间按 fps 1~2 加密，再自己读图。\n' +
  '- 要成文：video_summarize（mapreduce 或 tail-first），然后 video_report 出 markdown。\n' +
  '- video_index 里 vision=auto/asr=auto 需要用户在 设置 → 视频理解 填好端点；未配置时管线会跳过该阶段并在输出里说明。\n' +
  '- 路径一律给绝对路径；相对路径按配置的 workdir 解析。首次耗时长（40 分钟视频约 3~8 分钟），不要重复跑同一个索引，用 --resume。'

// ---------------------------------------------------------------------------
// 进程与文本
// ---------------------------------------------------------------------------

/** 跑 videokit.py，返回 {code, killed, stdout, stderr}。 */
function run(cmd, args, { timeoutMs = 600000, cwd } = {}) {
  return new Promise((resolvePromise) => {
    execFile(cmd, args, { timeout: timeoutMs, maxBuffer: 32 * 1024 * 1024, cwd, windowsHide: true },
      (err, stdout, stderr) => {
        resolvePromise({
          code: err && typeof err.code === 'number' ? err.code : (err ? 1 : 0),
          killed: !!(err && err.killed),
          stdout: String(stdout || ''),
          stderr: String(stderr || ''),
        })
      })
  })
}

function textResult(text) {
  return [{ type: 'text', text }]
}

function fmtRun(r) {
  const parts = []
  if (r.stdout.trim()) parts.push(r.stdout.trim())
  if (r.stderr.trim()) parts.push('[stderr] ' + r.stderr.trim())
  if (r.killed) parts.push('[超时被杀；长视频可调大 timeout，或先用 --resume 分段续跑]')
  if (r.code === 2) parts.push('[exit 2] 缺少可选依赖，已给出安装提示（见上）')
  else if (r.code !== 0) parts.push('[exit ' + r.code + ']')
  return parts.join('\n') || '(无输出)'
}

const isUrl = (s) => /^(https?|ftp):\/\//i.test(String(s || ''))

/** 相对路径按配置的 workdir 解析（插件进程 cwd ≠ 会话 workspace）。 */
function makeResolver(runtime) {
  return function abs(p) {
    const raw = String(p ?? '').trim()
    if (!raw) return runtime.workdir
    if (isUrl(raw)) return raw
    if (isAbsolute(raw)) return raw
    if (existsSync(raw)) return resolve(raw)
    return join(runtime.workdir, raw)
  }
}

/** 本地视频 → 默认产物目录名。 */
function stemOf(p) {
  const base = basename(String(p))
  const ext = extname(base)
  return ext ? base.slice(0, -ext.length) : base
}

/** 端点未配置时的提醒（不阻断，管线自己会跳过该阶段）。 */
function endpointHints(settings) {
  const missing = []
  if (!settings.asr.base || !settings.asr.key) missing.push('语音识别')
  if (!settings.vision.base || !settings.vision.key) missing.push('画面理解')
  if (!missing.length) return ''
  return '\n\n· 未配置端点：' + missing.join(' / ') +
    '（设置 → 视频理解 里填接口地址、API Key、模型名）。缺失的阶段会被自动跳过，索引仍然可用但内容会少一部分。'
}

// ---------------------------------------------------------------------------
// 工具
// ---------------------------------------------------------------------------

export function registerTools(ctx) {
  /** 统一入口：读配置 → 定位 python → 执行 → 拼文本。 */
  const exec = async (argv, timeoutMs = 600000) => {
    const settings = readSettings()
    const runtime = resolveRuntime(settings)
    // 工作目录可能还不存在（用户第一次用）。execFile 的 cwd 不存在会直接
    // ENOENT 且 stdout/stderr 全空，看起来像"exit 1 无输出"，非常难查。
    let cwd
    try {
      mkdirSync(runtime.workdir, { recursive: true })
      cwd = runtime.workdir
    } catch {
      cwd = HERE // 目录建不出来（只读盘/盘符不存在）就退回插件目录，别让工具直接挂掉
    }
    const full = [SCRIPT, ...argv, '--config', settingsPath(), '--workdir', runtime.workdir]
    const r = await run(runtime.python, full, { timeoutMs, cwd })
    return { text: fmtRun(r), code: r.code }
  }

  // ---- video_probe ----
  ctx.tools.register(defineTool({
    name: 'video_probe',
    description: '探测一个视频：时长、分辨率、fps、音视频编码与章节，并把结果写成 probe.json。'
      + '支持本地绝对路径和 http(s) 链接（链接会先下载到工作目录缓存）。索引长视频前先跑它。',
    parameters: {
      video: { type: 'string', required: true, description: '视频本地绝对路径，或 B站/YouTube 等 http(s) 链接' },
      out: { type: 'string', description: '产物目录；省略则用 <工作目录>/<视频文件名>' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => true,
    async execute(args) {
      const runtime = resolveRuntime()
      const abs = makeResolver(runtime)
      const video = abs(args.video)
      const argv = ['probe', video]
      if (args.out) argv.push('--out', abs(args.out))
      else if (!isUrl(video)) argv.push('--out', join(runtime.workdir, stemOf(video)))
      return await exec(argv, 180000)
    },
  }))

  // ---- video_download ----
  ctx.tools.register(defineTool({
    name: 'video_download',
    description: '把在线视频取回本地：B站走 playurl 取 dash 音视频流再 ffmpeg 合流；'
      + 'YouTube 等走 yt-dlp（未安装时给出明确安装提示）。B站分 P 视频默认只下第一个 P，'
      + '要下全部分 P 用 allPages=true（可断点续传，每下完一个就写 manifest）。返回本地绝对路径。',
    parameters: {
      url: { type: 'string', required: true, description: '视频链接（B站 / YouTube / 其他 yt-dlp 支持的站点）' },
      out: { type: 'string', description: '保存目录；省略则用 <工作目录>/downloads' },
      p: { type: 'integer', description: '只下第 N 个分 P（B站）；省略则取链接里的 ?p=，再省略取 P1' },
      allPages: { type: 'boolean', description: '下载全部分 P（B站分 P 视频必需；30 小时课程请先用这个）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const runtime = resolveRuntime()
      const out = args.out ? makeResolver(runtime)(args.out) : join(runtime.workdir, 'downloads')
      const argv = ['download', String(args.url), '--out', out]
      if (args.p !== undefined) argv.push('--p', String(args.p))
      if (args.allPages) argv.push('--all-pages')
      return await exec(argv, 3600000)
    },
  }))

  // ---- video_index ----
  ctx.tools.register(defineTool({
    name: 'video_index',
    description: '给视频建结构化索引（重活，几十秒到几分钟）：音频按段全长转写 → 场景切分抽帧 + 尾部加密 + VLM 描述 → '
      + 'timeline.json（带时间戳的事件流）+ chapters.json。vision/asr 端点未配置时自动跳过该阶段。'
      + '产物目录可复用，resume=true 跳过已完成的分段与帧。',
    parameters: {
      video: { type: 'string', required: true, description: '视频绝对路径（或 http(s) 链接，会先下载）' },
      out: { type: 'string', description: '产物目录；省略则用 <工作目录>/<视频文件名>' },
      density: { type: 'number', description: '均匀抽帧密度倍数，默认 1.0（density=1 → 每 2 分钟一帧）；2.0 更密' },
      tailRatio: { type: 'number', description: '尾部加密区间占总时长比例，默认 0.2；半小时以上建议 0.25' },
      maxFrames: { type: 'integer', description: '送 VLM 的帧数上限，默认 200（按 场景分 > 尾部 > 中段 淘汰）' },
      vision: { type: 'string', enum: ['auto', 'none'], description: 'auto=调视觉端点描述画面；none=只出帧不描述' },
      asr: { type: 'string', enum: ['auto', 'none'], description: 'auto=调语音端点转写；none=不转写' },
      resume: { type: 'boolean', description: '跳过已完成的音频分段、ASR 结果与帧描述' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const settings = readSettings()
      const runtime = resolveRuntime(settings)
      const abs = makeResolver(runtime)
      const video = abs(args.video)
      const argv = ['index', video]
      argv.push('--out', args.out ? abs(args.out) : (isUrl(video) ? join(runtime.workdir, 'downloads') : join(runtime.workdir, stemOf(video))))
      if (args.density !== undefined) argv.push('--density', String(args.density))
      if (args.tailRatio !== undefined) argv.push('--tail-ratio', String(args.tailRatio))
      if (args.maxFrames !== undefined) argv.push('--max-frames', String(args.maxFrames))
      if (args.vision) argv.push('--vision', String(args.vision))
      if (args.asr) argv.push('--asr', String(args.asr))
      if (args.resume) argv.push('--resume')
      const r = await exec(argv, 3600000)
      // 只在这条重活上贴端点提醒：轻量工具贴了纯属噪音
      return { text: r.text + endpointHints(settings) }
    },
  }))

  // ---- video_timeline ----
  ctx.tools.register(defineTool({
    name: 'video_timeline',
    description: '紧凑读取 timeline.json，每行 `[mm:ss] 正文`。问"结论/最后怎么样"时先 tail=40 看结尾，'
      + '不要从头扫；再按 from/to 或 grep 定位具体片段。',
    parameters: {
      dir: { type: 'string', required: true, description: '索引产物目录（video_index 的 out）' },
      from: { type: 'string', description: '起始时间：90 / 1:30 / 01:02:03' },
      to: { type: 'string', description: '结束时间，写法同上' },
      grep: { type: 'string', description: '按正文关键词过滤（不区分大小写）' },
      tail: { type: 'integer', description: '只取时间线最后 N 条（与 from/to 互斥，优先级更高）' },
      kind: { type: 'string', enum: ['speech', 'scene', 'ocr'], description: '只看某类事件：语音 / 画面 / 屏幕文字' },
      limit: { type: 'integer', description: '最多输出多少行，默认 200' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => true,
    async execute(args) {
      const argv = ['timeline', makeResolver(resolveRuntime())(args.dir)]
      if (args.from) argv.push('--from', String(args.from))
      if (args.to) argv.push('--to', String(args.to))
      if (args.grep) argv.push('--grep', String(args.grep))
      if (args.tail !== undefined) argv.push('--tail', String(args.tail))
      if (args.kind) argv.push('--kind', String(args.kind))
      if (args.limit !== undefined) argv.push('--limit', String(args.limit))
      return await exec(argv, 120000)
    },
  }))

  // ---- video_frames ----
  ctx.tools.register(defineTool({
    name: 'video_frames',
    description: '在 [from, to] 区间定点高密度抽帧（不调视觉模型），返回图片绝对路径供会话主模型自己读图。'
      + '用于"结论在某段"时二次细看画面。',
    parameters: {
      video: { type: 'string', required: true, description: '视频绝对路径' },
      out: { type: 'string', required: true, description: '输出目录' },
      from: { type: 'string', required: true, description: '起始时间，如 12:30' },
      to: { type: 'string', required: true, description: '结束时间，如 13:10' },
      fps: { type: 'number', description: '抽帧频率，默认 1.0（即每秒一帧）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const abs = makeResolver(resolveRuntime())
      const argv = ['frames', abs(args.video), '--out', abs(args.out),
        '--from', String(args.from), '--to', String(args.to)]
      if (args.fps !== undefined) argv.push('--fps', String(args.fps))
      return await exec(argv, 900000)
    },
  }))

  // ---- video_summarize ----
  ctx.tools.register(defineTool({
    name: 'video_summarize',
    description: '把时间线切成块，每块独立抽 3-5 条带时间戳的事实，再只做去重排序的归并（map-reduce，不二次压缩）。'
      + 'mode=tail-first 时归并阶段把尾部块放最前面，适合"结论在结尾"的长视频。需要已配置文本汇总端点。',
    parameters: {
      dir: { type: 'string', required: true, description: '索引产物目录' },
      mode: { type: 'string', enum: ['mapreduce', 'tail-first'], description: '归并策略，默认 mapreduce' },
      question: { type: 'string', description: '只关注某个问题时的检索焦点（例如"最后的结论是什么"）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const settings = readSettings()
      if (!settings.llm.base || !settings.llm.key) {
        return {
          text: '文本汇总端点未配置。请在 设置 → 视频理解 → 文本汇总 里填写接口地址、API Key、模型名，'
            + '然后再调用 video_summarize。（配置存在 ' + settingsPath() + '）',
        }
      }
      const argv = ['summarize', makeResolver(resolveRuntime(settings))(args.dir)]
      if (args.mode) argv.push('--mode', String(args.mode))
      if (args.question) argv.push('--question', String(args.question))
      return await exec(argv, 1800000)
    },
  }))

  // ---- video_series_report ----
  ctx.tools.register(defineTool({
    name: 'video_series_report',
    description: '把 download allPages 下的一组分 P 索引聚合成一份系列报告：总览、逐集统计、'
      + '课程大纲（需 llm 端点，summarize=true）。一个 30 小时的课程要看整体结构时用它。',
    parameters: {
      dir: { type: 'string', required: true, description: 'download --all-pages 的输出目录' },
      out: { type: 'string', description: 'md 输出路径；默认 <dir>/series.md' },
      summarize: { type: 'boolean', description: '逐集 LLM 摘要 + 课程大纲（要 llm 端点，慢）' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const argv = ['series-report', makeResolver(resolveRuntime())(args.dir)]
      if (args.out) argv.push('--out', makeResolver(resolveRuntime())(args.out))
      if (args.summarize) argv.push('--summarize')
      return await exec(argv, 3600000)
    },
  }))

  // ---- video_index_series ----
  ctx.tools.register(defineTool({
    name: 'video_index_series',
    description: '逐分 P 建索引：每个 P 一套独立产物（<dir>/<P01>/），失败不中断，'
      + 'resume=true 跳过已完成的。30 小时课程不要用 video_index 吞成一坨：单目录的 maxFrames '
      + '摊到 30 小时等于每 9 分钟一帧，画面信息全丢。',
    parameters: {
      dir: { type: 'string', required: true, description: 'download --all-pages 的输出目录' },
      maxFrames: { type: 'integer', description: '每个 P 的帧数上限，默认 60（不是整系列的总数）' },
      tailRatio: { type: 'number', description: '每个 P 的尾部加密比例，默认 0.2' },
      density: { type: 'number', description: '每个 P 的均匀抽帧密度倍数，默认 1.0' },
      vision: { type: 'string', enum: ['auto', 'none'], description: 'auto=调视觉端点；none=只出帧' },
      asr: { type: 'string', enum: ['auto', 'none'], description: 'auto=调语音端点；none=不转写' },
      only: { type: 'string', description: '只处理这些分 P，逗号分隔，如 1,2,61' },
      resume: { type: 'boolean', description: '跳过已经建好索引的分 P' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => false,
    async execute(args) {
      const settings = readSettings()
      const runtime = resolveRuntime(settings)
      const argv = ['index-series', makeResolver(runtime)(args.dir)]
      if (args.density !== undefined) argv.push('--density', String(args.density))
      if (args.tailRatio !== undefined) argv.push('--tail-ratio', String(args.tailRatio))
      if (args.maxFrames !== undefined) argv.push('--max-frames', String(args.maxFrames))
      if (args.vision) argv.push('--vision', String(args.vision))
      if (args.asr) argv.push('--asr', String(args.asr))
      if (args.only) argv.push('--only', String(args.only))
      if (args.resume) argv.push('--resume')
      const r = await exec(argv, 14400000)
      return { text: r.text + endpointHints(settings) }
    },
  }))

  // ---- video_report ----
  ctx.tools.register(defineTool({
    name: 'video_report',
    description: '把概要、章节时间线、map-reduce 结论、尾部事实清单、关键帧路径整理成 markdown 报告并写文件。',
    parameters: {
      dir: { type: 'string', required: true, description: '索引产物目录' },
      out: { type: 'string', description: 'md 输出路径；默认 <dir>/report.md' },
    },
    output: { schema: { type: 'json' }, render: (_a, v) => textResult(String(v?.text ?? '')) },
    isConcurrencySafe: () => true,
    async execute(args) {
      const abs = makeResolver(resolveRuntime())
      const argv = ['report', abs(args.dir)]
      if (args.out) argv.push('--out', abs(args.out))
      return await exec(argv, 300000)
    },
  }))
}

// ---------------------------------------------------------------------------
// /videokit-api：设置页的数据面
// ---------------------------------------------------------------------------

/**
 * 可选注入 webServer。
 * 不能写成 ctx.webServer（未声明 inject 会抛），也不能把 webServer 写进顶层
 * inject（那会变成必需依赖，非 web 模式下阻塞整个插件）。
 */
function registerApi(webServer) {
  const json = (res, obj, status = 200) => {
    const body = JSON.stringify(obj)
    res.writeHead(status, { 'Content-Type': 'application/json; charset=utf-8', 'Cache-Control': 'no-store' })
    res.end(body)
  }
  const readBody = (req) => new Promise((resolvePromise, reject) => {
    const chunks = []
    req.on('data', (c) => chunks.push(c))
    req.on('end', () => resolvePromise(Buffer.concat(chunks).toString('utf8')))
    req.on('error', reject)
  })

  webServer.register({
    kind: 'exact',
    path: '/videokit-api',
    async handler(req, res) {
      if (req.method === 'GET') {
        try {
          const detected = await detectBinaries()
          return json(res, {
            ok: true,
            config: redact(readSettings()),
            defaults: redact(defaultsCopy()),
            detected,
            settingsPath: settingsPath(),
          })
        } catch (error) {
          return json(res, { ok: false, error: msgOf(error) }, 500)
        }
      }

      let body
      try {
        body = JSON.parse(await readBody(req))
      } catch {
        return json(res, { ok: false, error: '无效的 JSON 请求体' }, 400)
      }

      const op = String(body?.op || '')
      try {
        if (op === 'save') {
          return json(res, { ok: true, config: redact(applyPatch(body.patch)) })
        }
        if (op === 'test') {
          const section = String(body.section || '')
          if (!['asr', 'vision', 'llm'].includes(section)) {
            return json(res, { ok: false, error: '未知 section: ' + section }, 400)
          }
          return json(res, await testEndpoint(section, body.draft || {}))
        }
        if (op === 'detect') {
          return json(res, { ok: true, detected: await detectBinaries() })
        }
        if (op === 'clear') {
          // 便捷入口：清空某一组的 key（设置页也可用 key: '__clear__'）
          const section = String(body.section || '')
          if (!['asr', 'vision', 'llm'].includes(section)) {
            return json(res, { ok: false, error: '未知 section: ' + section }, 400)
          }
          return json(res, { ok: true, config: redact(applyPatch({ [section]: { key: CLEAR } })) })
        }
        return json(res, { ok: false, error: '未知操作: ' + op }, 400)
      } catch (error) {
        return json(res, { ok: false, error: msgOf(error) }, 500)
      }
    },
  })

  console.log('[dsh-videokit] 设置页接口: /videokit-api（配置 ' + settingsPath() + '）')
}

function msgOf(error) {
  return error instanceof Error ? error.message : String(error)
}

// ---------------------------------------------------------------------------

export function apply(ctx) {
  const settings = readSettings()
  const runtime = resolveRuntime(settings)
  const configured = ['asr', 'vision', 'llm'].filter((s) => settings[s].base && settings[s].key)
  console.log('[dsh-videokit] python=' + runtime.python + ' ffmpeg=' + runtime.ffmpeg)
  console.log('[dsh-videokit] workdir=' + runtime.workdir + ' 已配置端点: ' + (configured.join(',') || '(无)'))

  registerTools(ctx)
  ctx.systemPrompt.section({
    name: 'dsh-videokit:usage',
    order: 145,
    text: USAGE_PROMPT,
  })

  ctx.inject(['webServer'], (scope) => {
    registerApi(scope.webServer)
  })
}
