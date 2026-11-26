import { registerTools } from '@pilot/workspace'
export const name = 'pilot-model-assets'
export const inject = ['pilotWorkspace', 'tools']
export function apply(ctx, config) { registerTools(ctx, config) }
