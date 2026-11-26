/** Versioned public desired/applied settings. Credentials remain in the native store. */
import {existsSync,lstatSync,readFileSync,mkdirSync,writeFileSync,renameSync,realpathSync} from 'node:fs'
import {access,lstat,realpath} from 'node:fs/promises'
import {constants} from 'node:fs'
import {resolve,isAbsolute,dirname,basename,relative,sep} from 'node:path'
import {randomUUID,createHash} from 'node:crypto'
import {fullyQualified} from '@deepseek-ai/dsh-host-directory-picker-browse'
export class SettingError extends Error{}
const fail=m=>{throw new SettingError(m)}
export const NOAUTH_REF='PILOT_MODEL_NOAUTH'
export function pathOf(root){return resolve(root,'.local/host-settings.json')}
export function stored(root){
  const path=pathOf(root);if(!existsSync(path))return null
  if(lstatSync(path).isSymbolicLink()||lstatSync(dirname(path)).isSymbolicLink())fail('设置文件路径异常')
  const value=JSON.parse(readFileSync(path,'utf8'))
  if(![1,2].includes(value.version)||!Number.isInteger(value.revision)||!Number.isInteger(value.applied_revision)||value.applied_revision<0||value.applied_revision>value.revision||!value.desired||!value.applied)fail('设置文件格式无效')
  for(const section of [value.desired,value.applied]){
    if(!fullyQualified(section.workspace_root))fail('设置文件格式无效')
    section.resource_root??=resolve(root,'resources')
    if(!fullyQualified(section.resource_root))fail('资源目录格式无效')
    const models=value.version===1?[section.model]:section.models
    if(!Array.isArray(models)||!models.length||models.length>16)fail('模型目录需保留1–16项')
    const ids=new Set()
    for(const m of models){
      if(!m||typeof m.credential_ref!=='string'||!/^(REPOPILOT_API_KEY|PILOT_MODEL_(NOAUTH|KEY_[A-F0-9]{32}))$/.test(m.credential_ref)||!fullyQualified(m.credential_file))fail('设置文件格式无效')
      const {credential_ref,credential_file,...input}=m;checkedModel(input)
      if(value.version===2){if(!m.config_id||ids.has(m.config_id))fail('模型配置ID重复');ids.add(m.config_id)}
    }
    if(value.version===1){section.models=[{...section.model,config_id:'apps',display_name:section.model.model_id,enabled:true,vision:false,reasoning:'auto'}];section.default_model_id='apps'}
    const selected=section.models.find(m=>m.config_id===section.default_model_id)
    if(!selected?.enabled)fail('默认模型必须启用')
    if(value.version===2&&JSON.stringify(section.model)!==JSON.stringify(selected))fail('默认模型投影不一致')
    section.model=selected
  }
  value.version=2
  return value
}
export function write(root,value){
  const path=pathOf(root),local=dirname(path)
  if(!existsSync(local))mkdirSync(local,{mode:0o700})
  if(lstatSync(local).isSymbolicLink()||(existsSync(path)&&lstatSync(path).isSymbolicLink()))fail('设置文件路径异常')
  const temp=path+'.'+randomUUID()+'.tmp';writeFileSync(temp,JSON.stringify(value,null,2),{mode:0o600,flag:'wx'});renameSync(temp,path)
}
export function appliedRoot(root){return stored(root)?.applied.workspace_root??null}
export function checkedModel(input){
  if(!input||typeof input!=='object'||Array.isArray(input))fail('请填写模型配置')
  const fields=['model_id','base_url','context_window','max_output_tokens','auth_required','api_key','confirm_saved_key','config_id','display_name','enabled','vision','reasoning']
  if(Object.keys(input).some(k=>!fields.includes(k)))fail('模型配置包含不支持的字段')
  if(typeof input.model_id!=='string'||!input.model_id.trim()||input.model_id.length>200||/[\x00-\x20\x7f]/.test(input.model_id))fail('请填写有效的模型ID')
  let url;try{url=new URL(input.base_url)}catch{fail('请填写完整的HTTP(S)接口地址')}
  if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.search||url.hash)fail('接口地址不能包含账号、Key、查询参数或片段')
  const base_url=url.href.replace(/\/$/,'').replace(/\/(chat\/completions|models)$/,'')
  const context_window=input.context_window,max_output_tokens=input.max_output_tokens
  if(!Number.isInteger(context_window)||context_window<2048||context_window>2000000)fail('上下文窗口需在2048–2000000之间')
  if(!Number.isInteger(max_output_tokens)||max_output_tokens<64||max_output_tokens>32768||max_output_tokens>=context_window)fail('最大输出需为64–32768，且小于上下文窗口')
  if(typeof input.auth_required!=='boolean')fail('请选择是否需要API Key')
  if(input.config_id!==undefined&&!/^(apps|[a-f0-9]{32})$/.test(input.config_id))fail('模型配置ID无效')
  if(input.display_name!==undefined&&(typeof input.display_name!=='string'||!input.display_name.trim()||input.display_name.length>80||/[\x00-\x1f\x7f]/.test(input.display_name)))fail('模型名称需为1–80字')
  for(const field of ['enabled','vision'])if(input[field]!==undefined&&typeof input[field]!=='boolean')fail('模型能力配置无效')
  if(input.reasoning!==undefined&&!['auto','off','low'].includes(input.reasoning))fail('推理方式无效')
  const key=input.api_key??'';if(typeof key!=='string'||key.length>8192||/[\r\n\0]/.test(key))fail('API Key格式无效')
  return {model_id:input.model_id,base_url,context_window,max_output_tokens,auth_required:input.auth_required,api_key:key,confirm_saved_key:input.confirm_saved_key===true,...input.config_id?{config_id:input.config_id}:{},display_name:input.display_name??input.model_id,enabled:input.enabled??true,vision:input.vision??false,reasoning:input.reasoning??'auto'}
}
export function isZhipuEndpoint(model){return new URL(model.base_url).hostname==='open.bigmodel.cn'}
export function configuredModel(input){
  const model=checkedModel(input)
  // Keep structurally valid historical drafts readable, but reject unusable
  // public-provider settings before saving, testing or applying them.
  if(isZhipuEndpoint(model)){
    if(/^\d+$/.test(model.model_id))fail('模型ID不是列表序号。GLM-5.3-Flash的官方模型ID是glm-5.3-flash，请按厂商文档填写。')
    if(/^glm-5\.3(?:-|$)/i.test(model.model_id)&&model.reasoning==='off')fail('GLM-5.3系列不能关闭思考，请将“推理方式”改为“兼容现有配置”或“推理模型”。')
  }
  return model
}
export function probeFailure(model,status,data){
  const zhipu={
    '1000':['authentication','身份验证失败，请检查此模型保存的Key；已保存不代表Key有效。'],
    '1001':['authentication','接口未收到认证信息，请检查认证配置。'],
    '1003':['authentication','认证Token已过期，请更新此模型的Key。'],
    '1005':['authentication','账号启用了二次认证，请按平台要求完成认证。'],
    '1113':['quota','账户已欠费，请检查标准API余额或所用资源包。'],
    '1210':['parameters','请求参数有误，请检查模型ID、图片能力与推理方式。'],
    '1211':['model','模型不存在，请填写厂商的模型代码，而不是显示名称或列表序号。'],
    '1212':['protocol','模型不支持当前调用方式，请核对Chat Completions接口。'],
    '1214':['parameters','请求参数非法，请按模型文档核对图片能力和推理方式。'],
    '1220':['api-permission','平台拒绝访问当前API。错误模型ID也可能触发此码，请先核对厂商模型代码，再检查Key产品类型、模型权限和Base URL；不能直接判定Key无效。'],
    '1221':['protocol','当前API已下线，请按厂商文档更新接口地址。'],
    '1222':['protocol','当前API不存在，请检查Base URL。'],
    '1261':['context','输入超出模型上下文限制。'],
    '1301':['content','接口因内容检查拒绝请求，请核对平台规则。'],
    '1302':['rate-limit','账户请求速率达到限制，请稍后重试。'],
    '1305':['rate-limit','模型当前繁忙，请稍后重试。'],
    '1308':['quota','已达到平台使用上限，请在平台查看额度恢复时间。'],
    '1309':['quota','Coding Plan套餐已到期，请检查套餐状态。'],
    '1310':['quota','已达到套餐每周或每月使用上限。'],
    '1311':['model-permission','当前套餐未开放此模型，请检查套餐的模型权限。'],
    '1314':['quota','企业套餐已失效，请联系企业管理员。'],
    '1315':['api-permission','此Key仅限企业编程套餐，请核对Key产品类型与相应端点。'],
  }
  const openai={invalid_api_key:['authentication','API Key无效，请检查此模型的凭据。'],model_not_found:['model','模型不存在或当前Key无权访问，请核对模型ID与权限。'],insufficient_quota:['quota','平台额度不足，请检查账户或套餐额度。'],rate_limit_exceeded:['rate-limit','平台请求速率达到限制，请稍后重试。']}
  const raw=data?.error?.code,code=typeof raw==='string'||Number.isInteger(raw)?String(raw):'',known=isZhipuEndpoint(model)?zhipu[code]:openai[code]
  // Never expose provider message/body, arbitrary codes, Key or redirect URL.
  if(known)return {error_category:known[0],provider_code:code,message:known[1]}
  return {error_category:status===401?'authentication':status===403?'api-permission':status===429?'rate-limit':'http',message:status===401?'认证失败，请检查此模型保存的Key和接口地址':status===403?'接口拒绝访问，请检查此Key的API或模型权限；403不能直接判定为Key无效':status===429?'接口限流或额度不足，请检查平台账户状态':'接口拒绝测试请求，请检查模型ID、地址与Chat Completions协议'}
}
export const providerOf=model=>model.config_id&&model.config_id!=='apps'?'pilot_model_'+model.config_id:'apps'
export function profileMatches(actual,expected){
  if(Array.isArray(expected))return Array.isArray(actual)&&actual.length===expected.length&&expected.every((v,i)=>profileMatches(actual[i],v))
  if(expected&&typeof expected==='object')return actual&&Object.keys(expected).every(k=>profileMatches(actual[k],expected[k]))
  return actual===expected
}
export function projectDefault(section){section.model=section.models.find(m=>m.config_id===section.default_model_id);if(!section.model?.enabled)fail('默认模型必须启用');return section}
export function providers(section,previous={}){
  const result=Object.fromEntries(Object.entries(previous).filter(([k])=>k!=='apps'&&!k.startsWith('pilot_model_')))
  for(const m of section.models)if(m.enabled)result[providerOf(m)]=profile(m)
  return result
}
export function profile(model){
  const required=model.model_id.startsWith('glm-5.3'),glm=model.model_id.startsWith('glm')
  const reasoning=model.reasoning??'auto',efforts=reasoning==='off'?false:reasoning==='low'?{low:'low',high:'high'}:required?{low:'low',high:'high'}:{off:null,low:'low'}
  return {displayName:model.display_name??model.model_id,api:'openai-completions',apiKeyEnv:model.credential_ref,baseURL:model.base_url,timeoutMs:180000,reasoning:reasoning==='auto'?required?'low':'off':reasoning,compat:{supportsDeveloperRole:false,...glm?{thinkingFormat:'zai'}:{}},models:[{id:model.model_id,name:(model.display_name??model.model_id)+(model.vision?' · 图片':' · 文本'),input:model.vision?['text','image']:['text'],contextWindow:model.context_window,maxTokens:model.max_output_tokens,reasoningEfforts:efforts}]}
}
export function selection(model){return {provider:providerOf(model),model:model.model_id,...model.reasoning==='off'?{}:{reasoningEffort:model.reasoning&&model.reasoning!=='auto'?model.reasoning:model.model_id.startsWith('glm-5.3')?'low':'off'}}}
// The official compaction policies are composed at Host startup. Compare only
// fields used by those policies, so credential or display changes do not warn.
export function compactionSignature(models){return JSON.stringify(models.filter(m=>m.enabled!==false).map(m=>[providerOf(m),m.model_id,m.context_window,m.max_output_tokens]).sort((a,b)=>a[0].localeCompare(b[0])))}
export async function checkRoot(project,value,create=false){
  if(typeof value!=='string'||!fullyQualified(value)||value.length>4096)fail('请填写Pilot服务器上的绝对目录路径')
  const path=resolve(value),control=resolve(project,'.local'),part=relative(path,control)
  if(dirname(path)===path||(!isAbsolute(part)&&part!=='..'&&!part.startsWith('..'+sep))||path===control||path.startsWith(control+sep))fail('主目录不能覆盖服务器根目录或Pilot控制与凭据目录')
  if(process.platform!=='win32'&&['/etc','/usr','/proc','/sys','/dev','/var','/boot'].some(p=>path===p||path.startsWith(p+'/')))fail('不能使用宿主系统目录作为工作区')
  const exists=existsSync(path),parent=exists?path:dirname(path)
  try {
    const canonical=await realpath(parent);let current=parent
    for(;;){if((await lstat(current)).isSymbolicLink())fail('主目录及父路径不能包含符号链接');if(dirname(current)===current)break;current=dirname(current)}
    if(!(await lstat(parent)).isDirectory())fail('主目录的父路径不是目录')
    await access(parent,constants.R_OK|constants.W_OK|constants.X_OK)
    if(!exists&&!create)fail('目录尚不存在；可勾选创建缺失目录，父目录必须已存在')
    return {path:exists?canonical:resolve(canonical,basename(path)),exists,create:!exists}
  }catch(error){if(error instanceof SettingError)throw error;fail('目录或父路径不可访问，请检查服务器路径与账号权限')}
}
export async function checkResourceRoot(project,value,create=false){
  const checked=await checkRoot(project,value,create),namespace=createHash('sha256').update(resolve(project)).digest('hex')
  if(checked.path===resolve(project)||checked.path===resolve(project,'harness')||checked.path.startsWith(resolve(project,'harness')+sep)||checked.path===resolve(project,'src')||checked.path.startsWith(resolve(project,'src')+sep))fail('资源目录不能覆盖项目代码与运行时')
  if(checked.exists){
    const {readdirSync}=await import('node:fs'),marker=resolve(checked.path,'.pilot-resources.json')
    if(existsSync(marker)){if(lstatSync(marker).isSymbolicLink()||JSON.parse(readFileSync(marker,'utf8')).owner_namespace!==namespace)fail('资源目录属于其他项目或标记异常')}
    else if(readdirSync(checked.path).length)fail('请选择空的专属目录；现有资源请走登记导入')
  }
  return {...checked,namespace}
}
export async function prepareResourceRoot(project,value){
  const checked=await checkResourceRoot(project,value,true)
  if(checked.create)mkdirSync(checked.path,{mode:0o700})
  const marker=resolve(checked.path,'.pilot-resources.json')
  if(!existsSync(marker))writeFileSync(marker,JSON.stringify({version:1,owner_namespace:checked.namespace}),{flag:'wx',mode:0o600})
  return checked.path
}
export function publicModel(model,hasKey){const {credential_ref,credential_file,...value}=model;return {...value,key_configured:!!hasKey,provider:providerOf(model)}}
export function initial(root,input){const model={...input,config_id:'apps',display_name:input.display_name??input.model_id,enabled:true,vision:input.vision??false,reasoning:input.reasoning??'auto'},section={workspace_root:resolve(root,'runs/harness-inbox'),resource_root:resolve(root,'resources'),model,models:[model],default_model_id:'apps'};return {version:2,revision:0,applied_revision:0,desired:structuredClone(section),applied:structuredClone(section),last_sync:null}}
