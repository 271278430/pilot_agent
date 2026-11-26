/** Shared quota admission under real async-generator ordering; no model calls. */
import assert from 'node:assert/strict'
import { mkdtempSync, readFileSync, writeFileSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { apply } from '../harness/plugin/budget.mjs'

function fixture(t, { max = 200, initial, estimate = () => 110, resolveBudget } = {}) {
  const root = mkdtempSync(join(tmpdir(), 'pilot-budget-'))
  t.after(() => rmSync(root, { recursive: true, force: true }))
  const meterPath = join(root, 'meter.json'), checkpointPath = join(root, 'checkpoint.json'), hooks = {}
  writeFileSync(checkpointPath, '{}')
  if (initial) writeFileSync(meterPath, JSON.stringify({ steps: 0, calls: 0, input_tokens: 0, output_tokens: 0, cost: 0, estimated: false, stop_reason: null, ...initial }))
  apply({ on: (name, hook) => { hooks[name] = hook } }, {
    checkpointPath, meterPath, budget: { max_tokens: max, max_steps: 20, output_tokens: 10, max_cost: 5, input_price: 0, output_price: 0 },
    measureRequest: options => ({ tokens: estimate(options), source: 'official_token_meter_usage' }),
    resolveBudget,
    errorFactory: (reason, detail) => Object.assign(new Error(reason), { code: 'PILOT_BUDGET_LIMIT', detail }),
  })
  return { stream: (next, options = {}) => hooks['llm/stream'](options, next),
    step: () => hooks['agent/pre-step']({ agent: {} }, () => 'continue'),
    meter: () => JSON.parse(readFileSync(meterPath, 'utf8')) }
}
const usage = (input = 10, output = 10) => ({ type: 'usage', usage: { inputTokens: input, outputTokens: output } })
const consume = async stream => { for await (const _ of stream) { /* Settle every chunk. */ } }
const turn = async () => { for (let i = 0; i < 5; i++) await Promise.resolve() }

test('official measured pressure admits Unicode requests whose byte size is much larger', async t => {
  const f = fixture(t)
  await consume(f.stream(async function* () { yield usage() }, { messages: [{ content: '课程'.repeat(4000) }] }))
  assert.equal(f.meter().calls, 1)
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 20)
  assert.match(f.meter().last_reservation.source, /^official_/)
})

test('reported usage replaces the in-flight reservation before the generator closes', async t => {
  const f = fixture(t, { estimate: () => 90 })
  const first = f.stream(async function* () { yield usage(90, 10) })
  await first.next() // first request has spent 100, but its finally has not run
  await consume(f.stream(async function* () { yield usage(90, 10) }))
  await first.next()
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 200)
  assert.equal(f.meter().stop_reason, null)
})

test('temporary contention waits for settlement instead of stopping the whole family', async t => {
  const f = fixture(t)
  const first = f.stream(async function* () { yield usage() })
  await first.next()
  let reached = false
  const second = consume(f.stream(async function* () { reached = true; yield usage() }))
  await turn(); assert.equal(reached, false)
  assert.equal(f.meter().stop_reason, null)
  await first.next(); await second
  assert.equal(reached, true)
  assert.equal(f.meter().calls, 2)
})

test('cancelling a waiting request neither calls the provider nor charges tokens', async t => {
  const f = fixture(t), abort = new AbortController()
  const first = f.stream(async function* () { yield usage() })
  await first.next()
  const second = consume(f.stream(async function* () { assert.fail('provider must not run') }, { signal: abort.signal }))
  abort.abort(new Error('cancelled'))
  await assert.rejects(second, /cancelled/)
  await first.next()
  assert.equal(f.meter().calls, 1)
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 20)
})

test('repeated cumulative usage chunks are not charged twice', async t => {
  const f = fixture(t)
  await consume(f.stream(async function* () { yield usage(); yield usage(); yield usage(12, 15) }))
  assert.equal(f.meter().input_tokens, 12)
  assert.equal(f.meter().output_tokens, 15)
})

test('a stopped reservation is re-evaluated on resume without erasing actual usage', async t => {
  let size = 110
  const f = fixture(t, { initial: { input_tokens: 90, output_tokens: 10 }, estimate: () => size })
  await assert.rejects(consume(f.stream(async function* () { assert.fail('refused') })), { code: 'PILOT_BUDGET_LIMIT' })
  size = 30
  assert.equal(await f.step(), 'continue')
  await consume(f.stream(async function* () { yield usage() }))
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 120)
  assert.equal(f.meter().previous_stop.reason, 'token_budget_reservation_exhausted')
})

test('actual exhausted quota remains blocked in a later activation', async t => {
  const f = fixture(t, { initial: { input_tokens: 190, output_tokens: 10, stop_reason: 'token_budget_reservation_exhausted' } })
  assert.equal((await f.step()).kind, 'reject')
  assert.equal(f.meter().stop_reason, 'token_budget_exhausted')
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 200)
})

test('an owner limit change restores admission but retains cumulative usage', async t => {
  let max_tokens = 200
  const f = fixture(t, { initial: { input_tokens: 200, output_tokens: 0 }, resolveBudget: () => ({ max_tokens }) })
  assert.equal((await f.step()).kind, 'reject')
  max_tokens = 400
  assert.equal(await f.step(), 'continue')
  await consume(f.stream(async function* () { yield usage() }))
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 220)
  assert.equal(f.meter().previous_stop.reason, 'token_budget_exhausted')
})

test('optional cumulative ceiling can be disabled without disabling step protection or accounting', async t => {
  const f = fixture(t, { max: null, initial: { input_tokens: 900000, output_tokens: 0 } })
  assert.equal(await f.step(), 'continue')
  await consume(f.stream(async function* () { yield usage(50, 20) }))
  assert.equal(f.meter().input_tokens + f.meter().output_tokens, 900070)
  for (let i = 1; i < 20; i++) await f.step()
  assert.equal((await f.step()).kind, 'reject')
  assert.equal(f.meter().stop_reason, 'step_budget_exhausted')
})
