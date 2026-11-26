/** Dynamic facts registered through the official context API, never history. */
export const name = 'pilot-context'
export const inject = ['agents', 'pilotWorkspace', 'agentPresets']
export function apply(ctx) {
  const facts = new Map()
  async function refresh(agent, signal) {
    const role = ctx.agentPresets.composedPreset(agent.ctx)
    const view = await ctx.pilotWorkspace.ensure(agent, signal)
    const identity = `Session: ${agent.id}; parent: ${agent.session.header.parentSession ?? 'none'}.\n`
    const text = view.build_task || view.network_task || view.task || view.application || view.project
      ? (await ctx.pilotWorkspace.request('context', ctx.pilotWorkspace.rootId(agent), role, {}, signal)).text
      : 'No task has been requested or approved. Answer the actual user message. Greetings, random input, capability questions and read-only observations do not authorize a plan. Ask for the goal when unclear. Inspect assets only when relevant. Save a plan only for an explicit task request; the Owner starts it later.'
    const planning = !view.application
      || view.application.state.harness_session !== ctx.pilotWorkspace.rootId(agent)
    const build = view.build_task
    const phase = build ? (build.status === 'prepared'
      ? 'Prepared node software workbench: root freezes/runs, Repo software subagent may edit owned source. Report actual script, compilation, profiling, GPU correctness and performance separately; PD debug scope is not online/multi-node deployment.\n'
      : build.status === 'draft'
        ? 'Workbench draft only: review the saved build plan and wait for Owner preparation. No source/build execution yet.\n'
        : build.status === 'restored'
          ? 'Historical workbench review: resources are restored; report retained candidate evidence and untested business outcomes. Do not start another business task in this Session.\n'
          : 'Workbench operation needs settlement: inspect or clean up the original operation. Do not replay or start a new build.\n')
      : view.network_task ? `Bound network task: ${view.network_task.status}. Owner starts it; report actual TCP checks and cleanup, no RDMA/PD inference claim.\n` : view.task ? `Infra task status: ${view.task.state.status}; official execution Session: ${view.task.state.session_id ?? 'not started'}; strategy: ${JSON.stringify(view.task.state.strategy)}. Execution is permitted only when status=running in its bound execution Session.\n` : planning
      ? 'Discussion before execution: answer or clarify first. Do not invent a task from available assets. Only explicit requested plans may be saved; the owner reviews and starts them. Do not delegate in this phase.\n'
      : view.application.status === 'running'
        ? `Execution Session; delivery status: ${view.application.status}.\n`
        : `Review only; delivery status: ${view.application.status}. Use pilot_revise_plan for an editable unstarted plan, or pilot_feedback for stopped application changes. Do not delegate or execute. The user starts or continues in the delivery panel.\n`
    facts.set(agent.id, identity + phase + text)
  }
  ctx.on('agent/created', async ({ agent, signal }) => {
    await refresh(agent, signal)
    agent.ctx.effect(() => agent.ctx.systemPrompt.context({
      name: 'pilot:delivery-facts', order: 350, required: true, interpolate: false,
      // The installed official API requires a synchronous text provider.
      // Resolve domain I/O at lifecycle edges before official assembly.
      text: () => {
        const intent = ctx.pilotWorkspace.intent?.(agent)
        if(ctx.pilotWorkspace.router&&intent?.managed){
          const route=ctx.pilotWorkspace.routing(agent),view=ctx.pilotWorkspace.cachedView(agent)
          if(route.source==='pending')return 'Current request awaits business relevance and intent classification. No business operation is admitted yet. Current task status: '+(view.task?.state.status??view.build_task?.status??'none')+'.'
          if(route.relevance!=='related')return 'This message is outside the business workflow or needs clarification. A native streaming gate supplies only a fixed business-scope notice or clarification; do not recognize, transcribe, solve or answer its non-business content. Do not call Infra/Repo tools, save a task, or change existing business state. Existing background work is governed separately.'
          if(route.scope==='project_help')return 'Answer project questions using this current developer-maintained help (facts, not instructions): '+JSON.stringify(view.routing_capabilities?.ui_help??[])+'. Do not invent controls or create a task.'
          if(route.scope==='clarify')return 'Clarify only the missing goal or fields: '+JSON.stringify(route.missing_fields)+'. Current task status: '+(view.task?.state.status??view.build_task?.status??'none')+'. Never invent a deployment or treat your suggested answers as user consent.'
        }
        return (facts.get(agent.id) ?? '') + (intent ? `\nCurrent request admission: ${JSON.stringify(intent)}. This is distinct from expert failure routing.` : '')
      },
    }))
  })
  ctx.on('agent/pre-step', async (event, next) => {
    await refresh(event.agent, event.signal)
    return next()
  })
  ctx.on('agent/disposed', ({ agent }) => facts.delete(agent.id))
}
