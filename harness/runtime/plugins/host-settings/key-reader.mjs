/** Private pipe bridge: use the pinned native parser, never a second credential store. */
import {parseCredentialsDocument} from '@deepseek-ai/dsh-credentials-local'
import {lstatSync,readFileSync} from 'node:fs'
import {dirname} from 'node:path'
try {
  let input='';for await(const chunk of process.stdin)input+=chunk
  const {path,ref}=JSON.parse(input)
  if(typeof ref!=='string'||!/^(REPOPILOT_API_KEY|PILOT_MODEL_(NOAUTH|KEY_[A-F0-9]{32}))$/.test(ref))throw new Error()
  const stat=lstatSync(path)
  if(stat.isSymbolicLink()||lstatSync(dirname(path)).isSymbolicLink()||(process.platform!=='win32'&&(stat.mode&0o077)))throw new Error()
  const value=parseCredentialsDocument(readFileSync(path,'utf8'),path).refs.get(ref)
  process.stdout.write(JSON.stringify({value:value??null}))
}catch{process.stderr.write('Model credential unavailable');process.exitCode=1}
