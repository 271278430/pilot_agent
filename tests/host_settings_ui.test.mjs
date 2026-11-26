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
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/host-settings/client.js',import.meta.url),'utf8'),{
    window:{__ModuleLoader__:{load:v=>{plugin=v}}},document:{createElement:()=>({remove(){}}),head:{append(){}}},
  })
  plugin.factory(()=>React).apply({effect:fn=>fn(),slots:{inject:(_n,fn)=>fn(),register:(s,c)=>{slots.set(s.id,c)}},connection:{rpc:{call:async(path,op,payload)=>{calls.push({path,op,payload});return handler(op,payload)}}}})
  const wrapper=slots.get('pilot-host')(),render=()=>{cursor=0;const tree=wrapper.type();for(const effect of effects.splice(0))effect();return tree}
  return {render,calls}
}
const base={revision:0,applied_revision:0,pending:false,host_project:'/srv/pilot',desired:{workspace_root:'/srv/workspaces',model:{model_id:'fixture',base_url:'https://api.example/v1',context_window:32768,max_output_tokens:6000,auth_required:true,key_configured:true}},applied:{workspace_root:'/srv/workspaces'}}
const settle=async()=>{for(let i=0;i<20;i++)await Promise.resolve()}
const buttons=(t,name)=>nodes(t).filter(n=>n.type==='button'&&text(n)===name)
const field=(t,label)=>nodes(t).find(n=>n.type==='label'&&n.children[0]===label).children.find(n=>n&&typeof n==='object'&&(n.type==='input'||n.type==='textarea'))
test('settings display actual root and masked Key; saved drafts and sync use shared revisions',async()=>{
 let saved
 const f=fixture((op,p)=>{if(op==='save'){saved=p;return {ok:true,value:{...base,revision:1,pending:true,desired:{...base.desired,model:{...base.desired.model,model_id:p.model.model_id}}}}}if(op==='sync')return {ok:true,value:{...base,revision:1,applied_revision:1}};return {ok:true,value:base}})
 f.render();await settle();let t=f.render();assert.equal(field(t,'当前生效的完整路径').props.value,'/srv/workspaces');assert.equal(field(t,'API Key').props.type,'password');assert.equal(field(t,'API Key').props.value,'')
 field(t,'模型ID').props.onChange({target:{value:'new-fixture'}});t=f.render();assert(buttons(t,'同步配置').every(b=>b.props.disabled))
 field(t,'API Key').props.onChange({target:{value:'fixture-private'}});t=f.render();await buttons(t,'保存模型配置')[0].props.onClick();t=f.render()
 assert.equal(saved.expected_revision,0);assert.equal(saved.model.api_key,'fixture-private');assert.equal(field(t,'API Key').props.value,'');assert(buttons(t,'同步配置').every(b=>!b.props.disabled))
 await buttons(t,'同步配置')[0].props.onClick();assert.equal(f.calls.at(-1).payload.expected_revision,1)
})
test('root save retains an unsaved model draft and conflicting revision can reload',async()=>{
 const f=fixture((op,p)=>op==='save'?{ok:true,value:{...base,revision:1,pending:true,desired:{...base.desired,workspace_root:p.workspace_root.replace(/\/$/,'')}}}:{ok:true,value:base})
 f.render();await settle();let t=f.render();field(t,'模型ID').props.onChange({target:{value:'draft-model'}});t=f.render();field(t,'设置新的主目录').props.onChange({target:{value:'/srv/new/'}});t=f.render()
 await buttons(t,'保存路径')[0].props.onClick();t=f.render();assert.equal(field(t,'设置新的主目录').props.value,'/srv/new');assert.equal(field(t,'模型ID').props.value,'draft-model');assert(buttons(t,'同步配置').every(b=>b.props.disabled))
 await buttons(t,'重新读取配置')[0].props.onClick();t=f.render();assert.equal(field(t,'模型ID').props.value,'fixture')
})
test('connection failures stay visible and never trigger save or sync',async()=>{
 const f=fixture((op)=>op==='test-model'?{ok:true,value:{passed:false,message:'认证失败',latency_ms:20}}:{ok:true,value:base})
 f.render();await settle();await buttons(f.render(),'测试连接')[0].props.onClick();assert.match(text(f.render()),/认证失败/);assert.equal(f.calls.filter(c=>c.op==='save'||c.op==='sync').length,0)
 field(f.render(),'模型ID').props.onChange({target:{value:'other'}});assert.doesNotMatch(text(f.render()),/认证失败/)
})
test('model capacity changes disclose the pending compaction restart',async()=>{
 const f=fixture(()=>({ok:true,value:{...base,compaction_restart_required:true}}))
 f.render();await settle();assert.match(text(f.render()),/上下文压缩预算仍使用启动时的模型配置/)
})
test('model settings explain IDs and product endpoints and keep platform permission errors distinct',async()=>{
 const f=fixture(op=>op==='test-model'?{ok:true,value:{passed:false,test_kind:'image-request',http_status:403,provider_code:'1220',error_category:'api-permission',message:'此Key无权访问当前API',latency_ms:118}}:{ok:true,value:base})
 f.render();await settle();let t=f.render();assert.match(text(t),/模型ID会原样发送给API，不是列表序号/);assert.match(text(t),/api\/coding\/paas\/v4/)
 field(t,'模型ID').props.onChange({target:{value:'2'}});t=f.render();assert.match(text(t),/当前模型ID只有数字/)
 await buttons(t,'测试连接')[0].props.onClick();t=f.render();assert.match(text(t),/图片请求未通过/);assert.match(text(t),/HTTP 403/);assert.match(text(t),/平台码 1220/);assert.doesNotMatch(text(t),/认证失败/)
 assert.equal(f.calls.filter(c=>c.op==='save'||c.op==='sync').length,0)
})
