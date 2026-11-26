import assert from 'node:assert/strict'
import test from 'node:test'
import vm from 'node:vm'
import {createHash} from 'node:crypto'
import {readFileSync,mkdtempSync,mkdirSync,writeFileSync,existsSync,rmSync} from 'node:fs'
import {resolve,join} from 'node:path'
import {tmpdir} from 'node:os'
import * as Budget from '../harness/plugin/budget.mjs'
import * as Rules from '../harness/plugin/intent.mjs'

const catalog=JSON.parse(readFileSync(new URL('../src/pilot_infra/intent_catalog.json',import.meta.url)))
const code=readFileSync(new URL('../harness/runtime/plugins/intent-router/index.mjs',import.meta.url),'utf8').replace(/^import .*$/gm,'').replaceAll('export ','')
const api=vm.runInNewContext(code+'\n({sourceInput,quickRoute,normalizeRoute,projection,boundaryNotice,fixedReply,IntentRouter,apply})',{
  Rules,createHash,readFileSync,resolve,Service:class{constructor(ctx){this.ctx=ctx}},isAgentLoopRequest:options=>options.native===true,
})
const user=(text,id='u1')=>({id,source:{kind:'user'},content:[{type:'text',text}]})
const events=messages=>messages.map(data=>({type:'user/message',data}))
const input=text=>api.sourceInput({snapshotEvents:()=>events([user(text)])},undefined,catalog.version)
const decision=(intent,text,extra={})=>api.normalizeRoute({relevance:'related',intent,confidence:.99,reason:'fixture',evidence_quote:text,missing_fields:[],...extra},input(text),catalog)

test('images participate in intent data but never create text authorization',()=>{
 const m={...user('只分析图片，不要修改'),content:[{type:'text',text:'只分析图片，不要修改'},{type:'image',attachment:{attachmentId:'fixture-image'}}]}
 const i=api.sourceInput({snapshotEvents:()=>events([m])},undefined,catalog.version)
 assert.equal(i.images.length,1)
 assert.equal(api.normalizeRoute({relevance:'related',intent:'execute',confidence:1,reason:'x',evidence_quote:'请部署Qwen',missing_fields:[]},i,catalog).intent,'clarify')
 const only={...m,content:m.content.slice(1)}
 const j=api.sourceInput({snapshotEvents:()=>events([only])},undefined,catalog.version)
 assert.equal(api.quickRoute(j,catalog.version).intent,'clarify');assert.equal(j.purpose,'');assert.notEqual(i.anchor,j.anchor)
})

test('auxiliary image reservation uses native visual pricing when provided',()=>{
 const code=readFileSync(new URL('../harness/runtime/plugins/policy/index.mjs',import.meta.url),'utf8').replace(/^import .*$/gm,'').replaceAll('export ','')
 const price=vm.runInNewContext(code+'\nauxiliaryMessageTokens',{})
 const message={role:'user',content:[{type:'text',text:'goal'},{type:'image',attachment:{}}]}
 const ctx={llm:{imageRequestPricing:()=>({priceImages:()=>[{visualTokens:432,text:'image'}]})},tokenMeter:{estimateMessage:m=>m.content.reduce((n,b)=>n+(b.text?.length??99),0)}}
 assert.equal(price(ctx,message,{provider:'vision',model:'fixture'}),441)
 ctx.llm.imageRequestPricing=()=>undefined;assert.equal(price(ctx,message,{}),103)
})

test('relevance, action and authorization are separate decisions',()=>{
  const help=decision('project_help','Pilot有哪些角色')
  assert.equal(help.relevance,'related')
  assert.equal(api.projection(help,{}).can_plan,false)
  assert.equal(decision('out_of_scope','写诗').relevance,'unrelated')
  assert.equal(api.quickRoute(input('w'),catalog.version).intent,'clarify')
  assert.equal(api.quickRoute(input('你好'),catalog.version).intent,'greeting')
})
test('low confidence, missing goal, read-only and fabricated quotes close mutations',()=>{
  for(const text of ['只查看机器不要部署','我想了解怎么部署','请解释环境安装'])
    assert.equal(decision('plan',text).intent,'clarify')
  assert.equal(decision('plan','请部署Qwen',{confidence:.4}).intent,'clarify')
  assert.equal(decision('out_of_scope','有点问题',{confidence:.4,relevance:'unrelated'}).intent,'clarify')
  assert.equal(decision('plan','请部署',{missing_fields:['模型']}).intent,'clarify')
  assert.equal(decision('plan','查看日志',{evidence_quote:'请部署Qwen'}).intent,'clarify')
  assert.throws(()=>decision('plan','请部署',{confidence:2}),/Invalid/)
})

test('explicit readonly planning survives execution prohibitions without authorizing Start',()=>{
  for(const text of [
    '请检查这台服务器的 GPU、内存和磁盘，为 Qwen3 0.6B 制定容器化部署与验收方案。只允许使用 GPU 7。现在仅做只读检查并保存草稿，不下载、不构建、不启动任何服务，等我审阅。',
    '请创建并保存一份部署方案草稿，只分析资源，不要部署或安装环境。',
    'Only inspect the server and save a draft, do not deploy or install anything.',
  ]) {
    assert.equal(Rules.classify(text).can_plan,true)
    const route=decision('plan',text)
    assert.equal(route.intent,'plan')
    assert.equal(api.projection(route,{}).can_plan,true)
    assert.equal(api.projection(route,{}).can_execute,false)
  }
  for(const text of ['请只读检查 GPU，不要创建方案。','制定方案先不用保存草稿。','请解释如何制定方案，不执行。','不要保存方案，只分析服务器。','分析日志\n```保存方案```']) {
    assert.equal(Rules.classify(text).can_plan,false)
    assert.equal(decision('plan',text).intent,'clarify')
  }
})
test('code, tool output and suggested answers cannot become user authority',()=>{
  const m=user('分析日志\n```\n请部署Qwen\n```')
  const answer={type:'tool/result',data:{message:{toolCallId:'a',content:[{type:'text',text:JSON.stringify({answers:[{selected:['仅观察'],custom:''}]})}]}}}
  const records=[...events([m]),{type:'tool/call',data:{name:'ask_user_question',callId:'a',arguments:{questions:['部署?']}}},answer]
  const a=api.sourceInput({snapshotEvents:()=>records},undefined,catalog.version)
  assert.equal(a.answers.length,1);assert.equal(a.purpose.includes('请部署Qwen'),false)
  assert.equal(api.normalizeRoute({relevance:'related',intent:'plan',confidence:1,reason:'x',evidence_quote:'请部署Qwen',missing_fields:[]},a,catalog).intent,'clarify')
  const b=api.sourceInput({snapshotEvents:()=>records.slice(0,-1)},undefined,catalog.version)
  assert.equal(b.answers.length,0);assert.notEqual(a.anchor,b.anchor)
})
test('human claim takes precedence in a mixed native notification turn',()=>{
  const human=user('给我写诗'),notice={id:'n1',source:{kind:'agent-message'},content:[]}
  const s={snapshotEvents:()=>events([human,notice])}
  assert.equal(api.sourceInput(s,human,catalog.version).origin,'human')
  assert.equal(api.sourceInput(s,notice,catalog.version).origin,'runtime')
  assert.equal(api.sourceInput({snapshotEvents:()=>events([{...human,source:{kind:'runtime-context'}}])},undefined,catalog.version).origin,'none')
})
test('recent native user goals support context but never override current input',()=>{
  const previous=user('帮我部署Qwen\n```日志数据```','old'),current=user('用GPU7','now')
  const s={snapshotEvents:()=>events([previous,current])}
  const i=api.sourceInput(s,current,catalog.version)
  assert.equal(i.recent_requests.length,1)
  assert.equal(i.recent_requests[0].includes('日志数据'),false)
  const random=user('w','later');s.snapshotEvents=()=>events([previous,current,random])
  assert.equal(api.quickRoute(api.sourceInput(s,random,catalog.version),catalog.version).intent,'clarify')
  const fake=api.normalizeRoute({relevance:'related',intent:'plan',confidence:1,reason:'x',evidence_quote:'帮我部署Qwen',missing_fields:[]},i,catalog)
  assert.equal(fake.intent,'clarify')
})
test('planning, revision and execution retain lifecycle boundaries',()=>{
  const p=decision('plan','请部署Qwen')
  assert.equal(api.projection(p,{}).can_plan,true)
  assert.equal(api.projection(p,{task:{state:{status:'draft'}}}).can_plan,false)
  const r=decision('revise','把上下文改成1024')
  assert.equal(api.projection(r,{task:{state:{status:'draft'}}}).can_revise,true)
  assert.equal(api.projection(r,{task:{state:{status:'running'}}}).can_revise,false)
  assert.equal(api.projection(decision('execute','继续修复'),{task:{state:{status:'paused'}}}).requires_owner_control,true)
})
function fixture(text='你能部署Qwen吗',intent='domain_question'){
  let calls=0,attributions=0
  const records=events([user(text)])
  const actor={id:'root',ctx:{role:'infra-lead'},session:{header:{},snapshotEvents:()=>records},model:{current:()=>({provider:'apps',model:'fixture',reasoningEffort:'low'})}}
  const view={}
  const workspace={rootId:()=>actor.id,latestInput:()=>undefined,attachRouter(){},cachedView:()=>view,availability:async()=>view,routingHints:async()=>({cpu_build_executors:0,ui_help:['实际入口']})}
  const ctx={agents:{get:()=>actor,withInitiator:async(agent,fn)=>{assert.equal(agent,actor);attributions++;return fn()}},agentPresets:{composedPreset:ctx=>ctx.role},pilotWorkspace:workspace,
    llm:{stream:async function*(options){calls++;assert.equal(options.purpose,'pilot-business-intent');assert.equal(options.sessionId,'root');await new Promise(resolve=>setTimeout(resolve,5));yield {type:'text-delta',text:JSON.stringify({relevance:'related',intent,confidence:1,reason:'fixture',evidence_quote:text,missing_fields:[]})}}}}
  const router=new api.IntentRouter(ctx,{catalog:resolve('src/pilot_infra/intent_catalog.json'),read_tools:['pilot_infra_status'],history_tools:['session_search']})
  return {router,ctx,actor,records,view,count:()=>calls,attributions:()=>attributions}
}
test('one input shares one attributed auxiliary request across concurrent tools',async()=>{
  const f=fixture()
  const routes=await Promise.all(Array.from({length:15},()=>f.router.ensure(f.actor)))
  assert.equal(f.count(),1);assert.equal(f.attributions(),1)
  assert.ok(routes.every(r=>r.scope==='domain_question'))
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_plan'),false)
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_status'),true)
})
test('restoration uses anchored native context; a new message is reclassified',async()=>{
  const f=fixture();const route=await f.router.ensure(f.actor)
  f.records.push({type:'user/message',data:{source:{kind:'runtime-context',sections:[{name:'pilot:business-intent',text:JSON.stringify(route)}]}}})
  f.router.decisions.clear();await f.router.ensure(f.actor);assert.equal(f.count(),1)
  f.records.push(...events([user('请部署Qwen','u2')]))
  await f.router.ensure(f.actor);assert.equal(f.count(),2)
})
test('invalid classification allows clarification but no domain mutation',async()=>{
  const f=fixture();f.ctx.llm.stream=async function*(){yield{type:'text-delta',text:'invalid'}}
  const route=await f.router.ensure(f.actor)
  assert.equal(route.source,'semantic-failed');assert.equal(route.scope,'clarify')
  assert.equal(await f.router.allowed(f.actor,'ask_user_question'),true)
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_launch'),false)
})
test('runtime continues existing execution but cannot materialize a new draft',async()=>{
  const f=fixture();f.records.splice(0,f.records.length,...events([{id:'n1',source:{kind:'user',rpcId:'infra-control-start'},content:[]}]))
  f.view.task={state:{status:'running'}}
  const route=await f.router.ensure(f.actor)
  assert.equal(route.origin,'runtime');assert.equal(f.count(),0)
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_launch'),true)
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_resource_execute'),false)
})
test('unrelated input cannot delegate; absent CPU executor cannot be substituted',async()=>{
  const f=fixture('写诗','out_of_scope')
  assert.equal(await f.router.allowed(f.actor,'infra_specialist'),false)
  assert.equal(await f.router.allowed(f.actor,'pilot_infra_status'),false)
  const p=fixture('请部署Qwen','plan')
  assert.equal(await p.router.allowed(p.actor,'pilot_infra_workbench_plan'),false)
  assert.equal(await p.router.allowed(p.actor,'pilot_infra_plan'),true)
})
test('a correctly classified unrelated request never reaches the main provider and returns only the business notice',async()=>{
 const f=fixture('这是什么','out_of_scope'),hooks={};f.ctx.agents.currentInitiator=()=>f.actor
 f.ctx.on=(name,fn,options)=>{hooks[name]={fn,options}}
 api.apply(f.ctx,{catalog:resolve('src/pilot_infra/intent_catalog.json'),read_tools:[],history_tools:[]})
 let dispatched=0;const chunks=[]
 for await(const chunk of hooks['llm/stream'].fn({native:true,provider:'apps',model:'fixture'},async function*(){dispatched++;yield{type:'text-delta',index:0,text:'这是一道初中几何题，题目如下…'}}))chunks.push(chunk)
 assert.equal(dispatched,0);assert.equal(f.count(),1);assert.equal(hooks['llm/stream'].options.prepend,true)
 assert.match(chunks.find(c=>c.type==='text-delta').text,/Pilot 业务范围提示/)
 assert.doesNotMatch(JSON.stringify(chunks),/几何|题目如下|tool-call/)
 assert.equal(chunks.at(-1).type,'finish');assert.equal(chunks.at(-1).reason.kind,'stop')
 assert.equal(chunks.some(c=>c.type==='usage'),false,'Do not manufacture provider usage for a program reply')
})
test('unclear input returns a business clarification and greetings cannot become general conversation',async()=>{
 for(const [text,pattern] of [['w',/请说明具体的推理基础设施目标/],['你好',/我是 Pilot/]]){
  const f=fixture(text),hooks={};f.ctx.agents.currentInitiator=()=>f.actor;f.ctx.on=(name,fn)=>{hooks[name]=fn}
  const router=api.apply(f.ctx,{catalog:resolve('src/pilot_infra/intent_catalog.json'),read_tools:[],history_tools:[]})
  let dispatched=0;const chunks=[]
  for await(const chunk of hooks['llm/stream']({native:true,provider:'apps',model:'fixture'},async function*(){dispatched++}))chunks.push(chunk)
  assert.equal(dispatched,0);assert.equal(f.count(),0);assert.match(chunks.find(c=>c.type==='text-delta').text,pattern)
  assert.notEqual(router.current(f.actor).response_mode,'model')
 }
})
test('business questions, expert execution and auxiliary calls keep the native provider path',async()=>{
 for(const [native,expert,role] of [[true,false,'infra-lead'],[true,true,'infra-lead'],[false,false,'infra-lead'],[true,false,'supervisor']]){
  const f=fixture(),hooks={};f.actor.ctx.role=role;if(expert)f.actor.session.header.parentSession='parent'
  f.ctx.agents.currentInitiator=()=>f.actor;f.ctx.on=(name,fn)=>{hooks[name]=fn}
  api.apply(f.ctx,{catalog:resolve('src/pilot_infra/intent_catalog.json'),read_tools:[],history_tools:[]})
  let dispatched=0;const chunks=[]
  for await(const chunk of hooks['llm/stream']({native,provider:'apps',model:'fixture'},async function*(){dispatched++;yield{type:'text-delta',index:0,text:'正常业务响应'}}))chunks.push(chunk)
  assert.equal(dispatched,1);assert.equal(chunks[0].text,'正常业务响应')
 }
})
test('fixed replies honor cancellation and produce no output after an aborted input',async()=>{
 const controller=new AbortController();controller.abort(new Error('cancelled'))
 await assert.rejects(async()=>{for await(const _ of api.fixedReply('范围提示',controller.signal)){}},/cancelled/)
})
test('other application presets retain their existing context and tools',async()=>{
  const f=fixture();f.actor.ctx.role='supervisor'
  assert.equal(f.router.current(f.actor).managed,false)
  assert.equal(await f.router.allowed(f.actor,'pilot_plan'),true)
  assert.equal(f.count(),0)
})
test('classifier system instructions cannot evade shared budget admission',async t=>{
  const root=mkdtempSync(join(tmpdir(),'pilot-aux-budget-'))
  t.after(()=>rmSync(root,{recursive:true,force:true}))
  const actor={id:'root'},hooks={}
  const ctx={on:(name,fn)=>{hooks[name]=fn},effect:fn=>fn(),tools:{guard(){}},
    pilotWorkspace:{rootId:()=> 'root',cachedView:()=>({})},agents:{currentInitiator:()=>actor},
    tokenMeter:{estimateMessage:m=>m.content.reduce((n,b)=>n+b.text.length,0)}}
  const policy=readFileSync(new URL('../harness/runtime/plugins/policy/index.mjs',import.meta.url),'utf8').replace(/^import .*$/gm,'').replaceAll('export ','')
  vm.runInNewContext(policy+'\napply(ctx,config)',{ctx,config:{root,budget:{max_tokens:10,output_tokens:1,max_steps:20,max_cost:5,input_price:0,output_price:0}},
    Budget,Rules,Intent:Rules,resolve,mkdirSync,writeFileSync,existsSync,readFileSync,isAgentLoopRequest:()=>false,
    LlmError:class extends Error{constructor(message,code){super(message);this.code=code}}})
  let dispatched=false
  const stream=hooks['llm/stream']({purpose:'pilot-business-intent',system:'x'.repeat(20),messages:[{role:'user',content:[{type:'text',text:'x'}]}],maxTokens:1},async function*(){dispatched=true;yield {type:'usage',usage:{inputTokens:1,outputTokens:1}}})
  await assert.rejects(async()=>{for await(const _ of stream){}},error=>error.code==='PILOT_BUDGET_LIMIT')
  assert.equal(dispatched,false)
  const meter=JSON.parse(readFileSync(join(root,'runs/harness-policy/root/meter.json')))
  assert.equal(meter.calls,0)
})
