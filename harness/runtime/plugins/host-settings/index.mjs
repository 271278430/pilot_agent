/** Owner settings coordinator over native credentials, settings and model selections. */
import {Service} from '@deepseek-ai/cordis'
import {credentialRef} from '@deepseek-ai/dsh-credentials'
import {resolveSpec} from '@deepseek-ai/dsh-credentials-local'
import {mkdirSync} from 'node:fs'
import {randomUUID} from 'node:crypto'
import {performance} from 'node:perf_hooks'
import * as C from './config.mjs'
export const name='pilot-host-settings'
export const inject=['connection','pilotWorkspace','settings','credentials','llm','agents','agentPresets','agentDefaultModel','sessionController']
const clone=v=>JSON.parse(JSON.stringify(v))
export class HostSettings extends Service {
  constructor(ctx,config){
    super(ctx,'pilotHostSettings');this.root=config.root;this.busy=false;this.barrier=null;this.broken=C.stored(this.root)?.last_sync?.needs_repair===true
    try{this.composedCompaction=C.compactionSignature(this.config().applied.models)}catch{this.composedCompaction=null}
    // Native picker records the Session choice, then also offers to persist a
    // deployment default. Pilot keeps the latter an explicit settings action.
    const defaults=ctx.agentDefaultModel,original=defaults.saveSelection
    this.persistDefault=original.bind(defaults)
    const save=async selection=>{
      if(selection.provider==='apps'||selection.provider.startsWith('pilot_model_'))return
      return this.persistDefault(selection)
    }
    ctx.effect(()=>{defaults.saveSelection=save;return()=>{if(defaults.saveSelection===save)defaults.saveSelection=original}})
    ctx.on('agent/pre-step',async(event,next)=>{if(this.barrier)await this.barrier;event.signal?.throwIfAborted();if(this.broken)throw new Error('模型配置同步未完成，请在设置中重新同步');
      const saved=C.stored(this.root),s=event.modelSelection,m=saved?.applied.models.find(m=>C.providerOf(m)===s?.provider&&m.model_id===s?.model)
      if(m&&!m.vision&&(event.messages??[]).some(v=>v.content?.some(b=>b.type==='image')))throw new Error('当前模型仅支持文本，请在输入框旁选择支持图片的模型后重新发送。图片不会转发给其他模型。')
      return next()})
    // Native Session selection and expert inheritance are authoritative.
    ctx.on('agent/created',async({agent})=>{if(this.barrier)await this.barrier;const saved=C.stored(this.root);if(saved?.applied_revision>0&&!agent.model.current())agent.model.select(C.selection(saved.applied.model))})
    ctx.effect(()=>ctx.connection.rpc.handle('/pilot-host-settings-api',async(op,payload,signal,peer)=>{
      if(peer.principal.kind!=='owner')return {ok:false,error:{code:'pilot/forbidden',message:'Owner required',details:{}}}
      try {
        if(op==='view')return {ok:true,value:await this.view()}
        if(this.busy)throw new C.SettingError('另一个设置操作正在处理，请稍后再试')
        this.busy=true
        try {
          const args=payload??{}
          if(op==='check-directory')return {ok:true,value:await C.checkRoot(this.root,args.workspace_root,args.create_missing===true)}
          if(op==='check-resource-directory')return {ok:true,value:await C.checkResourceRoot(this.root,args.resource_root,args.create_missing===true)}
          if(op==='save')return {ok:true,value:await this.save(args)}
          if(op==='test-model')return {ok:true,value:await this.test(args,signal)}
          if(op==='sync')return {ok:true,value:await this.sync(args)}
          throw new C.SettingError('不支持的设置操作')
        }finally{this.busy=false}
      }catch(error){return {ok:false,error:{code:'pilot/rejected',message:error instanceof C.SettingError?error.message:'设置操作失败，请检查权限、原生配置状态并重试',details:{}}}}
    }))
  }
  descriptor(){const d=this.ctx.settings.describe({redactSecrets:true}).find(v=>v.ns==='llm-pi-ai');if(!d)throw new C.SettingError('原生模型设置暂不可用');return d}
  current(){
    const selected=this.ctx.agentDefaultModel.currentSelection(),p=this.descriptor().value.providers[selected.provider],m=p?.models.find(v=>v.id===selected.model)
    if(!m)throw new C.SettingError('默认模型不在可用目录中，请修复原生模型设置')
    return {model_id:m.id,base_url:p.baseURL,context_window:m.contextWindow,max_output_tokens:m.maxTokens,auth_required:p.apiKeyEnv!==C.NOAUTH_REF,credential_ref:p.apiKeyEnv??'REPOPILOT_API_KEY',credential_file:resolveSpec(this.ctx.credentials.config??{}).filename,vision:m.input?.includes('image')??false}
  }
  config(){return C.stored(this.root)??C.initial(this.root,this.current())}
  async hasKey(model){return model.auth_required&&(await this.ctx.credentials.describe(credentialRef(model.credential_ref))).configured}
  async view(){
    const value=this.config();let live=null,native={}
    try{live=this.current();native=this.descriptor().value.providers}catch{/* Saved settings must remain editable when the native catalogue needs repair. */}
    const drift=!live||['model_id','base_url','context_window','max_output_tokens','credential_ref','vision'].some(k=>live[k]!==value.applied.model[k])||value.applied.models.filter(m=>m.enabled).some(m=>!C.profileMatches(native[C.providerOf(m)],C.profile(m)))
    const section=async s=>({workspace_root:s.workspace_root,resource_root:s.resource_root,default_model_id:s.default_model_id,model:C.publicModel(s.model,await this.hasKey(s.model)),models:await Promise.all(s.models.map(async m=>C.publicModel(m,await this.hasKey(m))))})
    return {version:2,revision:value.revision,applied_revision:value.applied_revision,desired:await section(value.desired),applied:await section(value.applied),live_model:live?C.publicModel(live,await this.hasKey(live)):null,live_error:live?null:'原生模型目录暂不可用；已保存配置仍可编辑，请重新同步修复。',pending:value.revision!==value.applied_revision||drift||value.last_sync?.passed===false,compaction_restart_required:this.composedCompaction===null||this.composedCompaction!==C.compactionSignature(value.applied.models),last_sync:value.last_sync,busy:this.busy,host_project:this.root}
  }
  revision(value,args){if(args.expected_revision!==value.revision)throw new C.SettingError('配置版本已变化，请刷新后再保存或同步')}
  async save(args){
    const value=this.config();this.revision(value,args)
    if(args.section==='workspace'){
      const checked=await C.checkRoot(this.root,args.workspace_root,args.create_missing===true)
      if(checked.create)mkdirSync(checked.path,{mode:0o700})
      value.desired.workspace_root=checked.path
    }else if(args.section==='resources'){
      const checked=await C.checkResourceRoot(this.root,args.resource_root,args.create_missing===true)
      if(checked.create)mkdirSync(checked.path,{mode:0o700})
      value.desired.resource_root=checked.path
    }else if(args.section==='model'){
      const input=C.configuredModel(args.model),id=input.config_id??(args.add_new?randomUUID().replaceAll('-',''):value.desired.default_model_id),old=value.desired.models.find(m=>m.config_id===id)
      if(!old&&!args.add_new)throw new C.SettingError('模型配置不存在，请刷新')
      if(!old&&value.desired.models.length>=16)throw new C.SettingError('最多登记16个模型')
      if(!input.enabled&&id===value.desired.default_model_id)throw new C.SettingError('请先选择另一个默认模型')
      let ref=old?.credential_ref
      if(!input.auth_required){ref=C.NOAUTH_REF;await this.ctx.credentials.set(credentialRef(ref),'pilot-local-no-auth')}
      else if(input.api_key){ref='PILOT_MODEL_KEY_'+randomUUID().replaceAll('-','').toUpperCase();await this.ctx.credentials.set(credentialRef(ref),input.api_key)}
      else if(!old||!await this.hasKey(old))throw new C.SettingError('请填写API Key，或选择无需Key的服务')
      if(old&&input.auth_required&&!input.api_key&&input.base_url!==old.base_url&&!input.confirm_saved_key)throw new C.SettingError('新地址需要确认使用旧Key，或填写新的Key')
      const {api_key,confirm_saved_key,...publicInput}=input
      const model={...publicInput,config_id:id,credential_ref:ref,credential_file:resolveSpec(this.ctx.credentials.config??{}).filename}
      value.desired.models=old?value.desired.models.map(m=>m.config_id===id?model:m):[...value.desired.models,model];C.projectDefault(value.desired)
    }else if(args.section==='default-model'){
      value.desired.default_model_id=args.config_id;C.projectDefault(value.desired)
    }else if(args.section==='remove-model'){
      if(args.config_id===value.desired.default_model_id)throw new C.SettingError('默认模型不能删除')
      if(!value.desired.models.some(m=>m.config_id===args.config_id))throw new C.SettingError('模型配置不存在')
      value.desired.models=value.desired.models.filter(m=>m.config_id!==args.config_id);C.projectDefault(value.desired)
    }else throw new C.SettingError('请选择工作区或模型配置')
    value.revision++;C.write(this.root,value);return this.view()
  }
  async test(args,signal){
    const input=C.configuredModel(args.model),value=this.config(),saved=value.desired.models.find(m=>m.config_id===(input.config_id??(args.add_new?'':value.desired.default_model_id)))
    let key=input.api_key
    if(input.auth_required&&!key){
      if(!saved?.auth_required)throw new C.SettingError('请填写API Key')
      if(input.base_url!==saved.base_url&&!input.confirm_saved_key)throw new C.SettingError('测试新地址前，请确认使用已保存的Key')
      key=(await this.ctx.credentials.resolve(credentialRef(saved.credential_ref)))?.value
      if(!key)throw new C.SettingError('API Key尚未配置')
    }
    if(args.test_image&&!input.vision)throw new C.SettingError('请先声明此模型支持图片')
    const image='iVBORw0KGgoAAAANSUhEUgAAACAAAAAgCAIAAAD8GO2jAAAAKElEQVR4nO3NsQ0AAAzCMP5/un0CNkuZ41wybXsHAAAAAAAAAAAAxR4yw/wuPL6QkAAAAABJRU5ErkJggg=='
    const begin=performance.now(),body={model:input.model_id,messages:[{role:'user',content:args.test_image?[{type:'text',text:'描述图片颜色，简短回答。'},{type:'image_url',image_url:{url:'data:image/png;base64,'+image}}]:'回复OK'}],max_tokens:64,stream:false,...input.model_id.startsWith('glm')?{thinking:{type:input.reasoning==='off'?'disabled':input.model_id.startsWith('glm-5.3')?'enabled':'disabled'},...input.model_id.startsWith('glm-5.3')&&input.reasoning!=='off'?{reasoning_effort:'low'}:{}}:{}}
    try {
      const response=await fetch(input.base_url+'/chat/completions',{method:'POST',redirect:'manual',headers:{'Content-Type':'application/json',...(input.auth_required?{Authorization:'Bearer '+key}:{})},body:JSON.stringify(body),signal:AbortSignal.any([AbortSignal.timeout(20000),...(signal?[signal]:[])])})
      const result={test_kind:args.test_image?'image-request':'text-request',http_status:response.status,latency_ms:Math.round(performance.now()-begin)}
      if(response.status>=300&&response.status<400){await response.body?.cancel();return {...result,passed:false,error_category:'redirect',message:'接口发生重定向，未转发Key；请填写最终API地址'}}
      const reader=response.body.getReader(),decoder=new TextDecoder();let text='',bytes=0
      for(;;){const {done,value}=await reader.read();if(done)break;bytes+=value.byteLength;if(bytes>65536){await reader.cancel();throw new Error('bounded response')};text+=decoder.decode(value,{stream:true})}
      text+=decoder.decode()
      let data;try{data=JSON.parse(text)}catch{if(response.ok)throw new Error('invalid response')}
      if(!response.ok)return {...result,passed:false,...C.probeFailure(input,response.status,data)}
      const message=data.choices?.[0]?.message,passed=!!message&&(typeof message.content==='string'||typeof message.reasoning_content==='string')
      const count=v=>Number.isSafeInteger(v)&&v>=0&&v<10000000?v:null
      return {passed,test_kind:args.test_image?'image-request':'text-request',http_status:200,latency_ms:Math.round(performance.now()-begin),usage:{input_tokens:count(data.usage?.prompt_tokens),output_tokens:count(data.usage?.completion_tokens)},message:passed?args.test_image?'图片请求已被接口接受；不代表识图质量已通过评测':'认证与模型请求成功；仅验证基础连接，工具与业务能力由任务验收':'响应格式不符合Chat Completions协议'}
    }catch{return {passed:false,latency_ms:Math.round(performance.now()-begin),message:'连接或响应解析失败，请检查网络、地址与服务协议'}}
  }
  async sync(args){
    const value=this.config();this.revision(value,args)
    for(const m of value.desired.models.filter(m=>m.enabled)){const {credential_ref,credential_file,...input}=m;C.configuredModel(input)}
    if(!this.ctx.agents.list().every(a=>a.status==='idle'))throw new C.SettingError('还有会话或专家正在执行，请暂停并空闲后同步')
    const state=await this.ctx.pilotWorkspace.request('settings-preflight','','owner',{})
    if(!state.idle)throw new C.SettingError('还有执行中或尚未确认结束的任务，请先暂停或恢复核对再同步')
    await C.checkRoot(this.root,value.desired.workspace_root)
    await C.checkResourceRoot(this.root,value.desired.resource_root,true)
    const before=this.descriptor(),oldDefault=this.ctx.agentDefaultModel.currentSelection(),agents=this.ctx.agents.list().map(a=>({agent:a,selection:a.model.current()})),next=C.selection(value.desired.model)
    const modelChanged=JSON.stringify(value.desired.models)!==JSON.stringify(value.applied.models)||value.desired.default_model_id!==value.applied.default_model_id||value.desired.models.some(m=>m.enabled&&!C.profileMatches(before.value.providers[C.providerOf(m)],C.profile(m)))
    const remaps=agents.map(row=>{const old=value.applied.models.find(m=>C.providerOf(m)===row.selection?.provider),chosen=old&&value.desired.models.find(m=>m.config_id===old.config_id)
      if(old&&!chosen?.enabled)throw new C.SettingError('有会话或专家仍选择此模型，请先在该会话切换模型再停用或删除')
      return {...row,next:chosen&&(row.selection.model!==chosen.model_id||old.reasoning!==chosen.reasoning)?C.selection(chosen):row.selection}})
    let stage='admission',release;this.barrier=new Promise(resolve=>release=resolve)
    try {
      if(!this.ctx.agents.list().every(a=>a.status==='idle'))throw new C.SettingError('会话状态已变化，请暂停并空闲后同步')
      await C.prepareResourceRoot(this.root,value.desired.resource_root)
      if(modelChanged){
        stage='provider-update'
        await this.ctx.settings.update(before.ns,{providers:C.providers(value.desired,before.value.providers)},before.revision)
        stage='model-resolution'
        for(const m of value.desired.models.filter(m=>m.enabled))await this.ctx.llm.resolveCallConfig(C.selection(m))
        stage='default-save'
        await this.persistDefault(next)
        stage='session-selection'
        for(const {agent,selection,next:chosen} of remaps)if(chosen&&JSON.stringify(chosen)!==JSON.stringify(selection))agent.model.select(chosen)
      }
      value.applied=clone(value.desired);value.applied_revision=value.revision;value.last_sync={passed:true,at:new Date().toISOString()};C.write(this.root,value);this.broken=false
    }catch(error) {
      try {
        const unchanged=JSON.stringify(this.descriptor().value)===JSON.stringify(before.value)&&JSON.stringify(this.ctx.agentDefaultModel.currentSelection())===JSON.stringify(oldDefault)&&agents.every(({agent,selection})=>JSON.stringify(agent.model.current())===JSON.stringify(selection))
        if(!unchanged){await this.ctx.settings.replace(before.ns,before.value);await this.persistDefault(oldDefault);for(const {agent,selection} of agents)if(selection)agent.model.select(selection)}
        this.broken=false
      }catch{this.broken=true}
      value.last_sync={passed:false,needs_repair:this.broken,stage,error_code:error.code??error.name,at:new Date().toISOString()};C.write(this.root,value)
      throw new C.SettingError(this.broken?'同步未完成，请重新同步修复；新模型请求已阻止':'同步失败，已恢复原生模型配置；请检查后重试')
    }finally{release();this.barrier=null}
    return this.view()
  }
}
export function apply(ctx,config){return new HostSettings(ctx,config)}
