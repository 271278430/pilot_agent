window.__ModuleLoader__.load({
  id: '@pilot/infra-domain',
  factory: require => {
    const React = require('react'), h = React.createElement
    const { useState, useEffect, useRef, useSyncExternalStore } = React
    const ID = '@pilot/infra-domain'
    const inject = ['slots', 'sidebarRightTabs', 'sidebarRight', 'connection', 'uiWorkspace', 'layout', 'uiConversation', 'sessions', 'workspaces']
    const kinds = { deploy_verify: '部署并验证', diagnose_repair: '诊断并修复', compare: '回归比较', test: '质量与性能实验' }
    const statuses = { draft: '待确认', running: '执行中', paused: '已暂停', failed: '失败', completed: '已完成', cancelled: '已取消' }
    const assetStates={registered:'已登记，尚未核验',preparing:'准备中',ready:'核验通过',submitted:'已提交',running:'执行中',completed:'已完成',failed:'失败',cancelled:'已取消',unknown:'效果待核对',pending:'待审阅',registering:'正在登记',rejected:'已拒绝'}
    const phases = { plan: '方案待审阅', inspect: '检查与执行', review: '检查测试结果', done: '验收完成', pause: '已暂停', cancel: '已取消' }
    const labels = { start: '开始任务', resume: '继续任务', pause: '暂停', cancel: '取消任务', stop_service: '停止自有服务', recover: '核对并停止未知操作' }
    const onMachine=(asset,machine)=>asset.server_id?asset.server_id===machine?.id:(machine?.transport??'local')==='local'
    const numberList=value=>value.split(/[,，]/).map(v=>v.trim()).filter(Boolean).map(Number)
    const backends=image=>image?.stack_recipe?.attention_backends.map(value=>[value,({torch_native:'PyTorch',triton:'Triton',auto:'引擎自动选择',flash_attn:'FlashAttention'})[value]??value])??(image?.engine==='vllm'?[['auto','引擎自动选择'],['flash_attn','FlashAttention'],['torch_native','PyTorch SDPA']]:[['triton','Triton'],['torch_native','PyTorch']])
    const fresh = assets => {const machine=assets.machines.find(m=>assets.models.some(a=>onMachine(a,m))&&assets.images.some(i=>onMachine(i,m)))??assets.machines[0],model=assets.models.find(a=>onMachine(a,machine)),image=assets.images.find(i=>onMachine(i,machine)&&(i.backend??'docker')==='docker'&&['sglang','vllm'].includes(i.engine??'sglang'));return ({ title: '', goal: '', kind: 'deploy_verify', machine_id: machine?.id ?? 'a100-server',
      gpu_ids: machine?.allowed_gpu_ids ?? [], model_asset: model?.id ?? '', image_asset:image?.id??'', strategy: 'auto',
      serving: { context_length: 4096, mem_fraction_static:image?.serving_defaults?.mem_fraction_static??.18, disable_cuda_graph: true, attention_backend:backends(image)[0][0], max_new_tokens: 96, stop: [] },
      workload: { cache_mode:'cold',seed:17,input_lengths: [128,1024], concurrency: [1,2], requests_per_case: 4, max_new_tokens: 48, timeout_seconds: 90 },
      target_task_id: null, engine_source_profile:null, candidate_ids: [], performance_required: true, ttft_slo_ms: null, deadline_seconds: 2400, max_tokens: 300000 })}
    function apply(ctx) {
      const RECEIPT = 'pilot-infra-receipt'
      const receiptTools = ['pilot_infra_plan', 'pilot_infra_revise', 'pilot_infra_resource_execute', 'pilot_infra_finish', 'pilot_infra_workbench_plan', 'pilot_infra_workbench_cleanup']
      function receiptState(state, event) {
        if (event.type === 'tool/call' && receiptTools.includes(event.data.name))
          return { ...state, calls: { ...state.calls, [event.data.callId]: event.data.name } }
        const message = event.data.message, name = state.calls[message?.toolCallId]
        if (event.type !== 'tool/result' || !name || message.isError) return state
        for (const block of message.content ?? []) {
          if (block.type !== 'text') continue
          try {
            const value = JSON.parse(block.text)
            if (name.startsWith('pilot_infra_workbench_')) {
              const build = value.build_task ?? value
              if (!build.id || !build.plan || (name.endsWith('_plan') ? value.requires_start !== true : build.restored !== true)) continue
              return {...state,receipt:{taskId:build.id,title:build.plan.title,goal:build.plan.goal,workbench:true,completed:build.restored===true}}
            }
            const task = value.task
            if (!task?.id || !task.plan || !task.state) continue
            const completed = name === 'pilot_infra_finish'
            if (completed ? value.passed !== true || task.state.status !== 'completed' || task.state.outcome?.passed !== true
              : value.requires_start !== true || task.state.status !== 'draft') continue
            return { ...state, receipt: { taskId: task.id, title: task.plan.title, goal: task.plan.goal,
              revision: task.revision, completed, candidate: task.state.outcome?.current_candidate } }
          } catch { /* Only a successful, matched domain result is a receipt. */ }
        }
        return state
      }
      ctx.effect(() => ctx.uiConversation.events.register({
        kind: RECEIPT,
        match: event => ['turn/start', 'tool/call', 'tool/result'].includes(event.type)
          ? { id: String(event.data.turn), role: 'start' } : null,
        start: (_context, match) => receiptState({ calls: {}, receipt: null }, match.event),
        update: (context, match) => receiptState(context.state, match.event),
        buildLocationData: (context, scope) => scope === 'turn' && context.state?.receipt
          ? { kind: 'turn', turn: Number(context.id), key: RECEIPT, value: context.state.receipt } : null,
      }))
      const navigationListeners=new Set()
      let navigation=null
      const subscribeNavigation=fn=>{navigationListeners.add(fn);return()=>navigationListeners.delete(fn)}
      const readNavigation=()=>navigation
      const openBuild=receipt=>{navigation={page:'build',taskId:receipt.taskId};for(const fn of navigationListeners)fn();ctx.layout.selectPanel('pilot-infra-workbench')}
      function ReceiptTail({ turn }) {
        const source = turn.data.source(RECEIPT)
        const receipt = useSyncExternalStore(source.subscribe, source.getSnapshot, source.getSnapshot)
        return receipt && turn.status === 'closed' ? h('section', { className: 'infra-pane infra-receipt',
          'aria-label': receipt.completed ? '推理任务完成回执' : '推理任务方案回执' },
          h('h3', null, receipt.completed ? (receipt.workbench?'工作区已清理，构建证据保留':'任务已完成，独立验收通过') : '本轮方案已保存'),
          h('strong', null, receipt.title), h('p', null, receipt.goal),
          h('p', { className: 'infra-meta' }, receipt.workbench ? '请在“软件与算子”中查看任务、独立检查和恢复状态。方案保存后需准备节点专属工作区，CPU与GPU验证分别记录。' : receipt.completed
            ? '验收候选：' + receipt.candidate + '。报告和原始证据可在当前任务中查看。'
            : '方案版本 ' + receipt.revision + '。请在当前任务中审阅并点击“开始任务”；保存方案不会执行。'),
          btn('当前任务与操作', () => receipt.workbench ? openBuild(receipt) : ctx.sidebarRight.openTab('pilot-infra')),
          h('p', { className: 'infra-meta' }, '此卡片记录本轮结果，最新状态以当前任务为准。')) : null
      }
      async function rpc(method, payload) {
        const r = await ctx.connection.rpc.call('/pilot-infra-api', method, payload)
        if (!r.ok) throw new Error(r.error.message)
        return r.value
      }
      let creatingSession = false, creationError = ''
      const creationListeners = new Set()
      const subscribeCreation = listener => { creationListeners.add(listener); return () => creationListeners.delete(listener) }
      const readCreationError = () => creationError
      async function createFreshSession() {
        if (creatingSession) return
        creatingSession = true; creationError = ''
        const navigation=ctx.layout.beginNavigation()
        try { const listed=ctx.sessions.list.getSnapshot(); const selected=listed.ids.find(id=>ctx.sessions.retainInfo(id).getSnapshot().retainedBy.mainView>0); const value = await rpc('new-session', selected?{session_id:selected}:{}); if(!navigation.aborted)ctx.uiWorkspace.openSession(String(value.sessionId)) }
        catch (error) { if(!navigation.aborted){creationError = '新会话创建失败：' + error.message; ctx.layout.selectPanel('pilot-infra-workbench')} }
        finally { creatingSession = false; for (const listener of creationListeners) listener() }
      }
      // The fixed upstream version has no new-chat action slot. Scope this
      // adapter to the two global New Session buttons and their displayed key.
      // Actual creation/navigation still use the official Session APIs.
      ctx.effect(() => {
        const click = event => {
          if (!event.target.closest?.('button[aria-label="新建会话"]')) return
          event.preventDefault(); event.stopImmediatePropagation(); createFreshSession()
        }
        const keydown = event => {
          if (!(event.ctrlKey && event.altKey && !event.shiftKey && event.key.toLowerCase() === 'n')) return
          event.preventDefault(); event.stopImmediatePropagation(); createFreshSession()
        }
        document.addEventListener('click', click, true); document.addEventListener('keydown', keydown, true)
        return () => { document.removeEventListener('click', click, true); document.removeEventListener('keydown', keydown, true) }
      })
      const btn = (text, fn, disabled, primary, props={}) => h('button', { type: 'button', onClick: fn, disabled, className: primary ? 'infra-primary' : '',...props }, text)
      function Evaluations() {
        const [data,setData]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState(''),[selected,setSelected]=useState('')
        const generation=useRef(0)
        async function refresh(reportId=selected){const ticket=++generation.current;setBusy(true);try{const value=await rpc('evaluation-catalog',reportId?{report_id:reportId}:{});if(ticket===generation.current){setData(value);setSelected(value.selected_report_id??'');setError(value.error??'')}}catch(e){if(ticket===generation.current)setError('评测读取失败：'+e.message)}finally{if(ticket===generation.current)setBusy(false)}}
        useEffect(()=>{refresh();return()=>{generation.current++}},[])
        const report=data?.report
        const resultLabels={passed:'独立验收通过',failed:'失败',budget_stopped:'预算停止',unavailable:'不可执行',running:'执行中'}
        return h('section',{className:'infra-stack','aria-label':'历史评测'},
          h('section',{className:'infra-card'},h('div',{className:'infra-heading'},h('h2',null,'历史评测'),btn(busy?'读取中…':'刷新结果',()=>refresh(),busy)),
            h('p',null,'查看已记录的试验、独立验收、策略开销与实验复原。此页只读取历史结果。'),
            error&&h('p',{role:'alert',className:'infra-error'},error),
            data?.history?.length>0&&h('label',null,'选择历史报告',h('select',{value:selected,disabled:busy,onChange:e=>refresh(e.target.value)},...data.history.map(r=>h('option',{key:r.id,value:r.id,disabled:!r.available},new Date(r.saved_at).toLocaleString()+' · '+r.id.slice(0,8)+' · '+(r.available?'记录 '+r.recorded+' 次':'身份未确认'))))),
            !data&&h('p',null,'正在读取评测目录…'),data&&!report&&h('p',null,'尚未发布评测结果。工程回归与已知演示不能替代未见故障成绩。')),
          report&&h('section',{className:'infra-card'},h('h3',null,report.phase==='calibration'?'开发集成本校准':'冻结留出集试验'),
            h('p',null,'计划 '+report.scheduled+' 次 · 已记录 '+report.recorded+' 次 · 已用 '+report.used_tokens.toLocaleString()+' / '+report.max_tokens.toLocaleString()+' token'),
            report.campaign&&h('p',null,'首轮累计（含早期失效试验）：'+report.campaign.used_tokens.toLocaleString()+' / '+report.campaign.max_tokens.toLocaleString()+' token；主控、子会话与上下文压缩共用额度。'),
            h('p',null,'费用：'+(report.pricing==='unpriced'?'未计价':report.trials.reduce((sum,t)=>sum+(t.cost??0),0).toFixed(4)+' '+report.trials[0]?.currency)),
            h('p',null,report.fully_paired?'已具备完整策略配对；小样本仍需按故障家族解释。':'配对尚不完整，当前不能比较策略领先。'),
            ...report.strategies.map(s=>h('p',{key:s.strategy},s.strategy+'：有效试验 '+s.executed_valid+'，通过 '+s.passed+'，误报完成 '+s.false_completions+'，不可执行 '+s.unavailable+'；'+s.tokens.toLocaleString()+' token')),
            ...report.trials.map(t=>h('details',{key:t.id},h('summary',null,t.case_id+' / '+t.strategy+' / '+t.repetition+' · '+(resultLabels[t.result]??t.phase)),
              h('p',null,'故障复现有效：'+(t.baseline_valid?'是':'尚未确认')+' · 自有实验清理：'+(t.cleanup_passed?'通过':'尚未确认')),
              h('p',null,'实际准入策略：'+(t.resolved_mode??'待核对')+' · 允许专家角色：'+(t.admitted_expert_roles??[]).join('、')),
              h('p',null,'实际子会话 '+t.experts.length+' 个 · 专家请求 '+(t.expert_calls??[]).length+' 次 · 原生审批 '+t.native_approvals+' 次 · 额外人工记录 '+t.interventions.length+' 次'),
              h('p',null,t.tokens.toLocaleString()+' token'+(t.usage_estimated?'（含估算）':'')+' · '+(t.duration_seconds==null?'尚未结束':t.duration_seconds.toFixed(1)+' 秒')),
              t.error&&h('p',null,t.error))),
            h('details',null,h('summary',null,'评测口径与版本'),h('p',null,'协议 '+report.protocol+' · manifest '+report.manifest_sha256),h('pre',null,JSON.stringify(report.identities,null,2)),...report.limits.map((limit,i)=>h('p',{key:i},limit)))),
          data&&h('details',{className:'infra-card'},h('summary',null,'评测协议与问题集（'+data.cases.length+'个目录实例，数量不是成绩）'),...data.cases.map(c=>h('p',{key:c.id},c.id+' · '+c.family+' · '+(c.split==='development'?'开发':'留出')+' · '+(c.supported?'待实际复现与验收':'评分链路待接入')))))
      }
      function History({renderSlot}) {
        const [page,setPage]=useState('evaluations')
        const tabs=[['evaluations','历史评测'],['applications','历史应用']]
        return h('div',{className:'infra-pane','data-pilot-history':true},
          h('header',null,h('h1',null,'历史记录'),h('p',null,'评测记录用于检查智能体表现；历史应用保留旧应用与试用入口。'),
            h('nav',{role:'tablist','aria-label':'历史记录分类'},...tabs.map(([key,label],index)=>h('button',{
              key,type:'button',role:'tab',id:'pilot-history-tab-'+key,'aria-selected':page===key,
              'aria-controls':'pilot-history-content',tabIndex:page===key?0:-1,onClick:()=>setPage(key),
              onKeyDown:event=>{
                if(!['ArrowLeft','ArrowRight','Home','End'].includes(event.key))return
                event.preventDefault()
                const next=event.key==='Home'?0:event.key==='End'?tabs.length-1:(index+(event.key==='ArrowRight'?1:-1)+tabs.length)%tabs.length
                setPage(tabs[next][0]);event.currentTarget.parentElement.children[next].focus()
              },
            },label)))),
          h('div',{id:'pilot-history-content',role:'tabpanel','aria-labelledby':'pilot-history-tab-'+page,
            style:{flex:1,minHeight:0},tabIndex:0},renderSlot('pilot.history.page',{}, {entryKey:page})))
      }
      function PreviousHistoryEntry() {
        useEffect(()=>{ctx.layout.selectPanel('pilot-history')},[])
        return null
      }
      function ExecutorControl({server}) {
        const [root,setRoot]=useState(''),[enabled,setEnabled]=useState(false),[busy,setBusy]=useState(false),[error,setError]=useState(''),[record,setRecord]=useState(null)
        const [assetRoot,setAssetRoot]=useState('')
        const [check,setCheck]=useState(null),[supported,setSupported]=useState(true),epoch=useRef(0)
        useEffect(()=>{let current=true;epoch.current++;setCheck(null);setError('');rpc('asset-catalog',{}).then(v=>{if(!current)return;const r=v.executors.find(e=>e.server_id===server.id),local=server.connection?.transport==='local',suggested=server.last_node_check?.server_revision===server.revision?{root:server.last_node_check.paths?.root?.path??server.last_node_check.suggested?.root,asset_root:server.last_node_check.paths?.asset_root?.path??server.last_node_check.suggested?.asset_root}:null,next=r?.root??suggested?.root??(local?v.resource_paths?.local_executor:'')??'';setRecord(r??null);setRoot(next);setAssetRoot(r?.asset_root??(r?next+'/assets':suggested?.asset_root??(local?v.resource_paths?.root:'')??''));setEnabled(r?.enabled??false);setSupported(!local||v.host_executor_supported!==false)}).catch(e=>{if(current)setError(e.message)});return()=>{current=false;epoch.current++}},[server.id,server.revision])
        async function inspectNode(){const ticket=++epoch.current;setBusy(true);setError('');try{const v=await rpc('asset-executor-check',{server_id:server.id,expected_revision:server.revision,...root?{root,asset_root:assetRoot||root+'/assets'}:{}});if(ticket!==epoch.current)return;const next=root||v.paths?.root?.path||v.suggested?.root||'',assets=assetRoot||v.paths?.asset_root?.path||v.suggested?.asset_root||'';setRoot(next);setAssetRoot(assets);setCheck({...v,checked_root:next,checked_assets:assets})}catch(e){if(ticket===epoch.current)setError(e.message)}finally{if(ticket===epoch.current)setBusy(false)}}
        const checked=check?.ready&&check.server_revision===server.revision&&check.checked_root===root&&check.checked_assets===assetRoot
        async function save(e){e.preventDefault();setBusy(true);setError('');try{setRecord(await rpc('resource-executor',{server_id:server.id,expected_revision:server.revision,root,enabled,...assetRoot?{asset_root:assetRoot}:{}}))}catch(e){setError(e.message)}finally{setBusy(false)}}
        const [confirmCleanup,setConfirmCleanup]=useState(false)
        async function cleanup(){setBusy(true);setError('');try{await rpc('asset-executor-cleanup',{server_id:server.id});setRecord(null);setEnabled(false);setConfirmCleanup(false)}catch(e){setError(e.message)}finally{setBusy(false)}}
        return h('details',{className:'infra-card'},h('summary',null,'持久执行器 · '+(record?.enabled&&record.server_revision===server.revision?'已登记（上次核验通过）':'需配置或重新核验')),
          h('p',null,'这些路径属于当前服务器。先只读检查真实数据盘、目录与Docker，再准备节点Worker；GPU任务的设备与审批另外配置。'),
          !supported&&h('p',{className:'infra-meta'},'Windows本机支持主控；节点Worker需要Linux，请选择SSH服务器。'),
          h('form',{onSubmit:save},h('label',null,'Pilot专属执行根',h('input',{value:root,disabled:busy||!supported,required:true,placeholder:'检查后按服务器数据盘推荐',onChange:e=>{setRoot(e.target.value);setCheck(null)}})),
            h('label',null,'节点资源主目录',h('input',{value:assetRoot,disabled:busy||!supported,required:true,onChange:e=>{setAssetRoot(e.target.value);setCheck(null)}})),h('p',{className:'infra-meta'},'模型保存在本节点资源目录/models/资产ID/固定版本，日志在执行根/operations。真实挂载必须属于数据盘；新目录集中在pilot-data。已有资产不自动搬迁，Docker镜像层由节点daemon管理。'),
            btn('检查节点目录与能力',inspectNode,busy||!supported||!server.enabled),
            check&&h('section',{role:'status'},h('p',null,check.ready?'节点准备条件核验通过；此次检查未创建目录或授予GPU权限':'节点准备条件未满足'),...check.reasons.map(reason=>h('p',{className:'infra-meta',key:reason},reason)),...Object.values(check.paths??{}).map(p=>h('p',{className:'infra-meta',key:p.path},p.path+' · '+(p.data_disk?'数据盘':'需核验')+(p.free_bytes?' · 可用 '+(p.free_bytes/1024**3).toFixed(1)+' GiB':'')))),
            h('label',{className:'infra-check'},h('input',{type:'checkbox',checked:enabled,disabled:busy,onChange:e=>setEnabled(e.target.checked)}),'启用并核验节点执行器'),
            btn(busy?'正在核验…':'保存执行器配置',save,busy||!supported||(!enabled?!record:!checked),true)),
          record?.probe&&h('p',{className:'infra-meta'},'Docker '+record.probe.docker+' · 固定Worker '+record.probe.worker_sha256.slice(0,12)+' · 权限/连接变化后重新核验'),
          record&&btn('清理执行器试验目录',()=>setConfirmCleanup(true),busy),confirmCleanup&&h('section',{className:'infra-card'},h('p',null,'将先把节点操作证据归档到Pilot，再移除这个执行器的专属目录和模型缓存。存在未结算操作或自有服务时拒绝清理；节点外的模型目录与共享Docker镜像保留。'),h('strong',null,record?.root),h('div',{className:'infra-actions'},btn('确认归档并清理专属目录',cleanup,busy),btn('返回',()=>setConfirmCleanup(false),busy))),
          error&&h('p',{role:'alert',className:'infra-error'},error))
      }
      function AssetWorkbench() {
        const [data,setData]=useState(null),[servers,setServers]=useState([]),[error,setError]=useState(''),[busy,setBusy]=useState(false),[form,setForm]=useState(null),[recipe,setRecipe]=useState(null),[matches,setMatches]=useState(null),[resolved,setResolved]=useState(null)
        const generation=useRef(0)
        async function refresh(){const ticket=++generation.current;try{const [v,r]=await Promise.all([rpc('asset-catalog',{}),rpc('resource-catalog',{})]);if(ticket===generation.current){setData(v);setServers(r.servers);setError('')}}catch(e){if(ticket===generation.current)setError(e.message)}}
        useEffect(()=>{refresh();return()=>{generation.current++}},[])
        async function act(action,payload){setBusy(true);setError('');try{const v=await rpc('asset-'+action,payload);await refresh();return v}catch(e){await refresh();setError(e.message);return null}finally{setBusy(false)}}
        const activeOperations=(data?.operations??[]).filter(o=>['submitted','running','unknown'].includes(o.status)).map(o=>o.id).join(',')
        useEffect(()=>{if(!activeOperations)return;let working=false,disposed=false;const timer=setInterval(async()=>{if(working||disposed)return;working=true;try{await Promise.allSettled(activeOperations.split(',').map(id=>rpc('asset-refresh',{operation_id:id})));if(!disposed)await refresh()}finally{working=false}},5000);return()=>{disposed=true;clearInterval(timer)}},[activeOperations])
        const field=(label,value,change,props={})=>h('label',null,label,h('input',{value,disabled:busy,onChange:e=>change(e.target.value),...props}))
        const options=(label,value,change,rows)=>h('label',null,label,h('select',{value,disabled:busy,onChange:e=>change(e.target.value)},...rows.map(([id,text])=>h('option',{key:id,value:id},text))))
        const set=(key,value)=>setForm(f=>({...f,[key]:value}))
        const setSource=(key,value)=>{setResolved(null);setForm(f=>({...f,source:{...f.source,[key]:value}}))}
        const csv=(value)=>value.split(',').map(s=>s.trim()).filter(Boolean)
        const newAsset=kind=>setForm({request_id:crypto.randomUUID(),expected_revision:0,asset_id:'',label:'',server_id:servers[0]?.id??'',kind,max_bytes:10*1024**3,stack_id:kind==='image'?data?.stacks[0]?.recipe.id??'':null,source:{kind:kind==='model'?'huggingface':'oci',location:'',revision:'',endpoint:'https://huggingface.co'}})
        const newRecipe=()=>setRecipe({expected_revision:0,recipe:{id:'',label:'',vendor:'',platform:'cuda',architecture:'x86_64',engine:'sglang',model_types:['qwen3'],capabilities:['chat','completions'],evidence_url:'',evidence_note:'',minimum_driver:'',attention_backends:['triton','torch_native'],device_templates:[],shared_devices:[],verified:false}})
        const updateRecipe=(key,value)=>setRecipe(r=>({...r,recipe:{...r.recipe,[key]:value}}))
        if(!data)return h('section',{className:'infra-card'},h('p',null,error||'正在读取节点资产…'),error&&btn('重试读取资产',refresh,busy))
        return h('div',{className:'infra-stack','data-pilot-assets':true},
          h('section',{className:'infra-card'},h('div',{className:'infra-heading'},h('h2',null,'模型、镜像与兼容栈'),btn('刷新资产',refresh,busy)),
            h('p',null,'登记记录来源、固定版本和目标服务器；只有下载/导入核验完成的资产才进入部署选择。兼容栈有支持资料和硬约束，匹配后仍须真实推理验收。'),
            h('div',{className:'infra-actions'},btn('添加模型',()=>newAsset('model'),busy),btn('添加镜像',()=>newAsset('image'),busy),btn('添加兼容栈',newRecipe,busy)),
            h('p',{className:'infra-meta'},'先在服务器与权限启用并核验持久执行器。模型/镜像放在目标服务器；保存登记不自动下载。')),
          data.logical_assets?.length>0&&h('section',{className:'infra-card'},h('h3',null,'同一版本的节点位置'),...data.logical_assets.map((group,i)=>h('details',{key:i},h('summary',null,group.label+' · '+group.locations.length+' 个节点位置'),h('p',{className:'infra-meta'},JSON.stringify(group.identity)),...group.locations.map(location=>h('p',{key:location.asset_id},location.server_id+' · '+(assetStates[location.status]??location.status)+' · '+(location.path??location.asset_id))),group.identity_consistent===false&&h('p',{role:'alert',className:'infra-error'},'节点清单不一致，需分别核验后再部署。')))),
          error&&h('p',{role:'alert',className:'infra-error'},error),
          recipe&&h('section',{className:'infra-card'},h('h3',null,'兼容栈支持配方'),h('form',{onSubmit:async e=>{e.preventDefault();if(await act('stack-save',recipe))setRecipe(null)}},
            h('div',{className:'infra-form-grid'},field('配方ID',recipe.recipe.id,v=>updateRecipe('id',v),{required:true}),field('配方名称',recipe.recipe.label,v=>updateRecipe('label',v),{required:true}),field('厂商/维护者',recipe.recipe.vendor,v=>updateRecipe('vendor',v),{required:true}),
              options('硬件平台',recipe.recipe.platform,v=>{updateRecipe('platform',v);updateRecipe('device_templates',v==='musa'?['/dev/mtgpu.{index}']:v==='ascend'?['/dev/davinci{index}']:[]);updateRecipe('shared_devices',[]);updateRecipe('attention_backends',v==='cuda'?['triton','torch_native']:['torch_native'])},[['cuda','NVIDIA CUDA'],['musa','摩尔线程 MUSA'],['ascend','昇腾 CANN']]),
              options('CPU架构',recipe.recipe.architecture,v=>updateRecipe('architecture',v),[['x86_64','x86_64'],['aarch64','aarch64']]),options('引擎',recipe.recipe.engine,v=>{updateRecipe('engine',v);updateRecipe('attention_backends',v==='vllm'?['auto','torch_native']:['torch_native'])},[['sglang','SGLang'],['vllm','vLLM / 平台扩展镜像']]),
              field('支持的model_type（逗号分隔）',recipe.recipe.model_types.join(','),v=>updateRecipe('model_types',csv(v)),{required:true}),field('最低驱动版本（可留空）',recipe.recipe.minimum_driver,v=>updateRecipe('minimum_driver',v)),field('注意力后端（逗号分隔）',recipe.recipe.attention_backends.join(','),v=>updateRecipe('attention_backends',csv(v))),
              field('支持量化方式（逗号分隔，默认none）',(recipe.recipe.quantizations??['none']).join(','),v=>updateRecipe('quantizations',csv(v))),
              field('单设备节点模板（逗号分隔）',recipe.recipe.device_templates.join(','),v=>updateRecipe('device_templates',csv(v))),field('共享控制设备（逗号分隔）',recipe.recipe.shared_devices.join(','),v=>updateRecipe('shared_devices',csv(v))),field('官方/厂商支持资料URL',recipe.recipe.evidence_url,v=>updateRecipe('evidence_url',v),{type:'url',required:true})),
            h('fieldset',null,h('legend',null,'镜像依赖精确版本（按支持资料填写）'),...(recipe.recipe.runtime_versions??[]).map((c,i)=>h('div',{key:i,className:'infra-form-grid'},options('依赖包',c.package,v=>updateRecipe('runtime_versions',recipe.recipe.runtime_versions.map((r,j)=>j===i?{...r,package:v}:r)),[['torch','Torch'],['torch_musa','Torch MUSA'],['torch_npu','Torch NPU'],['sglang','SGLang'],['vllm','vLLM']]),field('精确版本',c.version,v=>updateRecipe('runtime_versions',recipe.recipe.runtime_versions.map((r,j)=>j===i?{...r,version:v}:r))),btn('移除此约束',()=>updateRecipe('runtime_versions',recipe.recipe.runtime_versions.filter((_,j)=>j!==i)),busy))),btn('增加依赖约束',()=>updateRecipe('runtime_versions',[...(recipe.recipe.runtime_versions??[]),{package:'torch',version:''}]),busy)),
            h('label',null,'支持条件与版本说明',h('textarea',{value:recipe.recipe.evidence_note,required:true,minLength:10,onChange:e=>updateRecipe('evidence_note',e.target.value)})),
            h('p',{className:'infra-meta'},'只声明资料支持条件，不手工标记已验证；当前执行协议为单机chat/completions，PD与完整准确率另行实现。'),
            h('div',{className:'infra-actions'},h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存兼容栈'),btn('取消',()=>setRecipe(null),busy)))),
          form&&h('section',{className:'infra-card'},h('h3',null,form.kind==='model'?'模型登记':'镜像登记'),h('form',{onSubmit:async e=>{e.preventDefault();if(await act('save',form)){setForm(null);setResolved(null)}}},
            h('div',{className:'infra-form-grid'},field('资产ID',form.asset_id,v=>set('asset_id',v),{required:true,pattern:'[A-Za-z0-9_-]+'}),field('显示名称',form.label,v=>set('label',v),{required:true}),options('目标服务器',form.server_id,v=>set('server_id',v),servers.map(s=>[s.id,s.label])),
              options('资源来源',form.source.kind,v=>setSource('kind',v),form.kind==='model'?[['huggingface','下载固定模型版本'],['existing','导入节点现有目录']]:[['oci','下载固定OCI镜像'],['existing','导入节点现有镜像ID']]),
              field(form.source.kind==='huggingface'?'模型组织/仓库':form.source.kind==='oci'?'镜像仓库@sha256摘要':form.kind==='model'?'目标节点模型绝对目录':'完整sha256镜像ID',form.source.location,v=>setSource('location',v),{required:true}),
              field('最大资源大小 GiB',form.max_bytes/1024**3,v=>set('max_bytes',Math.round(Number(v)*1024**3)),{type:'number',min:.001,step:'any'}),
              form.kind==='image'&&options('兼容栈配方',form.stack_id??'',v=>set('stack_id',v),[['','请选择配方'],...data.stacks.map(s=>[s.recipe.id,s.recipe.label])])),
            form.source.kind==='huggingface'&&h('section',null,options('模型仓库入口',form.source.endpoint,v=>setSource('endpoint',v),[['https://huggingface.co','Hugging Face'],['https://hf-mirror.com','HF镜像站']]),
              field('固定40位revision',form.source.revision,v=>setSource('revision',v)),btn('解析当前版本与大小',async()=>{const r=await act('resolve-model',{server_id:form.server_id,repository:form.source.location,endpoint:form.source.endpoint,revision:form.source.revision||'main'});if(r){setForm(f=>({...f,source:{...f.source,revision:r.revision}}));setResolved(r)}},busy||!form.source.location),
              resolved&&h('p',null,'固定版本 '+resolved.revision+' · '+resolved.files.length+'个文件 · '+(resolved.bytes/1024**3).toFixed(3)+' GiB。保存前核对来源；不会立即下载。')),
            form.source.kind==='oci'&&btn('解析镜像版本与摘要',async()=>{const r=await act('resolve-image',{server_id:form.server_id,reference:form.source.location});if(r?.candidates?.length)setSource('location',r.candidates[0].location)},busy||!form.source.location),
            h('div',{className:'infra-actions'},h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存资产登记'),btn('取消',()=>setForm(null),busy)))),
          h('section',{className:'infra-card'},h('h3',null,'节点资产'),!data.assets.length&&h('p',null,'尚无新增节点资产；已有本机资产仍可在部署方案中选择。'),
            ...data.assets.map(a=>h('section',{key:a.asset_id,className:'infra-card'},h('h3',null,a.label+' · '+(a.kind==='model'?'模型':'镜像')),h('p',null,a.server_id+' · '+(assetStates[a.status]??a.status)+' · 登记版本 '+a.revision),h('p',{className:'infra-meta'},a.source.location+(a.source.revision?' / '+a.source.revision:'')),
              a.prepared&&h('p',null,'核验身份 '+(a.prepared.digest??a.prepared.manifest?.digest)+' · '+((a.prepared.bytes??a.prepared.manifest?.bytes??0)/1024**3).toFixed(3)+' GiB'),
              h('div',{className:'infra-actions'},!a.operation_id&&a.status!=='ready'&&btn(a.source.kind==='existing'?'核验并导入':'开始下载并核验',()=>act('prepare',{asset_id:a.asset_id,expected_revision:a.revision,request_id:crypto.randomUUID()}),busy,true),
                !a.operation_id&&btn('编辑登记',()=>setForm({request_id:crypto.randomUUID(),expected_revision:a.revision,asset_id:a.asset_id,server_id:a.server_id,kind:a.kind,label:a.label,source:a.source,max_bytes:a.max_bytes,stack_id:a.stack_id}),busy),
                a.operation_id&&btn('核对原准备操作',()=>act('refresh',{operation_id:a.operation_id}),busy),a.operation_id&&btn('请求取消原操作',()=>act('cancel',{operation_id:a.operation_id}),busy),
                a.operation_id&&a.kind==='model'&&data.operations.find(o=>o.id===a.operation_id)?.status==='unknown'&&btn('核对已退出的下载任务',()=>act('reconcile',{operation_id:a.operation_id}),busy),a.kind==='model'&&btn('匹配兼容栈',async()=>{const v=await act('match',{server_id:a.server_id,asset_id:a.asset_id,capabilities:['chat','completions']});if(v)setMatches(v)},busy))))),
          (data.proposals??[]).length>0&&h('section',{className:'infra-card'},h('h3',null,'智能体登记提案'),...data.proposals.map(p=>h('section',{key:p.id,className:'infra-card'},h('h3',null,p.kind==='asset'?p.payload.label:p.payload.recipe.label),h('p',null,p.kind==='asset'?p.payload.server_id+' · '+p.payload.source.location:p.payload.recipe.platform+' / '+p.payload.recipe.engine+' · '+p.payload.recipe.evidence_note),h('p',{className:'infra-meta'},'状态 '+(assetStates[p.status]??p.status)+'；登记不会立即下载或部署。'),h('details',null,h('summary',null,'审阅完整登记条件'),h('pre',null,JSON.stringify(p.payload,null,2))),['pending','registering'].includes(p.status)&&h('div',{className:'infra-actions'},btn('批准并登记',()=>act('register-proposal',{proposal_id:p.id,expected_digest:p.digest,decision:'register'}),busy,true),btn('拒绝登记',()=>act('register-proposal',{proposal_id:p.id,expected_digest:p.digest,decision:'reject'}),busy||p.status==='registering'))))),
          matches&&h('section',{className:'infra-card'},h('h3',null,'当前节点兼容候选'),...matches.candidates.map(c=>h('section',{key:c.id},h('strong',null,c.label+' · '+(c.matched?'硬约束匹配':'未满足条件')+' · '+(c.verification==='historical_runtime'?'同类任务已有验收证据':'仅资料声明支持')),h('p',null,[...c.reasons,...c.unknown,c.note].join('；')),h('a',{href:c.evidence_url,target:'_blank',rel:'noreferrer'},'支持资料')))),
          data.stacks.length>0&&h('section',{className:'infra-card'},h('h3',null,'兼容栈登记'),...data.stacks.map(s=>h('section',{key:s.recipe.id},h('strong',null,s.recipe.label+' · '+s.recipe.platform+' / '+s.recipe.engine),h('p',null,s.recipe.evidence_note),btn('编辑兼容栈',()=>setRecipe({expected_revision:s.revision,recipe:s.recipe}),busy)))),
          data.operations.length>0&&h('section',{className:'infra-card'},h('h3',null,'下载与核验操作'),...data.operations.map(o=>h('details',{key:o.id},h('summary',null,o.asset_id+' · '+(assetStates[o.status]??o.status)),h('pre',null,JSON.stringify(o.node??{operation_id:o.id},null,2))))))
      }
      function DockerControl({server,onOpenTask}) {
        const [data,setData]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
        async function refresh(){setBusy(true);setError('');try{setData(await rpc('asset-docker-inventory',{server_id:server.id}))}catch(e){setError(e.message)}finally{setBusy(false)}}
        useEffect(()=>{setData(null);setError('')},[server.id,server.revision])
        return h('details',{className:'infra-card'},h('summary',null,'Docker 镜像与容器'),btn('读取 Docker 状态',refresh,busy||!server.enabled),
          error&&h('p',{role:'alert',className:'infra-error'},error),data&&h('section',null,
            h('p',null,'Docker '+data.daemon.version+' · '+data.daemon.root+' · '+(data.daemon.data_disk?'数据盘':'系统盘')),
            h('p',{className:'infra-meta'},data.management_boundary),
            ...data.containers.map(c=>h('section',{key:c.id},h('p',null,c.name+' · '+c.state+' · '+(c.owned?'本项目 / '+c.task_id:'外部容器，仅查看')),
              c.owned&&c.task_id&&onOpenTask&&btn('查看所属任务',()=>onOpenTask(c),busy))),
            h('details',null,h('summary',null,'镜像 '+data.images.length+' 项'),...data.images.map(i=>h('p',{key:i.Id},(i.RepoTags??[]).join(', ')+' · '+i.Id+' · '+(i.Size/1024**3).toFixed(2)+' GiB')))))
      }
      function SoftwareRecipeForm({onSaved}) {
        const [models,setModels]=useState([]),[modelId,setModelId]=useState('')
        const [servers,setServers]=useState([]),[node,setNode]=useState(''),[images,setImages]=useState([]),[image,setImage]=useState(''),[template,setTemplate]=useState('http_metrics'),[id,setId]=useState(''),[label,setLabel]=useState(''),[python,setPython]=useState('/usr/bin/python3'),[error,setError]=useState(''),[busy,setBusy]=useState(false)
        useEffect(()=>{let active=true;rpc('resource-catalog',{}).then(v=>{if(active){setServers(v.servers);setNode(v.servers[0]?.id??'')}}).catch(e=>setError(e.message));return()=>{active=false}},[])
        useEffect(()=>{let active=true;setModelId('');rpc('asset-catalog',{}).then(v=>{if(active)setModels(v.assets.filter(a=>a.server_id===node&&a.kind==='model'&&a.status==='ready'))}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[node])
        useEffect(()=>{let active=true;setImages([]);setImage('');setError('');if(node)rpc('asset-docker-inventory',{server_id:node}).then(v=>{if(active){setImages(v.images);setImage(v.images[0]?.Id??'');setError('')}}).catch(e=>{if(active)setError(e.message)});return()=>{active=false}},[node])
        const input=(name,value,change)=>h('label',null,name,h('input',{value,required:true,disabled:busy,onChange:e=>change(e.target.value)}))
        return h('details',{className:'infra-card'},h('summary',null,'登记软件验证配方'),h('p',null,'固定节点、已有镜像、镜像内解释器与独立检查。自定义编译配方也可通过业务 CLI 登记。'),
          h('form',{onSubmit:async e=>{e.preventDefault();setBusy(true);setError('');try{await rpc('workbench-save-profile',{expected_revision:0,recipe:{id,label,server_id:node,image_digest:image,python,template,timeout:template==='pd_vllm_shared'?600:120,...template==='pd_vllm_shared'?{model_asset_id:modelId}:{}}});await onSaved()}catch(e){setError(e.message)}finally{setBusy(false)}}},
            input('配方ID',id,setId),input('配方名称',label,setLabel),h('label',null,'已登记服务器',h('select',{value:node,disabled:busy,onChange:e=>setNode(e.target.value)},...servers.map(s=>h('option',{key:s.id,value:s.id},s.label)))),
            h('label',null,'节点已有固定镜像',h('select',{value:image,disabled:busy,onChange:e=>setImage(e.target.value)},...images.map(i=>h('option',{key:i.Id,value:i.Id},(i.RepoTags??[]).join(', ')+' · '+i.Id.slice(7,19))))),
            input('镜像内 Python 绝对路径',python,setPython),h('label',null,'独立验证模板',h('select',{value:template,disabled:busy,onChange:e=>setTemplate(e.target.value)},h('option',{value:'http_metrics'},'推理评测指标脚本'),h('option',{value:'cpu_profile'},'CPU profiling'),h('option',{value:'rmsnorm_cuda'},'CUDA RMSNorm 数值与性能'),h('option',{value:'pd_vllm_shared'},'vLLM 0.8.5 同机1P1D KV复用验证'))),
            template==='pd_vllm_shared'&&h('section',null,h('label',null,'本节点已核验模型',h('select',{required:true,value:modelId,disabled:busy,onChange:e=>setModelId(e.target.value)},h('option',{value:''},'请选择模型'),...models.map(m=>h('option',{key:m.asset_id,value:m.asset_id},m.label)))),h('p',{className:'infra-meta'},'使用上游 SharedStorageConnector 的调试路径，验证实际KV存取和输出一致性；不建立在线或跨机器PD服务。')),
            h('p',{className:'infra-meta'},'指标脚本验证统计口径；CUDA模板验证8组数值案例和基线性能。GPU任务启动前检查配额与占用。'),
            h('button',{type:'submit',disabled:busy||!image,className:'infra-primary'},'保存软件配方')),error&&h('p',{role:'alert',className:'infra-error'},error))
      }
      function DistributedWorkbench() {
        const [data,setData]=useState(null),[tasks,setTasks]=useState([]),[mode,setMode]=useState('sglang_pd')
        const [left,setLeft]=useState({server_id:'',address:'',gpus:'',model:'',image:'',rdma:'',ports:'31001,31002,31003'})
        const [right,setRight]=useState({server_id:'',address:'',gpus:'',model:'',image:'',rdma:'',ports:'31001'})
        const [title,setTitle]=useState('双节点推理方案'),[modelName,setModelName]=useState(''),[result,setResult]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
        const portKeys={prefill:['api','bootstrap','router'],decode:['api'],head:['gcs','api','nccl','dashboard','ray_client','node_manager','object_manager','agent_http','agent_grpc','runtime_env','metrics','worker_1','worker_2','worker_3','worker_4'],worker:['node_manager','object_manager','agent_http','agent_grpc','runtime_env','metrics','worker_1','worker_2','worker_3','worker_4']}
        async function refresh(){const [r,t]=await Promise.all([rpc('resource-catalog',{}),rpc('distributed-catalog',{})]);setData(r);setTasks(t);setLeft(v=>({...v,server_id:v.server_id||r.servers[0]?.id||''}));setRight(v=>({...v,server_id:v.server_id||r.servers[1]?.id||''}))}
        useEffect(()=>{refresh().catch(e=>setError(e.message))},[])
        function selectMode(value){setMode(value);setResult(null);const ray=value!=='sglang_pd';setLeft(v=>({...v,ports:ray?'31001,31002,31003,31004,31005,31006,31007,31008,31009,31010,31011,31012,31013,31014,31015':'31001,31002,31003'}));setRight(v=>({...v,ports:ray?'31006,31007,31008,31009,31010,31011,31012,31013,31014,31015':'31001'}))}
        function payload(){
          const build=(value,role)=>{const keys=portKeys[role],ports=numberList(value.ports),gpus=numberList(value.gpus);if(ports.length!==keys.length||ports.some(n=>!Number.isInteger(n))||!gpus.length||gpus.some(n=>!Number.isInteger(n)))throw new Error('请按标注填写完整端口和GPU编号');return {server_id:value.server_id,role,address:value.address,gpu_ids:gpus,model_asset:value.model,image_asset:value.image,rdma_device:mode==='sglang_pd'?value.rdma:null,ports:Object.fromEntries(keys.map((k,i)=>[k,ports[i]]))}}
          return {request_id:crypto.randomUUID(),title,mode,served_model_name:modelName,nodes:[build(left,mode==='sglang_pd'?'prefill':'head'),build(right,mode==='sglang_pd'?'decode':'worker')]}
        }
        async function act(action,args){setBusy(true);setError('');try{const value=await rpc('distributed-'+action,args);if(action==='preflight'||action==='review')setResult(value.preflight??value);else if(action==='create'){await refresh();setResult(null)}}catch(e){setError(e.message)}finally{setBusy(false)}}
        const nodeForm=(value,set,role)=>{const models=data?.assets.models.filter(m=>m.server_id===value.server_id)??[],images=data?.assets.images.filter(m=>m.server_id===value.server_id)??[];const change=(key,next)=>{setResult(null);set(v=>({...v,[key]:next}))};const field=(label,key)=>h('label',null,label,h('input',{value:value[key],disabled:busy,onChange:e=>change(key,e.target.value)}));return h('fieldset',null,h('legend',null,role),h('label',null,'服务器',h('select',{value:value.server_id,disabled:busy,onChange:e=>{setResult(null);set(v=>({...v,server_id:e.target.value,address:'',model:'',image:'',rdma:''}))}},h('option',{value:''},'请选择'),...(data?.servers??[]).map(s=>h('option',{key:s.id,value:s.id},s.label)))),field('节点专用 IPv4','address'),field('GPU 编号（逗号分隔）','gpus'),mode==='sglang_pd'&&field('RDMA 设备名','rdma'),h('label',null,'已核验模型',h('select',{value:value.model,disabled:busy,onChange:e=>change('model',e.target.value)},h('option',{value:''},'请选择'),...models.map(m=>h('option',{key:m.id,value:m.id},m.label)))),h('label',null,'已核验镜像',h('select',{value:value.image,disabled:busy,onChange:e=>change('image',e.target.value)},h('option',{value:''},'请选择'),...images.map(m=>h('option',{key:m.id,value:m.id},m.label)))),field('固定端口：'+portKeys[role].join('、'),'ports'))}
        return h('section',{className:'infra-card'},h('h2',null,'跨机 PD / Ray 方案'),h('p',null,'可保存双节点拓扑并检查已有权限、设备、资产与固定端口。跨机执行和动态端口隔离尚未开放，此处不会启动服务。'),
          error&&h('p',{role:'alert',className:'infra-error'},error),h('form',{onSubmit:e=>{e.preventDefault();try{act('create',payload())}catch(x){setError(x.message)}}},
            h('label',null,'部署类型',h('select',{value:mode,disabled:busy,onChange:e=>selectMode(e.target.value)},h('option',{value:'sglang_pd'},'SGLang 在线 PD（1P1D）'),h('option',{value:'sglang_ray'},'SGLang + Ray'),h('option',{value:'vllm_ray'},'vLLM + Ray'))),
            h('label',null,'方案名称',h('input',{value:title,disabled:busy,onChange:e=>{setResult(null);setTitle(e.target.value)}})),h('label',null,'统一服务模型名',h('input',{value:modelName,disabled:busy,onChange:e=>{setResult(null);setModelName(e.target.value)}})),
            nodeForm(left,setLeft,mode==='sglang_pd'?'prefill':'head'),nodeForm(right,setRight,mode==='sglang_pd'?'decode':'worker'),
            btn('静态预检',()=>{try{act('preflight',payload())}catch(e){setError(e.message)}},busy),h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存拓扑草稿')),
          result&&h('section',null,h('h3',null,result.ready_for_review?'静态条件已满足':'静态条件待处理'),h('p',null,'方案：'+(result.plan?.title??'')),...(result.issues??[]).map((x,i)=>h('p',{key:i},x)),...(result.execution_blockers??[]).map((x,i)=>h('p',{key:'block-'+i,className:'infra-meta'},'执行待完成：'+x)),...(result.limitations??[]).map((x,i)=>h('p',{key:'limit-'+i,className:'infra-meta'},x))),
          ...tasks.map(t=>h('section',{key:t.id},h('p',null,t.plan.title+' · '+t.status),t.status==='draft'&&btn('重新预检',()=>act('review',{task_id:t.id}),busy))))
      }
      function NetworkWorkbench() {
        const [servers,setServers]=useState([]),[selected,setSelected]=useState([]),[tasks,setTasks]=useState([]),[proposal,setProposal]=useState(null),[busy,setBusy]=useState(false),[error,setError]=useState('')
        async function refresh(){const [r,t]=await Promise.all([rpc('resource-catalog',{}),rpc('network-catalog',{})]);setServers(r.servers);setTasks(t)}
        useEffect(()=>{refresh().catch(e=>setError(e.message))},[])
        async function act(name,args){setBusy(true);setError('');try{const value=await rpc('network-'+name,args);if(name==='recommend'){if(!value.available)throw new Error(value.reason);setProposal({request_id:crypto.randomUUID(),title:'多节点双向TCP链路验证',peers:value.peers,payload_bytes:1048576,rounds:2,duration_seconds:90})}else{await refresh();if(name==='create')setProposal(null)}}catch(e){setError(e.message)}finally{setBusy(false)}}
        return h('section',{className:'infra-stack'},h(DistributedWorkbench),h('section',{className:'infra-card'},h('h2',null,'多节点链路验证'),h('p',null,'按已登记节点的既有接口推荐方案，验证双向载荷和节点身份，结束后恢复。此项不证明 RDMA 或 PD 的 KV 传输。'),
          ...servers.map(s=>h('label',{key:s.id,className:'infra-check'},h('input',{type:'checkbox',checked:selected.includes(s.id),disabled:busy,onChange:e=>{setSelected(v=>e.target.checked?[...v,s.id]:v.filter(x=>x!==s.id));setProposal(null)}}),s.label)),
          btn('检测接口并出方案',()=>act('recommend',{server_ids:selected}),busy||selected.length<2||selected.length>4),error&&h('p',{role:'alert',className:'infra-error'},error)),
          proposal&&h('section',{className:'infra-card'},h('h3',null,'审阅链路方案'),...proposal.peers.map((p,i)=>h('section',{key:p.server_id},h('p',null,p.server_id+' · '+p.address),h('label',null,'专属监听端口',h('input',{type:'number',min:1024,max:65535,value:p.port,disabled:busy,onChange:e=>setProposal(v=>({...v,peers:v.peers.map((x,j)=>j===i?{...x,port:Number(e.target.value)}:x)}))})))),h('p',null,'每方向2次，每次1 MiB；限时90秒。端口须在节点权限中登记。'),btn('保存方案',()=>act('create',proposal),busy,true)),
          ...tasks.map(t=>h('section',{key:t.id,className:'infra-card'},h('h3',null,t.plan.title),h('p',null,t.status+' · 清理 '+(t.restored?'已确认':'未确认')),t.status==='draft'&&btn('执行并恢复',()=>act('run',{task_id:t.id}),busy,true),!t.restored&&t.status!=='draft'&&btn('重试原任务清理',()=>act('cleanup',{task_id:t.id}),busy),...t.results.map((r,i)=>h('p',{key:i},r.source+' → '+r.target+' · '+(r.passed?'校验通过':'未通过'))),t.error&&h('p',{role:'alert'},t.error))))
      }
      function BuildWorkbench({taskId}={}) {
        const [data,setData]=useState(null),[chosen,setChosen]=useState(taskId??''),[profile,setProfile]=useState(''),[title,setTitle]=useState(''),[goal,setGoal]=useState('')
        const [busy,setBusy]=useState(false),[error,setError]=useState(''),[source,setSource]=useState(null),[candidate,setCandidate]=useState('')
        const [gpuText,setGpuText]=useState('')
        const [serverFilter,setServerFilter]=useState('')
        const task=data?.tasks.find(t=>t.id===chosen)
        useEffect(()=>{if(taskId)setChosen(taskId)},[taskId])
        async function refresh(){const v=await rpc('workbench-catalog',{});setData(v);setProfile(p=>p||v.profiles[0]?.id||'')}
        useEffect(()=>{refresh().catch(e=>setError(e.message))},[])
        async function act(action,args={}){
          setBusy(true);setError('')
          try {
            const v=await rpc('workbench-'+action,action==='create'?args:{task_id:chosen,...args})
            if(action==='read')setSource(v)
            if(action==='write')setSource(s=>({...s,sha256:v.sha256}))
            if(action==='freeze')setCandidate(v.candidates.at(-1).candidate_id)
            if(action==='create'){setChosen(v.id);setSource(null);setCandidate('')}
            await refresh()
          }catch(e){setError(e.message)}finally{setBusy(false)}
        }
        const states={draft:'待审阅',preparing:'准备中',prepared:'工作区就绪',unknown:'需要核对',restored:'已清理复核'}
        async function sourceBundle(id){setBusy(true);setError('');try{const value=await rpc('workbench-source-download',{task_id:chosen,candidate_id:id});const bytes=Uint8Array.from(atob(value.data),c=>c.charCodeAt(0));const url=URL.createObjectURL(new Blob([bytes],{type:'application/gzip'}));const link=document.createElement('a');link.href=url;link.download=value.filename;link.click();setTimeout(()=>URL.revokeObjectURL(url),1000)}catch(e){setError(e.message)}finally{setBusy(false)}}
        return h('section',{className:'infra-stack'},
          h(SoftwareRecipeForm,{onSaved:refresh}),
          h('section',{className:'infra-card'},h('h2',null,'软件与算子工作区'),h('p',null,'在登记节点的专属目录修改源码、冻结候选，执行独立编译、评测脚本、profiling或GPU算子检查。'),
            h('p',{className:'infra-meta'},'构建步骤和验收由登记执行器定义，目标描述不会自动增加测试项。'),
            error&&h('p',{className:'infra-error',role:'alert'},error),
            !data?h('p',null,'正在读取工作区…'):!data.profiles.length?h('p',null,'尚未登记环境与源码执行器，当前无法创建此类任务。'):
            h('form',{onSubmit:e=>{e.preventDefault();act('create',{request_id:crypto.randomUUID(),title,goal,profile_id:profile,gpu_ids:data.profiles.find(p=>p.id===profile)?.platform==='cpu'?[]:numberList(gpuText)})}},
              h('div',{className:'infra-form-grid'},h('label',null,'执行器',h('select',{value:profile,disabled:busy,onChange:e=>setProfile(e.target.value)},...data.profiles.map(p=>h('option',{key:p.id,value:p.id},p.label)))),
                h('label',null,'任务名称',h('input',{required:true,minLength:2,value:title,disabled:busy,onChange:e=>setTitle(e.target.value)}))),
              h('label',null,'目标与验证要求',h('textarea',{required:true,minLength:5,value:goal,disabled:busy,onChange:e=>setGoal(e.target.value),rows:3})),
              data.profiles.find(p=>p.id===profile)?.platform!=='cpu'&&h('label',null,'独立 GPU 编号（逗号分隔）',h('input',{value:gpuText,required:true,disabled:busy,onChange:e=>setGpuText(e.target.value)})),
              btn('检测并推荐当前资源',async()=>{setBusy(true);setError('');try{const v=await rpc('workbench-recommend',{profile_id:profile});setGpuText(v.gpu_ids.join(','));if(!v.available)setError(v.reasons.join('；'))}catch(e){setError(e.message)}finally{setBusy(false)}},busy),
              h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存工作区方案'))),
          data&&h('section',{className:'infra-card'},h('h3',null,'工作区任务'),
            h('label',null,'按服务器筛选',h('select',{'aria-label':'按服务器筛选工作区任务',value:serverFilter,onChange:e=>{setServerFilter(e.target.value);setChosen('');setSource(null);setCandidate('')}},h('option',{value:''},'全部服务器'),...[...new Set(data.tasks.map(t=>t.server_id??t.execution_scope?.server_id??'未记录服务器'))].sort().map(id=>h('option',{key:id,value:id},id)))),
            !data.tasks.filter(t=>!serverFilter||(t.server_id??t.execution_scope?.server_id??'未记录服务器')===serverFilter).length&&h('p',null,'没有符合条件的工作区任务。'),
            ...data.tasks.filter(t=>!serverFilter||(t.server_id??t.execution_scope?.server_id??'未记录服务器')===serverFilter).map(t=>btn(t.plan.title+' · '+(t.server_id??t.execution_scope?.server_id??'未记录服务器')+' · '+({operator:'算子',profile:'性能剖析',pd:'PD验证',build:'构建'}[t.kind]??'软件')+' · '+(states[t.status]??t.status)+' · '+t.id.slice(0,8),()=>{setChosen(t.id);setSource(null);setCandidate(t.candidates.at(-1)?.candidate_id??'')},busy))),
          task&&h('section',{className:'infra-card'},h('h2',null,task.plan.title),h('p',null,task.plan.goal),h('p',null,states[task.status]??task.status),
            task.execution_scope&&h('section',{'aria-label':'软件执行范围'},h('h3',null,'方案与执行范围'),h('p',null,'节点 '+task.execution_scope.server_id+' · '+task.execution_scope.platform+' · GPU '+(task.execution_scope.gpu_ids.join(',')||'无')),h('p',null,'目录 '+task.execution_scope.directory),h('p',null,task.execution_scope.cpu_cores+' CPU / '+task.execution_scope.memory_gib+' GiB / '+task.execution_scope.timeout_seconds+' 秒'),h('details',null,h('summary',null,'固定镜像'),h('code',null,task.execution_scope.image_digest))),
            task.session_id&&btn('任务对话',()=>ctx.uiWorkspace.openSession(task.session_id),busy),
            task.status==='draft'&&btn('准备隔离工作区',()=>act('prepare'),busy,true),
            task.status==='prepared'&&!task.active_operation&&h('div',null,
              task.session_id&&btn('让智能体执行此目标',()=>act('agent'),busy,true),
              btn('冻结源码候选',()=>act('freeze'),busy),
              h('select',{'aria-label':'构建候选',value:candidate,disabled:busy,onChange:e=>setCandidate(e.target.value)},h('option',{value:''},'选择候选'),...task.candidates.map(c=>h('option',{key:c.candidate_id,value:c.candidate_id},c.candidate_id))),
              btn('环境、编译与验证',()=>act('run',{candidate_id:candidate,request_id:crypto.randomUUID()}),busy||!candidate,true),
              h('h3',null,'工作副本源码'),...Object.keys(task.files??{}).map(path=>btn(path,()=>act('read',{path}),busy))),
            task.active_operation&&btn('核对原构建操作',()=>act('recover'),busy),
            task.status!=='restored'&&btn('清理并复核',()=>act('cleanup'),busy),
            task.restored&&h('p',null,'本任务工作区和容器已清理，证据记录保留。'),
            task.candidates.some(c=>c.source_archive)&&h('section',null,h('h3',null,'候选源码归档'),h('p',{className:'infra-meta'},'完整冻结源码保存在主控数据目录，服务器清理后仍可下载。'),...task.candidates.filter(c=>c.source_archive).map(c=>btn('下载 '+c.candidate_id+' 源码归档',()=>sourceBundle(c.candidate_id),busy))),
            task.source_archive_version&&task.candidates.some(c=>!c.source_archive)&&h('section',null,h('p',{role:'alert'},'候选已冻结，但源码归档未确认；清理前需补齐。'),...task.candidates.filter(c=>!c.source_archive).map(c=>btn('归档 '+c.candidate_id+' 源码',()=>act('source-export',{candidate_id:c.candidate_id}),busy))),
            task.status==='prepared'&&h('p',{className:'infra-meta'},'可以直接编辑并构建，也可打开任务对话，让智能体根据目标继续执行。准备工作区本身不会启动对话。'),
            source&&task.status==='prepared'&&!task.active_operation&&h('section',null,h('h3',null,source.path),h('textarea',{'aria-label':'工作副本源码',rows:15,value:source.content,disabled:busy,onChange:e=>setSource(s=>({...s,content:e.target.value}))}),btn('保存源码修改',()=>act('write',{path:source.path,content:source.content,expected_sha256:source.sha256}),busy)),
            task.validation&&h('section',{'aria-label':'工作区验收范围'},h('h3',null,'验收范围'),
              h('p',null,'最新冻结候选 '+(task.validation.candidate_id??'尚未创建')+' · 登记构建检查：'+({passed:'通过',failed:'失败',not_run:'未执行',unsettled:'需核对原操作',unverified:'证据未确认'}[task.validation.pipeline]??'未确认')),
              task.validation.working_copy_matches_candidate===false&&h('p',null,'工作副本与该候选不同，需要冻结并构建新的修改。'),
              h('p',null,'业务检查：'+task.validation.business_goal+' · GPU数值：'+task.validation.gpu_numerical+' · 算子性能：'+task.validation.operator_performance),
              h('p',{className:'infra-meta'},'推理服务：'+task.validation.inference_service+'。以上仅覆盖配方固定检查。'),
              h('p',null,'实验清理：'+(task.validation.restoration==='passed'?'已确认':'尚未确认'))),
            h('h3',null,'构建与验证证据'),...task.results.map(r=>h('details',{key:r.operation_id,open:true},h('summary',null,r.candidate_id+' · '+(r.passed?'登记构建检查通过':'登记构建检查失败')),
              h('p',null,'GPU 执行：'+(r.gpu_execution?'是':'否')+' · 冻结源码保持：'+(r.frozen_source_unchanged?'是':'待核对')),
              ...(r.steps??[]).map((s,i)=>h('section',{key:i},h('strong',null,s.stage+' · 退出码 '+s.exit_code),h('pre',null,[s.stdout,s.stderr,s.error].filter(Boolean).join('\n')))),r.error&&h('pre',null,r.error))),
            btn('下载任务证据',()=>{const blob=new Blob([JSON.stringify(task,null,2)],{type:'application/json'});const url=URL.createObjectURL(blob);const a=document.createElement('a');a.href=url;a.download='pilot-workbench-'+task.id+'.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000)},busy)))
      }
      function Editor({ assets, tasks, initial, onSave, onCancel, busy, resourceReview=false }) {
        const [plan, setPlan] = useState(initial ?? fresh(assets))
        const [matrixText,setMatrixText]=useState({input_lengths:plan.workload.input_lengths.join(','),concurrency:plan.workload.concurrency.join(',')})
        const set = (key, value) => setPlan(p => ({ ...p, [key]: value }))
        const section = (group, key, value) => setPlan(p => ({ ...p, [group]: { ...p[group], [key]: value } }))
        const text = (title, key, multiline = false) => h('label', null, title, h(multiline ? 'textarea' : 'input', { value: plan[key], required: true, minLength:multiline?5:2,maxLength:multiline?2400:80,onChange: e => set(key, e.target.value), rows: multiline ? 3 : undefined }))
        const number = (title, key, group, min, max) => h('label', null, title, h('input', { type: 'number', min, max, step: key === 'mem_fraction_static' ? .01 : 1,
          value: group ? plan[group][key] : plan[key], onChange: e => group ? section(group, key, Number(e.target.value)) : set(key, Number(e.target.value)) }))
        const onNode=(asset,id=plan.machine_id)=>asset.server_id?asset.server_id===id:(assets.machines.find(m=>m.id===id)?.transport??'local')==='local'
        const change=(key,value)=>{
          if(key==='kind'){setPlan(p=>({...p,kind:value,candidate_ids:[],target_task_id:value==='deploy_verify'?null:p.target_task_id}));return}
          if(key==='machine_id'){
            const model=assets.models.find(a=>onNode(a,value)),image=assets.images.find(a=>onNode(a,value)&&(a.backend??'docker')==='docker')
            setPlan(p=>({...p,machine_id:value,model_asset:model?.id??'',image_asset:image?.id??'',gpu_ids:assets.machines.find(m=>m.id===value)?.allowed_gpu_ids??[],engine_source_profile:null,serving:{...p.serving,attention_backend:image?.stack_recipe?.attention_backends[0]??(image?.engine==='vllm'?'auto':'triton')}}));return
          }
          if(key==='image_asset'){const image=assets.images.find(i=>i.id===value);setPlan(p=>({...p,image_asset:value,engine_source_profile:(assets.engine_source_profiles??[]).some(r=>r.id===p.engine_source_profile&&r.image_id===value)?p.engine_source_profile:null,serving:{...p.serving,mem_fraction_static:image?.serving_defaults?.mem_fraction_static??.18,attention_backend:image?.stack_recipe?.attention_backends[0]??(image?.engine==='vllm'?'auto':'triton')}}));return}
          set(key,value)
        }
        const select = (title, key, entries) => h('label', null, title, h('select', { value: plan[key], disabled: plan.kind!=='deploy_verify'&&['machine_id','model_asset','image_asset'].includes(key), onChange:e=>change(key,e.target.value) }, ...entries.map(([v,t]) => h('option', { key: v, value: v }, t))))
        const targets = tasks.filter(v => v.task.plan.kind === 'deploy_verify' && v.candidates.length > 0)
        const editableServing = !['test','compare'].includes(plan.kind)
        const target = tasks.find(v => v.task.id === plan.target_task_id)
        const machine = assets.machines.find(m => m.id === plan.machine_id)
        return h('form', { className: 'infra-card infra-editor', onSubmit: e => { e.preventDefault(); onSave(plan) } },
          h('h2', null, initial ? '编辑任务方案' : '新建推理任务'),
          h('p', null, '业务对象是整台服务器。任务设备仅从已授权配额选择；保存方案后再开始执行。'),
          h('div', { className: 'infra-form-grid' }, text('任务名称', 'title'), select('业务类型', 'kind', resourceReview ? [['deploy_verify',kinds.deploy_verify]] : Object.entries(kinds))), text('业务目标', 'goal', true),
          h('p', { className: 'infra-meta' }, '独立验收使用固定质量协议与下方性能设置；目标描述不会自动新增验收项。未设置性能阈值时，仅验证测量完整性。'),
          plan.kind !== 'deploy_verify' && h('label', null, '目标部署任务', h('select', { required: true, value: plan.target_task_id ?? '', onChange: e => {
            const t = tasks.find(v => v.task.id === e.target.value)?.task
            if (t) setPlan(p => ({ ...p, target_task_id: t.id, machine_id: t.plan.machine_id, gpu_ids: t.plan.gpu_ids, model_asset: t.plan.model_asset,
              image_asset: t.plan.image_asset, engine_source_profile:t.plan.engine_source_profile??null, candidate_ids: [], serving: t.plan.serving }))
          } }, h('option', { value: '' }, '选择已登记部署'), ...targets.map(v => h('option', { key: v.task.id, value: v.task.id }, v.task.plan.title)))),
          plan.kind === 'compare' && h('fieldset', null, h('legend', null, '比较两个已有候选'), ...(target?.candidates ?? []).map(c => h('label', { className: 'infra-check', key: c.id },
            h('input', { type: 'checkbox', checked: plan.candidate_ids.includes(c.id), disabled: !plan.candidate_ids.includes(c.id)&&plan.candidate_ids.length>=2, onChange: e => set('candidate_ids', e.target.checked ? [...plan.candidate_ids,c.id] : plan.candidate_ids.filter(id => id !== c.id)) }), c.id + ' · ' + c.reason))),
          h('div', { className: 'infra-form-grid' }, select('目标机器', 'machine_id', assets.machines.map(m => [m.id,m.label ?? m.id])),
            select('模型资产', 'model_asset', assets.models.filter(a=>onNode(a)).map(m => [m.id,m.label ?? m.id])), select('Docker引擎镜像', 'image_asset', assets.images.filter(i=>onNode(i)&&(i.backend??'docker')==='docker'&&['sglang','vllm'].includes(i.engine??'sglang')).map(m => [m.id,m.label ?? m.id])),
            select('协作偏好', 'strategy', [['auto','按证据选择（默认）'],['single','单主控'],['multi','专家协作']]),
            h('p',{className:'infra-meta'},'默认auto由Pilot主控处理；按需请Infra或Repo专家，跨层问题可先后使用两位专家。')),
          !assets.images.find(i=>i.id===plan.image_asset)?.server_id && (assets.engine_source_profiles?.length>0 || plan.engine_source_profile) && h('label',null,'任务内引擎源码配方',h('select',{value:plan.engine_source_profile??'',disabled:plan.kind!=='deploy_verify',onChange:e=>set('engine_source_profile',e.target.value||null)},h('option',{value:''},'使用登记环境原版'),...(assets.engine_source_profiles??[]).filter(p=>p.image_id===plan.image_asset).map(p=>h('option',{key:p.id,value:p.id},p.label+' · '+p.version)))),
          h('fieldset', null, h('legend', null, '本任务执行配额'), ...(machine?.allowed_gpu_ids ?? []).map(id => h('label', { className: 'infra-check', key: id }, h('input', {
            type: 'checkbox', disabled:plan.kind!=='deploy_verify', checked: plan.gpu_ids.includes(id), onChange: e => set('gpu_ids', e.target.checked ? [...plan.gpu_ids,id] : plan.gpu_ids.filter(n => n !== id)) }), 'GPU ' + id))),
          h('details', null, h('summary', null, editableServing ? '引擎、负载与预算' : '负载、验收与预算'),
            !editableServing && h('p',null,'部署参数取自冻结候选；此任务只修改测试负载、验收要求和预算。'),
            h('div', { className: 'infra-form-grid' }, editableServing && number('上下文长度','context_length','serving',512,32768), editableServing && number(assets.images.find(i=>i.id===plan.image_asset)?.engine==='vllm'?'GPU显存利用率':'静态显存比例','mem_fraction_static','serving',.05,.6),
              editableServing && number('质量探针最大输出','max_new_tokens','serving',8,1024), number('每组请求数','requests_per_case','workload',2,32),
              number('性能输出长度','max_new_tokens','workload',8,512), number('请求超时（秒）','timeout_seconds','workload',5,300),
              h('label',null,'性能输入与缓存',h('select',{value:plan.workload.cache_mode??'cold',onChange:e=>section('workload','cache_mode',e.target.value)},h('option',{value:'cold'},'独立输入（默认）'),h('option',{value:'warm_prefix'},'重复前缀预热'))),
              number('输入集种子','seed','workload',0,4294967295),
              number('任务时限（秒）','deadline_seconds',null,120,14400),
              h('label', null, '累计 token 预算（空值表示无累计上限）', h('input', { type: 'number', min: 1000, value: plan.max_tokens ?? '', onChange: e => set('max_tokens', e.target.value === '' ? null : Number(e.target.value)) })),
              h('label', null, 'TTFT P95 目标（毫秒，可留空）', h('input', { type: 'number', min: 1, value: plan.ttft_slo_ms ?? '', onChange: e => set('ttft_slo_ms', e.target.value === '' ? null : Number(e.target.value)) })),
              editableServing && h('label', null, '注意力后端', h('select', { value: plan.serving.attention_backend, onChange: e => section('serving','attention_backend',e.target.value) }, ...backends(assets.images.find(i=>i.id===plan.image_asset)).map(([value,label])=>h('option',{key:value,value},label)))),
              ...[['输入 token 长度','input_lengths',16,16384],['并发数','concurrency',1,16]].map(([title,key,min,max]) => h('label',{key}, title + '（中英文逗号分隔）',h('input',{value:matrixText[key],required:true,onChange:e=>{
                const raw=e.target.value,parts=raw.split(/[,，]/).map(v=>v.trim()),values=parts.map(Number)
                const valid=parts.length>=1&&parts.length<=4&&parts.every(v=>/^\d+$/.test(v))&&values.every(v=>Number.isInteger(v)&&v>=min&&v<=max)&&new Set(values).size===values.length
                setMatrixText(previous=>({...previous,[key]:raw}))
                e.target.setCustomValidity(valid?'':title+'需为1–4个不重复整数，范围'+min+'–'+max+'。')
                if(valid)section('workload',key,values)
              }}))),
              editableServing && h('label', null, '停止词（每行一个）', h('textarea', { value: plan.serving.stop.join('\n'), onChange: e => section('serving','stop',e.target.value.split('\n').filter(Boolean)) }))),
            editableServing && h('label',{className:'infra-check'},h('input',{type:'checkbox',checked:plan.serving.disable_cuda_graph,onChange:e=>section('serving','disable_cuda_graph',e.target.checked)}),'禁用 CUDA graph'),
            h('label',{className:'infra-check'},h('input',{type:'checkbox',checked:plan.performance_required,onChange:e=>set('performance_required',e.target.checked)}),'性能矩阵作为必选验收')),
          h('div',{className:'infra-actions'},h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存方案'),btn('返回',onCancel,busy)))
      }
      function EnginePanel({view,busy,run}) {
        const {task:t,engine_source:source}=view,s=t.state
        const [path,setPath]=useState('sglang/srt/layers/sampler.py'),[start,setStart]=useState(1),[read,setRead]=useState(null),[query,setQuery]=useState(''),[matches,setMatches]=useState(null),[old,setOld]=useState(''),[replacement,setReplacement]=useState(''),[reason,setReason]=useState('')
        if(!source)return null
        const permitted=source.manual_mutation&&view.authority?.valid!==false&&!s.active_job
        const editable=permitted&&s.status==='running'&&s.execution_mode==='manual'&&t.plan.kind==='deploy_verify'
        const rows=source.candidates??[]
        const action=(method,args)=>run(method,args)
        const readFile=async(p=path)=>{setPath(p);const value=await action('engine-source-read',{path:p,start_line:Number(start),max_lines:100});if(value)setRead(value)}
        return h('section',{className:'infra-card'},h('h3',null,'引擎源码与制品'),h('p',null,source.profile+' · '+source.file_count+' 个文件 · '+(source.prepared?'已准备':'尚未准备')),
          h('p',{className:'infra-meta'},'当前配方修复 SGLang Python 引擎源码。冻结、构建、实际加载和独立验收分别记录。'+(s.execution_mode==='manual'?'当前由你手动操作；任务运行时可交回智能体执行。':'智能体与手动入口使用同一份任务和证据。')),
          source.error&&h('p',{className:'infra-error'},source.error),
          permitted&&['draft','paused','failed','running'].includes(s.status)&&s.execution_mode!=='manual'&&btn('进入手动操作',()=>action('engine-manual',{request_id:crypto.randomUUID(),expected_revision:t.revision}),busy),
          editable&&!source.prepared&&btn('准备任务源码',()=>action('engine-source-prepare',{}),busy,true),
          editable&&btn('交回智能体执行',()=>action('engine-agent',{}),busy),
          source.prepared&&h('details',null,h('summary',null,'读取、搜索与精确修改'),
            h('label',null,'源码路径',h('input',{value:path,onChange:e=>setPath(e.target.value)})),h('label',null,'起始行',h('input',{type:'number',min:1,value:start,onChange:e=>setStart(e.target.value)})),btn('读取源码',()=>readFile(),busy),
            h('label',null,'搜索内容',h('input',{value:query,onChange:e=>setQuery(e.target.value)})),btn('搜索',async()=>{const v=await action('engine-source-search',{text:query,limit:30});if(v)setMatches(v)},busy||query.length<2),
            matches&&h('div',null,...matches.matches.map((m,i)=>btn(m.path+':'+m.line+' '+m.text,()=>{setStart(m.line);setPath(m.path)},busy))),
            read&&h('div',null,h('p',{className:'infra-meta'},read.path+' · SHA256 '+read.sha256),h('pre',null,read.content)),
            editable&&read&&read.path===path&&h('div',null,h('label',null,'唯一原文片段',h('textarea',{value:old,rows:4,onChange:e=>setOld(e.target.value)})),h('label',null,'替换片段',h('textarea',{value:replacement,rows:4,onChange:e=>setReplacement(e.target.value)})),btn('保存精确补丁',async()=>{const v=await action('engine-source-patch',{path,expected_sha256:read.sha256,old_text:old,new_text:replacement});if(v){setMatches(null);await readFile()}},busy||!old))),
          editable&&source.prepared&&h('div',null,h('label',null,'候选说明',h('input',{value:reason,onChange:e=>setReason(e.target.value)})),btn('冻结源码候选',()=>action('candidate',{request_id:crypto.randomUUID(),parent_id:rows.at(-1)?.candidate_id??null,reason,serving:t.plan.serving}),busy||reason.length<5||s.repairs>=3,true)),
          ...rows.map(c=>h('div',{key:c.candidate_id,className:'infra-evidence'},h('strong',null,c.candidate_id+' · '+({not_built:'未构建',passed:'构建通过',failed:'构建失败',unverified:'制品待核对',running:'构建中',submitted:'等待构建',cancelled:'构建已取消'}[c.build]??c.build)),h('p',{className:'infra-meta'},'源码 '+c.source_digest+' · 修改：'+(c.changed_files.join('、')||'原始基线')),c.error&&h('p',{className:'infra-error'},c.error),
            c.artifact&&h('details',null,h('summary',null,'制品与审批身份'),h('pre',null,JSON.stringify({artifact:c.artifact,review_digest:c.review_digest,decision:c.decision},null,2))),
            editable&&['not_built','failed','cancelled'].includes(c.build)&&btn('构建并离线安装',()=>action('engine-build',{candidate_id:c.candidate_id}),busy),
            permitted&&c.build==='passed'&&!c.decision&&h('div',null,h('p',null,'审阅上述源码和制品后，批准此确切候选在本任务隔离运行。'),btn('批准此制品',()=>action('engine-decision',{candidate_id:c.candidate_id,expected_digest:c.review_digest,decision:'allowed-once'}),busy,true),btn('拒绝制品',()=>action('engine-decision',{candidate_id:c.candidate_id,expected_digest:c.review_digest,decision:'rejected'}),busy)),
            editable&&c.build==='passed'&&c.decision==='allowed-once'&&btn(c.active?'核对已加载制品':'加载此制品',()=>action('launch',{candidate_id:c.candidate_id}),busy),
            editable&&c.build==='passed'&&c.active&&btn('独立验收此制品',()=>action('test',{candidate_id:c.candidate_id}),busy),
            permitted&&c.build==='passed'&&c.decision==='allowed-once'&&!c.active&&['running','completed'].includes(s.status)&&s.experiments.some(r=>r.candidate_id===c.candidate_id&&r.passed)&&btn('回滚并重新验收',()=>action('engine-rollback',{candidate_id:c.candidate_id}),busy))),
          editable&&view.current_acceptance?.current&&s.experiments.at(-1)?.passed&&rows.some(c=>c.active&&c.build==='passed'&&c.decision==='allowed-once')&&btn('确认当前验收并交付',()=>action('engine-finish',{}),busy,true),
          s.experiments.at(-1)?.identity?.engine_loaded&&h('details',null,h('summary',null,'实际加载证据'),h('pre',null,JSON.stringify(s.experiments.at(-1).identity,null,2))))
      }
      function Task({ view, busy, run, edit, evidence, openChat,assets={} }) {
        const {task:t,candidates,actions,budget}=view,s=t.state,p=t.plan
        const [quota,setQuota]=useState(budget.max_tokens??'')
        useEffect(()=>setQuota(budget.max_tokens??''),[budget.max_tokens])
        const last=s.experiments.at(-1),current=view.current_acceptance?.current===true
        const servingImage=(assets.images??[]).find(i=>i.id===p.image_asset)
        const containerLoad=view.events.filter(e=>e.kind==='job_settled'&&e.data.status==='completed'&&['launch','rollback'].includes(e.data.action)).at(-1)?.data.result?.identity
        const settled=view.events.filter(e=>e.kind==='job_settled')
        const hasExecution=Boolean(candidates.length||settled.length||s.active_job||s.deadline_epoch)
        const stage=!hasExecution?0:s.status==='completed'?3:current||s.phase==='test'?2:1
        const line=(label,value)=>h('div',null,h('dt',null,label),h('dd',null,String(value??'—')))
        const assetLabel=(group,id)=>(assets[group]??[]).find(value=>value.id===id)?.label??id
        return h('div',{className:'infra-stack'},
          h('section',{className:'infra-card'},h('div',{className:'infra-heading'},h('span',{className:'infra-tag'},kinds[p.kind]),h('span',{role:'status'},statuses[s.status])),
            h('h2',null,p.title),h('p',null,p.goal),
            h('ol',{className:'infra-journey','aria-label':'任务交付流程'},...['确认方案','执行与诊断','独立验收','交付报告'].map((label,i)=>h('li',{key:label,'aria-current':i===stage?'step':undefined,className:i===stage?'infra-current':''},label))),
            h('p',{className:'infra-meta'},s.status==='draft'?'下一步：核对下面的方案；需要变更可编辑或回到对话。点击开始后才执行。':s.status==='cancelled'&&!hasExecution?'草稿已撤销，保留方案记录。新需求请在新会话或手动表单中创建。':s.status==='completed'?'当前任务已交付，可下载报告；用完停止自有服务。':s.active_job?'程序正在执行有界作业，结果结算后会回到任务对话。':'查看当前阶段和证据；暂停或失败时核对原因后继续。'),
            h('div',{className:'infra-actions'},...actions.filter(a=>labels[a]).map(a=>btn(a==='cancel'&&s.status==='draft'?'撤销草稿':labels[a],()=>run(a,{request_id:crypto.randomUUID(),expected_revision:t.revision}),busy,a==='start'||a==='resume')),
              actions.includes('edit')&&btn('编辑方案',edit,busy),btn('回到任务对话',openChat,busy),actions.includes('report')&&btn('下载验收报告',()=>run('report',{}),busy))),
          view.request_routing?.origin==='human'&&view.request_routing?.source!=='pending'&&h('details',{className:'infra-card'},
            h('summary',null,'当前需求判定 · '+(view.request_routing.label??'需要澄清')),
            h('p',null,view.request_routing.reason),
            view.request_routing.missing_fields?.length>0&&h('p',null,'待补充：'+view.request_routing.missing_fields.join('、')),
            h('p',{className:'infra-meta'},view.request_routing.requires_owner_control?'任务控制请使用上方Owner操作；聊天判断不会自动开始或恢复任务。':'识别结果只限定本次请求的处理范围；执行仍须通过任务状态和权限检查。')),
          view.authority?.valid===false&&h('p',{className:'infra-error',role:'alert'},'执行权限不可用：'+view.authority.reason),
          view.session_permission==='observe'&&h('p',{className:'infra-meta'},'当前会话仅观察。需要执行时在服务器与权限中切换到审阅执行。'),
          s.error&&h('p',{className:'infra-error',role:'alert'},s.error),
          h('section',{className:'infra-card'},h('h3',null,'方案与执行范围'),h('dl',{className:'infra-plan'},
            line('服务器',assetLabel('machines',p.machine_id)),line('执行配额',p.gpu_ids.map(g=>'GPU '+g).join('、')),line('模型',assetLabel('models',p.model_asset)),line('Docker引擎镜像',assetLabel('images',p.image_asset)),line('部署方式','Docker 容器'),
            line('引擎源码',p.engine_source_profile||'使用登记引擎'),line('上下文长度',p.serving.context_length),line('输入长度',p.workload.input_lengths.join('、')),line('并发',p.workload.concurrency.join('、')),
            line('每组请求数',p.workload.requests_per_case),line('输出上限',p.workload.max_new_tokens+' token'),line('独立验收',p.performance_required?'身份、质量探针与性能矩阵':'身份与质量探针'),
            line('性能输入',p.workload.cache_mode==='cold'?'固定种子的独立输入':p.workload.cache_mode==='warm_prefix'?'重复前缀预热':'历史重复输入协议'),line('输入集种子',p.workload.seed??'历史未登记'),
            line('TTFT目标',p.ttft_slo_ms?p.ttft_slo_ms+' ms':'仅测量，不设达标目标'),p.target_task_id&&line('已有部署任务',p.target_task_id)),
            h('details',null,h('summary',null,'完整方案参数与授权'),h('pre',null,JSON.stringify(p,null,2)),view.authority?.grant&&h('p',null,'授权期限：'+new Date(view.authority.grant.expires_at*1000).toLocaleString()))),
          hasExecution&&h('section',{className:'infra-card'},h('h3',null,'执行进度与协作'),h('p',null,'当前阶段：'+(phases[s.phase]??s.phase)+(s.active_job?' · 作业 '+s.active_job.slice(0,8):'')),
            h('p',null,(s.strategy.mode==='multi'?'专家协作':'Pilot主控执行')+' · '+s.strategy.reason),
            (s.strategy.experts??[]).length>0&&h('p',null,'准入专家：'+s.strategy.experts.map(role=>role==='infra-expert'?'Infra专家':'Repo专家').join('、')),
            h('details',null,h('summary',null,'协作决策记录'),h('p',{className:'infra-meta'},'准入是许可；实际专家参与以会话轨迹为准。'),h('pre',null,JSON.stringify(s.strategy_history,null,2)))),
          view.engine_source&&h('details',{className:'infra-card'},h('summary',null,'引擎源码与手动操作'),h(EnginePanel,{view,busy,run})),
          last&&h('section',{className:'infra-card'},h('h3',null,current?'当前独立验收':'历史验收记录'),
            !current&&h('p',{className:'infra-meta'},'此结果不代表当前候选或服务通过，需重新验收。'),
            h('div',{className:'infra-gates'},...Object.entries(last.gates??{}).map(([k,v])=>{
              const unconfigured=k==='performance'&&(last.performance_target?.configured===false||(last.performance_target==null&&p.ttft_slo_ms==null))
              return h('span',{key:k,className:!current||unconfigured?'infra-meta':v?'infra-pass':'infra-fail'},(({identity:'服务身份',quality:'质量探针',test_complete:'测试完整性',performance:'性能目标'})[k]??k)+(!current?' · 历史':unconfigured?' · 未设置（仅报告测量）':v?' · 通过':' · 未通过'))
            })),
            h('p',{className:'infra-meta'},'候选 '+last.candidate_id+'。质量探针仅验证协议，不等同完整准确率。'),
            h('div',{className:'infra-table'},h('table',null,h('thead',null,h('tr',null,...['输入/并发','成功/总请求','失败率','TTFT P95(ms)','E2E P95(ms)','TPOT估算 P95(ms)','请求/s','输出 token/s'].map(label=>h('th',{key:label},label)))),h('tbody',null,...(last.cases??[]).map((c,i)=>h('tr',{key:i},h('td',null,c.input_length+' / '+c.concurrency),h('td',null,c.successful+' / '+c.attempted),h('td',null,c.failure_rate==null?'—':(c.failure_rate*100).toFixed(1)+'%'),...['ttft_p95_ms','latency_p95_ms','tpot_estimate_p95_ms','requests_per_second','output_tokens_per_second'].map(key=>h('td',{key},c[key]?.toFixed(1)??'—'))))))),
            h('p',{className:'infra-meta'},'延迟为客户端流式观测；TPOT按内容块时间估算，无法替代逐token ITL。缺失指标显示—；P95/P99需结合实际样本数，短样本不证明稳定性。未设性能目标时不宣称达标。')),
          p.kind==='compare'&&s.experiments.length>0&&h('section',{className:'infra-card'},h('h3',null,'候选顺序比较'),...p.candidate_ids.map(id=>{const result=s.experiments.filter(r=>r.candidate_id===id).at(-1);return h('div',{key:id},h('strong',null,id),result?h('pre',null,JSON.stringify({gates:result.gates,cases:result.cases},null,2)):h('p',null,'此候选尚未测试。'))})),
          hasExecution&&h('section',{className:'infra-card'},h('h3',null,'模型容器'),h('p',null,'新部署统一Docker；模型只读、任务私有网络与授权GPU。'),h('dl',{className:'infra-plan-grid'},line('镜像SHA',servingImage?.digest),line('引擎',servingImage?.engine??'sglang'),line('最近容器ID',containerLoad?.container_id),line('验收状态',current&&last?.passed?'当前容器验收通过':current?'当前测试未通过':'当前无有效验收'))),
          candidates.length>0&&h('details',{className:'infra-card'},h('summary',null,'部署候选（'+candidates.length+'）'),...candidates.map(c=>h('details',{key:c.id},h('summary',null,c.id+(view.service_candidate===c.id?' · 当前服务':'')),h('pre',null,JSON.stringify(c,null,2))))),
          (settled.length>0||s.hypotheses.length>0)&&h('section',{className:'infra-card'},h('h3',null,'故障分析与原始证据'),...s.hypotheses.map((v,i)=>h('p',{key:i},'待证据复核的假设：'+v.cause)),...settled.map(e=>h('div',{className:'infra-evidence',key:e.seq},h('span',null,'#'+e.seq+' · '+e.data.action+' · '+e.data.status),btn('查看原始结果',()=>evidence(e.data.evidence),busy),btn('查看引擎日志',()=>evidence(e.data.evidence.replace('result.json','engine.log')),busy),e.data.result.error&&h('p',{className:'infra-error'},e.data.result.error.slice(0,600))))),
          h('details',{className:'infra-card'},h('summary',null,'用量与预算'),h('p',null,'累计预算占用 '+budget.used_tokens.toLocaleString()+' token · '+budget.requests+' 次模型请求 · API费用未计价'),budget.estimated&&h('p',{className:'infra-meta'},'包含未返回usage请求的保守预留；此部分是预算占用估算，不是已确认的API用量。'),
            budget.last_stop&&h('p',{className:'infra-meta'},'上次预算停止：'+budget.last_stop.reason),h('label',null,'累计预算（空值表示无累计上限）',h('input',{type:'number',min:1000,value:quota,onChange:e=>setQuota(e.target.value)})),btn('保存预算',()=>run('budget',{request_id:crypto.randomUUID(),expected_revision:budget.revision,max_tokens:quota===''?null:Number(quota)}),busy)))
      }
      function ResourceCredential({busy,act}){
        const input=(label,value,change,props={})=>h('label',null,label,h('input',{value,disabled:busy,onChange:e=>change(e.target.value),...props}))
          const [kind,setKind]=useState('password'),[label,setLabel]=useState(''),[value,setValue]=useState('')
          return h('details',{className:'infra-card'},h('summary',null,'添加连接凭据'),h('form',{onSubmit:async e=>{e.preventDefault();const v=await act('secret',{label,kind,value});if(v){setValue('');setLabel('')}}},
            input('凭据名称',label,setLabel,{required:true}),h('label',null,'类型',h('select',{value:kind,disabled:busy,onChange:e=>setKind(e.target.value)},h('option',{value:'password'},'密码'),h('option',{value:'private_key'},'私钥'))),
            kind==='password'?input('密码',value,setValue,{type:'password',required:true,autoComplete:'new-password'}):h('label',null,'私钥内容',h('textarea',{value,disabled:busy,required:true,rows:5,onChange:e=>setValue(e.target.value)})),
            h('p',{className:'infra-meta'},'凭据由 Pilot Host 加密保存，登记与观测结果不返回明文。加密私钥可使用 Host 的 SSH Agent。'),h('button',{disabled:busy,type:'submit'},'加密保存凭据')))
        }
      function Resources({sessionId,onTask,onInvalidSession}) {
        const [data,setData]=useState(null),[chosen,setChosen]=useState(''),[editing,setEditing]=useState(null),[analysis,setAnalysis]=useState(null),[model,setModel]=useState(''),[context,setContext]=useState(4096),[concurrency,setConcurrency]=useState(2),[busy,setBusy]=useState(false),[error,setError]=useState(''),[pollError,setPollError]=useState(''),[policy,setPolicy]=useState(null),[gpuText,setGpuText]=useState(''),[portText,setPortText]=useState(''),[networkText,setNetworkText]=useState(''),[planning,setPlanning]=useState(false),[limits,setLimits]=useState({cpu_cores:4,memory_gib:16,disk_gib:5,duration_seconds:2400}),[reason,setReason]=useState('根据整机观测与容量分析开展隔离推理验证')
        const generation=useRef(0)
        async function refresh(){const ticket=++generation.current;try{const v=await rpc('resource-catalog',{session_id:sessionId??''});if(ticket!==generation.current)return;setData(v);setPollError('');setChosen(old=>v.servers.some(s=>s.id===old)?old:v.servers[0]?.id??'');setModel(old=>old||v.assets.models[0]?.id||'');if(v.session_selection?.status==='missing')onInvalidSession?.(v.session_selection.session_id)}catch(e){if(ticket===generation.current)setPollError(e.message)}}
        useEffect(()=>{refresh();const timer=setInterval(refresh,8000);return()=>{clearInterval(timer);generation.current++}},[sessionId])
        const server=data?.servers.find(s=>s.id===chosen)
        useEffect(()=>{if(!data)return;const available=data.assets.models.filter(m=>onMachine(m,server));if(!available.some(m=>m.id===model)){setModel(available[0]?.id??'');setAnalysis(null);setPlanning(false)}},[chosen,data?.assets?.models])
        useEffect(()=>{setPolicy(server?.policy??null);setPortText((server?.policy.network_ports??[]).join(','));setNetworkText((server?.policy.distributed_networks??[]).join(','));setGpuText(server?.policy.approvable_gpu_ids.join(',')??'');setAnalysis(null)},[chosen,server?.revision])
        async function act(method,args={}){setBusy(true);setError('');try{const v=await rpc('resource-'+method,args);if(method==='assess'){setAnalysis(v);if(v.suggested_limits)setLimits(v.suggested_limits)}if(method==='observe')setAnalysis(null);if(method==='propose'){setPlanning(false);onTask(v.root_session,false)}if(method==='execute')onTask(v.sessionId,true);await refresh();return v}catch(e){setError(e.message);return null}finally{setBusy(false)}}
        const input=(label,value,change,props={})=>h('label',null,label,h('input',{value,disabled:busy,onChange:e=>change(e.target.value),...props}))
        const endpoint=()=>({host:'',port:22,username:'',auth:'password',credential_ref:null,fingerprint:null})
        const form=editing
        const updateConnection=value=>setEditing(s=>({...s,connection:{...s.connection,...value}}))
        function EndpointForm({value,index}){
          const update=patch=>{if(index==='target')updateConnection({target:{...value,...patch}});else updateConnection({jumps:form.connection.jumps.map((v,i)=>i===index?{...v,...patch}:v)})}
          return h('fieldset',{key:index},h('legend',null,index==='target'?'目标服务器':'跳板 '+(index+1)),
            h('div',{className:'infra-form-grid'},input('主机 / IP',value.host,v=>update({host:v}),{required:true}),input('SSH端口',value.port,v=>update({port:Number(v)}),{type:'number',min:1,max:65535,required:true}),input('登录用户',value.username,v=>update({username:v}),{required:true}),
              h('label',null,'认证方式',h('select',{value:value.auth,disabled:busy,onChange:e=>update({auth:e.target.value,credential_ref:null})},h('option',{value:'password'},'密码'),h('option',{value:'private_key'},'私钥'),h('option',{value:'agent'},'Pilot Host 的 SSH Agent')))),
            value.auth!=='agent'&&h('label',null,'已保存凭据',h('select',{value:value.credential_ref??'',disabled:busy,onChange:e=>update({credential_ref:e.target.value||null})},h('option',{value:''},'请选择凭据'),...(data.credentials??[]).filter(c=>c.kind===value.auth).map(c=>h('option',{key:c.credential_ref,value:c.credential_ref},c.label)))),
            h('p',{className:'infra-meta'},'指纹：'+(value.fingerprint??'未核对；首次连接不会提交此跳凭据。')),
            index!=='target'&&btn('移除此跳板',()=>updateConnection({jumps:form.connection.jumps.filter((_,i)=>i!==index)}),busy))
        }
        if(!data)return pollError||error?h('section',{className:'infra-card'},h('p',{className:'infra-error',role:'alert'},'资源读取失败：'+(error||pollError)),btn('重试读取资源',refresh,busy)):h('p',null,'正在读取服务器与权限…')
        return h('div',{className:'infra-stack'},pollError&&h('p',{className:'infra-meta',role:'status'},'资源刷新暂时中断：'+pollError),error&&h('p',{className:'infra-error',role:'alert'},error),
          h('section',{className:'infra-card'},h('h2',null,'服务器资源与权限'),h('p',null,'整机观测 → 容量与栈分析 → 有界资源方案 → 一次性审批 → 启动与独立验收'),
            h('p',{className:'infra-meta'},'观测所有设备与进程，任务按单独授权执行。连接、观测和推理执行是分别核验的能力。'),
            h('div',{className:'infra-actions'},...data.servers.map(s=>btn(s.label+(chosen===s.id?' · 当前':''),()=>{setChosen(s.id);setEditing(null);setPlanning(false)},busy)),btn('添加服务器',()=>setEditing({request_id:crypto.randomUUID(),server_id:'',expected_revision:0,label:'',enabled:false,connection:{transport:'ssh',origin:'pilot_host',target:endpoint(),jumps:[],prerequisite:''}}),busy)),
            data.session_permission&&h('label',null,'当前会话权限',h('select',{value:data.session_permission.mode,disabled:busy,onChange:e=>act('session-permission',{session_id:sessionId,mode:e.target.value})},h('option',{value:'observe'},'仅观察与分析'),h('option',{value:'review'},'审阅后执行')))),
          editing&&h('section',{className:'infra-card'},h('h3',null,form.expected_revision?'编辑服务器连接':'添加服务器'),h('form',{onSubmit:async e=>{e.preventDefault();const v=await act('save',form);if(v){setChosen(v.id);setEditing(null)}}},
            h('div',{className:'infra-form-grid'},input('资源ID',form.server_id,v=>setEditing(s=>({...s,server_id:v})),{required:true,pattern:'[A-Za-z0-9_-]+',readOnly:form.expected_revision>0}),input('显示名称',form.label,v=>setEditing(s=>({...s,label:v})),{required:true,minLength:2})),
            h('label',null,'访问方式',h('select',{value:form.connection.transport,disabled:busy||form.connection.transport==='local',onChange:e=>updateConnection({transport:e.target.value,target:['manual','local'].includes(e.target.value)?null:form.connection.target??endpoint(),jumps:[]})},h('option',{value:'local',disabled:form.connection.transport!='local'&&data.servers.some(s=>s.connection.transport==='local')},'Pilot Host 本机'),h('option',{value:'ssh'},'直接 SSH'),h('option',{value:'ssh_chain'},'多跳 SSH / 堡垒跳板'),h('option',{value:'existing_tunnel'},'现有转发隧道'),h('option',{value:'manual'},'VPN / 网页堡垒等人工前置接入'))),
            form.connection.transport==='ssh_chain'&&h('div',null,...form.connection.jumps.map((v,i)=>EndpointForm({value:v,index:i})),btn('添加跳板',()=>updateConnection({jumps:[...form.connection.jumps,endpoint()]}),busy||form.connection.jumps.length>=4)),
            form.connection.target&&EndpointForm({value:form.connection.target,index:'target'}),
            h('label',null,'Docker 访问方式',h('select',{value:form.docker?.mode??'direct',disabled:busy,onChange:e=>setEditing(v=>({...v,docker:{mode:e.target.value,credential_ref:null}}))},h('option',{value:'direct'},'当前 SSH 用户直接访问'),h('option',{value:'sudo_nopasswd'},'sudo 免密访问'),h('option',{value:'sudo_password'},'sudo 密码访问'))),
            form.docker?.mode==='sudo_password'&&h('label',null,'Docker sudo 密码凭据',h('select',{required:true,value:form.docker.credential_ref??'',disabled:busy,onChange:e=>setEditing(v=>({...v,docker:{...v.docker,credential_ref:e.target.value||null}}))},h('option',{value:''},'选择已加密保存的密码'),...(data.credentials??[]).filter(c=>c.kind==='password').map(c=>h('option',{key:c.credential_ref,value:c.credential_ref},c.label)))),
            h('label',null,'接入前置条件',h('textarea',{rows:2,value:form.connection.prerequisite,disabled:busy,onChange:e=>updateConnection({prerequisite:e.target.value})})),
            h('label',{className:'infra-check'},h('input',{type:'checkbox',checked:form.enabled,disabled:busy,onChange:e=>setEditing(s=>({...s,enabled:e.target.checked}))}),'启用资源'),
            h('div',{className:'infra-actions'},h('button',{type:'submit',disabled:busy,className:'infra-primary'},'保存服务器'),btn('取消编辑',()=>setEditing(null),busy))),h(ResourceCredential,{busy,act})),
          server&&h('section',{className:'infra-card'},h('div',{className:'infra-heading'},h('h2',null,server.label),btn('编辑连接',()=>setEditing({request_id:crypto.randomUUID(),server_id:server.id,expected_revision:server.revision,label:server.label,enabled:server.enabled,connection:structuredClone(server.connection),docker:structuredClone(server.docker??{mode:'direct',credential_ref:null})}),busy)),
            h('p',null,'接入：'+server.connection.transport+' · '+(server.enabled?'已启用':'已停用')+' · 节点执行器：'+(server.executor_available?'已登记；任务仍须核验与审批':'未准备，可先连接与检测环境')),
            server.node_readiness&&h('section',{role:'status'},h('p',null,'节点已核验资产 '+server.node_readiness.ready_assets+' 项'),...server.node_readiness.reasons.map(reason=>h('p',{className:'infra-meta',key:reason},reason))),
            h('div',{className:'infra-actions'},btn('测试连接',()=>act('test',{server_id:chosen}),busy),btn('刷新整机观测',()=>act('observe',{server_id:chosen}),busy||!server.enabled)),
            server.last_connection_test&&h('section',null,h('p',null,'连接结果：'+(server.last_connection_test.ready?'通过':server.last_connection_test.message??server.last_connection_test.stage)),
              server.last_connection_test.stage==='host_identity'&&h('div',null,h('p',null,'第 '+(server.last_connection_test.hop+1)+' 跳指纹：'+server.last_connection_test.fingerprint),h('p',{className:'infra-meta'},'先向资源管理员核对指纹；确认后再测试，才会提交凭据。'),btn('确认已核对的主机指纹',()=>act('trust',{server_id:chosen,expected_revision:server.revision,hop:server.last_connection_test.hop,fingerprint:server.last_connection_test.fingerprint}),busy))),
            server.observation&&h('section',null,h('h3',null,'整机当前状态 · '+new Date(server.observation.received_at*1000).toLocaleString()),h('p',null,(server.observation.identity.hostname??'未知主机')+' · '+server.observation.accelerator_platform+' · '+server.observation.cpu.logical_cores+' 个逻辑CPU · 可用内存 '+(server.observation.memory.available_bytes==null?'未知':(server.observation.memory.available_bytes/1024**3).toFixed(1))+' GiB'),
              server.observation.gpus?.length>0&&h('p',{className:'infra-meta',role:'status'},server.observation.gpu_processes_known===true?'GPU进程占用查询完整':'GPU进程占用未知，请刷新整机观测并检查设备查询；未知设备不推荐或执行。'),
              h('div',{className:'infra-table'},h('table',null,h('thead',null,h('tr',null,...['GPU','型号','空闲 / 总显存 MiB','占用进程','可审批'].map(x=>h('th',{key:x},x)))),h('tbody',null,...server.observation.gpus.map(g=>h('tr',{key:g.index},h('td',null,g.index),h('td',null,g.name),h('td',null,(g.free_mib??'未知')+' / '+(g.total_mib??'未知')),h('td',null,server.observation.gpu_processes.filter(p=>p.gpu_uuid===g.uuid).map(p=>p.owner+' / PID '+p.pid).join('；')||(server.observation.gpu_processes_known===true?'无':'未知')),h('td',null,server.policy.approvable_gpu_ids.includes(g.index)?'可提交审批':'仅观察')))))),
              h('details',null,h('summary',null,'CPU、全部磁盘与进程摘要'),h('pre',null,JSON.stringify({cpu:server.observation.cpu,memory:server.observation.memory,disks:server.observation.disks,processes:server.observation.processes,process_count:server.observation.process_count,limitations:server.observation.limitations},null,2))))),
          server&&h(ExecutorControl,{key:server.id,server}),
          server&&h(DockerControl,{key:'docker-'+server.id,server,onOpenTask:async c=>{if(c.kind==='software'){openBuild({taskId:c.task_id});return}try{const v=await rpc('open',{task_id:c.task_id});onTask(String(v.sessionId),true)}catch(e){setError(e.message)}}}),
          server&&policy&&h('details',{className:'infra-card'},h('summary',null,'所有者权限上限 · 当前 '+(policy.mode==='observe'?'仅观察':'审阅执行')),h('p',{className:'infra-meta'},'上限允许 Agent 提交申请，不直接授权任务。每次变更会使旧方案和授权失效。'),
            h('label',null,'服务器权限',h('select',{value:policy.mode,disabled:busy,onChange:e=>setPolicy(s=>({...s,mode:e.target.value}))},h('option',{value:'observe'},'仅观察'),h('option',{value:'review'},'审阅执行'))),
            input('可审批 GPU 编号，以逗号分隔',gpuText,setGpuText),
            h('fieldset',null,h('legend',null,'允许业务操作'),...Object.entries({assets:'模型/镜像导入',downloads:'资源下载',containers:'本任务容器',software:'软件与算子工作区',network:'多节点网络检查',inference:'推理部署和测试',profiling:'性能剖析'}).map(([key,label])=>{const allowed=policy.operations??['assets','downloads','containers','inference',...(policy.allow_source_edit?['software']:[])];return h('label',{key,className:'infra-check'},h('input',{type:'checkbox',disabled:busy,checked:allowed.includes(key),onChange:e=>setPolicy(v=>({...v,operations:e.target.checked?[...allowed,key]:allowed.filter(x=>x!==key)}))}),label)})),
            input('允许网络检查端口（逗号分隔）',portText,setPortText),
            input('额外允许的跨机 IPv4 网段（CIDR，逗号分隔）',networkText,setNetworkText),
            h('p',{className:'infra-meta'},'RFC 1918 地址兼容既有策略；其他网段由所有者明确登记，地址仍须在节点核验接口中。网段登记不证明网络隔离。'),
            h('label',null,'GPU 占用策略',h('select',{value:policy.gpu_sharing??'exclusive',disabled:busy,onChange:e=>setPolicy(v=>({...v,gpu_sharing:e.target.value}))},h('option',{value:'exclusive'},'仅使用无业务占用设备'),h('option',{value:'shared'},'允许共享已有占用设备'))),
            h('div',{className:'infra-form-grid'},...Object.entries({max_cpu_cores:'CPU核心上限',max_memory_gib:'内存上限 GiB',max_disk_gib:'工作区磁盘上限 GiB',max_duration_seconds:'授权时限上限 秒'}).map(([k,label])=>input(label,policy[k],v=>setPolicy(s=>({...s,[k]:Number(v)})),{type:'number',min:1}))),
            h('label',{className:'infra-check'},h('input',{type:'checkbox',checked:policy.allow_source_edit,disabled:busy,onChange:e=>setPolicy(s=>({...s,allow_source_edit:e.target.checked}))}),'允许任务内请求适配及登记引擎源码修改'),btn('保存权限上限',()=>act('policy',{server_id:chosen,expected_revision:server.revision,policy:{...policy,approvable_gpu_ids:numberList(gpuText),network_ports:numberList(portText),distributed_networks:networkText.split(/[,，]/).map(s=>s.trim()).filter(Boolean)}}),busy)),
          server&&h('section',{className:'infra-card'},h('h3',null,'模型容量与引擎选择'),h('div',{className:'infra-form-grid'},h('label',null,'模型',h('select',{value:model,disabled:busy,onChange:e=>{setModel(e.target.value);setAnalysis(null);setPlanning(false)}},...data.assets.models.filter(m=>onMachine(m,server)).map(m=>h('option',{key:m.id,value:m.id},m.label??m.id)))),input('上下文长度',context,v=>{setContext(Number(v));setAnalysis(null);setPlanning(false)},{type:'number',min:512,max:32768}),input('并发',concurrency,v=>{setConcurrency(Number(v));setAnalysis(null);setPlanning(false)},{type:'number',min:1,max:32})),
            btn('分析整机与适配方案',()=>act('assess',{server_id:chosen,model_asset:model,context_length:context,concurrency}),busy||!server.observation||!model),
            analysis&&h('section',null,h('p',null,analysis.requirements.reason),h('p',null,'规划最小显存：'+(analysis.requirements.minimum_gpu_gib?.toFixed(2)??'未知')+' GiB · 建议可审批设备：'+(analysis.suggested_gpu_ids.join(',')||'尚无满足条件的设备')),
              h('p',null,'建议任务配额：'+(analysis.suggested_limits?analysis.suggested_limits.cpu_cores+' CPU · '+analysis.suggested_limits.memory_gib.toFixed(1)+' GiB内存 · '+analysis.suggested_limits.disk_gib.toFixed(1)+' GiB磁盘 · '+analysis.suggested_limits.duration_seconds+'秒':'资源未知或不足；暂不生成建议')),
              ...(analysis.health_findings??[]).map((f,i)=>h('p',{key:i,className:'infra-meta'},f.scope+'：'+f.reason)),
              ...analysis.engines.map(e=>h('p',{key:e.id},e.label+' · '+e.reason)),h('p',{className:'infra-meta'},analysis.limitations.join('；'))),
            btn('制定资源审批方案',()=>setPlanning(true),busy||!analysis?.recommendation_ready||!server.executor_available||server.policy.mode!=='review',true)),
          planning&&server&&h('section',{className:'infra-card'},h('h3',null,'申请任务配额'),h('div',{className:'infra-form-grid'},...Object.entries({cpu_cores:'CPU核心',memory_gib:'内存 GiB',disk_gib:'工作区磁盘 GiB',duration_seconds:'授权时限 秒'}).map(([k,label])=>input(label,limits[k],v=>setLimits(s=>({...s,[k]:Number(v)})),{type:'number',min:1}))),input('申请理由',reason,setReason,{minLength:5,required:true}),
            h(Editor,{resourceReview:true,assets:{...data.assets,machines:data.assets.machines.filter(m=>m.id===chosen).map(m=>({...m,allowed_gpu_ids:server.policy.approvable_gpu_ids}))},tasks:[],initial:{...fresh(data.assets),machine_id:chosen,gpu_ids:analysis?.suggested_gpu_ids.length?analysis.suggested_gpu_ids:server.policy.approvable_gpu_ids.slice(0,1),model_asset:model,image_asset:analysis?.engines.find(e=>e.compatible)?.id??fresh(data.assets).image_asset,serving:{...fresh(data.assets).serving,...analysis?.suggested_serving,context_length:context},workload:{...fresh(data.assets).workload,concurrency:[Math.min(concurrency,16)]},deadline_seconds:limits.duration_seconds},onSave:p=>act('propose',{request_id:crypto.randomUUID(),plan:p,limits,reason}),onCancel:()=>setPlanning(false),busy})),
          h('section',{className:'infra-card'},h('h3',null,'资源方案与审批'),data.proposals.length?data.proposals.map(p=>h('section',{key:p.id,className:'infra-card'},h('strong',null,p.plan.title),h('p',null,p.reason),h('p',null,p.server_id+' · GPU '+p.plan.gpu_ids.join(',')+' · '+({pending:'待审阅',approved:'已一次性批准',denied:'已拒绝',activated:'已生成任务'}[p.status]??p.status)),h('p',{className:'infra-meta'},'摘要 '+p.digest+' · 审阅有效至 '+new Date(p.review_expires_at*1000).toLocaleString()),
            h('details',null,h('summary',null,'冻结方案、资产与配额'),h('pre',null,JSON.stringify({plan:p.plan,limits:p.limits,assets:p.assets,observation_id:p.observation_id,policy_revision:p.policy_revision},null,2))),
            ['pending','registering'].includes(p.status)&&h('div',{className:'infra-actions'},btn('批准此方案一次',()=>act('decision',{proposal_id:p.id,expected_digest:p.digest,decision:'allowed-once'}),busy||Date.now()/1000>p.review_expires_at,true),btn('拒绝',()=>act('decision',{proposal_id:p.id,expected_digest:p.digest,decision:'rejected'}),busy)),
            p.status==='approved'&&btn('生成待启动任务',()=>act('execute',{proposal_id:p.id,expected_digest:p.digest}),busy,true),
            p.task_id&&btn('查看任务',async()=>{const v=await rpc('open',{task_id:p.task_id});onTask(v.sessionId,true)},busy),p.grant_id&&btn('撤销执行授权',()=>act('revoke',{grant_id:p.grant_id}),busy))):h('p',null,'尚无资源审批方案。')),
          h('details',{className:'infra-card'},h('summary',null,'资源与权限审计'),...data.audit.map(a=>h('p',{key:a.seq},'#'+a.seq+' · '+a.created+' · '+a.kind+' · '+JSON.stringify(a.data)))))
      }
      function Body({ sessionId, compact=false }) {
        const destination=useSyncExternalStore(subscribeNavigation,readNavigation,readNavigation)
        const newSessionError = useSyncExternalStore(subscribeCreation, readCreationError, readCreationError)
        const [catalog,setCatalog]=useState(null),[view,setView]=useState(null),[selected,setSelected]=useState(sessionId??(!compact?localStorage.getItem('pilot-infra-selected'):null)),[editing,setEditing]=useState(false),[page,setPage]=useState('tasks'),[busy,setBusy]=useState(false),[error,setError]=useState(''),[loadError,setLoadError]=useState(''),[raw,setRaw]=useState(null)
        const epoch=useRef(0)
        const scroll=useRef(null)
        useEffect(()=>{scroll.current?.scrollTo({top:0})},[editing,selected,page])
        useEffect(()=>{if(!compact&&destination)setPage(destination.page)},[destination,compact])
        useEffect(()=>{if(compact)setSelected(sessionId??null)},[sessionId,compact])
        useEffect(()=>{if(!compact){if(selected)localStorage.setItem('pilot-infra-selected',selected);else localStorage.removeItem('pilot-infra-selected')}setRaw(null)},[selected,compact])
        async function reload(selection=selected) {
          const ticket=++epoch.current
          try { const c=await rpc('catalog',{}); if(ticket!==epoch.current)return; setCatalog(c);setLoadError('')
            const id=selection??(!compact?(c.tasks??[]).find(v=>v.task.state.session_id)?.task.state.session_id:null)
            if(id){const v=await rpc('view',{session_id:id});if(ticket!==epoch.current)return;setView(v.task?v:null);if(!v.task&&!compact&&page==='tasks'){const next=(c.tasks??[]).find(row=>row.task.state.session_id);if(next&&next.task.state.session_id!==id)setSelected(next.task.state.session_id)}else if(id!==selection)setSelected(id)}
            else setView(null)
          } catch(e){if(ticket===epoch.current)setLoadError(e.message)}
        }
        useEffect(()=>{setView(null);reload();const timer=setInterval(reload,5000);return()=>{clearInterval(timer);epoch.current++}},[selected])
        async function run(method,args={}) {
          setBusy(true);setError('')
          try { const result=await rpc(method,{...args,session_id:selected})
            if(method==='report'){const blob=new Blob([JSON.stringify(result,null,2)],{type:'application/json'}),url=URL.createObjectURL(blob),a=document.createElement('a');a.href=url;a.download='pilot-infra-'+view.task.id+'.json';a.click();URL.revokeObjectURL(url)}
            else if(method==='evidence-download'){const bytes=Uint8Array.from(atob(result.data),c=>c.charCodeAt(0)),url=URL.createObjectURL(new Blob([bytes],{type:'application/gzip'})),a=document.createElement('a');a.href=url;a.download=result.name;a.click();URL.revokeObjectURL(url)}
            else if(result.sessionId)setSelected(String(result.sessionId))
            await reload()
            return result
          } catch(e){setError(e.message)} finally{setBusy(false)}
        }
        async function save(plan) {
          setBusy(true);setError('')
          try { let selection=selected
            if(editing==='new'){const v=await rpc('create',{request_id:crypto.randomUUID(),plan});selection=String(v.sessionId);setSelected(selection);setView(v)}
            else {const v=await rpc('revise',{session_id:selected,request_id:crypto.randomUUID(),expected_revision:view.task.revision,plan});setView(v)}
            setEditing(false);await reload(selection)
          } catch(e){setError(e.message)} finally{setBusy(false)}
        }
        async function evidence(path){setBusy(true);try{setRaw(await rpc('evidence',{session_id:selected,path}));setError('')}catch(e){setError(e.message)}finally{setBusy(false)}}
        return h('div',{className:'infra-pane'}, !compact&&h('header',null,h('div',null,h('h1',null,'任务与验收'),h('p',null,'审阅方案、控制执行、核对独立验收。需求与诊断在任务对话中进行。')),
          h('nav',null,btn('任务',()=>setPage('tasks'),busy),btn('服务器与权限',()=>setPage('assets'),busy),btn('模型与镜像',()=>setPage('models'),busy),btn('软件与算子',()=>setPage('build'),busy),btn('多节点链路',()=>setPage('network'),busy),btn('对话描述需求',createFreshSession,busy,true),
            catalog?.assets.ready&&btn('手动填写方案',()=>{setEditing('new');setPage('tasks')},busy))),
          h('div',{className:'infra-scroll',ref:scroll},loadError&&h('p',{className:'infra-meta',role:'status'},'任务刷新暂时中断：'+loadError),error&&h('p',{className:'infra-error',role:'alert'},error),
            newSessionError&&h('p',{className:'infra-error',role:'alert'},newSessionError),
            !compact&&page==='tasks'&&h('p',{className:'infra-meta'},'对话负责理解需求与诊断；这里负责审阅和执行控制。保存方案后，须点击开始才会执行。'),
            page==='build'?h(BuildWorkbench,{taskId:destination?.taskId}):page==='network'?h(NetworkWorkbench):page==='models'?h(AssetWorkbench):page==='assets'?h(Resources,{sessionId:selected,onInvalidSession:id=>setSelected(current=>current===id?null:current),onTask:(id,show)=>{setSelected(String(id));if(show)setPage('tasks')}}):!catalog?h('p',null,'正在读取任务与资产…'):!catalog.assets.ready?h('section',{className:'infra-card'},h('h2',null,'推理执行资源尚未登记'),h('p',null,'请在资源与权限页面登记服务器并核验连接与执行资源。当前没有可启动的推理任务。')):
            editing?h(Editor,{assets:catalog.assets,tasks:catalog.tasks,initial:editing==='new'?null:view.task.plan,onSave:save,onCancel:()=>setEditing(false),busy}):
            h('div',{className:compact?'':'infra-workspace'},!compact&&h('aside',null,h('h3',null,'任务记录'),catalog.tasks.length?catalog.tasks.map(v=>btn(v.task.plan.title+' · '+statuses[v.task.state.status],async()=>{try{const r=await rpc('open',{task_id:v.task.id});setView(null);setSelected(String(r.sessionId));await reload(String(r.sessionId))}catch(e){setError(e.message)}},busy,false,{className:'infra-task-record','aria-pressed':v.task.state.session_id===selected})):h('p',null,'还没有推理任务。')),
              h('main',null,view?h(Task,{view,busy,run,assets:catalog.assets,edit:()=>setEditing('edit'),evidence,openChat:()=>ctx.uiWorkspace.openSession(selected)}):h('section',{className:'infra-card'},h('h2',null,'还没有任务方案'),h('p',null,'先在对话中说明部署或修复目标；也可手动填写方案。查看服务器状态不需要创建任务。'),btn('在对话中描述需求',createFreshSession,busy,true)))),
            raw&&h('section',{className:'infra-card'},h('div',{className:'infra-heading'},h('h3',null,raw.path),btn('下载完整原始文件',()=>run('evidence-download',{path:raw.path}),busy),btn('关闭证据',()=>setRaw(null))),h('p',{className:'infra-meta'},'SHA256 '+raw.sha256+' · '+raw.bytes+' 字节 · 页面展示末 '+raw.display_limit_chars+' 字符，下载包含完整文件。'),h('pre',null,raw.text))))
      }
      ctx.effect(()=>{const style=document.createElement('style');style.textContent=`
        .infra-pane{height:100%;display:flex;flex-direction:column;font:14px/1.6 var(--dsw-font-family,'Segoe UI',sans-serif);color:var(--dsw-alias-label-primary,#222);background:var(--dsw-alias-bg-base,#fff)}
        .infra-pane *{box-sizing:border-box;min-width:0}.infra-pane header{padding:26px 30px 18px;border-bottom:1px solid var(--dsw-alias-border-l2,#ddd)}
        .infra-pane h1{font-size:26px;margin:4px 0}.infra-pane h2{font-size:20px;margin:0 0 12px}.infra-pane h3{font-size:15px;margin:0 0 10px}.infra-pane p{margin:7px 0}
        .infra-pane.infra-receipt{height:auto;margin:12px 0;padding:18px;border:1px solid var(--dsw-alias-border-l2,#ddd);border-radius:12px;display:block}
        .infra-pane button{font:inherit;padding:7px 13px;border-radius:8px;border:1px solid var(--dsw-alias-border-l2,#ddd);background:transparent;color:inherit;cursor:pointer}.infra-pane button:disabled{opacity:.45;cursor:wait}
        .infra-pane .infra-primary{background:#176b59;color:white;border-color:#176b59}.infra-pane nav,.infra-actions,.infra-heading{display:flex;gap:8px;flex-wrap:wrap;align-items:center}.infra-pane nav{margin-top:16px}.infra-heading{justify-content:space-between}
        .infra-journey{display:flex;gap:8px;list-style:none;padding:0;margin:16px 0;flex-wrap:wrap}.infra-journey li{padding:6px 10px;border-bottom:2px solid transparent;color:var(--dsw-alias-label-secondary,#777)}.infra-journey .infra-current{color:#176b59;border-color:#176b59}.infra-plan{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin:0}.infra-plan dt{font-size:12px;color:var(--dsw-alias-label-secondary,#777)}.infra-plan dd{margin:2px 0;overflow-wrap:anywhere}.infra-task-record[aria-pressed=true]{border-color:#176b59;background:#17865b10}.infra-eyebrow{font-size:11px;letter-spacing:2px;color:#176b59}.infra-scroll{padding:24px;overflow:auto;flex:1}.infra-workspace{display:grid;grid-template-columns:220px minmax(0,1fr);gap:24px}.infra-workspace aside{display:flex;flex-direction:column;gap:8px}.infra-workspace aside button{text-align:left}
        .infra-stack{display:flex;flex-direction:column;gap:14px}.infra-card{border:1px solid var(--dsw-alias-border-l2,#ddd);border-radius:12px;padding:20px}.infra-meta,.infra-pane header p{color:var(--dsw-alias-label-secondary,#777);font-size:12px}.infra-tag{color:#176b59}.infra-gates{display:flex;gap:8px;flex-wrap:wrap}.infra-gates span{padding:6px 10px;border-radius:6px}.infra-pass{color:#176b59;background:#17865b15}.infra-fail,.infra-error{color:#bc4545;background:#c43b3812;padding:10px;border-radius:8px}
        .infra-pane label{display:flex;flex-direction:column;gap:5px;margin:8px 0}.infra-pane input,.infra-pane select,.infra-pane textarea{font:inherit;color:inherit;background:transparent;border:1px solid var(--dsw-alias-border-l2,#ddd);border-radius:7px;padding:8px;width:100%}.infra-pane .infra-check{display:inline-flex;flex-direction:row;align-items:center;margin-right:14px}.infra-check input{width:auto}.infra-pane fieldset{border:1px solid var(--dsw-alias-border-l2,#ddd);border-radius:8px;margin:12px 0}.infra-form-grid{display:grid;grid-template-columns:1fr 1fr;gap:0 14px}.infra-pane details{margin:8px 0}.infra-pane summary{cursor:pointer}.infra-pane pre{white-space:pre-wrap;overflow-wrap:anywhere;font-size:12px;max-height:480px;overflow:auto}.infra-table{overflow:auto}.infra-pane table{width:100%;border-collapse:collapse}.infra-pane td,.infra-pane th{text-align:left;padding:8px;border-bottom:1px solid var(--dsw-alias-border-l2,#ddd);white-space:nowrap}.infra-evidence{display:flex;gap:8px;flex-wrap:wrap;margin:10px 0}.infra-pane button:focus-visible,.infra-pane input:focus-visible{outline:2px solid #176b59;outline-offset:2px}
        @media(max-width:800px){.infra-workspace{grid-template-columns:1fr}.infra-workspace aside{max-height:180px;overflow:auto;padding-right:4px}.infra-pane header{padding:18px}.infra-scroll{padding:14px}.infra-form-grid,.infra-plan{grid-template-columns:1fr}}`;document.head.append(style);return()=>style.remove()})
      ctx.effect(()=>ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'pilot-infra-workbench'},()=>h(Body,{}))))
      ctx.effect(()=>ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:'pilot-infra-workbench',label:()=> '推理任务',order:-10},()=>h('span',null,'◎'))))
      ctx.effect(()=>ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'pilot-workspaces'},()=>{useEffect(()=>ctx.layout.selectPanel(null),[]);return null})))
      ctx.effect(()=>ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'pilot-history',children:{'pilot.history.page':{kind:'keyed',scope:'root'}}},History)))
      ctx.effect(()=>ctx.slots.inject('pilot.history.page',()=>ctx.slots.register({name:'pilot.history.page',key:'evaluations'},()=>h('div',{className:'infra-scroll',style:{height:'100%'}},h(Evaluations)))))
      ctx.effect(()=>ctx.slots.inject('main',()=>ctx.slots.register({name:'main',key:'pilot-evaluation-history'},PreviousHistoryEntry)))
      ctx.effect(()=>ctx.slots.inject('sidebar.panellist',()=>ctx.slots.register({name:'sidebar.panellist',id:'pilot-history',label:()=> '历史记录',order:-9},()=>h('span',null,'◷'))))
      ctx.effect(()=>ctx.sidebarRightTabs.register({id:ID,kind:'pilot-infra',priority:'extension',title:()=> '当前任务',guide:[{id:'infra',order:0,title:()=> '当前任务',description:()=> '推理目标、候选、验收和证据'}]}))
      ctx.effect(()=>ctx.slots.inject('sidebar.right.pane.tab',()=>ctx.slots.register({name:'sidebar.right.pane.tab',key:ID,inject:sessionId=>({sessionId})},props=>h(Body,{...props,compact:true}))))
      ctx.effect(()=>ctx.slots.inject('sidebar.right.pane.tab.title',()=>ctx.slots.register({name:'sidebar.right.pane.tab.title',key:ID},()=>h('span',null,'当前任务'))))
      ctx.effect(()=>ctx.slots.inject('conversation.chat.turnTail',()=>ctx.slots.register({name:'conversation.chat.turnTail',id:ID},ReceiptTail)))
    }
    return { inject, apply }
  },
})
