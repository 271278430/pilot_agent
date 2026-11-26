import {readFileSync} from 'node:fs'
export const inject=['pilotWorkspace']
export function apply(ctx,config){
  ctx.on('approval/request', async(request,next)=>{
    if(request.toolName!=='pilot_infra_engine_request_approval')return next()
    let roots
    try{roots=JSON.parse(readFileSync(config.root+'/.local/evaluation-approved-roots.json','utf8')).session_ids}catch{return next()}
    const rootId=ctx.pilotWorkspace.rootId(request.agent)
    if(!roots.includes(rootId))return next()
    const status=await ctx.pilotWorkspace.request('infra-engine-source-status',rootId,'owner',{})
    const c=status.candidates.find(c=>c.build==='passed'&&c.artifact&&c.review_digest&&request.reason.startsWith('Pilot engine artifact '+c.candidate_id+' / SHA256 '+c.review_digest+'\n')&&request.reason.includes('Artifact: '+JSON.stringify(c.artifact)))
    if(!c||c.decision)return 'rejected'
    return 'allowed-once'
  }, {prepend:true})
}
