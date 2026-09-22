import assert from 'node:assert/strict'
import {readFile} from 'node:fs/promises'
import test from 'node:test'
import vm from 'node:vm'

const source = await readFile(new URL('../static/public.js', import.meta.url), 'utf8')
// Exercise the real UI event handlers without downloading/starting the engine.
const handlers = source.replace(/^init\(\)\.catch\(.*$/m, '')
assert.notEqual(handlers, source)

test('closure hides an existing invoice and rejects a delayed invoice response', () => {
  const elements = new Map()
  const element = id => {
    if (!elements.has(id)) elements.set(id, {hidden: false, addEventListener() {}})
    return elements.get(id)
  }
  const context = vm.createContext({
    URL, clearTimeout, clearInterval, setTimeout,
    window: {
      createLNbitsExtensionClient: () => ({}),
      addEventListener() {},
      QUAKEJS_QR_DATA_URI: value => 'qr:' + value
    },
    document: {
      getElementById: element,
      querySelector: () => ({src: 'https://example.com/quakejs/static/arena/engine.js'}),
      addEventListener() {}
    }
  })
  vm.runInContext(handlers, context)
  vm.runInContext(`
    arena.game = {status: 'active'}
    showInvoice({paymentRequest: 'first-invoice'})
  `, context)
  assert.equal(element('invoice').hidden, false)
  vm.runInContext(`
    applyState({game: {status: 'closed', name: 'Closed arena', joinAmount: 100,
      haircut: 5, prizePerKill: 19, playersCount: 0, maxPlayers: 8}, player: null})
    showInvoice({paymentRequest: 'delayed-invoice'})
  `, context)
  assert.equal(element('invoice').hidden, true)
  assert.equal(element('join-form').hidden, true)
  assert.equal(element('resume').hidden, true)
  assert.equal(element('overlay').hidden, false)
  assert.match(element('status').textContent, /arena has been closed/)
  assert.equal(element('invoice-text').value, 'first-invoice')
})
