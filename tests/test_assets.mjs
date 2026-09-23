import assert from 'node:assert/strict'
import {webcrypto} from 'node:crypto'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'

const source=await readFile(new URL('../static/arena/loader.js',import.meta.url),'utf8')
const pack=new Uint8Array(200).fill(42)
const digest=Buffer.from(await webcrypto.subtle.digest('SHA-256',pack)).toString('hex')

function loader(responses) {
  const calls=[]
  const context={window:{},Uint8Array,TextDecoder,crypto:webcrypto,fetch:async(url,options)=>{
    assert.equal(url,'/quakejs/static/arena/baseoa/arena.pk3?v='+digest)
    calls.push(options.cache)
    assert.ok(responses.length,'Asset retries must be bounded')
    return new Response(responses.shift())
  }}
  vm.runInNewContext(source,context)
  const assets=context.window.QuakeAssets
  assets.manifest={size:pack.length,sha256:digest}
  return {assets,calls}
}

test('valid cached assets do not cause another download',async()=>{
  const {assets,calls}=loader([pack])
  assert.deepEqual(await assets.load(()=>{}),pack)
  assert.deepEqual(calls,['force-cache'])
})

test('cached LFS pointer is replaced by the restored game pack',async()=>{
  const {assets,calls}=loader(['version https://git-lfs.github.com/spec/v1\noid sha256:test\n',pack])
  assert.deepEqual(await assets.load(()=>{}),pack)
  assert.deepEqual(calls,['force-cache','reload'])
})

test('persistent missing LFS files give an actionable error without looping',async()=>{
  const pointer='version https://git-lfs.github.com/spec/v1\n'
  const {assets,calls}=loader([pointer,pointer])
  await assert.rejects(assets.load(()=>{}),/Game files are missing from this server/)
  assert.deepEqual(calls,['force-cache','reload'])
})

test('wrong hash is retried but never returned to the engine',async()=>{
  const corrupt=new Uint8Array(pack.length).fill(43)
  const {assets,calls}=loader([corrupt,corrupt])
  await assert.rejects(assets.load(()=>{}),/integrity check/)
  assert.deepEqual(calls,['force-cache','reload'])
})

test('oversized response is refused and retried once',async()=>{
  const {assets,calls}=loader([new Uint8Array(pack.length+1),pack])
  assert.deepEqual(await assets.load(()=>{}),pack)
  assert.deepEqual(calls,['force-cache','reload'])
})
