import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

test('QA authorizer precedes browser forwarding and only grants a frozen whitelisted artifact',async()=>{
  let handler,options,queries=0,nextCalls=0,roots=['root']
  const ctx={on:(_event,fn,opts)=>{handler=fn;options=opts},pilotWorkspace:{rootId:a=>a.id,request:async(op,id,role)=>{
    assert.equal(op,'infra-engine-source-status');assert.equal(id,'root');assert.equal(role,'owner');queries++
    return {candidates:[{candidate_id:'c2',build:'passed',artifact:{wheel_sha256:'sha'},review_digest:'review',decision:null}]}
  }}}
  const code=readFileSync(new URL('../src/pilot_eval/approval_authorizer.mjs',import.meta.url),'utf8').replace(/^import .*$/m,'').replaceAll('export ','')
  vm.runInNewContext(code+'\napply(ctx,{root:"/qa"})',{ctx,readFileSync:()=>JSON.stringify({session_ids:roots})})
  const req={agent:{id:'root'},toolName:'pilot_infra_engine_request_approval',reason:'Pilot engine artifact c2 / SHA256 review\nArtifact: {"wheel_sha256":"sha"}'}
  const next=()=>{nextCalls++;return 'unavailable'}
  assert.equal(options.prepend,true)
  assert.equal(await handler(req,next),'allowed-once')
  assert.equal(await handler({...req,reason:req.reason.replace('review','old')},next),'rejected')
  roots=[]
  assert.equal(await handler(req,next),'unavailable')
  assert.equal(queries,2);assert.equal(nextCalls,1)
  assert.equal(await handler({...req,toolName:'pilot_infra_resource_request_approval'},next),'unavailable')
  assert.equal(queries,2)
})
