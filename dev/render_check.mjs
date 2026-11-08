#!/usr/bin/env node
/**
 * dev/render_check.mjs — dsh-videokit 设置页(Client 半边)离线渲染检查。
 *
 * 不装浏览器、不启动 dsh:在 Node 里伪造 window/document/fetch,真实执行 client.js,
 * 然后渲染成 HTML 字符串,断言四张卡片、文案、以及发给 /videokit-api 的请求形状。
 *
 * 三段:
 *   1) 静态渲染  —— factory 无副作用 → apply 注册 slot → renderToStaticMarkup(初始 loading 态)
 *   2) 配置载入  —— 最小 hooks 驱动跑 panel 的 useEffect(fetch GET),渲染回填后的 HTML,
 *                  检查脱敏 key / 已保存值 / 探测结果
 *   3) 交互模拟  —— 驱动 SectionCard,点「测试连接」「保存」「清除」「自动探测」,
 *                  检查 POST body 与卡片内就地结果文案
 *
 * 用法: node dev/render_check.mjs   (退出码 0 = 全过, 1 = 有失败)
 *
 * 需要一对**版本配对**的 react / react-dom（版本不一致时 SSR 直接抛
 * "Objects are not valid as a React child"）。查找顺序：
 *   VIDEOKIT_REACT_DIR 环境变量 > <plugin>/node_modules > <上一级>/node_modules
 * 都没有就设一个指过去，例如：
 *   VIDEOKIT_REACT_DIR=/path/to/node_modules node dev/render_check.mjs
 */
import fs from 'node:fs'
import path from 'node:path'
import { fileURLToPath } from 'node:url'
import { createRequire } from 'node:module'

const require = createRequire(import.meta.url)
const HERE = path.dirname(fileURLToPath(import.meta.url))
const CLIENT = path.resolve(HERE, '..', 'client.js')

const passed = []
const failed = []
function check(name, cond, detail) {
  if (cond) {
    passed.push(name)
    console.log('  ✓ ' + name)
  } else {
    failed.push({ name, detail })
    console.log('  ✗ ' + name + (detail === undefined ? '' : ' → ' + String(detail)))
  }
}
function section(title) {
  console.log('\n== ' + title + ' ==')
}

// ---------------------------------------------------------------------------
// 0. 找一个 react / react-dom 版本配对(两个包版本不一致会直接渲染报错)
// ---------------------------------------------------------------------------
// react 与 react-dom 必须是**配对的同一版本**，否则 SSR 会抛
// "Objects are not valid as a React child"。所以这里逐个候选实测一次真渲染，
// 通过了才用。顺序：显式 env > 插件自己的 node_modules > 上一级仓库的 node_modules。
const NODE_MODULES = path.resolve(HERE, '..', 'node_modules')
const PARENT_NODE_MODULES = path.resolve(HERE, '..', '..', 'node_modules')
const CANDIDATES = [
  process.env.VIDEOKIT_REACT_DIR && {
    label: 'VIDEOKIT_REACT_DIR',
    react: path.join(process.env.VIDEOKIT_REACT_DIR, 'react'),
    reactDom: path.join(process.env.VIDEOKIT_REACT_DIR, 'react-dom'),
  },
  {
    label: NODE_MODULES,
    react: path.join(NODE_MODULES, 'react'),
    reactDom: path.join(NODE_MODULES, 'react-dom'),
  },
  {
    label: PARENT_NODE_MODULES,
    react: path.join(PARENT_NODE_MODULES, 'react'),
    reactDom: path.join(PARENT_NODE_MODULES, 'react-dom'),
  },
].filter(Boolean)

function resolveReactPair() {
  for (const cand of CANDIDATES) {
    try {
      const React = require(cand.react)
      const Server = require(cand.reactDom + '/server')
      if (Server.renderToStaticMarkup(React.createElement('div', null, 'ok')) !== '<div>ok</div>') continue
      return {
        ...cand,
        React,
        Server,
        reactVersion: require(cand.react + '/package.json').version,
        domVersion: require(cand.reactDom + '/package.json').version,
      }
    } catch (error) {
      console.log('  · 跳过 ' + cand.label + '：' + (error && error.message ? error.message : error))
    }
  }
  return null
}

// ---------------------------------------------------------------------------
// 假的 /videokit-api(SPEC §4 形状),并记录每次调用
// ---------------------------------------------------------------------------
const AES = 'sk-****abcd'
const VES = 'vk-****wxyz'
const LES = 'll-****1234'
const DETECTED = {
  python: 'C:/Python311/python.exe',
  ffmpeg: 'C:/ffmpeg/bin/ffmpeg.exe',
  ffprobe: 'C:/ffmpeg/bin/ffprobe.exe',
  ytdlp: null,
}

function baseConfig() {
  return {
    pythonPath: '',
    ffmpeg: '',
    ffprobe: '',
    workdir: 'D:/VideoKit/demo',
    asr: { base: 'https://api.example.com/v1', key: AES, model: 'mimo-v2.5-asr', auth: 'api-key', chunkMinutes: 15 },
    vision: { base: 'https://api.example.com/v1', key: VES, model: 'vision-model', auth: 'bearer', maxFrames: 200 },
    llm: { base: 'https://api.example.com/v1', key: LES, model: 'text-model', auth: 'bearer', maxTokens: 2048 },
  }
}

function maskKey(key) {
  const s = String(key || '')
  if (!s) return ''
  return s.length >= 8 ? s.slice(0, 3) + '****' + s.slice(-4) : '****'
}

const fetchCalls = []
function makeFakeFetch(state) {
  return async function fakeFetch(url, init) {
    const method = (init && init.method) || 'GET'
    const call = { url: String(url), method, body: init && init.body ? JSON.parse(init.body) : null }
    fetchCalls.push(call)
    const respond = (obj) => ({ ok: true, status: 200, json: async () => obj, text: async () => JSON.stringify(obj) })

    if (method === 'GET') {
      return respond({ ok: true, config: state.config, defaults: baseConfig(), detected: DETECTED })
    }
    const op = call.body && call.body.op
    if (op === 'test') {
      if (state.throwOnTest) throw new Error('Failed to fetch')
      if (state.failTest) return respond({ ok: false, latencyMs: 12, model: null, error: 'HTTP 401 — invalid api key' })
      return respond({ ok: true, latencyMs: 812, model: 'mimo-v2.5-asr', error: null })
    }
    if (op === 'detect') return respond({ ok: true, detected: DETECTED })
    if (op === 'save') {
      // 模拟 host:key 为 '' 保持原值,__clear__ 清空,其它原样存;回传时脱敏。
      const patch = (call.body && call.body.patch) || {}
      for (const key of Object.keys(patch)) {
        if (key === 'asr' || key === 'vision' || key === 'llm') {
          const sec = patch[key]
          const prev = state.config[key]
          const nextKey = sec.key === '__clear__' ? '' : sec.key ? sec.key : prev.key
          state.config[key] = Object.assign({}, prev, sec, { key: maskKey(nextKey) })
          if (sec.key === '__clear__') state.config[key].key = ''
        } else {
          state.config[key] = patch[key]
        }
      }
      return respond({ ok: true, config: state.config })
    }
    return respond({ ok: false, error: '未知操作: ' + op })
  }
}

// ---------------------------------------------------------------------------
// 假的浏览器环境
// ---------------------------------------------------------------------------
function createFakeBrowser() {
  const captured = { module: null }
  const appended = []
  const fakeWindow = {
    __ModuleLoader__: {
      load(mod) {
        captured.module = mod
      },
    },
  }
  const fakeDocument = {
    createElement(tag) {
      const el = {
        tagName: String(tag).toUpperCase(),
        textContent: '',
        style: {},
        removed: false,
        appendChild() {},
        remove() {
          el.removed = true
        },
      }
      return el
    },
    head: {
      appendChild(el) {
        appended.push(el)
        return el
      },
    },
    body: { appendChild() {} },
  }
  return { captured, appended, fakeWindow, fakeDocument }
}

// ---------------------------------------------------------------------------
// 最小 hooks 驱动:跑函数组件的 useState/useEffect(仅供离线检查)
//   - driver 主动调用组件时(active)用自带 state cell / effect 表,按组件分 scope
//   - 被 react-dom 真实渲染时(active=false)原样转给真的 React(SSR 下 useEffect 是空操作)
// ---------------------------------------------------------------------------
function createDriver(ReactReal) {
  const scopes = new Map()
  let active = null
  let dirty = false
  let lastTree = null
  let lastComp = null
  let lastProps = null

  const shim = Object.assign({}, ReactReal)

  function scopeFor(comp) {
    let scope = scopes.get(comp)
    if (!scope) {
      scope = { cells: [], refs: [], cursor: 0, pending: [], previous: [] }
      scopes.set(comp, scope)
    }
    return scope
  }

  shim.useState = function (initial) {
    if (!active) return ReactReal.useState(initial)
    const scope = active
    const index = scope.cursor++
    if (!(index in scope.cells)) scope.cells[index] = typeof initial === 'function' ? initial() : initial
    return [
      scope.cells[index],
      function (value) {
        const next = typeof value === 'function' ? value(scope.cells[index]) : value
        if (next !== scope.cells[index]) {
          scope.cells[index] = next
          dirty = true
        }
      },
    ]
  }
  shim.useEffect = function (fn, deps) {
    if (!active) return ReactReal.useEffect(fn, deps)
    const index = active.cursor++
    active.pending.push({ index, fn, deps: deps === undefined ? null : deps })
  }
  shim.useRef = function (initial) {
    if (!active) return ReactReal.useRef(initial)
    const index = active.cursor++
    if (!(index in active.refs)) active.refs[index] = { current: initial }
    return active.refs[index]
  }
  shim.useCallback = function (fn) {
    return active ? fn : ReactReal.useCallback(fn)
  }
  shim.useMemo = function (factory) {
    return active ? factory() : ReactReal.useMemo(factory)
  }

  function render(comp, props) {
    const scope = scopeFor(comp)
    lastComp = comp
    lastProps = props
    scope.cursor = 0
    scope.pending = []
    active = scope
    try {
      lastTree = comp(props)
    } finally {
      active = null
    }
    return lastTree
  }

  function runEffects() {
    if (!lastComp) return
    const scope = scopeFor(lastComp)
    const next = []
    for (const eff of scope.pending) {
      const before = scope.previous.find((item) => item.index === eff.index)
      let changed = !before
      if (before) {
        const a = before.deps
        const b = eff.deps
        if (!a || !b) changed = true
        else if (a.length !== b.length) changed = true
        else changed = a.some((value, index) => value !== b[index])
      }
      if (changed) {
        if (before && typeof before.cleanup === 'function') {
          try {
            before.cleanup()
          } catch {
            /* ignore */
          }
        }
        const cleanup = eff.fn()
        next.push({ ...eff, cleanup: typeof cleanup === 'function' ? cleanup : null })
      } else {
        next.push({ ...eff, cleanup: before.cleanup })
      }
    }
    scope.previous = next
  }

  async function drain() {
    for (let round = 0; round < 40; round += 1) {
      dirty = false
      runEffects()
      // 给 effect 里 await fetch 的续体让路
      await new Promise((resolve) => setTimeout(resolve, 0))
      await new Promise((resolve) => setImmediate(resolve))
      if (dirty) {
        render(lastComp, lastProps)
        continue
      }
      break
    }
    return lastTree
  }

  /** 首次挂载某组件:清掉它的 scope(模拟全新实例) */
  async function start(comp, props) {
    scopes.delete(comp)
    render(comp, props)
    return drain()
  }

  /** 已有实例再渲染一次(组件内部 state 保留) */
  async function reRender(comp, props) {
    render(comp, props)
    return drain()
  }

  async function click(label) {
    const buttons = findAll(lastTree, (node) => node.type === 'button')
    const labels = buttons.map((node) => flattenText(node).trim())
    const target = buttons.find((node) => flattenText(node).trim() === label)
    if (!target) throw new Error('找不到按钮「' + label + '」，现有: ' + labels.join(' | '))
    if (target.props.disabled) throw new Error('按钮「' + label + '」是 disabled 状态')
    target.props.onClick()
    // 立刻渲染一次:捕捉「测试中… / 保存中… / 探测中…」这类中间态
    const midTree = render(lastComp, lastProps)
    const finalTree = await drain()
    return { midTree, finalTree }
  }

  return { shim, start, reRender, click, getTree: () => lastTree }
}

// ---- 极简 element 树遍历(不依赖 react-dom) ----
function flattenText(node) {
  if (node === null || node === undefined || node === true || node === false) return ''
  if (typeof node === 'string' || typeof node === 'number') return String(node)
  if (Array.isArray(node)) return node.map(flattenText).join('')
  if (typeof node === 'object' && node.props) return flattenText(node.props.children)
  return ''
}
function findAll(tree, predicate) {
  const out = []
  const visit = (node) => {
    if (!node || typeof node !== 'object') return
    if (Array.isArray(node)) {
      for (const child of node) visit(child)
      return
    }
    if (predicate(node)) out.push(node)
    visit(node.props && node.props.children)
  }
  visit(tree)
  return out
}
function inputsOf(tree) {
  return findAll(tree, (node) => node.type === 'input' || node.type === 'select')
}
function buttonsOf(tree) {
  return findAll(tree, (node) => node.type === 'button').map((node) => flattenText(node).trim())
}
function applyFetch(captured) {
  const registered = []
  const injected = []
  const effects = []
  const ctx = {
    slots: {
      inject(name, cb) {
        injected.push(name)
        return cb({
          register(opts, comp) {
            registered.push({ opts, comp })
            return comp
          },
        })
      },
      register(opts, comp) {
        registered.push({ opts, comp })
        return comp
      },
    },
    effect(fn) {
      const dispose = fn()
      effects.push(dispose)
      return dispose
    },
  }
  captured.apply(ctx)
  return { registered, injected, effects }
}

// ---------------------------------------------------------------------------
// main
// ---------------------------------------------------------------------------
async function main() {
  const pair = resolveReactPair()
  if (!pair) {
    console.error('\n[FATAL] 找不到可用且版本配对的 react / react-dom,无法渲染检查')
    process.exit(1)
  }
  console.log('[env] ' + pair.label + '  react=' + pair.reactVersion + '  react-dom=' + pair.domVersion)
  console.log('[env] client.js = ' + CLIENT)

  const { captured, appended, fakeWindow, fakeDocument } = createFakeBrowser()
  const state = { config: baseConfig() }
  const fakeFetch = makeFakeFetch(state)

  const src = fs.readFileSync(CLIENT, 'utf8')
  globalThis.window = fakeWindow
  new Function('window', 'document', 'fetch', 'console', src)(fakeWindow, fakeDocument, fakeFetch, console)

  // -------------------------------------------------------------------------
  section('1. 模块加载 / factory 无副作用 / slot 注册')

  check('window.__ModuleLoader__.load 被调用', captured.module !== null)
  const mod = captured.module
  check('module.id === dsh-videokit', mod && mod.id === 'dsh-videokit', mod && mod.id)
  check('factory 是函数', mod && typeof mod.factory === 'function')
  check('factory 无副作用:未发请求', fetchCalls.length === 0, fetchCalls.length)
  check('factory 无副作用:未写 DOM', appended.length === 0, appended.length)

  const exported = mod.factory((name) => (name === 'react' ? pair.React : {}))
  check('导出对象含 inject', exported && Array.isArray(exported.inject), exported && JSON.stringify(exported.inject))
  check("inject === ['slots']", exported && exported.inject.length === 1 && exported.inject[0] === 'slots')
  check('导出对象含 apply 函数', exported && typeof exported.apply === 'function')

  // 静态渲染用真实 React,拿注册出来的面板组件
  let staticPanel = null
  const staticCtx = {
    slots: {
      inject(name, cb) {
        return cb({
          register(opts, comp) {
            staticPanel = comp
            return comp
          },
        })
      },
      register(opts, comp) {
        staticPanel = comp
        return comp
      },
    },
    effect(fn) {
      return fn()
    },
  }
  exported.apply(staticCtx)
  check('面板组件已生成', typeof staticPanel === 'function')

  // apply 的注册行为单独验证一次(记录 opts / style / effect)
  const reg = applyFetch(exported)
  const opts = reg.registered[0] && reg.registered[0].opts
  check('注册了 1 个 settings.section', reg.registered.length === 1, reg.registered.length)
  check('注入到 slot: settings.section', reg.injected[0] === 'settings.section', reg.injected[0])
  check('slot name === settings.section', opts && opts.name === 'settings.section', opts && opts.name)
  check('slot id === videokit', opts && opts.id === 'videokit', opts && opts.id)
  check('slot order === 26', opts && opts.order === 26, opts && opts.order)
  check('slot label === 视频理解', opts && opts.label === '视频理解', opts && opts.label)
  check('注入了一个 <style> 到 document.head', appended.length === 2, appended.length)
  const css = appended[0] ? appended[0].textContent : ''
  check('CSS 含 .vk- 前缀与 var(--dsw-alias-*) 变量', css.includes('.vk-panel') && css.includes('var(--dsw-alias-'), css.slice(0, 30))
  check('CSS 危险色带 --dsw-alias 兜底', css.includes('var(--dsw-alias-status-danger,var(--dsw-alias-state-error-primary,#d9534f))'))
  reg.effects.forEach((dispose) => typeof dispose === 'function' && dispose())
  check('ctx.effect 清理时移除 <style>', appended[1] && appended[1].removed === true)

  // -------------------------------------------------------------------------
  section('2. 静态渲染(初始 loading 态,无 props 依赖)')

  const html1 = pair.Server.renderToStaticMarkup(pair.React.createElement(staticPanel))
  check('HTML 含「语音识别」', html1.includes('语音识别'))
  check('HTML 含「画面理解」', html1.includes('画面理解'))
  check('HTML 含「文本汇总」', html1.includes('文本汇总'))
  check('HTML 含「全局」', html1.includes('全局'))
  check('HTML 含「自动探测」', html1.includes('自动探测'))
  check('HTML 含「测试连接」', html1.includes('测试连接'))
  check('HTML 含「每段分钟数」', html1.includes('每段分钟数'))
  check('HTML 含「工作目录」', html1.includes('工作目录'))
  check('HTML 含「API Key」+「认证方式」', html1.includes('API Key') && html1.includes('认证方式'))
  check('HTML 含提示 ~/.dsh/dsh-videokit.json', html1.includes('~/.dsh/dsh-videokit.json'))
  check('HTML 含「密钥明文保存在本机」', html1.includes('密钥明文保存在本机'))
  check('HTML 不含 undefined', !html1.includes('undefined'))
  console.log('\n  --- 渲染 HTML(前 600 字符)---\n' + html1.slice(0, 600) + '\n  ---')

  // -------------------------------------------------------------------------
  section('3. 配置载入:GET /videokit-api → 回填 → 再渲染')

  const driver = createDriver(pair.React)
  const mod2 = mod.factory((name) => (name === 'react' ? driver.shim : {}))
  const reg2 = applyFetch(mod2)
  // slot 注册的是 () => React.createElement(VideoKitPanel),要先解包出真正的面板组件
  const Panel = reg2.registered[0].comp({}).type
  check('解包出面板函数组件', typeof Panel === 'function')

  fetchCalls.length = 0
  const tree2 = await driver.start(Panel, {})
  const html2 = pair.Server.renderToStaticMarkup(tree2)
  check(
    '挂载时发了 1 次 GET /videokit-api',
    fetchCalls.length === 1 && fetchCalls[0].method === 'GET' && fetchCalls[0].url === '/videokit-api',
    JSON.stringify(fetchCalls.map((call) => call.method + ' ' + call.url)),
  )
  check('回填后 HTML 含脱敏 asr key(sk-****abcd)', html2.includes('sk-****abcd'))
  check('回填后 HTML 含脱敏 vision key(vk-****wxyz)', html2.includes('vk-****wxyz'))
  check('回填后 HTML 的三个输入框已回填接口地址', html2.includes('value="https://api.example.com/v1"'))
  check('回填后 HTML 含 chunkMinutes=15', html2.includes('value="15"'))
  check('回填后 HTML 含工作目录', html2.includes('value="D:/VideoKit/demo"'))
  check('回填后 HTML 含探测结果行 + 探测到的 python', html2.includes('探测结果') && html2.includes('C:/Python311/python.exe'))
  check('回填后 HTML 不含 undefined', !html2.includes('undefined'))
  console.log('\n  --- 配置载入后的 HTML(前 600 字符)---\n' + html2.slice(0, 600) + '\n  ---')

  // 面板树里只有 SectionCard 组件元素,内部 input 要从 SSR 出来的 HTML 里数
  const passwordTags = html2.match(/<input type="password"[^>]*>/g) || []
  check('3 个 API Key 输入框 type=password', passwordTags.length === 3, passwordTags.length)
  check('API Key 输入框的值都是空字符串', passwordTags.every((tag) => tag.includes('value=""')), passwordTags.join(' || '))
  const placeholders = passwordTags.map((tag) => (tag.match(/placeholder="([^"]*)"/) || [])[1] || '')
  check(
    'API Key placeholder 显示三组脱敏串',
    placeholders[0].includes(AES) && placeholders[1].includes(VES) && placeholders[2].includes(LES),
    placeholders.join(' || '),
  )

  // -------------------------------------------------------------------------
  section('4. 交互模拟:测试连接 / 保存 / 清除 / 自动探测')

  const cardEls = findAll(tree2, (node) => node && node.props && node.props.def && node.props.cfg)
  check('panel 树里找到 4 张卡片元素', cardEls.length === 4, cardEls.length)
  const SectionCard = cardEls[0].type
  const asrProps = cardEls.find((node) => node.props.def.key === 'asr').props
  const globalProps = cardEls.find((node) => node.props.def.key === 'global').props
  check('SectionCard 是函数组件', typeof SectionCard === 'function')

  // 4.1 测试连接
  fetchCalls.length = 0
  await driver.start(SectionCard, { ...asrProps })
  let clicked = await driver.click('测试连接')
  const testCall = fetchCalls.find((call) => call.body && call.body.op === 'test')
  check('点「测试连接」发 POST op=test section=asr', !!testCall && testCall.body.section === 'asr', testCall && JSON.stringify(testCall.body))
  check('测试中按钮文案变「测试中…」', flattenText(clicked.midTree).includes('测试中…'), buttonsOf(clicked.midTree).join('|'))
  check('测试中按钮 disabled', findAll(clicked.midTree, (node) => node.type === 'button' && flattenText(node).trim() === '测试中…').every((node) => node.props.disabled === true))
  check('test draft.key 未动时是空串(host 保留原 key)', testCall && testCall.body.draft.key === '', testCall && JSON.stringify(testCall.body.draft))
  check(
    'test draft.base/model/auth 与已保存配置一致',
    testCall && testCall.body.draft.base === 'https://api.example.com/v1' && testCall.body.draft.model === 'mimo-v2.5-asr' && testCall.body.draft.auth === 'api-key',
    testCall && JSON.stringify(testCall.body.draft),
  )
  const testText = flattenText(clicked.finalTree)
  check('结果就地显示「✓ 812 ms · mimo-v2.5-asr」', testText.includes('✓ 812 ms · mimo-v2.5-asr'), testText.slice(-100))
  check('结果节点带 vk-ok 类(绿色)', findAll(clicked.finalTree, (node) => String((node.props && node.props.className) || '').includes('vk-ok')).length === 1)

  // 4.2 保存(key 未动 → 空串;chunkMinutes 必须是数字)
  fetchCalls.length = 0
  clicked = await driver.click('保存')
  const saveCall = fetchCalls.find((call) => call.body && call.body.op === 'save')
  const patch = saveCall && saveCall.body.patch
  check('保存后就地显示「✓ 已保存」', flattenText(clicked.finalTree).includes('✓ 已保存'), flattenText(clicked.finalTree).slice(-100))
  check('save patch 只含 asr 一组', patch && Object.keys(patch).join(',') === 'asr', patch && Object.keys(patch).join(','))
  check('save patch.asr.key === ""(不覆盖已保存 key)', patch && patch.asr.key === '', patch && JSON.stringify(patch))
  check('save patch.asr.chunkMinutes 是数字 15', patch && patch.asr.chunkMinutes === 15, patch && patch.asr.chunkMinutes)
  check('save patch.asr.model/auth/base 齐全', patch && patch.asr.model === 'mimo-v2.5-asr' && patch.asr.auth === 'api-key' && patch.asr.base === 'https://api.example.com/v1', patch && JSON.stringify(patch.asr))

  // 4.3 清除 key → 保存 → patch.key === '__clear__'
  fetchCalls.length = 0
  clicked = await driver.click('清除')
  check('点「清除」后出「已标记清除」提示', flattenText(clicked.finalTree).includes('已标记清除'), flattenText(clicked.finalTree).slice(-120))
  const clearedKeyInput = findAll(clicked.finalTree, (node) => node.type === 'input' && node.props.type === 'password')[0]
  check('清除后 placeholder 变「保存后清空」', String(clearedKeyInput.props.placeholder) === '保存后清空', clearedKeyInput.props.placeholder)
  check('清除按钮变「撤销清除」', buttonsOf(clicked.finalTree).includes('撤销清除'), buttonsOf(clicked.finalTree).join('|'))
  clicked = await driver.click('保存')
  const clearCall = fetchCalls.find((call) => call.body && call.body.op === 'save')
  check('清除后保存 patch.asr.key === "__clear__"', clearCall && clearCall.body.patch.asr.key === '__clear__', clearCall && JSON.stringify(clearCall.body.patch))

  // 4.4 手输 key → 测试:草稿里的 key 进 test draft
  fetchCalls.length = 0
  const keyInput = findAll(driver.getTree(), (node) => node.type === 'input' && node.props.type === 'password')[0]
  keyInput.props.onChange({ target: { value: 'sk-typed-key-0001' } })
  await driver.reRender(SectionCard, { ...asrProps })
  clicked = await driver.click('测试连接')
  const typedTest = fetchCalls.find((call) => call.body && call.body.op === 'test')
  check('手输的 key 进入 test draft', typedTest && typedTest.body.draft.key === 'sk-typed-key-0001', typedTest && JSON.stringify(typedTest.body.draft))

  // 4.5 每段分钟数非法 → 就地报红,不发 save
  fetchCalls.length = 0
  const numberInput = findAll(driver.getTree(), (node) => node.type === 'input' && node.props.type === 'number')[0]
  check('语音识别卡有 number 输入框', !!numberInput)
  numberInput.props.onChange({ target: { value: '999' } })
  await driver.reRender(SectionCard, { ...asrProps })
  clicked = await driver.click('保存')
  check('999 被拦下(不发 save)', fetchCalls.filter((call) => call.body && call.body.op === 'save').length === 0, JSON.stringify(fetchCalls.map((call) => call.body && call.body.op)))
  check('非法值就地报红', flattenText(clicked.finalTree).includes('每段分钟数需为 1–600 的整数'), flattenText(clicked.finalTree).slice(-100))

  // 4.6 全局卡:自动探测 → 回填空输入框 + 就地结果
  fetchCalls.length = 0
  await driver.start(SectionCard, { ...globalProps })
  const detectClicked = await driver.click('自动探测')
  const detectCall = fetchCalls.find((call) => call.body && call.body.op === 'detect')
  check('点「自动探测」发 POST op=detect', !!detectCall, JSON.stringify(fetchCalls.map((call) => call.body && call.body.op)))
  check('探测中按钮文案变「探测中…」', flattenText(detectClicked.midTree).includes('探测中…'), buttonsOf(detectClicked.midTree).join('|'))
  check('探测完成后就地显示「✓ 探测完成」', flattenText(detectClicked.finalTree).includes('✓ 探测完成'))
  const globalValues = inputsOf(detectClicked.finalTree).map((node) => node.props.value)
  check('探测到的 python 回填进空输入框', globalValues.includes('C:/Python311/python.exe'), JSON.stringify(globalValues))
  check('探测到的 ffmpeg/ffprobe 回填进空输入框', globalValues.includes('C:/ffmpeg/bin/ffmpeg.exe') && globalValues.includes('C:/ffmpeg/bin/ffprobe.exe'), JSON.stringify(globalValues))

  // 4.7 全局卡保存:顶层字段(不是打包成 global:{...})
  fetchCalls.length = 0
  clicked = await driver.click('保存')
  const globalSave = fetchCalls.find((call) => call.body && call.body.op === 'save')
  check('全局保存 patch 是顶层字段', !!globalSave && Object.keys(globalSave.body.patch).includes('pythonPath'), globalSave && JSON.stringify(globalSave.body.patch))
  check('全局保存写入探测到的 python 路径', globalSave && globalSave.body.patch.pythonPath === 'C:/Python311/python.exe', globalSave && globalSave.body.patch.pythonPath)
  check('全局保存保留原有工作目录', globalSave && globalSave.body.patch.workdir === 'D:/VideoKit/demo', globalSave && globalSave.body.patch.workdir)

  // 4.8 失败路径:HTTP 401 / 网络异常 → 卡片内红色提示,面板不崩
  state.failTest = true
  fetchCalls.length = 0
  await driver.start(SectionCard, { ...asrProps })
  clicked = await driver.click('测试连接')
  const failText = flattenText(clicked.finalTree)
  check('测试失败就地显示「✗ HTTP 401 — …」', failText.includes('✗ HTTP 401 — invalid api key'), failText.slice(-100))
  check('失败结果带 vk-err 类(红色)', findAll(clicked.finalTree, (node) => String((node.props && node.props.className) || '').includes('vk-err')).length === 1)
  state.failTest = false
  state.throwOnTest = true
  await driver.start(SectionCard, { ...asrProps })
  clicked = await driver.click('测试连接')
  check('网络异常被 catch 成红色提示', flattenText(clicked.finalTree).includes('✗ 请求失败：Failed to fetch'), flattenText(clicked.finalTree).slice(-100))
  check('网络异常后卡片仍完整(按钮还在)', buttonsOf(clicked.finalTree).includes('保存'), buttonsOf(clicked.finalTree).join('|'))
  state.throwOnTest = false

  // 4.9 保存成功 → onSaved 带回脱敏 config;面板把 savedTick+1 传下来后本卡草稿重置
  const savedCalls = []
  const propsWithSpy = { ...asrProps, onSaved: (section, nextCfg) => savedCalls.push({ section, nextCfg }) }
  fetchCalls.length = 0
  await driver.start(SectionCard, propsWithSpy)
  const typedKeyInput = findAll(driver.getTree(), (node) => node.type === 'input' && node.props.type === 'password')[0]
  typedKeyInput.props.onChange({ target: { value: 'sk-fresh-key-9999' } })
  await driver.reRender(SectionCard, propsWithSpy)
  clicked = await driver.click('保存')
  check('保存成功回调 onSaved(section, config)', savedCalls.length === 1 && savedCalls[0].section === 'asr', savedCalls.length)
  check(
    '回调 config 里 key 已脱敏(不含明文)',
    savedCalls[0] && !JSON.stringify(savedCalls[0].nextCfg).includes('sk-fresh-key-9999') && String(savedCalls[0].nextCfg.asr.key).includes('****'),
    JSON.stringify(savedCalls[0] && savedCalls[0].nextCfg.asr),
  )
  await driver.reRender(SectionCard, { ...propsWithSpy, cfg: savedCalls[0].nextCfg, savedTick: (propsWithSpy.savedTick || 0) + 1 })
  const afterSaveInput = findAll(driver.getTree(), (node) => node.type === 'input' && node.props.type === 'password')[0]
  check('保存后 key 输入框被清空', afterSaveInput.props.value === '', afterSaveInput.props.value)
  check('保存后 placeholder 换成新的脱敏串', String(afterSaveInput.props.placeholder).includes('sk-****9999'), afterSaveInput.props.placeholder)

  // -------------------------------------------------------------------------
  section('5. 网络故障:GET 失败仍有完整四张卡片(不白屏)')

  const brokenBrowser = createFakeBrowser()
  const brokenFetch = async () => {
    throw new Error('Failed to fetch')
  }
  new Function('window', 'document', 'fetch', 'console', src)(brokenBrowser.fakeWindow, brokenBrowser.fakeDocument, brokenFetch, console)
  const brokenMod = brokenBrowser.captured.module
  const brokenDriver = createDriver(pair.React)
  const brokenReg = applyFetch(brokenMod.factory((name) => (name === 'react' ? brokenDriver.shim : {})))
  const BrokenPanel = brokenReg.registered[0].comp({}).type
  const brokenTree = await brokenDriver.start(BrokenPanel, {})
  const brokenHtml = pair.Server.renderToStaticMarkup(brokenTree)
  check('GET 网络错误显示红色 banner', brokenHtml.includes('无法连接 /videokit-api') && brokenHtml.includes('vk-banner'), brokenHtml.slice(0, 160))
  check('网络错误时面板不白屏(四张卡片都在)', ['语音识别', '画面理解', '文本汇总', '全局', '自动探测'].every((text) => brokenHtml.includes(text)))
  check('网络错误时给「重试」按钮', brokenHtml.includes('重试'))
  check('网络错误 HTML 不含 undefined', !brokenHtml.includes('undefined'))

  // -------------------------------------------------------------------------
  section('结果')
  console.log('  通过 ' + passed.length + ' 项,失败 ' + failed.length + ' 项')
  if (failed.length) {
    for (const item of failed) console.log('  ✗ ' + item.name + (item.detail === undefined ? '' : ' → ' + String(item.detail)))
    process.exit(1)
  }
  process.exit(0)
}

main().catch((error) => {
  console.error('\n[FATAL] ' + (error && error.stack ? error.stack : error))
  process.exit(1)
})
