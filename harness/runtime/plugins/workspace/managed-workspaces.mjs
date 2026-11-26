/** Owned directories and retry receipts; native registry and Sessions stay authoritative. */
import { createHash, randomUUID } from 'node:crypto'
import { existsSync, lstatSync, mkdirSync, readFileSync, realpathSync, renameSync, writeFileSync } from 'node:fs'
import { isAbsolute, relative, resolve } from 'node:path'
import { appliedRoot } from '../host-settings/config.mjs'

const hash = value => createHash('sha256').update(JSON.stringify(value)).digest('hex')
function directory(path) {
  if (!existsSync(path)) mkdirSync(path, { mode: 0o700 })
  if (lstatSync(path).isSymbolicLink() || !lstatSync(path).isDirectory()) throw new Error('工作区父目录必须是真实目录，不能使用符号链接')
  return realpathSync(path)
}
function atomic(path, value) {
  if (existsSync(path) && lstatSync(path).isSymbolicLink()) throw new Error('工作区回执不能是符号链接')
  const temp = path + '.' + randomUUID() + '.tmp'
  writeFileSync(temp, JSON.stringify(value), { mode: 0o600, flag: 'wx' })
  renameSync(temp, path)
}
const idOf = value => {
  if (typeof value !== 'string' || !/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/.test(value)) throw new Error('工作区操作需要固定的UUID请求编号')
  return value
}
function nameOf(value) {
  if (typeof value !== 'string') throw new Error('请填写工作区名称')
  const name = value.trim()
  if (!name || name.length > 80 || /[\x00-\x1f\x7f/\\]/.test(name) || /^\.+$/.test(name)) throw new Error('名称需为1–80个字符，不能包含路径分隔符或控制字符')
  return name
}
export class ManagedWorkspaces {
  constructor(ctx, root) { this.ctx = ctx; this.root = root; this.pending = new Map() }
  base() {
    const root = realpathSync(this.root)
    const runs = directory(resolve(root, 'runs'))
    return directory(resolve(runs, 'harness-inbox'))
  }
  owned(path) {
    if(this.ctx.workspaceRegistry.list().some(w=>w.path===path))return true
    return this.scoped(path)
  }
  scoped(path) {
    return [this.base(),appliedRoot(this.root)].filter(Boolean).some(root=>{
      const part = relative(root, path)
      return !!part && !isAbsolute(part) && part !== '..' && !part.startsWith('..' + (process.platform === 'win32' ? '\\' : '/'))
    })
  }
  catalog() {
    const root = this.base()
    return { root, workspaces: this.ctx.workspaceRegistry.list().filter(w => this.scoped(w.path)).map(w => {
      let available = false
      try { available = this.owned(realpathSync(w.path)) && !lstatSync(w.path).isSymbolicLink() && lstatSync(w.path).isDirectory() } catch { /* A missing path remains visible, without recreation. */ }
      return { id: String(w.id), title: w.title, path: w.path, sessions: w.sessionIds.length, available }
    }) }
  }
  create(args) { return this.run('create', args) }
  session(args) { return this.run('session', args) }
  async run(kind, args) {
    if (!args || typeof args !== 'object' || Array.isArray(args)) throw new Error('工作区操作需要对象参数')
    const requestId = idOf(args.request_id)
    const keys = kind === 'create' ? ['request_id', 'title'] : ['request_id', 'workspace_id']
    if (Object.keys(args).some(k => !keys.includes(k))) throw new Error('工作区操作不接受自定义路径、权限或执行参数')
    const input = kind === 'create' ? { kind, title: nameOf(args.title) } : { kind, workspace_id: args.workspace_id }
    if (kind === 'session' && (typeof input.workspace_id !== 'string' || input.workspace_id.length > 200)) throw new Error('请选择已登记的工作区')
    const digest = hash(input), pending = this.pending.get(requestId)
    if (pending) {
      if (pending.digest !== digest) throw new Error('请求编号对应不同的工作区操作')
      return pending.promise
    }
    const promise = this.perform(requestId, input, digest)
    this.pending.set(requestId, { digest, promise })
    try { return await promise } finally { this.pending.delete(requestId) }
  }
  async perform(requestId, input, digest) {
    const root = realpathSync(this.root), local = directory(resolve(root, '.local'))
    const receipts = directory(resolve(local, 'workspace-requests')), file = resolve(receipts, requestId + '.json')
    const base = this.base()
    const slug = input.kind === 'create' ? input.title.normalize('NFKC').replace(/[^\p{L}\p{N}_-]+/gu, '-').slice(0, 32) : ''
    const expectedPath = input.kind === 'create' ? resolve(base, 'pilot-' + slug + '-' + requestId) : null
    let receipt
    if (existsSync(file)) {
      if (lstatSync(file).isSymbolicLink()) throw new Error('工作区回执身份异常')
      receipt = JSON.parse(readFileSync(file, 'utf8'))
      if (receipt.digest !== digest) throw new Error('请求编号对应不同的工作区操作')
      if (receipt.path !== expectedPath || !['preparing','completed'].includes(receipt.phase) || typeof receipt.sessionId !== 'string') throw new Error('工作区操作回执身份异常')
    } else {
      receipt = { digest, input, phase: 'preparing', sessionId: 'session-workspace-' + requestId,
        path: expectedPath }
      atomic(file, receipt)
    }
    let workspace
    if (input.kind === 'create') {
      if (!this.owned(receipt.path)) throw new Error('工作区路径越界')
      const marker = resolve(receipt.path, '.pilot-workspace.json')
      if (!existsSync(receipt.path)) {
        if (receipt.phase === 'completed') throw new Error('原工作区目录已移除，请另建工作区')
        mkdirSync(receipt.path, { mode: 0o700 })
        atomic(marker, { requestId, digest })
      } else {
        if (!lstatSync(receipt.path).isDirectory() || lstatSync(receipt.path).isSymbolicLink() || !existsSync(marker) || lstatSync(marker).isSymbolicLink()) throw new Error('已有目录不属于此创建操作，不能接管')
        const owner = JSON.parse(readFileSync(marker, 'utf8'))
        if (owner.digest !== digest || owner.requestId !== requestId) throw new Error('已有目录不属于此创建操作，不能接管')
      }
      workspace = await this.ctx.workspaceRegistry.create(receipt.path, input.title)
    } else {
      workspace = this.ctx.workspaceRegistry.get(input.workspace_id)
      if (!workspace || !this.owned(workspace.path) || !this.owned(realpathSync(workspace.path)) || lstatSync(workspace.path).isSymbolicLink()) throw new Error('请选择Pilot需求工作区中的有效目录')
    }
    const found = await this.ctx.sessionController.resolveAgent(receipt.sessionId)
    let agent
    if ('error' in found) {
      if (found.error.code !== 'session/not-found' || receipt.phase === 'completed') throw found.error
      try {
        const created = await this.ctx.sessionController.create({ workspaceId: workspace.id, sessionId: receipt.sessionId, agentPreset: 'infra-lead' })
        receipt.sessionId = String(created.sessionId)
      } catch (error) {
        // Native create can commit a Session before attachment fails. Preserve
        // its actual identity; retry repairs attachment instead of creating again.
        if (error.code === 'session/workspace-attach-failed' && error.details?.workspaceId === workspace.id && typeof error.details?.sessionId === 'string') {
          receipt.sessionId = error.details.sessionId
          atomic(file, receipt)
        }
        throw error
      }
      atomic(file, receipt)
      const resolved = await this.ctx.sessionController.resolveAgent(receipt.sessionId)
      if ('error' in resolved) throw resolved.error
      agent = resolved.agent
    } else agent = found.agent
    if (agent.session.header.parentSession || relative(realpathSync(agent.session.header.cwd), workspace.path) !== '') throw new Error('原会话不属于这个工作区，不能重新绑定')
    if (!workspace.sessionIds.includes(agent.id)) await workspace.attachSession(agent.id)
    receipt.phase = 'completed'; receipt.workspaceId = String(workspace.id); atomic(file, receipt)
    return { workspace: { id: String(workspace.id), title: workspace.title, path: workspace.path }, sessionId: receipt.sessionId }
  }
}
