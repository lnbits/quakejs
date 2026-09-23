// Same-origin APIs. Wallet keys are only present on the authenticated admin page.
window.createLNbitsExtensionClient = () => {
  const wallets = JSON.parse(document.getElementById('quakejs-wallets')?.textContent || '[]')
  async function request(path, method = 'GET', data, token) {
    const headers = {'Content-Type': 'application/json'}
    if (token) headers.Authorization = 'Bearer ' + token
    else if (wallets[0]) headers['X-Api-Key'] = wallets[0].adminkey
    const controller = new AbortController()
    const timer = setTimeout(() => controller.abort(), 30000)
    try {
      const response = await fetch('/quakejs/api/v1/' + path, {method, headers, body: data ? JSON.stringify(data) : undefined, cache:'no-store', signal:controller.signal})
      const result = await response.json()
      if (!response.ok) throw new Error(typeof result.detail === 'string' ? result.detail : 'Request failed. Check the entered values.')
      return result
    } catch (error) {
      if (controller.signal.aborted) throw new Error(method === 'POST' && path.endsWith('/entry')
        ? 'The server is taking too long. Retry shortly; your invoice request has been kept.'
        : 'The server is taking too long. Please retry shortly.')
      throw error
    } finally { clearTimeout(timer) }
  }
  return {
    getSessionValue: async key => ({value:localStorage.getItem(key) || ''}),
    setSessionValue: async (key,value) => localStorage.setItem(key,value),
    getPublicGame: (id,token) => request('public/' + id,'GET',undefined,token),
    createEntry: (id,token,data) => request('public/' + id + '/entry','POST',data,token),
    listWallets: async () => ({wallets}),
    getSettings: () => request('settings'),
    saveSettings: data => request('settings','PUT',data),
    createGame: data => request('games','POST',data),
    listGames: data => request('games?' + new URLSearchParams(data)),
    deleteGame: id => request('games/' + id,'DELETE'),
    getPayouts: id => request('games/' + id + '/payouts'),
    notify: async (message,type) => window.Quasar.Notify.create({message,type})
  }
}
