import {readFileSync} from 'node:fs'
import {resolve} from 'node:path'
import {Context} from '@deepseek-ai/cordis'
import Tools from '@deepseek-ai/dsh-tools'
import SystemPrompt from '@deepseek-ai/dsh-system-prompt'
import * as Mcp from '@deepseek-ai/dsh-mcp-client'
import {readConfig,changeConfig,nativeConfig} from './config.mjs'
const input=process.argv.slice(2),take=key=>{const n=input.indexOf(key);if(n<0)return undefined;const value=input[n+1];input.splice(n,2);return value}
const root=resolve(take('--root')??process.cwd()),file=take('--file'),revision=take('--revision'),[op,name]=input
try{
  let value
  if(op==='list')value=readConfig(root)
  else if(['put','delete','enable','disable'].includes(op))value=changeConfig(root,op,{name,server:op==='put'?JSON.parse(readFileSync(file,'utf8')):undefined,expected_revision:Number(revision)})
  else if(op==='test'){
    const row=readConfig(root).servers.find(s=>s.name===name);if(!row)throw new Error('Unknown MCP server')
    const ctx=new Context();let fiber,timer,timedOut=false
    try{
      await ctx.plugin(SystemPrompt);await ctx.plugin(Tools)
      fiber=ctx.plugin(Mcp,nativeConfig(row,root));timer=setTimeout(()=>{timedOut=true;fiber.dispose().catch(()=>{})},20000)
      await fiber.await();if(timedOut)throw new Error('Connection timed out')
      value={server:name,status:'connection_verified',checked_at:new Date().toISOString(),registered_tools:ctx.tools.definitions().map(d=>({name:d.name,description:d.description})),note:'Isolated CLI connection closed after discovery; no tool called. Web uses its own native connection.'}
    }catch{throw new Error('MCP connection test failed; check endpoint, executable and environment references')}
    finally{clearTimeout(timer);await ctx.fiber.dispose()}
  }else throw new Error('Use list, put, delete, enable, disable or test')
  process.stdout.write(JSON.stringify(value,null,2)+'\n')
}catch(error){process.stderr.write(String(error.message).slice(0,500)+'\n');process.exitCode=1}
