import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,readFileSync,rmSync,symlinkSync,writeFileSync} from 'node:fs'
import {join} from 'node:path'
import {fileURLToPath} from 'node:url'
import {Context} from '../harness/runtime/node_modules/@deepseek-ai/cordis/lib/index.js'
import * as Plugin from '../harness/runtime/plugins/host-settings/index.mjs'
import * as C from '../harness/runtime/plugins/host-settings/config.mjs'
import {PilotWorkspace} from '../harness/runtime/plugins/workspace/index.mjs'
import {resolveChildAgentOptions} from '../harness/runtime/node_modules/@deepseek-ai/dsh-subagent/lib/index.js'
const privateRoot=fileURLToPath(new URL('../.local/',import.meta.url))
const model=(extra={})=>({model_id:'scripted',base_url:'https://fixture.invalid/v1',context_window:32768,max_output_tokens:6000,auth_required:true,...extra})
async function fixture(){
 const root=mkdtempSync(join(privateRoot,'host-settings-test-'));mkdirSync(join(root,'runs/harness-inbox'),{recursive:true})
 const ctx=new Context(),keys=new Map([['REPOPILOT_API_KEY','fixture-secret-2']]);let rpc,selection=C.selection(model()),providers={apps:C.profile({...model(),credential_ref:'REPOPILOT_API_KEY'})},revision=1,idle=true,fail=false
 const actor={id:'root',status:'idle',model:{current:()=>actor.selected,select:s=>actor.selected=s},selected:selection}
 await ctx.plugin({apply(c){
  c.provide('connection',{rpc:{handle:(_p,fn)=>{rpc=fn;return()=>{}}}})
  c.provide('pilotWorkspace',{request:async()=>({idle})})
  c.provide('settings',{describe:()=>[{ns:'llm-pi-ai',value:{providers},revision}],update:async(_n,v,r)=>{assert.equal(r,revision);providers=v.providers;revision++},replace:async(_n,v)=>{providers=v.providers;revision++}})
  c.provide('credentials',{config:{path:join(root,'.local/.credentials.yaml')},describe:async ref=>({configured:keys.has(ref)}),resolve:async ref=>({value:keys.get(ref)}),set:async(ref,key)=>{keys.set(ref,key)}})
  c.provide('llm',{resolveCallConfig:async()=>{if(fail)throw new Error('fixture failure')}})
  c.provide('agents',{list:()=>[actor]});c.provide('agentPresets',{});c.provide('sessionController',{})
  c.provide('agentDefaultModel',{currentSelection:()=>selection,saveSelection:async s=>{selection=s}})
 }})
 await ctx.plugin(Plugin,{root})
 return {root,ctx,actor,keys,clearProfiles:()=>{providers={}},call:async(op,p={})=>rpc(op,p,undefined,{principal:{kind:'owner'}}),idle:v=>idle=v,fail:v=>fail=v,close:async()=>{await ctx.fiber.dispose();rmSync(root,{recursive:true})}}
}
test('saved parameters and key remain unapplied until idle native sync; failures restore profile and selection',async()=>{
 const f=await fixture();try{
  let r=await f.call('view');assert.equal(r.ok,true);assert.equal(r.value.desired.model.key_configured,true)
  assert.equal((await f.call('save',{section:'model',model:model({model_id:'new-model',api_key:'fixture-secret-2-1'}),expected_revision:0})).ok,true)
  assert.equal(f.actor.model.current().model,'scripted');assert.equal(C.stored(f.root).applied.model.model_id,'scripted')
  assert.doesNotMatch(readFileSync(C.pathOf(f.root),'utf8'),/fixture-secret-2-1|fixture-secret-2/)
  assert.equal((await f.call('save',{section:'workspace',workspace_root:f.root,expected_revision:0})).ok,false)
  f.actor.status='running';assert.equal((await f.call('sync',{expected_revision:1})).ok,false);f.actor.status='idle'
  f.idle(false);assert.equal((await f.call('sync',{expected_revision:1})).ok,false);f.idle(true)
  f.fail(true);r=await f.call('sync',{expected_revision:1});assert.equal(r.ok,false);assert.equal(f.actor.model.current().model,'scripted');assert.equal(C.stored(f.root).applied_revision,0)
  f.fail(false);r=await f.call('sync',{expected_revision:1});assert.equal(r.ok,true);assert.equal(r.value.pending,false);assert.equal(f.actor.model.current().model,'new-model')
  assert.equal(r.value.compaction_restart_required,true)
  const path=join(f.root,'new-root');r=await f.call('save',{section:'workspace',workspace_root:path,create_missing:true,expected_revision:1});assert(r.ok);assert.notEqual(C.appliedRoot(f.root),path)
  r=await f.call('sync',{expected_revision:2});assert(r.ok);assert.equal(C.appliedRoot(f.root),path)
  const inbox=await PilotWorkspace.prototype.newInbox.call({inboxPath:()=>path,ctx:{workspaceRegistry:{create:async folder=>({path:folder})}}},'manual-task')
  assert(inbox.path.startsWith(path));assert.notEqual(inbox.path,path)
  assert.equal((await f.call('save',{section:'model',model:model({auth_required:false}),expected_revision:2})).ok,true)
  assert.equal((await f.call('sync',{expected_revision:3})).ok,true);assert.equal(C.stored(f.root).applied.model.credential_ref,C.NOAUTH_REF)
 }finally{await f.close()}
})
test('probe never exposes provider bodies, forwards redirects or uses saved Key at an unconfirmed new URL',async()=>{
 const f=await fixture(),original=globalThis.fetch;try{
  let requests=[];globalThis.fetch=async(url,options)=>{requests.push({url,options});return new Response('fixture-secret-2',{status:302,headers:{location:'https://other.invalid'}})}
  let r=await f.call('test-model',{model:model({base_url:'https://new.invalid/v1'})});assert.equal(r.ok,false);assert.equal(requests.length,0)
  r=await f.call('test-model',{model:model()});assert(r.ok);assert.equal(r.value.passed,false);assert.equal(requests[0].options.redirect,'manual');assert.doesNotMatch(JSON.stringify(r),/fixture-secret-2/)
  globalThis.fetch=async()=>new Response(JSON.stringify({choices:[{message:{content:'fixture-secret-2'}}],usage:{prompt_tokens:10,completion_tokens:1}}))
  r=await f.call('test-model',{model:model()});assert.equal(r.value.passed,true);assert.equal(r.value.usage.output_tokens,1);assert.doesNotMatch(JSON.stringify(r),/fixture-secret-2/)
  globalThis.fetch=async()=>new Response('x'.repeat(65537));r=await f.call('test-model',{model:model()});assert.equal(r.value.passed,false)
  globalThis.fetch=async()=>{throw new Error('fixture-secret-2')};r=await f.call('test-model',{model:model()});assert.equal(r.value.passed,false);assert.doesNotMatch(JSON.stringify(r),/fixture-secret-2/)
  assert.equal(C.stored(f.root),null)
 }finally{globalThis.fetch=original;await f.close()}
})
test('provider errors distinguish authentication, product permissions, model names and quotas without exposing response text',async()=>{
 const f=await fixture(),original=globalThis.fetch;try{
  const configured=model({base_url:'https://open.bigmodel.cn/api/paas/v4',model_id:'glm-5.3-flash',reasoning:'auto',vision:true,api_key:'fixture-secret-2'})
  for(const [status,code,category] of [[401,'1000','authentication'],[403,'1220','api-permission'],[400,'1211','model'],[429,'1113','quota'],[429,'1311','model-permission']]){
   globalThis.fetch=async(_url,options)=>{assert.equal(JSON.parse(options.body).thinking.type,'enabled');return new Response(JSON.stringify({error:{code,message:'fixture-secret-2 '+configured.api_key}}),{status})}
   const r=await f.call('test-model',{model:configured,test_image:true});assert(r.ok);assert.equal(r.value.passed,false);assert.equal(r.value.http_status,status);assert.equal(r.value.provider_code,code);assert.equal(r.value.error_category,category);assert.equal(r.value.test_kind,'image-request');assert.doesNotMatch(JSON.stringify(r),/fixture-secret-2/)
   if(code==='1220')assert.match(r.value.message,/错误模型ID/)
  }
  globalThis.fetch=async()=>new Response(JSON.stringify({error:{code:'fixture-secret-2',message:'fixture-secret-2'}}),{status:403})
  const r=await f.call('test-model',{model:configured});assert.equal(r.value.provider_code,undefined);assert.equal(r.value.error_category,'api-permission');assert.doesNotMatch(JSON.stringify(r),/fixture-secret-2/)
  globalThis.fetch=async()=>new Response('not-json fixture-secret-2',{status:401})
  assert.equal((await f.call('test-model',{model:configured})).value.error_category,'authentication')
 }finally{globalThis.fetch=original;await f.close()}
})
test('invalid official model IDs and thinking choices are rejected before requests, writes or native sync; old drafts remain editable',async()=>{
 const f=await fixture(),original=globalThis.fetch;try{
  let requests=0;globalThis.fetch=async()=>{requests++;throw new Error('must not call')}
  for(const extra of [{model_id:'2'},{model_id:'glm-5.3-flash',reasoning:'off'}]){
   const invalid=model({...extra,base_url:'https://open.bigmodel.cn/api/paas/v4',api_key:'fixture-secret-2'})
   assert.equal((await f.call('test-model',{model:invalid})).ok,false)
   assert.equal((await f.call('save',{section:'model',add_new:true,model:invalid,expected_revision:0})).ok,false)
  }
  assert.equal(requests,0);assert.equal(C.stored(f.root),null)
  assert.equal(C.configuredModel(model({model_id:'2',base_url:'http://127.0.0.1:8000/v1'})).model_id,'2')
  await f.call('save',{section:'model',add_new:true,model:model({api_key:'fixture-secret-2'}),expected_revision:0})
  const saved=C.stored(f.root),m=saved.desired.models[1];m.model_id='2';m.base_url='https://open.bigmodel.cn/api/paas/v4';C.write(f.root,saved)
  assert.equal((await f.call('view')).ok,true)
  assert.equal((await f.call('sync',{expected_revision:1})).ok,false)
  assert.equal(C.stored(f.root).applied_revision,0)
  assert.equal((await f.call('save',{section:'model',model:model({...m,model_id:'glm-5.3-flash',reasoning:'auto',credential_ref:undefined,credential_file:undefined}),expected_revision:1})).ok,false)
  const {credential_ref,credential_file,...editable}=m
  assert.equal((await f.call('save',{section:'model',model:{...editable,model_id:'glm-5.3-flash',reasoning:'auto'},expected_revision:1})).ok,true)
  assert.equal((await f.call('sync',{expected_revision:2})).ok,true)
 }finally{globalThis.fetch=original;await f.close()}
})
test('directory and model validation reject unsafe roots and ambiguous API settings',async()=>{
 const f=await fixture();try{
  for(const path of [join(f.root,'.local'),f.root,'relative'])await assert.rejects(C.checkRoot(f.root,path))
  await assert.rejects(C.checkRoot(f.root,join(f.root,'missing','child'),true))
  if(process.platform!=='win32'){symlinkSync(join(f.root,'runs'),join(f.root,'linked'));await assert.rejects(C.checkRoot(f.root,join(f.root,'linked')))}
  for(const extra of [{base_url:'https://u:private@example.org/v1'},{base_url:'https://example.org/?key=private'},{base_url:'file:///x'},{max_output_tokens:32768},{api_key:'a\nb'},{unknown:true}])assert.throws(()=>C.checkedModel(model(extra)))
  assert.equal(C.checkedModel(model({base_url:'https://example.org/v1/chat/completions'})).base_url,'https://example.org/v1')
 }finally{await f.close()}
})
test('multiple routes retain independent credentials, Session choice and explicit default',async()=>{
 const f=await fixture();try{
  let v=(await f.call('save',{section:'model',add_new:true,model:model({display_name:'视觉',vision:true,reasoning:'off',api_key:'fixture-secret-3'}),expected_revision:0})).value
  const vision=v.desired.models[1];assert.equal(v.desired.models.length,2);assert.notEqual(vision.provider,'apps')
  assert.equal((await f.call('sync',{expected_revision:1})).ok,true)
  f.actor.model.select({provider:vision.provider,model:'scripted',reasoningEffort:'off'})
  await f.ctx.agentDefaultModel.saveSelection(f.actor.model.current())
  assert.equal(f.ctx.agentDefaultModel.currentSelection().provider,'apps')
  await f.ctx.parallel('agent/created',{agent:f.actor})
  assert.equal(f.actor.model.current().provider,vision.provider)
  v=(await f.call('save',{section:'default-model',config_id:vision.config_id,expected_revision:1})).value
  assert.equal((await f.call('sync',{expected_revision:2})).ok,true)
  assert.equal(f.ctx.agentDefaultModel.currentSelection().provider,vision.provider)
  await f.call('save',{section:'default-model',config_id:'apps',expected_revision:2});assert.equal((await f.call('sync',{expected_revision:3})).ok,true)
  assert.equal(f.actor.model.current().provider,vision.provider)
  await f.call('save',{section:'remove-model',config_id:vision.config_id,expected_revision:3})
  assert.equal((await f.call('sync',{expected_revision:4})).ok,false)
  f.actor.model.select(C.selection(C.stored(f.root).applied.models[0]));assert.equal((await f.call('sync',{expected_revision:4})).ok,true)
  assert.doesNotMatch(readFileSync(C.pathOf(f.root),'utf8'),/fixture-secret-3|fixture-secret-2/)
 }finally{await f.close()}
})
test('legacy migration preserves Key references and has no silent vision assumption',async()=>{
 const f=await fixture();try{
  const m={...model(),credential_ref:'REPOPILOT_API_KEY',credential_file:join(f.root,'.local/.credentials.yaml')}
  const value={version:1,revision:3,applied_revision:3,desired:{workspace_root:join(f.root,'runs/harness-inbox'),model:m},applied:{workspace_root:join(f.root,'runs/harness-inbox'),model:m}}
  mkdirSync(join(f.root,'.local'),{recursive:true})
  writeFileSync(C.pathOf(f.root),JSON.stringify(value));const v=C.stored(f.root)
  assert.equal(v.version,2);assert.equal(v.applied.models[0].credential_ref,'REPOPILOT_API_KEY');assert.equal(v.applied.model.vision,false)
  assert.equal(JSON.parse(readFileSync(C.pathOf(f.root))).version,1)
 }finally{await f.close()}
})
test('native experts inherit the latest requested route, and plain models omit effort',()=>{
 const model={config_id:'a'.repeat(32),model_id:'vision',reasoning:'off'},selected=C.selection(model)
 assert.equal(selected.reasoningEffort,undefined)
 const parent={options:{provider:'apps',model:'old',reasoningEffort:'low',maxTokens:1024},session:{requestHeader:()=>({config:selected})}}
 for(const role of ['infra-expert','repo-expert']){
  const child=resolveChildAgentOptions(parent,undefined,1)
  assert.equal(child.provider,selected.provider,role);assert.equal(child.model,'vision',role);assert.equal(child.reasoningEffort,undefined,role)
 }
})
test('missing native catalog retains editable saved settings and can synchronize repair',async()=>{
 const f=await fixture();try{
  assert.equal((await f.call('sync',{expected_revision:0})).ok,true)
  f.clearProfiles();const view=await f.call('view');assert(view.ok);assert.equal(view.value.live_model,null);assert(view.value.live_error);assert(view.value.pending)
  assert.equal(view.value.desired.models.length,1)
  assert.equal((await f.call('sync',{expected_revision:0})).ok,true);assert.equal((await f.call('view')).value.pending,false)
 }finally{await f.close()}
})
test('resource root save is unapplied, owned, conflict-safe and never moves old data',async()=>{
 const f=await fixture();try{
  const old=join(f.root,'resources'),next=join(f.root,'new-resources')
  await f.call('sync',{expected_revision:0});writeFileSync(join(old,'receipt.txt'),'preserve')
  const v=await f.call('save',{section:'resources',resource_root:next,create_missing:true,expected_revision:0});assert.equal(v.ok,true);assert.equal(v.value.applied.resource_root,old)
  assert.equal((await f.call('sync',{expected_revision:1})).ok,true);assert.equal(C.stored(f.root).applied.resource_root,next);assert.equal(readFileSync(join(old,'receipt.txt'),'utf8'),'preserve')
  assert.equal((await f.call('save',{section:'resources',resource_root:f.root,expected_revision:1})).ok,false)
  const foreign=join(f.root,'foreign');mkdirSync(foreign);writeFileSync(join(foreign,'file'),'owner');await assert.rejects(C.checkResourceRoot(f.root,foreign))
  writeFileSync(join(next,'.pilot-resources.json'),JSON.stringify({owner_namespace:'foreign'}));await assert.rejects(C.checkResourceRoot(f.root,next))
 }finally{await f.close()}
})
