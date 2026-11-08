/**
 * dsh-videokit — 设置页「视频理解」面板(Client 半边)。
 *
 * 以 dsh.client bundle 格式加载:注册 settings.section,渲染四张卡片
 * (语音识别 / 画面理解 / 文本汇总 / 全局),数据全部走 /videokit-api。
 *
 * 约定:
 *   - 无构建步骤、无 JSX、无 import;factory 内无副作用(不改 DOM、不发请求)。
 *   - 副作用只放在 apply(ctx):注入 <style>、注册 slot;样式用 ctx.effect 清理。
 *   - API Key 只以脱敏串出现在 placeholder 里,输入框值恒为空;
 *     用户不动就提交空字符串(host 保持原 key),点「清除」提交 __clear__。
 */
window.__ModuleLoader__.load({
  id: 'dsh-videokit',
  factory: function (require) {
    var React = require('react')
    var h = React.createElement

    var API_PATH = '/videokit-api'
    var CLEAR_KEY = '__clear__'

    var AUTH_OPTIONS = [
      { value: 'api-key', label: 'api-key（小米 MiMo）' },
      { value: 'bearer', label: 'Bearer' },
    ]

    // 四张卡片全部由这份配置驱动:global 是顶层字段(top),其余是嵌套 section。
    // detectKeys 把「自动探测」返回的 detected 字段映射到输入框。
    var SECTIONS = [
      {
        key: 'asr',
        title: '语音识别',
        sub: '音轨 → 带时间戳的语音事件',
        auth: 'api-key',
        fields: [
          {
            name: 'base',
            label: '接口地址',
            type: 'text',
            span: 2,
            placeholder: 'https://api.example.com/v1（结尾会自动补 /chat/completions）',
          },
          { name: 'key', label: 'API Key', type: 'key', span: 2 },
          { name: 'model', label: '模型名', type: 'text', span: 1, placeholder: 'mimo-v2.5-asr' },
          { name: 'auth', label: '认证方式', type: 'select', span: 1 },
          {
            name: 'chunkMinutes',
            label: '每段分钟数',
            type: 'number',
            span: 1,
            min: 1,
            max: 600,
            dflt: 5,
            hint: '默认 5 分钟。太长会被模型悄悄跳读（实测 10 分钟起就丢内容）；'
              + '估算超过 9.5MB 会自动减半',
          },
        ],
      },
      {
        key: 'vision',
        title: '画面理解',
        sub: '关键帧 → 画面描述与画面文字',
        auth: 'bearer',
        fields: [
          { name: 'base', label: '接口地址', type: 'text', span: 2, placeholder: 'https://api.example.com/v1' },
          { name: 'key', label: 'API Key', type: 'key', span: 2 },
          { name: 'model', label: '模型名', type: 'text', span: 1, placeholder: '视觉模型名' },
          { name: 'auth', label: '认证方式', type: 'select', span: 1 },
        ],
      },
      {
        key: 'llm',
        title: '文本汇总',
        sub: '时间线 → 结构化事实（map-reduce）',
        auth: 'bearer',
        fields: [
          { name: 'base', label: '接口地址', type: 'text', span: 2, placeholder: 'https://api.example.com/v1' },
          { name: 'key', label: 'API Key', type: 'key', span: 2 },
          { name: 'model', label: '模型名', type: 'text', span: 1, placeholder: '文本模型名' },
          { name: 'auth', label: '认证方式', type: 'select', span: 1 },
        ],
      },
      {
        key: 'global',
        top: true,
        detect: true,
        title: '全局',
        sub: 'Python、ffmpeg 与工作目录',
        fields: [
          {
            name: 'pythonPath',
            label: 'Python 路径',
            type: 'text',
            span: 2,
            detectKey: 'python',
            placeholder: '绝对路径；留空则用 PATH / 自动探测到的 python',
          },
          {
            name: 'ffmpeg',
            label: 'ffmpeg 路径',
            type: 'text',
            span: 2,
            detectKey: 'ffmpeg',
            placeholder: '绝对路径；留空则用 PATH / 自动探测到的 ffmpeg',
          },
          {
            name: 'ffprobe',
            label: 'ffprobe 路径',
            type: 'text',
            span: 2,
            detectKey: 'ffprobe',
            placeholder: '绝对路径；留空则用 PATH / 自动探测到的 ffprobe',
          },
          {
            name: 'workdir',
            label: '工作目录',
            type: 'text',
            span: 2,
            placeholder: '索引产物默认写在这里（绝对路径，不存在会自动创建）',
          },
        ],
      },
    ]

    var CSS = [
      '.vk-panel{display:flex;flex-direction:column;gap:12px;font-size:13px;line-height:1.5;color:var(--dsw-alias-label-primary)}',
      '.vk-hint,.vk-foot{font-size:12px;color:var(--dsw-alias-label-tertiary)}',
      '.vk-card{display:flex;flex-direction:column;gap:10px;padding:12px 14px;border:1px solid var(--dsw-alias-settings-card-stroke,var(--dsw-alias-border-l1));border-radius:10px;background:var(--dsw-alias-settings-card-fill,var(--dsw-alias-bg-layer-1))}',
      '.vk-card-head{display:flex;align-items:baseline;gap:8px;flex-wrap:wrap}',
      '.vk-card-title{font-size:13px;font-weight:600;color:var(--dsw-alias-label-primary)}',
      '.vk-card-sub{font-size:12px;color:var(--dsw-alias-label-tertiary)}',
      '.vk-grid{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:10px}',
      '.vk-field{display:flex;flex-direction:column;gap:4px;min-width:0}',
      '.vk-span2{grid-column:1 / -1}',
      '.vk-label{font-size:12px;color:var(--dsw-alias-label-secondary)}',
      '.vk-label-hint{font-weight:400;color:var(--dsw-alias-label-tertiary)}',
      '.vk-input{box-sizing:border-box;width:100%;padding:6px 8px;font-size:13px;font-family:inherit;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:6px;outline:none}',
      '.vk-input::placeholder{color:var(--dsw-alias-label-quaternary)}',
      '.vk-input:focus{border-color:var(--dsw-alias-brand-primary)}',
      '.vk-key-row{display:flex;align-items:center;gap:6px}',
      '.vk-key-row .vk-input{flex:1 1 auto;min-width:0}',
      '.vk-btn{flex:0 0 auto;padding:5px 10px;font-size:12px;font-family:inherit;color:var(--dsw-alias-label-primary);background:var(--dsw-alias-bg-layer-2);border:1px solid var(--dsw-alias-border-l1);border-radius:6px;cursor:pointer}',
      '.vk-btn:hover:not(:disabled){border-color:var(--dsw-alias-brand-primary)}',
      '.vk-btn:disabled{opacity:.55;cursor:default}',
      '.vk-btn-main{color:var(--dsw-alias-label-primary-inverted,#fff);background:var(--dsw-alias-button-primary-fill,var(--dsw-alias-brand-primary));border-color:transparent}',
      '.vk-acts{display:flex;align-items:center;gap:8px;flex-wrap:wrap}',
      '.vk-result{font-size:12px;word-break:break-all}',
      '.vk-ok{color:var(--dsw-alias-status-success,var(--dsw-alias-state-success-primary,#2e7d32))}',
      '.vk-err{color:var(--dsw-alias-status-danger,var(--dsw-alias-state-error-primary,#d9534f))}',
      '.vk-note{font-size:12px;color:var(--dsw-alias-state-warn-primary,#b26a00)}',
      '.vk-detected{font-size:12px;color:var(--dsw-alias-label-tertiary);word-break:break-all}',
      '.vk-banner{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:8px 10px;font-size:12px;color:var(--dsw-alias-status-danger,var(--dsw-alias-state-error-primary,#d9534f));background:var(--dsw-alias-bg-layer-1);border:1px solid var(--dsw-alias-status-danger,var(--dsw-alias-state-error-primary,#d9534f));border-radius:8px}',
      '@media (max-width:520px){.vk-grid{grid-template-columns:minmax(0,1fr)}}',
    ].join('')

    // ---- 纯函数:配置 ↔ 表单草稿 ----

    function str(value) {
      return value === null || value === undefined ? '' : String(value)
    }

    function errText(error) {
      if (error && typeof error.message === 'string' && error.message) return error.message
      return String(error)
    }

    function sectionSource(def, cfg) {
      if (!cfg) return {}
      if (def.top) return cfg
      var sec = cfg[def.key]
      return sec && typeof sec === 'object' ? sec : {}
    }

    /** 从配置生成草稿。key 永远留空(只显示脱敏 placeholder),clearKey 标记显式清除。 */
    function draftFrom(def, cfg) {
      var src = sectionSource(def, cfg)
      var draft = { key: '', clearKey: false }
      for (var i = 0; i < def.fields.length; i += 1) {
        var f = def.fields[i]
        if (f.type === 'key') continue
        if (f.type === 'select') {
          draft[f.name] = str(src[f.name]) || (f.dflt || def.auth || 'bearer')
          continue
        }
        if (f.type === 'number') {
          var n = src[f.name]
          draft[f.name] = n === null || n === undefined || n === '' ? str(f.dflt === undefined ? '' : f.dflt) : str(n)
          continue
        }
        draft[f.name] = str(src[f.name])
      }
      return draft
    }

    function maskedKeyOf(def, cfg) {
      if (def.top) return ''
      var sec = cfg && cfg[def.key]
      return sec && typeof sec === 'object' ? str(sec.key) : ''
    }

    /** 测试用 draft:不带数字字段,key 未动就是空串(host 回落到已保存的 key)。 */
    function draftForTest(def, draft) {
      var out = {}
      for (var i = 0; i < def.fields.length; i += 1) {
        var f = def.fields[i]
        if (f.type === 'number') continue
        if (f.type === 'key') {
          out.key = draft.clearKey ? '' : str(draft.key)
          continue
        }
        out[f.name] = str(draft[f.name]).trim()
      }
      return out
    }

    /** 保存用 patch:global 是顶层字段,其余包成 {asr:{...}} 之类的深合并一层形状。 */
    function buildPatch(def, draft) {
      var target = {}
      for (var i = 0; i < def.fields.length; i += 1) {
        var f = def.fields[i]
        if (f.type === 'key') {
          target[f.name] = draft.clearKey ? CLEAR_KEY : str(draft.key)
          continue
        }
        if (f.type === 'number') {
          var n = parseInt(str(draft[f.name]), 10)
          if (!isFinite(n) || n < f.min || n > f.max) {
            return { error: f.label + '需为 ' + f.min + '–' + f.max + ' 的整数' }
          }
          target[f.name] = n
          continue
        }
        target[f.name] = str(draft[f.name]).trim()
      }
      if (def.top) return { patch: target }
      var patch = {}
      patch[def.key] = target
      return { patch: patch }
    }

    function mergeConfig(prev, next) {
      if (!next || typeof next !== 'object') return prev
      var out = Object.assign({}, prev || {}, next)
      var keys = ['asr', 'vision', 'llm']
      for (var i = 0; i < keys.length; i += 1) {
        var k = keys[i]
        if (next[k] && typeof next[k] === 'object') {
          out[k] = Object.assign({}, (prev && prev[k]) || {}, next[k])
        }
      }
      return out
    }

    function detectedLine(detected) {
      if (!detected) return h('div', { className: 'vk-detected' }, '尚未探测：点「自动探测」查找 python / ffmpeg / ffprobe')
      var names = [
        { key: 'python', label: 'python' },
        { key: 'ffmpeg', label: 'ffmpeg' },
        { key: 'ffprobe', label: 'ffprobe' },
        { key: 'ytdlp', label: 'yt-dlp' },
      ]
      var parts = []
      for (var i = 0; i < names.length; i += 1) {
        var p = str(detected[names[i].key])
        parts.push(names[i].label + (p ? ' ' + p : ' 未找到'))
      }
      return h('div', { className: 'vk-detected' }, '探测结果：' + parts.join(' · '))
    }

    // ---- HTTP ----

    function parseJsonResponse(res) {
      if (!res) return null
      return res.json()
    }

    async function getConfig() {
      var res = await fetch(API_PATH, {
        method: 'GET',
        headers: { Accept: 'application/json' },
        cache: 'no-store',
      })
      if (!res || res.ok === false) throw new Error('HTTP ' + (res ? res.status : '?'))
      return parseJsonResponse(res)
    }

    async function postOp(payload) {
      var res = await fetch(API_PATH, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload),
      })
      if (!res || res.ok === false) throw new Error('HTTP ' + (res ? res.status : '?'))
      return parseJsonResponse(res)
    }

    // ---- 组件 ----

    function Field(def, f, draft, setDraft, masked) {
      var content = null
      if (f.type === 'key') {
        content = h(
          'div',
          { className: 'vk-key-row' },
          h('input', {
            type: 'password',
            className: 'vk-input',
            autoComplete: 'new-password',
            spellCheck: false,
            value: str(draft.key),
            placeholder: draft.clearKey ? '保存后清空' : masked ? masked + '（已保存，留空不改）' : '粘贴 API Key',
            onChange: function (e) {
              setDraft(Object.assign({}, draft, { key: e.target.value, clearKey: false }))
            },
          }),
          h(
            'button',
            {
              type: 'button',
              className: 'vk-btn',
              onClick: function () {
                setDraft(Object.assign({}, draft, { key: '', clearKey: !draft.clearKey }))
              },
            },
            draft.clearKey ? '撤销清除' : '清除',
          ),
        )
      } else if (f.type === 'select') {
        content = h(
          'select',
          {
            className: 'vk-input',
            value: str(draft[f.name]) || f.dflt || def.auth || 'bearer',
            onChange: function (e) {
              var next = Object.assign({}, draft)
              next[f.name] = e.target.value
              setDraft(next)
            },
          },
          AUTH_OPTIONS.map(function (opt) {
            return h('option', { key: opt.value, value: opt.value }, opt.label)
          }),
        )
      } else if (f.type === 'number') {
        content = h('input', {
          type: 'number',
          className: 'vk-input',
          min: f.min,
          max: f.max,
          step: 1,
          value: str(draft[f.name]),
          placeholder: str(f.dflt),
          onChange: function (e) {
            var next = Object.assign({}, draft)
            next[f.name] = e.target.value
            setDraft(next)
          },
        })
      } else {
        content = h('input', {
          type: 'text',
          className: 'vk-input',
          spellCheck: false,
          value: str(draft[f.name]),
          placeholder: str(f.placeholder),
          onChange: function (e) {
            var next = Object.assign({}, draft)
            next[f.name] = e.target.value
            setDraft(next)
          },
        })
      }

      var kids = [
        h('span', { className: 'vk-label' }, f.label, f.hint ? h('span', { className: 'vk-label-hint' }, ' · ' + f.hint) : null),
        content,
      ]
      if (f.type === 'key' && draft.clearKey) {
        kids.push(h('div', { className: 'vk-note' }, '已标记清除：保存后这张卡的 Key 会被清空'))
      }
      // 用变参传 children(.apply 展开),避免数组元素缺 key 的警告
      return h.apply(null, ['div', { key: f.name, className: 'vk-field' + (f.span === 2 ? ' vk-span2' : '') }].concat(kids))
    }

    function SectionCard(props) {
      var def = props.def
      var cfg = props.cfg
      var state = React.useState(function () {
        return draftFrom(def, cfg)
      })
      var draft = state[0]
      var setDraft = state[1]
      var busyState = React.useState('')
      var busy = busyState[0]
      var setBusy = busyState[1]
      var resultState = React.useState(null)
      var result = resultState[0]
      var setResult = resultState[1]

      // 只在「整页配置重新载入」或「本卡保存成功」时重置草稿,
      // 其它卡片保存不会冲掉这里正在输入的内容。
      React.useEffect(
        function () {
          setDraft(draftFrom(def, cfg))
        },
        [props.loadTick, props.savedTick],
      )

      var disabled = busy !== '' || !cfg

      function doTest() {
        setBusy('test')
        setResult(null)
        var body = { op: 'test', section: def.key, draft: draftForTest(def, draft) }
        postOp(body)
          .then(function (res) {
            if (res && res.ok) {
              var bits = []
              if (res.latencyMs !== null && res.latencyMs !== undefined) bits.push(res.latencyMs + ' ms')
              if (res.model) bits.push(str(res.model))
              setResult({ ok: true, text: '✓' + (bits.length ? ' ' + bits.join(' · ') : '') })
            } else {
              setResult({ ok: false, text: '✗ ' + ((res && res.error) || '测试失败') })
            }
            setBusy('')
          })
          .catch(function (error) {
            setResult({ ok: false, text: '✗ 请求失败：' + errText(error) })
            setBusy('')
          })
      }

      function doSave() {
        var built = buildPatch(def, draft)
        if (built.error) {
          setResult({ ok: false, text: '✗ ' + built.error })
          return
        }
        setBusy('save')
        setResult(null)
        postOp({ op: 'save', patch: built.patch })
          .then(function (res) {
            if (res && res.ok) {
              if (res.config) props.onSaved(def.key, res.config)
              setResult({ ok: true, text: '✓ 已保存' })
            } else {
              setResult({ ok: false, text: '✗ 保存失败：' + ((res && res.error) || '未知错误') })
            }
            setBusy('')
          })
          .catch(function (error) {
            setResult({ ok: false, text: '✗ 保存失败：' + errText(error) })
            setBusy('')
          })
      }

      function doDetect() {
        setBusy('detect')
        setResult(null)
        Promise.resolve(props.onDetect())
          .then(function (out) {
            if (!out || !out.detected) {
              setResult({ ok: false, text: '✗ 探测失败：' + ((out && out.error) || '未知错误') })
              setBusy('')
              return
            }
            var det = out.detected
            var next = Object.assign({}, draft)
            for (var i = 0; i < def.fields.length; i += 1) {
              var f = def.fields[i]
              if (f.detectKey && !str(next[f.name]).trim() && str(det[f.detectKey])) next[f.name] = str(det[f.detectKey])
            }
            setDraft(next)
            setResult({ ok: true, text: '✓ 探测完成' })
            setBusy('')
          })
          .catch(function (error) {
            setResult({ ok: false, text: '✗ 探测失败：' + errText(error) })
            setBusy('')
          })
      }

      var acts = []
      if (def.detect) {
        acts.push(
          h('button', { key: 'detect', type: 'button', className: 'vk-btn', disabled: disabled, onClick: doDetect }, busy === 'detect' ? '探测中…' : '自动探测'),
        )
      } else {
        acts.push(
          h('button', { key: 'test', type: 'button', className: 'vk-btn', disabled: disabled, onClick: doTest }, busy === 'test' ? '测试中…' : '测试连接'),
        )
      }
      acts.push(
        h('button', { key: 'save', type: 'button', className: 'vk-btn vk-btn-main', disabled: disabled, onClick: doSave }, busy === 'save' ? '保存中…' : '保存'),
      )
      if (result) {
        acts.push(h('span', { key: 'result', className: 'vk-result ' + (result.ok ? 'vk-ok' : 'vk-err') }, result.text))
      }

      var masked = maskedKeyOf(def, cfg)

      return h(
        'section',
        { className: 'vk-card' },
        h('div', { className: 'vk-card-head' }, h('span', { className: 'vk-card-title' }, def.title), h('span', { className: 'vk-card-sub' }, def.sub)),
        h(
          'div',
          { className: 'vk-grid' },
          def.fields.map(function (f) {
            return Field(def, f, draft, setDraft, masked)
          }),
        ),
        def.detect ? detectedLine(props.detected) : null,
        h.apply(null, ['div', { className: 'vk-acts' }].concat(acts)),
      )
    }

    function VideoKitPanel() {
      var cfgState = React.useState(null)
      var cfg = cfgState[0]
      var setCfg = cfgState[1]
      var detectedState = React.useState(null)
      var detected = detectedState[0]
      var setDetected = detectedState[1]
      var errState = React.useState('')
      var loadErr = errState[0]
      var setLoadErr = errState[1]
      var loadingState = React.useState(true)
      var loading = loadingState[0]
      var setLoading = loadingState[1]
      var tickState = React.useState(0)
      var loadTick = tickState[0]
      var setLoadTick = tickState[1]
      var savedState = React.useState({ asr: 0, vision: 0, llm: 0, global: 0 })
      var savedTicks = savedState[0]
      var setSavedTicks = savedState[1]

      async function load() {
        setLoading(true)
        setLoadErr('')
        try {
          var res = await getConfig()
          if (!res || res.ok === false) {
            setLoadErr('加载配置失败：' + ((res && res.error) || '未知错误'))
            setLoading(false)
            return
          }
          setCfg(mergeConfig(null, res.config || {}))
          setDetected(res.detected || null)
          setLoadTick(function (n) {
            return n + 1
          })
        } catch (error) {
          setLoadErr('无法连接 /videokit-api：' + errText(error))
        }
        setLoading(false)
      }

      React.useEffect(function () {
        load()
      }, [])

      function onSaved(section, nextCfg) {
        if (nextCfg) setCfg(mergeConfig(cfg, nextCfg))
        setSavedTicks(function (prev) {
          var next = { asr: prev.asr, vision: prev.vision, llm: prev.llm, global: prev.global }
          next[section] = (next[section] || 0) + 1
          return next
        })
      }

      function onDetect() {
        return postOp({ op: 'detect' })
          .then(function (res) {
            if (!res || res.ok === false) {
              return { error: (res && res.error) || '未知错误' }
            }
            var det = res.detected || {}
            setDetected(det)
            return { detected: det }
          })
          .catch(function (error) {
            return { error: errText(error) }
          })
      }

      var cards = SECTIONS.map(function (def) {
        return h(SectionCard, {
          key: def.key,
          def: def,
          cfg: cfg,
          detected: detected,
          loadTick: loadTick,
          savedTick: savedTicks[def.key] || 0,
          onSaved: onSaved,
          onDetect: onDetect,
        })
      })

      return h(
        'div',
        { className: 'vk-panel' },
        h('div', { className: 'vk-hint' }, '配置存在 ~/.dsh/dsh-videokit.json，密钥明文保存在本机'),
        loadErr
          ? h(
              'div',
              { className: 'vk-banner' },
              h('span', null, loadErr),
              h('button', { type: 'button', className: 'vk-btn', onClick: function () { load() } }, '重试'),
            )
          : null,
        loading && !cfg ? h('div', { className: 'vk-foot' }, '正在加载配置…') : null,
        cards,
      )
    }

    function apply(ctx) {
      var styleEl = document.createElement('style')
      styleEl.id = 'dsh-videokit-css'
      styleEl.textContent = CSS
      document.head.appendChild(styleEl)
      ctx.effect(function () {
        return function () {
          styleEl.remove()
        }
      })

      if (!ctx.slots || typeof ctx.slots.inject !== 'function') return
      ctx.slots.inject('settings.section', function () {
        return ctx.slots.register(
          { name: 'settings.section', id: 'videokit', order: 26, label: '视频理解' },
          function () {
            return React.createElement(VideoKitPanel)
          },
        )
      })
    }

    return {
      inject: ['slots'],
      apply: apply,
    }
  },
})
