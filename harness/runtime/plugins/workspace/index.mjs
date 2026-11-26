/** Official Workspace/Session binding and bounded Python domain transport. */
import { Service } from '@deepseek-ai/cordis'
import { defineTool } from '@deepseek-ai/dsh-tools'
import { spawn } from 'node:child_process'
import { mkdirSync,existsSync,lstatSync } from 'node:fs'
import { resolve } from 'node:path'
import { randomUUID } from 'node:crypto'
import * as Intent from '../../../plugin/intent.mjs'
import { ManagedWorkspaces } from './managed-workspaces.mjs'
import { appliedRoot } from '../host-settings/config.mjs'

export const name = 'pilot-workspace'
export const inject = ['agents', 'sessions', 'workingDirectory', 'workspaceRegistry', 'subagents', 'sessionController']

export class PilotWorkspace extends Service {
  constructor(ctx, config) {
    super(ctx, 'pilotWorkspace')
    this.config = config
    this.userInputs = new Map()
    this.domainViews = new Map()
    this.available = new Map()
    this.hints = new Map()
    this.managedWorkspaces = new ManagedWorkspaces(ctx, config.root)
    this.managedWorkspaces.base()
  }
  rootId(agent) {
    let session = agent.session
    const seen = new Set()
    while (session.header.parentSession) {
      if (seen.has(session.id)) throw new Error('Invalid Session lineage')
      seen.add(session.id)
      const parent = this.ctx.sessions.get(session.header.parentSession)
      if (!parent) throw new Error('Parent Session must be restored before domain access')
      session = parent
    }
    return String(session.id)
  }
  async newInbox(title) {
    const path = resolve(this.inboxPath(), randomUUID())
    mkdirSync(path, { mode: 0o700 })
    return this.ctx.workspaceRegistry.create(path, title)
  }
  workspaceCatalog() { return {root:this.inboxPath(),workspaces:this.ctx.workspaceRegistry.list().map(w=>{let available=false;try{available=lstatSync(w.path).isDirectory()&&!lstatSync(w.path).isSymbolicLink()}catch{}return {id:String(w.id),title:w.title,path:w.path,sessions:w.sessionIds.length,available}})} }
  inboxPath() { return appliedRoot(this.config.root)??this.managedWorkspaces.base() }
  directoryScopes() { return [this.inboxPath(),this.managedWorkspaces.base(),...this.ctx.workspaceRegistry.list().map(w=>w.path)] }
  createWorkspace(args) { return this.managedWorkspaces.create(args) }
  newWorkspaceSession(args) { return this.managedWorkspaces.session(args) }
  async request(operation, sessionId, role, arguments_ = {}, signal) {
    signal?.throwIfAborted()
    return await new Promise((done, fail) => {
      const child = spawn(this.config.python, ['-m', 'pilot_host.bridge', this.config.root], {
        cwd: this.config.root, windowsHide: true,
        env: { ...process.env, PILOT_HOST_ROOT:this.config.root, PYTHONPATH: this.config.pythonPath ?? resolve(this.config.root, 'src'), PYTHONIOENCODING: 'utf-8' },
        stdio: ['pipe', 'pipe', 'pipe'],
      })
      let output = '', overflow = false
      // Child termination leaves any begun domain operation unsettled. The
      // existing operation claim blocks blind replay after unknown effects.
      const abort = () => child.kill()
      signal?.addEventListener('abort', abort, { once: true })
      child.stdout.setEncoding('utf8')
      child.stdout.on('data', chunk => {
        output += chunk
        if (output.length > 4000000) { overflow = true; child.kill() }
      })
      child.stderr.resume() // diagnostics stay out of model/client credentials
      child.on('error', fail)
      child.on('close', () => {
        signal?.removeEventListener('abort', abort)
        try {
          signal?.throwIfAborted()
          if (overflow) throw new Error('Domain response exceeds byte budget')
          const reply = JSON.parse(output)
          if (!reply.ok) throw new Error(reply.error)
          done(reply.value)
        } catch (error) { fail(error) }
      })
      child.stdin.end(JSON.stringify({ operation, session_id: sessionId, role, arguments: arguments_ }))
    })
  }
  latestInput(agent) { return this.userInputs.get(agent?.id)?.message }
  attachRouter(router) { this.router = router }
  routing(agent) { return this.router?.current(agent) }
  routingDenial(agent,name) { return this.router?.guard(agent,name) }
  cachedView(agent) { return agent ? this.domainViews.get(this.rootId(agent)) ?? {} : {} }
  availability(agent,signal) {
    const id=this.rootId(agent)
    if(!this.available.has(id))this.available.set(id,this.view(agent,signal).catch(error=>{this.available.delete(id);throw error}))
    return this.available.get(id)
  }
  routingHints(agent,signal) {
    const id=this.rootId(agent)
    if(!this.hints.has(id))this.hints.set(id,this.request('infra-routing-context',id,'infra-lead',{},signal).then(hints=>{const view=this.domainViews.get(id);if(view)view.routing_capabilities=hints;return hints}).catch(error=>{this.hints.delete(id);throw error}))
    return this.hints.get(id)
  }
  async toolAllowed(agent,name,signal,selection) {
    if(this.router)return this.router.allowed(agent,name,signal,selection)
    return !Intent.denial(name,this.intent(agent))
  }
  intent(agent) { return this.router ? this.router.current(agent) : Intent.nativeRequest(agent?.session, this.latestInput(agent)) }
  async view(agent, signal) {
    const id=this.rootId(agent),view=await this.request('view', id, 'supervisor', {}, signal)
    if(this.hints.has(id))view.routing_capabilities=await this.hints.get(id)
    this.domainViews.set(id,view)
    return view
  }
  async rename(agent, title) {
    // Service methods keep the provider's declared dependencies. Accessing
    // workspace.ctx from a consumer instead uses that consumer's inject scope.
    return this.ctx.sessionController.rename({ sessionId: agent.id, title })
  }
  async ensure(agent, signal) {
    const view = await this.view(agent, signal)
    this.available.set(this.rootId(agent),Promise.resolve(view))
    // A draft has a Session identity, but its first planning tool still owns
    // the inbox scope. Switching cwd there replaces the tool pipeline before
    // it can commit concludeTurn. Bind execution cwd at the started step.
    if (view.cwd && (view.application?.state.harness_session === this.rootId(agent)
      || (view.task?.state.session_id === this.rootId(agent) && view.task.state.status !== 'draft'))
      && this.ctx.workingDirectory.get(agent.session) !== view.cwd)
      await this.ctx.workingDirectory.set(agent, view.cwd, signal)
    return view
  }
  async waitTree(agent, signal) {
    // Official child settlement may wake the parent. SessionController.wait
    // alone can return as soon as the root becomes idle.
    const wait = async promise => {
      if (!signal) return promise
      let aborted
      try {
        return await Promise.race([promise, new Promise((_, reject) => {
          aborted = () => reject(signal.reason)
          if (signal.aborted) aborted()
          else signal.addEventListener('abort', aborted, { once: true })
        })])
      } finally { signal.removeEventListener('abort', aborted) }
    }
    for (;;) {
      signal?.throwIfAborted()
      await wait(agent.whenIdle())
      const joined = await wait(this.ctx.subagents.waitForChildren(agent))
      if (!joined && agent.status === 'idle') return
    }
  }
}
export function apply(ctx, config) {
  const workspace = new PilotWorkspace(ctx, config)
  ctx.on('agent/inbox/claimed', ({agent,message,turn}) => {
    const human=message.source?.kind==='user'&&!/^infra-(?:control-|settled-|manual-agent-)/.test(message.source.rpcId??'')
    const previous=workspace.userInputs.get(agent.id)
    if(previous?.turn!==turn||human||!previous.human)workspace.userInputs.set(agent.id,{message,turn,human})
    workspace.available.delete(workspace.rootId(agent))
    workspace.hints.delete(workspace.rootId(agent))
  })
  ctx.on('agent/disposed', ({agent}) => workspace.userInputs.delete(agent.id))
  return workspace
}

/** Scoped tool registration owned by each domain plugin's effect lifetime. */
export function registerTools(ctx, config) {
  for (const tool of config.tools) ctx.effect(() => ctx.tools.register(defineTool({
    name: 'pilot_' + tool.name, description: tool.description,
    parameters: tool.parameters,
    output: { schema: { type: 'json' }, render: (_args, value) => [{ type: 'text', text: JSON.stringify(value) }] },
    execute: async (args, exec) => {
      const agent = exec.agent
      if (!agent) throw new Error('Official Agent required')
      const workspace = ctx.pilotWorkspace
      const denied = workspace.router ? workspace.routingDenial(agent,'pilot_' + tool.name) : Intent.denial('pilot_' + tool.name, workspace.intent(agent))
      if (denied) throw new Error(denied)
      const rootId = workspace.rootId(agent)
      const operation = tool.name.startsWith('infra_') ? 'infra-tool' : ({ plan: 'plan', resources: 'resources', revise_plan: 'revise-plan', feedback: 'feedback' })[tool.name] ?? 'tool'
      await workspace.ensure(agent, exec.signal)
      if (operation === 'infra-tool' && !['infra_status','infra_assets','infra_probe','infra_evidence','infra_source_read','infra_strategy','infra_resource_catalog','infra_resource_observe','infra_resource_assess','infra_resource_review','infra_workbench_profiles','infra_workbench_status','infra_workbench_read','infra_engine_source_status','infra_engine_source_read','infra_engine_source_search','infra_asset_catalog','infra_asset_status','infra_stack_candidates','infra_model_metadata','infra_compatibility_docs'].includes(tool.name)
        && ctx.sandboxPolicy.resolve({ session: agent.session }).mode === 'read-only')
        throw new Error('This Session is observation-only; the owner must change permission before execution planning')
      let value = await workspace.request(operation, rootId, config.role,
        operation === 'infra-tool' ? { name: tool.name.slice(6), input: args } : operation === 'tool' ? { name: tool.name, input: args } : args, exec.signal)
      if (tool.name==='infra_asset_request_prepare'&&!value.decision) {
        const asset=value.scope.asset
        const reason=`Pilot asset preparation ${value.id} / SHA256 ${value.digest}\nNode: ${asset.server_id}; asset: ${asset.asset_id}; revision: ${asset.revision}\nSource: ${JSON.stringify(asset.source)}\nMaximum size: ${asset.max_bytes} bytes. Download/import only; no GPU deployment or host installation.`
        const decision=await ctx.approval.request({agent,toolName:'pilot_'+tool.name,callId:exec.callId,reason,displayReason:reason,signal:exec.signal})
        value=await workspace.request('infra-asset-native-decision',rootId,'native-approval',{review_id:value.id,expected_digest:value.digest,decision})
      }
      if (tool.name === 'infra_resource_request_approval' && !value.decision) {
        const reason = `Pilot resource proposal ${value.id} / SHA256 ${value.digest}\n${value.reason}\nServer: ${value.server_id}; GPUs: ${value.plan.gpu_ids.join(', ')}\nLimits: ${JSON.stringify(value.limits)}\nModel/engine: ${value.plan.model_asset} / ${value.plan.image_asset}\nOne task draft only; Owner Start remains required. No host administration.`
        const decision = await ctx.approval.request({ agent, toolName: 'pilot_' + tool.name,
          callId: exec.callId, reason, displayReason: reason, signal: exec.signal })
        // Commit the actual native outcome, including cancellation, even when
        // the tool signal aborted. A decision can never be model-supplied.
        value = await workspace.request('infra-resource-native-decision', rootId, 'native-approval',
          { proposal_id: value.id, expected_digest: value.digest, decision })
      }
      if (tool.name === 'infra_engine_request_approval' && !value.decision) {
        const reason = `Pilot engine artifact ${value.candidate_id} / SHA256 ${value.digest}\nChanged files: ${value.changed_files.join(', ') || 'baseline'}\nArtifact: ${JSON.stringify(value.artifact)}\nExecute this exact task artifact only in its isolated runtime; independent acceptance remains required.`
        const decision = await ctx.approval.request({ agent, toolName: 'pilot_' + tool.name,
          callId: exec.callId, reason, displayReason: reason, signal: exec.signal })
        value = await workspace.request('infra-engine-native-decision', rootId, 'native-approval',
          { candidate_id: value.candidate_id, expected_digest: value.digest, decision })
      }
      if (operation === 'feedback') value = { application: value, requires_start: true }
      // Domain failures are official tool failures too, so the model and Web
      // see the same failed result. Never silently replay a failed mutation.
      if (operation === 'tool' && value?.ok === false)
        throw new Error(value.error || 'Pilot domain tool failed; inspect the previous result before retrying')
      const updated = await workspace.ensure(agent, exec.signal)
      if (operation === 'infra-tool' && value.requires_start && ['infra_plan', 'infra_revise'].includes(tool.name))
        await workspace.rename(agent, value.task.plan.title)
      if (operation === 'infra-tool' && tool.name === 'infra_workbench_plan' && value.requires_start)
        await workspace.rename(agent, value.build_task.plan.title)
      if (operation === 'infra-tool' && value.requires_start) exec.concludeTurn()
      if (operation === 'infra-tool' && tool.name === 'infra_finish' && value.passed) exec.concludeTurn()
      if (operation === 'infra-tool' && tool.name === 'infra_workbench_cleanup' && value.restored) exec.concludeTurn()
      if (operation === 'infra-tool' && value.job_id) exec.concludeTurn()
      // Official terminal-success marker commits the plan result, then closes
      // this turn while the user reviews it in the delivery panel.
      if (['plan', 'revise-plan', 'feedback'].includes(operation) && value.requires_start) exec.concludeTurn()
      // Completion is a committed domain result, not a request for one more
      // model-authored closing sentence. Use the official terminal marker.
      if (operation === 'tool' && tool.name === 'finish' && value.ok && value.result?.passed) {
        const application = updated.application
        value = { ...value, application, delivery_completed: true }
        exec.concludeTurn()
      }
      return value
    },
  }), tool.name.startsWith('infra_')
    ? {when: ({agent,signal,modelSelection}) => ctx.pilotWorkspace.toolAllowed
      ? ctx.pilotWorkspace.toolAllowed(agent,'pilot_' + tool.name,signal,modelSelection)
      : !Intent.denial('pilot_' + tool.name,ctx.pilotWorkspace.intent(agent))}
    : undefined))
}
