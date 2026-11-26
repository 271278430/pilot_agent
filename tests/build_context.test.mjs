import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

test('official context includes prepared workbench verdict instead of unapproved application planning', async () => {
  const hooks = new Map(), requests = []
  let projection, view = { build_task: { status: 'prepared' } }
  const agent = { id: 'root', session: { header: {} }, ctx: {
    effect: callback => callback(), systemPrompt: { context: spec => { projection = spec } },
  } }
  const ctx = {
    on: (event, fn) => hooks.set(event, fn), agentPresets: { composedPreset: () => 'infra-lead' },
    pilotWorkspace: {
      rootId: () => 'root', ensure: async () => view,
      request: async (...args) => { requests.push(args); return { text: 'candidate c2; business_goal=not_validated' } },
    },
  }
  const code = readFileSync(new URL('../harness/runtime/plugins/context/index.mjs', import.meta.url), 'utf8').replaceAll('export ', '')
  vm.runInNewContext(code + '\napply(ctx)', { ctx })
  await hooks.get('agent/created')({ agent })
  assert.match(projection.text(), /Prepared node software workbench/)
  assert.match(projection.text(), /business_goal=not_validated/)
  assert.doesNotMatch(projection.text(), /No task is approved|Planning only: propose/)
  assert.equal(requests[0][0], 'context')
  view = { build_task: { status: 'restored' } }
  await hooks.get('agent/pre-step')({ agent }, async () => {})
  assert.match(projection.text(), /Historical workbench review/)
  assert.doesNotMatch(projection.text(), /Prepared node software workbench/)
})
