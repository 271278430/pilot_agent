import assert from 'node:assert/strict'
import { readFileSync } from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import * as Intent from '../harness/plugin/intent.mjs'

async function fixture() {
  const hooks=new Map(),agents=new Map(),root={id:'root',ctx:{},session:{header:{}},status:'running'}
  const task={id:'task',state:{status:'running',session_id:'root',strategy:{mode:'multi',experts:['infra-expert','repo-expert']}}}
  let children=[],guard
  agents.set('root',root)
  const ctx={on:(name,fn)=>{const rows=hooks.get(name)??[];rows.push(fn);hooks.set(name,rows)},effect:fn=>fn(),
    tools:{guard:fn=>{guard=fn}},agents:{get:id=>agents.get(id)},subagents:{listChildren:async()=>children},
    agentPresets:{composedPreset:scope=>scope?.role??'infra-lead'},workingDirectory:{get:()=>'/approved'},
    pilotWorkspace:{rootId:()=> 'root',ensure:async()=>({task})}}
  const source=readFileSync(new URL('../harness/runtime/plugins/policy/index.mjs',import.meta.url),'utf8').replace(/^import .*$/gm,'').replaceAll('export ','')
  vm.runInNewContext(source+'\napply(ctx,{root:"/fixture",budget:{},allowed:{"infra-lead":["infra_specialist","repo_specialist"]}})',{
    ctx,Intent,Budget:{apply:(ctx)=>ctx.on('agent/pre-step',(_e,next)=>next())},LlmError:Error,
    mkdirSync(){},writeFileSync(){},existsSync:()=>true,readFileSync:()=> '{}',resolve:(...parts)=>parts.join('/'),isAgentLoopRequest:()=>false,
  })
  await hooks.get('agent/pre-step')[0]({agent:root,turn:1,messages:[]},async()=>({kind:'continue'}))
  const execute=(name,token={})=>({name,token,agent:root,arguments:{},signal:new AbortController().signal})
  const pre=hooks.get('tools/pre-execute')[0]
  return {root,task,guard,pre,execute,agents,setChildren:value=>{children=value},result:exec=>{for(const hook of hooks.get('tools/result'))hook(exec,{isError:false})}}
}

test('actual active native child blocks another role, then permits sequential admission',async()=>{
  const f=await fixture();f.setChildren([{id:'infra-child'}]);f.agents.set('infra-child',{status:'running'})
  assert.equal((await f.pre(f.execute('repo_specialist'),async()=>({kind:'allow'}))).kind,'deny')
  f.agents.get('infra-child').status='idle'
  const exec=f.execute('repo_specialist')
  assert.equal((await f.pre(exec,async()=>({kind:'allow'}))).kind,'allow');f.result(exec)
  assert.equal(f.guard(exec),undefined)
})

test('parallel expert creation is denied until the original official result settles',async()=>{
  const f=await fixture(),first=f.execute('infra_specialist'),second=f.execute('repo_specialist')
  assert.equal((await f.pre(first,async()=>({kind:'allow'}))).kind,'allow')
  assert.equal((await f.pre(second,async()=>({kind:'allow'}))).kind,'deny')
  f.result(first)
  assert.equal((await f.pre(second,async()=>({kind:'allow'}))).kind,'allow');f.result(second)
})

test('a failed pre-execute policy releases the admission claim for a corrected attempt',async()=>{
  const f=await fixture()
  await assert.rejects(f.pre(f.execute('infra_specialist'),async()=>{throw new Error('cancelled')}),/cancelled/)
  const exec=f.execute('repo_specialist')
  assert.equal((await f.pre(exec,async()=>({kind:'allow'}))).kind,'allow');f.result(exec)
})

test('resolved routing cannot revive a former expert or permit its next source mutation',async()=>{
  const f=await fixture();f.task.state.strategy.experts=[]
  f.agents.set('old-repo',{ctx:{role:'repo-expert'},status:'idle'})
  const send=f.execute('send_message');send.arguments={agent_id:'old-repo'}
  assert.match(f.guard(send),/not admitted/)
  f.root.ctx.role='repo-expert'
  assert.match(f.guard(f.execute('pilot_infra_source_write')),/no longer admitted/)
})

test('continuation reuses an expert but cannot wake another while a sibling is active',async()=>{
  const f=await fixture(),infra={id:'infra',ctx:{role:'infra-expert'},status:'running'},repo={id:'repo',ctx:{role:'repo-expert'},status:'idle'}
  f.agents.set('infra',infra);f.agents.set('repo',repo);f.setChildren([{id:'infra'},{id:'repo'}])
  const other=f.execute('send_message');other.arguments={agent_id:'repo'}
  assert.equal((await f.pre(other,async()=>({kind:'allow'}))).kind,'deny')
  const same=f.execute('send_message');same.arguments={agent_id:'infra'}
  assert.equal((await f.pre(same,async()=>({kind:'allow'}))).kind,'allow');f.result(same)
})
