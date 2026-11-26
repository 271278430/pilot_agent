import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs'
import {resolve,join} from 'node:path'
import {fileURLToPath} from 'node:url'
import {Context} from '../harness/runtime/node_modules/@deepseek-ai/cordis/lib/index.js'
import Tools from '../harness/runtime/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../harness/runtime/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import * as NativeExecutor from '../harness/runtime/node_modules/@deepseek-ai/dsh-tool-executor-native/lib/index.js'
import * as ToolSystem from '../harness/runtime/plugins/tool-system/index.mjs'
import {readConfig,changeConfig,nativeConfig,validateServer} from '../harness/runtime/plugins/tool-system/config.mjs'
const project=fileURLToPath(new URL('../',import.meta.url)),privateRoot=join(project,'.local')
mkdirSync(privateRoot,{recursive:true})
const row=(extra={})=>({name:'docs',description:'Test docs',enabled:false,roles:['infra-lead'],access:'reference',transport:'stdio',image:'sha256:'+'b'.repeat(64),command:process.platform==='win32'?join(project,'.venv/Scripts/python.exe'):join(project,'.venv/bin/python'),args:[join(project,'tests/mcp_fixture.py')],...extra})
test('shared store rejects conflicting writers, unknown fields, credentials and invalid transports',()=>{
  const root=mkdtempSync(join(privateRoot,'mcp-config-test-'))
  try{
    assert.equal(readConfig(root).revision,0)
    const first=changeConfig(root,'put',{server:row(),expected_revision:0})
    assert.equal(first.revision,1);assert.equal(readConfig(root).servers[0].name,'docs')
    assert.throws(()=>changeConfig(root,'enable',{name:'docs',expected_revision:0}),/conflict/)
    writeFileSync(join(root,'.local/tool-system.lock'),'busy')
    assert.throws(()=>changeConfig(root,'delete',{name:'docs',expected_revision:1}),/writer/)
    rmSync(join(root,'.local/tool-system.lock'))
    assert.equal(changeConfig(root,'enable',{name:'docs',expected_revision:1}).servers[0].enabled,true)
    assert.equal(changeConfig(root,'disable',{name:'docs',expected_revision:2}).servers[0].enabled,false)
    assert.equal(changeConfig(root,'delete',{name:'docs',expected_revision:3}).servers.length,0)
    for(const extra of [{apiKey:'secret'},{roles:['owner']},{enabled:'true'},{args:'python -m mcp'},{transport:'sse'},{environmentRefs:{API_KEY:'secret value'}}])assert.throws(()=>validateServer(row(extra)))
    for(const url of ['https://user:password@example.org/mcp','https://example.org/mcp?api_key=secret','file:///x'])assert.throws(()=>validateServer({name:'http',enabled:false,roles:['infra-lead'],access:'task',transport:'streamable-http',url}))
    assert.equal(validateServer({name:'http',enabled:false,roles:['infra-lead'],access:'task',transport:'streamable-http',url:'http://example.org/mcp?workspace=pilot'}).url,'http://example.org/mcp?workspace=pilot')
    assert.equal(validateServer({name:'http',enabled:false,roles:['infra-lead'],access:'task',transport:'streamable-http',url:'http://127.0.0.1:1234/mcp'}).transport,'streamable-http')
    assert.throws(()=>nativeConfig(validateServer(row({environmentRefs:{KEY:'PILOT_MCP_MISSING'}})),root),/Missing/)
    assert.doesNotMatch(readFileSync(join(root,'.local/tool-system.json'),'utf8'),/secret value/)
  }finally{rmSync(root,{recursive:true})}
})
test('native MCP initialization, canonical output, scoped gating, idle reload, failure and disposal',async()=>{
  const root=mkdtempSync(join(privateRoot,'mcp-native-test-')),ctx=new Context(),actor={id:'session-test',status:'idle',session:{header:{}},role:'infra-lead'}
  let routeDenial,mode='workspace-write',rpc,view={}
  try{
    await ctx.plugin(SystemPrompt);await ctx.plugin(Tools)
    await ctx.plugin({apply(inner){
      inner.provide('agents',{list:()=>[actor],get:id=>id===actor.id?actor:undefined})
      inner.provide('agentPresets',{composedPreset:c=>c?.role??actor.role})
      inner.provide('pilotWorkspace',{rootId:a=>a.id,ensure:async()=>({}),routingDenial:()=>routeDenial,cachedView:()=>view,
        toolAllowed:async()=>{if(routeDenial==='pending'){await Promise.resolve();routeDenial=undefined}return !routeDenial}})
      inner.provide('sandboxPolicy',{resolve:()=>({mode})})
      inner.provide('pilotExecutionSandbox',{prepare:async r=>({id:'fixture',spec_path:'unused-fixture',command:r.command,args:r.args,cwd:root}),stop:async()=>{},call:async()=>({policy:{revision:0,probe_image:null},leases:[],active_count:0,max_active:4})})
      inner.provide('workingDirectory',{get:()=>root})
      inner.provide('connection',{rpc:{handle:(_path,fn)=>{rpc=fn;return()=>{}}}})
    }})
    actor.ctx=ctx.extend({role:'infra-lead'})
    await ctx.plugin(NativeExecutor)
    changeConfig(root,'put',{server:row({enabled:true,roles:['infra-lead','repo-expert']}),expected_revision:0})
    await ctx.plugin(ToolSystem,{root})
    const system=ctx.pilotToolSystem
    for(let i=0;i<150&&system.busy;i++)await new Promise(r=>setTimeout(r,100))
    assert.equal(system.clients.get('docs').status,'verified')
    const echo=ctx.tools.get('mcp__docs__echo');assert(echo)
    const selected={view:()=>new Map([[echo.name,echo]])}
    assert((await ctx.systemPrompt.assemble({scope:actor,toolSet:selected})).sections.some(s=>s.text.includes('fixture contextual instructions')))
    assert(!(await ctx.systemPrompt.assemble({scope:actor,toolSet:{view:()=>new Map()}})).sections.some(s=>s.text.includes('fixture contextual instructions')))
    const run={agent:actor,signal:new AbortController().signal}
    routeDenial='pending'
    const admitted=await ctx.tools.resolve(run)
    assert(admitted.view(actor).has(echo.name),'MCP must wait for intent resolution before checking cached denial')
    routeDenial='Unrelated intent'
    assert(!(await ctx.tools.resolve(run)).view(actor).has(echo.name))
    routeDenial=undefined
    assert.equal((await echo.execute({text:'native payload'},run)).content[0].text,'native payload')
    assert.equal(system.clients.get('docs').last_call_result,'passed')
    await assert.rejects(ctx.tools.get('mcp__docs__fail').execute({},run),/fixture failure/)
    assert.equal(system.clients.get('docs').last_call_result,'failed')
    assert.equal(system.denial(actor,echo.name),undefined)
    actor.role='infra-expert';actor.ctx=ctx.extend({role:'infra-expert'});assert.match(system.denial(actor,echo.name),/role/)
    actor.role='repo-expert';actor.ctx=ctx.extend({role:'repo-expert'});view={task:{state:{status:'running',strategy:{experts:[]}}}};assert.match(system.denial(actor,echo.name),/strategy/);view={}
    actor.role='infra-lead';actor.ctx=ctx.extend({role:'infra-lead'})
    view={task:{state:{execution_mode:'manual'}}};assert.match(system.denial(actor,echo.name),/manual/);view={}
    mode='read-only';assert.match(system.denial(actor,echo.name),/Observation/);mode='workspace-write'
    routeDenial='Unrelated intent';assert.equal(system.denial(actor,echo.name),routeDenial);routeDenial=undefined
    const outsider=await rpc('delete',{name:'docs',expected_revision:1},run.signal,{principal:{kind:'agent'}});assert.equal(outsider.ok,false);assert.equal(outsider.error.code,'pilot/forbidden');assert.equal(typeof outsider.error.details,'object')
    const rejected=await rpc('delete',{name:'docs',expected_revision:0},run.signal,{principal:{kind:'owner'}});assert.equal(rejected.error.code,'pilot/rejected');assert.equal(typeof rejected.error.details,'object')
    actor.status='running';changeConfig(root,'disable',{name:'docs',expected_revision:1});await system.reconcile()
    assert.match(system.denial(actor,echo.name),/disabled/);assert.equal(system.appliedRevision,1);assert(ctx.tools.get(echo.name))
    actor.status='idle';await system.reconcile();assert.equal(ctx.tools.get(echo.name),undefined)
    assert(!(await ctx.systemPrompt.assemble({scope:actor,toolSet:selected})).sections.some(s=>s.text.includes('fixture contextual instructions')))
    changeConfig(root,'put',{server:row({enabled:true,command:join(root,'not-an-executable')}),expected_revision:2});await system.reconcile()
    assert.equal(system.clients.get('docs').status,'failed');assert.equal(ctx.tools.get(echo.name),undefined)
    const catalog=await system.catalog({},run.signal);assert.equal(catalog.servers[0].status,'failed')
    assert.doesNotMatch(JSON.stringify(catalog),/password|apiKey/)
  }finally{await ctx.fiber.dispose();rmSync(root,{recursive:true})}
})
