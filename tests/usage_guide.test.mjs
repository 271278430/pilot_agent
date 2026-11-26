import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import vm from 'node:vm'

function fixture(handler) {
  const values=[],effects=[],slots=new Map(),calls=[],downloads=[]
  let cursor=0,plugin
  const React={Fragment:'fragment',createElement:(type,props,...children)=>({type,props:props??{},children}),
    useState:initial=>{const i=cursor++;if(!(i in values))values[i]=initial;return [values[i],v=>{values[i]=v}]},
    useRef:initial=>{const i=cursor++;return values[i]??={current:initial}},
    useEffect:(effect,deps)=>{const i=cursor++;if(!values[i]||deps.some((v,j)=>v!==values[i].deps[j])){values[i]={deps};effects.push(effect)}}}
  const ctx={effect:fn=>fn(),slots:{inject:(_name,fn)=>fn(),register:(def,component)=>{slots.set(def.id,component)}},
    connection:{rpc:{call:async(path,method,payload)=>{calls.push({path,method,payload});return handler()}}}}
  vm.runInNewContext(readFileSync(new URL('../harness/runtime/plugins/brand/client.js',import.meta.url),'utf8'),{
    window:{__ModuleLoader__:{load:value=>{plugin=value}}},Document:function(){},
    document:{querySelector:()=>null,querySelectorAll:()=>[],createElement:type=>type==='a'?{click(){downloads.push(this.download)}}:{remove(){}},head:{append(){}},body:{}},
    Blob,URL:{createObjectURL:()=> 'blob:diagram',revokeObjectURL(){}},setTimeout:fn=>fn(),
    MutationObserver:class{observe(){}disconnect(){}},NodeFilter:{SHOW_TEXT:4},Date,
  })
  plugin.factory(name=>{assert.equal(name,'react');return React}).apply(ctx)
  return {calls,slots,downloads,render:(id='pilot-guide')=>{cursor=0;const tree=slots.get(id)();for(const effect of effects.splice(0))effect();return tree}}
}
const nodes=tree=>typeof tree==='object'&&tree?[tree,...tree.children.flat(Infinity).flatMap(nodes)]:[]
const text=tree=>typeof tree==='string'?tree:tree?.children?.flat(Infinity).map(text).join(' ')??''
function payload(){
  const data=JSON.parse(readFileSync(new URL('../src/pilot_infra/usage_guide.json',import.meta.url)))
  return {...data,configuration:{checked_at:'2026-11-21T00:00:00Z',models:['Qwen3'],engines:['SGLang'],engine_recipes:['SGLang Python'],cpu_executors:0,execution_quotas:[],warnings:[]},
    features:data.features.map(item=>({...item,availability:{state:item.gate==='workbench'?'unconfigured':'registered',label:item.gate==='workbench'?'当前未启用':'已登记',note:'执行仍须审阅'}}))}
}

test('guide uses the full catalogue, searches features and labels unavailable workbench',async()=>{
  const f=fixture(()=>({ok:true,value:payload()}))
  f.render()
  assert.match(text(f.render()),/正在读取/)
  await new Promise(setImmediate)
  let tree=f.render()
  assert.ok(text(tree).includes('共'+payload().features.length+'项功能'))
  assert.match(text(tree),/引擎源码修改与制品交付/)
  assert.match(text(tree),/当前未启用/)
  assert.match(text(tree),/Pilot主控（含监督职责）/)
  assert.match(text(tree),/Infra专家/)
  assert.match(text(tree),/Repo专家/)
  assert.match(text(tree),/默认协作：auto/)
  assert.match(text(tree),/Worker/)
  assert.match(text(tree),/从干净服务器到容器推理服务/)
  assert.match(text(tree),/需要你决定的地方/)
  assert.match(text(tree),/资源来源/)
  assert.match(text(tree),/失败与恢复/)
  assert.equal(f.calls.length,1)
  assert.equal(f.calls[0].method,'guide')
  assert.deepEqual(Object.keys(f.calls[0].payload),[])
  nodes(tree).find(n=>n.type==='input').props.onChange({target:{value:'回滚'}})
  tree=f.render()
  assert.match(text(tree),/实际回滚与重新验收/)
  assert.doesNotMatch(text(tree),/服务器资源池与接入管理/)
  nodes(tree).find(n=>n.type==='input').props.onChange({target:{value:'Infra专家'}})
  tree=f.render()
  assert.match(text(tree),/Infra专家/)
  assert.doesNotMatch(text(tree),/质量与性能实验/)
})

test('guide failure is visible and retry reloads current configuration',async()=>{
  let attempt=0
  const f=fixture(()=>++attempt===1?{ok:false,error:{message:'unavailable'}}:{ok:true,value:payload()})
  f.render();await new Promise(setImmediate)
  let tree=f.render()
  assert.match(text(tree),/无法读取当前使用说明/)
  assert.doesNotMatch(text(tree),/已登记/)
  await nodes(tree).find(n=>n.type==='button'&&text(n)==='重试读取说明').props.onClick()
  tree=f.render()
  assert.doesNotMatch(text(tree),/无法读取/)
  assert.ok(text(tree).includes('共'+payload().features.length+'项功能'))
  assert.equal(attempt,2)
})

function architecturePayload(){
  const data=JSON.parse(readFileSync(new URL('../src/pilot_infra/architecture.json',import.meta.url)))
  data.views=data.views.map(v=>({...v,svg:readFileSync(new URL('../docs/architecture/'+v.id+'.svg',import.meta.url),'utf8')}))
  data.components.repo.tools=['source_read','engine_source_patch']
  return {...data,configuration:payload().configuration}
}

test('architecture shows a real total SVG and drills through SVG targets with a single read',async()=>{
  const f=fixture(()=>({ok:true,value:architecturePayload()}))
  assert.ok(f.slots.has('pilot-guide')&&f.slots.has('pilot-architecture'))
  f.render('pilot-architecture');await new Promise(setImmediate)
  let tree=f.render('pilot-architecture')
  assert.equal(f.calls.length,1);assert.equal(f.calls[0].method,'architecture')
  assert.deepEqual(Object.keys(f.calls[0].payload),[])
  const graphic=()=>nodes(tree).find(n=>n.props.className==='pilot-diagram-svg')
  for(const label of ['Agent Loop','上下文工程','记忆与业务状态','工具系统'])assert.ok(graphic().props.dangerouslySetInnerHTML.__html.includes(label))
  assert.ok(!nodes(tree).some(n=>n.props.className==='pilot-architecture-module'))
  let pageTopReached=0
  nodes(tree).find(n=>n.props['data-pilot-architecture']).props.ref.current={scrollIntoView(){pageTopReached++}}
  const buttons=()=>nodes(tree).filter(n=>n.type==='button')
  const target=id=>({closest:()=>({getAttribute:name=>name==='data-architecture-target'?id:null})})
  graphic().props.onClick({type:'click',target:target('loop')})
  tree=f.render('pilot-architecture')
  assert.equal(graphic().props['data-diagram-id'],'loop')
  assert.equal(pageTopReached,1)
  nodes(tree).find(n=>n.type==='select').props.onChange({target:{value:'loop_step'}})
  tree=f.render('pilot-architecture')
  assert.equal(graphic().props['data-diagram-id'],'loop')
  graphic().props.onKeyDown({type:'keydown',key:'Enter',target:target('loop_actions'),preventDefault(){}})
  tree=f.render('pilot-architecture')
  assert.equal(graphic().props['data-diagram-id'],'loop')
  assert.ok(!nodes(tree).some(n=>n.type==='option'&&n.props.value==='loop_step'))
  assert.equal(graphic().props['data-diagram-id'],'loop')
  buttons().find(n=>text(n)==='系统总图').props.onClick()
  tree=f.render('pilot-architecture')
  assert.equal(graphic().props['data-diagram-id'],'system')
  assert.equal(f.calls.length,1)
})

test('diagram viewer preserves the picture in fullscreen, supports zoom and exports the same SVG',async()=>{
  const f=fixture(()=>({ok:true,value:architecturePayload()}))
  f.render('pilot-architecture');await new Promise(setImmediate)
  let tree=f.render('pilot-architecture')
  const buttons=()=>nodes(tree).filter(n=>n.type==='button')
  nodes(tree).find(n=>n.type==='select').props.onChange({target:{value:'context'}})
  tree=f.render('pilot-architecture')
  let image=nodes(tree).find(n=>n.props.className==='pilot-diagram-svg')
  assert.ok(image.props.dangerouslySetInnerHTML.__html.includes('原生上下文组装'))
  buttons().find(n=>text(n)==='全屏看图').props.onClick()
  tree=f.render('pilot-architecture')
  assert.ok(nodes(tree).some(n=>n.type==='dialog'))
  buttons().find(n=>n.props['aria-label']==='放大架构图').props.onClick()
  tree=f.render('pilot-architecture')
  image=nodes(tree).find(n=>n.props.className==='pilot-diagram-svg')
  assert.ok(image.props.style.width>1200)
  buttons().find(n=>text(n)==='适应宽度').props.onClick()
  tree=f.render('pilot-architecture')
  assert.equal(nodes(tree).find(n=>n.props.className==='pilot-diagram-svg').props.style.width,'100%')
  await buttons().find(n=>text(n)==='保存SVG').props.onClick()
  assert.deepEqual(f.downloads,['Pilot-context.svg'])
  buttons().find(n=>text(n)==='关闭全屏').props.onClick()
  tree=f.render('pilot-architecture')
  assert.ok(!nodes(tree).some(n=>n.type==='dialog'))
  assert.equal(nodes(tree).find(n=>n.props.className==='pilot-diagram-svg').props['data-diagram-id'],'context')
  assert.equal(f.calls.length,1)
})

test('architecture failed read exposes retry without displaying stale diagram',async()=>{
  let attempt=0
  const f=fixture(()=>++attempt===1?{ok:false}:{ok:true,value:architecturePayload()})
  f.render('pilot-architecture');await new Promise(setImmediate)
  let tree=f.render('pilot-architecture')
  assert.match(text(tree),/无法读取项目架构/)
  assert.ok(!nodes(tree).some(n=>n.type==='svg'))
  await nodes(tree).find(n=>n.type==='button'&&text(n)==='重试读取架构图').props.onClick()
  tree=f.render('pilot-architecture')
  assert.ok(nodes(tree).some(n=>n.props.className==='pilot-diagram-svg'))
  assert.doesNotMatch(text(tree),/无法读取项目架构/)
})
