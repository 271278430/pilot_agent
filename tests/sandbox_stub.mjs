/** Scripted execution seam for native protocol/approval tests, never a production option. */
import {Service} from '../harness/runtime/node_modules/@deepseek-ai/cordis/lib/index.js'
export const name='scripted-sandbox-test-seam'
export function apply(ctx){return new class extends Service {
 constructor(){super(ctx,'pilotExecutionSandbox')}
 prepare(row){return Promise.resolve({id:'scripted-only',spec_path:'unused-scripted-only',command:row.command,args:row.args,cwd:row.cwd||process.cwd()})}
 stop(){return Promise.resolve()}
 call(){return Promise.resolve({policy:{revision:0,probe_image:null},leases:[],max_active:4,active_count:0,supported:false,boundary:'scripted-only'})}
}}
