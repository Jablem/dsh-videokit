/**
 * 离线自检：不启动 dsh 也能验证 host 半边的装配是否成立。
 *
 *   node verify/apply_check.mjs
 *
 * 伪造一个 Cordis ctx（tools / systemPrompt / inject），跑 apply()，
 * 打印注册到的工具名、必填参数、是否声明了并发安全，
 * 然后对 config.js 的配置读写与脱敏做一次往返。
 */
import { fileURLToPath, pathToFileURL } from 'node:url'
import { dirname, join } from 'node:path'
import { rmSync } from 'node:fs'

const HERE = dirname(fileURLToPath(import.meta.url))
const ROOT = join(HERE, '..')
process.env.VIDEOKIT_CONFIG = join(HERE, '.tmp-settings.json')

const { apply } = await import(pathToFileURL(join(ROOT, 'index.js')).href)
const configMod = await import(pathToFileURL(join(ROOT, 'config.js')).href)

const tools = []
const sections = []
const injections = []
const ctx = {
  tools: { register: (def) => tools.push(def) },
  systemPrompt: { section: (s) => sections.push(s) },
  inject: (names, callback) => {
    injections.push(names)
    // 模拟 webServer 就绪：给一个只记录注册的假对象
    callback({ webServer: { register: (route) => injections.push('route:' + route.path) } })
  },
}

apply(ctx)

const required = (t) => t.parameters?.required || []
const optional = (t) => Object.keys(t.parameters?.properties || {}).filter((k) => !required(t).includes(k))

console.log('== 工具 (' + tools.length + ') ==')
for (const t of tools) {
  console.log(`- ${t.name}  required=[${required(t).join(',')}]  optional=[${optional(t).join(',')}]  ` +
    `outSchema=${JSON.stringify(t.output?.schema)}  safeFn=${typeof t.isConcurrencySafe}`)
  if (typeof t.description !== 'string' || t.description.length < 20) throw new Error(t.name + ' 描述太短')
  if (!t.output?.render) throw new Error(t.name + ' 缺 render')
  if (typeof t.execute !== 'function') throw new Error(t.name + ' 缺 execute')
  if (required(t).length === 0) throw new Error(t.name + ' 没有任何必填参数，模型容易乱调')
}
console.log('== systemPrompt ==')
for (const s of sections) console.log(`- ${s.name} order=${s.order} chars=${s.text.length}`)
console.log('== inject ==')
for (const i of injections) console.log('- ' + JSON.stringify(i))

// ---- 配置往返 ----
console.log('== config ==')
const initial = configMod.readSettings()
console.log('工作目录:', initial.workdir)
console.log('默认端点已配置:', configMod.SECTIONS.filter((s) => initial[s].base && initial[s].key))

const saved = configMod.applyPatch({
  workdir: 'X:/tmp/videokit',
  llm: { base: 'https://example.invalid/v1', key: 'sk-test-1234567890', auth: 'bearer' },
})
if (saved.workdir !== 'X:/tmp/videokit') throw new Error('workdir 未写入')
if (saved.llm.key !== 'sk-test-1234567890') throw new Error('key 未写入')

const masked = configMod.redact(saved).llm.key
console.log('脱敏后:', masked)
if (masked.includes('1234567890')) throw new Error('脱敏泄漏了 key')

// 前端把脱敏串送回来 → 必须保持原 key
const afterMaskedSave = configMod.applyPatch({ llm: { key: masked } })
if (afterMaskedSave.llm.key !== 'sk-test-1234567890') throw new Error('脱敏串回传把 key 改坏了')

// 空串也代表"没动"
const afterEmptySave = configMod.applyPatch({ llm: { key: '' } })
if (afterEmptySave.llm.key !== 'sk-test-1234567890') throw new Error('空串把 key 清掉了')

// 显式清除
const afterClear = configMod.applyPatch({ llm: { key: configMod.CLEAR } })
if (afterClear.llm.key !== '') throw new Error('清除标记没有生效')

const runtime = configMod.resolveRuntime(configMod.readSettings())
console.log('解析到的运行时:', runtime)
for (const [k, v] of Object.entries(runtime)) {
  if (typeof v !== 'string' || !v) throw new Error('运行时常量缺失: ' + k)
}

rmSync(process.env.VIDEOKIT_CONFIG, { force: true })
console.log('\nAPPLY-CHECK OK')

// ---------------------------------------------------------------------------
// 真实往返：execute 的返回值必须能被 render 吃掉（这是唯一真正的契约）
//
// 这条检查是补上一个线上 bug 后加的：execute 返回裸字符串、render 却读 v.text，
// 拿到 undefined 后被 dsh 判成 "output.render returned non-lossless JSON"，
// 七个工具全部不可用。装配自检当时只看了 schema，看不出这个。
// ---------------------------------------------------------------------------
console.log('\n== execute → render 往返 ==')

const byName = Object.fromEntries(tools.map((t) => [t.name, t]))

function checkRoundTrip(name, args, expectOk) {
  const tool = byName[name]
  if (!tool) throw new Error('缺少工具 ' + name)
  return tool.execute(args, { signal: undefined }).then((value) => {
    // 1) canonical value 必须是 lossless JSON
    const round = JSON.parse(JSON.stringify(value))
    if (JSON.stringify(round) !== JSON.stringify(value)) {
      throw new Error(name + ': execute 返回值不是 lossless JSON（可能是 undefined / NaN / 函数）')
    }
    // 2) render 必须给出至少一个 text 块，且内容非空
    const blocks = tool.output.render(args, value)
    if (!Array.isArray(blocks) || blocks.length === 0) throw new Error(name + ': render 没返回内容')
    const text = blocks[0].text
    if (typeof text !== 'string' || !text.trim()) throw new Error(name + ': render 的 text 为空或不是字符串')
    // 3) 顺带看看是成功还是失败信息，避免"格式对了但内容是空的"
    const line = text.split('\n').find((l) => l.trim()) || ''
    console.log(`- ${name}: ${line.slice(0, 110)}`)
    if (expectOk && /\[exit \d+\]/.test(text)) throw new Error(name + ' 期望成功但退出码非零: ' + text.slice(0, 200))
    return text
  })
}

const probeText = await checkRoundTrip('video_probe', { video: 'test/fixture.mp4' }, true)
if (!/时长:/.test(probeText)) throw new Error('video_probe 输出里没有时长行')

// 其余六个用"注定失败但很快"的参数走一遍，验的只是返回形状
await checkRoundTrip('video_timeline', { dir: 'no-such-dir' }, false)
await checkRoundTrip('video_summarize', { dir: 'no-such-dir' }, false)
await checkRoundTrip('video_report', { dir: 'no-such-dir' }, false)
await checkRoundTrip('video_index', { video: 'no-such-video.mp4' }, false)
await checkRoundTrip('video_frames', { video: 'no-such-video.mp4', out: 'x', from: '0', to: '1' }, false)

console.log('\nROUND-TRIP OK')

