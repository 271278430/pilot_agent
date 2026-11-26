import { registerTools } from '@pilot/workspace'
export const name = 'pilot-delivery'
export const inject = ['pilotWorkspace', 'tools', 'agents', 'sessionController', 'subagents', 'agentPresets']
export function apply(ctx, config) {
  if (!config.host) { registerTools(ctx, config); return }
  const waiting = new Set(), abort = new AbortController()
  ctx.effect(() => () => abort.abort())
  const roots = new Map(), checking = new Set()
  ctx.on('agent/created', ({ agent }) => {
    if (!agent.session.header.parentSession) roots.set(agent.id, agent)
  })
  ctx.on('agent/disposed', ({ agent }) => roots.delete(agent.id))
  async function deadline(agent) {
    if (ctx.agentPresets.composedPreset(agent.ctx) !== 'supervisor') return
    const view = await ctx.pilotWorkspace.view(agent, abort.signal)
    const app = view.application
    if (app?.state.harness_session !== agent.id || app.status !== 'running'
      || Date.now() < app.state.deadline_epoch * 1000) return
    await ctx.pilotWorkspace.request('expire', String(agent.id), 'supervisor', {}, abort.signal)
    for (const child of await ctx.subagents.listChildren(agent.id, abort.signal))
      ctx.subagents.interrupt(child.sessionId, { kind: 'ancestor', agent })
    await ctx.sessionController.cancel({ sessionId: agent.id })
  }
  ctx.effect(() => {
    const timer = setInterval(() => {
      for (const agent of roots.values()) {
        if (checking.has(agent.id)) continue
        checking.add(agent.id)
        deadline(agent).catch(error => {
          if (!abort.signal.aborted) ctx.logger.warn(`Delivery deadline check failed: ${String(error.message).slice(0,300)}`)
        }).finally(() => checking.delete(agent.id))
      }
    }, 5000)
    timer.unref()
    return () => clearInterval(timer)
  })
  // Observe, await the official tree, then settle domain acceptance. This
  // neither drives model turns nor replaces the Subagent scheduler.
  ctx.on('agent/status', ({ agent, status }) => {
    if (status !== 'idle' || !roots.has(agent.id) || waiting.has(agent.id)) return
    waiting.add(agent.id)
    const settle = async () => {
      if (ctx.agentPresets.composedPreset(agent.ctx) !== 'supervisor') return
      await ctx.pilotWorkspace.waitTree(agent, abort.signal)
      const view = await ctx.pilotWorkspace.view(agent, abort.signal)
      if (view.application?.state.harness_session === agent.id && view.application.status === 'running')
        await ctx.pilotWorkspace.request('reconcile', String(agent.id), 'supervisor', {}, abort.signal)
    }
    settle().catch(error => {
      if (!abort.signal.aborted) ctx.logger.warn(`Delivery settlement failed for ${agent.id}: ${String(error.message).slice(0,300)}`)
    }).finally(() => waiting.delete(agent.id))
  })
}
