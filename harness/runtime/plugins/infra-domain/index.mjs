/** Machine-domain RPC and Job settlement through official Session APIs. */
import { SessionId } from '@deepseek-ai/dsh-session'
export const name = 'pilot-infra-domain'
export const inject = ['connection', 'pilotWorkspace', 'sessionController', 'workspaceRegistry', 'agents', 'subagents', 'agentPresets', 'permissionPresets', 'sandboxPolicy']
export function apply(ctx) {
  const pending = new Set(), checking = new Set(), abort = new AbortController()
  ctx.effect(() => () => abort.abort())
  const bridge = (operation, id, args = {}, signal) => ctx.pilotWorkspace.request('infra-' + operation, id, 'owner', args, signal)
  async function resolveAgent(id) {
    const found = await ctx.sessionController.resolveAgent(SessionId(id))
    if ('error' in found) throw found.error
    return found.agent
  }
  async function requireIdleTree(agent, signal) {
    const children = await ctx.subagents.listChildren(agent.id, signal)
    if (agent.status !== 'idle' || children.some(child => { const live = ctx.agents.get(child.id); return live && live.status !== 'idle' }))
      throw new Error('Manual changes require an idle root and idle experts; pause and wait for settlement first')
  }
  async function open(taskId, signal) {
    const catalog = await bridge('catalog', '', {}, signal)
    const view = catalog.tasks.find(v => v.task.id === taskId)
    if (!view) throw new Error('Unknown Infra task')
    let id = view.task.state.session_id
    if (!id) {
      // Bind planning and execution to one native root; no extra chat runtime.
      const workspace = await ctx.workspaceRegistry.create(view.cwd, view.task.plan.title)
      const created = await ctx.sessionController.create({ workspaceId: workspace.id, agentPreset: 'infra-lead' })
      id = String(created.sessionId)
      await bridge('bind', id, { task_id: taskId }, signal)
      await ctx.sessionController.rename({ sessionId: created.sessionId, title: view.task.plan.title })
    } else await ctx.sessionController.resolveAgent(SessionId(id))
    return id
  }
  async function work(endpoint, payload, signal) {
    let { session_id: id = '', ...args } = payload
    if (endpoint==='workspace-catalog') return ctx.pilotWorkspace.workspaceCatalog()
    if (endpoint==='workspace-create') return ctx.pilotWorkspace.createWorkspace(args)
    if (endpoint==='workspace-session') return ctx.pilotWorkspace.newWorkspaceSession(args)
    if (endpoint.startsWith('asset-') || endpoint==='resource-executor') {
      return bridge(endpoint,'',args,signal)
    }
    if (endpoint==='workbench-save-profile') return bridge(endpoint,'',args,signal)
    if (endpoint.startsWith('distributed-')) return bridge(endpoint,'',args,signal)
    if (endpoint.startsWith('network-')) {
      let task
      if (endpoint === 'network-run') {
        task=await bridge('network-view','',{task_id:args.task_id},signal)
        if (task.session_id) {
          const agent=await resolveAgent(task.session_id)
          if (ctx.agentPresets.composedPreset(agent.ctx)!=='infra-lead') throw new Error('Pilot root Session required')
          if (ctx.sandboxPolicy.resolve({session:agent.session}).mode==='read-only') throw new Error('This network Session is observation-only; switch to reviewed execution first')
          await requireIdleTree(agent,signal)
        }
      }
      const value=await bridge(endpoint,'',args,signal)
      if (endpoint === 'network-run' && task?.session_id) {
        try {
          await ctx.sessionController.prompt({sessionId:SessionId(task.session_id),requestId:'network-owner-settled-'+task.id,mode:'queue',content:[{type:'text',text:'Owner executed the existing bounded network task '+task.id+'. Read network_status for actual results and restoration. A TCP test is not RDMA or PD validation. Preserve failure and unknown cleanup states; do not rerun the task or create another plan.'}]},signal)
        } catch(error) { return {...value,notification_error:String(error.message).slice(0,500)} }
      }
      return value
    }
    if (endpoint.startsWith('engine-') || ['candidate','launch','test','adapter_check'].includes(endpoint)) {
      if (endpoint === 'engine-native-decision') throw new Error('Native approval decisions cannot be submitted by a Web client')
      const agent=await resolveAgent(id)
      const readonly=['engine-source-status','engine-source-read','engine-source-search'].includes(endpoint)
      if (!readonly && (ctx.sandboxPolicy.resolve({session:agent.session}).mode==='read-only' || agent.status!=='idle'))
        throw new Error('Manual engine operations require an idle root Session with reviewed execution permission')
      if (!readonly) await requireIdleTree(agent, signal)
      if (ctx.agentPresets.composedPreset(agent.ctx)!=='infra-lead') throw new Error('Pilot root Session required')
      const value=await bridge(endpoint,id,args,signal)
      if(endpoint==='engine-agent') await ctx.sessionController.prompt({sessionId:SessionId(id),requestId:'infra-manual-agent-'+crypto.randomUUID(),mode:'queue',content:[{type:'text',text:'Owner returned the current task from manual execution. Continue from the existing engine candidates, approvals and Job evidence. Do not replay unknown operations.'}]},signal)
      return value
    }
    if (endpoint.startsWith('resource-')) {
      if (endpoint === 'resource-native-decision') throw new Error('Native approval decisions cannot be submitted by a Web client')
      if (endpoint === 'resource-session-permission') {
        if (!id || !['observe', 'review'].includes(args.mode)) throw new Error('Select an Infra Session and a supported permission mode')
        const agent = await resolveAgent(id)
        if (ctx.agentPresets.composedPreset(agent.ctx) !== 'infra-lead' || agent.status !== 'idle') throw new Error('Change permissions only for an idle Pilot root Session')
        await requireIdleTree(agent, signal)
        ctx.permissionPresets.set(agent.session, args.mode)
        return { sessionId: id, mode: args.mode }
      }
      if (endpoint === 'resource-propose' && !id) {
        const workspace = await ctx.pilotWorkspace.newInbox(args.plan.title)
        const created = await ctx.sessionController.create({ workspaceId: workspace.id, agentPreset: 'infra-lead' })
        id = String(created.sessionId)
      }
      const value = await bridge(endpoint, id, args, signal)
      if (endpoint === 'resource-catalog' && id) {
        const found = await ctx.sessionController.resolveAgent(SessionId(id))
        if ('error' in found) {
          // An old browser selection may outlive a deleted Session. Only that
          // explicit failure can omit optional metadata from the Owner catalog.
          if (found.error.code !== 'session/not-found') throw found.error
          value.session_permission = null
          value.session_selection = { session_id: id, status: 'missing' }
        } else {
          value.session_permission = { session_id: id, mode: ctx.permissionPresets.current(found.agent.session) }
        }
      }
      if (endpoint === 'resource-execute') {
        await ctx.sessionController.rename({ sessionId: SessionId(value.sessionId), title: value.task.plan.title })
      }
      return value
    }
    if (endpoint.startsWith('workbench-')) {
      if (['workbench-agent','workbench-prepare','workbench-write','workbench-patch','workbench-freeze','workbench-run'].includes(endpoint)) {
        const catalog = await bridge('workbench-catalog', '', {}, signal)
        const task = catalog.tasks.find(t => t.id === args.task_id)
        if (task?.session_id) {
          const agent = await resolveAgent(task.session_id)
          if (ctx.agentPresets.composedPreset(agent.ctx) !== 'infra-lead') throw new Error('Pilot root Session required')
          if (ctx.sandboxPolicy.resolve({session:agent.session}).mode === 'read-only') throw new Error('This workbench Session is observation-only; switch to reviewed execution first')
          await requireIdleTree(agent, signal)
        }
        if (endpoint === 'workbench-agent') {
          if (!task?.session_id || task.status !== 'prepared' || task.active_operation) throw new Error('Prepare and settle the existing workbench before starting its agent')
          await ctx.sessionController.prompt({sessionId:SessionId(task.session_id),requestId:'workbench-owner-'+crypto.randomUUID(),mode:'queue',content:[{type:'text',text:'Owner started the existing prepared software task. Read workbench_status for task '+task.id+'. Use its frozen node, image, device scope and trusted checks to pursue the saved goal. Preserve the baseline and failures, use the software subagent only as admitted, freeze complete source before running, report separate numerical/performance/quality evidence and restore task-owned objects. Do not create another plan or replay unknown operations.'}]},signal)
          return {...task, sessionId:task.session_id}
        }
      }
      if (endpoint === 'workbench-create') {
        const task = await bridge(endpoint, '', args, signal)
        if (task.session_id) return {...task, sessionId:task.session_id}
        const workspace = await ctx.pilotWorkspace.newInbox(task.plan.title)
        const created = await ctx.sessionController.create({ workspaceId: workspace.id, agentPreset: 'infra-lead' })
        await bridge('workbench-bind', '', { task_id: task.id, session_id: String(created.sessionId) }, signal)
        return { ...task, sessionId: String(created.sessionId) }
      }
      return bridge(endpoint, '', args, signal)
    }
    if (['catalog', 'probe', 'guide', 'architecture', 'evaluation-catalog'].includes(endpoint)) return bridge(endpoint, '', args, signal)
    if (endpoint === 'new-session') {
      // A domain-bound Session can still be blank to the official chat UI.
      // A new Pilot request must never reuse that existing business identity.
      let workspace = args.workspace_id ? ctx.workspaceRegistry.get(args.workspace_id) : undefined
      if(args.workspace_id&&!workspace)throw new Error('所选工作区已不可用，请重新选择')
      if(!workspace&&id){const current=await resolveAgent(id),rootId=ctx.pilotWorkspace.rootId(current);workspace=ctx.workspaceRegistry.list().find(w=>w.sessionIds.includes(rootId))}
      workspace ??= ctx.workspaceRegistry.list()[0]
      if(!workspace)workspace=await ctx.workspaceRegistry.create(ctx.pilotWorkspace.inboxPath(),'Pilot 工作区')
      const created = await ctx.sessionController.create({ workspaceId: workspace.id, agentPreset: 'infra-lead' })
      return { sessionId: String(created.sessionId) }
    }
    if (endpoint === 'create') {
      const view = await bridge(endpoint, '', args, signal)
      return { ...view, sessionId: await open(view.task.id, signal) }
    }
    if (endpoint === 'open') return { sessionId: await open(args.task_id, signal) }
    if (endpoint === 'view') {
      const agent = ctx.agents.get(SessionId(id))
      if (agent && agent.status === 'idle' && !pending.has(id)) await check(agent, true)
      const view = await bridge(endpoint, id, args, signal)
      if(agent){const routing=ctx.pilotWorkspace.routing?.(agent);if(routing)view.request_routing=routing}
      if (view.task && agent && ctx.sandboxPolicy.resolve({session:agent.session}).mode === 'read-only') {
        view.actions = view.actions.filter(a => !['start','resume'].includes(a))
        view.session_permission = 'observe'
        if (view.engine_source) view.engine_source.manual_mutation=false
      }
      if (view.engine_source) view.engine_source.manual_mutation=Boolean(agent && agent.status==='idle' && ctx.sandboxPolicy.resolve({session:agent.session}).mode!=='read-only')
      return view
    }
    if (endpoint === 'revise') {
      const view = await bridge(endpoint, id, args, signal)
      await ctx.sessionController.rename({ sessionId: SessionId(id), title: view.task.plan.title })
      return view
    }
    if (['budget', 'evidence', 'evidence-download', 'report'].includes(endpoint)) return bridge(endpoint, id, args, signal)
    if (['start', 'resume', 'pause', 'cancel', 'stop_service', 'recover'].includes(endpoint)) {
      if (['start', 'resume'].includes(endpoint)) {
        const agent = await resolveAgent(id)
        if (ctx.sandboxPolicy.resolve({ session: agent.session }).mode === 'read-only') throw new Error('This Session is observation-only; switch to reviewed execution before Start')
        const current = await bridge('view', id, {}, signal)
        if (!current.task) throw new Error('No reviewed Infra plan')
        await bridge('bind', id, { task_id: current.task.id }, signal)
      }
      const view = await bridge(endpoint, id, args, signal)
      if (['start', 'resume'].includes(endpoint)) {
        await ctx.sessionController.prompt({ sessionId: SessionId(id), requestId: 'infra-control-' + args.request_id, mode: 'queue',
          content: [{ type: 'text', text: 'Owner started/resumed the reviewed machine InfraTask. Read pilot_infra_status and strategy. Continue from registered candidates and actual evidence; use only admitted GPUs and optional experts. Submit bounded Jobs and wait for real settlement. Only finish after independent current acceptance.' }] }, signal)
      } else if (['pause', 'cancel', 'recover'].includes(endpoint)) {
        const agent = ctx.agents.get(SessionId(id))
        if (agent) for (const child of await ctx.subagents.listChildren(agent.id, signal))
          ctx.subagents.interrupt(child.id, { kind: 'ancestor', agent })
        await ctx.sessionController.cancel({ sessionId: SessionId(id) })
      }
      return { ...view, sessionId: id }
    }
    throw new Error('Unsupported Infra operation')
  }
  ctx.effect(() => ctx.connection.rpc.handle('/pilot-infra-api', async (endpoint, payload, signal, peer) => {
    if (peer.principal.kind !== 'owner') return { ok: false, error: { code: 'pilot/forbidden', message: 'Owner required', details: {} } }
    const key = endpoint.startsWith('workspace-') ? 'workspace-'+payload?.request_id : payload?.session_id ?? payload?.task_id ?? 'new-task'
    const mutation = !['workspace-catalog','catalog', 'view', 'evidence', 'evidence-download', 'report', 'probe', 'guide', 'architecture', 'evaluation-catalog', 'resource-catalog', 'resource-review', 'resource-assess','asset-catalog','asset-match','asset-refresh','network-catalog','network-view','distributed-catalog','distributed-preflight'].includes(endpoint)
    let claimed = false
    try {
      if (mutation && pending.has(key)) throw new Error('A task operation is still being settled')
      if (mutation) { pending.add(key); claimed = true }
      return { ok: true, value: await work(endpoint, payload ?? {}, signal) }
    } catch (error) { return { ok: false, error: { code: 'pilot/rejected', message: String(error.message).slice(0,1500), details: {} } } }
    finally { if (claimed) pending.delete(key) }
  }))
  const roots = new Map()
  ctx.on('agent/created', ({ agent }) => { if (!agent.session.header.parentSession) roots.set(agent.id, agent) })
  ctx.on('agent/disposed', ({ agent }) => roots.delete(agent.id))
  async function check(agent, idle = false) {
    if (ctx.agentPresets.composedPreset(agent.ctx) !== 'infra-lead') return
    if (checking.has(agent.id)) return
    checking.add(agent.id)
    try {
      try {
        const assets=await bridge('asset-monitor',String(agent.id),{},abort.signal)
        if (Array.isArray(assets)&&assets.length) {
          const operation=assets.at(-1)
          await ctx.sessionController.prompt({sessionId:agent.id,requestId:'infra-settled-asset-'+operation.id,mode:'queue',content:[{type:'text',text:'The approved node asset operation settled. Read pilot_infra_asset_status for operation '+operation.id+'. This is asset preparation, not model deployment or task completion. Do not replay an unknown operation or create a default task.'}]},abort.signal)
          await bridge('asset-notified',String(agent.id),{operation_id:operation.id},abort.signal)
        }
      } catch(error) { if(abort.signal.aborted)throw error }
      const view = await bridge('view', String(agent.id), {}, abort.signal)
      if (!view.task || view.task.state.status !== 'running') return
      if (view.task.state.execution_mode==='manual') return
      const notified = view.task.state.notified_jobs ?? []
      const settled = view.events.filter(e => e.kind === 'job_settled' && !notified.includes(e.data.id))
      if (settled.length) {
        const event = settled[settled.length - 1]
        await ctx.sessionController.prompt({ sessionId: agent.id, requestId: 'infra-settled-' + event.data.id, mode: 'queue',
          content: [{ type: 'text', text: 'The bounded Infra Job settled. This is progress, not completion. Current domain facts contain its summary. Read only the needed registered evidence, then choose the next operation. Job reference: ' + JSON.stringify({event_id:event.seq,job_id:event.data.id,action:event.data.action,candidate_id:event.data.candidate_id,status:event.data.status,evidence:event.data.evidence}) }] }, abort.signal)
        await bridge('notified', String(agent.id), { job_ids: settled.map(e => e.data.id) }, abort.signal)
      } else if ((idle || agent.status === 'idle') && !view.task.state.active_job) {
        // A timer may have overlapped the idle edge. Settle the official tree
        // again so that an active child cannot be mistaken for abandonment.
        await ctx.pilotWorkspace.waitTree(agent, abort.signal)
        const current = await bridge('view', String(agent.id), {}, abort.signal)
        if (current.task.state.active_job || agent.status !== 'idle') return
        await bridge('idle', String(agent.id), {}, abort.signal)
      }
    } finally { checking.delete(agent.id) }
  }
  ctx.on('agent/status', ({ agent, status }) => {
    if (status !== 'idle' || !roots.has(agent.id)) return
    ctx.pilotWorkspace.waitTree(agent, abort.signal).then(() => check(agent, true)).catch(() => {})
  })
  ctx.effect(() => {
    let sweeping = false
    // Reconcile persisted grants even when their native Session is unloaded
    // or the accepted task has finished. The domain only stops owned services.
    const sweep = async () => {
      if (sweeping || abort.signal.aborted) return
      sweeping = true
      try { await bridge('authority-reconcile', '', {}, abort.signal) }
      catch (error) { if (!abort.signal.aborted) ctx.logger.warn('Resource lifecycle: ' + String(error.message).slice(0,200)) }
      finally { sweeping = false }
    }
    const timer = setInterval(() => { for (const agent of roots.values()) check(agent).catch(error => {
      if (!abort.signal.aborted) ctx.logger.warn('Infra settlement check: ' + String(error.message).slice(0,200))
    }); sweep() }, 5000)
    timer.unref()
    return () => clearInterval(timer)
  })
}
