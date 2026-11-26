/** RepoPilot policy plugin. The upstream Harness owns the agent loop. */
import { readFileSync, writeFileSync, renameSync, existsSync } from 'node:fs'

export const name = 'pilot-budget'
export const inject = ['agents', 'llm']

export function apply(ctx, config) {
  const meterPath = config.meterPath
  let budget = config.budget
  const refreshBudget = () => {
    budget = { ...config.budget, ...(config.resolveBudget?.() ?? {}) }
    if (budget.max_tokens != null && (!Number.isSafeInteger(budget.max_tokens) || budget.max_tokens < 1))
      throw new Error('Invalid cumulative token limit')
  }
  // One Host policy meters all official Agents. Pending streams reserve budget
  // before awaiting the provider, including overlap between lead and children.
  let pendingTokens = 0
  let pendingCost = 0
  const waiters = new Set()
  const wake = () => { for (const waiter of [...waiters]) waiter() }
  const waitForSettlement = signal => new Promise((done, fail) => {
    const cleanup = () => { waiters.delete(released); signal?.removeEventListener('abort', aborted) }
    const released = () => { cleanup(); done() }
    const aborted = () => { cleanup(); fail(signal.reason) }
    waiters.add(released)
    if (signal?.aborted) aborted()
    else signal?.addEventListener('abort', aborted, { once: true })
  })
  const globalRead = () => config.aggregateMeterPath && existsSync(config.aggregateMeterPath)
    ? JSON.parse(readFileSync(config.aggregateMeterPath, 'utf8')) : null
  const globalCharge = delta => {
    const aggregate = globalRead()
    if (!aggregate) return
    for (const [key, value] of Object.entries(delta)) {
      if (key === 'estimated') aggregate.estimated ||= value
      else aggregate[key] = (aggregate[key] ?? 0) + value
    }
    writeFileSync(config.aggregateMeterPath + '.tmp', JSON.stringify(aggregate))
    renameSync(config.aggregateMeterPath + '.tmp', config.aggregateMeterPath)
  }
  let meter = existsSync(meterPath)
    ? JSON.parse(readFileSync(meterPath, 'utf8'))
    : { steps: 0, calls: 0, input_tokens: 0, output_tokens: 0, cost: 0, estimated: false, stop_reason: null }
  const save = () => {
    writeFileSync(meterPath + '.tmp', JSON.stringify(meter, null, 2))
    renameSync(meterPath + '.tmp', meterPath)
  }
  const halt = (reason, detail) => {
    meter.stop_reason = reason
    meter.last_stop = { reason, time: Date.now(), used_tokens: meter.input_tokens + meter.output_tokens,
      max_tokens: budget.max_tokens, ...detail }
    save()
  }
  const failure = () => config.errorFactory
    ? config.errorFactory(meter.stop_reason, meter.last_stop)
    : new Error(meter.stop_reason)
  ctx.on('agent/pre-step', async ({ agent }, next) => {
    refreshBudget()
    // sdk-minimal's official session-checkpoint-policy owns durability before
    // model requests, tool side effects and subsequent steps.
    // A refused reservation did not consume provider tokens. Re-evaluate it
    // in a later official activation; never erase actual usage or hard limits.
    if (config.measureRequest && (meter.stop_reason?.endsWith('_reservation_exhausted')
      || meter.stop_reason === 'token_budget_exhausted' && (budget.max_tokens == null
        || meter.input_tokens + meter.output_tokens < budget.max_tokens))) {
      meter.previous_stop = meter.last_stop ?? { reason: meter.stop_reason }
      meter.stop_reason = null
    }
    const checkpoint = JSON.parse(readFileSync(config.checkpointPath, 'utf8'))
    if (config.taskStatusPath && existsSync(config.taskStatusPath)
      && JSON.parse(readFileSync(config.taskStatusPath, 'utf8')).status !== 'running') halt('application_not_running')
    if (checkpoint.halt_reason) halt(checkpoint.halt_reason)
    if (meter.steps >= budget.max_steps) halt('step_budget_exhausted')
    const aggregate = globalRead()
    if (aggregate && aggregate.steps >= budget.max_steps) halt('release_step_budget_exhausted')
    if (config.deadlineEpochMs && Date.now() >= config.deadlineEpochMs) halt('release_wall_budget_exhausted')
    if (budget.max_tokens != null && meter.input_tokens + meter.output_tokens >= budget.max_tokens) halt('token_budget_exhausted')
    if (meter.cost >= budget.max_cost) halt('cost_budget_exhausted')
    if (meter.stop_reason) { save(); return { kind: 'reject' } }
    meter.steps++
    globalCharge({ steps: 1 })
    save()
    return next()
  })
  ctx.on('llm/stream', async function* (options, next) {
    refreshBudget()
    if (meter.stop_reason) throw failure()
    // Current Host supplies the official replay-aware token meter. Retain the
    // old byte-bound mode solely for frozen SDK entrypoints and their reports.
    const measured = config.measureRequest?.(options)
    const promptBound = measured?.tokens ?? Buffer.byteLength(JSON.stringify({ messages: options.messages ?? [], tools: options.tools ?? [] }), 'utf8')
    if (!Number.isSafeInteger(promptBound) || promptBound < 0) throw new Error('Invalid request token measurement')
    const outputBound = options.maxTokens ?? budget.output_tokens
    const reservedCost = (promptBound * budget.input_price + outputBound * budget.output_price) / 1e6
    for (;;) {
      options.signal?.throwIfAborted()
      const aggregate = globalRead()
      let reason
      if (budget.max_tokens != null && aggregate && aggregate.input_tokens + aggregate.output_tokens + pendingTokens + promptBound + outputBound > budget.max_tokens)
        reason = 'release_token_budget_reservation_exhausted'
      else if (budget.max_tokens != null && meter.input_tokens + meter.output_tokens + pendingTokens + promptBound + outputBound > budget.max_tokens)
        reason = 'token_budget_reservation_exhausted'
      else if (meter.cost + pendingCost + reservedCost > budget.max_cost)
        reason = 'cost_budget_reservation_exhausted'
      if (!reason) break
      // Contention is temporary. Let official in-flight streams settle rather
      // than persistently stopping the whole family because they overlap.
      if (config.measureRequest && (pendingTokens > 0 || pendingCost > 0)) {
        await waitForSettlement(options.signal)
        continue
      }
      halt(reason, { used_tokens: meter.input_tokens + meter.output_tokens,
        input_reservation: promptBound, output_reservation: outputBound,
        measurement: measured?.source ?? 'legacy_byte_bound' })
      throw failure()
    }
    meter.calls++
    if (measured) meter.last_reservation = { input_tokens: promptBound, output_tokens: outputBound,
      source: measured.source, used_tokens: meter.input_tokens + meter.output_tokens, pending_tokens: pendingTokens }
    globalCharge({ calls: 1 })
    pendingTokens += promptBound + outputBound
    pendingCost += reservedCost
    let remainingTokens = promptBound + outputBound, remainingCost = reservedCost
    let chargedInput = 0, chargedOutput = 0
    let usageSeen = false
    try {
    for await (const chunk of next()) {
      if (chunk.type === 'usage') {
        usageSeen = true
        const u = chunk.usage
        const input = u.inputTokens + (u.cacheReadTokens ?? 0) + (u.cacheWriteTokens ?? 0)
        const inputDelta = Math.max(0, input - chargedInput), outputDelta = Math.max(0, u.outputTokens - chargedOutput)
        chargedInput += inputDelta; chargedOutput += outputDelta
        const outstandingTokens = Math.max(0, promptBound + outputBound - chargedInput - chargedOutput)
        const outstandingCost = Math.max(0, reservedCost - (chargedInput * budget.input_price + chargedOutput * budget.output_price) / 1e6)
        // Actual usage replaces its reservation; it must not count twice while
        // a sibling is admitted before this generator's finally runs.
        pendingTokens -= remainingTokens - outstandingTokens; remainingTokens = outstandingTokens
        pendingCost -= remainingCost - outstandingCost; remainingCost = outstandingCost
        meter.input_tokens += inputDelta
        meter.output_tokens += outputDelta
        globalCharge({ input_tokens: inputDelta, output_tokens: outputDelta })
        meter.cost += (inputDelta * budget.input_price + outputDelta * budget.output_price) / 1e6
        save()
        wake()
      }
      yield chunk
    }
    } finally {
    if (!usageSeen) {
      meter.estimated = true
      globalCharge({ input_tokens: promptBound, output_tokens: outputBound, estimated: true })
      meter.input_tokens += promptBound
      meter.output_tokens += outputBound
      meter.cost += reservedCost
      save()
    }
      pendingTokens -= remainingTokens
      pendingCost -= remainingCost
      wake()
    }
  })
  save()
}
