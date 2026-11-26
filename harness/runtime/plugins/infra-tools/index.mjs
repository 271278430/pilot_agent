import { registerTools } from '../workspace/index.mjs'
export const name = 'pilot-infra-tools'
export const inject = ['pilotWorkspace', 'tools', 'approval', 'sandboxPolicy']
export function apply(ctx, config) { registerTools(ctx, config) }
