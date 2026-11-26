import test from 'node:test'
import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,writeFileSync,readFileSync,rmSync} from 'node:fs'
import {join} from 'node:path'
import {fileURLToPath} from 'node:url'
import {spawn} from 'node:child_process'
import {Context} from '../harness/runtime/node_modules/@deepseek-ai/cordis/lib/index.js'
import Tools from '../harness/runtime/node_modules/@deepseek-ai/dsh-tools/lib/index.js'
import SystemPrompt from '../harness/runtime/node_modules/@deepseek-ai/dsh-system-prompt/lib/index.js'
import * as Sandbox from '../harness/runtime/plugins/execution-sandbox/index.mjs'
import * as ToolSystem from '../harness/runtime/plugins/tool-system/index.mjs'
import {changeConfig,validateServer} from '../harness/runtime/plugins/tool-system/config.mjs'
const project=fileURLToPath(new URL('../',import.meta.url)),privateDir=join(project,'.local')

test('legacy stdio and control-environment overrides cannot bypass container registration',()=>{
 const row={name:'legacy',transport:'stdio',command:'/usr/bin/python3',enabled:true,roles:['infra-lead'],access:'reference'}
 assert.equal(validateServer(row).image,null)
 for(const extra of [{image:'latest'},{leaseSeconds:0},{environmentRefs:{LD_PRELOAD:'LIB'}},{environmentRefs:{TOKEN:'REPOPILOT_API_KEY'}}])assert.throws(()=>validateServer({...row,...extra}))
})

test('real Docker + native MCP: root isolation, credentials, readonly snapshots, network and cancellation cleanup',
 {skip:!process.env.PILOT_TEST_MCP_IMAGE,timeout:120000},async()=>{
 const root=mkdtempSync(join(privateDir,'real-mcp-sandbox-')),ctx=new Context(),image=process.env.PILOT_TEST_MCP_IMAGE
 const python=join(project,'.venv/bin/python'),actors=['A','B'].map(id=>({id,role:'infra-lead',status:'idle',session:{header:{cwd:join(root,'runs',id)}}}))
 const bridge=(operation,args)=>new Promise((done,fail)=>{
  const child=spawn(python,['-m','pilot_host.bridge',root],{cwd:project,env:{...process.env,PYTHONPATH:join(project,'src')},stdio:['pipe','pipe','pipe']})
  let result='';child.stdout.on('data',v=>result+=v);child.stderr.resume();child.on('error',fail)
  child.on('close',()=>{try{const r=JSON.parse(result);if(!r.ok)throw new Error(r.error);done(r.value)}catch(e){fail(e)}})
  child.stdin.end(JSON.stringify({operation,role:'sandbox-broker',arguments:args}))
 })
 const previous=process.env.PILOT_MCP_TEST_TOKEN;process.env.PILOT_MCP_TEST_TOKEN='dedicated-fixture-private-value';let passed=false
 try{
  for(const a of actors){mkdirSync(a.session.header.cwd,{recursive:true});writeFileSync(join(a.session.header.cwd,'public.txt'),a.id)}
  await ctx.plugin(SystemPrompt);await ctx.plugin(Tools)
  await ctx.plugin({apply(c){
   c.provide('connection',{rpc:{handle:()=>()=>{}}});c.provide('agents',{list:()=>actors,get:id=>actors.find(a=>a.id===id)})
   c.provide('agentPresets',{composedPreset:c=>c?.role??'infra-lead'});c.provide('sandboxPolicy',{resolve:()=>({mode:'workspace-write'})})
   c.provide('workingDirectory',{get:s=>s.header.cwd})
   c.provide('pilotWorkspace',{config:{python},rootId:a=>a.id,request:(op,_id,_role,args)=>bridge(op,args),routingDenial:()=>undefined,cachedView:()=>({})})
  }})
  for(const a of actors)a.ctx=ctx.extend({role:a.role})
  await ctx.plugin(Sandbox,{root})
  changeConfig(root,'put',{expected_revision:0,server:{name:'isolation',transport:'stdio',command:'/usr/local/bin/python3',args:['/opt/server.py'],image,
    workspaceRead:true,leaseSeconds:300,environmentRefs:{TEST_TOKEN:'PILOT_MCP_TEST_TOKEN'},enabled:true,roles:['infra-lead'],access:'reference'}})
  await ctx.plugin(ToolSystem,{root})
  const system=ctx.pilotToolSystem
  for(let i=0;i<200&&system.busy;i++)await new Promise(r=>setTimeout(r,100))
  assert.equal(system.clients.get('isolation').status,'verified',system.clients.get('isolation').reason)
  const call=async(actor,name,args={},signal=new AbortController().signal)=>{
    const tool=ctx.tools.get('mcp__isolation__'+name);assert(tool)
    return (await tool.execute(args,{agent:actor,signal})).content[0].text
  }
  assert.equal(await call(actors[0],'count'),'1');assert.equal(await call(actors[0],'count'),'2');assert.equal(await call(actors[1],'count'),'1')
  assert.equal(await call(actors[0],'read',{path:'/input/public.txt'}),'A');assert.equal(await call(actors[1],'read',{path:'/input/public.txt'}),'B')
  await call(actors[0],'write',{path:'/workspace/private.txt',text:'A-only'})
  await assert.rejects(call(actors[1],'read',{path:'/workspace/private.txt'}),/denied/)
  assert.equal(await call(actors[1],'count'),'2')
  for(const path of ['/input/public.txt','/host-probe'])await assert.rejects(call(actors[0],'write',{path,text:'bad'}),/denied/)
  await assert.rejects(call(actors[0],'read',{path:join(project,'.local/glm.env')}),/denied/)
  assert.equal(await call(actors[0],'env',{name:'REPOPILOT_API_KEY'}),'absent')
  assert.equal(await call(actors[0],'env',{name:'TEST_TOKEN'}),'[REDACTED]')
  await assert.rejects(call(actors[0],'network'),/denied/)
  const cancelled=new AbortController();setTimeout(()=>cancelled.abort(),150)
  await assert.rejects(call(actors[0],'wait',{},cancelled.signal))
  await assert.rejects(call(actors[0],'count'),/未确认完成/)
  assert.equal(readFileSync(join(actors[0].session.header.cwd,'public.txt'),'utf8'),'A')
  await ctx.fiber.dispose()
  assert.equal((await bridge('sandbox-view',{})).active_count,0)
  assert(!(await bridge('sandbox-view',{})).leases.some(v=>v.status==='running'))
  passed=true
 }finally{
  await ctx.fiber.dispose();if(previous===undefined)delete process.env.PILOT_MCP_TEST_TOKEN;else process.env.PILOT_MCP_TEST_TOKEN=previous
  // Keep failed diagnostics; successful tests own and can remove this isolated root.
  const state=await bridge('sandbox-view',{}).catch(()=>null);if(passed&&state?.active_count===0)rmSync(root,{recursive:true})
 }
})
