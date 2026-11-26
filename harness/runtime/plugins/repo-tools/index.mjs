import { registerTools } from '@pilot/workspace'
export const name = 'pilot-repo-tools'
export const inject = ['pilotWorkspace', 'tools']
export function apply(ctx, config) { registerTools(ctx, config) }
