window.__ModuleLoader__.load({
  id: '@pilot/brand',
  factory: require => {
    const React = require('react'), h = React.createElement
    const inject = ['slots', 'theme', 'connection']
    function Mark({ size = 28, className }) {
      return h('img', { src: '/pilot-brand.svg', alt: 'Pilot', width: size, height: size, className, style: { display: 'block', flex: 'none' } })
    }
    function HeroBrand({ size = 34 }) {
      return h('span', { 'data-pilot-hero-brand': true }, h('span', { className: 'pilot-welcome-label' }, h(Mark, { size }), 'PILOT'),
        h('span', { className: 'pilot-welcome-title' }, '让推理服务，经得起验证。'),
        h('span', { className: 'pilot-welcome-note' }, '面向整台服务器，诊断故障、修复候选并独立复验。'),
        h('span', { className: 'pilot-welcome-steps' }, ...['01 · 目标', '02 · 实验', '03 · 证据'].map(text => h('span', { key: text }, text))))
    }
    function apply(ctx) {
      function ProjectArchitecture() {
        const [data,setData]=React.useState(null),[error,setError]=React.useState(''),[busy,setBusy]=React.useState(false)
        const [viewId,setView]=React.useState('system'),[scale,setScale]=React.useState(null),[full,setFull]=React.useState(false),[selected,setSelected]=React.useState(''),[exportError,setExportError]=React.useState('')
        const request=React.useRef(0),top=React.useRef(null),canvas=React.useRef(null),dialog=React.useRef(null)
        const read=async()=>{
          const id=++request.current;setBusy(true);setError('')
          try {
            const response=await ctx.connection.rpc.call('/pilot-infra-api','architecture',{})
            if(!response.ok)throw new Error('Architecture unavailable')
            if(id===request.current)setData(response.value)
          } catch {if(id===request.current){setData(null);setError('无法读取项目架构图，请重试。')}}
          finally {if(id===request.current)setBusy(false)}
        }
        React.useEffect(()=>{read();return()=>{request.current++}},[])
        React.useEffect(()=>{if(full)canvas.current?.scrollTo?.({left:0,top:0});else top.current?.scrollIntoView({block:'start'})},[viewId])
        React.useEffect(()=>{if(full&&!dialog.current?.open)dialog.current?.showModal();return()=>{if(dialog.current?.open)dialog.current.close()}},[full])
        const pages=new Map(data?.views.map(v=>[v.id,v])??[]),view=pages.get(viewId)??pages.get(data?.entry),trail=[]
        for(let p=view;p;p=pages.get(p.parent))trail.unshift(p)
        const go=id=>{const target=data?.aliases?.[id]??id;if(pages.has(target)){setView(target);setScale(null);setSelected('');setExportError('')}}
        const act=e=>{
          const node=e.target.closest?.('[data-architecture-target],[data-architecture-component]')
          if(!node)return
          const target=node.getAttribute('data-architecture-target'),component=node.getAttribute('data-architecture-component')
          if(target)go(target);else if(component)setSelected(component)
          if(e.type==='keydown')e.preventDefault()
        }
        const adjust=factor=>setScale(Math.max(.25,Math.min(2,(scale??(canvas.current?.clientWidth??view.diagram.width)/view.diagram.width)*factor)))
        const fitAll=()=>setScale(Math.min((canvas.current?.clientWidth??view.diagram.width)/view.diagram.width,(canvas.current?.clientHeight??view.diagram.height)/view.diagram.height))
        const save=async type=>{
          setExportError('')
          try {
            const blob=new Blob([view.svg],{type:'image/svg+xml;charset=utf-8'}),source=URL.createObjectURL(blob)
            let output=source
            if(type==='png'){
              try {
                const img=new Image()
                await new Promise((resolve,reject)=>{img.onload=resolve;img.onerror=reject;img.src=source})
                const drawing=document.createElement('canvas');drawing.width=view.diagram.width*2;drawing.height=view.diagram.height*2
                drawing.getContext('2d').drawImage(img,0,0,drawing.width,drawing.height)
                const png=await new Promise(resolve=>drawing.toBlob(resolve,'image/png'))
                if(!png)throw new Error('PNG unavailable')
                output=URL.createObjectURL(png)
              } finally {URL.revokeObjectURL(source)}
            }
            const link=document.createElement('a');link.href=output;link.download='Pilot-'+view.id+'.'+type;link.click()
            setTimeout(()=>URL.revokeObjectURL(output),30000)
          } catch {setExportError('图片导出失败，请重试。')}
        }
        const menu=()=>h('label',{className:'pilot-diagram-menu'},'切换图 ',h('select',{'aria-label':'切换架构图',value:view.id,onChange:e=>go(e.target.value)},
          h('option',{value:data.entry},'系统总图'),...data.views.filter(v=>v.parent===data.entry).map(p=>h('optgroup',{key:p.id,label:p.label},
            h('option',{value:p.id},p.label+' ·模块图'),...data.views.filter(v=>v.parent===p.id).map(v=>h('option',{key:v.id,value:v.id},v.label))))))
        const legend=()=>h(React.Fragment,null,h('div',{className:'pilot-diagram-legend'},...Object.entries({flow:['#386b55','调用 /执行'],data:['#5d7f9e','数据 /结果'],event:['#b47b43','消息 /恢复'],control:['#8b71a7','权限 /控制']}).map(([key,[color,label]])=>h('span',{key},h('i',{style:{background:color}}),label))),h('p',{className:'pilot-diagram-boundary-key'},'实线框：执行位置或服务依赖。虚线框：会话、文件或原生调用范围。节点颜色区分原生能力与Pilot程序，不表示数据可信或权限等级。'))
        const graphic=()=>h('div',{ref:canvas,className:'pilot-diagram-canvas',tabIndex:0,'aria-label':'架构流程图画布'},
          h('div',{className:'pilot-diagram-svg',style:{width:scale?view.diagram.width*scale:'100%'},'data-diagram-id':view.id,
            onClick:act,onKeyDown:e=>{if(['Enter',' '].includes(e.key))act(e)},dangerouslySetInnerHTML:{__html:view.svg}}))
        const controls=()=>h('div',{className:'pilot-diagram-controls'},
          h('button',{type:'button',onClick:()=>adjust(.8),'aria-label':'缩小架构图'},'−'),h('button',{type:'button',onClick:()=>adjust(1.25),'aria-label':'放大架构图'},'+'),
          h('button',{type:'button',onClick:()=>setScale(null)},'适应宽度'),full&&h('button',{type:'button',onClick:fitAll},'查看全图'),
          h('button',{type:'button',onClick:()=>save('svg')},'保存SVG'),h('button',{type:'button',onClick:()=>save('png')},'保存PNG'))
        const detail=()=>selected&&data.components[selected]&&h('details',{className:'pilot-diagram-detail',open:true},
          h('summary',null,data.components[selected].label+' ·职责与代码'),h('button',{type:'button',onClick:()=>setSelected('')},'关闭详情'),
          h('ul',null,...data.components[selected].points.map((p,i)=>h('li',{key:i},p))),
          h('p',null,...data.components[selected].refs.map(p=>h('code',{key:p},p))),
          data.components[selected].tools.length>0&&h('p',null,...data.components[selected].tools.map(t=>h('code',{key:t},'pilot_infra_'+t))))
        return h('section',{ref:top,className:'pilot-architecture','data-pilot-architecture':true,'data-architecture-page':view?.id},
          h('div',{className:'pilot-diagram-heading'},h('h2',null,view?.id==='system'?'Pilot 系统架构总图':'Pilot ·'+(view?.label??'项目架构')),
            h('button',{type:'button',disabled:busy,onClick:read},busy?'正在读取…':error?'重试读取架构图':'刷新架构图')),
          error&&h('p',{role:'alert'},error),!data&&busy&&h('p',{role:'status'},'正在读取当前架构图…'),
          data&&h(React.Fragment,null,
            h('nav',{className:'pilot-diagram-breadcrumb','aria-label':'架构图层级'},...trail.map((p,i)=>h(React.Fragment,{key:p.id},i>0&&h('span',{'aria-hidden':true},'›'),i===trail.length-1?h('strong',{'aria-current':'page'},p.id===data.entry?'系统总图':p.label):h('button',{type:'button',onClick:()=>go(p.id)},p.id===data.entry?'系统总图':p.label)))),
            h('div',{className:'pilot-diagram-nav'},menu(),h('button',{type:'button',onClick:()=>setFull(true)},'全屏看图')),
            legend(),!full&&h(React.Fragment,null,controls(),graphic(),detail()),
            h('p',{className:'pilot-diagram-hint'},'点击图中的模块或边界进入分图；静态设计图，'+data.updated_at+'。'),
            exportError&&h('p',{role:'alert'},exportError),
            full&&h('dialog',{ref:dialog,className:'pilot-diagram-full','aria-label':'Pilot架构图全屏画布',onCancel:()=>setFull(false)},
              h('header',null,h('strong',null,view.id===data.entry?'Pilot 系统架构总图':view.label),view.parent&&h('button',{type:'button',onClick:()=>go(view.parent)},'返回上级图'),h('button',{type:'button',onClick:()=>setFull(false)},'关闭全屏')),
              h('div',{className:'pilot-diagram-full-toolbar'},menu(),controls()),graphic(),detail(),exportError&&h('p',{role:'alert'},exportError)),
            h('details',{className:'pilot-diagram-boundaries'},h('summary',null,'实现边界与代码依据'),h('p',null,data.intro),h('ul',null,...data.boundaries.map((p,i)=>h('li',{key:i},p))),
              ...data.configuration.warnings.map((p,i)=>h('p',{key:i,role:'alert'},p)))))
      }
      function UsageGuide() {
        const [guide,setGuide]=React.useState(null), [error,setError]=React.useState(''), [busy,setBusy]=React.useState(false), [query,setQuery]=React.useState('')
        const request=React.useRef(0)
        const read=async()=>{
          const id=++request.current;setBusy(true);setError('')
          try {
            const response=await ctx.connection.rpc.call('/pilot-infra-api','guide',{})
            if(!response.ok)throw new Error('Guide unavailable')
            if(id===request.current)setGuide(response.value)
          } catch {
            if(id===request.current){setGuide(null);setError('无法读取当前使用说明，请重试。')}
          } finally { if(id===request.current)setBusy(false) }
        }
        React.useEffect(()=>{read();return()=>{request.current++}},[])
        const list=(items,ordered=false)=>h(ordered?'ol':'ul',null,...items.map((text,i)=>h('li',{key:i},text)))
        const features=guide?.features.filter(item=>JSON.stringify(item).toLowerCase().includes(query.trim().toLowerCase()))??[]
        return h('section',{className:'pilot-usage-guide','data-pilot-guide':true},
          h('h2',null,'使用 Pilot'),
          h('div',{className:'pilot-guide-toolbar'},
            h('label',null,'搜索功能',h('input',{type:'search',value:query,placeholder:'例如：配额、源码、回滚、预算',onChange:e=>setQuery(e.target.value)})),
            h('button',{type:'button',disabled:busy,onClick:read},busy?'正在读取说明…':error?'重试读取说明':'刷新说明')),
          error&&h('p',{role:'alert'},error),
          !guide&&busy&&h('p',{role:'status'},'正在读取当前功能与登记条件…'),
          guide&&h(React.Fragment,null,
            h('p',null,guide.intro),
            h('p',{className:'pilot-guide-meta'},'说明更新：'+guide.updated_at+' · 共'+guide.features.length+'项功能'),
            h('details',{className:'pilot-guide-config'},h('summary',null,'当前登记条件'),
              h('p',null,'读取时间：'+new Date(guide.configuration.checked_at).toLocaleString('zh-CN',{timeZone:'Asia/Shanghai'})),
              h('p',null,'模型：'+(guide.configuration.models.join('、')||'尚未登记')),
              h('p',null,'引擎环境：'+(guide.configuration.engines.join('、')||'尚未登记')),
              h('p',null,'源码配方：'+(guide.configuration.engine_recipes.join('、')||'尚未登记')),
              h('p',null,'独立CPU执行器：'+guide.configuration.cpu_executors+'个'),
              list(guide.configuration.execution_quotas.map(q=>q.server+'：当前设备配额 '+(q.gpu_ids.length?q.gpu_ids.map(g=>'GPU '+g).join('、'):'暂无')+'；'+(q.executor_registered?'推理执行器已登记':'推理执行器未登记'))),
              ...guide.configuration.warnings.map((text,i)=>h('p',{key:i,role:'alert'},text)),h('p',null,guide.configuration_note)),
            !query.trim()&&h('section',null,h('h3',null,'从需求到交付'),list(guide.quick_start,true)),
            !query.trim()&&guide.environment_setup&&h('details',{className:'pilot-guide-feature','data-pilot-environment-flow':true},
              h('summary',null,h('strong',null,guide.environment_setup.title)),h('p',null,guide.environment_setup.summary),
              list(guide.environment_setup.steps,true),h('strong',null,'当前边界'),list(guide.environment_setup.limits)),
            ...guide.groups.map(group=>{
              const rows=features.filter(item=>item.group===group.id)
              return rows.length?h('section',{key:group.id},h('h3',null,group.title),...rows.map(item=>h('details',{key:item.id,className:'pilot-guide-feature'},
                h('summary',null,h('strong',null,item.title),h('span',{'data-availability':item.availability.state},item.availability.label)),
                h('p',null,item.summary),h('p',null,h('strong',null,'入口：'),item.entry),
                item.workflow&&h(React.Fragment,null,h('p',null,h('strong',null,'典型场景：'),item.workflow.scenario),
                  h('p',null,h('strong',null,'执行者：'),item.workflow.actors),h('h4',null,'资源来源'),list(item.workflow.resources)),
                h('h4',null,'操作流程'),list(item.steps,true),
                item.workflow&&h(React.Fragment,null,h('h4',null,'需要你决定的地方'),list(item.workflow.decisions),
                  h('h4',null,'交付结果'),list(item.workflow.outputs),h('h4',null,'失败与恢复'),list(item.workflow.failure)),
                item.roles&&h(React.Fragment,null,h('p',null,'默认协作：'+item.default_strategy+'（按证据选择）。'),
                  h('table',null,h('thead',null,h('tr',null,...['角色','使用场景','职责','默认行为'].map(title=>h('th',{key:title},title)))),
                    h('tbody',null,...item.roles.map(role=>h('tr',{key:role.id},...['title','when','responsibility','default'].map(key=>h('td',{key},role[key])))))),
                  h('h4',null,'协作策略'),list(item.strategies),h('h4',null,'程序与历史角色'),list(item.support_components)),
                item.example&&h('blockquote',null,item.example),h('p',{className:'pilot-guide-meta'},item.availability.note),h('strong',null,'使用边界'),list(item.limits)))):null
            }),
            !features.length&&h('p',{role:'status'},'没有匹配的功能，请换一个关键词。'),
            !query.trim()&&h('section',null,h('h3',null,'尚未完成的能力'),list(guide.pending))))
      }
      function FontSettings() {
        const snapshot = React.useSyncExternalStore(listener => ctx.on('theme/change', listener), () => ctx.theme.getTheme())
        const [error, setError] = React.useState('')
        const change = (role, size) => {
          try { ctx.theme.setFontSize(role, size); setError('') }
          catch (e) { setError(e.message) }
        }
        return h('section', { className: 'pilot-font-settings' },
          ...[['text', '会话字号', 10, 22], ['code', '代码字号', 10, 16]].map(([role, title, min, max]) =>
            h('div', { key: role, className: 'pilot-font-row' }, h('span', null, title),
              h('div', null, h('button', { type: 'button', 'aria-label': '减小' + title, disabled: snapshot.fontSizes[role] <= min,
                onClick: () => change(role, snapshot.fontSizes[role] - 1) }, '−'),
                h('output', { 'aria-label': title }, snapshot.fontSizes[role] + ' px'),
                h('button', { type: 'button', 'aria-label': '增大' + title, disabled: snapshot.fontSizes[role] >= max,
                  onClick: () => change(role, snapshot.fontSizes[role] + 1) }, '+')))),
          error && h('p', { role: 'alert' }, error))
      }
      ctx.effect(() => ctx.slots.inject('settings.general.item', () => ctx.slots.register({
        name: 'settings.general.item', id: 'font-size', priority: -100, order: 11,
      }, FontSettings)))
      // Official product welcome copy is irrelevant to this configured Pilot
      // Host. Finish its public onboarding slot without persisting an upstream
      // acknowledgement or touching the user's model/credential settings.
      function PilotWelcome({ complete }) {
        React.useEffect(() => complete(), [complete])
        return null
      }
      ctx.effect(() => ctx.slots.inject('settings.onboarding', () => ctx.slots.register({
        name: 'settings.onboarding', id: 'welcome-notice', priority: -100, order: -100,
      }, PilotWelcome)))
      ctx.effect(() => ctx.slots.inject('sidebar.brand.mark', () => ctx.slots.register({ name: 'sidebar.brand.mark', priority: -100 }, Mark)))
      ctx.effect(() => ctx.slots.inject('conversation.hero.brand.mark', () => ctx.slots.register({ name: 'conversation.hero.brand.mark', priority: -100 }, HeroBrand)))
      ctx.effect(() => ctx.slots.inject('sidebar.brand.name', () => ctx.slots.register({ name: 'sidebar.brand.name', priority: -100 },
        () => h('span', { style: { fontFamily: 'var(--dsw-font-family)', fontWeight: 650, fontSize: 20, letterSpacing: '-.5px' } }, 'Pilot'))))
      ctx.effect(() => ctx.slots.inject('settings.section', () => ctx.slots.register({ name: 'settings.section',
        id: 'pilot-guide', label: () => '使用说明', order: 30 }, UsageGuide)))
      ctx.effect(() => ctx.slots.inject('settings.section', () => ctx.slots.register({ name: 'settings.section',
        id: 'pilot-architecture', label: () => '项目架构', order: 40 }, ProjectArchitecture)))
      // Replace stock chrome through public slots without changing stored
      // permissions, developer preferences or model credentials.
      const Empty = () => null
      for (const id of ['developer-tools', 'performance-usage'])
        ctx.effect(() => ctx.slots.inject('settings.general.item', () => ctx.slots.register({
          name: 'settings.general.item', id, priority: -100,
        }, Empty)))
      ctx.effect(() => ctx.slots.inject('settings.action', () => ctx.slots.register({
        name: 'settings.action', id: 'open-document', priority: -100,
      }, Empty)))
      ctx.effect(() => ctx.slots.inject('settings.general.item', () => ctx.slots.register({ name: 'settings.general.item',
        id: 'current-version', priority: -100, order: 100 }, () => {
          const version = document.querySelector('meta[name="pilot-version"]')?.content
          return h('p', { style: { color: 'var(--dsw-alias-label-secondary)', fontSize: 12 } }, version ? 'Pilot ' + version : 'Pilot')
        })))
      // The pinned official layout bakes its product suffix at build time.
      // Normalize its title writes before they reach the browser tab, leaving
      // native Session/panel selection authoritative. Limit the override to
      // this document and restore it on unload; never edit transcript/history.
      ctx.effect(() => {
        const normalize = value => String(value).replace(/(^| — )DeepSeek Harness$/i, '$1Pilot')
        const own = Object.getOwnPropertyDescriptor(document, 'title')
        const native = Object.getOwnPropertyDescriptor(Document.prototype, 'title')
        if (native?.get && native?.set && (!own || own.configurable)) {
          Object.defineProperty(document, 'title', { configurable: true, enumerable: native.enumerable,
            get: () => native.get.call(document), set: value => native.set.call(document, normalize(value)) })
          document.title = document.title
          return () => { if (own) Object.defineProperty(document, 'title', own); else delete document.title }
        }
        const title = document.querySelector('title')
        if (!title) return
        const update = () => { const next = normalize(document.title); if (next !== document.title) document.title = next }
        const observer = new MutationObserver(update)
        observer.observe(title, { childList: true, subtree: true, characterData: true })
        update()
        return () => observer.disconnect()
      })
      // The native running indicator has a decorative whale but no mark slot
      // in this pinned client. Replace that decoration with a small spinner;
      // elapsed time, live status and screen-reader announcements stay native.
      ctx.effect(() => {
        const style = document.createElement('style')
        // This pinned HeroShell has a mark slot but no headline/copy slot.
        // Supply accessible product copy in our slot and hide only its adjacent
        // native headline/badge. No locale namespace or conversation data changes.
        style.textContent = `span:has(> [data-slot='conversation.hero.brand.mark'] > [data-pilot-hero-brand]) { min-width: 0; max-width: 100%; flex-shrink: 1; }
        span:has(> [data-slot='conversation.hero.brand.mark'] > [data-pilot-hero-brand]) + span { display: none !important; }
        [data-pilot-hero-brand] { display: flex; flex-direction: column; align-items: flex-start; gap: 16px; min-width: 0; max-width: 100%; font-family: var(--dsw-font-family); text-align: left; }
        .pilot-usage-guide table { width: 100%; border-collapse: collapse; table-layout: fixed; margin: 12px 0; font-size: inherit; }
        .pilot-usage-guide th, .pilot-usage-guide td { text-align: left; vertical-align: top; padding: 8px; border-bottom: 1px solid var(--dsw-color-border, #dce4df); overflow-wrap: anywhere; }
        .pilot-usage-guide th:first-child, .pilot-usage-guide td:first-child { width: 18%; font-weight: 600; }
        .pilot-architecture { color: var(--dsw-alias-label-primary); line-height: 1.5; }
        .pilot-diagram-heading { display:flex; justify-content:space-between; align-items:center; gap:10px; margin-bottom:12px; }
        .pilot-diagram-heading h2 { font-size:20px; margin:0; letter-spacing:-.4px; }
        .pilot-architecture button, .pilot-architecture select { border:1px solid #cbd6d2; border-radius:7px; padding:5px 8px; font:inherit; font-size:12px; color:inherit; background:transparent; cursor:pointer; }
        .pilot-architecture button:hover, .pilot-architecture button:focus-visible { border-color:#4f9170; outline-offset:2px; }
        .pilot-architecture button:disabled { opacity:.55; cursor:wait; }
        .pilot-diagram-breadcrumb { display:flex; flex-wrap:wrap; gap:6px; align-items:center; font-size:11px; margin:10px 0; }
        .pilot-diagram-breadcrumb button { padding:3px 6px; font-size:11px; }
        .pilot-diagram-nav { display:flex; flex-wrap:wrap; gap:8px; align-items:center; justify-content:space-between; margin:10px 0; }
        .pilot-diagram-menu { display:flex; align-items:center; gap:5px; font-size:11px; min-width:0; }
        .pilot-diagram-menu select { max-width:300px; min-width:0; }
        .pilot-diagram-legend { display:flex; flex-wrap:wrap; gap:12px; font-size:10px; color:var(--dsw-alias-label-secondary); margin:8px 0; }
        .pilot-diagram-legend span { display:inline-flex; align-items:center; gap:5px; }
        .pilot-diagram-legend i { width:15px; height:2px; display:block; }
        .pilot-diagram-boundary-key { font-size:12px; line-height:1.6; color:var(--dsw-alias-label-secondary); margin:4px 0 10px; }
        .pilot-diagram-controls { display:flex; flex-wrap:wrap; gap:5px; align-items:center; margin:8px 0; }
        .pilot-diagram-controls button { font-size:11px; padding:4px 7px; }
        .pilot-diagram-canvas { width:100%; overflow:auto; border:1px solid #d8e1e4; border-radius:12px; background:#fbfcfd; }
        .pilot-diagram-svg { margin:0 auto; max-width:none; }
        .pilot-diagram-svg svg { display:block; width:100%; height:auto; }
        .pilot-diagram-svg g[role='button']:focus-visible { outline:none; }
        .pilot-diagram-svg g[role='button']:hover > rect, .pilot-diagram-svg g[role='button']:focus-visible > rect,
        .pilot-diagram-svg g[role='button']:hover > path, .pilot-diagram-svg g[role='button']:focus-visible > path { stroke:#286348; stroke-width:3; }
        .pilot-diagram-hint { font-size:11px; color:var(--dsw-alias-label-secondary); margin:10px 0; }
        .pilot-diagram-detail { padding:12px; margin:12px 0; border:1px solid #d8e1e4; border-radius:8px; }
        .pilot-diagram-detail summary, .pilot-diagram-boundaries summary { cursor:pointer; font-weight:600; font-size:12px; }
        .pilot-diagram-detail code { display:inline-block; font-size:10px; margin:3px 6px 3px 0; overflow-wrap:anywhere; }
        .pilot-diagram-detail > button { float:right; }
        .pilot-diagram-detail ul, .pilot-diagram-boundaries ul { padding-left:20px; font-size:12px; }
        .pilot-diagram-boundaries { margin-top:14px; font-size:12px; }
        dialog.pilot-diagram-full { position:fixed; inset:8px; width:calc(100vw - 16px); height:calc(100vh - 16px); max-width:none; max-height:none; margin:0; border:1px solid #b8c8c0; border-radius:12px; padding:12px; color:#243c49; background:#f6f8fa; box-sizing:border-box; }
        .pilot-diagram-full::backdrop { background:rgba(19,34,27,.28); }
        .pilot-diagram-full > header { display:flex; align-items:center; justify-content:space-between; gap:10px; height:32px; }
        .pilot-diagram-full > header strong { font-size:17px; margin-right:auto; }
        .pilot-diagram-full-toolbar { display:flex; flex-wrap:wrap; align-items:center; justify-content:space-between; gap:6px; }
        .pilot-diagram-full > .pilot-diagram-canvas { height:calc(100% - 86px); }
        .pilot-diagram-full .pilot-diagram-detail { position:absolute; right:24px; bottom:24px; width:min(420px,70vw); max-height:45vh; overflow:auto; background:white; box-shadow:0 4px 24px #324e4330; }
        @media(max-width:600px) { .pilot-diagram-menu select { max-width:220px; } .pilot-diagram-full > .pilot-diagram-canvas { height:calc(100% - 126px); } }
        [data-chat-running] > span:last-child > span[aria-hidden='true'] {
          display: inline-block; width: 14px; height: 14px; box-sizing: border-box;
          border: 2px solid currentColor; border-right-color: transparent; border-radius: 50%;
          background: none; mask: none; animation: pilot-running .8s linear infinite;
        }
        [data-chat-running] > span:last-child > span[aria-hidden='true'] > * { display: none; }
        @keyframes pilot-running { to { transform: rotate(360deg); } }
        @media(prefers-reduced-motion:reduce) { [data-chat-running] > span:last-child > span[aria-hidden='true'] { animation: none; } }`
        document.head.append(style)
        return () => style.remove()
      })
      // There is no running-copy slot in this pinned client. Adapt only live
      // indicator text, including its existing polite status announcement.
      ctx.effect(() => {
        const update = () => {
          const normalize = value => value.replace(/深度求索中/g, '正在处理')
            .replace(/Deep diving/gi, 'Processing')
          for (const region of document.querySelectorAll('[data-chat-running]')) {
            const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT)
            while (walker.nextNode()) {
              const node = walker.currentNode
              const next = normalize(node.nodeValue)
              if (next !== node.nodeValue) node.nodeValue = next
            }
            for (const decoration of region.querySelectorAll('[data-shimmer-text]')) {
              const value = decoration.getAttribute('data-shimmer-text')
              const next = normalize(value)
              if (next !== value) decoration.setAttribute('data-shimmer-text', next)
            }
          }
        }
        const observer = new MutationObserver(update)
        observer.observe(document.body, { childList: true, subtree: true, characterData: true,
          attributes: true, attributeFilter: ['data-shimmer-text'] })
        update()
        return () => observer.disconnect()
      })
    }
    return { inject, apply }
  },
})
