/* Bounded local HTTP compatibility inside the existing opaque sandbox. */
(() => {
  const pending = new Map()
  let sequence = 0
  window.fetch = async (input, init = {}) => {
    const path = typeof input === 'string' ? input : input?.url
    const method = (init.method ?? 'GET').toUpperCase()
    if (!((path === '/chat' && method === 'POST') || (path === '/health' && method === 'GET')))
      throw new Error('此试用只支持应用的 /chat 与 /health 接口。')
    let body = {}
    if (path === '/chat') {
      if (typeof init.body !== 'string' || init.body.length > 40000) throw new Error('应用请求格式或长度不正确。')
      body = JSON.parse(init.body)
    }
    const id = String(++sequence)
    return new Promise((resolve, reject) => {
      const cleanup = () => { clearTimeout(timer); pending.delete(id); init.signal?.removeEventListener('abort', abort) }
      const abort = () => { cleanup(); reject(new Error('应用请求已取消。')) }
      const timer = setTimeout(() => { cleanup(); reject(new Error('应用请求超时，请稍后重试。')) }, 150000)
      pending.set(id, value => {
        cleanup()
        resolve(new Response(JSON.stringify(value.body), { status: value.status,
          headers: { 'Content-Type': 'application/json' } }))
      })
      if (init.signal?.aborted) { abort(); return }
      init.signal?.addEventListener('abort', abort, { once: true })
      window.parent.postMessage({ type: 'pilot-demo-http', id, path, body }, '*')
    })
  }
  window.addEventListener('message', event => {
    if (event.source !== window.parent || event.data?.type !== 'pilot-demo-http-result') return
    pending.get(event.data.id)?.(event.data)
  })
})()
