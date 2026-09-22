const client = window.createLNbitsExtensionClient({extensionId: 'quakejs'})
const touchMode = window.matchMedia?.('(pointer: coarse)').matches === true
const $ = id => document.getElementById(id)
$('license-text').textContent = (window.QuakeLicenses || []).map(item => item.name + '\n\n' + item.text).join('\n\n')
const assetOrigin = new URL(document.querySelector('script[src*="/arena/engine.js"]').src).origin
$('source-instructions').textContent = ['engine', 'asset'].map(name => `curl -f '${assetOrigin}/quakejs/static/sources/${name}-source.js' | sed '1d;$d' | base64 --decode > ${name}-source.tar.gz`).join('\n')
const arena = {
  gameId: '', playerToken: '', player: null, game: null,
  session: null, module: null, assets: null, transports: new Map(),
  clients: new Map(), consumed: new Set(), rewarded: new Set(), owned: new Set(),
  tally: 0, joining: false, stopped: false, fragQueue: Promise.resolve(), engineMessages: []
}

function status(message) { $('status').textContent = message }
function canRespawn() { return arena.player && ['dead', 'left'].includes(arena.player.status) && arena.player.livesRemaining > 0 }
function entryStatus(ready = false) {
  if (arena.entryMode === 'payment') return ready ? 'Payment received. Your arena is ready.' : 'Payment received. Joining the arena…'
  const lives = Number(arena.player?.livesRemaining || 0)
  const action = ready ? 'Ready' : arena.entryMode === 'respawn' ? 'Respawning…' : 'Joining the arena…'
  return `${action} · ${lives} ${lives === 1 ? 'life' : 'lives'} left`
}
function showEntry() {
  $('overlay').hidden = false
  $('join-form').hidden = arena.player?.status === 'settling'
  $('invoice').hidden = true
  $('resume').hidden = true
  $('join-button').disabled = !!arena.engineFailed || arena.player?.status === 'settling'
  $('join-button').textContent = canRespawn() ? `Respawn · ${arena.player.livesRemaining} lives left` : 'Pay for 5 lives'
  $('address').hidden = canRespawn()
  $('address-label').hidden = canRespawn()
}
function feed(message) {
  const item = document.createElement('li')
  item.textContent = message
  $('feed').prepend(item)
  while ($('feed').children.length > 5) $('feed').lastChild.remove()
  setTimeout(() => item.remove(), 5000)
}
function sessionKey(name) { return `quakejs.${arena.gameId}.${name}` }
async function remember(name, value) { await client.setSessionValue(sessionKey(name), String(value)) }
async function recall(name) { return (await client.getSessionValue(sessionKey(name)))?.value || '' }
function command(text) {
  if (!arena.module?._Cbuf_AddText) return
  const pointer = arena.module.stringToNewUTF8(text + '\n')
  arena.module._Cbuf_AddText(pointer)
  arena.module._free(pointer)
}

function randomToken() { return [...crypto.getRandomValues(new Uint8Array(24))].map(v=>v.toString(16).padStart(2,'0')).join('') }
function stopPaymentWatch() {}
function wakeRefresh() { arena.transport?.sendJSON({type:'refresh'}) }
async function syncMatch() {
  if (!arena.module || arena.player?.status !== 'alive') return
  if (arena.enginePlayerId === arena.player.id) return
  arena.enginePlayerId=arena.player.id
  arena.engineReadyPlayerId=''
  arena.session={isHost:false}
  $('join-form').hidden=true; $('invoice').hidden=true; $('resume').hidden=true
  command(`setu lnbits_id ${arena.player.id}\nname ${arena.player.name.replace(/[^A-Za-z0-9_.-]/g,'_')}\nconnect webrtc-peer0`)
  status(entryStatus())
}
async function refresh() { applyState(await client.getPublicGame(arena.gameId,arena.playerToken)) }
function selectedMap() {
  const map = arena.game?.map || 'aggressor'
  const manifest = window.QuakeAssets.manifest
  if (!(manifest.maps || [manifest.map || 'aggressor']).includes(map)) throw new Error('This arena map is not installed. Reload the page to update game assets.')
  return map
}

function renderSize() {
  if (!touchMode) return {width:960, height:540}
  const box = $('viewport').getBoundingClientRect()
  const scale = Math.min(1, 960 / box.width, 540 / box.height)
  return {width:Math.max(2, Math.round(box.width * scale / 2) * 2), height:Math.max(2, Math.round(box.height * scale / 2) * 2)}
}
function engineOptions(session) {
  const size = renderSize()
  arena.renderSize = size
  const args = [
    '+set', 'com_basegame', 'baseoa',
    '+set', 'fs_basepath', '/', '+set', 'fs_homepath', '/',
    '+set', 'com_hunkMegs', '96', '+set', 'com_zoneMegs', '32',
    '+set', 'vm_game', '1', '+set', 'vm_cgame', '1', '+set', 'vm_ui', '1',
    '+set', 'r_mode', '-1', '+set', 'r_customwidth', String(size.width), '+set', 'r_customheight', String(size.height),
    '+set', 'r_fullscreen', '0', '+set', 'r_picmip', '1', '+set', 'com_maxfps', '0',
    '+set', 'r_hdr', '0', '+set', 'r_postProcess', '0', '+set', 'r_sunShadows', '0',
    '+set', 'r_shadowFilter', '0', '+set', 'r_ext_framebuffer_object', '0',
    '+set', 'model', 'major', '+set', 'headmodel', 'major',
    '+set', 'cl_allowDownload', '0', '+set', 'sv_allowDownload', '0',
    '+set', 'sv_pure', '1', '+set', 'sv_maxclients', String(arena.game?.maxPlayers || 8),
    '+set', 'sv_fps', '30', '+set', 'snaps', '30', '+set', 'cl_maxpackets', '30',
    '+set', 'cl_lanForcePackets', '0', '+set', 'sv_lanForceRate', '0',
    '+set', 'sv_maxRate', '25000', '+set', 'cl_packetdup', '1',
    '+set', 'cg_nopredict', '0', '+set', 'cg_smoothClients', '0',
    '+set', 'cg_errorDecay', '100', '+set', 'cl_timeNudge', '0',
    '+set', 'rate', '25000', '+set', 'bot_enable', '0',
    '+set', 'g_gametype', '0', '+set', 'fraglimit', '0', '+set', 'timelimit', '0',
    '+set', 'sv_reconnectlimit', '0', '+set', 'name', 'Player' + (arena.player?.slot || 1),
    '+setu', 'lnbits_id', arena.player?.id || 'spectator',
    '+bind', 'w', '+forward', '+bind', 's', '+back', '+bind', 'a', '+moveleft', '+bind', 'd', '+moveright',
    '+bind', 'SPACE', '+moveup', '+bind', 'MOUSE1', '+attack',
    ...(session.isHost ? ['+map', selectedMap()] : [])
  ]
  return {
    canvas: $('canvas'), arguments: args,
    _webrtc: {recvQueue: [], peers: {}, isHost: session.isHost},
    arenaTouchControls: touchMode,
    arenaInputEnabled() { return arena.player?.status === 'alive' && $('overlay').hidden && !document.hidden && (touchMode || document.pointerLockElement === $('canvas')) },
    arenaReady() {
      arena.engineReadyPlayerId = arena.enginePlayerId
      if (arena.player?.status === 'alive') {
        $('resume').hidden = false
        status(entryStatus(true))
        arena.entryMode = ''
      }
    },
    preRun: [module => {
      module.FS.mkdir('/baseoa')
      module.FS.writeFile('/baseoa/arena.pk3', arena.assets, {canOwn: true})
      arena.assets = null
      window.QuakeAssets.data = null
    }],
    print(line) {
      arena.engineMessages.push(String(line).slice(0, 500))
      if (arena.engineMessages.length > 80) arena.engineMessages.shift()
      if (/ERROR:|Error:|Couldn't load|Failed to load/.test(line)) console.warn('[quake engine]', line)
    },
    printErr(line) { arena.engineMessages.push(String(line).slice(0, 500)); if (arena.engineMessages.length > 80) arena.engineMessages.shift() },
    onExit() { engineStopped() },
    onAbort() { engineStopped() }
  }
}

function updateNetworkStatus() {
  const stats=arena.transport?.stats()
  if (!stats) return
  $('network').textContent=stats.rtt===null?'':`Ping ${Math.round(stats.rtt)} ms · jitter ${Math.round(stats.jitter)} ms`
  $('connection').textContent=arena.transport.socket?.readyState===WebSocket.OPEN ? (stats.silentMs>6000?'Connection stalled':'Online'):'Reconnecting…'
}

function engineStopped() {
  arena.engineFailed = true
  status('The game engine stopped. Reload to reconnect.')
  $('overlay').hidden = false
  $('resume').hidden = true
  $('join-button').disabled = true
}

function showInvoice(invoice) {
  clearTimeout(arena.invoiceTimer)
  if(invoice.expiresAt) arena.invoiceTimer=setTimeout(()=>{
    try { arena.transport.sendJSON({type:'refresh'}) } catch (_) {}
  },Math.max(1000,invoice.expiresAt*1000-Date.now()+1000))
  $('join-form').hidden=true; $('invoice').hidden=false
  $('invoice-qr').src=window.QUAKEJS_QR_DATA_URI(invoice.paymentRequest)
  $('invoice-text').value=invoice.paymentRequest
  $('copy-invoice').onclick=async()=>{
    try { await navigator.clipboard.writeText(invoice.paymentRequest);status('Invoice copied.') }
    catch (_) { $('invoice-text').focus();$('invoice-text').select() }
  }
  status('Waiting for payment…')
}
function applyState(response) {
  const previous=arena.player
  arena.game=response.game; arena.player=response.player
  $('arena-name').textContent=arena.game.name
  $('terms').textContent=`${arena.game.joinAmount} sats buys 5 lives · ${arena.game.prizePerKill} sats paid per kill after the ${arena.game.haircut}% arena fee. Each life is worth 1/5 of the entry fee; payouts are rounded down to whole sats.`
  $('players').textContent=`${arena.game.playersCount} / ${arena.game.maxPlayers} players`
  $('lives').textContent=`${arena.player?.livesRemaining||0} lives left`
  $('tally').textContent=`Won: ${response.won||0} sats`
  if(response.pendingWinnings) $('tally').textContent+=` · Pending: ${response.pendingWinnings} sats`
  if(response.failedWinnings) $('tally').textContent+=` · Payout needs review: ${response.failedWinnings} sats`
  $('role').textContent='Dedicated arena server'
  if (arena.player?.status==='alive') {
    $('join-form').hidden=true; $('invoice').hidden=true
    syncMatch().catch(error=>status(error.message))
  } else if (arena.player?.autoAdmit && arena.module && !arena.admitting) {
    arena.admitting=true
    arena.entryMode='payment'
    arena.transport.sendJSON({type:'admit'})
  } else if (arena.player) {
    if (previous?.status==='alive') {
      document.exitPointerLock?.()
      arena.enginePlayerId=''
      command('disconnect')
    }
    showEntry()
    status(canRespawn()?'Your next life is ready. Click Respawn; no payment is needed.':'No lives left. Pay for another five lives.')
  } else if (response.invoice) showInvoice(response.invoice)
  else if (response.invoiceExpired) {
    showEntry();status('Invoice expired. Create a new invoice for five lives.')
    remember('invoice-nonce','').catch(()=>{})
  }
  if (arena.player?.status==='alive') arena.admitting=false
}
async function join(event) {
  event.preventDefault()
  if(arena.joining || arena.engineFailed || !arena.module) return
  arena.joining=true; $('join-button').disabled=true
  try {
    if(canRespawn()) {
      arena.entryMode='respawn'
      status(entryStatus())
      arena.transport.sendJSON({type:'admit'})
      return
    }
    const lnAddress=$('address').value.trim()
    await client.setSessionValue('quakejs.address',lnAddress)
    let nonce=await recall('invoice-nonce')
    // Reuse a nonce after uncertain invoice creation; rotate only after funded lives were exhausted.
    if(!nonce || (arena.player && !arena.player.livesRemaining)) {
      nonce=randomToken();await remember('invoice-nonce',nonce)
    }
    const invoice=await client.createEntry(arena.gameId,arena.playerToken,{lnAddress,name:lnAddress.split('@')[0].slice(0,18)||'PLAYER',nonce})
    arena.entryMode='payment'
    showInvoice(invoice)
    await refresh()
  } catch(error) { status(error.message||'Could not join the arena.');$('overlay').hidden=false }
  finally { arena.joining=false;$('join-button').disabled=!!arena.engineFailed }
}

async function init() {
  if (!touchMode && !$('canvas').requestPointerLock) throw new Error('This browser needs touch controls or mouse capture support.')
  if (touchMode) {
    document.body.classList.add('touch-mode')
    $('control-help').textContent = 'Left stick to move · Swipe the right side to aim · Fire, Jump and Weapon buttons · Menu to pause controls. Landscape gives the widest view.'
    $('canvas').setAttribute('aria-label', 'Game: use the touch movement stick, swipe to aim, and action buttons')
    arena.touchControls = new window.QuakeTouchControls({
      root: $('touch-controls'), canvas: $('canvas'), overlay: $('overlay'),
      enabled: () => arena.player?.status === 'alive' && $('overlay').hidden && !arena.engineFailed && !document.hidden,
      command,
      look: (x, y) => arena.module?._Arena_TouchLook(x, y),
      menu: () => { $('overlay').hidden = false; arena.touchControls?.release() }
    })
    window.addEventListener('resize', () => {
      clearTimeout(arena.resizeTimer)
      arena.touchControls.release()
      arena.resizeTimer = setTimeout(() => {
        if (!arena.module || arena.engineFailed || arena.stopped) return
        const size = renderSize()
        if (size.width === arena.renderSize.width && size.height === arena.renderSize.height) return
        arena.renderSize = size
        command(`r_customwidth ${size.width}\nr_customheight ${size.height}\nvid_restart`)
      }, 750)
    })
  }
  if (!$('arena-shell').requestFullscreen) $('fullscreen').hidden = true
  const match=window.location.pathname.match(/\/games\/([a-f0-9]{48})/)
  arena.gameId=match?.[1]||''
  if(!arena.gameId) throw new Error('Arena id is missing.')
  arena.playerToken=await recall('native-player')
  if(!arena.playerToken) { arena.playerToken=randomToken();await remember('native-player',arena.playerToken) }
  $('address').value=(await client.getSessionValue('quakejs.address'))?.value||''
  await refresh()
  arena.transport=new window.QuakeTransport(arena.gameId,arena.playerToken,applyState,message=>{
    arena.admitting=false;status(message);$('connection').textContent='Reconnecting…'
    $('overlay').hidden=false;document.exitPointerLock?.()
  })
  arena.networkTimer=setInterval(updateNetworkStatus,1000)
  arena.assets = await window.QuakeAssets.load(value => {
    $('progress').value = Math.round(value * 100)
    $('loading').textContent = `Loading OpenArena · ${Math.round(value * 100)}%`
  })
  $('loading').textContent = 'Starting OpenArena…'
  arena.module = await window.QuakeArena(engineOptions({isHost: false}))
  if (arena.engineFailed) throw new Error('OpenArena could not start on this browser. No entry payment is required.')
  $('loading').textContent = 'OpenArena ready'
  $('progress').hidden = true
  $('join-button').disabled = false
  arena.transport.bind(arena.module)
  await refresh()
  await syncMatch()
}

document.addEventListener('visibilitychange', updateNetworkStatus)
$('fullscreen').addEventListener('click', async () => {
  try {
    if (document.fullscreenElement) await document.exitFullscreen()
    else await $('arena-shell').requestFullscreen()
  } catch (_) { feed('Fullscreen was denied by the browser.') }
})
document.addEventListener('fullscreenchange', () => {
  $('fullscreen').textContent = document.fullscreenElement ? 'Exit fullscreen' : 'Fullscreen'
})
$('join-form').addEventListener('submit', join)
$('join-button').addEventListener('click', join)
$('address').addEventListener('keydown', event => { if (event.key === 'Enter') join(event) })
$('resume').addEventListener('click', () => {
  if (arena.player?.status !== 'alive') return
  arena.entryMode = ''
  status(entryStatus(true))
  $('overlay').hidden = true
  $('canvas').focus()
  if (touchMode) { arena.touchControls?.update(); return }
  const failed = () => { $('overlay').hidden = false; status('Mouse capture was denied. Click Enter arena to retry.') }
  try { $('canvas').requestPointerLock()?.catch(failed) } catch (_) { failed() }
})
document.addEventListener('keydown', event => {
  if (event.code !== 'Escape' || arena.player?.status !== 'alive') return
  event.preventDefault()
  event.stopImmediatePropagation()
  $('overlay').hidden = false
  document.exitPointerLock?.()
}, true)
document.addEventListener('pointerlockchange', () => {
  if (touchMode) return
  if (document.pointerLockElement && !$('overlay').hidden) document.exitPointerLock?.()
  if (!document.pointerLockElement && arena.player?.status === 'alive') $('overlay').hidden = false
})
window.addEventListener('pagehide', () => {
  arena.stopped = true
  clearTimeout(arena.maintenanceTimer)
  clearTimeout(arena.wakeTimer)
  clearTimeout(arena.resizeTimer)
  clearTimeout(arena.invoiceTimer)
  stopPaymentWatch()
  arena.eventsUnsubscribe?.()
  clearInterval(arena.networkTimer)
  arena.transport?.close()
  // The server journals a disconnect once; unused lives remain available.
})
init().catch(error => { status(error.message || 'The arena could not load.'); $('connection').textContent = 'Unavailable' })
