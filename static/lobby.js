'use strict'
;(() => {
  const $ = id => document.getElementById(id)
  const id = document.body.dataset.lobby
  const base = '/quakejs/api/v1/lobby/' + encodeURIComponent(id)
  let socket,
    timer,
    reconnect,
    stopped = false,
    state = null,
    page = 1,
    creating = false
  let nonce = ''
  const key = 'quakejs.lobby.' + id + '.pending'
  try {
    $('creator-address').value =
      localStorage.getItem('quakejs.creatorAddress') || ''
    const pending = JSON.parse(localStorage.getItem(key) || 'null')
    if (pending) {
      nonce = pending.nonce
      $('game-title').value = pending.name
      $('entry-sats').value = pending.joinAmount
      $('creator-fee').value = pending.creatorHaircut
      $('creator-address').value = pending.lnAddress
      $('game-map').dataset.pending = pending.map
    }
  } catch (_) {}
  function random() {
    return [...crypto.getRandomValues(new Uint8Array(24))]
      .map(n => n.toString(16).padStart(2, '0'))
      .join('')
  }
  function text(tag, value, className = '') {
    const el = document.createElement(tag)
    el.textContent = value
    if (className) el.className = className
    return el
  }
  function preview() {
    if (!state) return
    const amount = Number($('entry-sats').value),
      fee = Number($('creator-fee').value)
    $('creator-fee').max = Math.max(0, 50 - state.adminHaircut)
    $('creator-address').required = fee > 0
    $('admin-fee').textContent =
      `Admin fee: ${state.adminHaircut}% of each life’s value.`
    $('fee-preview').textContent =
      Number.isInteger(amount) &&
      amount >= 100 &&
      Number.isInteger(fee) &&
      fee >= 0 &&
      fee + state.adminHaircut <= 50
        ? `Per kill: ${Math.floor((amount * (100 - state.adminHaircut - fee)) / 500)} sats to the killer · ${Math.floor((amount * fee) / 500)} sats to you. Rounding remainder stays in the arena wallet.`
        : 'Entry must be at least 100 whole sats; total fees cannot exceed 50%.'
    $('create-button').disabled = creating || !state.canCreate
  }
  function render(data) {
    state = data
    page = data.page
    if (!$('game-map').options.length) {
      for (const map of data.maps) {
        const option = new Option(map.label, map.value)
        $('game-map').add(option)
      }
      if ($('game-map').dataset.pending)
        $('game-map').value = $('game-map').dataset.pending
    }
    preview()
    const games = $('games')
    games.replaceChildren()
    if (!data.games.length)
      games.append(
        text('p', 'No games yet. Create one and invite your friends.', 'fine')
      )
    for (const game of data.games) {
      const full = game.playersCount >= game.maxPlayers
      const admin = game.publicCreated === false
      const item = text('a', '', admin ? 'game game-admin' : 'game')
      const name = text('span', game.name, 'game-name')
      if (admin) name.append(text('span', 'ADMIN', 'admin-badge'))
      item.href = '/quakejs/games/' + encodeURIComponent(game.id)
      const details = text('span', '', 'game-details')
      details.append(
        text('span', data.maps.find(map => map.value === game.map)?.label || game.map, 'game-map'),
        text('span', `Haircuts: ${game.haircut}% admin + ${game.creatorHaircut}% creator = ${game.haircut + game.creatorHaircut}% total`, 'game-fees')
      )
      item.append(
        name,
        text('span', `${game.playersCount}/${game.maxPlayers}${full ? ' · Full' : ''}`, 'game-players'),
        details,
        text('span', `${game.joinAmount} sats / 5 lives · ${game.prizePerKill} sats/kill`, 'game-price')
      )
      games.append(item)
    }
    $('previous').disabled = page <= 1
    $('next').disabled = page * 20 >= data.total
    $('page-info').textContent = `Page ${page} · ${data.total} games`
    const board = $('scoreboard')
    board.replaceChildren()
    if (!data.scoreboard.length)
      board.append(text('li', 'No paid kill winnings yet.'))
    data.scoreboard.forEach((entry, index) => {
      const row = document.createElement('li')
      row.append(
        text('span', `${index + 1}. ${entry.address}`, 'address'),
        text('strong', `${entry.sats} sats`)
      )
      board.append(row)
    })
  }
  async function load() {
    const response = await fetch(base + '?page=' + page, {cache: 'no-store'})
    if (!response.ok) throw Error('This public lobby is unavailable.')
    render(await response.json())
  }
  function connect() {
    if (stopped) return
    socket = new WebSocket(
      `${location.protocol === 'https:' ? 'wss:' : 'ws:'}//${location.host}${base}/ws`
    )
    socket.onopen = () => {
      $('lobby-status').textContent = 'Live'
      if (page !== 1) socket.send(JSON.stringify({type: 'page', page}))
    }
    socket.onmessage = event => {
      try {
        render(JSON.parse(event.data))
      } catch (_) {
        $('lobby-status').textContent = 'Unable to update lobby.'
      }
    }
    socket.onclose = () => {
      if (stopped) return
      if (state) state.canCreate = false
      preview()
      $('lobby-status').textContent =
        'Lobby disconnected or disabled. Reconnecting…'
      reconnect = setTimeout(connect, 5000)
    }
    socket.onerror = () => socket.close()
  }
  function changePage(next) {
    page = next
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify({type: 'page', page}))
    else load().catch(error => ($('lobby-status').textContent = error.message))
  }
  $('open-create').onclick = () => {
    $('create-dialog').showModal()
    $('create-dialog').scrollTop = 0
    $('game-title').focus({preventScroll: true})
  }
  $('close-create').onclick = () => $('create-dialog').close()
  $('create-dialog').addEventListener('close', () => $('open-create').focus())
  $('previous').onclick = () => changePage(Math.max(1, page - 1))
  $('next').onclick = () => changePage(page + 1)
  $('entry-sats').oninput = preview
  $('creator-fee').oninput = preview
  $('create-form').onsubmit = async event => {
    event.preventDefault()
    if (creating || !state?.canCreate) return
    creating = true
    preview()
    $('created-link').hidden = true
    const data = {
      name: $('game-title').value.trim(),
      joinAmount: Number($('entry-sats').value),
      map: $('game-map').value,
      creatorHaircut: Number($('creator-fee').value),
      lnAddress: $('creator-address').value.trim(),
      nonce: nonce || random()
    }
    nonce = data.nonce
    try {
      localStorage.setItem(key, JSON.stringify(data))
      localStorage.setItem('quakejs.creatorAddress', data.lnAddress)
    } catch (_) {}
    try {
      const response = await fetch(base + '/games', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify(data)
      })
      const result = await response.json()
      if (!response.ok) {
        if (response.status < 500) {
          nonce = ''
          try {
            localStorage.removeItem(key)
          } catch (_) {}
        }
        throw Error(
          typeof result.detail === 'string'
            ? result.detail
            : 'Check the game details and try again.'
        )
      }
      nonce = ''
      try {
        localStorage.removeItem(key)
      } catch (_) {}
      $('create-status').textContent =
        'Your game is ready. Share its link and join when you’re ready.'
      $('created-link').href =
        '/quakejs/games/' + encodeURIComponent(result.game.id)
      $('created-link').hidden = false
      $('create-dialog').close()
      await load().catch(error => {
        $('lobby-status').textContent = error.message
      })
    } catch (error) {
      $('create-status').textContent =
        error.message || 'Could not create the game. Retry shortly.'
    } finally {
      creating = false
      preview()
    }
  }
  timer = setInterval(() => {
    if (socket?.readyState === WebSocket.OPEN)
      socket.send(JSON.stringify({type: 'ping'}))
  }, 20000)
  window.addEventListener('pagehide', () => {
    stopped = true
    clearInterval(timer)
    clearTimeout(reconnect)
    socket?.close()
  })
  load()
    .catch(error => {
      $('lobby-status').textContent = error.message
    })
    .finally(connect)
})()
