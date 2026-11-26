/** Isolated evaluation only: observe requests that actually pass the scope gate. */
import {isAgentLoopRequest} from '../harness/runtime/node_modules/@deepseek-ai/dsh-llm/lib/index.js'
export const inject=['connection','agents','pilotWorkspace','tokenMeter']
export function apply(ctx,config){
 if(typeof config.root!=='string'||!/(?:\/\.local\/t-|\/\.local\/tmp\/tests\/t-)[^/]+(?:\/|$)/.test(config.root.replaceAll('\\','/')))throw new Error('Private evaluation root required')
 const calls=[]
 ctx.on('llm/stream',async function*(options,next){
  const agent=ctx.agents.currentInitiator(),record={session_id:agent?.id,kind:isAgentLoopRequest(options)?'main':options.purpose==='pilot-business-intent'?'classification':'auxiliary',usage:null,finish:null}
  calls.push(record)
  for await(const chunk of next()){
   if(chunk.type==='usage')record.usage=chunk.usage
   if(chunk.type==='finish')record.finish=chunk.reason.kind
   yield chunk
  }
 })
 ctx.effect(()=>ctx.connection.rpc.handle('/pilot-test-scope',async(op,args,_signal,peer)=>{
  if(op!=='stats'||peer.principal.kind!=='owner')return {ok:false,error:{code:'test/forbidden',message:'Owner stats only',details:{}}}
  const agent=ctx.agents.get(args?.session_id)
  if(!agent)return {ok:false,error:{code:'test/agent',message:'Fixture Agent required',details:{}}}
  return {ok:true,value:{calls:calls.filter(c=>c.session_id===agent.id),route:ctx.pilotWorkspace.routing(agent),context_tokens:ctx.tokenMeter.measure(agent.session,agent.session.requestHeader()).totalTokens}}
 }))
}
