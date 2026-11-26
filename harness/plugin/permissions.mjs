/** Role policy over official scoped Tools and Presets. */
import { appendFileSync } from 'node:fs'
export const name = 'pilot-permissions'
export const inject = ['agents', 'tools']
export function apply(ctx, config) {
  const allowed = config.allowedTools ?? ['search', 'read', 'edit', 'shell', 'test', 'evidence'].map(t => `mcp__repopilot__${t}`)
  const roleOf = agent => config.rootPreset
    ? ctx.agentPresets.composedPreset(agent.ctx) : null
  const toolsFor = agent => config.roleAllowedTools?.[roleOf(agent)] ?? allowed
  ctx.effect(() => ctx.tools.guard(exec => {
    if (config.rootPreset && (!exec.agent || !config.roleAllowedTools[roleOf(exec.agent)])) return 'Unknown role; execution denied'
    return toolsFor(exec.agent).includes(exec.name) ? undefined : 'Tool is outside this role permission scope'
  }))
  ctx.on('agent/created', async ({ agent }) => {
    if (config.rootPreset && !ctx.agentPresets.composedPreset(agent.ctx)) {
      const recorded = ctx.agentPresets.presetForSession(agent.session)
      if (recorded) await ctx.agentPresets.mount(agent.ctx, recorded)
      else await ctx.agentPresets.select(agent, config.rootPreset)
    }
    agent.ctx.tools.restrict({ allow: toolsFor(agent) })
    if (config.rosterPath) appendFileSync(config.rosterPath, JSON.stringify({
      id: agent.id, role: roleOf(agent), preset: roleOf(agent),
    }) + '\n')
  })
}
