import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import * as Intent from '../harness/plugin/intent.mjs'

function rpcFixture(handler, additions = {}) {
  let rpc
  const ctx = {
    effect: fn => fn(), on() {}, logger: { warn() {} },
    connection: { rpc: { handle: (_path, callback) => { rpc = callback } } },
    pilotWorkspace: { request: handler },
    ...additions,
  }
  const code = readFileSync(new URL('../harness/runtime/plugins/infra-domain/index.mjs', import.meta.url), 'utf8')
    .replace(/^import .*$/m, 'const SessionId = value => value').replaceAll('export ', '')
  vm.runInNewContext(code + '\napply(ctx)', { ctx, AbortController, setInterval: () => ({ unref() {} }), clearInterval() {} })
  return (endpoint, session = 'root', args = {}, kind = 'owner') => rpc(endpoint, { ...args, session_id: session }, undefined, { principal: { kind } })
}

test('architecture Owner read ignores stale selection and does not compete with task mutations',async()=>{
  let release,started=false
  const gate=new Promise(resolve=>{release=resolve}),calls=[]
  const rpc=rpcFixture(async(op,id,role)=>{
    calls.push({op,id,role})
    if(op==='infra-budget'){started=true;await gate}
    return {version:'pilot-architecture-v1'}
  })
  const pending=rpc('budget','deleted-root')
  assert.equal(started,true)
  assert.equal((await rpc('architecture','deleted-root')).ok,true)
  assert.equal(calls[1].id,'');assert.equal(calls[1].op,'infra-architecture');assert.equal(calls[1].role,'owner')
  assert.equal((await rpc('architecture','',{},'agent')).ok,false)
  assert.equal(calls.length,2)
  release();assert.equal((await pending).ok,true)
})

test('network and distributed catalog reads can load concurrently without sharing a mutation claim',async()=>{
  let release
  const gate=new Promise(resolve=>{release=resolve}), calls=[]
  const rpc=rpcFixture(async operation=>{calls.push(operation);if(operation==='infra-network-catalog')await gate;return []})
  const first=rpc('network-catalog','')
  assert.equal((await rpc('distributed-catalog','')).ok,true)
  assert.equal((await rpc('distributed-preflight','')).ok,true)
  assert.equal((await rpc('distributed-catalog','',{},'agent')).ok,false)
  release();assert.equal((await first).ok,true)
  assert.deepEqual(calls,['infra-network-catalog','infra-distributed-catalog','infra-distributed-preflight'])
})

test('pause interrupts the actual native catalog child id before cancelling the root', async () => {
  const stopped=[],root={id:'root'},rpc=rpcFixture(async()=>({task:{id:'t',state:{status:'paused'}}}),{
    agents:{get:()=>root},subagents:{listChildren:async()=>[{id:'native-child'}],interrupt:(id,authority)=>{stopped.push(id);assert.equal(authority.agent,root)}},
    sessionController:{cancel:async()=>{stopped.push('root')}}})
  assert.equal((await rpc('pause','root')).ok,true)
  assert.deepEqual(stopped,['native-child','root'])
})

test('native settlement notification contains only a Job reference and preserves raw evidence', async () => {
  const hooks=new Map(),prompts=[],raw={artifact:{runtime_files:{huge:'private-raw-marker'.repeat(5000)}}}
  const event={seq:41,kind:'job_settled',data:{id:'job',action:'engine_build',candidate_id:'c2',status:'completed',evidence:'jobs/job/result.json',result:raw}}
  const agent={id:'root',session:{header:{}},status:'idle'},view={task:{state:{status:'running',notified_jobs:[],active_job:null}},events:[event]}
  const ctx={effect:fn=>fn(),on:(name,fn)=>hooks.set(name,fn),logger:{warn(){}},connection:{rpc:{handle:()=>{}}},
    agentPresets:{composedPreset:()=> 'infra-lead'},pilotWorkspace:{request:async operation=>{
      if(operation==='infra-notified')view.task.state.notified_jobs=['job'];return view},waitTree:async()=>{}},
    sessionController:{prompt:async value=>prompts.push(value)}}
  const code=readFileSync(new URL('../harness/runtime/plugins/infra-domain/index.mjs',import.meta.url),'utf8').replace(/^import .*$/m,'const SessionId=value=>value').replaceAll('export ','')
  vm.runInNewContext(code+'\napply(ctx)',{ctx,AbortController,setInterval:()=>({unref(){}}),clearInterval(){}})
  hooks.get('agent/created')({agent});hooks.get('agent/status')({agent,status:'idle'})
  await new Promise(setImmediate)
  assert.equal(prompts.length,1)
  assert.match(prompts[0].content[0].text,/jobs\/job\/result.json/)
  assert.doesNotMatch(prompts[0].content[0].text,/private-raw-marker|runtime_files/)
  assert.ok(prompts[0].content[0].text.length < 1000)
  assert.equal(event.data.result,raw)
  hooks.get('agent/status')({agent,status:'idle'});await new Promise(setImmediate)
  assert.equal(prompts.length,1)
})

test('deleted Session selection cannot block the Owner resource catalog or permit mutations', async () => {
  const calls = []
  const rpc = rpcFixture(async operation => { calls.push(operation); return { servers: [{ id: 'machine' }] } }, {
    sessionController: { resolveAgent: async id => {
      assert.equal(id, 'deleted-root')
      return { error: Object.assign(new Error('Session was deleted'), { code: 'session/not-found' }) }
    } },
  })
  const catalog = await rpc('resource-catalog', 'deleted-root')
  assert.equal(catalog.ok, true)
  assert.equal(catalog.value.servers[0].id, 'machine')
  assert.equal(catalog.value.session_permission, null)
  assert.equal(catalog.value.session_selection.status, 'missing')
  assert.equal(catalog.value.session_selection.session_id, 'deleted-root')
  for (const endpoint of ['resource-session-permission', 'start', 'resume']) {
    const result = await rpc(endpoint, 'deleted-root', { mode: 'review' })
    assert.equal(result.ok, false)
    assert.match(result.error.message, /Session was deleted/)
  }
  assert.deepEqual(calls, ['infra-resource-catalog'])
})

test('resource catalog preserves valid proposal-only Session permissions and rejects other failures', async () => {
  let failure
  const rpc = rpcFixture(async () => ({ servers: [] }), {
    sessionController: { resolveAgent: async () => failure ? { error: failure } : { agent: { session: 'proposal-root' } } },
    permissionPresets: { current: session => { assert.equal(session, 'proposal-root'); return 'observe' } },
  })
  const valid = await rpc('resource-catalog')
  assert.equal(valid.ok, true)
  assert.equal(valid.value.session_permission.mode, 'observe')
  assert.equal(valid.value.session_selection, undefined)
  failure = Object.assign(new Error('Ownership failure'), { code: 'session/subagent-owned' })
  assert.equal((await rpc('resource-catalog')).ok, false)
  failure = undefined
  const broken = rpcFixture(async () => ({}), {
    sessionController: { resolveAgent: async () => { throw new Error('Storage unavailable') } },
  })
  const result = await broken('resource-catalog')
  assert.equal(result.ok, false)
  assert.match(result.error.message, /Storage unavailable/)
})

test('rejected overlapping RPCs cannot release another request claim', async () => {
  let release
  const calls = []
  const rpc = rpcFixture(async (operation, session) => {
    calls.push([operation, session])
    if (calls.length === 1) return new Promise(resolve => { release = resolve })
    return { saved: true }
  })
  const first = rpc('budget')
  assert.equal((await rpc('budget')).ok, false)
  assert.equal((await rpc('budget')).ok, false)
  assert.equal(calls.length, 1)
  assert.equal((await rpc('budget', 'other-root')).ok, true)
  release({ saved: true })
  assert.equal((await first).ok, true)
  assert.equal((await rpc('budget')).ok, true)
  assert.equal(calls.length, 3)
})

test('usage guide never resolves stale Sessions or clears an active mutation claim', async () => {
  let release
  const calls=[]
  const rpc=rpcFixture(async operation=>{
    calls.push(operation)
    if(operation==='infra-budget')return new Promise(resolve=>{release=resolve})
    assert.equal(operation,'infra-guide');return {features:[]}
  },{sessionController:{resolveAgent:()=>{throw new Error('Guide must not resolve Sessions')}}})
  const pending=rpc('budget','deleted-root')
  assert.equal((await rpc('guide','deleted-root')).ok,true)
  assert.equal((await rpc('budget','deleted-root')).ok,false)
  assert.deepEqual(calls,['infra-budget','infra-guide'])
  release({saved:true});await pending
})

test('new chat always creates a fresh native root and never offers a bound blank for reuse', async () => {
  const creates = []
  const rpc = rpcFixture(() => { throw new Error('New chat must not modify a task') }, {
    pilotWorkspace: { rootId:()=> 'bound-blank',inboxPath:()=> '/fixture/runs/harness-inbox' },
    workspaceRegistry: { get:()=>({id:'selected-workspace'}),list:()=>[{id:'selected-workspace',sessionIds:['bound-blank']}] },
    sessionController: { resolveAgent:async()=>({agent:{id:'bound-blank'}}),create: async request => { creates.push(request); return { sessionId: 'fresh-' + creates.length } } },
  })
  assert.equal((await rpc('new-session', 'bound-blank')).value.sessionId, 'fresh-1')
  assert.equal((await rpc('new-session', 'bound-blank')).value.sessionId, 'fresh-2')
  assert.ok(creates.every(request => request.sessionId === undefined && request.agentPreset === 'infra-lead'))
  assert.ok(creates.every(request => request.workspaceId==='selected-workspace'))
})

function receiptFixture({ handler, selected = null, navigation = new AbortController() } = {}) {
  let plugin, definition, active
  const slots = new Map(), opened = [], listeners = new Map(), requests = []
  const storage = new Map(selected ? [['pilot-infra-selected', selected]] : [])
  const React = { createElement: (type, props, ...children) => ({ type, props: props ?? {}, children }),
    useSyncExternalStore: (_subscribe, snapshot) => snapshot(),
    useState: initial => {
      const index = active.cursor++, instance = active
      if (!(index in instance.values)) instance.values[index] = initial
      return [instance.values[index], value => { instance.values[index] = typeof value === 'function' ? value(instance.values[index]) : value }]
    },
    useRef: initial => { const index = active.cursor++; return active.values[index] ??= { current: initial } },
    useEffect: (effect, deps) => {
      const index = active.cursor++, previous = active.values[index], instance = active
      if (!previous || deps.some((value, i) => value !== previous.deps[i])) {
        previous?.cleanup?.()
        instance.effects.push(() => { instance.values[index] = { deps, cleanup: effect() } })
      }
    },
  }
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/infra-domain/client.js', import.meta.url), 'utf8'), {
    window: { __ModuleLoader__: { load: value => { plugin = value } } },
    document: { createElement: () => ({ remove() {} }), head: { append() {} },
      addEventListener: (name, fn) => listeners.set(name, fn), removeEventListener: name => listeners.delete(name) },
    localStorage: { getItem: key => storage.get(key) ?? null, setItem: (key, value) => storage.set(key, value), removeItem: key => storage.delete(key) },
    setInterval: () => 1, clearInterval() {},
  })
  plugin.factory(() => React).apply({
    effect: fn => fn(),
    uiConversation: { events: { register: value => { definition = value } } },
    slots: { inject: (_name, fn) => fn(), register: (slot, component) => slots.set(['settings.section','main','sidebar.panellist','pilot.history.page'].includes(slot.name)?slot.name+':'+(slot.id??slot.key):slot.name, component) },
    sidebarRightTabs: { register() {} }, sidebarRight: { openTab: kind => opened.push(kind) },
    connection: { rpc: { call: async (_path, method, payload) => {
      requests.push({ method, payload })
      try { return { ok: true, value: handler ? await handler(method, payload) : { sessionId: 'fresh-root' } } }
      catch (error) { return { ok: false, error: { message: error.message } } }
    } } },
    uiWorkspace: { openSession: id => opened.push(id) },
    sessions: {list:{getSnapshot:()=>({ids:selected?[selected]:[]})},retainInfo:()=>({getSnapshot:()=>({retainedBy:{mainView:1}})})},workspaces:{},
    layout: { selectPanel: id => opened.push(id),beginNavigation:()=>navigation.signal },
  })
  const replay = events => {
    const states = new Map()
    for (const event of events) {
      const match = definition.match(event)
      if (!match) continue
      const state = states.get(match.id)
      states.set(match.id, state ? definition.update({ state }, { event }) : definition.start({}, { event }))
    }
    return new Map([...states].map(([id, state]) => [id, definition.buildLocationData({ id, state }, 'turn')?.value]))
  }
  const render = (receipt, status = 'closed') => slots.get('conversation.chat.turnTail')({ turn: {
    status, data: { source: () => ({ getSnapshot: () => receipt }) },
  } })
  const mount = element => {
    const instance = { cursor: 0, values: [], effects: [] }
    let tree
    const render = () => {
      active = instance; instance.cursor = 0; instance.effects = []
      tree = element.type(element.props)
      for (const effect of instance.effects) effect()
      return tree
    }
    render()
    return { render, tree: () => tree }
  }
  return { replay, render, opened, listeners, requests, mount, storage, slots, main: () => slots.get('main:pilot-infra-workbench')(),settings:()=>slots.get('pilot.history.page:evaluations')() }
}

test('evaluation page shows empty catalog and failed reads without starting tasks', async()=>{
  let attempt=0
  const f=receiptFixture({handler:async method=>{
    if(method==='catalog')return {assets:{ready:false}}
    assert.equal(method,'evaluation-catalog')
    if(++attempt===1)throw new Error('temporary failure')
    return {cases:[{id:'case',family:'runtime',split:'held_out',supported:false}],report:null,error:null}
  }})
  const all=tree=>typeof tree==='object'&&tree?[tree,...tree.children.flat(Infinity).flatMap(all)]:[]
  const words=tree=>typeof tree==='string'?tree:tree?.children?.flat(Infinity).map(words).join(' ')??''
  const page=f.mount(all(f.settings()).find(n=>typeof n.type==='function'&&n.type.name==='Evaluations'))
  assert.ok(f.slots.has('sidebar.panellist:pilot-history'))
  assert.ok(!f.slots.has('sidebar.panellist:pilot-evaluation-history'))
  assert.ok(!f.slots.has('settings.section:pilot-evaluation'))
  await new Promise(setImmediate)
  assert.match(words(page.render()),/评测读取失败/)
  await all(page.render()).find(n=>n.type==='button').props.onClick()
  assert.match(words(page.render()),/尚未发布评测结果/)
  assert.match(words(page.render()),/评分链路待接入/)
  assert.ok(f.requests.every(r=>['catalog','evaluation-catalog'].includes(r.method)))
})
test('history keeps one sidebar destination and selects keyed pages with keyboard navigation',()=>{
  const f=receiptFixture(), rendered=[]
  const component=f.slots.get('main:pilot-history')
  const page=f.mount({type:component,props:{renderSlot:(name,owner,options)=>{rendered.push({name,owner,key:options.entryKey});return null}}})
  assert.equal(rendered.at(-1).key,'evaluations')
  let tree=page.render()
  let tabs=nodes(tree).filter(n=>n.props.role==='tab')
  assert.deepEqual(tabs.map(t=>text(t)),['历史评测','历史应用'])
  tabs[1].props.onClick();tree=page.render()
  assert.equal(rendered.at(-1).key,'applications')
  assert.equal(nodes(tree).find(n=>n.props.role==='tabpanel').props['aria-labelledby'],'pilot-history-tab-applications')
  let focused=false,prevented=false
  tabs=nodes(tree).filter(n=>n.props.role==='tab')
  tabs[1].props.onKeyDown({key:'Home',preventDefault(){prevented=true},currentTarget:{parentElement:{children:[{focus(){focused=true}},{}]}}})
  page.render()
  assert.ok(focused&&prevented);assert.equal(rendered.at(-1).key,'evaluations')
  assert.ok(f.requests.length===0)
  assert.ok(!f.slots.has('settings.section:pilot-history'))
  f.mount({type:f.slots.get('main:pilot-evaluation-history'),props:{}})
  assert.equal(f.opened.at(-1),'pilot-history')
})
const task = { id: 'task-a', revision: 1, plan: { title: '部署 Qwen3', goal: '部署并完成独立测试' }, state: { status: 'draft' } }
const call = (turn, name = 'pilot_infra_plan') => ({ type: 'tool/call', data: { turn, name, callId: 'call-' + turn } })
const result = (turn, value, isError = false) => ({ type: 'tool/result', data: { turn, message: {
  toolCallId: 'call-' + turn, isError, content: [{ type: 'text', text: JSON.stringify(value) }],
} } })
const nodes = tree => Array.isArray(tree) ? tree.flatMap(nodes) : !tree || typeof tree !== 'object' ? [] : [tree, ...tree.children.flatMap(nodes)]
const text = tree => Array.isArray(tree) ? tree.map(text).join(' ') : typeof tree === 'string' ? tree : tree && typeof tree === 'object' ? tree.children.map(text).join(' ') : ''
const settleUi = async () => { for (let i = 0; i < 12; i++) await Promise.resolve() }

async function mountEngineView({ current = false, tampered = false, measurements = {}, ttft_slo_ms = null } = {}) {
  const last = { candidate_id: 'c1', passed: true, gates: { identity: true, quality: true, test_complete: true, performance: true }, cases: [], ...measurements }
  const view = { task: { ...task, plan: { ...task.plan, kind: 'deploy_verify', machine_id: 'server', gpu_ids: [7],serving:{context_length:4096},workload:{input_lengths:[128],concurrency:[1],requests_per_case:2,max_new_tokens:24} },
    state: { status: 'running', execution_mode: 'manual', active_job: null, experiments: [last], hypotheses: [], repairs: 0,
      strategy: { mode: 'single', reason: 'fixture' }, strategy_history: [] } }, candidates: [], actions: [], events: [],
    budget: { max_tokens: 1000, used_tokens: 0, requests: 0 }, authority: { valid: true }, service_candidate: 'c1',
    current_acceptance: { current }, engine_source: { profile: 'fixture', prepared: true, file_count: 3, manual_mutation: true,
      candidates: [{ candidate_id: 'c1', build: tampered ? 'unverified' : 'passed', changed_files: [], active: true,
        source_digest: 'source', decision: 'allowed-once', ...(tampered ? { error: 'Installed artifact changed' } : {}) }] } }
  view.task.plan.ttft_slo_ms=ttft_slo_ms
  const ui = receiptFixture({ selected: 'engine-root', handler: async method => method === 'view' ? view : { assets: { ready: true }, tasks: [view] } })
  const body = ui.mount(ui.main()); await settleUi()
  const taskUi = ui.mount(nodes(body.render()).find(n => n.type?.name === 'Task'))
  const engineUi = ui.mount(nodes(taskUi.tree()).find(n => n.type?.name === 'EnginePanel'))
  return { ui, taskUi, engineUi }
}

test('performance target is unconfigured without SLO and old missing metrics never crash',async()=>{
  const sample={input_length:128,concurrency:1,attempted:4,successful:3,ttft_p95_ms:20,output_tokens_per_second:null}
  const noTarget=await mountEngineView({current:true,measurements:{cases:[sample]}})
  assert.match(text(noTarget.taskUi.tree()),/性能目标 · 未设置（仅报告测量）/)
  assert.doesNotMatch(text(noTarget.taskUi.tree()),/性能目标 · 通过/)
  assert.match(text(noTarget.taskUi.tree()),/TPOT估算 P95/)
  const target=await mountEngineView({current:true,ttft_slo_ms:100})
  assert.match(text(target.taskUi.tree()),/性能目标 · 通过/)
})

test('matrix fields accept Chinese commas and submit current values without relying on blur',async()=>{
  const assets={ready:true,machines:[{id:'server',allowed_gpu_ids:[7]}],models:[{id:'qwen'}],images:[{id:'sglang',engine:'sglang'}]}
  const ui=receiptFixture({handler:async()=>({assets,tasks:[]})})
  const body=ui.mount(ui.main());await settleUi();body.render()
  nodes(body.tree()).find(n=>n.type==='button'&&text(n)==='手动填写方案').props.onClick()
  const element=nodes(body.render()).find(n=>n.type?.name==='Editor')
  let saved,validity=''
  const editor=ui.mount({...element,props:{...element.props,onSave:plan=>{saved=plan}}})
  const field=()=>nodes(nodes(editor.tree()).find(n=>n.type==='label'&&text(n).startsWith('输入 token 长度'))).find(n=>n.type==='input')
  field().props.onChange({target:{value:'128，256',setCustomValidity:v=>{validity=v}}})
  editor.render()
  assert.equal(validity,'')
  editor.tree().props.onSubmit({preventDefault(){}})
  assert.deepEqual(Array.from(saved.workload.input_lengths),[128,256])
  field().props.onChange({target:{value:'128,',setCustomValidity:v=>{validity=v}}})
  assert.match(validity,/不重复整数/)
  editor.render()
  assert.equal(field().props.value,'128,')
})

test('old acceptance after reloading the same candidate cannot show current gates or finish', async () => {
  const { taskUi, engineUi } = await mountEngineView({ current: false })
  assert.match(text(taskUi.tree()), /历史验收记录/)
  assert.doesNotMatch(text(engineUi.tree()), /确认当前验收并交付/)
  const accepted = await mountEngineView({ current: true })
  assert.match(text(accepted.taskUi.tree()), /独立验收/)
  assert.match(text(accepted.engineUi.tree()), /确认当前验收并交付/)
})

test('unverified artifact hides load and test actions while retaining source reads', async () => {
  const { engineUi } = await mountEngineView({ tampered: true })
  assert.match(text(engineUi.tree()), /Installed artifact changed/)
  assert.doesNotMatch(text(engineUi.tree()), /加载此制品|核对已加载制品|独立验收此制品/)
  assert.match(text(engineUi.tree()), /读取源码/)
})

test('task console selects its existing draft and shows the review contract without empty execution panels',async()=>{
  const view={task:{id:'draft',revision:1,plan:{title:'Qwen部署',goal:'部署并验收',kind:'deploy_verify',machine_id:'server',gpu_ids:[7],model_asset:'qwen',image_asset:'sglang',serving:{context_length:4096},workload:{input_lengths:[256],concurrency:[1],requests_per_case:2,max_new_tokens:24}},state:{status:'draft',session_id:'root',experiments:[],hypotheses:[],strategy:{mode:'single',reason:'no faults'},strategy_history:[]}},candidates:[],events:[],actions:['edit','start'],budget:{used_tokens:0,max_tokens:500000,requests:0}}
  const ui=receiptFixture({handler:async method=>method==='view'?view:{assets:{ready:true},tasks:[view],source_workbench:{available:false}}})
  const body=ui.mount(ui.main());await settleUi();body.render();await settleUi();const rendered=body.render()
  const taskUi=ui.mount(nodes(rendered).find(n=>n.type?.name==='Task'))
  assert.match(text(taskUi.tree()),/方案与执行范围/)
  assert.match(text(taskUi.tree()),/4096/)
  assert.doesNotMatch(text(taskUi.tree()),/部署候选|故障分析与原始证据|历史验收记录|执行进度与协作/)
  assert.doesNotMatch(text(rendered),/历史应用|领域评测|源码与构建|还没有任务方案/)
  assert.equal(ui.storage.get('pilot-infra-selected'),'root')
  assert.ok(ui.requests.every(r=>['catalog','view'].includes(r.method)))
})

test('initial resource load failure shows an error and retry recovers the catalog', async () => {
  let disconnected = true
  const ui = receiptFixture({ handler: async method => {
    if (method === 'resource-catalog') {
      if (disconnected) throw new Error('Connection unavailable')
      return { servers: [], assets: { models: [] }, proposals: [], audit: [] }
    }
    return { assets: { ready: false }, tasks: [] }
  } })
  const body = ui.mount(ui.main())
  nodes(body.tree()).find(n => n.type === 'button' && text(n) === '服务器与权限').props.onClick()
  const resources = ui.mount(nodes(body.render()).find(n => n.type?.name === 'Resources'))
  await settleUi(); resources.render()
  assert.match(text(resources.tree()), /资源读取失败：Connection unavailable/)
  assert.doesNotMatch(text(resources.tree()), /正在读取服务器与权限/)
  disconnected = false
  await nodes(resources.tree()).find(n => n.type === 'button' && text(n) === '重试读取资源').props.onClick()
  resources.render()
  assert.match(text(resources.tree()), /服务器资源与权限/)
  assert.doesNotMatch(text(resources.tree()), /Connection unavailable/)
})

test('resource page clears only deleted selection and retains valid proposal-only roots', async () => {
  for (const missing of [true, false]) {
    const ui = receiptFixture({ selected: 'old-root', handler: async method => {
      if (method === 'view') return { task: null }
      if (method === 'resource-catalog') return { servers: [], assets: { models: [] }, proposals: [], audit: [],
        ...(missing ? { session_selection: { session_id: 'old-root', status: 'missing' } } : {}) }
      return { assets: { ready: false }, tasks: [] }
    } })
    const body = ui.mount(ui.main())
    await settleUi()
    nodes(body.tree()).find(n => n.type === 'button' && text(n) === '服务器与权限').props.onClick()
    ui.mount(nodes(body.render()).find(n => n.type?.name === 'Resources'))
    await settleUi()
    const resource = nodes(body.render()).find(n => n.type?.name === 'Resources')
    assert.equal(resource.props.sessionId, missing ? null : 'old-root')
    assert.equal(ui.storage.get('pilot-infra-selected'), missing ? undefined : 'old-root')
  }
})

test('tool-only plan and revision retain their own receipt and open native task controls without execution', () => {
  const ui = receiptFixture(), revised = { ...task, revision: 2, plan: { ...task.plan, title: '修订后部署' } }
  const history = ui.replay([call(1), result(1, { task, requires_start: true }),
    call(2, 'pilot_infra_revise'), result(2, { task: revised, requires_start: true })])
  const old = ui.render(history.get('1')), latest = ui.render(history.get('2'))
  assert.match(text(old), /部署 Qwen3/)
  assert.doesNotMatch(text(old), /修订后部署/)
  assert.match(text(latest), /方案版本 2/)
  assert.match(text(latest), /保存方案不会执行/)
  assert.equal(ui.render(history.get('2'), 'running'), null)
  nodes(latest).find(node => node.type === 'button').props.onClick()
  assert.deepEqual(ui.opened, ['pilot-infra'])
})

test('failed, unmatched and unrelated results never produce a plan or successful completion', () => {
  const ui = receiptFixture(), success = { task, requires_start: true }
  for (const events of [[call(1), result(1, success, true)], [result(1, success)],
    [call(1, 'pilot_infra_status'), result(1, success)], [call(1), result(1, { task })],
    [call(1), result(2, success)], [call(1, 'pilot_infra_finish'), result(1, { task, passed: true })]]) {
    assert.equal(ui.render(ui.replay(events).get('1')), null)
  }
  const completed = { ...task, state: { status: 'completed', outcome: { passed: true, current_candidate: 'c2' } } }
  const receipt = ui.replay([call(3, 'pilot_infra_finish'), result(3, { task: completed, passed: true })]).get('3')
  assert.match(text(ui.render(receipt)), /独立验收通过/)
  assert.match(text(ui.render(receipt)), /验收候选：c2/)
  assert.doesNotMatch(text(ui.render(receipt)), /性能达标/)
})

test('workbench draft and restoration keep distinct receipts without claiming inference acceptance', () => {
  const ui = receiptFixture(), build = { id: 'build-a', plan: { title: 'MUSA 编译', goal: '隔离编译并清理' }, status: 'draft' }
  const draft = ui.replay([call(1, 'pilot_infra_workbench_plan'), result(1, { build_task: build, requires_start: true })]).get('1')
  assert.match(text(ui.render(draft)), /方案保存后需准备节点专属工作区/)
  nodes(ui.render(draft)).find(node => node.type === 'button').props.onClick()
  assert.deepEqual(ui.opened, ['pilot-infra-workbench'])
  const restored = ui.replay([call(2, 'pilot_infra_workbench_cleanup'), result(2, { ...build, status: 'restored', restored: true })]).get('2')
  assert.match(text(ui.render(restored)), /工作区已清理/)
  assert.doesNotMatch(text(ui.render(restored)), /独立验收通过/)
})

test('global new-chat button uses fresh root RPC, coalesces clicks and leaves other clicks alone', async () => {
  const ui = receiptFixture()
  let prevented = 0
  const event = { target: { closest: () => ({}) }, preventDefault: () => prevented++, stopImmediatePropagation() {} }
  ui.listeners.get('click')(event)
  ui.listeners.get('click')(event)
  for (let i = 0; i < 8; i++) await Promise.resolve()
  assert.equal(ui.requests.length, 1)
  assert.equal(ui.requests[0].method, 'new-session')
  assert.deepEqual(ui.opened, ['fresh-root'])
  ui.listeners.get('click')({ ...event, target: { closest: () => null } })
  assert.equal(prevented, 2)
})

test('new chat uses native mainView selection and cannot steal a later navigation',async()=>{
  let finish;const navigation=new AbortController(),ui=receiptFixture({selected:'current-root',navigation,handler:()=>new Promise(resolve=>finish=resolve)})
  ui.listeners.get('click')({target:{closest:()=>({})},preventDefault(){},stopImmediatePropagation(){}})
  assert.equal(ui.requests[0].payload.session_id,'current-root');navigation.abort();finish({sessionId:'fresh-in-old-workspace'})
  await settleUi();assert.deepEqual(ui.opened,[])
})

test('successful chat plan renames its native Session and ends the planning turn without launching work', async () => {
  let tool
  const renamed = [], operations = [], value = { task, requires_start: true }
  const ctx = { effect: fn => fn(), tools: { register: definition => { tool = definition } },
    sandboxPolicy: { resolve: () => ({ mode: 'workspace-write' }) }, pilotWorkspace: {
    rootId: () => 'root', ensure: async () => value,intent:()=>Intent.classify('帮我部署Qwen3'),
    request: async (operation) => { operations.push(operation); return value },
    rename: async (agent, title) => renamed.push({ sessionId: agent.id, title }),
  } }
  const code = readFileSync(new URL('../harness/runtime/plugins/workspace/index.mjs', import.meta.url), 'utf8')
    .replace(/^import .*$/gm, '').replaceAll('export ', '')
  vm.runInNewContext(code + '\nregisterTools(ctx, {role:"infra-lead",tools:[{name:"infra_plan"}]})', {
    ctx, Intent,Service: class {}, defineTool: value => value,
  })
  let ended = 0
  await tool.execute({}, { agent: { id: 'root' }, concludeTurn: () => ended++ })
  assert.equal(renamed[0].title, task.plan.title)
  assert.equal(renamed[0].sessionId, 'root')
  assert.deepEqual(operations, ['infra-tool'])
  assert.equal(ended, 1)
})

test('observation-only Session rejects domain mutations before reaching Python', async () => {
  const tools = new Map(), operations = []
  const ctx = { effect: fn => fn(), tools: { register: definition => tools.set(definition.name, definition) },
    sandboxPolicy: { resolve: () => ({ mode: 'read-only' }) }, pilotWorkspace: {
      rootId: () => 'root', ensure: async () => ({}),intent:()=>Intent.classify('帮我部署Qwen3'),
      request: async operation => { operations.push(operation); return {} },
    } }
  const code = readFileSync(new URL('../harness/runtime/plugins/workspace/index.mjs', import.meta.url), 'utf8')
    .replace(/^import .*$/gm, '').replaceAll('export ', '')
  vm.runInNewContext(code + '\nregisterTools(ctx, {role:"infra-lead",tools:[{name:"infra_plan"},{name:"infra_resource_observe"}]})', {
    ctx, Intent,Service: class {}, defineTool: value => value,
  })
  await assert.rejects(() => tools.get('pilot_infra_plan').execute({}, { agent: { id: 'root' } }), /observation-only|read-only/i)
  assert.deepEqual(operations, [])
  await tools.get('pilot_infra_resource_observe').execute({}, { agent: { id: 'root' } })
  assert.deepEqual(operations, ['infra-tool'])
})


test('an idle parent with a running expert cannot accept manual source or permission changes', async () => {
  const root={id:'root',status:'idle',session:{}}, child={id:'expert',status:'running'},calls=[]
  const rpc=rpcFixture(async(op)=>{calls.push(op);return {}},{
    sessionController:{resolveAgent:async()=>({agent:root})},
    sandboxPolicy:{resolve:()=>({mode:'workspace-write'})},agentPresets:{composedPreset:()=> 'infra-lead'},
    subagents:{listChildren:async()=>[{id:'expert'}]},agents:{get:()=>child},permissionPresets:{set:()=>calls.push('permission-set')},
  })
  assert.equal((await rpc('engine-source-patch','root',{path:'sglang/srt/a.py'})).ok,false)
  assert.equal((await rpc('resource-session-permission','root',{mode:'observe'})).ok,false)
  assert.deepEqual(calls,[])
  assert.equal((await rpc('engine-source-read','root',{path:'sglang/srt/a.py'})).ok,true)
  child.status='idle'
  assert.equal((await rpc('resource-session-permission','root',{mode:'observe'})).ok,true)
})
