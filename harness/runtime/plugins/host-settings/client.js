window.__ModuleLoader__.load({id:'@pilot/host-settings',factory:require=>{
 const React=require('react'),h=React.createElement,inject=['slots','connection']
 function apply(ctx){
  const rpc=async(op,payload={})=>{const r=await ctx.connection.rpc.call('/pilot-host-settings-api',op,payload);if(!r.ok)throw new Error(r.error.message);return r.value}
  const draft=m=>({config_id:m.config_id??'apps',display_name:m.display_name??m.model_id,model_id:m.model_id,base_url:m.base_url,context_window:m.context_window,max_output_tokens:m.max_output_tokens,auth_required:m.auth_required,enabled:m.enabled??true,vision:m.vision??false,reasoning:m.reasoning??'auto',api_key:'',confirm_saved_key:false})
  function HostPage(){
   const [data,setData]=React.useState(null),[workspace,setWorkspace]=React.useState(null),[model,setModel]=React.useState(null),[busy,setBusy]=React.useState(false),[error,setError]=React.useState(''),[note,setNote]=React.useState(''),[probe,setProbe]=React.useState(null)
   const alive=React.useRef(true),flight=React.useRef(false),epoch=React.useRef(0)
   const [resources,setResources]=React.useState(null)
   async function refresh(reset=false){const ticket=++epoch.current;try{const value=await rpc('view');if(alive.current&&ticket===epoch.current){setData(value);setWorkspace(old=>reset||!old?{workspace_root:value.desired.workspace_root,create_missing:false}:old);setModel(old=>reset||!old?draft(value.desired.model):old);setResources(old=>reset||!old?{resource_root:value.desired.resource_root??value.host_project+'/resources',create_missing:false}:old);setError('');if(reset){setNote('已重新读取保存的配置');setProbe(null)}}}catch(e){if(alive.current&&ticket===epoch.current)setError(e.message)}}
   React.useEffect(()=>{alive.current=true;refresh();return()=>{alive.current=false;epoch.current++}},[])
   React.useEffect(()=>{if(data&&!resources)setResources({resource_root:data.desired.resource_root??data.host_project+'/resources',create_missing:false})},[data,resources])
   async function act(op,payload={},section){if(flight.current)return;flight.current=true;setBusy(true);setError('');setNote('');try{const result=await rpc(op,{...payload,expected_revision:data.revision});if(!alive.current)return
    if(op==='test-model'){setProbe(result);return}
    if(op==='check-directory'||op==='check-resource-directory'){setNote(result.exists?'目录存在，可读写':'父目录可访问；保存时将创建此目录');return}
    setData(result);setNote(op==='sync'?'已同步，后续请求与新建目录使用已保存配置':'已保存，点击“同步配置”后应用')
    if(section==='model'){setModel(draft(result.desired.models?.find(m=>m.config_id===model.config_id)??result.desired.models?.at(-1)??result.desired.model));setProbe(null)}
    if(section==='remove-model'){setModel(draft(result.desired.model));setProbe(null)}
    if(section==='workspace')setWorkspace({workspace_root:result.desired.workspace_root,create_missing:false})
    if(section==='resources')setResources({resource_root:result.desired.resource_root,create_missing:false})
    if(op==='view')setResources({resource_root:result.desired.resource_root,create_missing:false})
   }catch(e){if(alive.current)setError(e.message)}finally{flight.current=false;if(alive.current)setBusy(false)}}
   const btn=(label,fn,disabled=false,primary=false)=>h('button',{type:'button',onClick:fn,disabled:busy||disabled,className:primary?'infra-primary':''},label)
   if(!data)return h('section',{className:'infra-stack'},h('h2',null,'工作区与模型'),error?h('p',{role:'alert'},error):h('p',null,'正在读取服务器设置…'),btn('重新读取',refresh))
   const models=data.desired.models??[data.desired.model],savedModel=models.find(m=>(m.config_id??'apps')===model?.config_id),baseline=savedModel&&draft(savedModel)
   const changedModel=model&&(!baseline||Object.keys(baseline).some(k=>k==='api_key'?!!model.api_key:k==='confirm_saved_key'?false:model[k]!==baseline[k])),changedRoot=workspace?.workspace_root!==data.desired.workspace_root
   const changedResources=resources&&resources.resource_root!==(data.desired.resource_root??data.host_project+'/resources')
   const syncDisabled=changedModel||changedRoot||changedResources||!data.pending
   const field=(label,name,type='text')=>h('label',null,label,h('input',{type,value:model[name],disabled:busy,autoComplete:'off',spellCheck:false,onChange:e=>{setModel({...model,[name]:type==='number'?Number(e.target.value):e.target.value,confirm_saved_key:false});setProbe(null)}}))
   return h('section',{className:'infra-stack pilot-host-config','data-pilot-host-settings':true},h('h2',null,'工作区与模型'),
    error&&h('p',{role:'alert',className:'infra-error'},error),note&&h('p',{role:'status'},note),
    data.live_error&&h('p',{role:'alert',className:'infra-error'},data.live_error),
    data.compaction_restart_required&&h('p',{role:'status',className:'infra-meta'},'模型已同步；上下文压缩预算仍使用启动时的模型配置。请在任务空闲后重启 Pilot 主控，使新增模型或容量调整完整生效。'),
    h('div',{className:'infra-actions'},h('p',{className:'infra-meta'},'配置版本 '+data.revision+' · '+(data.pending?'有待同步配置':'已同步')+' · 生效版本 '+data.applied_revision),btn('重新读取配置',()=>refresh(true))),
    h('section',{className:'infra-card'},h('h3',null,'工作区主目录'),h('p',null,'原生目录弹窗中的“主目录”位于Pilot所在服务器。已有工作区和会话保持原位置。'),
     h('label',null,'当前生效的完整路径',h('textarea',{value:data.applied.workspace_root,readOnly:true,rows:2,spellCheck:false})),
     h('label',null,'设置新的主目录',h('input',{value:workspace?.workspace_root??'',disabled:busy,onChange:e=>setWorkspace({...workspace,workspace_root:e.target.value})})),
     h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:workspace?.create_missing??false,disabled:busy,onChange:e=>setWorkspace({...workspace,create_missing:e.target.checked})}),'目录不存在时创建（父目录须已存在）'),
     h('div',{className:'infra-actions'},btn('检查目录',()=>act('check-directory',workspace)),btn('保存路径',()=>act('save',{section:'workspace',...workspace},'workspace'),false,true),btn('同步配置',()=>act('sync'),syncDisabled)),
     h('p',{className:'infra-meta'},'只改变新建与浏览目录的默认位置，工作区登记、会话历史和部署制品保持。')),
    h('section',{className:'infra-card'},h('h3',null,'主控本机资源目录'),h('p',null,'此路径属于当前Pilot Host（Windows主控使用本机盘符）。服务器上的权重、运行工作区和构建产物使用各节点独立目录，在“服务器与权限”检查并配置。'),
     h('label',null,'当前生效的资源主目录',h('textarea',{readOnly:true,rows:2,value:data.applied.resource_root??data.host_project+'/resources'})),
     h('label',null,'设置新的资源主目录',h('input',{value:resources?.resource_root??'',disabled:busy,onChange:e=>setResources({...resources,resource_root:e.target.value})})),
     h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:resources?.create_missing??false,disabled:busy,onChange:e=>setResources({...resources,create_missing:e.target.checked})}),'创建缺失的专属目录（父目录须已存在）'),
     h('div',{className:'infra-actions'},btn('检查资源目录',()=>act('check-resource-directory',resources),!resources),btn('保存资源路径',()=>act('save',{section:'resources',...resources},'resources'),!resources,true),btn('同步配置',()=>act('sync'),syncDisabled)),
     h('p',{className:'infra-meta'},'models/：新模型权重；sources/、cache/、exports/：源码、缓存、归档分类目录。任务工作副本和执行根保持独立，本机执行根默认项目/runs/nodes/pilot-executor。远端资源目录在“服务器与权限”单独配置。Docker镜像层由目标节点Docker管理，此处不迁移Docker data-root或修改共享服务。')),
    model&&h('section',{className:'infra-card'},h('h3',null,'Pilot模型目录'),h('p',null,'登记多个OpenAI兼容Chat Completions接口，每个模型有独立Key和容量。在输入框旁选择当前会话模型；新会话使用默认模型，专家继承主控选择。部署目标模型仍在模型与镜像模块中管理。'),
     h('div',{className:'infra-actions'},h('select',{'aria-label':'编辑模型配置',value:model.config_id,disabled:busy||changedModel,onChange:e=>{setModel(draft(models.find(m=>m.config_id===e.target.value)));setProbe(null);setNote('')}},...models.map(m=>h('option',{key:m.config_id??'apps',value:m.config_id??'apps'},(m.display_name??m.model_id)+(m.config_id===data.desired.default_model_id?' · 默认':'')+(m.enabled===false?' · 停用':''))),!savedModel&&h('option',{value:''},'新模型（未保存）')),
       btn('添加模型',()=>{setModel({...draft(data.desired.model),config_id:'',display_name:'',model_id:'',base_url:'',context_window:32768,max_output_tokens:4096,auth_required:true,enabled:true,vision:false,reasoning:'off',api_key:''});setProbe(null)},changedModel||models.length>=16),
       btn('设为默认模型',()=>act('save',{section:'default-model',config_id:model.config_id}),changedModel||!savedModel||!model.enabled||model.config_id===data.desired.default_model_id),
       btn('删除模型配置',()=>act('save',{section:'remove-model',config_id:model.config_id},'remove-model'),changedModel||!savedModel||model.config_id===data.desired.default_model_id)),
     h('p',{className:'infra-meta'},'先保存或重新读取草稿，再切换编辑项。同步不会把其他会话改成默认模型；删除或停用前须在仍选择它的会话中切换。'),
     field('显示名称','display_name'),field('模型ID','model_id'),field('接口Base URL','base_url'),
     h('p',{className:'infra-meta'},'显示名称只用于界面；模型ID会原样发送给API，不是列表序号。例如GLM-5.3-Flash填写glm-5.3-flash。',h('a',{href:'https://docs.bigmodel.cn/cn/guide/models/vlm/glm-5.3-flash',target:'_blank',rel:'noopener noreferrer'},' 查看模型文档')),
     /^\d+$/.test(model.model_id)&&h('p',{role:'status',className:'infra-error'},'当前模型ID只有数字，请确认这确实是服务端的模型代码；智谱模型不能填写列表序号。'),
     h('p',{className:'infra-meta'},'智谱标准模型API：https://open.bigmodel.cn/api/paas/v4；Coding Plan套餐：https://open.bigmodel.cn/api/coding/paas/v4。Key需与产品类型匹配，系统不会自动切换计费端点。',h('a',{href:'https://docs.bigmodel.cn/cn/coding-plan/notice/api-key-management',target:'_blank',rel:'noopener noreferrer'},' 查看Key与端点说明')),
     h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:model.vision,disabled:busy,onChange:e=>{setModel({...model,vision:e.target.checked});setProbe(null)}}),'支持图片输入（请按模型文档声明，并测试图片请求）'),
     h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:model.enabled,disabled:busy||model.config_id===data.desired.default_model_id,onChange:e=>setModel({...model,enabled:e.target.checked})}),'启用，展示在会话模型选择器'),
     h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:model.auth_required,disabled:busy,onChange:e=>{setModel({...model,auth_required:e.target.checked,confirm_saved_key:false});setProbe(null)}}),'服务需要API Key'),
     h('label',null,'API Key',h('input',{type:'password',value:model.api_key,disabled:busy||!model.auth_required,autoComplete:'off',placeholder:savedModel?.key_configured?'已配置；留空保留此模型Key':'输入API Key',onChange:e=>{setModel({...model,api_key:e.target.value});setProbe(null)}})),
     h('p',{className:'infra-meta'},!model.auth_required?'当前服务设为无需Key':savedModel?.key_configured?'此模型已保存Key；界面不回显凭据':'此模型尚未配置Key'),
     model.auth_required&&!model.api_key&&savedModel&&model.base_url!==savedModel.base_url&&h('label',{className:'pilot-host-inline'},h('input',{type:'checkbox',checked:model.confirm_saved_key,disabled:busy,onChange:e=>setModel({...model,confirm_saved_key:e.target.checked})}),'确认使用此模型已保存Key访问新地址'),
     h('details',null,h('summary',null,'模型容量与推理'),field('上下文窗口（token）','context_window','number'),field('最大单次输出（token）','max_output_tokens','number'),h('label',null,'推理方式',h('select',{value:model.reasoning,disabled:busy,onChange:e=>{setModel({...model,reasoning:e.target.value});setProbe(null)}},h('option',{value:'auto'},'兼容现有配置'),h('option',{value:'off'},'普通模型（不发送推理等级）'),h('option',{value:'low'},'推理模型（支持low/high等级）'))),h('p',{className:'infra-meta'},'GLM-5.3系列在官方接口上仅支持启用思考，请使用“兼容现有配置”或“推理模型”。')),
     h('div',{className:'infra-actions'},btn('测试连接',()=>act('test-model',{model:{...model,...!model.config_id?{config_id:undefined}:{}},add_new:!savedModel})),btn('测试图片请求',()=>act('test-model',{model:{...model,...!model.config_id?{config_id:undefined}:{}},add_new:!savedModel,test_image:true}),!model.vision),btn('保存模型配置',()=>act('save',{section:'model',model:{...model,...!model.config_id?{config_id:undefined}:{}},add_new:!savedModel},'model'),false,true),btn('同步配置',()=>act('sync'),syncDisabled)),
     h('p',{className:'infra-meta'},'每次测试仅发送一次最多64输出token的请求，可能产生少量API用量。图片测试验证接口接受请求，不代表识图质量。支持PNG、JPEG、WebP、GIF；音视频及生成图片暂无专门支持。'),
     probe&&h('p',{role:'status',className:probe.passed?'':'infra-error'},(probe.test_kind==='image-request'?'图片请求':'连接')+(probe.passed?'通过：':'未通过：')+probe.message+(probe.http_status?' · HTTP '+probe.http_status:'')+(probe.provider_code?' · 平台码 '+probe.provider_code:'')+' · '+probe.latency_ms+' ms'+(probe.usage?.output_tokens!=null?' · 输出 '+probe.usage.output_tokens+' token':''))),
    h('p',{className:'infra-meta'},'先保存正在编辑的内容，再同步已保存配置。空Key保留原凭据；不用打开配置文件。'))
  }
  ctx.effect(()=>ctx.slots.inject('settings.section',()=>ctx.slots.register({name:'settings.section',id:'pilot-host',label:()=> '工作区与模型',order:35},()=>h(HostPage))))
  ctx.effect(()=>{const style=document.createElement('style');style.textContent='.pilot-host-config{max-width:980px;width:100%;gap:12px}.pilot-host-config h2{font-size:24px;margin:0 0 10px}.pilot-host-config h3{font-size:18px;margin:0 0 12px}.pilot-host-config p{margin:8px 0;line-height:1.65}.pilot-host-config .infra-meta{font-size:12px;color:var(--dsw-alias-text-l2,#737373);overflow-wrap:anywhere}.pilot-host-config button{padding:8px 12px;border:1px solid var(--dsw-alias-border-l3,#ddd);border-radius:8px;background:var(--dsw-alias-bg-l2,#f7f8f8);color:inherit;cursor:pointer;font-size:13px}.pilot-host-config button:hover:enabled{border-color:#397a68;background:#eaf3ef}.pilot-host-config button.infra-primary{background:#226d57;border-color:#226d57;color:white}.pilot-host-config button:disabled{opacity:.45;cursor:default}.pilot-host-config .infra-actions{align-items:center}.pilot-host-config>.infra-actions{justify-content:space-between}.pilot-host-config textarea{resize:none;line-height:1.6;overflow-wrap:anywhere;font-family:monospace;font-size:12px;width:100%;box-sizing:border-box}.pilot-host-config label{display:flex;flex-direction:column;gap:7px;margin:12px 0}.pilot-host-config input,.pilot-host-config textarea{padding:9px;border:1px solid var(--dsw-alias-border-l3,#ddd);border-radius:8px;background:transparent;color:inherit}.pilot-host-config .pilot-host-inline{flex-direction:row;align-items:center}.pilot-host-config .pilot-host-inline input{width:auto}.pilot-host-config .infra-actions{display:flex;gap:8px;flex-wrap:wrap;margin-top:16px}.pilot-host-config .infra-card{padding:20px;border:1px solid var(--dsw-alias-border-l3,#ddd);border-radius:12px;margin:18px 0}';document.head.append(style);return()=>style.remove()})
 }
 return {inject,apply}
}})
