/** Plugin presentation/lifecycle checks; browser verification covers the real renderer. */
import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import { webcrypto } from 'node:crypto'

const application = { id: 'reviewed-app', title: '课程章节导航', status: 'planned', state: {},
  plan: { title: '课程章节导航', goal: '根据课程目录定位章节，并在聊天区回答。', model_source: 'existing_teacher8b', model_id: 'teaching-model',
    application_requirements: ['课程目录树', '回答后高亮章节'], acceptance_examples: ['带我去第三章', '介绍第五章'] } }
const actions = { start: true, edit_plan: true, feedback: false, cancel: true }
function setup(handler = async () => ({ application })) {
  const slots = new Map(), calls = [], navigated = [], timers = new Map(), timeouts = new Map(), listeners = new Map()
  let definition, plugin, active
  const React = {
    createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useState: initial => {
      const index = active.cursor++
      if (!(index in active.values)) active.values[index] = initial
      const instance = active
      return [instance.values[index], value => { instance.values[index] = value }]
    },
    useRef: initial => {
      const index = active.cursor++
      return active.values[index] ??= { current: initial }
    },
    useEffect: (effect, deps) => {
      const index = active.cursor++, previous = active.values[index]
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        previous?.cleanup?.()
        active.effects.push(() => { active.values[index] = { deps, cleanup: effect() } })
      }
    },
    useId: () => 'fixture',
    useSyncExternalStore: (_subscribe, getSnapshot) => getSnapshot(),
  }
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/delivery-ui/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load: value => { plugin = value.factory(() => React) } },
      addEventListener: (type, listener) => listeners.set(type, listener),
      removeEventListener: (type, listener) => { if (listeners.get(type) === listener) listeners.delete(type) } },
    AbortController, crypto: webcrypto, setInterval: callback => { const id = timers.size + 1; timers.set(id, callback); return id },
    clearInterval: id => timers.delete(id),
    setTimeout: callback => { const id = Symbol(); timeouts.set(id, callback); return id }, clearTimeout: id => timeouts.delete(id),
  })
  plugin.apply({
    effect: callback => callback(),
    uiConversation: { events: { register: value => { definition = value } } },
    slots: { inject: (_name, callback) => callback(), register: (slot, component) => slots.set(slot.name, component) },
    sidebarRightTabs: { register() {} },
    connection: { rpc: { call: async (_channel, operation, payload, signal) => {
      calls.push({ operation, payload, signal })
      try { const value = await handler(operation, payload); return { ok: true, value: value?.application ? { actions, ...value } : value } }
      catch (e) { return { ok: false, error: { message: e.message } } }
    } } },
    sessions: { refresh: async () => {} }, uiWorkspace: { openSession: id => navigated.push(id) },
  })
  const replay = events => {
    const states = new Map()
    for (const event of events) {
      const match = definition.match(event)
      if (!match) continue
      const state = states.get(match.id)
      states.set(match.id, state ? definition.update({ state }, { event }) : definition.start({}, { event }))
    }
    return new Map([...states].map(([id, state]) => [id, definition.buildLocationData({ id, state }, 'turn', null)?.value]))
  }
  const tail = (result, sessionId = 'planner') => slots.get('conversation.chat.turnTail')({ sessionId,
    turn: { status: 'closed', data: { source: () => ({ getSnapshot: () => result }) } } })
  const mount = element => {
    const instance = { cursor: 0, values: [], effects: [], tree: null }
    const render = () => {
      active = instance; instance.cursor = 0; instance.effects = []
      instance.tree = element.type(element.props)
      for (const effect of instance.effects) effect()
      return instance.tree
    }
    render()
    return { render, tree: () => instance.tree,
      unmount: () => { for (const value of instance.values) value?.cleanup?.() } }
  }
  return { replay, tail, mount, calls, navigated, timers,
    sendMessage: event => listeners.get('message')?.(event),
    flushTimeouts: async () => { for (const [id, callback] of timeouts) { timeouts.delete(id); await callback() } },
    body: sessionId => ({ type: slots.get('sidebar.right.pane.tab'), props: { sessionId } }) }
}
const call = (turn, name = 'pilot_plan') => ({ type: 'tool/call', data: { turn, name, callId: 'plan-' + turn } })
const result = (turn, isError = false, app = application) => ({ type: 'tool/result', data: { turn,
  message: { toolCallId: 'plan-' + turn, isError,
    content: [{ type: 'text', text: JSON.stringify({ application: app, requires_start: true }) }] } } })
function nodes(tree) { return Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...tree.children.flatMap(nodes)] }
function text(tree) { return Array.isArray(tree) ? tree.map(text).join(' ') : typeof tree === 'string' ? tree : tree && typeof tree === 'object' && tree.type !== 'style' ? tree.children.map(text).join(' ') : '' }
const settle = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }
const startButton = tree => nodes(tree).find(node => node.type === 'button' && /开始交付|正在启动/.test(text(node)))

async function previewSetup() {
  const binding = { candidate: 'c1', source_sha256: 'a'.repeat(64), model_identity: 'model-one' }, replies = []
  const ui = setup(async operation => {
    if (operation === 'html') return { html: '<html></html>', transport: '//trusted', binding }
    if (operation === 'preview') return { answer: '真实答案', model_id: 'model-one', finish_reason: 'stop' }
    return { application: { ...application, status: 'completed', state: { candidate: { id: 'c1' } } }, actions: { preview: true } }
  })
  const panel = ui.mount(ui.body('executor'))
  await settle(); panel.render()
  await nodes(panel.tree()).find(n => n.type === 'button' && text(n).trim() === '试用应用').props.onClick()
  panel.render()
  const source = { postMessage: data => replies.push(data) }
  nodes(panel.tree()).find(n => n.type === 'iframe').props.ref.current = { contentWindow: source }
  return { ui, panel, source, replies, binding }
}

test('preview coalesces a legacy notification followed by fetch into one rich request', async () => {
  const { ui, panel, source, replies, binding } = await previewSetup()
  await ui.sendMessage({ source, data: { type: 'pilot-demo-input', message: '问题' } })
  await ui.sendMessage({ source, data: { type: 'pilot-demo-http', id: '1', path: '/chat', body: {
    message: '问题', profile: { grade: '高二' }, history: [{ role: 'user', content: '上一轮' }] } } })
  await ui.flushTimeouts()
  const calls = ui.calls.filter(c => c.operation === 'preview')
  assert.equal(calls.length, 1)
  assert.deepEqual(JSON.parse(JSON.stringify(calls[0].payload)), { session_id: 'executor', message: '问题',
    profile: { grade: '高二' }, history: [{ role: 'user', content: '上一轮' }], binding })
  assert.equal(replies.length, 1)
  assert.equal(replies[0].type, 'pilot-demo-http-result')
  assert.equal(replies[0].status, 200)
  panel.unmount()
})

test('legacy-only previews still work and unmounted or foreign frames cannot infer', async () => {
  const { ui, panel, source, replies } = await previewSetup()
  await ui.sendMessage({ source: {}, data: { type: 'pilot-demo-http', id: 'x', path: '/chat', body: { message: '无效来源' } } })
  await ui.sendMessage({ source, data: { type: 'pilot-demo-input', message: '旧模板问题' } })
  assert.equal(ui.calls.filter(c => c.operation === 'preview').length, 0)
  await ui.flushTimeouts()
  assert.equal(ui.calls.filter(c => c.operation === 'preview').length, 1)
  assert.equal(replies[0].type, 'pilot-demo-result')
  await ui.sendMessage({ source, data: { type: 'pilot-demo-input', message: '关闭后不调用' } })
  panel.unmount(); await ui.flushTimeouts()
  assert.equal(ui.calls.filter(c => c.operation === 'preview').length, 1)
})

test('retained tool-only success renders the saved plan without starting delivery', async () => {
  const ui = setup(), data = ui.replay([call(1), result(1), { type: 'turn/end', data: { turn: 1 } }])
  const card = ui.mount(ui.tail(data.get('1')))
  assert.match(text(card.tree()), /方案已生成，待开始交付/)
  assert.match(text(card.tree()), /课程章节导航/)
  assert.match(text(card.tree()), /带我去第三章/)
  assert.equal(startButton(card.tree()).props.disabled, true)
  await settle(); card.render()
  assert.equal(startButton(card.tree()).props.disabled, false)
  assert.deepEqual(ui.calls.map(c => c.operation), ['view'])
  assert.deepEqual(ui.navigated, [])
  card.unmount()
})

test('failed and unrelated tools never produce a success card on other turns or sessions', () => {
  const ui = setup(), data = ui.replay([call(1), result(1), call(2), result(2, true), call(3, 'pilot_status'), result(3)])
  assert.ok(ui.tail(data.get('1')))
  assert.equal(ui.tail(data.get('2')), null)
  assert.equal(ui.tail(data.get('3')), null)
  assert.equal(ui.tail(ui.replay([]).get('1'), 'different-session'), null)
})

test('start uses the existing endpoint once and opens the returned execution session', async () => {
  let finish
  const pending = new Promise(resolve => { finish = resolve })
  const ui = setup(async operation => operation === 'start' ? pending : { application })
  const card = ui.mount(ui.tail(ui.replay([call(1), result(1)]).get('1')))
  await settle(); card.render()
  const button = startButton(card.tree())
  const first = button.props.onClick(); await button.props.onClick()
  assert.equal(ui.calls.filter(c => c.operation === 'start').length, 1)
  finish({ sessionId: 'delivery-reviewed-app' }); await first; card.render()
  assert.deepEqual(ui.navigated, ['delivery-reviewed-app'])
  card.unmount()
})

test('late start response after switching away cannot navigate the new session', async () => {
  let finish
  const ui = setup(operation => operation === 'start' ? new Promise(resolve => { finish = resolve }) : { application })
  const card = ui.mount(ui.tail(ui.replay([call(1), result(1)]).get('1')))
  await settle(); card.render()
  const starting = startButton(card.tree()).props.onClick()
  card.unmount(); finish({ sessionId: 'old-delivery' }); await starting
  assert.deepEqual(ui.navigated, [])
  assert.equal(ui.timers.size, 0)
  assert.equal(ui.calls[0].signal.aborted, true)
})

test('changed session binding disables the original plan action', async () => {
  const ui = setup(async () => ({ application: { ...application, id: 'another-app' } }))
  const card = ui.mount(ui.tail(ui.replay([call(1), result(1)]).get('1')))
  await settle(); card.render()
  assert.equal(startButton(card.tree()).props.disabled, true)
  await startButton(card.tree()).props.onClick()
  assert.equal(ui.calls.filter(c => c.operation === 'start').length, 0)
  assert.match(text(card.tree()), /已不再关联当前会话/)
  card.unmount()
})

test('read failure is visible and clears after reconnect; start rejection remains visible', async () => {
  let offline = true
  const ui = setup(async operation => {
    if (offline || operation === 'start') throw new Error('fixture connection failed')
    return { application }
  })
  const card = ui.mount(ui.tail(ui.replay([call(1), result(1)]).get('1')))
  await settle(); card.render()
  assert.match(text(card.tree()), /正在重试/)
  assert.equal(startButton(card.tree()).props.disabled, true)
  offline = false; await [...ui.timers.values()][0](); card.render()
  assert.doesNotMatch(text(card.tree()), /正在重试/)
  await startButton(card.tree()).props.onClick(); card.render()
  assert.match(text(card.tree()), /fixture connection failed/)
  assert.deepEqual(ui.navigated, [])
  card.unmount()
})

test('chat revision renders a genuine saved plan; manual editor saves API choice without starting', async () => {
  let saved
  const ui = setup(async (operation, payload) => {
    if (operation === 'plan-options') return { models: [
      { source: 'existing_teacher8b', model_id: 'teaching-model', label: '教学模型' },
      { source: 'configured_api', model_id: 'glm-5.3', label: '已配置 API' },
    ] }
    if (operation === 'revise-plan') { saved = payload; return { application: { ...application, plan: payload.plan } } }
    return { application }
  })
  const card = ui.mount(ui.tail(ui.replay([call(2, 'pilot_revise_plan'), result(2)]).get('2')))
  await settle(); card.render()
  await nodes(card.tree()).find(n => n.type === 'button' && text(n).trim() === '编辑方案').props.onClick(); card.render()
  assert.equal(startButton(card.tree()).props.disabled, true)
  const editor = ui.mount(nodes(card.tree()).find(n => n.type?.name === 'PlanEditor'))
  await settle(); editor.render()
  nodes(editor.tree()).find(n => n.type === 'select').props.onChange({ target: { value: 'configured_api' } }); editor.render()
  await nodes(editor.tree()).find(n => n.type === 'button' && text(n).trim() === '保存方案').props.onClick()
  assert.equal(saved.expected_version, 1)
  assert.equal(saved.plan.model_source, 'configured_api')
  assert.equal(saved.plan.model_id, 'glm-5.3')
  assert.deepEqual(JSON.parse(JSON.stringify(saved.plan.application_requirements)), application.plan.application_requirements)
  assert.equal(ui.calls.filter(c => c.operation === 'start').length, 0)
  editor.unmount(); card.unmount()
})

test('editor disables stale draft saves instead of overwriting a chat revision', async () => {
  const ui = setup(async operation => operation === 'plan-options' ? { models: [] } : { application })
  const card = ui.mount(ui.tail(ui.replay([call(1), result(1)]).get('1')))
  await settle(); card.render()
  nodes(card.tree()).find(n => n.type === 'button' && text(n).trim() === '编辑方案').props.onClick(); card.render()
  const element = nodes(card.tree()).find(n => n.type?.name === 'PlanEditor')
  element.props.currentVersion = 2
  const editor = ui.mount(element)
  await settle(); editor.render()
  const save = nodes(editor.tree()).find(n => n.type === 'button' && text(n).trim() === '保存方案')
  assert.equal(save.props.disabled, true)
  await save.props.onClick()
  assert.match(text(editor.tree()), /方案已在其他位置更新/)
  assert.equal(ui.calls.filter(c => c.operation === 'revise-plan').length, 0)
  editor.unmount(); card.unmount()
})

test('delivery panel hides unavailable mutations and shows the same draft editor entry', async () => {
  let available = true
  const ui = setup(async () => ({ application, actions: available ? actions : {} }))
  const panel = ui.mount(ui.body('planner'))
  await settle(); panel.render()
  assert.match(text(panel.tree()), /编辑方案/)
  assert.match(text(panel.tree()), /开始交付/)
  assert.doesNotMatch(text(panel.tree()), /修改应用|试用应用|打开配置文件|创建插件/)
  available = false
  await [...ui.timers.values()][0](); panel.render()
  assert.doesNotMatch(text(panel.tree()), /编辑方案|开始交付|修改应用|试用应用/)
  panel.unmount()
})

test('superseded chat plans stay as compact original evidence and latest plan keeps its actions', async () => {
  const revised = { ...application, state: { plan_version: 2 }, plan: { ...application.plan, model_id: 'glm-5.3' } }
  const ui = setup(async () => ({ application: revised }))
  const data = ui.replay([call(1), result(1), call(2, 'pilot_revise_plan'), result(2, false, revised)])
  const old = ui.mount(ui.tail(data.get('1'))), current = ui.mount(ui.tail(data.get('2')))
  await settle(); old.render(); current.render()
  assert.match(text(old.tree()), /历史方案已修订/)
  assert.match(text(old.tree()), /teaching-model/)
  assert.equal(startButton(old.tree()), undefined)
  assert.match(text(current.tree()), /glm-5.3/)
  assert.equal(startButton(current.tree()).props.disabled, false)
  old.unmount(); current.unmount()
})

test('retained successful finish shows actual delivery despite a later historical turn error', async () => {
  const completed = { ...application, status: 'completed', state: { candidate: { id: 'c1' }, verification: { passed: true, cases: [{}, {}, {}, {}] } } }
  const ui = setup(async () => ({ application: completed }))
  const oldResult = { type: 'tool/result', data: { turn: 5, message: { toolCallId: 'plan-5', isError: false,
    content: [{ type: 'text', text: JSON.stringify({ ok: true, result: { passed: true } }) }] } } }
  const data = ui.replay([call(5, 'pilot_finish'), oldResult,
    { type: 'turn/end', data: { turn: 5, reason: { kind: 'error', error: { code: 'PILOT_BUDGET_LIMIT' } } } }])
  const card = ui.mount(ui.tail(data.get('5'), 'executor'))
  await settle(); card.render()
  assert.match(text(card.tree()), /应用已交付/)
  assert.match(text(card.tree()), /4 项真实模型调用验收通过/)
  assert.equal(startButton(card.tree()), undefined)
  assert.equal(ui.calls.filter(c => c.operation === 'start').length, 0)
  card.unmount()
  assert.equal(ui.tail(ui.replay([call(6, 'pilot_finish'), { ...oldResult, data: { ...oldResult.data, turn: 6,
    message: { ...oldResult.data.message, toolCallId: 'plan-6', isError: true } } }]).get('6')), null)
})

test('budget editor saves owner control with revision and never starts execution', async () => {
  let saved
  const budget = { max_tokens: 500000, revision: 1, used_tokens: 489268, input_tokens: 472134, output_tokens: 17134, calls: 35, near_limit: true }
  const ui = setup(async (operation, payload) => {
    if (operation === 'budget-update') { saved = payload; return budget }
    return { application, budget, actions: { ...actions, edit_budget: true } }
  })
  const panel = ui.mount(ui.body('executor'))
  await settle(); panel.render()
  const control = ui.mount(nodes(panel.tree()).find(n => n.type?.name === 'BudgetControls'))
  assert.match(text(control.tree()), /489,268/)
  nodes(control.tree()).find(n => n.type === 'button' && text(n).trim() === '调整累计预算').props.onClick(); control.render()
  nodes(control.tree()).find(n => n.type === 'input').props.onChange({ target: { value: '' } }); control.render()
  const button = nodes(control.tree()).find(n => n.type === 'button' && text(n).trim() === '保存预算')
  const first = button.props.onClick(); await button.props.onClick(); await first
  assert.equal(saved.max_tokens, null)
  assert.equal(saved.expected_revision, 1)
  assert.equal(ui.calls.filter(c => c.operation === 'budget-update').length, 1)
  assert.equal(ui.calls.filter(c => c.operation === 'start').length, 0)
  control.unmount(); panel.unmount()
})
