import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

const nodes=t=>Array.isArray(t)?t.flatMap(nodes):!t||typeof t!=='object'?[]:[t,...t.children.flatMap(nodes)]
const text=t=>Array.isArray(t)?t.map(text).join(' '):typeof t==='string'?t:t&&typeof t==='object'?t.children.map(text).join(' '):''
const button=(tree,name)=>nodes(tree).find(n=>n.type==='button'&&text(n)===name)
const settle=async()=>{for(let i=0;i<15;i++)await Promise.resolve()}

function fixture(handler,component='AssetWorkbench',props={}){
 const values=[],effects=[],calls=[];let cursor=0,plugin,components
 const React={Fragment:'fragment',createElement:(type,props,...children)=>({type,props:props??{},children}),
  useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=typeof v==='function'?v(values[i]):v}]},
  useRef:initial=>{const i=cursor++;return values[i]??={current:initial}},
  useEffect:(fn,deps)=>{const i=cursor++;if(!values[i]||deps.some((v,j)=>v!==values[i][j])){values[i]=deps;effects.push(fn)}},
  useSyncExternalStore:(_s,read)=>read()}
 const code=readFileSync(new URL('../harness/runtime/plugins/infra-domain/client.js',import.meta.url),'utf8')
  .replace('function BuildWorkbench(', 'window.capture({AssetWorkbench,ExecutorControl});function BuildWorkbench(')
 vm.runInNewContext(code,{window:{__ModuleLoader__:{load:p=>{plugin=p}},capture:v=>{components=v}},
  document:{addEventListener(){},removeEventListener(){},createElement:()=>({remove(){}}),head:{append(){}}},
  crypto:globalThis.crypto,setInterval:()=>1,clearInterval(){},Date,Blob,URL})
 plugin.factory(()=>React).apply({effect:fn=>fn(),slots:{inject:(_s,fn)=>fn(),register(){}},sidebarRightTabs:{register(){}},
  uiConversation:{events:{register(){}}},connection:{rpc:{call:async(_p,op,payload)=>{calls.push({op,payload});return handler(op,payload)}}}})
 return {calls,render:()=>{cursor=0;const tree=components[component](props);for(const fn of effects.splice(0))fn();return tree}}
}
const catalog=()=>({assets:[],stacks:[],executors:[],operations:[],proposals:[]})
const server={id:'s5000-a',label:'节点 A',revision:2,enabled:true,last_connection_test:{ready:true}}
const basic=(op)=>({ok:true,value:op==='resource-catalog'?{servers:[server]}:catalog()})

test('executor form restores local and registered paths; remote defaults await real data disk check',async()=>{
 const paths={root:'/data/pilot/resources',local_executor:'/data/pilot/resources/executor',remote_executor:'/data/pilot-executor'}
 for(const [transport,registered,expectedRoot,expectedAssets] of [
  ['local',null,paths.local_executor,paths.root],
  ['ssh',null,'',''],
  ['ssh',{root:'/data/custom-executor',asset_root:'/data/custom-models'},'/data/custom-executor','/data/custom-models'],
 ]){
  const selected={...server,connection:{transport}},data={...catalog(),resource_paths:paths}
  if(registered)data.executors=[{...registered,server_id:server.id,server_revision:2,enabled:true}]
  const f=fixture(()=>({ok:true,value:data}),'ExecutorControl',{server:selected})
  f.render();await settle();const tree=f.render()
  const field=label=>nodes(tree).find(n=>n.type==='label'&&n.children[0]===label).children.find(n=>n?.type==='input')
  assert.equal(field('Pilot专属执行根').props.value,expectedRoot)
  assert.equal(field('节点资源主目录').props.value,expectedAssets)
  assert.ok(!f.calls.some(c=>c.op==='resource-executor'||c.op==='asset-prepare'))
 }
})

test('remote data paths come from node inspection; editing them invalidates the save gate',async()=>{
 const data=catalog(),paths={root:{path:'/mnt/nvme0/pilot-data/pilot-node',data_disk:true},asset_root:{path:'/mnt/nvme0/pilot-data/resources',data_disk:true}}
 const f=fixture(op=>({ok:true,value:op==='asset-executor-check'?{ready:true,server_revision:2,reasons:[],paths}:data}),'ExecutorControl',{server:{...server,connection:{transport:'ssh'}}})
 f.render();await settle()
 const checkbox=nodes(f.render()).find(n=>n.type==='input'&&n.props.type==='checkbox')
 checkbox.props.onChange({target:{checked:true}})
 assert.equal(button(f.render(),'保存执行器配置').props.disabled,true)
 await button(f.render(),'检查节点目录与能力').props.onClick()
 assert.equal(button(f.render(),'保存执行器配置').props.disabled,false)
 assert.match(text(f.render()),/此次检查未创建目录或授予GPU权限/)
 const field=nodes(f.render()).find(n=>n.type==='label'&&n.children[0]==='节点资源主目录').children.find(n=>n?.type==='input')
 field.props.onChange({target:{value:'/data/other'}})
 assert.equal(button(f.render(),'保存执行器配置').props.disabled,true)
 assert.ok(!f.calls.some(c=>c.op==='resource-executor'))
})

test('Windows local controller does not offer an enabled Linux node worker',async()=>{
 const data={...catalog(),host_executor_supported:false,resource_paths:{local_executor:'D:/pilot/runs/nodes/pilot-executor',root:'D:/pilot/resources'}}
 const f=fixture(()=>({ok:true,value:data}),'ExecutorControl',{server:{...server,connection:{transport:'local'}}})
 f.render();await settle()
 assert.equal(button(f.render(),'检查节点目录与能力').props.disabled,true)
 assert.equal(button(f.render(),'保存执行器配置').props.disabled,true)
 assert.match(text(f.render()),/Windows本机支持主控/)
})

test('executor form submits an edited node resource directory with its server revision',async()=>{
 const f=fixture(()=>({ok:true,value:catalog()}),'ExecutorControl',{server:{...server,connection:{transport:'ssh'}}})
 f.render();await settle()
 const label=nodes(f.render()).find(n=>n.type==='label'&&n.children[0]==='节点资源主目录')
 label.children.find(n=>n?.type==='input').props.onChange({target:{value:'/data/team-pilot-assets'}})
 await nodes(f.render()).find(n=>n.type==='form').props.onSubmit({preventDefault(){}})
 const submitted=f.calls.filter(c=>c.op==='resource-executor')
 assert.equal(submitted.length,1)
 assert.equal(submitted[0].payload.server_id,server.id)
 assert.equal(submitted[0].payload.expected_revision,server.revision)
 assert.equal(submitted[0].payload.asset_root,'/data/team-pilot-assets')
 assert.ok(!f.calls.some(c=>c.op==='asset-prepare'))
})

test('model form saves one fixed node revision, without starting any download',async()=>{
 let saved
 const f=fixture((op,p)=>{if(op==='asset-save'){saved=p;return {ok:true,value:p}}return basic(op)})
 f.render();await settle();button(f.render(),'添加模型').props.onClick()
 const field=(label,value)=>{const tree=f.render(),n=nodes(tree).find(n=>n.type==='label'&&n.children[0]===label);n.children.find(v=>typeof v==='object').props.onChange({target:{value}})}
 field('资产ID','qwen-node');field('显示名称','Qwen fixture');field('模型组织/仓库','Qwen/Qwen3-0.6B');field('固定40位revision','a'.repeat(40))
 await nodes(f.render()).find(n=>n.type==='form').props.onSubmit({preventDefault(){}})
 assert.equal(saved.server_id,'s5000-a');assert.equal(saved.source.revision,'a'.repeat(40));assert.equal(saved.expected_revision,0)
 assert.ok(!f.calls.some(c=>c.op==='asset-prepare'))
})

test('revision failure keeps reviewable asset form and displays the real error',async()=>{
 const f=fixture(op=>op==='asset-save'?{ok:false,error:{message:'资产版本冲突，请刷新'}}:basic(op))
 f.render();await settle();button(f.render(),'添加镜像').props.onClick()
 await nodes(f.render()).find(n=>n.type==='form').props.onSubmit({preventDefault(){}})
 const tree=f.render();assert.match(text(tree),/资产版本冲突/);assert.ok(button(tree,'保存资产登记'))
})

test('prepare cancel and reconciliation retain the original durable operation',async()=>{
 const data=catalog();data.assets=[{asset_id:'model',label:'模型',server_id:server.id,kind:'model',revision:1,status:'preparing',operation_id:'a'.repeat(32),source:{kind:'existing',location:'/data/model'}}]
 data.operations=[{id:'a'.repeat(32),status:'unknown'}]
 const f=fixture(op=>({ok:true,value:op==='resource-catalog'?{servers:[server]}:data}))
 f.render();await settle();let tree=f.render();assert.ok(!button(tree,'开始下载并核验'))
 await button(tree,'请求取消原操作').props.onClick();await button(f.render(),'核对已退出的下载任务').props.onClick()
 assert.deepEqual(f.calls.filter(c=>['asset-cancel','asset-reconcile'].includes(c.op)).map(c=>c.payload.operation_id),['a'.repeat(32),'a'.repeat(32)])
})

test('executor cleanup requires a separate concrete confirmation and uses the selected node',async()=>{
 const data=catalog();data.executors=[{server_id:server.id,server_revision:2,enabled:true,root:'/data/pilot-fixture'}]
 const f=fixture(op=>({ok:true,value:op==='asset-executor-cleanup'?{restored:true}:data}),'ExecutorControl',{server})
 f.render();await settle();button(f.render(),'清理执行器试验目录').props.onClick()
 assert.ok(!f.calls.some(c=>c.op==='asset-executor-cleanup'));assert.match(text(f.render()),/共享Docker镜像保留/)
 await button(f.render(),'确认归档并清理专属目录').props.onClick()
 assert.equal(f.calls.at(-1).payload.server_id,server.id)
})
