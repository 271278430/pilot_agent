/** Narrow official history tools to one native root and its descendants. */
import * as NativeHistory from '@deepseek-ai/dsh-tool-session-query'
export const name = 'pilot-history'
export const inject = NativeHistory.inject

export function familyHeaders(records, caller) {
  const headers = new Map(records.map(row => [String(row.header.id), row.header]))
  headers.set(String(caller.id), caller)
  function rootOf(id) {
    const seen = new Set()
    for (;;) {
      if (seen.has(id) || !headers.has(id)) throw new Error('History lineage unavailable')
      seen.add(id)
      const parent = headers.get(id).parentSession
      if (!parent) return id
      id = String(parent)
    }
  }
  const root = rootOf(String(caller.id)), ids = new Set()
  for (const id of headers.keys()) {
    try { if (rootOf(id) === root) ids.add(id) } catch { /* unrelated incomplete family is invisible */ }
  }
  return ids
}

export async function scopedArguments(query, name, args, exec) {
  if (!exec.agent) throw new Error('History requires an official Agent')
  exec.signal?.throwIfAborted()
  const family = familyHeaders(await query.listSessions(exec.signal), exec.agent.session.header)
  const allowed = id => {
    if (!family.has(String(id))) throw new Error('History access is limited to this root Session and its experts')
  }
  const value = { ...args }
  if (name === 'session_search') {
    for (const id of value.session_ids ?? []) allowed(id)
    for (const id of value.parent_session_ids ?? []) allowed(id)
    value.session_ids ??= [...family]
  } else allowed(value.session_id ?? exec.agent.session.id)
  return value
}

export function apply(ctx, config) {
  // Explicit service facade; official schema, presentation, output, timeout,
  // workspace authorization and query implementation remain unchanged.
  NativeHistory.apply({
    agents: ctx.agents, logger: ctx.logger, sessionQuery: ctx.sessionQuery,
    systemPrompt: ctx.systemPrompt,
    tools: { register(definition) {
      return ctx.tools.register({ ...definition,
        execute: async (args, exec) => definition.execute(
          await scopedArguments(ctx.sessionQuery, definition.name, args, exec), exec),
      })
    } },
  }, config)
  ctx.systemPrompt.section({ name: 'pilot:history-isolation', order: 910,
    text: 'History is isolated to this root Session and its native experts. Earlier turns and exact events remain readable after compaction. Other user conversations are inaccessible, including those sharing a working directory.' })
}
