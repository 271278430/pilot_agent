import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const nodes=t=>Array.isArray(t)?t.flatMap(nodes):!t||typeof t!=='object'?[]:[t,...t.children.flatMap(nodes)]
const text=t=>Array.isArray(t)?t.map(text).join(' '):typeof t==='string'?t:t&&typeof t==='object'?t.children.map(text).join(' '):''
const settle=async()=>{for(let i=0;i<30;i++)await Promise.resolve()}
function fixture(handler,component,props={}){
 const values=[],effects=[],calls=[],downloads=[];let cursor=0,plugin,components
 const React={createElement:(type,props,...children)=>({type,props:props??{},children}),useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=typeof v==='function'?v(values[i]):v}]},useRef:initial=>{const i=cursor++;return values[i]??={current:initial}},useEffect:(fn,deps)=>{const i=cursor++;if(!values[i]||deps.some((v,j)=>v!==values[i][j])){values[i]=deps;effects.push(fn)}},useSyncExternalStore:(_s,read)=>read()}
 const code=readFileSync(new URL('../harness/runtime/plugins/infra-domain/client.js',import.meta.url),'utf8').replace('function BuildWorkbench(', 'window.capture({SoftwareRecipeForm,DockerControl,NetworkWorkbench,DistributedWorkbench,BuildWorkbench,Resources});function BuildWorkbench(')
 vm.runInNewContext(code,{window:{__ModuleLoader__:{load:p=>{plugin=p}},capture:v=>{components=v}},document:{addEventListener(){},removeEventListener(){},createElement:()=>({remove(){},click(){downloads.push({filename:this.download,href:this.href})}}),head:{append(){}}},crypto:globalThis.crypto,setInterval:()=>1,clearInterval(){},setTimeout:fn=>fn(),atob:globalThis.atob,Uint8Array,Date,Blob,URL})
 plugin.factory(()=>React).apply({effect:fn=>fn(),slots:{inject:(_s,fn)=>fn(),register(){}},sidebarRightTabs:{register(){}},uiConversation:{events:{register(){}}},connection:{rpc:{call:async(_p,op,payload)=>{calls.push({op,payload});return handler(op,payload)}}}})
 return {calls,downloads,render:()=>{cursor=0;const tree=components[component](props);for(const fn of effects.splice(0))fn();return tree}}
}
const server={id:'node-a',label:'Node A',enabled:true,revision:1}
test('software PD form saves fixed node model and bounded template without launching',async()=>{
 const calls=[]
 const ui=fixture((op,payload)=>{calls.push({op,payload});return {ok:true,value:op==='resource-catalog'?{servers:[server]}:op==='asset-catalog'?{assets:[{asset_id:'model-a',label:'Model A',server_id:'node-a',kind:'model',status:'ready'}]}:op==='asset-docker-inventory'?{images:[{Id:'sha256:'+'a'.repeat(64),RepoTags:['fixed:1']}]}:{}}},'SoftwareRecipeForm',{onSaved:async()=>{}})
 for(let i=0;i<4;i++){ui.render();await settle()}
 const fields=nodes(ui.render()).filter(n=>n.type==='label')
 for(const [label,value] of [['配方ID','pd-fixture'],['配方名称','PD fixture'],['独立验证模板','pd_vllm_shared']]){
  const field=fields.find(n=>text(n).startsWith(label));nodes(field).find(n=>['input','select'].includes(n.type)).props.onChange({target:{value}})
 }
 const tree=ui.render();nodes(tree).find(n=>n.type==='label'&&text(n).startsWith('本节点已核验模型')).children.find(n=>n?.type==='select').props.onChange({target:{value:'model-a'}})
 nodes(ui.render()).find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await settle()
 const saved=calls.find(c=>c.op==='workbench-save-profile').payload.recipe
 assert.equal(saved.model_asset_id,'model-a');assert.equal(saved.server_id,'node-a');assert.equal(saved.timeout,600)
 assert.ok(!calls.some(c=>c.op==='workbench-run'||c.op==='workbench-prepare'))
})
test('Docker view offers no mutation to an external container',async()=>{
 const ui=fixture(()=>({ok:true,value:{daemon:{version:'1',root:'/data/docker',data_disk:true},management_boundary:'外部仅查看',containers:[{id:'foreign',name:'production',state:'running',owned:false}],images:[]}}),'DockerControl',{server})
 nodes(ui.render()).find(n=>n.type==='button').props.onClick();await settle()
 const tree=ui.render();assert.match(text(tree),/production.*外部容器，仅查看/)
 assert.deepEqual(nodes(tree).filter(n=>n.type==='button').map(text),['读取 Docker 状态'])
 assert.equal(ui.calls.length,1);assert.equal(ui.calls[0].op,'asset-docker-inventory')
})
test('owned Docker container opens its task without issuing a Docker mutation',async()=>{
 let selected
 const ui=fixture(()=>({ok:true,value:{daemon:{version:'1',root:'/data/docker',data_disk:true},management_boundary:'外部仅查看',containers:[{id:'own',name:'pilot',state:'running',owned:true,kind:'software',task_id:'a'.repeat(32)},{id:'foreign',name:'production',state:'running',owned:false}],images:[]}}),'DockerControl',{server,onOpenTask:c=>{selected=c}})
 nodes(ui.render()).find(n=>n.type==='button').props.onClick();await settle()
 const buttons=nodes(ui.render()).filter(n=>n.type==='button'&&text(n)==='查看所属任务')
 assert.equal(buttons.length,1);buttons[0].props.onClick();assert.equal(selected.task_id,'a'.repeat(32));assert.equal(selected.kind,'software')
 assert.deepEqual(ui.calls.map(c=>c.op),['asset-docker-inventory'])
})
test('inference container navigation resolves the existing task session',async()=>{
 let opened
 const node={...server,connection:{transport:'ssh'},policy:{mode:'observe',operations:[],network_ports:[],approvable_gpu_ids:[],gpu_sharing:'exclusive',max_cpu_cores:2,max_memory_gib:4,max_disk_gib:5,max_duration_seconds:90,allow_source_edit:false}}
 const catalog={servers:[node],assets:{models:[],images:[],machines:[]},proposals:[],audit:[]}
 const ui=fixture(op=>({ok:true,value:op==='resource-catalog'?catalog:op==='open'?{sessionId:'existing-session'}:{}}),'Resources',{onTask:(id,show)=>{opened={id,show}}})
 for(let i=0;i<4;i++){ui.render();await settle()}
 const docker=nodes(ui.render()).find(n=>typeof n.type==='function'&&n.type.name==='DockerControl')
 await docker.props.onOpenTask({kind:'inference',task_id:'a'.repeat(32)})
 assert.deepEqual(opened,{id:'existing-session',show:true})
 assert.equal(ui.calls.at(-1).op,'open');assert.equal(ui.calls.at(-1).payload.task_id,'a'.repeat(32))
})
test('Restored software task downloads its complete archive without a remote mutation',async()=>{
 const task={id:'a'.repeat(32),plan:{title:'Archived code',goal:'Keep complete source'},status:'restored',restored:true,candidates:[{candidate_id:'c1',source_archive:'local/c1.tar.gz'}],results:[]}
 const ui=fixture(op=>({ok:true,value:op==='workbench-catalog'?{profiles:[],tasks:[task]}:op==='workbench-source-download'?{filename:'source.tar.gz',data:Buffer.from('source fixture').toString('base64')}: {}}),'BuildWorkbench',{taskId:task.id})
 ui.render();await settle();const tree=ui.render()
 nodes(tree).find(n=>n.type==='button'&&text(n)==='下载 c1 源码归档').props.onClick();await settle()
 assert.equal(ui.downloads.length,1);assert.equal(ui.downloads[0].filename,'source.tar.gz')
 assert.ok(ui.calls.some(c=>c.op==='workbench-source-download'))
 assert.ok(!ui.calls.some(c=>['workbench-run','workbench-prepare','workbench-source-export'].includes(c.op)))
})
test('software tasks show frozen server and can be filtered without executing',async()=>{
 const task=(id,server_id,kind)=>({id:id.repeat(32),server_id,kind,plan:{title:'同名任务',goal:'验证'},status:'draft',candidates:[],results:[]})
 const ui=fixture(op=>({ok:true,value:op==='workbench-catalog'?{profiles:[],tasks:[task('a','node-a','operator'),task('b','node-b','profile')]}:{}}),'BuildWorkbench')
 ui.render();await settle()
 let tree=ui.render()
 assert.match(text(tree),/同名任务 · node-a · 算子/);assert.match(text(tree),/同名任务 · node-b · 性能剖析/)
 nodes(tree).find(n=>n.type==='select'&&n.props['aria-label']==='按服务器筛选工作区任务').props.onChange({target:{value:'node-b'}})
 tree=ui.render();assert.doesNotMatch(text(tree),/同名任务 · node-a/);assert.match(text(tree),/同名任务 · node-b/)
 assert.deepEqual(ui.calls.map(c=>c.op),['workbench-catalog'])
})
test('Network port editing preserves a trailing comma until a second port is entered',async()=>{
 const node={...server,connection:{transport:'ssh'},policy:{mode:'review',operations:['network'],network_ports:[23181],approvable_gpu_ids:[],gpu_sharing:'exclusive',max_cpu_cores:2,max_memory_gib:4,max_disk_gib:5,max_duration_seconds:90,allow_source_edit:false}}
 const catalog={servers:[node],assets:{models:[],images:[],machines:[]},proposals:[],audit:[]}
 const ui=fixture(op=>({ok:true,value:op==='resource-catalog'?catalog:{}}),'Resources',{})
 for(let i=0;i<4;i++){ui.render();await settle()}
 const portField=()=>nodes(ui.render()).find(n=>n.type==='label'&&text(n).startsWith('允许网络检查端口'))
 const input=()=>nodes(portField()).find(n=>n.type==='input')
 input().props.onChange({target:{value:'23181,'}})
 assert.equal(input().props.value,'23181,')
 input().props.onChange({target:{value:'23181，23182'}})
 assert.equal(input().props.value,'23181，23182')
 nodes(ui.render()).find(n=>n.type==='button'&&text(n)==='保存权限上限').props.onClick();await settle()
 const saved=ui.calls.find(c=>c.op==='resource-policy')
 assert.deepEqual(Array.from(saved.payload.policy.network_ports),[23181,23182])
})
test('owner can enter extra routed networks without implicitly granting network access',async()=>{
 const node={...server,connection:{transport:'ssh'},policy:{mode:'observe',operations:[],network_ports:[],approvable_gpu_ids:[],gpu_sharing:'exclusive',max_cpu_cores:2,max_memory_gib:4,max_disk_gib:5,max_duration_seconds:90,allow_source_edit:false}}
 const catalog={servers:[node],assets:{models:[],images:[],machines:[]},proposals:[],audit:[]}
 const ui=fixture(op=>({ok:true,value:op==='resource-catalog'?catalog:{}}),'Resources',{})
 for(let i=0;i<4;i++){ui.render();await settle()}
 const field=nodes(ui.render()).find(n=>n.type==='label'&&text(n).startsWith('额外允许的跨机 IPv4 网段'))
 nodes(field).find(n=>n.type==='input').props.onChange({target:{value:'100.63.105.0/24，100.63.106.0/24'}})
 nodes(ui.render()).find(n=>n.type==='button'&&text(n)==='保存权限上限').props.onClick();await settle()
 const saved=ui.calls.find(c=>c.op==='resource-policy').payload.policy
 assert.deepEqual(Array.from(saved.distributed_networks),['100.63.105.0/24','100.63.106.0/24'])
 assert.equal(saved.mode,'observe');assert.deepEqual(Array.from(saved.network_ports),[]);assert.deepEqual(Array.from(saved.approvable_gpu_ids),[])
})
test('missing GPU process evidence is shown as unknown, not an idle GPU',async()=>{
 const node={...server,connection:{transport:'ssh'},policy:{mode:'observe',operations:[],network_ports:[],approvable_gpu_ids:[],gpu_sharing:'exclusive',max_cpu_cores:2,max_memory_gib:4,max_disk_gib:5,max_duration_seconds:90,allow_source_edit:false},
  observation:{received_at:1,identity:{hostname:'fixture'},accelerator_platform:'cuda',cpu:{logical_cores:8},memory:{available_bytes:1024},gpus:[{index:0,uuid:'gpu-0',name:'GPU',free_mib:7000,total_mib:8000}],gpu_processes:[],gpu_processes_known:false}}
 const catalog={servers:[node],assets:{models:[],images:[],machines:[]},proposals:[],audit:[]}
 const ui=fixture(op=>({ok:true,value:op==='resource-catalog'?catalog:{}}),'Resources',{})
 for(let i=0;i<4;i++){ui.render();await settle()}
 const tree=ui.render();assert.match(text(tree),/GPU进程占用未知/);assert.match(text(tree),/7000 \/ 8000 未知/)
})
test('distributed topology can be reviewed without launching or changing authority',async()=>{
 const servers=[{id:'node-a',label:'Node A'},{id:'node-b',label:'Node B'}]
 const assets={models:servers.map((s,i)=>({id:'model-'+i,label:'Model '+i,server_id:s.id})),images:servers.map((s,i)=>({id:'image-'+i,label:'Image '+i,server_id:s.id}))}
 const ui=fixture(op=>({ok:true,value:op==='resource-catalog'?{servers,assets}:op==='distributed-catalog'?[]:op==='distributed-preflight'?{ready_for_review:false,issues:['节点仅允许观察'],execution_blockers:['远端跨机执行器尚未接入'],limitations:['未执行实际任务']}:{id:'draft'}}),'DistributedWorkbench')
 for(let i=0;i<3;i++){ui.render();await settle()}
 const main=()=>ui.render()
 const modelField=()=>nodes(main()).find(n=>n.type==='label'&&text(n).startsWith('统一服务模型名'))
 nodes(modelField()).find(n=>n.type==='input').props.onChange({target:{value:'example'}})
 const change=(index,label,value)=>{const fieldsets=nodes(main()).filter(n=>n.type==='fieldset');const field=nodes(fieldsets[index]).find(n=>n.type==='label'&&text(n).startsWith(label));nodes(field).find(n=>['input','select'].includes(n.type)).props.onChange({target:{value}})}
 for(const [index,ip,gpu] of [[0,'10.0.0.1','1'],[1,'10.0.0.2','2']]){
  change(index,'节点专用 IPv4',ip);change(index,'GPU 编号',gpu);change(index,'RDMA 设备名','mlx5_0');change(index,'已核验模型','model-'+index);change(index,'已核验镜像','image-'+index)
 }
 nodes(main()).find(n=>n.type==='button'&&text(n)==='静态预检').props.onClick();await settle()
 const request=ui.calls.find(c=>c.op==='distributed-preflight').payload
 assert.equal(request.mode,'sglang_pd');assert.equal(request.nodes[0].role,'prefill');assert.equal(request.nodes[1].role,'decode')
 assert.deepEqual(Array.from(request.nodes[0].gpu_ids),[1]);assert.equal(request.nodes[0].ports.bootstrap,31002)
 assert.match(text(main()),/节点仅允许观察/);assert.match(text(main()),/尚未开放/);assert.match(text(main()),/执行待完成：远端跨机执行器尚未接入/)
 change(0,'GPU 编号','3');assert.doesNotMatch(text(main()),/节点仅允许观察/)
 assert.ok(!ui.calls.some(c=>['distributed-start','resource-policy','network-run'].includes(c.op)))
})
