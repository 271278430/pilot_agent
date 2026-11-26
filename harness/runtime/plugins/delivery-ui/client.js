window.__ModuleLoader__.load({
  id: '@pilot/delivery-ui',
  factory: require => {
    const React = require('react'), h = React.createElement
    const { useState, useEffect, useRef, useId, useSyncExternalStore } = React
    const ID = '@pilot/delivery-ui'
    const inject = ['slots', 'sidebarRightTabs', 'connection', 'uiWorkspace', 'sessions', 'layout', 'uiConversation']
    function apply(ctx) {
      const PLAN_RESULT = 'pilot-plan-result'
      const latestPlanVersions = new Map()
      // Read genuine retained tool results through the official per-Turn
      // assembler. Never synthesize assistant messages or attach the latest
      // application to unrelated older Turns.
      function readPlanResult(message) {
        if (message?.isError) return null
        for (const block of message?.content ?? []) {
          if (block.type !== 'text') continue
          try {
            const value = JSON.parse(block.text)
            if (value.requires_start === true && value.application?.id && value.application?.plan)
              return value.application
          } catch { /* Other tool content is not a plan. */ }
        }
        return null
      }
      ctx.effect(() => ctx.uiConversation.events.register({
        kind: PLAN_RESULT,
        match: event => ['turn/start', 'tool/call', 'tool/result'].includes(event.type)
          ? { id: String(event.data.turn), role: 'start' } : null,
        start: (_context, match) => planState({ calls: [], finishes: [], application: null, completed: false }, match.event),
        update: (context, match) => planState(context.state, match.event),
        buildLocationData: (context, scope, previous) => {
          if (scope !== 'turn' || (!context.state?.application && !context.state?.completed)) return null
          if (context.state.completed) return { kind: 'turn', turn: Number(context.id), key: PLAN_RESULT,
            value: { application: context.state.application, completed: true } }
          if (previous?.value.application === context.state.application) return previous
          return { kind: 'turn', turn: Number(context.id), key: PLAN_RESULT,
            value: { application: context.state.application } }
        },
      }))
      function planState(state, event) {
        if (event.type === 'tool/call' && event.data.name === 'pilot_finish')
          return { ...state, finishes: [...state.finishes, event.data.callId] }
        if (event.type === 'tool/result' && state.finishes.includes(event.data.message?.toolCallId)
          && !event.data.message.isError) {
          for (const block of event.data.message.content ?? []) {
            if (block.type !== 'text') continue
            try {
              const value = JSON.parse(block.text)
              if (value.ok === true && value.result?.passed === true)
                return { ...state, completed: true, application: value.application ?? null }
            } catch { /* Failed and unrelated tools cannot create completion. */ }
          }
        }
        if (event.type === 'tool/call' && ['pilot_plan', 'pilot_revise_plan', 'pilot_feedback'].includes(event.data.name))
          return { ...state, calls: [...state.calls, event.data.callId] }
        if (event.type === 'tool/result' && state.calls.includes(event.data.message?.toolCallId)) {
          const application = readPlanResult(event.data.message)
          if (application) {
            latestPlanVersions.set(application.id, Math.max(latestPlanVersions.get(application.id) ?? 1, application.state?.plan_version ?? 1))
            return { ...state, application }
          }
        }
        return state
      }
      async function rpc(operation, payload, signal) {
        const result = await ctx.connection.rpc.call('/pilot-api', operation, payload, signal)
        if (!result.ok) throw new Error(result.error.message)
        return result.value
      }
      // Domain controls use the Host theme plus Pilot's product color tokens;
      // editor, native Session state and layout navigation remain official.
      const styles = `
        .pilot-pane {
          --pilot-bg: var(--dsw-alias-bg-base, #fff);
          --pilot-ink: var(--dsw-alias-label-primary, #202124);
          --pilot-muted: var(--dsw-alias-label-secondary, #73777f);
          --pilot-line: var(--dsw-alias-border-l2, #e7e8eb);
          --pilot-accent: var(--dsw-alias-brand-primary-new-colorprimary-new-color, #4176e6);
          --pilot-soft: color-mix(in srgb, var(--pilot-ink) 4%, var(--pilot-bg));
          --pilot-success: var(--dsw-alias-state-success-primary, #17865b);
          --pilot-error: var(--dsw-alias-state-error-primary, #c43b38);
          container-type: inline-size;
          display: flex; flex-direction: column; height: 100%; min-height: 0;
          color: var(--pilot-ink); background: var(--pilot-bg);
          font-family: var(--dsw-font-family, 'Segoe UI', 'Microsoft YaHei', sans-serif);
          font-size: 14px; line-height: 1.6; overflow: hidden;
        }
        .pilot-plan-result { height: auto; overflow: visible; margin: 10px 0 18px;
          border: 1px solid var(--pilot-line); border-radius: 16px; padding: 20px;
          width: 100%; min-width: 0; }
        .pilot-plan-result .pilot-title { font-size: 20px; }
        .pilot-plan-result .pilot-row { margin-top: 12px; }
        .pilot-plan-result .pilot-section-note { margin-top: 8px !important; }
        .pilot-pane *, .pilot-pane *::before, .pilot-pane *::after { box-sizing: border-box; }
        .pilot-pane h2, .pilot-pane h3, .pilot-pane h4, .pilot-pane p { margin: 0; }
        .pilot-pane button, .pilot-pane input, .pilot-pane textarea { font: inherit; }
        .pilot-pane button { -webkit-tap-highlight-color: transparent; }
        .pilot-pane button:focus-visible, .pilot-pane input:focus-visible,
        .pilot-pane textarea:focus-visible, .pilot-pane summary:focus-visible {
          outline: 2px solid var(--pilot-accent); outline-offset: 3px;
        }
        .pilot-top { padding: 18px 22px 0; flex: none; }
        .pilot-brand { display: flex; align-items: center; gap: 10px; margin-bottom: 17px; }
        .pilot-brand-icon { display: grid; place-items: center; width: 34px; height: 34px;
          border-radius: 10px; background: var(--pilot-soft); color: var(--pilot-accent); flex: none; }
        .pilot-brand-title { font-size: 15px; font-weight: 600; line-height: 1.4; }
        .pilot-brand-note { color: var(--pilot-muted); font-size: 12px; }
        .pilot-nav { display: flex; gap: 4px; border-bottom: 1px solid var(--pilot-line); }
        .pilot-nav button { flex: 1; min-width: 0; border: 0; background: transparent;
          color: var(--pilot-muted); padding: 10px 4px 12px; cursor: pointer;
          position: relative; font-size: 13px; white-space: nowrap; }
        .pilot-nav button[aria-selected='true'] { color: var(--pilot-ink); font-weight: 600; }
        .pilot-nav button[aria-selected='true']::after { content: ''; position: absolute;
          bottom: -1px; left: 22%; right: 22%; height: 2px; background: var(--pilot-ink); border-radius: 2px; }
        .pilot-nav button:hover { color: var(--pilot-ink); }
        .pilot-scroll { flex: 1; min-height: 0; overflow-y: auto; padding: 22px;
          scrollbar-gutter: stable; overscroll-behavior: contain; }
        .pilot-content { max-width: 760px; margin: 0 auto; }
        .pilot-stack { display: flex; flex-direction: column; gap: 20px; }
        .pilot-row { display: flex; align-items: center; gap: 10px; flex-wrap: wrap; }
        .pilot-spread { justify-content: space-between; }
        .pilot-eyebrow { font-size: 12px; color: var(--pilot-muted); }
        .pilot-title { font-size: 23px; font-weight: 600; letter-spacing: -.4px; line-height: 1.45;
          overflow-wrap: anywhere; margin: 9px 0 !important; }
        .pilot-description { color: var(--pilot-muted); overflow-wrap: anywhere; }
        .pilot-section-title { font-size: 14px; font-weight: 600; }
        .pilot-section-note { color: var(--pilot-muted); font-size: 12px; margin-top: 3px !important; }
        .pilot-card { border: 1px solid var(--pilot-line); border-radius: 14px; padding: 17px 18px; }
        .pilot-card + .pilot-card { margin-top: 10px; }
        .pilot-btn { display: inline-flex; align-items: center; justify-content: center; gap: 7px;
          border: 1px solid var(--pilot-line); border-radius: 9px; padding: 8px 13px;
          background: var(--pilot-bg); color: var(--pilot-ink); cursor: pointer; line-height: 1.4;
          transition: background .15s, border-color .15s; text-align: center; }
        .pilot-btn:hover:not(:disabled) { background: var(--pilot-soft); }
        .pilot-btn-primary { background: var(--pilot-accent); color: #fff; border-color: var(--pilot-accent); }
        .pilot-btn-primary:hover:not(:disabled) { background: color-mix(in srgb, var(--pilot-accent) 88%, #000); }
        .pilot-btn-quiet { border-color: transparent; color: var(--pilot-muted); padding: 6px 9px; }
        .pilot-btn-danger { color: var(--pilot-error); }
        .pilot-btn:disabled { opacity: .45; cursor: default; }
        .pilot-badge { display: inline-flex; align-items: center; gap: 6px; font-size: 12px;
          color: var(--pilot-muted); background: var(--pilot-soft); border-radius: 6px;
          padding: 3px 8px; white-space: nowrap; }
        .pilot-badge::before { content: ''; width: 5px; height: 5px; border-radius: 50%; background: currentColor; }
        .pilot-badge[data-status='completed'] { color: var(--pilot-success);
          background: color-mix(in srgb, var(--pilot-success) 9%, var(--pilot-bg)); }
        .pilot-badge[data-status='running'] { color: var(--pilot-accent);
          background: color-mix(in srgb, var(--pilot-accent) 9%, var(--pilot-bg)); }
        .pilot-badge[data-status='failed'] { color: var(--pilot-error);
          background: color-mix(in srgb, var(--pilot-error) 8%, var(--pilot-bg)); }
        .pilot-meta { display: flex; flex-wrap: wrap; gap: 8px 18px; color: var(--pilot-muted);
          font-size: 12px; padding: 12px 0; }
        .pilot-meta strong { color: var(--pilot-ink); font-weight: 500; margin-left: 7px; overflow-wrap: anywhere; }
        .pilot-list { padding: 0; margin: 11px 0 0; list-style: none; }
        .pilot-list li { display: flex; gap: 9px; padding: 4px 0; overflow-wrap: anywhere; }
        .pilot-list li::before { content: ''; width: 4px; height: 4px; flex: none;
          background: var(--pilot-muted); border-radius: 50%; margin-top: 9px; }
        .pilot-disclosure { border-top: 1px solid var(--pilot-line); padding-top: 13px; margin-top: 13px; }
        .pilot-disclosure summary { color: var(--pilot-muted); font-size: 13px; cursor: pointer; width: fit-content; }
        .pilot-disclosure[open] summary { color: var(--pilot-ink); margin-bottom: 10px; }
        .pilot-field { display: flex; flex-direction: column; gap: 7px; margin: 14px 0; }
        .pilot-field-label { font-size: 13px; font-weight: 500; }
        .pilot-input { width: 100%; min-width: 0; border: 1px solid var(--pilot-line);
          background: var(--pilot-bg); color: var(--pilot-ink); border-radius: 10px; padding: 10px 12px; }
        .pilot-input::placeholder { color: var(--pilot-muted); opacity: .8; }
        textarea.pilot-input { resize: vertical; min-height: 100px; }
        .pilot-notice { border-radius: 10px; background: var(--pilot-soft); color: var(--pilot-muted);
          padding: 12px 14px; font-size: 13px; overflow-wrap: anywhere; }
        .pilot-notice-error { background: color-mix(in srgb, var(--pilot-error) 7%, var(--pilot-bg));
          color: var(--pilot-error); white-space: pre-wrap; }
        .pilot-empty { padding: 30px 16px; text-align: center; }
        .pilot-empty-icon { color: var(--pilot-muted); margin: 0 auto 14px; display: block; }
        .pilot-empty h3 { font-size: 17px; font-weight: 600; margin-bottom: 8px; }
        .pilot-empty p { color: var(--pilot-muted); max-width: 360px; margin: 0 auto; font-size: 13px; }
        .pilot-empty .pilot-row { justify-content: center; margin-top: 18px; }
        .pilot-example { color: var(--pilot-muted); background: var(--pilot-soft); border: 0;
          font-size: 13px; text-align: left; border-radius: 12px; padding: 13px 15px; margin-top: 15px !important; }
        .pilot-item { padding: 15px 0; border-bottom: 1px solid var(--pilot-line); }
        .pilot-item:last-child { border-bottom: 0; }
        .pilot-item-open { width: 100%; border: 0; background: transparent; color: var(--pilot-ink);
          padding: 0; cursor: pointer; text-align: left; display: flex; align-items: center; gap: 12px; }
        .pilot-item-open:disabled { opacity: .45; cursor: default; }
        .pilot-item-open:hover:not(:disabled) .pilot-item-title { color: var(--pilot-accent); }
        .pilot-item-icon { display: grid; place-items: center; width: 36px; height: 36px;
          background: var(--pilot-soft); color: var(--pilot-muted); border-radius: 10px; flex: none; }
        .pilot-item-copy { flex: 1; min-width: 0; }
        .pilot-item-title { display: block; font-size: 14px; font-weight: 500; overflow-wrap: anywhere; }
        .pilot-item-meta { display: block; color: var(--pilot-muted); font-size: 12px; margin-top: 3px; }
        .pilot-chevron { color: var(--pilot-muted); flex: none; }
        .pilot-project-actions { margin: 11px 0 0 48px; gap: 4px; }
        .pilot-choice { display: flex; gap: 9px; align-items: flex-start; padding: 10px 12px;
          border: 1px solid var(--pilot-line); border-radius: 9px; margin: 7px 0; cursor: pointer; }
        .pilot-choice:has(input:checked) { border-color: var(--pilot-accent);
          background: color-mix(in srgb, var(--pilot-accent) 5%, var(--pilot-bg)); }
        .pilot-choice input, .pilot-check input { accent-color: var(--pilot-accent); margin-top: 5px; flex: none; }
        .pilot-check { display: flex; gap: 8px; margin: 14px 0; color: var(--pilot-muted); font-size: 13px; }
        .pilot-archive-message { white-space: pre-wrap; overflow-wrap: anywhere; padding: 14px 0;
          border-bottom: 1px solid var(--pilot-line); }
        .pilot-archive-message strong { display: block; font-size: 12px; color: var(--pilot-muted); margin-bottom: 7px; }
        .pilot-preview { display: flex; flex-direction: column; gap: 14px; height: 100%; min-height: 440px; }
        .pilot-preview h2 { font-size: 18px; font-weight: 600; overflow-wrap: anywhere; }
        .pilot-preview-window { flex: 1; min-height: 330px; border: 1px solid var(--pilot-line);
          border-radius: 12px; overflow: hidden; display: flex; background: #fff; }
        .pilot-preview-window iframe { width: 100%; min-width: 0; flex: 1; border: 0; background: #fff; }
        .pilot-loading { color: var(--pilot-muted); font-size: 12px; margin-left: auto; }
        @container (max-width: 420px) {
          .pilot-top { padding: 16px 16px 0; }
          .pilot-scroll { padding: 18px 16px; scrollbar-gutter: auto; }
          .pilot-title { font-size: 20px; }
          .pilot-card { padding: 14px; }
          .pilot-item-open { gap: 9px; }
          .pilot-item-icon { width: 30px; height: 30px; border-radius: 8px; }
          .pilot-project-actions { margin-left: 0; }
          .pilot-preview .pilot-row { align-items: flex-start; }
        }
        @media (prefers-reduced-motion: reduce) { .pilot-btn { transition: none; } }
      `
      const icon = (name, size = 18, className) => {
        const paths = {
          app: ['M5 3h14a2 2 0 0 1 2 2v14a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V5a2 2 0 0 1 2-2Z', 'M3 9h18', 'M8 14l-2 2 2 2', 'M16 14l2 2-2 2', 'M13 13l-2 6'],
          folder: ['M3 7V5a2 2 0 0 1 2-2h5l2 3h7a2 2 0 0 1 2 2v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V7Z'],
          archive: ['M4 4h16v4H4Z', 'M6 8v12h12V8', 'M10 12h4'],
          arrow: ['m9 5 7 7-7 7'],
          play: ['m8 5 11 7-11 7Z'],
          plus: ['M12 5v14', 'M5 12h14'],
        }
        return h('svg', { width: size, height: size, viewBox: '0 0 24 24', fill: 'none', stroke: 'currentColor', strokeWidth: 1.6,
          strokeLinecap: 'round', strokeLinejoin: 'round', 'aria-hidden': true, className },
          ...(paths[name] || paths.app).map((d, i) => h('path', { d, key: i })))
      }
      const button = (text, action, disabled = false, variant = '', glyph) => h('button', {
        type: 'button', onClick: action, disabled, className: 'pilot-btn' + (variant ? ' pilot-btn-' + variant : ''),
      }, glyph && icon(glyph, 16), text)
      const statuses = { planned: '待开始', running: '交付中', paused: '已暂停', completed: '验收通过', failed: '交付失败', cancelled: '已取消' }
      // Product-facing copy for the migration demo; its reviewed plan and
      // candidate evidence remain unchanged in storage.
      const displayTitle = title => title === 'Harness 迁移验收：中文问答' ? 'Pilot · 中文问答演示' : title
      const displayGoal = goal => goal === '创建一个调用已配置 API 的简洁中文问答网页，验证官方 Harness 角色与领域插件交付'
        ? '创建一个调用已配置模型 API 的中文问答网页，支持示例问题、清空和复制回答。' : goal
      const badge = status => h('span', { className: 'pilot-badge', 'data-status': status }, statuses[status] || status)
      const list = items => h('ul', { className: 'pilot-list' }, ...(items || []).map((text, i) => h('li', { key: i }, text)))
      const empty = (glyph, title, description, extra) => h('div', { className: 'pilot-empty' }, icon(glyph, 30, 'pilot-empty-icon'),
        h('h3', null, title), h('p', null, description), extra)
      function PlanEditor({ sessionId, initial, currentVersion, canEdit, onSaved, onCancel }) {
        const [draft, setDraft] = useState({ ...initial.plan,
          requirementsText: initial.plan.application_requirements.join('\n'),
          examplesText: initial.plan.acceptance_examples.join('\n') })
        const [options, setOptions] = useState(null), [error, setError] = useState(''), [saving, setSaving] = useState(false)
        const mounted = useRef(true), submitting = useRef(false), request = useRef(null)
        const version = initial.state.plan_version ?? 1
        const stale = currentVersion !== version
        useEffect(() => {
          mounted.current = true
          const abort = new AbortController()
          rpc('plan-options', { session_id: sessionId }, abort.signal).then(value => {
            if (mounted.current) setOptions(value)
          }).catch(e => { if (!abort.signal.aborted && mounted.current) setError(e.message) })
          return () => { mounted.current = false; abort.abort() }
        }, [sessionId, initial.id])
        const change = (key, value) => { setDraft({ ...draft, [key]: value }); setError('') }
        const field = (label, key, multiline = false, limit = 2000) => h('label', { className: 'pilot-field' },
          h('span', { className: 'pilot-field-label' }, label), h(multiline ? 'textarea' : 'input', {
            className: 'pilot-input', value: draft[key], maxLength: limit, disabled: saving,
            onChange: e => change(key, e.target.value),
          }))
        async function save() {
          if (submitting.current || stale || !canEdit || !options) return
          const { requirementsText, examplesText, ...base } = draft
          const lines = text => text.split('\n').map(line => line.trim()).filter(Boolean)
          const plan = { ...base, application_requirements: lines(requirementsText), acceptance_examples: lines(examplesText) }
          const model = options.models.find(option => option.source === plan.model_source)
          if (!model || (plan.model_source !== 'modelscope' && plan.model_id !== model.model_id)) {
            setError('请选择当前可选模型。'); return
          }
          if (plan.title.trim().length < 2 || plan.goal.trim().length < 5
            || plan.application_requirements.length < 1 || plan.application_requirements.length > 8
            || plan.acceptance_examples.length < 2 || plan.acceptance_examples.length > 5
            || [...plan.application_requirements, ...plan.acceptance_examples].some(line => line.length > 500)) {
            setError('请填写标题和目标；功能需 1–8 项，验收输入需 2–5 项，每项最多 500 字。'); return
          }
          const signature = JSON.stringify(plan)
          if (request.current?.signature !== signature) request.current = { signature, id: crypto.randomUUID().replaceAll('-', '') }
          submitting.current = true; setSaving(true); setError('')
          try {
            await rpc('revise-plan', { session_id: sessionId, request_id: request.current.id, expected_version: version, plan })
            if (mounted.current) await onSaved()
          } catch (e) { if (mounted.current) setError(e.message) }
          finally { submitting.current = false; if (mounted.current) setSaving(false) }
        }
        return h('section', { className: 'pilot-card', 'aria-label': '编辑应用方案', 'data-pilot-plan-editor': initial.id },
          h('h3', { className: 'pilot-section-title' }, '编辑方案'),
          h('p', { className: 'pilot-section-note' }, '修改标题、目标、模型和功能。保存后查看方案，再点击开始交付。'),
          field('应用名称', 'title', false, 70), field('应用目标', 'goal', true),
          h('label', { className: 'pilot-field' }, h('span', { className: 'pilot-field-label' }, '应用使用的模型'),
            h('select', { className: 'pilot-input', value: draft.model_source, disabled: saving || !options || options.model_locked,
              onChange: e => { const option = options.models.find(o => o.source === e.target.value); setDraft({ ...draft, model_source: option.source, model_id: option.model_id }); setError('') } },
              !options && h('option', { value: draft.model_source }, '正在读取可选模型…'),
              ...(options?.models ?? []).map(option => h('option', { key: option.source, value: option.source }, option.label + ' · ' + option.model_id)))),
          options?.model_locked && h('p', { className: 'pilot-section-note' }, '模型沿用项目已确认的运行方式。'),
          draft.model_source === 'modelscope' && field('ModelScope 模型（owner/name）', 'model_id', false, 150),
          options?.note && h('p', { className: 'pilot-section-note' }, options.note),
          field('应用功能（每行一项，1–8 项）', 'requirementsText', true, 4008),
          field('验收输入（每行一项，2–5 项）', 'examplesText', true, 2505),
          h('p', { className: 'pilot-section-note' }, '验收输入是实际发送给应用的问题。网页交互和答案质量仍需单独检查。'),
          stale && h('div', { className: 'pilot-notice pilot-notice-error', role: 'alert' }, '方案已在其他位置更新。请取消编辑，再打开最新方案。'),
          !canEdit && h('div', { className: 'pilot-notice pilot-notice-error', role: 'alert' }, '当前方案已开始执行或状态已改变，请取消编辑并查看最新状态。'),
          error && h('div', { className: 'pilot-notice pilot-notice-error', role: 'alert' }, error),
          h('div', { className: 'pilot-row' }, button(saving ? '正在保存…' : '保存方案', save, saving || stale || !canEdit || !options, 'primary'),
            button('取消编辑', onCancel, saving, 'quiet')))
      }
      function PlanTail({ turn, sessionId }) {
        const source = turn.data.source(PLAN_RESULT)
        const result = useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot)
        return result && turn.status === 'closed'
          ? result.completed ? h(CompletionResult, { key: sessionId + ':completed', sessionId, recorded: result.application })
            : h(PlanResult, { key: sessionId + ':' + result.application.id, sessionId, recorded: result.application }) : null
      }
      function CompletionResult({ sessionId, recorded }) {
        const [view, setView] = useState(null), [error, setError] = useState('')
        useEffect(() => {
          const abort = new AbortController()
          rpc('view', { session_id: sessionId }, abort.signal).then(setView)
            .catch(e => { if (!abort.signal.aborted) setError(e.message) })
          return () => abort.abort()
        }, [sessionId, recorded?.id])
        const app = recorded ?? view?.application
        const current = !!view?.application && (!recorded || view.application.id === recorded.id)
          && view.application.status === 'completed'
          && (!recorded || view.application.state.plan_version === recorded.state.plan_version)
        return h('section', { className: 'pilot-pane pilot-plan-result', 'aria-label': '交付结果' },
          h('style', null, styles), h('h2', { className: 'pilot-section-title' }, current ? '应用已交付' : '本轮已完成交付'),
          app && h('p', null, displayTitle(app.title)),
          current && h('p', null, `版本 ${view.application.state.candidate?.id ?? '当前版本'} · ${view.application.state.verification?.cases?.length ?? 0} 项真实模型调用验收通过`),
          h('p', { className: 'pilot-section-note' }, '交付以已保存的源码、部署和独立验收为准。旧轮次错误保留用于追溯。'),
          error && h('p', { role: 'alert' }, '读取当前交付状态失败：' + error),
          view?.application && !current && h('p', { className: 'pilot-section-note' }, '当前应用已修改或状态已改变，请查看最新交付状态。'),
          button('打开应用工作台', () => ctx.layout.selectPanel('pilot-workbench'), false, 'primary'))
      }
      function BudgetControls({ sessionId, value, canEdit, onSaved }) {
        const [editing, setEditing] = useState(false), [limit, setLimit] = useState('')
        const [saving, setSaving] = useState(false), [error, setError] = useState('')
        const mounted = useRef(true), submitting = useRef(false), revision = useRef(null), request = useRef(null)
        useEffect(() => { mounted.current = true; return () => { mounted.current = false } }, [sessionId])
        const stale = editing && revision.current !== value.revision
        async function save() {
          if (submitting.current || stale || !canEdit) return
          const amount = limit.trim() === '' ? null : Number(limit)
          if (amount !== null && (!Number.isSafeInteger(amount) || amount < 1 || amount > 1000000000)) {
            setError('请输入正整数；留空表示不设置累计 token 上限。'); return
          }
          const signature = String(amount)
          if (request.current?.signature !== signature) request.current = { signature, id: crypto.randomUUID().replaceAll('-', '') }
          submitting.current = true; setSaving(true); setError('')
          try {
            await rpc('budget-update', { session_id: sessionId, request_id: request.current.id,
              expected_revision: revision.current, max_tokens: amount })
            if (mounted.current) { await onSaved(); setEditing(false) }
          } catch (e) { if (mounted.current) setError(e.message) }
          finally { submitting.current = false; if (mounted.current) setSaving(false) }
        }
        return h('details', { className: 'pilot-disclosure' }, h('summary', null, '用量与预算'),
          h('p', null, `累计 ${value.used_tokens.toLocaleString()} token · ${value.calls} 次模型请求 · 上限 ${value.max_tokens == null ? '未设置' : value.max_tokens.toLocaleString()}`),
          h('p', { className: 'pilot-section-note' }, `输入 ${value.input_tokens.toLocaleString()} / 输出 ${value.output_tokens.toLocaleString()}${value.estimated ? '（含估算）' : ''}。包括执行会话和子智能体的重复上下文输入，不等于单次上下文长度或精确费用。`),
          value.near_limit && h('p', { className: 'pilot-section-note' }, '累计用量接近上限。下一次请求还需预留完整输入与输出，可能在到达上限前暂停。'),
          h('p', { className: 'pilot-section-note' }, '上下文压缩由运行时自动处理；累计预算由你控制。保存设置保留已有用量，不会开始交付。'),
          !editing && canEdit && button('调整累计预算', () => {
            revision.current = value.revision; request.current = null; setLimit(value.max_tokens == null ? '' : String(value.max_tokens)); setError(''); setEditing(true)
          }),
          !canEdit && h('p', { className: 'pilot-section-note' }, '执行中请先暂停，或确认未完成的操作结果，再调整预算。'),
          editing && h('div', null, h('label', { className: 'pilot-field' }, h('span', { className: 'pilot-field-label' }, '累计 token 上限（留空为不限）'),
            h('input', { className: 'pilot-input', inputMode: 'numeric', value: limit, disabled: saving, maxLength: 10,
              onChange: e => { setLimit(e.target.value); setError('') } })),
            stale && h('p', { role: 'alert' }, '预算已更新，请取消后重新编辑。'),
            error && h('p', { role: 'alert' }, error),
            h('div', { className: 'pilot-row' }, button('保存预算', save, saving || stale || !canEdit, 'primary'),
              button('取消预算编辑', () => setEditing(false), saving, 'quiet'))))
      }
      function PlanResult({ sessionId, recorded }) {
        const [view, setView] = useState(null), [error, setError] = useState('')
        const [loadError, setLoadError] = useState(''), [busy, setBusy] = useState(false)
        const [editing, setEditing] = useState(null)
        const [showCurrent, setShowCurrent] = useState(false)
        const generation = useRef(0), acting = useRef(false), readEpoch = useRef(0)
        useEffect(() => {
          const ticket = ++generation.current, abort = new AbortController()
          let loading = false
          setView(null); setError(''); setLoadError(''); setBusy(false); setEditing(null); setShowCurrent(false); acting.current = false
          async function refresh() {
            if (loading) return
            loading = true
            const epoch = readEpoch.current
            try {
              const value = await rpc('view', { session_id: sessionId }, abort.signal)
              if (generation.current === ticket && readEpoch.current === epoch) { setView(value); setLoadError('') }
            } catch (e) {
              if (!abort.signal.aborted && generation.current === ticket)
                setLoadError('暂时无法读取最新交付状态，正在重试：' + e.message)
            } finally { loading = false }
          }
          refresh()
          const timer = setInterval(refresh, 4000)
          return () => { generation.current++; abort.abort(); clearInterval(timer) }
        }, [sessionId, recorded.id])
        const bound = view?.application?.id === recorded.id
        const app = bound ? view.application : recorded
        async function start() {
          if (!bound || loadError || acting.current) return
          const ticket = generation.current
          acting.current = true; readEpoch.current++; setBusy(true); setError('')
          try {
            const value = await rpc('start', { session_id: sessionId, expected_version: app.state.plan_version ?? 1 })
            if (generation.current !== ticket) return
            await ctx.sessions.refresh()
            if (generation.current === ticket) ctx.uiWorkspace.openSession(value.sessionId)
          } catch (e) { if (generation.current === ticket) setError(e.message) }
          finally { if (generation.current === ticket) { acting.current = false; setBusy(false) } }
        }
        const historical = (recorded.state?.plan_version ?? 1) < (latestPlanVersions.get(recorded.id) ?? 1)
        if (historical && !showCurrent && !editing) return h('section', { className: 'pilot-pane pilot-plan-result',
          'aria-label': '历史应用方案', 'data-pilot-plan-history': recorded.id }, h('style', null, styles),
          h('strong', null, '历史方案已修订'),
          h('p', { className: 'pilot-section-note' }, displayTitle(recorded.title) + ' · ' + recorded.plan.model_id),
          h('p', { className: 'pilot-section-note' }, '原始方案保留在此轮记录中。请查看最新方案后开始交付。'),
          bound && button('查看当前方案', () => setShowCurrent(true), !!loadError, 'quiet'))
        return h('section', { className: 'pilot-pane pilot-plan-result', 'data-pilot-plan-result': recorded.id,
          'aria-label': '应用方案' }, h('style', null, styles),
          h('div', { className: 'pilot-row pilot-spread' }, h('strong', null,
            app.status === 'planned' ? '方案已生成，待开始交付' : '已保存的应用方案'), badge(app.status)),
          h('h2', { className: 'pilot-title' }, displayTitle(app.title)),
          h('p', { className: 'pilot-description' }, displayGoal(app.plan.goal)),
          h('div', { className: 'pilot-meta' }, h('span', null, '使用模型', h('strong', null, app.plan.model_id))),
          h('details', { className: 'pilot-disclosure' },
            h('summary', null, '查看功能与验收 · ' + app.plan.application_requirements.length + ' 项功能 · ' + app.plan.acceptance_examples.length + ' 项验收'),
            h('h3', { className: 'pilot-section-title' }, '应用功能'), list(app.plan.application_requirements),
            h('h3', { className: 'pilot-section-title', style: { marginTop: 14 } }, '验收问题'), list(app.plan.acceptance_examples)),
          app.status === 'planned' && h('p', { className: 'pilot-section-note' }, '方案已保存。点击开始交付后，Pilot 将构建应用并验收。'),
          app.state.feedback?.length > 0 && h('div', { className: 'pilot-notice' }, '最新修改要求：' + app.state.feedback.at(-1).message),
          (error || loadError) && h('div', { className: 'pilot-notice pilot-notice-error', role: 'alert' }, error || loadError),
          view && !bound && h('p', { className: 'pilot-section-note' }, '此方案已不再关联当前会话，可从应用库打开。'),
          h('div', { className: 'pilot-row' },
            (view?.actions?.start || !view && ['planned', 'paused', 'failed'].includes(app.status)) && button(busy ? '正在启动…' : app.status === 'planned' ? '开始交付' : '继续交付', start,
              busy || !bound || !!loadError || !!editing, 'primary', 'play'),
            bound && view.actions?.edit_plan && button('编辑方案', () => setEditing(app), busy || !!loadError || !!editing),
            bound && app.state.harness_session && button('查看交付会话', () => ctx.uiWorkspace.openSession(app.state.harness_session), busy, 'quiet')),
          editing && h(PlanEditor, { key: sessionId + ':' + editing.id, sessionId, initial: editing,
            currentVersion: app.state.plan_version ?? 1,
            canEdit: bound && view.actions?.edit_plan,
            onSaved: async () => { const ticket = generation.current; readEpoch.current++; const updated = await rpc('view', { session_id: sessionId });
              if (generation.current === ticket) { setView(updated); setEditing(null) } },
            onCancel: () => setEditing(null) }))
      }
      function Body({ sessionId, workbench = false }) {
        const [selectedSession, setSelectedSession] = useState(null)
        const identity = workbench ? selectedSession : sessionId
        const [view, setView] = useState(null), [catalog, setCatalog] = useState(null)
        const [error, setError] = useState(''), [busy, setBusy] = useState(false)
        const [loadError, setLoadError] = useState('')
        const [legacy, setLegacy] = useState(null), [preview, setPreview] = useState(null), [feedback, setFeedback] = useState('')
        const [tab, setTab] = useState(workbench ? 'library' : 'current')
        const [libraryFilter, setLibraryFilter] = useState('completed')
        const [editing, setEditing] = useState(null)
        const paneId = useId()
        const generation = useRef(0), frame = useRef(null), readEpoch = useRef(0), acting = useRef(false)
        async function navigate(id, ticket) {
          await ctx.sessions.refresh()
          if (generation.current === ticket) ctx.uiWorkspace.openSession(id)
        }
        useEffect(() => {
          const ticket = ++generation.current, abort = new AbortController()
          setView(null); setPreview(null); setLegacy(null); setFeedback(''); setError(''); setLoadError(''); setBusy(false); setEditing(null)
          async function refresh() {
            const epoch = readEpoch.current
            try {
              const value = identity ? await rpc('view', { session_id: identity }, abort.signal) : { application: null }
              if (generation.current === ticket && readEpoch.current === epoch) setView(value)
              if (workbench) {
                const updated = await rpc('catalog', {}, abort.signal)
                if (generation.current === ticket && readEpoch.current === epoch) setCatalog(updated)
              }
              if (generation.current === ticket) setLoadError('')
            } catch (e) { if (!abort.signal.aborted && generation.current === ticket) setLoadError('暂时无法读取交付信息，正在重试：' + e.message) }
          }
          refresh()
          const timer = setInterval(refresh, 4000)
          return () => { generation.current++; abort.abort(); clearInterval(timer) }
        }, [identity, workbench])
        async function action(name, payload = {}) {
          if (acting.current) return
          acting.current = true; readEpoch.current++
          const ticket = generation.current
          setBusy(true); setError('')
          try {
            const value = await rpc(name, { session_id: identity, ...payload })
            if (generation.current !== ticket) return
            if (value.sessionId) {
              if (workbench) { setSelectedSession(value.sessionId); setTab('current') }
              else { setTab('current'); await navigate(value.sessionId, ticket) }
            }
            else if (name === 'html') { setPreview(value); setTab('current') }
            else if (name === 'legacy') { setLegacy(value); setTab('archive') }
            else {
              const updated = await rpc('view', { session_id: identity })
              if (generation.current === ticket) { setView(updated); if (name === 'feedback') setFeedback('') }
            }
          } catch (e) { if (generation.current === ticket) setError(e.message) }
          finally { acting.current = false; if (generation.current === ticket) setBusy(false) }
        }
        useEffect(() => {
          if (!preview) return
          const ticket = generation.current
          let running = false, lastAnswer = '', copying = false, disposed = false
          let legacyTimer, queuedLegacy
          const listener = async event => {
            if (disposed || event.source !== frame.current?.contentWindow) return
            const current = () => !disposed && ticket === generation.current && event.source === frame.current?.contentWindow
            if (event.data?.type === 'pilot-demo-http') {
              const { id, path, body } = event.data
              if (typeof id !== 'string' || id.length > 40 || !['/chat', '/health'].includes(path)) return
              const reply = (status, body) => { if (current()) event.source.postMessage({ type: 'pilot-demo-http-result', id, status, body }, '*') }
              // Some generated pages notify the host and then fetch the same input.
              // Coalesce that notification before starting the richer HTTP request.
              if (path === '/chat' && queuedLegacy?.message === body?.message) {
                clearTimeout(legacyTimer); queuedLegacy = null
              }
              if (path === '/chat' && running) { reply(409, { error: '已有请求正在执行，请稍候。' }); return }
              if (path === '/chat') { running = true; lastAnswer = '' }
              try {
                if (path === '/health') {
                  const value = await rpc('preview-health', { session_id: identity, message: 'health', binding: preview.binding })
                  reply(value.status, value.body)
                } else {
                  const value = await rpc('preview', { session_id: identity, message: body?.message,
                    profile: body?.profile, history: body?.history, binding: preview.binding })
                  if (current()) lastAnswer = value.answer
                  reply(200, value)
                }
              } catch (e) { reply(400, { error: '调用未完成：' + e.message }) }
              finally { if (path === '/chat') running = false }
              return
            }
            if (event.data?.type === 'pilot-demo-copy' && lastAnswer && !copying) {
              copying = true
              try { await navigator.clipboard.writeText(lastAnswer); if (current()) event.source.postMessage({ type: 'pilot-demo-copied', ok: true }, '*') }
              catch { if (current()) event.source.postMessage({ type: 'pilot-demo-copied', ok: false }, '*') }
              finally { copying = false }
              return
            }
            const message = event.data?.message
            if (event.data?.type !== 'pilot-demo-input' || running || queuedLegacy || typeof message !== 'string' || !message.trim() || message.length > 3000) return
            queuedLegacy = { message }
            legacyTimer = setTimeout(async () => {
              queuedLegacy = null
              if (!current() || running) return
              running = true; lastAnswer = ''
              try {
                const result = await rpc('preview', { session_id: identity, message, binding: preview.binding })
                if (current()) { lastAnswer = result.answer; event.source.postMessage({ type: 'pilot-demo-result', result }, '*') }
              } catch (e) { if (current()) event.source.postMessage({ type: 'pilot-demo-result', result: { answer: '调用未完成：' + e.message } }, '*') }
              finally { running = false }
            }, 50)
          }
          window.addEventListener('message', listener)
          return () => { disposed = true; clearTimeout(legacyTimer); queuedLegacy = null; window.removeEventListener('message', listener) }
        }, [preview, identity])
        const app = view?.application, content = [], notice = error || loadError
        const apps = catalog?.applications ?? [], archives = catalog?.legacy_conversations ?? []
        const listedApps = apps.filter(item => libraryFilter === 'all' || (libraryFilter === 'active'
          ? ['planned', 'running', 'paused'].includes(item.status) : item.status === 'completed'))
        const openWorkbench = () => ctx.layout.selectPanel('pilot-workbench')
        const actions = view?.actions ?? {}
        const feedbackForm = app && actions.feedback && h('details', { className: 'pilot-disclosure', open: true },
          h('summary', null, '修改应用'),
          h('p', { className: 'pilot-section-note' }, '保存后可继续交付，重新构建并验收修改后的版本。'),
          h('p', { className: 'pilot-section-note' }, '当前模型保持不变。已执行应用如需切换模型，请在新对话中创建方案。'),
          h('label', { className: 'pilot-field' }, h('span', { className: 'pilot-field-label' }, '本次修改要求'),
            h('textarea', { className: 'pilot-input', value: feedback, onChange: e => setFeedback(e.target.value), placeholder: '例如：增加对话历史，回答支持 Markdown 展示' })),
          button('保存修改要求', () => action('feedback', { request_id: crypto.randomUUID().replaceAll('-', ''), message: feedback }), busy || feedback.trim().length < 3))
        if (notice) content.push(h('div', { className: 'pilot-notice pilot-notice-error', role: 'alert' }, notice))
        if (tab === 'current') {
          if (!view) content.push(empty('app', '正在读取交付信息', '请稍候'))
          else if (app && preview) content.push(h('section', { className: 'pilot-preview' },
            h('div', { className: 'pilot-row pilot-spread' }, h('div', null, h('h2', null, displayTitle(app.title)),
              h('p', { className: 'pilot-section-note' }, '在线试用 · ' + app.plan.model_id + ' · ' + (app.state.candidate?.id || '当前版本'))),
              button('返回方案', () => setPreview(null), false, 'quiet')),
            h('div', { className: 'pilot-preview-window' }, h('iframe', {
              ref: frame, title: '应用试用', sandbox: 'allow-scripts',
              srcDoc: '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; script-src \'unsafe-inline\'; style-src \'unsafe-inline\'; img-src data:;">'
                + (preview.transport ? '<script>' + preview.transport + '</script>' : '') + preview.html,
            }))))
          else if (app) {
            const controls = []
            if (!workbench && actions.start) controls.push(button(app.status === 'planned' ? '开始交付' : '继续交付', () => action('start', { expected_version: app.state.plan_version ?? 1 }), busy || !!editing, 'primary', 'play'))
            if (!workbench && actions.edit_plan) controls.push(button('编辑方案', () => setEditing(app), busy || !!editing))
            if (!workbench && actions.pause) controls.push(button('暂停交付', () => action('pause'), busy))
            if (!workbench && actions.cancel) controls.push(button('取消交付', () => action('cancel'), busy || !!editing, 'danger'))
            if (actions.preview) controls.push(button('试用应用', () => action('html'), busy, 'primary', 'play'))
            if (workbench) controls.push(button('查看会话', () => ctx.uiWorkspace.openSession(identity), busy, 'quiet'))
            content.push(h('section', null,
              h('div', { className: 'pilot-row pilot-spread' }, h('span', { className: 'pilot-eyebrow' }, '当前交付'), badge(app.status)),
              h('h2', { className: 'pilot-title' }, displayTitle(app.title)), h('p', { className: 'pilot-description' }, displayGoal(app.plan.goal)),
              h('div', { className: 'pilot-meta' }, h('span', null, '模型', h('strong', null, app.plan.model_id)),
                app.state.candidate && h('span', null, '版本', h('strong', null, app.state.candidate.id))),
              controls.length > 0 && h('div', { className: 'pilot-row' }, ...controls)))
            if (editing) content.push(h(PlanEditor, { key: identity + ':' + editing.id, sessionId: identity, initial: editing,
              currentVersion: app.state.plan_version ?? 1, canEdit: actions.edit_plan, onSaved: async () => { const ticket = generation.current; readEpoch.current++;
                const updated = await rpc('view', { session_id: identity }); if (ticket === generation.current) { setView(updated); setEditing(null) } },
              onCancel: () => setEditing(null) }))
            if (!workbench && view.budget) content.push(h('section', { className: 'pilot-card', key: 'budget' },
              h(BudgetControls, { key: identity, sessionId: identity, value: view.budget, canEdit: actions.edit_budget,
                onSaved: async () => { const ticket = generation.current; readEpoch.current++;
                  const updated = await rpc('view', { session_id: identity }); if (ticket === generation.current) setView(updated) } })))
            if (actions.blocked_reason) content.push(h('div', { className: 'pilot-notice pilot-notice-error' }, actions.blocked_reason))
            if (app.state.error || app.state.last_error) content.push(h('div', { className: 'pilot-notice pilot-notice-error' }, app.state.error || app.state.last_error.detail))
            content.push(h('section', { className: 'pilot-card' }, h('h3', { className: 'pilot-section-title' }, '应用需求'),
              list(app.plan.application_requirements),
              h('details', { className: 'pilot-disclosure' }, h('summary', null, '验收输入 · ' + app.plan.acceptance_examples.length + ' 项'), list(app.plan.acceptance_examples))))
            if (app.state.candidate) content.push(h('div', { className: 'pilot-notice' },
              app.state.verification?.passed ? '当前版本已通过独立验收。' : '当前版本正在等待独立验收。'))
            if (!workbench && feedbackForm) content.push(h('section', null, feedbackForm))
          } else content.push(empty('app', '此会话没有历史应用', '推理任务的方案、候选和证据请在当前任务面板中查看。'))
        }
        if (tab === 'library') {
          content.push(h('div', null, h('h2', { className: 'pilot-section-title' }, '应用库'),
            h('p', { className: 'pilot-section-note' }, catalog ? '共 ' + apps.length + ' 个应用 · ' + apps.filter(item => item.status === 'completed').length + ' 个已验收' : '正在读取交付记录…'),
            apps.length>0&&h('div', { className: 'pilot-library-filters', role: 'group', 'aria-label': '应用筛选' },
              ...[['completed', '已验收'], ['active', '进行中'], ['all', '全部']].map(([key, text]) => h('button', {
                type: 'button', className: 'pilot-btn', 'aria-pressed': key === libraryFilter,
                key, onClick: () => setLibraryFilter(key),
              }, text)))))
          if (!listedApps.length) content.push(empty('app', catalog ? '此分类暂无历史应用' : '正在读取应用库', '已保存的模型应用会显示在这里。'))
          else content.push(h('div', null, ...listedApps.map(item => h('div', { className: 'pilot-item', key: item.id },
            h('button', { className: 'pilot-item-open', type: 'button', disabled: busy, onClick: () => action('open', { application_id: item.id }) },
              h('span', { className: 'pilot-item-icon' }, icon('app')),
              h('span', { className: 'pilot-item-copy' }, h('span', { className: 'pilot-item-title' }, displayTitle(item.title)),
                item.plan?.model_id && h('span', { className: 'pilot-item-meta' }, item.plan.model_id + (item.state.candidate?.id ? ' · ' + item.state.candidate.id : '')),
                h('span', { className: 'pilot-item-meta' }, Number.isNaN(Date.parse(item.created_at)) ? '记录 ' + item.id.slice(0, 6)
                  : new Date(item.created_at).toLocaleString('zh-CN', { month: 'numeric', day: 'numeric', hour: '2-digit', minute: '2-digit' }))), badge(item.status), icon('arrow', 15, 'pilot-chevron'))))))
        }
        if (tab === 'archive') {
          content.push(h('div', { className: 'pilot-row pilot-spread' }, h('div', null,
            h('h2', { className: 'pilot-section-title' }, legacy ? legacy.title : '历史记录'),
            h('p', { className: 'pilot-section-note' }, '已保存的项目讨论记录 · 只读')), legacy && button('返回列表', () => setLegacy(null), false, 'quiet')))
          if (legacy) content.push(h('section', null, ...legacy.messages.map((message, i) => h('div', { className: 'pilot-archive-message', key: i },
            h('strong', null, message.role === 'user' ? '你' : '助手'), h('p', null, message.text)))))
          else if (!archives.length) content.push(empty('archive', catalog ? '暂无历史记录' : '正在读取历史记录', '当前对话可在左侧会话列表中查看。'))
          else content.push(h('div', null, ...archives.map(item => h('div', { className: 'pilot-item', key: item.id },
            h('button', { className: 'pilot-item-open', type: 'button', disabled: busy, onClick: () => action('legacy', { id: item.id }) },
              h('span', { className: 'pilot-item-icon' }, icon('archive')),
              h('span', { className: 'pilot-item-copy' }, h('span', { className: 'pilot-item-title' }, item.title), h('span', { className: 'pilot-item-meta' }, '只读对话')),
              icon('arrow', 15, 'pilot-chevron'))))))
        }
        const nav = workbench ? [...(identity ? [['current', '应用详情']] : []), ['library', '历史应用'], ...(archives.length ? [['archive', '旧对话']] : [])]
          : [['current', '当前交付']]
        const hasNav = workbench && nav.length > 1
        return h('div', { className: 'pilot-pane' + (workbench ? ' pilot-workbench' : ''), 'data-pilot-delivery': true, 'data-pilot-ui-version': '20261119' },
          h('style', null, styles),
          h('header', { className: 'pilot-top' }, h('div', { className: 'pilot-brand' }, h('span', { className: 'pilot-brand-icon' }, icon('app', 20)),
            h('div', null, h('div', { className: 'pilot-brand-title' }, '历史应用'), h('div', { className: 'pilot-brand-note' }, workbench ? '保留的模型应用、验收与旧对话' : '已有模型应用的方案与试用')),
            busy && h('span', { className: 'pilot-loading', role: 'status' }, '正在处理…')),
            hasNav && h('nav', { className: 'pilot-nav', role: 'tablist', 'aria-label': '历史应用与旧对话' }, ...nav.map(([key, title], index) => h('button', {
              key, type: 'button', role: 'tab', id: paneId + '-tab-' + key, 'aria-selected': tab === key,
              'aria-controls': paneId + '-body', tabIndex: tab === key ? 0 : -1,
              onClick: () => { if (key !== 'current') setPreview(null); setTab(key) },
              onKeyDown: e => {
                if (!['ArrowRight', 'ArrowLeft', 'Home', 'End'].includes(e.key)) return
                e.preventDefault()
                const next = e.key === 'Home' ? 0 : e.key === 'End' ? nav.length - 1 : (index + (e.key === 'ArrowRight' ? 1 : -1) + nav.length) % nav.length
                if (nav[next][0] !== 'current') setPreview(null)
                setTab(nav[next][0]); e.currentTarget.parentElement.children[next].focus()
              },
            }, title)))),
          h('div', { className: 'pilot-scroll', id: paneId + '-body', role: hasNav ? 'tabpanel' : 'region',
            'aria-labelledby': hasNav ? paneId + '-tab-' + tab : undefined, 'aria-label': hasNav ? undefined : workbench ? '历史应用' : '当前交付', tabIndex: 0 },
            h('div', { className: 'pilot-content' + (preview && tab === 'current' && !notice ? '' : ' pilot-stack'), style: preview && tab === 'current' ? { height: '100%' } : undefined }, ...content)))
      }
      ctx.effect(() => ctx.sidebarRightTabs.register({ id: ID, kind: 'pilot-delivery', priority: 'extension',
        title: () => '当前交付', guide: [{ id: 'delivery', order: 5, title: () => '当前交付', description: () => '当前方案、交付进度与应用试用' }] }))
      ctx.effect(() => ctx.slots.inject('sidebar.right.pane.tab', () => ctx.slots.register({ name: 'sidebar.right.pane.tab', key: ID,
        inject: sessionId => ({ sessionId }) }, Body)))
      ctx.effect(() => ctx.slots.inject('sidebar.right.pane.tab.title', () => ctx.slots.register({ name: 'sidebar.right.pane.tab.title', key: ID }, () => h('span', null, '当前交付'))))
      ctx.effect(() => ctx.slots.inject('conversation.chat.turnTail', () => ctx.slots.register({ name: 'conversation.chat.turnTail', id: ID,
        inject: sessionId => ({ sessionId }) }, PlanTail)))
      ctx.effect(() => ctx.slots.inject('main', () => ctx.slots.register({ name: 'main', key: 'pilot-workbench' },
        () => h(Body, { workbench: true }))))
      ctx.effect(() => ctx.slots.inject('pilot.history.page', () => ctx.slots.register({ name: 'pilot.history.page', key: 'applications' },
        () => h(Body, { workbench: true }))))
    }
    return { inject, apply }
  },
})
