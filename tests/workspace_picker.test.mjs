import assert from 'node:assert/strict'
import {mkdtempSync,mkdirSync,readdirSync,rmSync,symlinkSync} from 'node:fs'
import {tmpdir} from 'node:os'
import {join} from 'node:path'
import test from 'node:test'
import {PilotDirectoryPicker} from '../harness/runtime/plugins/workspace/directory-picker.mjs'
function fixture(t){const root=mkdtempSync(join(tmpdir(),'pilot-picker-'));t.after(()=>rmSync(root,{recursive:true,force:true}));const base=join(root,'workspaces');mkdirSync(base);const picker=Object.create(PilotDirectoryPicker.prototype);picker.config={maxEntries:1000};picker.workspace={inboxPath:()=>base};return {root,base,picker}}
test('native browse starts at the Pilot scope and creates a readable exact folder name',async t=>{
 const {base,picker}=fixture(t),before=await picker.list();assert.equal(before.path,base);assert.equal(before.home,base);assert.equal(before.crumbs.length,1)
 const path=await picker.createDirectory(base,'Qwen环境排查');assert.equal(path,join(base,'Qwen环境排查'));assert.deepEqual(readdirSync(base),['Qwen环境排查'])
 const after=await picker.list();assert.equal(after.entries[0].name,'Qwen环境排查');await assert.rejects(picker.createDirectory(base,'Qwen环境排查'),e=>e.code==='directory-exists'&&/选择已有文件夹/.test(e.message))
})
test('native navigation cannot browse or create outside the original Pilot scope',async t=>{
 const {root,base,picker}=fixture(t)
 for(const path of [root,join(base,'..'),'../escape']){await assert.rejects(picker.list(path));await assert.rejects(picker.createDirectory(path,'bad'))}
 assert.deepEqual(readdirSync(base),[])
 for(const name of ['../bad','a/b','..','x\n',' bad '])await assert.rejects(picker.createDirectory(base,name))
})
test('symbolic directories are neither listed nor usable as an ancestor',async t=>{
 const {root,base,picker}=fixture(t),foreign=join(root,'foreign');mkdirSync(foreign);mkdirSync(join(foreign,'inner'));symlinkSync(foreign,join(base,'link'),process.platform==='win32'?'junction':'dir')
 assert.deepEqual((await picker.list()).entries,[]);await assert.rejects(picker.list(join(base,'link','inner')));await assert.rejects(picker.createDirectory(join(base,'link'),'bad'));assert.deepEqual(readdirSync(foreign),['inner'])
})
test('cancelled directory scans preserve the native cancellation outcome',async t=>{
 const {picker}=fixture(t),abort=new AbortController();abort.abort(new Error('cancelled scan'));await assert.rejects(picker.list(undefined,abort.signal),/cancelled scan/)
})
