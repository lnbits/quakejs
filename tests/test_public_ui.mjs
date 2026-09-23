import assert from 'node:assert/strict'
import {webcrypto} from 'node:crypto'
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
  vm.runInContext(`
    let disconnected = false, reloaded = false
    arena.transport = {close() { disconnected = true }}
    window.location = {reload() { reloaded = true }}
    applyState({game: arena.game, player: null})
  `, context)
  assert.equal(vm.runInContext('disconnected && reloaded', context), true)
})

test('closure between page load and initial state prevents assets and sockets starting', async () => {
  const elements = new Map()
  const element = id => {
    if (!elements.has(id)) elements.set(id, {hidden: false, addEventListener() {}, requestPointerLock() {}})
    return elements.get(id)
  }
  let assetsStarted = false, socketStarted = false
  const context = vm.createContext({
    URL, clearTimeout, clearInterval, setTimeout,
    window: {
      location: {pathname: '/quakejs/games/' + 'a'.repeat(48)},
      createLNbitsExtensionClient: () => ({
        getSessionValue: async () => ({value: 'stored-token'}),
        getPublicGame: async () => ({game: {status: 'closed', name: 'Closed', joinAmount: 100, haircut: 5}, player: null})
      }),
      addEventListener() {},
      QuakeAssets: {load() { assetsStarted = true }},
      QuakeTransport: class { constructor() { socketStarted = true } }
    },
    document: {
      getElementById: element,
      querySelector: () => ({src: 'https://example.com/quakejs/static/arena/engine.js'}),
      addEventListener() {}
    }
  })
  vm.runInContext(handlers, context)
  await vm.runInContext('init()', context)
  assert.equal(assetsStarted, false)
  assert.equal(socketStarted, false)
  assert.equal(element('progress').hidden, true)
  assert.equal(element('loading').textContent, 'Arena closed')
})

function invoiceUI(createEntry) {
  const elements = new Map(), stored = new Map(), messages = []
  const element = id => {
    if (!elements.has(id)) elements.set(id, {hidden: false, value: '', addEventListener() {}})
    return elements.get(id)
  }
  element('address').value = 'player@example.com'
  element('invoice').hidden = true
  const context = vm.createContext({
    URL, crypto: webcrypto, clearTimeout, clearInterval, setTimeout,
    window: {
      createLNbitsExtensionClient: () => ({
        getSessionValue: async key => ({value: stored.get(key)}),
        setSessionValue: async (key, value) => stored.set(key, value),
        createEntry,
        getPublicGame: async () => { throw new Error('Unexpected HTTP refresh') }
      }),
      QUAKEJS_QR_DATA_URI: value => 'qr:' + value,
      addEventListener() {}
    },
    document: {
      getElementById: element,
      querySelector: () => ({src: 'https://example.com/quakejs/static/arena/engine.js'}),
      addEventListener() {}
    },
    messages
  })
  vm.runInContext(handlers, context)
  vm.runInContext(`
    arena.gameId = 'arena'
    arena.playerToken = 'player-token'
    arena.module = {}
    arena.game = {status: 'active', name: 'Arena', joinAmount: 100, haircut: 5}
    arena.invoiceNonce = 'request-nonce'
    arena.transport = {sendJSON(message) { messages.push(message) }}
  `, context)
  return {context, element, stored, messages}
}

test('old expiry updates cannot overwrite invoice creation or change its retry nonce', async () => {
  let reject, started
  const ready = new Promise(resolve => { started = resolve })
  const pending = new Promise((_, failure) => { reject = failure })
  const requests = []
  const {context, element} = invoiceUI((id, token, data) => {
    requests.push(data.nonce)
    started()
    return pending
  })
  const attempt = vm.runInContext('join({preventDefault() {}})', context)
  await ready
  vm.runInContext(`
    applyState({game: arena.game, player: null, invoiceExpired: true, entryNonce: 'old-nonce'})
  `, context)
  assert.equal(element('status').textContent, 'Creating invoice…')
  assert.equal(element('join-button').disabled, true)
  reject(new Error('Invoice creation is pending. Retry shortly.'))
  await attempt
  assert.equal(element('join-button').disabled, false)
  // A previously exhausted paid entry must not rotate an uncertain new request.
  vm.runInContext("arena.player = {status: 'dead', livesRemaining: 0}", context)
  await vm.runInContext('join({preventDefault() {}})', context)
  assert.deepEqual(requests, ['request-nonce', 'request-nonce'])
  vm.runInContext(`
    applyState({game: arena.game, player: null, invoiceExpired: true, entryNonce: 'old-nonce'})
  `, context)
  assert.equal(vm.runInContext('arena.invoiceNonce', context), 'request-nonce')
  vm.runInContext(`
    applyState({game: arena.game, player: null, invoiceExpired: true, entryNonce: 'request-nonce'})
  `, context)
  assert.equal(vm.runInContext('arena.invoiceNonce', context), '')
})

test('invoice success uses the websocket and releases the button without another HTTP wait', async () => {
  const {context, element, messages} = invoiceUI(async () => ({paymentRequest: 'invoice'}))
  await vm.runInContext('join({preventDefault() {}})', context)
  assert.equal(element('invoice').hidden, false)
  assert.equal(element('invoice-text').value, 'invoice')
  assert.equal(element('join-button').disabled, false)
  assert.equal(messages[0].type, 'refresh')
})

test('an existing invoice from another tab keeps its actual nonce until expiry', async () => {
  const {context, stored} = invoiceUI(async () => ({paymentRequest: 'shared-invoice', nonce: 'other-tab'}))
  await vm.runInContext('join({preventDefault() {}})', context)
  assert.equal(stored.get('quakejs.arena.invoice-nonce'), 'other-tab')
  vm.runInContext(`
    applyState({game: arena.game, player: null, invoiceExpired: true, entryNonce: 'other-tab'})
  `, context)
  assert.equal(vm.runInContext('arena.invoiceNonce', context), '')
})

test('a late invoice response does not display a QR after payment already supplied lives', async () => {
  const {context, element, stored} = invoiceUI(async () => {
    vm.runInContext(`
      applyState({game: arena.game, player: {status: 'left', livesRemaining: 5}, entryNonce: 'request-nonce'})
    `, context)
    return {paymentRequest: 'already-paid'}
  })
  await vm.runInContext('join({preventDefault() {}})', context)
  assert.equal(element('invoice').hidden, true)
  assert.equal(stored.get('quakejs.arena.invoice-nonce'), '')
})

const clientSource = await readFile(new URL('../static/client.js', import.meta.url), 'utf8')
for (const phase of ['fetch', 'body']) {
  test(`API timeout releases a request stalled during ${phase}`, async () => {
    let deadline, stalledReady, cleared = false
    const ready = new Promise(resolve => { stalledReady = resolve })
    const context = vm.createContext({
      window: {}, AbortController,
      document: {getElementById: () => null},
      setTimeout(callback, milliseconds) { assert.equal(milliseconds, 30000); deadline = callback; return 1 },
      clearTimeout() { cleared = true },
      fetch: async (_, {signal}) => {
        const stalled = () => new Promise((resolve, reject) => {
          signal.addEventListener('abort', () => reject(new Error('aborted')))
          stalledReady()
        })
        return phase === 'fetch' ? stalled() : {ok: true, json: stalled}
      }
    })
    vm.runInContext(clientSource, context)
    const pending = vm.runInContext("window.createLNbitsExtensionClient().createEntry('arena','token',{nonce:'same-request'})", context)
    const failure = assert.rejects(pending, /taking too long.*request has been kept/)
    await ready
    deadline()
    await failure
    assert.equal(cleared, true)
  })
}
