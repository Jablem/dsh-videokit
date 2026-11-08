/**
 * dsh-videokit —— 配置读写、路径探测、端点连通性测试（Host 侧共享工具）。
 *
 * 设计要点：
 *   1. 配置存 `~/.dsh/dsh-videokit.json`（稳定，不受包升级影响），key 明文存本地，
 *      与 dsh 现有凭据文件一致；**任何回传前端的地方都必须走 redact()**。
 *   2. 每次调用实时重读文件，改完设置页不用重启 dsh。
 *   3. 端点全部按 OpenAI 兼容实现：base 规范化成 `.../chat/completions`，
 *      认证头按 `auth` 二选一（api-key / Bearer）。
 */
import { chmodSync, existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs'
import { execFile } from 'node:child_process'
import { homedir } from 'node:os'
import { dirname, join } from 'node:path'
import { deflateSync } from 'node:zlib'

/** 配置文件路径：可用 VIDEOKIT_CONFIG 覆盖（测试用）。 */
export function settingsPath() {
  return process.env.VIDEOKIT_CONFIG || join(homedir(), '.dsh', 'dsh-videokit.json')
}

const IS_WIN = process.platform === 'win32'

/**
 * 已知的二进制安装位置（探测顺序：配置文件 > VIDEOKIT_* 环境变量 > PATH > 这些绝对路径）。
 * 这里只放各平台**常见的默认安装位置**，不放任何个人目录 —— 装到非常规位置的用户
 * 要么在设置页填路径，要么设环境变量。
 */
const KNOWN_PYTHON = IS_WIN
  ? [
      join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python312', 'python.exe'),
      join(process.env.LOCALAPPDATA || '', 'Programs', 'Python', 'Python311', 'python.exe'),
      join(homedir(), 'anaconda3', 'python.exe'),
      join(homedir(), 'miniconda3', 'python.exe'),
      join(homedir(), 'AppData', 'Local', 'anaconda3', 'python.exe'),
      'C:/Python312/python.exe',
      'C:/Python311/python.exe',
      'C:/Python310/python.exe',
    ]
  : ['/usr/bin/python3', '/usr/local/bin/python3', '/opt/homebrew/bin/python3']

const FFMPEG_DIRS = IS_WIN
  ? [
      join(homedir(), 'scoop', 'shims'),
      join(homedir(), 'chocolatey', 'bin'),
      'C:/ffmpeg/bin',
      'C:/Program Files/ffmpeg/bin',
    ]
  : ['/usr/bin', '/usr/local/bin', '/opt/homebrew/bin']

/** 插件自带的兜底工作目录（设置页可改）。 */
const DEFAULT_WORKDIR = join(homedir(), 'videokit')

/**
 * 默认配置。注意 `base`/`key` 一律留空 —— 那是"未配置"的信号，
 * 工具据此给出中文引导而不是去请求一个坏地址。model 留一个可用占位，
 * 用户在设置页填了 base 就能直接点「测试连接」。
 */
export const DEFAULTS = Object.freeze({
  pythonPath: '',
  ffmpeg: '',
  ffprobe: '',
  workdir: DEFAULT_WORKDIR,
  asr: { base: '', key: '', model: 'mimo-v2.5-asr', auth: 'api-key', chunkMinutes: 5 },
  vision: { base: '', key: '', model: 'glm-4.1v-thinking-flash', auth: 'bearer', maxFrames: 200 },
  llm: { base: '', key: '', model: 'deepseek-chat', auth: 'bearer', maxTokens: 2048 },
})

/** 端点分组名。 */
export const SECTIONS = ['asr', 'vision', 'llm']

/** 深拷贝一份默认值（避免调用方改到冻结对象内部）。 */
export function defaultsCopy() {
  return JSON.parse(JSON.stringify(DEFAULTS))
}

function mergeSection(base, patch) {
  const out = { ...base }
  for (const [k, v] of Object.entries(patch || {})) {
    if (v === undefined) continue
    out[k] = v
  }
  return out
}

/** 把磁盘上的片段合并进默认值，返回完整配置。 */
export function normalize(raw) {
  const cfg = defaultsCopy()
  if (!raw || typeof raw !== 'object') return cfg
  for (const [k, v] of Object.entries(raw)) {
    if (v === undefined || v === null) continue
    if (SECTIONS.includes(k) && v && typeof v === 'object') cfg[k] = mergeSection(cfg[k], v)
    else if (k in cfg && typeof cfg[k] !== 'object') cfg[k] = v
  }
  return cfg
}

/** 实时读配置。文件不存在/损坏都退化成默认值，绝不抛。 */
export function readSettings() {
  try {
    // 用户拿记事本/其他工具改过会带 UTF-8 BOM，JSON.parse 会拒绝，先剥掉
    const text = readFileSync(settingsPath(), 'utf8').replace(/^\uFEFF/, '')
    return normalize(JSON.parse(text))
  } catch {
    return defaultsCopy()
  }
}

/** 原子写配置（先写 .tmp 再 rename 覆盖），并尽量收紧权限。 */
export function writeSettings(cfg) {
  const file = settingsPath()
  mkdirSync(dirname(file), { recursive: true })
  const text = JSON.stringify(cfg, null, 2) + '\n'
  const tmp = file + '.tmp'
  writeFileSync(tmp, text, 'utf8')
  // rename 在两个平台都是原子的且可覆盖已存在文件
  renameSync(tmp, file)
  try { chmodSync(file, 0o600) } catch { /* 文件系统不支持，忽略 */ }
  return cfg
}

/** 用户显式要求清空某个 key 时用的哨兵值。 */
export const CLEAR = '__clear__'

/**
 * 应用一处设置补丁。
 *   - 顶层标量直接覆盖
 *   - asr/vision/llm 内部按字段补丁式合并
 *   - key 为 '' 或脱敏串 → 保持原 key；key 为 CLEAR → 清空
 * @returns 合并后的完整配置（未脱敏）
 */
export function applyPatch(patch) {
  const current = readSettings()
  if (!patch || typeof patch !== 'object') return current
  for (const [k, v] of Object.entries(patch)) {
    if (v === undefined || v === null) continue
    if (SECTIONS.includes(k) && typeof v === 'object') {
      const next = { ...current[k] }
      for (const [fk, fv] of Object.entries(v)) {
        if (fv === undefined) continue
        if (fk === 'key') {
          if (fv === CLEAR) next.key = ''
          else if (fv === '' || fv === maskKey(current[k].key)) continue // 用户没动
          else next.key = String(fv)
          continue
        }
        next[fk] = fv
      }
      current[k] = next
    } else if (k in current && typeof current[k] !== 'object') {
      current[k] = v
    }
  }
  return writeSettings(current)
}

/** 脱敏：sk-****abcd；太短就整串打码。 */
export function maskKey(key) {
  const s = String(key ?? '')
  if (!s) return ''
  if (s === CLEAR) return CLEAR
  if (s.length < 8) return '****'
  return s.slice(0, 3) + '****' + s.slice(-4)
}

/** 回传前端用：整份配置打码。 */
export function redact(cfg) {
  const out = { ...cfg }
  for (const s of SECTIONS) out[s] = { ...cfg[s], key: maskKey(cfg[s]?.key) }
  return out
}

/** base 规范化：去尾斜杠，非 /chat/completions 结尾就补上。 */
export function chatEndpoint(base) {
  const b = String(base ?? '').trim().replace(/\/+$/, '')
  if (!b) return ''
  return /\/chat\/completions$/i.test(b) ? b : b + '/chat/completions'
}

/**
 * 解析一次运行需要的可执行文件路径。纯同步（工具调用时用），
 * 不做 `where` 探测 —— 裸命令名交给子进程的 PATH 解析。
 *
 * 优先级：设置页配置 > 环境变量（VIDEOKIT_PYTHON / VIDEOKIT_FFMPEG / VIDEOKIT_FFPROBE）
 * > 常见安装位置 > 裸命令名走 PATH。
 */
export function resolveRuntime(settings = readSettings()) {
  const pick = (configured, candidates, fallback) => {
    if (configured) return configured
    for (const c of candidates) if (c && existsSync(c)) return c
    return fallback
  }
  const ffmpegName = IS_WIN ? 'ffmpeg.exe' : 'ffmpeg'
  const ffprobeName = IS_WIN ? 'ffprobe.exe' : 'ffprobe'
  return {
    python: pick(
      settings.pythonPath || process.env.VIDEOKIT_PYTHON,
      KNOWN_PYTHON, IS_WIN ? 'python' : 'python3'),
    ffmpeg: pick(
      settings.ffmpeg || process.env.VIDEOKIT_FFMPEG,
      FFMPEG_DIRS.map((d) => join(d, ffmpegName)), ffmpegName),
    ffprobe: pick(
      settings.ffprobe || process.env.VIDEOKIT_FFPROBE,
      FFMPEG_DIRS.map((d) => join(d, ffprobeName)), ffprobeName),
    workdir: String(settings.workdir || DEFAULT_WORKDIR).replace(/\\/g, '/').replace(/\/+$/, ''),
  }
}

/** `where`/`which` 探测一个裸命令，失败返回 null。 */
function which(cmd) {
  return new Promise((resolvePromise) => {
    const finder = IS_WIN ? 'where' : 'which'
    execFile(finder, [cmd], { timeout: 8000, windowsHide: true }, (err, stdout) => {
      if (err) return resolvePromise(null)
      const first = String(stdout || '').split(/\r?\n/).map((s) => s.trim()).find(Boolean)
      resolvePromise(first || null)
    })
  })
}

/**
 * 「自动探测」按钮用：真去解析 PATH，给出可写进配置的绝对路径。
 * 每一项都可能为 null（那就是没装）。
 */
export async function detectBinaries() {
  const rt = resolveRuntime()
  const out = { python: null, ffmpeg: null, ffprobe: null, ytdlp: null }
  // 已配置/已知路径优先，PATH 兜底
  if (rt.python && rt.python !== 'python' && rt.python !== 'python3' && existsSync(rt.python)) out.python = rt.python
  else out.python = await which(IS_WIN ? 'python' : 'python3')
  if (rt.ffmpeg && existsSync(rt.ffmpeg)) out.ffmpeg = rt.ffmpeg
  else out.ffmpeg = await which(IS_WIN ? 'ffmpeg.exe' : 'ffmpeg')
  if (rt.ffprobe && existsSync(rt.ffprobe)) out.ffprobe = rt.ffprobe
  else out.ffprobe = await which(IS_WIN ? 'ffprobe.exe' : 'ffprobe')
  out.ytdlp = await which(IS_WIN ? 'yt-dlp.exe' : 'yt-dlp')
  return out
}

// ---------------------------------------------------------------------------
// 最小测试载荷：让「测试连接」这一步花最少的钱
// ---------------------------------------------------------------------------

/** 0.5 s 全零 16 kHz 单声道 16-bit PCM WAV。 */
export function silentWav(seconds = 0.5, sampleRate = 16000) {
  const samples = Math.max(1, Math.floor(seconds * sampleRate))
  const dataLen = samples * 2
  const buf = Buffer.alloc(44 + dataLen)
  buf.write('RIFF', 0, 'ascii')
  buf.writeUInt32LE(36 + dataLen, 4)
  buf.write('WAVE', 8, 'ascii')
  buf.write('fmt ', 12, 'ascii')
  buf.writeUInt32LE(16, 16)   // fmt chunk size
  buf.writeUInt16LE(1, 20)    // PCM
  buf.writeUInt16LE(1, 22)    // mono
  buf.writeUInt32LE(sampleRate, 24)
  buf.writeUInt32LE(sampleRate * 2, 28) // byte rate
  buf.writeUInt16LE(2, 32)    // block align
  buf.writeUInt16LE(16, 34)   // bits per sample
  buf.write('data', 36, 'ascii')
  buf.writeUInt32LE(dataLen, 40)
  return buf
}

const CRC_TABLE = (() => {
  const table = new Int32Array(256)
  for (let n = 0; n < 256; n++) {
    let c = n
    for (let k = 0; k < 8; k++) c = c & 1 ? 0xedb88320 ^ (c >>> 1) : c >>> 1
    table[n] = c
  }
  return table
})()

function crc32(buf) {
  let c = 0xffffffff
  for (let i = 0; i < buf.length; i++) c = CRC_TABLE[(c ^ buf[i]) & 0xff] ^ (c >>> 8)
  return (c ^ 0xffffffff) >>> 0
}

function pngChunk(type, data) {
  const len = Buffer.alloc(4)
  len.writeUInt32BE(data.length, 0)
  const body = Buffer.concat([Buffer.from(type, 'ascii'), data])
  const crc = Buffer.alloc(4)
  crc.writeUInt32BE(crc32(body), 0)
  return Buffer.concat([len, body, crc])
}

/**
 * 生成一张纯色 PNG（默认 16×16 灰）。不引第三方库，也不内联一大坨 base64。
 * @param {number} size 边长
 * @param {[number,number,number]} rgb 像素颜色
 */
export function solidPng(size = 16, rgb = [128, 128, 128]) {
  const ihdr = Buffer.alloc(13)
  ihdr.writeUInt32BE(size, 0)
  ihdr.writeUInt32BE(size, 4)
  ihdr[8] = 8   // bit depth
  ihdr[9] = 2   // color type: truecolor
  ihdr[10] = 0  // deflate
  ihdr[11] = 0  // adaptive filtering
  ihdr[12] = 0  // no interlace
  const rowLen = 1 + size * 3
  const raw = Buffer.alloc(rowLen * size)
  for (let y = 0; y < size; y++) {
    raw[y * rowLen] = 0 // filter: none
    for (let x = 0; x < size; x++) {
      const o = y * rowLen + 1 + x * 3
      raw[o] = rgb[0]
      raw[o + 1] = rgb[1]
      raw[o + 2] = rgb[2]
    }
  }
  return Buffer.concat([
    Buffer.from([0x89, 0x50, 0x4e, 0x47, 0x0d, 0x0a, 0x1a, 0x0a]),
    pngChunk('IHDR', ihdr),
    pngChunk('IDAT', deflateSync(raw, { level: 9 })),
    pngChunk('IEND', Buffer.alloc(0)),
  ])
}

function authHeaders(auth, key) {
  return String(auth || 'bearer').toLowerCase() === 'api-key'
    ? { 'api-key': key, 'Content-Type': 'application/json' }
    : { Authorization: 'Bearer ' + key, 'Content-Type': 'application/json' }
}

/** 把 choices[0].message.content 可能是字符串/分片数组的情况统一成字符串。 */
function messageText(data) {
  const content = data?.choices?.[0]?.message?.content
  if (typeof content === 'string') return content
  if (Array.isArray(content)) {
    return content.map((p) => (typeof p === 'string' ? p : p?.text || '')).join('').trim()
  }
  return ''
}

/**
 * 对一组端点发一次最小请求。
 * @param {'asr'|'vision'|'llm'} section
 * @param {{base?:string,key?:string,model?:string,auth?:string}} draft 未给出的字段回落到已保存配置
 * @returns {Promise<{ok:boolean, latencyMs:number, model:string, error:string|null}>}
 */
export async function testEndpoint(section, draft = {}) {
  const settings = readSettings()
  const saved = settings[section] || {}
  const cfg = {
    base: String(draft.base ?? saved.base ?? '').trim(),
    key: String(draft.key ?? ''),
    model: String(draft.model ?? saved.model ?? '').trim(),
    auth: String(draft.auth ?? saved.auth ?? 'bearer'),
  }
  // 前端只会把脱敏串或空串送回来，两种都代表"用已保存的 key"
  if (!cfg.key || cfg.key === maskKey(saved.key) || cfg.key === CLEAR) cfg.key = String(saved.key || '')

  const fail = (error) => ({ ok: false, latencyMs: 0, model: cfg.model, error })
  if (!cfg.base) return fail('接口地址为空')
  if (!cfg.key) return fail('API Key 为空')
  if (!cfg.model) return fail('模型名为空')

  let body
  if (section === 'asr') {
    const b64 = silentWav(0.5).toString('base64')
    body = {
      model: cfg.model,
      messages: [{
        role: 'user',
        content: [{ type: 'input_audio', input_audio: { data: 'data:audio/wav;base64,' + b64 } }],
      }],
    }
  } else if (section === 'vision') {
    const b64 = solidPng(16, [128, 128, 128]).toString('base64')
    body = {
      model: cfg.model,
      max_tokens: 32,
      messages: [{
        role: 'user',
        content: [
          { type: 'image_url', image_url: { url: 'data:image/png;base64,' + b64 } },
          { type: 'text', text: '用两个字回答这张图是灰的还是彩色的。' },
        ],
      }],
    }
  } else {
    body = { model: cfg.model, max_tokens: 1, messages: [{ role: 'user', content: 'hi' }] }
  }

  const started = Date.now()
  try {
    const res = await fetch(chatEndpoint(cfg.base), {
      method: 'POST',
      headers: authHeaders(cfg.auth, cfg.key),
      body: JSON.stringify(body),
      signal: AbortSignal.timeout(60000),
    })
    const raw = await res.text()
    const latencyMs = Date.now() - started
    let data = null
    try { data = JSON.parse(raw) } catch { /* 非 JSON 响应，下面按文本处理 */ }
    if (!res.ok) {
      return { ok: false, latencyMs, model: cfg.model, error: `HTTP ${res.status} — ${raw.slice(0, 300)}` }
    }
    if (!data) {
      return { ok: false, latencyMs, model: cfg.model, error: '响应不是 JSON：' + raw.slice(0, 200) }
    }
    if (!Array.isArray(data.choices)) {
      return { ok: false, latencyMs, model: cfg.model, error: '响应缺少 choices：' + JSON.stringify(data).slice(0, 250) }
    }
    return {
      ok: true,
      latencyMs,
      model: String(data.model || cfg.model),
      error: null,
      sample: messageText(data).slice(0, 80),
    }
  } catch (error) {
    return {
      ok: false,
      latencyMs: Date.now() - started,
      model: cfg.model,
      error: error?.name === 'TimeoutError'
        ? '请求超时（60s）'
        : (error instanceof Error ? error.message : String(error)),
    }
  }
}
