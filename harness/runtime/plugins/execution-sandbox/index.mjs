/** Container lifecycle broker; native MCP and Loop remain their owners. */
import {Service} from '@deepseek-ai/cordis'
import {fileURLToPath} from 'node:url'
export const name='pilot-execution-sandbox'
export const inject=['connection','pilotWorkspace','agents']
export class ExecutionSandbox extends Service {
 constructor(ctx,config){
  super(ctx,'pilotExecutionSandbox');this.root=config.root;this.busy=false
  ctx.effect(()=>ctx.connection.rpc.handle('/pilot-sandbox-api',async(op,args,signal,peer)=>{
   if(peer.principal.kind!=='owner')return {ok:false,error:{code:'pilot/forbidden',message:'Owner required',details:{}}}
   try{
    if(op==='view')return {ok:true,value:await this.call('view',{},signal)}
    if(this.busy||!ctx.agents.list().every(a=>a.status==='idle'))throw new Error('请等待会话和专家空闲后操作执行隔离')
    if(!['configure','self-test','stop'].includes(op))throw new Error('Unsupported sandbox operation')
    this.busy=true
    try{
     const preflight=await ctx.pilotWorkspace.request('settings-preflight','','owner',{})
     if(!preflight.idle)throw new Error('还有执行中或未确认结束的任务')
     return {ok:true,value:await this.call(op,args??{},signal)}
    }finally{this.busy=false}
   }catch(error){return {ok:false,error:{code:'pilot/rejected',message:String(error.message).slice(0,500),details:{}}}}
  }))
 }
 call(op,args={},signal){return this.ctx.pilotWorkspace.request('sandbox-'+op,'','sandbox-broker',args,signal)}
 async prepare(row,scope,cwd,signal){
  if(!row.image)throw new Error('本地MCP必须登记固定容器镜像；不再在宿主直接启动程序')
  const spec=await this.call('prepare',{server_name:row.name,root_session:scope??'schema-discovery',image:row.image,
   command:row.command,args:row.args??[],env_names:Object.keys(row.environmentRefs??{}),cwd:cwd??this.root,
   workspace_read:!!scope&&row.workspaceRead===true,lease_seconds:row.leaseSeconds??900},signal)
  return {...spec,command:this.ctx.pilotWorkspace.config.python,args:[fileURLToPath(new URL('../../../../src/pilot_core/sandbox_stdio.py',import.meta.url))],cwd:this.root}
 }
 stop(spec){return this.call('stop',{id:spec.id})}
}
export function apply(ctx,config){return new ExecutionSandbox(ctx,config)}
