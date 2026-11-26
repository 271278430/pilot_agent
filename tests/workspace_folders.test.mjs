import assert from 'node:assert/strict'
import { randomUUID } from 'node:crypto'
import { existsSync, mkdtempSync, readFileSync, readdirSync, realpathSync, rmSync, symlinkSync, writeFileSync, mkdirSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { join } from 'node:path'
import test from 'node:test'
import { ManagedWorkspaces } from '../harness/runtime/plugins/workspace/managed-workspaces.mjs'

function fixture(t) {
  const root=mkdtempSync(join(tmpdir(),'pilot-workspace-'));t.after(()=>rmSync(root,{recursive:true,force:true}))
  const rows=new Map(),agents=new Map();let creates=0,attachments=0,resolveError,attachFailure=false,differentId=false
  const ctx={workspaceRegistry:{list:()=>[...rows.values()],get:id=>rows.get(id),create:async(path,title)=>{
    const canonical=realpathSync(path),old=[...rows.values()].find(w=>w.path===canonical);if(old)return old
    const row={id:randomUUID(),path:canonical,title,sessionIds:[],attachSession:async id=>{attachments++;if(!row.sessionIds.includes(id))row.sessionIds.push(id)}};rows.set(row.id,row);return row
  }},sessionController:{resolveAgent:async id=>resolveError?{error:resolveError}:agents.has(id)?{agent:agents.get(id)}:{error:Object.assign(new Error('missing'),{code:'session/not-found'})},create:async({workspaceId,sessionId})=>{
    creates++;const w=rows.get(workspaceId),id=differentId?randomUUID():sessionId
    agents.set(id,{id,session:{header:{cwd:w.path}}})
    if(attachFailure){attachFailure=false;throw Object.assign(new Error('attach failed'),{code:'session/workspace-attach-failed',details:{sessionId:id,workspaceId}})}
    await w.attachSession(id);return {sessionId:id}
  }}}
  return {root,ctx,rows,agents,manager:new ManagedWorkspaces(ctx,root),stats:()=>({creates,attachments}),failResolve:e=>resolveError=e,failAttach:()=>{attachFailure=true;differentId=true}}
}

test('named directory and native binding are idempotent, including cold restore',async t=>{
  const f=fixture(t),args={title:'模型环境 验证',request_id:randomUUID()}
  const [a,b]=await Promise.all([f.manager.create(args),f.manager.create(args)])
  assert.deepEqual(a,b);assert.equal(f.stats().creates,1);assert.ok(existsSync(a.workspace.path));assert.match(a.workspace.path,/pilot-模型环境-验证-/)
  const cold=new ManagedWorkspaces(f.ctx,f.root);assert.deepEqual(await cold.create(args),a);assert.equal(f.stats().creates,1)
  assert.equal(cold.catalog().workspaces[0].sessions,1)
  await assert.rejects(cold.create({...args,title:'different'}),/不同/)
  const c=await cold.session({workspace_id:a.workspace.id,request_id:randomUUID()})
  assert.notEqual(a.sessionId,c.sessionId);assert.equal(a.workspace.path,c.workspace.path);assert.equal(f.stats().creates,2)
  assert.equal(readdirSync(join(f.root,'runs/harness-inbox')).length,1)
})

test('invalid paths and parameter injection never create a workspace',async t=>{
  const f=fixture(t)
  for(const title of ['../escape','C:\\escape','..','a\nname','', 'x'.repeat(81)])await assert.rejects(f.manager.create({title,request_id:randomUUID()}))
  await assert.rejects(f.manager.create({title:'ok',request_id:randomUUID(),path:'/tmp/escape'}),/参数/)
  await assert.rejects(f.manager.create(null),/对象/)
  await assert.rejects(f.manager.create({title:'ok',request_id:'../../escape'}),/UUID/)
  assert.equal(f.rows.size,0);assert.equal(f.stats().creates,0)
})

test('missing completed directory and session are never recreated',async t=>{
  const f=fixture(t),args={title:'删除验证',request_id:randomUUID()},a=await f.manager.create(args)
  f.agents.delete(a.sessionId);await assert.rejects(f.manager.create(args),/missing/);assert.equal(f.stats().creates,1)
  rmSync(a.workspace.path,{recursive:true});assert.equal(f.manager.catalog().workspaces[0].available,false)
  await assert.rejects(f.manager.create(args),/已移除/);assert.ok(!existsSync(a.workspace.path))
})

test('native partial attachment preserves the actual returned session identity',async t=>{
  const f=fixture(t),args={title:'恢复验证',request_id:randomUUID()};f.failAttach()
  await assert.rejects(f.manager.create(args),/attach failed/)
  const a=await new ManagedWorkspaces(f.ctx,f.root).create(args)
  assert.equal(f.stats().creates,1);assert.ok(f.rows.get(a.workspace.id).sessionIds.includes(a.sessionId))
  assert.ok(!a.sessionId.startsWith('session-workspace-'))
})

test('unexpected native restore failure cannot fall back to new create',async t=>{
  const f=fixture(t);f.failResolve(Object.assign(new Error('storage unavailable'),{code:'session/restore-failed'}))
  await assert.rejects(f.manager.create({title:'失败验证',request_id:randomUUID()}),/storage unavailable/)
  assert.equal(f.stats().creates,0)
})

test('changed marker, parent Session and foreign cwd cannot be rebound',async t=>{
  const f=fixture(t),args={title:'归属验证',request_id:randomUUID()},a=await f.manager.create(args),agent=f.agents.get(a.sessionId)
  agent.session.header.parentSession='foreign';await assert.rejects(f.manager.create(args),/重新绑定/);delete agent.session.header.parentSession
  agent.session.header.cwd=f.root;await assert.rejects(f.manager.create(args),/重新绑定/);agent.session.header.cwd=a.workspace.path
  writeFileSync(join(a.workspace.path,'.pilot-workspace.json'),JSON.stringify({requestId:randomUUID(),digest:JSON.parse(readFileSync(join(a.workspace.path,'.pilot-workspace.json'))).digest}))
  await assert.rejects(f.manager.create(args),/不能接管/)
})

test('symlink parent or replaced workspace is refused without touching its target',async t=>{
  const f=fixture(t),outside=join(f.root,'outside');mkdirSync(outside)
  symlinkSync(outside,join(f.root,'runs'),process.platform==='win32'?'junction':'dir')
  assert.throws(()=>f.manager.base(),/符号链接/);assert.deepEqual(readdirSync(outside),[])
  rmSync(join(f.root,'runs'));const a=await f.manager.create({title:'符号链接',request_id:randomUUID()})
  rmSync(a.workspace.path,{recursive:true});symlinkSync(outside,a.workspace.path,process.platform==='win32'?'junction':'dir')
  assert.equal(f.manager.catalog().workspaces[0].available,false)
  await assert.rejects(f.manager.session({workspace_id:a.workspace.id,request_id:randomUUID()}),/有效目录/)
  assert.equal(f.stats().creates,1);assert.deepEqual(readdirSync(outside),[])
})

test('catalog excludes the inbox itself and all foreign native workspaces',async t=>{
  const f=fixture(t);await f.ctx.workspaceRegistry.create(f.root,'project');await f.ctx.workspaceRegistry.create(f.manager.base(),'inbox')
  assert.equal(f.manager.catalog().workspaces.length,0)
})
