/** Compatibility bundle for frozen SDK entrypoints; no execution loop. */
import * as Permissions from './permissions.mjs'
import * as Context from './context.mjs'
import * as Budget from './budget.mjs'
import * as Trace from './trace.mjs'
export const name = 'repopilot-policy'
export const inject = ['agents', 'tools', 'llm', 'sessionPersistence']
export function apply(ctx, config) {
  // Separate Loader rows are used by native composition. Frozen adapters keep
  // their historical entrypoint and behavior through these same modules.
  Permissions.apply(ctx, config)
  Context.apply(ctx, config)
  Budget.apply(ctx, config)
  Trace.apply(ctx, config)
}
