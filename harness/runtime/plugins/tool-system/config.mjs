/** One private, revisioned source for Owner Web and CLI. No credential values. */
import {mkdirSync,readFileSync,writeFileSync,renameSync,unlinkSync,lstatSync,realpathSync} from 'node:fs'
import {join,resolve} from 'node:path'
import {randomUUID,createHash} from 'node:crypto'
export const ROLES=['infra-lead','infra-expert','repo-expert']
const record=v=>v!==null&&typeof v==='object'&&!Array.isArray(v)
const fail=message=>{throw new Error(message)}
export function validateServer(value){
  if(!record(value))fail('Server must be an object')
  const fields=['name','description','enabled','roles','access','transport','command','args','cwd','url','environmentRefs','headerRefs','toolCallTimeoutMs','image','workspaceRead','leaseSeconds']
  if(Object.keys(value).some(k=>!fields.includes(k)))fail('Unknown server field; use environment references for credentials')
  if(typeof value.name!=='string'||!/^[-A-Za-z0-9_]{1,32}$/.test(value.name))fail('Name must be 1–32 ASCII letters, digits, _ or -')
  if(!['stdio','streamable-http'].includes(value.transport))fail('Supported transports: stdio, streamable-http')
  if(typeof value.enabled!=='boolean')fail('enabled must be boolean')
  if(!Array.isArray(value.roles)||!value.roles.length||value.roles.some(r=>!ROLES.includes(r))||new Set(value.roles).size!==value.roles.length)fail('Select supported unique Pilot roles')
  if(!['reference','task'].includes(value.access))fail('access must be reference or task')
  const refs=(v={})=>{if(!record(v)||Object.keys(v).length>40||Object.entries(v).some(([k,r])=>!k||k.length>128||/[\r\n\0]/.test(k)||typeof r!=='string'||!/^[_A-Za-z][_A-Za-z0-9]{0,127}$/.test(r)))fail('Environment/header references must map keys to environment variable names');return v}
  const description=value.description??''
  if(typeof description!=='string'||description.length>500)fail('Description must be at most 500 characters')
  const timeout=value.toolCallTimeoutMs??60000
  if(!Number.isInteger(timeout)||timeout<1000||timeout>180000)fail('Timeout must be 1000–180000 milliseconds')
  const row={name:value.name,description,enabled:value.enabled,roles:[...value.roles],access:value.access,transport:value.transport,toolCallTimeoutMs:timeout}
  if(value.transport==='stdio'){
    if(typeof value.command!=='string'||!value.command.trim()||value.command.length>2048||/[\0\r\n]/.test(value.command))fail('Provide an executable; no shell command string')
    if(value.url!==undefined||value.headerRefs!==undefined)fail('HTTP fields are not valid for stdio')
    const args=value.args??[]
    if(!Array.isArray(args)||args.length>100||args.some(a=>typeof a!=='string'||a.length>4096||a.includes('\0')))fail('args must be an array of bounded strings')
    if(value.cwd!==undefined&&(typeof value.cwd!=='string'||value.cwd.length>2048||/[\0\r\n]/.test(value.cwd)))fail('Invalid cwd')
    Object.assign(row,{command:value.command,args,cwd:value.cwd??'',environmentRefs:refs(value.environmentRefs)})
    if(value.image!=null&&value.image!==''&&(typeof value.image!=='string'||!/^sha256:[a-f0-9]{64}$/.test(value.image)))fail('MCP container image must be a full local sha256 image ID')
    if(value.workspaceRead!==undefined&&typeof value.workspaceRead!=='boolean')fail('Invalid workspace snapshot option')
    if(value.leaseSeconds!==undefined&&(!Number.isInteger(value.leaseSeconds)||value.leaseSeconds<30||value.leaseSeconds>1800))fail('Container lease must be 30–1800 seconds')
    Object.assign(row,{image:value.image||null,workspaceRead:value.workspaceRead===true,leaseSeconds:value.leaseSeconds??900})
    for(const [k,r] of Object.entries(row.environmentRefs))if(/^(?:LD_|DYLD_|REPOPILOT_|DSH_|PILOT_DOCKER_|PILOT_MODEL_)/.test(k)||['PATH','HOME','SHELL','ENV','BASH_ENV','PYTHONPATH','PYTHONHOME','PYTHONINSPECT','NODE_OPTIONS','NODE_PATH','SUDO_ASKPASS','PILOT_MCP_SANDBOX_SPEC','PILOT_MCP_CONTAINER_ENV','PILOT_LEASE_SECONDS','CUDA_VISIBLE_DEVICES','NVIDIA_VISIBLE_DEVICES'].includes(k)||/^(?:REPOPILOT_|DSH_|PILOT_DOCKER_|PILOT_MODEL_)/.test(r))fail('MCP references cannot inherit control credentials or override the broker')
  }else{
    if(['command','args','cwd','environmentRefs','image','workspaceRead','leaseSeconds'].some(k=>value[k]!==undefined))fail('stdio fields are not valid for HTTP')
    let url;try{url=new URL(value.url)}catch{fail('Invalid MCP URL')}
    if(!['http:','https:'].includes(url.protocol)||url.username||url.password||url.hash||[...url.searchParams.keys()].some(k=>/^(?:api[-_]?key|token|access[-_]?token|password|secret|authorization)$/i.test(k)))fail('Use HTTP(S) URL without embedded credentials; tokens belong in header environment references')
    Object.assign(row,{url:url.href,headerRefs:refs(value.headerRefs)})
    if(Object.values(row.headerRefs).some(r=>/^(?:REPOPILOT_|DSH_|PILOT_DOCKER_|PILOT_MODEL_)/.test(r)))fail('HTTP MCP requires dedicated credentials, not control-model or Docker credentials')
  }
  return row
}
export function identity(row){return createHash('sha256').update(JSON.stringify(row)).digest('hex')}
function paths(root){
  const base=realpathSync(root),privateDir=join(base,'.local');mkdirSync(privateDir,{recursive:true,mode:0o700})
  if(lstatSync(privateDir).isSymbolicLink()||realpathSync(privateDir)!==resolve(privateDir))fail('Private configuration directory cannot be a symlink')
  return {file:join(privateDir,'tool-system.json'),lock:join(privateDir,'tool-system.lock')}
}
export function readConfig(root){
  const {file}=paths(root);let data
  try{if(lstatSync(file).isSymbolicLink())fail('Configuration cannot be a symlink');data=readFileSync(file,'utf8')}catch(e){if(e.code==='ENOENT')return {version:1,revision:0,servers:[]};throw e}
  if(data.length>150000)fail('Configuration too large')
  const value=JSON.parse(data)
  if(value.version!==1||!Number.isSafeInteger(value.revision)||value.revision<0||!Array.isArray(value.servers)||value.servers.length>20||Object.keys(value).some(k=>!['version','revision','servers'].includes(k)))fail('Invalid tool configuration')
  const servers=value.servers.map(validateServer)
  if(new Set(servers.map(s=>s.name)).size!==servers.length)fail('Duplicate server name')
  return {...value,servers}
}
export function changeConfig(root,operation,payload){
  const {file,lock}=paths(root);let acquired=false,temp
  try{
    try{writeFileSync(lock,'Owner tool configuration edit',{flag:'wx',mode:0o600});acquired=true}catch(e){if(e.code==='EEXIST')fail('Another configuration writer is active; retry after it settles');throw e}
    const current=readConfig(root)
    if(payload.expected_revision!==current.revision)fail('Configuration revision conflict; reload before editing')
    let servers=[...current.servers]
    if(operation==='put'){
      const row=validateServer(payload.server),index=servers.findIndex(s=>s.name===row.name)
      if(index<0){if(servers.length>=20)fail('At most 20 servers');servers.push(row)}else servers[index]=row
    }else{
      const row=servers.find(s=>s.name===payload.name);if(!row)fail('Unknown MCP server')
      if(operation==='delete')servers=servers.filter(s=>s.name!==payload.name)
      else if(['enable','disable'].includes(operation))servers=servers.map(s=>s.name===payload.name?{...s,enabled:operation==='enable'}:s)
      else fail('Unknown configuration operation')
    }
    const next={version:1,revision:current.revision+1,servers}
    temp=file+'.'+randomUUID()+'.tmp';writeFileSync(temp,JSON.stringify(next,null,2)+'\n',{flag:'wx',mode:0o600});renameSync(temp,file);temp=undefined
    return next
  }finally{if(temp)unlinkSync(temp);if(acquired)unlinkSync(lock)}
}
export function nativeConfig(row,root){
  const resolveRefs=refs=>Object.fromEntries(Object.entries(refs??{}).map(([key,ref])=>{const value=process.env[ref];if(value===undefined||/[\r\n\0]/.test(value))fail('Missing or invalid credential reference: '+ref);return [key,value]}))
  const common={serverName:row.name,transport:row.transport,toolCallTimeoutMs:row.toolCallTimeoutMs,failOnStartupError:true,reconnect:{enabled:true,initialDelayMs:500,maxDelayMs:5000,maxAttempts:3},maxInstructionBytes:8192}
  if(row.transport==='stdio'){resolveRefs(row.environmentRefs);fail('stdio requires the execution-sandbox lifecycle; direct host launch is disabled')}
  return {...common,url:row.url,headers:resolveRefs(row.headerRefs)}
}
export function containerEnvironment(row){
  return Object.fromEntries(Object.entries(row.environmentRefs??{}).map(([name,ref])=>{
    const value=process.env[ref];if(value===undefined||/[\r\n\0]/.test(value))fail('Missing or invalid credential reference: '+ref)
    return [name,value]
  }))
}
