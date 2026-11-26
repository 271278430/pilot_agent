import assert from 'node:assert/strict'
import {readFileSync} from 'node:fs'
import test from 'node:test'
import {apply,projectVersion} from '../harness/runtime/plugins/brand/index.mjs'

test('product version comes from the project section and rejects unsafe metadata',()=>{
  assert.equal(projectVersion('[tool.example]\nversion="99"\n[project]\nname="pilot"\nversion = "2.3.4rc1"\n[tool.other]\nversion="88"'),'2.3.4rc1')
  assert.throws(()=>projectVersion('[tool.other]\nversion="99"'),/Invalid/)
  assert.throws(()=>projectVersion('[project]\nversion="<script>"'),/Invalid/)
})

test('boot metadata and public manifest expose the same project version',async()=>{
  const routes=new Map();let transform
  apply({effect:fn=>fn(),webServer:{register:r=>routes.set(r.path,r.resource),tapIndex:fn=>{transform=fn}}})
  const expected=projectVersion(readFileSync(new URL('../pyproject.toml',import.meta.url),'utf8'))
  assert.ok(transform('<html><head><title>Upstream</title></head></html>').includes(`name="pilot-version" content="${expected}"`))
  assert.equal((await (await routes.get('/pilot.webmanifest')()).json()).version,expected)
  assert.ok(!readFileSync(new URL('../harness/runtime/plugins/brand/client.js',import.meta.url),'utf8').includes('Pilot 0.9.2'))
})
