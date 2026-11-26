import { test } from 'node:test'
import assert from 'node:assert/strict'
import { familyHeaders, scopedArguments, apply } from '../harness/runtime/plugins/history/index.mjs'

const records = ['root-a', 'root-b'].map(id => ({header:{id,cwd:'/shared'}}))
records.push({header:{id:'expert-a',parentSession:'root-a',cwd:'/shared'}})
const exec = {agent:{session:{id:'root-a',header:records[0].header}},signal:new AbortController().signal}
test('same-directory conversations are separate, descendants retained', async () => {
  assert.deepEqual([...familyHeaders(records,exec.agent.session.header)].sort(), ['expert-a','root-a'])
  const query={listSessions:async()=>records}
  const args={query:'sentinel'}
  assert.deepEqual((await scopedArguments(query,'session_search',args,exec)).session_ids.sort(),['expert-a','root-a'])
  assert.equal(args.session_ids,undefined)
  for(const name of ['session_event_read','session_event_search','session_trace','session_event_trace'])
    await assert.rejects(scopedArguments(query,name,{session_id:'root-b',seq:1},exec),/limited/)
  for(const field of ['session_ids','parent_session_ids'])
    await assert.rejects(scopedArguments(query,'session_search',{[field]:['root-b']},exec),/limited/)
  assert.equal((await scopedArguments(query,'session_event_read',{session_id:'expert-a'},exec)).session_id,'expert-a')
})
test('missing or cyclic parent lineage fails closed, cancellation retained',async()=>{
  for(const parent of ['missing','cycle']) {
    const header={id:'cycle',parentSession:parent}
    assert.throws(()=>familyHeaders([],header),/lineage unavailable/)
  }
  const abort=new AbortController();abort.abort()
  await assert.rejects(scopedArguments({listSessions:async()=>records},'session_search',{}, {...exec,signal:abort.signal}))
})
test('official query implementation receives narrowed filters and unchanged tool contracts',async()=>{
  const defs=[], seen=[]
  const query={listSessions:async()=>records,readTitleSnapshots:async()=>[],searchSessions:async request=>{seen.push(request);return{items:[]}}}
  apply({tools:{register:def=>defs.push(def)},sessionQuery:query,systemPrompt:{section(){},getSectionOrder(){return 1}},agents:{turnBoundaryOf(){return{}}},logger:{warn(){}}},{maxSearchResults:10})
  assert.equal(defs.length,5)
  assert.equal(defs[0].timeoutMs,30000)
  assert.equal(typeof defs[0].presentCall,'function')
  await defs[0].execute({query:'sentinel'},exec)
  assert.deepEqual(seen[0].sessionFilters.find(row=>row.kind==='id').values.sort(),['expert-a','root-a'])
  assert.deepEqual(seen[0].sessionFilters.find(row=>row.kind==='cwd').values,['/shared'])
})
