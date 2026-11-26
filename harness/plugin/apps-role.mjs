/** Scoped persona only; official Preset owns tool and Skill composition. */
export const name = 'apps-role'
export const inject = ['systemPrompt']
export function apply(ctx, config) {
  ctx.effect(() => ctx.systemPrompt.section({
    name: 'apps:role', order: 20, text: config.prompt,
  }))
}
