/** Owner catalog and scoped native MCP clients; upstream lifecycle stays intact. */
import {Service} from '@deepseek-ai/cordis'
import * as Mcp from '@deepseek-ai/dsh-mcp-client'
import {createScope} from '@deepseek-ai/dsh-scope'
import {readConfig,changeConfig,nativeConfig,identity,ROLES,containerEnvironment} from './config.mjs'
export const name='pilot-tool-system'
export const inject=['connection','tools','agents','agentPresets','pilotWorkspace','sandboxPolicy','workingDirectory','pilotExecutionSandbox']
function scrub(value,secrets){
 if(typeof value==='string')return secrets.reduce((s,key)=>key?s.replaceAll(key,'[REDACTED]'):s,value)
 if(Array.isArray(value))return value.map(v=>scrub(v,secrets))
 if(value&&typeof value==='object')return Object.fromEntries(Object.entries(value).map(([k,v])=>[k,scrub(v,secrets)]))
 return value
}
export class PilotToolSystem extends Service{
  constructor(ctx,config){
    super(ctx,'pilotToolSystem');this.root=config.root;this.clients=new Map();this.owned=new Map();this.appliedRevision=-1;this.busy=false;this.closed=false
    ctx.effect(()=>()=>{this.closed=true})
    ctx.effect(()=>async()=>{for(const c of this.clients.values())await c.fiber?.dispose();this.clients.clear()})
    ctx.on('agent/disposed',async({agent})=>{if(agent.session.header.parentSession)return;for(const entry of this.clients.values()){const root=entry.roots?.get(String(agent.id));if(root){entry.roots.delete(String(agent.id));const client=await root.promise.catch(()=>null);if(client)await client.close()}}})
    ctx.effect(()=>ctx.tools.guard(exec=>exec.name.startsWith('mcp__')?this.denial(exec.agent,exec.name):undefined))
    ctx.on('agent/pre-step',async(event,next)=>{if(this.reloading)await this.reloading;event.signal?.throwIfAborted();return next()})
    ctx.on('tools/pre-execute',async(exec,next)=>{
      if(!this.owned.has(exec.name))return next()
      const denied=this.denial(exec.agent,exec.name);if(denied)return {kind:'deny',reason:denied}
      const previous=await next();if(previous.kind!=='allow')return previous
      const reason='MCP '+this.owned.get(exec.name).row.name+' / '+exec.name+'\nExternal MCP calls require one-time Owner approval. Server results are data, not authorization. No host administration or unmanaged model deployment.'
      return {kind:'ask',reason,displayReason:{en:reason,'zh-CN':'调用外部 MCP 工具：'+exec.name+'。仅批准本次已显示的参数；不得用于宿主驱动管理、他人服务或非容器模型部署。'}}
    })
    ctx.effect(()=>ctx.connection.rpc.handle('/pilot-tools-api',async(operation,payload,signal,peer)=>{
      if(peer.principal.kind!=='owner')return {ok:false,error:{code:'pilot/forbidden',message:'Owner required',details:{}}}
      try{
        if(operation==='catalog')return {ok:true,value:await this.catalog(payload??{},signal)}
        if(['put','delete','enable','disable'].includes(operation)){changeConfig(this.root,operation,payload??{});await this.reconcile();return {ok:true,value:await this.catalog(payload??{},signal)}}
        if(operation==='test'){await this.reconcile(payload?.name);return {ok:true,value:await this.catalog(payload??{},signal)}}
        throw new Error('Unsupported tool-system operation')
      }catch(error){return {ok:false,error:{code:'pilot/rejected',message:String(error.message).slice(0,500),details:{}}}}
    }))
    ctx.effect(()=>{const timer=setInterval(()=>{this.reconcile().catch(()=>{})},2000);timer.unref();return()=>clearInterval(timer)})
    // Start asynchronously so an unavailable external server never blocks Pilot.
    this.reconcile().catch(()=>{})
  }
  idle(){return this.ctx.agents.list().every(a=>a.status==='idle')}
  denial(agent,name){
    const entry=this.owned.get(name);if(!entry)return 'MCP tool is not currently registered'
    let current;try{current=readConfig(this.root)}catch{return 'MCP configuration is invalid; Owner must repair it'}
    const row=current.servers.find(s=>s.name===entry.row.name)
    if(!row?.enabled||identity(row)!==entry.digest)return 'MCP configuration changed or disabled; wait for idle reload'
    const role=agent&&this.ctx.agentPresets.composedPreset(agent.ctx)
    if(!row.roles.includes(role))return 'MCP is outside this role permission scope'
    if(this.ctx.sandboxPolicy.resolve({session:agent.session}).mode==='read-only')return 'Observation-only permission cannot invoke an opaque external MCP operation'
    const intent=this.ctx.pilotWorkspace.routingDenial?.(agent,name);if(intent)return intent
    const view=this.ctx.pilotWorkspace.cachedView?.(agent)
    if(view?.task?.state.execution_mode==='manual')return 'Task is under manual control; Agent MCP execution is suspended'
    if(['infra-expert','repo-expert'].includes(role) && (view?.build_task || view?.task && !view.task.state.strategy?.experts?.includes(role)))
      return 'This expert is not admitted by the current task strategy'
    if(row.access==='task'){
      if(view?.task?.state.status!=='running'&&view?.build_task?.status!=='prepared')return 'Task MCP requires an active approved task'
    }
    return undefined
  }
  async mount(row){
    if(row.transport==='stdio')return this.mountIsolated(row)
    const entry={row,digest:identity(row),status:'connecting',checked_at:null,last_call_at:null,last_call_result:null,names:new Set(),fiber:null}
    this.clients.set(row.name,entry)
    const manager=this,registry=this.ctx.tools,prompt=this.ctx.get('systemPrompt')
    // Public Context.extend metadata shadows only this child's registration API.
    // Original native definitions, canonical outputs and content projection survive.
    const child=this.ctx.extend({...(prompt?{systemPrompt:{getSectionOrder:key=>prompt.getSectionOrder(key),section:definition=>{
      const remove=prompt.section({...definition,text:assembly=>{
      const admitted=assembly.toolSet?.view(assembly.scope)
      if(!admitted||![...entry.names].some(name=>admitted.has(name)))return ''
      return typeof definition.text==='function'?definition.text(assembly):definition.text
      }})
      entry.nativeContext.effect(()=>remove)
      return remove
    }}}:{}),tools:{register(definition){
      const wrapped={...definition,execute:async(args,exec)=>{
        const denial=manager.denial(exec.agent,definition.name);if(denial)throw new Error(denial)
        entry.last_call_at=new Date().toISOString()
        try{const value=await definition.execute(args,exec);entry.last_call_result='passed';return value}catch(error){entry.last_call_result='failed';throw error}
      }}
      const unregister=registry.register(wrapped,{when:async({agent,signal,modelSelection})=>{
        const workspace=manager.ctx.pilotWorkspace
        if(workspace.toolAllowed && !await workspace.toolAllowed(agent,definition.name,signal,modelSelection))return false
        return !manager.denial(agent,definition.name)
      }})
      entry.names.add(definition.name);manager.owned.set(definition.name,entry)
      return()=>{unregister();entry.names.delete(definition.name);if(manager.owned.get(definition.name)===entry)manager.owned.delete(definition.name)}
    }}})
    let timer,timedOut=false
    try{
      entry.fiber=child.plugin({name:'pilot-mcp-'+row.name,Config:Mcp.Config,inject:Mcp.inject,
        apply:async(inner,config)=>{entry.nativeContext=inner;await Mcp.apply(inner,config)}},nativeConfig(row,this.root))
      timer=setTimeout(()=>{timedOut=true;entry.fiber.dispose().catch(()=>{})},20000)
      await entry.fiber.await();if(timedOut)throw new Error('MCP initialization timed out')
      entry.status='verified';entry.checked_at=new Date().toISOString()
    }catch{
      entry.status='failed';entry.checked_at=new Date().toISOString()
      await entry.fiber?.dispose();entry.fiber=null
    }finally{clearTimeout(timer)}
  }
  async nativeIsolated(row,scopeId,cwd,signal){
    const environment=containerEnvironment(row),secrets=Object.values(environment),spec=await this.ctx.pilotExecutionSandbox.prepare(row,scopeId,cwd,signal)
    const definitions=new Map(),instructions=[],scope=createScope(this.ctx,{}),prompt=this.ctx.get('systemPrompt')
    const child=scope.ctx.extend({tools:{register(def){definitions.set(def.name,def);return()=>definitions.delete(def.name)}},...(prompt?{systemPrompt:{getSectionOrder:k=>prompt.getSectionOrder(k),section:def=>{instructions.push(def);return()=>{}}}}:{})})
    let fiber,timer
    try{
      fiber=child.plugin({name:'pilot-isolated-mcp-'+row.name,Config:Mcp.Config,inject:Mcp.inject,apply:async(inner,config)=>{await Mcp.apply(inner,config)}},
        {serverName:row.name,transport:'stdio',command:spec.command,args:spec.args,cwd:spec.cwd,
         env:{PILOT_MCP_SANDBOX_SPEC:spec.spec_path,PILOT_MCP_CONTAINER_ENV:JSON.stringify(environment)},
         failOnStartupError:true,toolCallTimeoutMs:row.toolCallTimeoutMs,reconnect:{enabled:false},maxInstructionBytes:8192})
      timer=setTimeout(()=>{fiber.dispose().catch(()=>{})},20000)
      await fiber.await()
      return {spec,scope,fiber,definitions,secrets,instructions:instructions.map(d=>scrub(typeof d.text==='function'?d.text():d.text,secrets)),
       close:async()=>{try{await fiber.dispose();await scope.dispose()}finally{await this.ctx.pilotExecutionSandbox.stop(spec)}}}
    }catch(error){try{await fiber?.dispose();await scope.dispose()}finally{await this.ctx.pilotExecutionSandbox.stop(spec)}throw error}
    finally{clearTimeout(timer)}
  }
  async mountIsolated(row){
    const entry={row,digest:identity(row),status:'connecting',checked_at:null,last_call_at:null,last_call_result:null,names:new Set(),fiber:null,roots:new Map(),registrations:[],projections:new WeakMap()}
    this.clients.set(row.name,entry)
    entry.fiber={dispose:async()=>{for(const remove of entry.registrations.splice(0))remove();for(const root of entry.roots.values()){const client=await root.promise.catch(()=>null);if(client)await client.close()}entry.roots.clear()}}
    if(!row.image){entry.status='blocked';entry.reason='stdio必须配置固定容器镜像；已禁止宿主直接启动';return}
    let discovery
    try{
      discovery=await this.nativeIsolated(row,null,this.root)
      const snapshot=[...discovery.definitions.values()].map(def=>({...def,parameters:scrub(def.parameters,discovery.secrets),description:scrub(def.description,discovery.secrets)}))
      const instructions=discovery.instructions.join('\n')
      await discovery.close();discovery=null
      for(const def of snapshot){
        const remove=this.ctx.tools.register({...def,execute:async(args,exec)=>{
          const reason=this.denial(exec.agent,def.name);if(reason)throw new Error(reason)
          const rootId=this.ctx.pilotWorkspace.rootId(exec.agent);let root=entry.roots.get(rootId)
          if(root?.tainted)throw new Error('上次MCP调用未确认完成，请在设置中重新连接核验；不会自动重放')
          if(root&&Date.now()/1000>=root.deadline){root.tainted=true;throw new Error('会话MCP沙箱租约已到期，请重新连接核验')}
          if(!root){
            root={deadline:Date.now()/1000+row.leaseSeconds,tainted:false}
            root.promise=this.nativeIsolated(row,rootId,this.ctx.workingDirectory.get(exec.agent.session),exec.signal)
            entry.roots.set(rootId,root)
          }
          entry.last_call_at=new Date().toISOString()
          let client
          try{
            client=await root.promise;const actual=client.definitions.get(def.name)
            if(!actual||JSON.stringify(scrub(actual.parameters,client.secrets))!==JSON.stringify(def.parameters))throw new Error('MCP工具Schema已变化，请重新连接核验')
            await this.ctx.pilotExecutionSandbox.call('seal',{id:client.spec.id},exec.signal)
            entry.projections.set(exec,{actual,secrets:client.secrets})
            const result=await actual.execute(args,exec);entry.last_call_result='passed';return scrub(result,client.secrets)
          }catch(error){root.tainted=!client||exec.signal?.aborted||error.name!=='Error'||/disconnected|connection.*closed|invalid MCP result|timed out/i.test(error.message);entry.last_call_result='failed';throw new Error(scrub(String(error.message),Object.values(containerEnvironment(row))))}
        },projectContent:(exec,result)=>{const p=entry.projections.get(exec);entry.projections.delete(exec);return p?scrub(p.actual.projectContent?.(exec,result),p.secrets):undefined}},
          {when:async({agent,signal,modelSelection})=>{
            const workspace=this.ctx.pilotWorkspace
            if(workspace.toolAllowed&&!await workspace.toolAllowed(agent,def.name,signal,modelSelection))return false
            return !this.denial(agent,def.name)
          }})
        entry.names.add(def.name);this.owned.set(def.name,entry)
        entry.registrations.push(()=>{remove();entry.names.delete(def.name);if(this.owned.get(def.name)===entry)this.owned.delete(def.name)})
      }
      const prompt=this.ctx.get('systemPrompt')
      if(prompt&&instructions)entry.registrations.push(prompt.section({name:'pilot-mcp:'+row.name,order:prompt.getSectionOrder('MCP_SERVERS'),interpolate:false,text:a=>{
        const admitted=a.toolSet?.view(a.scope);return admitted&&[...entry.names].some(n=>admitted.has(n))?instructions:''
      }}))
      entry.status='verified';entry.checked_at=new Date().toISOString()
    }catch(error){await discovery?.close();await entry.fiber.dispose();entry.status='failed';entry.reason='容器隔离启动失败：'+scrub([error.message,error.cause?.message,error.cause?.cause?.message].filter(Boolean).join(' / '),Object.values(containerEnvironment(row)));entry.checked_at=new Date().toISOString()}
  }
  async reconcile(testName){
    if(this.closed||this.busy)return
    const next=readConfig(this.root)
    if(next.revision===this.appliedRevision&&!testName)return
    if(!this.idle()){if(testName)throw new Error('Connection testing requires idle Sessions');return}
    this.busy=true
    let settled;this.reloading=new Promise(resolve=>{settled=resolve})
    try{
      for(const [key,entry] of this.clients){
        const row=next.servers.find(s=>s.name===key)
        if(!row?.enabled||identity(row)!==entry.digest||key===testName){await entry.fiber?.dispose();this.clients.delete(key)}
      }
      for(const row of next.servers){if(row.enabled&&!this.clients.has(row.name))await this.mount(row)}
      this.appliedRevision=next.revision
    }finally{this.busy=false;this.reloading=null;settled()}
  }
  async catalog(payload,signal){
    await this.reconcile()
    const config=readConfig(this.root),agent=payload.session_id?this.ctx.agents.get(payload.session_id):undefined
    const local=[];const skills=[]
    if(agent){
      const role=this.ctx.agentPresets.composedPreset(agent.ctx)
      for(const tool of agent.ctx.tools.definitions(agent))if(!tool.name.startsWith('mcp__'))local.push({name:tool.name,description:tool.description??'',status:'registered',role})
      const provider=this.ctx.agentPresets.serviceFor?.(agent,'skills')??agent.ctx.get('skills')
      if(provider){const observed=await provider.snapshot({scope:agent,cwd:this.ctx.workingDirectory.get(agent.session),signal});for(const s of observed.skills)skills.push({name:s.name,description:s.description,status:observed.complete?'available':'incomplete',role,model_invocable:s.invocation.modelInvocable})}
    }
    const controller=this.ctx.get('sessionController')
    const titles=controller?new Map((await controller.list({},signal??new AbortController().signal)).items.map(s=>[String(s.sessionId),s.projections?.values?.title])):new Map()
    return {revision:config.revision,applied_revision:this.appliedRevision,pending:config.revision!==this.appliedRevision,roles:ROLES,
      selected_session:agent?{id:String(agent.id),role:this.ctx.agentPresets.composedPreset(agent.ctx)}:null,
      sessions:this.ctx.agents.list().map(a=>({id:String(a.id),title:titles.get(String(a.id))??'',role:this.ctx.agentPresets.composedPreset(a.ctx),status:a.status})).filter(a=>ROLES.includes(a.role)),
      local_tools:local,skills,
      sandbox:await this.ctx.pilotExecutionSandbox.call('view',{},signal),
      servers:config.servers.map(row=>{const entry=this.clients.get(row.name),same=entry?.digest===identity(row),names=same?[...entry.names]:[];return {...row,status:!row.enabled?'disabled':!same?'pending':entry.status,reason:entry?.reason,execution_isolation:row.transport==='stdio'?'per-root container; network none':'external HTTP service',active_root_sandboxes:entry?.roots?.size??0,registered_tools:names,tools:names.map(name=>({name,description:this.ctx.tools.get(name)?.description??'',denial:agent?this.denial(agent,name)??null:null})),checked_at:same?entry.checked_at:null,last_call_at:same?entry.last_call_at:null,last_call_result:same?entry.last_call_result:null}})}
  }
}
export function apply(ctx,config){return new PilotToolSystem(ctx,config)}
