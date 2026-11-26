/** Native browse UI/backend, with Pilot's existing directory scope. */
import BrowseDirectoryPicker, { fullyQualified } from '@deepseek-ai/dsh-host-directory-picker-browse'
import { DirectoryPickerError } from '@deepseek-ai/dsh-host-directory-picker'
import { lstat, realpath } from 'node:fs/promises'
import { isAbsolute, relative, resolve, sep } from 'node:path'

export const name = 'pilot-directory-picker'
export const inject = ['pilotWorkspace']
export class PilotDirectoryPicker extends BrowseDirectoryPicker {
  constructor(ctx) { super(ctx, { maxEntries: 1000 }); this.workspace = ctx.pilotWorkspace }
  inside(root, path) { const part=relative(root,path);return !isAbsolute(part)&&part!=='..'&&!part.startsWith('..'+sep) }
  async checked(path, code='directory-unreadable') {
    const home=this.workspace.inboxPath(),target=path??home
    const root=(this.workspace.directoryScopes?.()??[home]).find(r=>fullyQualified(target)&&this.inside(r,resolve(target)))
    if(!root)throw new DirectoryPickerError(code,String(target),'请选择Pilot工作区目录内的文件夹')
    try {
      const canonical=await realpath(target)
      if(!this.inside(root,canonical))throw new Error('目录越界')
      let current=resolve(target)
      for(;;){const stat=await lstat(current);if(stat.isSymbolicLink()||!stat.isDirectory())throw new Error('不能进入符号链接或非目录');if(relative(root,current)==='')break;current=resolve(current,'..')}
      return {root,target:canonical}
    }catch(error){throw new DirectoryPickerError(code,String(target),String(error.message))}
  }
  async list(path,signal) {
    signal?.throwIfAborted()
    const {root,target}=await this.checked(path),listing=await super.list(target,signal)
    const entries=[]
    for(const row of listing.entries){signal?.throwIfAborted();try{await this.checked(row.path);entries.push(row)}catch{/* A foreign symlink is not an available directory. */}}
    return {...listing,home:this.workspace.inboxPath(),crumbs:listing.crumbs.filter(c=>this.inside(root,c.path)).map(c=>relative(root,c.path)===''?{...c,name:'Pilot工作区'}:c),entries}
  }
  async createDirectory(path,name) {
    const {target}=await this.checked(path,'directory-create-failed')
    if(typeof name!=='string'||!name.trim()||name!==name.trim()||name.length>80||/[\x00-\x1f\x7f]/.test(name))throw new DirectoryPickerError('directory-create-failed',target,'文件夹名称需为1–80个字符，不能包含控制字符或首尾空格')
    try { return await super.createDirectory(target,name) }
    catch(error){if(error.code==='directory-exists')throw new DirectoryPickerError(error.code,error.path,'同名文件夹已存在，请选择已有文件夹后打开');throw error}
  }
}
export function apply(ctx){return new PilotDirectoryPicker(ctx)}
