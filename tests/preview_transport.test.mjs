import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import vm from 'node:vm'
import test from 'node:test'

function fixture() {
  const sent = [], callbacks = new Map(), timers = new Map(), parent = { postMessage: value => sent.push(value) }
  const window = { parent, addEventListener: (name, callback) => callbacks.set(name, callback) }
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/delivery-ui/preview-transport.js', import.meta.url), 'utf8'), {
    window, Response, setTimeout: callback => { const id = timers.size + 1; timers.set(id, callback); return id }, clearTimeout: id => timers.delete(id),
  })
  return { window, sent, timers, respond: (body, status = 200, source = parent) => callbacks.get('message')({ source,
    data: { type: 'pilot-demo-http-result', id: sent.at(-1).id, body, status } }) }
}

test('local chat fetch preserves profile/history and returns a real HTTP-shaped response', async () => {
  const f = fixture(), body = { message: '讲讲二次函数', profile: { grade: '高二' }, history: [{ role: 'user', content: '你好' }] }
  const pending = f.window.fetch('/chat', { method: 'POST', body: JSON.stringify(body) })
  assert.deepEqual(JSON.parse(JSON.stringify(f.sent[0].body)), body)
  f.respond({ answer: '真实答案' }, 200)
  const response = await pending
  assert.equal(response.ok, true)
  assert.deepEqual(await response.json(), { answer: '真实答案' })
  assert.equal(f.timers.size, 0)
})

test('transport only admits registered local routes and trusted parent responses', async () => {
  const f = fixture()
  await assert.rejects(f.window.fetch('https://example.com/chat'), /只支持/)
  await assert.rejects(f.window.fetch('/chat'), /只支持/)
  assert.equal(f.sent.length, 0)
  const request = f.window.fetch('/health')
  let finished = false; request.then(() => { finished = true })
  f.respond({ ok: true }, 200, {})
  await Promise.resolve(); assert.equal(finished, false)
  f.respond({ ok: true })
  assert.deepEqual(await (await request).json(), { ok: true })
})

test('domain rejection is a JSON error response and aborted requests do not post', async () => {
  const f = fixture(), abort = new AbortController()
  abort.abort()
  await assert.rejects(f.window.fetch('/health', { signal: abort.signal }), /已取消/)
  assert.equal(f.sent.length, 0)
  const request = f.window.fetch('/chat', { method: 'POST', body: '{"message":"hello"}' })
  f.respond({ error: '版本已更新' }, 400)
  const response = await request
  assert.equal(response.ok, false)
  assert.equal(response.status, 400)
  assert.deepEqual(await response.json(), { error: '版本已更新' })
})
