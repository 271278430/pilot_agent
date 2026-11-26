/** Role admission and per-root shared reservations over official hooks. */
import * as Budget from '../../../plugin/budget.mjs'
import { LlmError, isAgentLoopRequest } from '@deepseek-ai/dsh-llm'
import { mkdirSync, writeFileSync, existsSync, readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import * as Intent from '../../../plugin/intent.mjs'
export const name = 'pilot-policy'
export const inject = ['agents', 'tools', 'llm', 'agentPresets', 'pilotWorkspace', 'tokenMeter', 'workingDirectory', 'subagents']
export function auxiliaryMessageTokens(ctx,message,options){
  const images=(message.content??[]).filter(b=>b.type==='image')
  if(!images.length)return ctx.tokenMeter.estimateMessage(message)
  const pricing=ctx.llm.imageRequestPricing?.(options.provider,options.model)
  if(!pricing)return ctx.tokenMeter.estimateMessage(message)
  const prices=pricing.priceImages(images)
  if(prices.length!==images.length||prices.some(p=>!Number.isFinite(p.visualTokens)||p.visualTokens<0||typeof p.text!=='string'))throw new Error('Invalid native image pricing')
  let at=0
  const content=message.content.map(b=>b.type==='image'?{type:'text',text:prices[at++].text}:b)
  return ctx.tokenMeter.estimateMessage({...message,content})+prices.reduce((sum,p)=>sum+p.visualTokens,0)
}
export function apply(ctx, config) {
  const families = new Map()
  const planning = new Map()
  const views = new Map()
  const budgetPaths = new Map()
  const delegating = new Map()
  const planFailureLimit = config.budget.no_progress_limit ?? 4
  ctx.on('agent/disposed', ({ agent }) => { planning.delete(agent.id); views.delete(agent.id); delegating.delete(agent.id) })
  ctx.on('tools/result', (exec, result) => {
    if (delegating.get(exec.agent?.id) === exec.token) delegating.delete(exec.agent.id)
    if (!['pilot_plan', 'pilot_revise_plan', 'pilot_feedback', 'pilot_infra_plan', 'pilot_infra_revise', 'pilot_infra_workbench_plan'].includes(exec.name) || !exec.agent) return
    const state = planning.get(exec.agent.id)
    if (!state) return
    state.failures = result.isError ? state.failures + 1 : 0
    state.lastError = result.isError ? result.error.message : ''
  })
  ctx.on('tools/pre-execute', async (exec, next) => {
    const infra = views.get(exec.agent?.id)?.task
    const target = exec.name==='send_message' && ctx.agentPresets.composedPreset(exec.agent?.ctx)==='infra-lead'
      ? ctx.agents.get(exec.arguments?.agent_id) : undefined
    const continuing = target && ['infra-expert','repo-expert'].includes(ctx.agentPresets.composedPreset(target.ctx))
    if (!infra || (!['infra_specialist','repo_specialist'].includes(exec.name) && !continuing)) return next()
    if (delegating.has(exec.agent.id)) return {kind:'deny',reason:'Another expert admission is being settled; wait for its official result.'}
    delegating.set(exec.agent.id,exec.token)
    try {
      const children = await ctx.subagents.listChildren(exec.agent.id,exec.signal)
      if (children.some(child => {const live=ctx.agents.get(child.id);return live && live.status!=='idle' && child.id!==target?.id})) {
        delegating.delete(exec.agent.id)
        return {kind:'deny',reason:'One Infra expert is already active. Wait for official child completion before another expert.'}
      }
      const result = await next()
      if (result.kind !== 'allow') delegating.delete(exec.agent.id)
      return result
    } catch (error) { delegating.delete(exec.agent.id); throw error }
  })
  function family(agent) {
    if (!agent) throw new Error('Model request has no official Agent initiator')
    const id = ctx.pilotWorkspace.rootId(agent)
    const current=ctx.pilotWorkspace.cachedView?.(agent)
    if(current?.task)budgetPaths.set(id,resolve(config.root,'runs/pilot-infra',current.task.id,'budget-policy.json'))
    else if(current?.application)budgetPaths.set(id,resolve(config.root,'runs/apps',current.application.id,'budget-policy.json'))
    else if(current)budgetPaths.delete(id)
    if (!families.has(id)) {
      const directory = resolve(config.root, 'runs/harness-policy', id)
      mkdirSync(directory, { recursive: true })
      const checkpointPath = resolve(directory, 'checkpoint.json')
      if (!existsSync(checkpointPath)) writeFileSync(checkpointPath, '{}')
      const hooks = {}
      Budget.apply({ on: (event, handler) => { hooks[event] = handler } }, {
        checkpointPath, meterPath: resolve(directory, 'meter.json'), budget: config.budget,
        resolveBudget: () => {
          const path = budgetPaths.get(id)
          return path && existsSync(path) ? { max_tokens: JSON.parse(readFileSync(path, 'utf8')).max_tokens } : {}
        },
        measureRequest: options => {
          const initiator = ctx.agents.currentInitiator()
          if (!initiator) throw new Error('Model request has no official Agent initiator')
          if (isAgentLoopRequest(options)) {
            const measurement = ctx.tokenMeter.measure(initiator.session, initiator.session.requestHeader())
            return { tokens: measurement.totalTokens, source: 'official_token_meter_' + measurement.baseline.kind }
          }
          // Auxiliary classification and compaction carry their own system and
          // selected messages, not the whole Agent surface.
          const system = options.system ? ctx.tokenMeter.estimateMessage({role:'system',content:[{type:'text',text:options.system}]}) : 0
          const messages = (options.messages ?? []).reduce((sum, message) => sum + auxiliaryMessageTokens(ctx,message,options), 0)
          const tools = options.tools?.length ? ctx.tokenMeter.estimateMessage({ role: 'system', content: [{ type: 'text', text: JSON.stringify(options.tools) }] }) : 0
          return { tokens: system + messages + tools, source: 'official_message_estimator' }
        },
        errorFactory: (reason, detail) => new LlmError(
          `累计预算不足以发送下一次模型请求（${reason}）。已用 ${detail?.used_tokens ?? '—'} token，本次预留 ${detail?.input_reservation ?? '—'} + ${detail?.output_reservation ?? '—'} token。` + (budgetPaths.has(id)
            ? '进度已保存，请在任务或交付面板的“用量与预算”调整后继续。'
            : '当前会话尚未绑定任务，没有任务预算面板。请新建会话并按需引用已有对话继续咨询；不会自动创建执行任务。'),
          'PILOT_BUDGET_LIMIT'),
      })
      families.set(id, hooks)
    }
    return families.get(id)
  }
  ctx.effect(() => ctx.tools.guard(exec => {
    const intentDenied = ctx.pilotWorkspace.router ? ctx.pilotWorkspace.routingDenial(exec.agent,exec.name)
      : Intent.denial(exec.name, ctx.pilotWorkspace.intent?.(exec.agent))
    if (intentDenied) return intentDenied
    const role = exec.agent && ctx.agentPresets.composedPreset(exec.agent.ctx)
    const infra = views.get(exec.agent?.id)?.task
    const build = views.get(exec.agent?.id)?.build_task
    if (infra && ['infra-expert','repo-expert'].includes(role) && exec.name.startsWith('pilot_infra_')
      && !['pilot_infra_status','pilot_infra_strategy','pilot_infra_evidence','pilot_infra_source_read','pilot_infra_engine_source_read','pilot_infra_engine_source_search','pilot_infra_engine_source_status'].includes(exec.name)
      && !infra.state.strategy.experts.includes(role)) return 'This expert is no longer admitted by the current task strategy; return existing findings to the parent.'
    if (infra && role==='infra-lead' && exec.name==='send_message') {
      const target=ctx.agents.get(exec.arguments?.agent_id)
      const targetRole=target && ctx.agentPresets.composedPreset(target.ctx)
      if (['infra-expert','repo-expert'].includes(targetRole) && !infra.state.strategy.experts.includes(targetRole))
        return 'The current task has not admitted that expert; use the current strategy instead of reviving old work.'
    }
    if (build && ['infra_specialist', 'repo_specialist'].includes(exec.name)
      && (role!=='infra-lead' || build.status!=='prepared' || build.active_operation || exec.name!=='repo_specialist'))
      return 'Only the prepared software workbench root may admit the Repo software subagent; experts edit owned source while root freezes and runs checks.'
    if (build && exec.name.startsWith('pilot_infra_') && !exec.name.startsWith('pilot_infra_workbench_')
      && !['pilot_infra_assets', 'pilot_infra_probe', 'pilot_infra_resource_catalog', 'pilot_infra_resource_observe', 'pilot_infra_resource_assess', 'pilot_infra_resource_review'].includes(exec.name))
      return 'This Session owns a source workbench. Use workbench_status and workbench tools; serving tasks require a new Session.'
    if (['infra_specialist', 'repo_specialist'].includes(exec.name)) {
      const expert = exec.name === 'repo_specialist' ? 'repo-expert' : 'infra-expert'
      if (!build && (!infra || infra.state.status !== 'running' || infra.state.session_id !== ctx.pilotWorkspace.rootId(exec.agent)
        || infra.state.strategy.mode !== 'multi' || !infra.state.strategy.experts.includes(expert)))
        return 'This task strategy has not admitted this expert. Read pilot_infra_strategy; single root can execute without delegation.'
    }
    if (exec.name === 'send_message' && infra && infra.state.status !== 'running') return 'Infra task is stopped; expert notifications cannot resume it'
    if (['repo_agent', 'infra_agent', 'send_message'].includes(exec.name) && !infra) {
      const app = views.get(exec.agent?.id)?.application
      if (!app || app.status !== 'running' || app.state.harness_session !== ctx.pilotWorkspace.rootId(exec.agent))
        return 'Delivery is not running in this execution Session. Save the plan or feedback and wait for the user Start/Continue action.'
    }
    if (['repo_agent', 'infra_agent', 'repo_specialist', 'infra_specialist'].includes(exec.name) && exec.arguments?.cwd !== undefined
      && resolve(exec.arguments.cwd) !== resolve(ctx.workingDirectory.get(exec.agent.session)))
      return 'Role children must inherit the approved working directory; another cwd is denied'
    if (exec.name.startsWith('mcp__')) {
      const system=ctx.get('pilotToolSystem')
      return system ? system.denial(exec.agent,exec.name) : 'MCP tool is not admitted'
    }
    return config.allowed[role]?.includes(exec.name) ? undefined : `Tool is outside this role permission scope (${role ?? 'unknown'}: ${exec.name})`
  }))
  ctx.on('agent/pre-step', async (event, next) => {
    // A planning failure remains a failure even if the model changes JSON
    // whitespace or searches history between retries. Let the official loop
    // close this turn, preserving its errors; a new user turn can recover.
    let state = planning.get(event.agent.id)
    if (!state || state.turn !== event.turn) {
      state = { turn: event.turn, failures: 0, lastError: '', review: false }
      planning.set(event.agent.id, state)
    }
    if (state.failures >= planFailureLimit)
      throw new Error(`pilot_plan failed ${state.failures} times in this turn; stopped to avoid unproductive retries. Correct the plan and continue in a new turn. Last error: ${state.lastError.slice(0, 500)}`)
    const view = await ctx.pilotWorkspace.ensure(event.agent, event.signal)
    views.set(event.agent.id, view)
    if (view.application) budgetPaths.set(ctx.pilotWorkspace.rootId(event.agent),
      resolve(config.root, 'runs/apps', view.application.id, 'budget-policy.json'))
    if (view.task) budgetPaths.set(ctx.pilotWorkspace.rootId(event.agent),
      resolve(config.root, 'runs/pilot-infra', view.task.id, 'budget-policy.json'))
    if(!view.task&&!view.application)budgetPaths.delete(ctx.pilotWorkspace.rootId(event.agent))
    // A stopped delivery can receive a genuine user discussion turn. Child
    // notifications cannot restart work; domain tools and delegation guards
    // still require the user's explicit Start/Continue action.
    if (!event.agent.session.header.parentSession && event.messages.some(message => message.source?.kind === 'user'))
      state.review = true
    if (view.task && ['paused', 'cancelled', 'failed', 'completed'].includes(view.task.state.status)
      && !(state.review && roleIsSupervisor(event.agent))) return { kind: 'reject' }
    if (view.application && ctx.pilotWorkspace.rootId(event.agent) === view.application.state.harness_session
      && (['paused', 'cancelled', 'failed', 'completed'].includes(view.application.status)
        || (view.application.status === 'running' && Date.now() >= view.application.state.deadline_epoch * 1000))
      && !(state.review && roleIsSupervisor(event.agent) && view.application.status !== 'running'))
      return { kind: 'reject' }
    return family(event.agent)['agent/pre-step'](event, next)
  })
  function roleIsSupervisor(agent) { return ['supervisor', 'infra-lead'].includes(ctx.agentPresets.composedPreset(agent.ctx)) }
  ctx.on('llm/stream', async function* (options, next) {
    const agent = ctx.agents.currentInitiator()
    yield* family(agent)['llm/stream'](options, next)
  })
}
