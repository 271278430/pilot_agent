/** Read-only projection of official Session events for domain evidence. */
import { appendFileSync } from 'node:fs'
export const name = 'pilot-trace'
export const inject = ['sessions']
export function apply(ctx, config) {
  ctx.on('session/event', (session, event) => {
    appendFileSync(config.nativeTracePath, JSON.stringify({ session_id: session.id, event }) + '\n')
  })
}
