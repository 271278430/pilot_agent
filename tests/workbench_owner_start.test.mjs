import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

function fixture({status='prepared',active=null,mode='review',agentStatus='idle',childStatus='idle'}={}) {
  let handler
  const calls=[], prompts=[], task={id:'a'.repeat(32),session_id:'root',status,active_operation:active,plan:{title:'Software goal'}}
  const agent={id:'root',status:agentStatus,session:{},ctx:{}}
  const ctx={effect:fn=>fn(),on(){},connection:{rpc:{handle:(_path,fn)=>{handler=fn}}},
    pilotWorkspace:{request:async(op,id,role,args)=>{calls.push({op,id,role,args});if(op==='infra-workbench-catalog')return {tasks:[task]};if(op==='infra-network-run')return {...task,status:'failed',restored:true};if(op==='infra-workbench-create')return task;return task}},
    sessionController:{resolveAgent:async()=>({agent}),prompt:async args=>{prompts.push(args)},create:async()=>{throw new Error('Duplicate Session')}},
    agents:{get:()=>({status:childStatus})},subagents:{listChildren:async()=>[{id:'child'}]},agentPresets:{composedPreset:()=> 'infra-lead'},
    sandboxPolicy:{resolve:()=>({mode:mode==='observe'?'read-only':'review'})}}
  const code=readFileSync(new URL('../harness/runtime/plugins/infra-domain/index.mjs',import.meta.url),'utf8').replace(/^import .*$/mg,'').replaceAll('export ','')
  vm.runInNewContext(code+'\napply(ctx)',{ctx,SessionId:id=>id,crypto:globalThis.crypto,AbortController,setInterval:()=>({unref(){}}),clearInterval(){}})
  return {calls,prompts,invoke:endpoint=>handler(endpoint,{task_id:task.id},{},{principal:{kind:'owner'}})}
}
test('Owner explicitly starts only the existing prepared native root',async()=>{
  const f=fixture(),r=await f.invoke('workbench-agent')
  assert.equal(r.ok,true);assert.equal(r.value.sessionId,'root');assert.equal(f.prompts.length,1)
  assert.equal(f.prompts[0].sessionId,'root');assert.match(f.prompts[0].content[0].text,/frozen node.*restore task-owned/)
  assert.deepEqual(f.calls.map(c=>c.op),['infra-workbench-catalog'])
})
for(const config of [{status:'draft'},{status:'restored'},{active:'unknown-op'},{mode:'observe'},{agentStatus:'running'},{childStatus:'running'}])
  test('Owner start rejects an unsettled or unauthorized native tree '+JSON.stringify(config),async()=>{
    const f=fixture(config),r=await f.invoke('workbench-agent');assert.equal(r.ok,false);assert.equal(f.prompts.length,0)
    assert.ok(!f.calls.some(c=>c.op==='infra-workbench-run'))
  })
test('Preparation remains manual and does not prompt a model',async()=>{
  const f=fixture({status:'draft'}),r=await f.invoke('workbench-prepare');assert.equal(r.ok,true);assert.equal(f.prompts.length,0)
})
test('Retrying create reuses an already bound Session',async()=>{
  const f=fixture(),r=await f.invoke('workbench-create');assert.equal(r.ok,true);assert.equal(r.value.sessionId,'root');assert.equal(f.prompts.length,0)
})
test('Bound network execution reports actual failure and restoration to the same native root',async()=>{
  const f=fixture({status:'draft'}),r=await f.invoke('network-run')
  assert.equal(r.ok,true);assert.equal(r.value.status,'failed');assert.equal(r.value.restored,true)
  assert.equal(f.prompts.length,1);assert.equal(f.prompts[0].sessionId,'root')
  assert.match(f.prompts[0].content[0].text,/not RDMA or PD/)
  assert.deepEqual(f.calls.map(c=>c.op),['infra-network-view','infra-network-run'])
})
test('Observation-only network root rejects Owner run before any remote changes',async()=>{
  const f=fixture({mode:'observe'}),r=await f.invoke('network-run')
  assert.equal(r.ok,false);assert.equal(f.prompts.length,0)
  assert.deepEqual(f.calls.map(c=>c.op),['infra-network-view'])
})
