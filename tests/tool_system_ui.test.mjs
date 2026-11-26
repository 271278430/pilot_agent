import test from 'node:test'
import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import vm from 'node:vm'
const nodes=t=>Array.isArray(t)?t.flatMap(nodes):!t||typeof t!=='object'?[]:[t,...t.children.flatMap(nodes)]
const text=t=>Array.isArray(t)?t.map(text).join(' '):typeof t==='string'?t:t&&typeof t==='object'?t.children.map(text).join(' '):''
function fixture(handler){
  const values=[],effects=[],calls=[],slots=new Map();let cursor=0,plugin
  const React={Fragment:'fragment',createElement:(type,props,...children)=>({type,props:props??{},children}),
    useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=typeof v==='function'?v(values[i]):v}]},
    useRef:initial=>{const i=cursor++;return values[i]??={current:initial}},
    useEffect:(fn,deps)=>{const i=cursor++;if(!values[i]){values[i]=deps;effects.push(fn)}}}
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/tool-system/client.js',import.meta.url),'utf8'),{
    window:{__ModuleLoader__:{load:v=>{plugin=v}}},document:{createElement:()=>({remove(){}}),head:{append(){}}},
  })
  plugin.factory(()=>React).apply({effect:fn=>fn(),slots:{inject:(_n,fn)=>fn(),register:(s,c)=>{slots.set(s.id,c)}},connection:{rpc:{call:async(path,op,payload)=>{calls.push({path,op,payload});return handler(op,payload)}}}})
  const wrapper=slots.get('pilot-tools')(),render=()=>{cursor=0;const tree=wrapper.type();for(const effect of effects.splice(0))effect();return tree}
  return {render,calls}
}
const catalog=(selected=false)=>({revision:7,applied_revision:7,pending:false,roles:['infra-lead','repo-expert'],sessions:[{id:'root',role:'infra-lead',status:'idle',title:'部署咨询'}],selected_session:selected?{id:'root',role:'infra-lead'}:null,
 local_tools:selected?[{name:'pilot_infra_probe',description:'检查整机资源',status:'registered'}]:[],skills:selected?[{name:'coordinate-inference-delivery',description:'主控操作指南',status:'available',model_invocable:true}]:[],servers:[]})
const settle=async()=>{for(let i=0;i<10;i++)await Promise.resolve()}
const button=(t,name)=>nodes(t).find(n=>n.type==='button'&&text(n)===name)
test('catalog defaults to a real loaded Session, supports search and Skills without task execution',async()=>{
 const f=fixture((op,p)=>({ok:true,value:catalog(!!p.session_id)}));f.render();await settle();let tree=f.render()
 assert.match(text(tree),/pilot_infra_probe/);assert.equal(f.calls.length,2);assert.ok(f.calls.every(c=>c.op==='catalog'))
 nodes(tree).find(n=>n.type==='input'&&n.props.placeholder).props.onChange({target:{value:'nothing matches'}})
 assert.doesNotMatch(text(f.render()),/pilot_infra_probe/)
 nodes(f.render()).find(n=>n.type==='input'&&n.props.placeholder).props.onChange({target:{value:''}})
 button(f.render(),'Skills').props.onClick();assert.match(text(f.render()),/coordinate-inference-delivery/)
})
test('MCP saves structured shared revisions and failed deletion retains reviewable confirmation',async()=>{
 let saved
 const f=fixture((op,p)=>{if(op==='put'){saved=p;return {ok:true,value:{...catalog(true),revision:8,servers:[{...p.server,status:'disabled',registered_tools:[]}]}}}if(op==='delete')return {ok:false,error:{message:'Configuration revision conflict'}};return {ok:true,value:catalog(true)}})
 f.render();await settle();button(f.render(),'MCP 服务').props.onClick();button(f.render(),'添加 MCP 服务').props.onClick()
 let tree=f.render();const inputFor=label=>nodes(tree).find(n=>n.type==='label'&&n.children[0]===label).children.find(n=>typeof n==='object')
 inputFor('服务名称').props.onChange({target:{value:'docs'}});tree=f.render()
 inputFor('可执行程序（容器内路径）').props.onChange({target:{value:'python'}});tree=f.render()
 await nodes(tree).find(n=>n.type==='form').props.onSubmit({preventDefault(){}})
 assert.equal(saved.expected_revision,7);assert.equal(saved.server.enabled,false);assert.deepEqual(Array.from(saved.server.roles),['infra-lead']);assert.ok(Array.isArray(saved.server.args))
 tree=f.render();assert.match(text(tree),/已禁用/);button(tree,'删除配置').props.onClick()
 await nodes(f.render()).find(n=>n.type==='form').props.onSubmit({preventDefault(){}});await settle();tree=f.render()
 assert.match(text(tree),/revision conflict/);assert.ok(button(tree,'确认删除'))
})

test('sandbox page shows actual limits, stale self-test, failed cleanup and shared policy revision',async()=>{
 let current=false,lastOp
 const sandbox=()=>({policy:{revision:3,probe_image:'sha256:fixture'},active_count:1,max_active:4,
   limits:{cpu_cores:2,memory_mib:256,pids:32,workspace_mib:64,temp_mib:16},
   leases:[{id:'owned',server_name:'fixture',root_session:'A',status:'running',image:'sha256:fixture',scope:{input:'readonly snapshot'}}],
   last_self_test:{passed:true,current,image:'sha256:fixture',checks:{network_denied:true,no_gpu:false}}})
 const f=fixture((op,p)=>{lastOp={op,p};if(op==='stop')return {ok:false,error:{message:'Foreign container; cleanup refused'}};return {ok:true,value:{...catalog(true),sandbox:sandbox()}}})
 f.render();await settle();button(f.render(),'执行隔离').props.onClick()
 assert.match(text(f.render()),/结果已过期/);assert.match(text(f.render()),/本地 MCP \/ 自检活动租约 1/)
 assert.match(text(f.render()),/不包含构建容器、GPU服务/);assert.match(text(f.render()),/CPU 2核 · RAM 256MiB/)
 assert.match(text(f.render()),/通过 · 外部网络访问被拒绝/);assert.match(text(f.render()),/未通过 · GPU设备不可见/)
 assert.equal(nodes(f.render()).filter(n=>n.type==='input'&&n.props.placeholder).length,0)
 await button(f.render(),'保存自检镜像').props.onClick();assert(f.calls.some(c=>c.path==='/pilot-sandbox-api'&&c.payload.expected_revision===3))
 await button(f.render(),'停止此沙箱').props.onClick();assert.equal(lastOp.op,'stop');assert.match(text(f.render()),/cleanup refused/)
 current=true;await button(f.render(),'刷新').props.onClick();await settle();assert.match(text(f.render()),/最近自检：通过/)
})
