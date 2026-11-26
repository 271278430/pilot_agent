// Thin adapter over the installed official registry; no custom workspace format.
import {Context} from '@deepseek-ai/cordis';
import Storage from '@deepseek-ai/dsh-storage';
import * as JsonStorage from '@deepseek-ai/dsh-storage-json';
import * as StorageDomain from '@deepseek-ai/dsh-storage-domain';
import * as SessionJsonl from '@deepseek-ai/dsh-session-persistence-jsonl';
import WorkspaceRegistry from '@deepseek-ai/dsh-workspace';
import {mkdir, realpath} from 'node:fs/promises';
import {resolve, relative, isAbsolute, join} from 'node:path';

const input=JSON.parse(await new Promise((ok,bad)=>{let s='';process.stdin.setEncoding('utf8');process.stdin.on('data',c=>{s+=c;if(s.length>16384)bad(new Error('Oversize workspace request'));});process.stdin.on('end',()=>ok(s));}));
const root=await realpath(input.root), owned=resolve(root,'runs/projects');
const home=resolve(root,'.local/project-workspaces');
await mkdir(home,{recursive:true});
const ctx=new Context();
try {
  await ctx.plugin(Storage);
  await ctx.plugin(JsonStorage,{root:join(home,'storage')});
  await ctx.plugin(StorageDomain,{backend:'json',routes:{}});
  await ctx.plugin(SessionJsonl,{root:join(home,'sessions'),compression:'none'});
  await ctx.plugin(WorkspaceRegistry);
  const registry=ctx.workspaceRegistry;
  const view=w=>({id:w.id,title:w.title,path:w.path,updatedAt:w.updatedAt});
  let result;
  if(input.operation==='create'){
    const path=await realpath(input.path),part=relative(owned,path);
    if(!part||part.startsWith('..')||isAbsolute(part))throw new Error('Workspace outside managed projects');
    result=view(await registry.create(path,input.title));
  }else if(input.operation==='rename'){
    const workspace=registry.get(input.id);if(!workspace)throw new Error('Unknown workspace');
    await workspace.setTitle(input.title);result=view(workspace);
  }else if(input.operation==='remove'){
    const workspace=registry.get(input.id);if(!workspace)throw new Error('Unknown workspace');
    const expected=await realpath(input.path),actual=await realpath(workspace.path),part=relative(owned,actual);
    if(expected!==actual||!part||part.startsWith('..')||isAbsolute(part))throw new Error('Workspace identity/path mismatch');
    result={removed:await registry.delete(input.id)}; // Removes only registration, never directory or sessions.
  }else if(input.operation==='list')result=registry.list().map(view);
  else throw new Error('Unsupported workspace operation');
  process.stdout.write(JSON.stringify({ok:true,result})+'\n');
}catch(error){process.stdout.write(JSON.stringify({ok:false,error:String(error.message).slice(0,300)})+'\n');process.exitCode=1;}
finally{await ctx.fiber.dispose();}
