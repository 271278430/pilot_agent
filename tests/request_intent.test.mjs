import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'
import * as Intent from '../harness/plugin/intent.mjs'

const user=(text,id='u1')=>({id,source:{kind:'user'},content:[{type:'text',text}]})
const session=messages=>({header:{},snapshotEvents:()=>messages.map(message=>({type:'user/message',data:message}))})
test('ambiguous input, capability queries and observation cannot invent a task',()=>{
  for(const text of ['w','你好','可以做什么','你能部署Qwen吗','如何部署Qwen','看看GPU占用','只检查服务器，不要部署','为什么任务失败了','vllm是什么','不用创建方案','can you deploy a model?','explain deployment','部署','帮我部署','测试一下','latest','deployment failed','部署失败了','修改方案是什么意思','部署Qwen需要哪些条件'])
    assert.equal(Intent.classify(text).can_plan,false,text)
  for(const text of ['帮我部署Qwen3，先保存方案不要启动','部署Qwen3','给我提出部署与验证方案','Save a draft only. Wait for Owner Start.','创建一个隔离环境，编译SGLang','将并发改成4','检查机器并为Qwen提出方案'])
    assert.equal(Intent.classify(text).can_plan,true,text)
})
test('only native user input authorizes planning; data, titles and random followups cannot',()=>{
  const request=user('帮我部署Qwen3')
  assert.equal(Intent.nativeRequest(session([request]),user('w','u2')).can_plan,false)
  assert.equal(Intent.nativeRequest(session([request]),user('只看占用','u2')).can_plan,false)
  assert.equal(Intent.nativeRequest(session([request]),user('用Qwen3，GPU7','u2')).can_plan,true)
  assert.equal(Intent.nativeRequest(session([{...request,source:{kind:'runtime-context'}}])).can_plan,false)
  assert.equal(Intent.nativeRequest(session([{...request,source:{kind:'subagent'}}])).can_plan,false)
  assert.equal(Intent.nativeRequest(session([user('w')])).can_plan,false)
})
test('native conditional tool activation and execution fallback both protect the domain bridge',async()=>{
  const registrations=[],calls=[]
  let actor={id:'root',session:session([user('w')])}
  const workspace={intent:agent=>Intent.nativeRequest(agent.session),rootId:()=> 'root',ensure:async()=>({}),request:async(...args)=>{calls.push(args);return {}}}
  const ctx={effect:fn=>fn(),tools:{register:(definition,activation)=>registrations.push({definition,activation})},pilotWorkspace:workspace,sandboxPolicy:{resolve:()=>({mode:'workspace-write'})}}
  const source=readFileSync(new URL('../harness/runtime/plugins/workspace/index.mjs',import.meta.url),'utf8').replace(/^import .*$/gm,'').replaceAll('export ','')
  vm.runInNewContext(source+'\nregisterTools(ctx,{role:"infra-lead",tools:[{name:"infra_plan",parameters:{},description:"Save"}]})',{ctx,Intent,Service:class{},defineTool:definition=>definition})
  const tool=registrations[0]
  assert.equal(tool.activation.when({agent:actor}),false)
  await assert.rejects(tool.definition.execute({}, {agent:actor}),/没有明确请求/)
  assert.equal(calls.length,0)
  actor={id:'root',session:session([user('帮我部署Qwen3')])}
  assert.equal(tool.activation.when({agent:actor}),true)
  await tool.definition.execute({}, {agent:actor,concludeTurn(){}})
  assert.equal(calls.length,1)
})
