/** Domain RPC admitted by the official Connection; no duplicate chat API. */
import { SessionId } from '@deepseek-ai/dsh-session'
import { writeFileSync } from 'node:fs'
import { resolve } from 'node:path'
export const name = 'pilot-delivery-ui'
export const inject = ['connection', 'webServer', 'pilotWorkspace', 'sessionController', 'workspaceRegistry', 'subagents', 'agents']
// Presentation label for the retained migration demo, without rewriting its reviewed plan.
const displayTitle = title => title === 'Harness 迁移验收：中文问答' ? 'Pilot · 中文问答演示' : title
export function apply(ctx, config) {
  // Login URL is available to the launcher/browser, never printed in logs.
  writeFileSync(resolve(config.root, '.local/harness-web/entry.json'),
    JSON.stringify({ url: ctx.connection.authenticatedUrl(config.clientUrl) }), { mode: 0o600 })
  const pending = new Map()
  const work = async (endpoint, args, signal) => {
    const bridge = ctx.pilotWorkspace
    const sessionId = String(args.session_id ?? '')
    args = { ...args }
    delete args.session_id // Routing identity is not part of domain DTOs.
    if (['catalog', 'legacy', 'project-preflight', 'project-confirm', 'project-policy-preflight'].includes(endpoint))
      return bridge.request(endpoint, '', 'supervisor', args, signal)
    if (endpoint === 'project-manage') {
      const project = await bridge.request(endpoint, '', 'supervisor', args, signal)
      const workspace = ctx.workspaceRegistry.get(project.state.harness_workspace_id)
      if (workspace && args.request.action === 'rename') await workspace.setTitle(project.title)
      return project
    }
    if (endpoint === 'project-open') {
      const catalog = await bridge.request('catalog', '', 'supervisor', {}, signal)
      const project = catalog.projects.find(item => item.id === args.project_id && item.status === 'ready')
      if (!project) throw new Error('Project is not ready')
      const workspace = await ctx.workspaceRegistry.create(project.path, project.title)
      const created = await ctx.sessionController.create({ workspaceId: workspace.id, agentPreset: 'supervisor' })
      await bridge.request('project-bind', String(created.sessionId), 'supervisor', { project_id: project.id, workspace_id: workspace.id }, signal)
      await ctx.sessionController.rename({ sessionId: created.sessionId, title: project.title })
      return { sessionId: created.sessionId }
    }
    if (endpoint === 'open') {
      const catalog = await bridge.request('catalog', '', 'supervisor', {}, signal)
      const application = catalog.applications.find(item => item.id === args.application_id)
      if (!application) throw new Error('Unknown application')
      const id = SessionId(application.state.harness_session ?? 'delivery-' + application.id)
      const cwd = (await bridge.request('application-workspace', '', 'supervisor', { id: application.id }, signal)).cwd
      const workspace = await ctx.workspaceRegistry.create(cwd, displayTitle(application.title))
      if (!application.state.harness_session) {
        await ctx.sessionController.create({ workspaceId: workspace.id, sessionId: id, agentPreset: 'supervisor' })
        await bridge.request('bind', String(id), 'supervisor', { application_id: application.id }, signal)
      }
      return { sessionId: id }
    }
    if (['view', 'html', 'preview', 'preview-health', 'plan-options', 'revise-plan'].includes(endpoint))
      return bridge.request(endpoint, sessionId, 'supervisor', args, signal)
    if (endpoint === 'budget-update') return bridge.request(endpoint, sessionId, 'owner', args, signal)
    if (endpoint === 'wait') {
      const resolved = await ctx.sessionController.resolveAgent(SessionId(sessionId))
      await bridge.waitTree(resolved.agent, signal)
      return bridge.request('reconcile', sessionId, 'supervisor', {}, signal)
    }
    const view = await bridge.request('view', sessionId, 'supervisor', {}, signal)
    if (!view.application) throw new Error('No reviewed plan in this Session')
    if (endpoint === 'start') {
      const application = view.application
      const id = SessionId(application.state.harness_session ?? 'delivery-' + application.id)
      if (application.status === 'running') return { sessionId: id }
      const workspace = await ctx.workspaceRegistry.create(view.cwd, displayTitle(application.title))
      if (!application.state.harness_session)
        await ctx.sessionController.create({ workspaceId: workspace.id, sessionId: id, agentPreset: 'supervisor' })
      else await ctx.sessionController.resolveAgent(id)
      await ctx.sessionController.rename({ sessionId: id, title: displayTitle(application.title) })
      await bridge.request('bind', String(id), 'supervisor', { application_id: application.id }, signal)
      const started = await bridge.request('start', String(id), 'supervisor', { expected_version: args.expected_version }, signal)
      const requestId = started.state.harness_request_id
      await ctx.sessionController.prompt({ sessionId: id, requestId, mode: 'queue',
        content: [{ type: 'text', text: 'The user started or continued this reviewed application plan. Read current status and deliver using the role Presets. Call list_agents and reuse existing children and exact history on resume. Preserve the current tested candidate and prepared model; continue its deployment and independent acceptance when evidence is still current. Rebuild only for changed requirements or concrete source/acceptance failures. A child message is progress, not proof of completion; rely on official child settlement. Only finish after the current candidate passes independent genuine-inference acceptance.' }] }, signal)
      return { sessionId: id }
    }
    if (endpoint === 'pause' || endpoint === 'cancel') {
      const result = await bridge.request(endpoint, sessionId, 'supervisor', {}, signal)
      const execution = view.application.state.harness_session
      if (execution) {
        const parent = ctx.agents.get(SessionId(execution))
        if (parent) {
          for (const child of await ctx.subagents.listChildren(parent.id, signal))
            ctx.subagents.interrupt(child.sessionId, { kind: 'ancestor', agent: parent })
          await ctx.sessionController.cancel({ sessionId: parent.id })
        }
      }
      return result
    }
    if (endpoint === 'feedback') return bridge.request(endpoint, sessionId, 'supervisor', args, signal)
    throw new Error('Unknown delivery endpoint')
  }
  ctx.effect(() => ctx.connection.rpc.handle('/pilot-api', async (endpoint, payload, signal, peer) => {
    if (peer.principal.kind !== 'owner') return { ok: false, error: { code: 'pilot/forbidden', message: 'Owner connection required', details: {} } }
    try {
      const operation = endpoint
      const key = String(payload?.session_id ?? '')
      if (['start', 'pause', 'cancel', 'feedback', 'revise-plan', 'budget-update'].includes(operation)) {
        if (pending.has(key)) throw new Error('A delivery control is already being settled')
        const promise = work(operation, payload ?? {}, signal)
        pending.set(key, promise)
        try { return { ok: true, value: await promise } } finally { pending.delete(key) }
      }
      return { ok: true, value: await work(operation, payload ?? {}, signal) }
    } catch (error) { return { ok: false, error: { code: 'pilot/rejected', message: String(error.message).slice(0,1500), details: {} } } }
  }))
}
