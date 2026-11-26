/** Test-only receiver: native maintenance calls need an explicit initiator. */
export const inject=['connection','agents','commands']
export function apply(ctx,config){
 if(typeof config.root!=='string'||!/(?:\/\.local\/t-|\/\.local\/tmp\/tests\/t-)[^/]+(?:\/|$)/.test(config.root.replaceAll('\\','/')))throw new Error('Test-only isolated root required')
 ctx.effect(()=>ctx.connection.rpc.handle('/pilot-test-multimodal',async(op,args,signal,peer)=>{
  if(peer.principal.kind!=='owner')return {ok:false,error:{code:'test/forbidden',message:'Owner required',details:{}}}
  const agent=ctx.agents.get(args?.session_id)
  if(op!=='compact'||!agent||agent.status!=='idle')return {ok:false,error:{code:'test/rejected',message:'Idle live fixture Agent required',details:{}}}
  try{return {ok:true,value:await ctx.agents.withInitiator(agent,()=>ctx.commands.execute(agent,'/compact',[],signal))}}
  catch(error){return {ok:false,error:{code:'test/compact',message:error.message,details:{}}}}
 }))
}
