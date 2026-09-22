window.PageQuakejs = {
  template: '#page-quakejs',
  data() {
    return {
      loading: false,
      saving: false,
      savingCapacity: false,
      server: {maxMatches: 4, activeMatches: 0, canManage: false},
      creating: false,
      createDialog: false,
      deletingGameId: '',
      payoutDialog: {show: false, title: '', rows: []},
      deleteDialog: {show: false, game: null},
      settings: {
        enabled: false,
        haircut: 0,
        walletId: '',
        allowPublicCreation: false,
        publicLobbyUrl: ''
      },
      gameForm: {
        name: 'QUAKEJS public arena',
        joinAmount: 100,
        map: 'aggressor'
      },
      maps: [],
      games: [],
      pagination: {
        sortBy: 'createdAt',
        descending: true,
        page: 1,
        rowsPerPage: 10,
        rowsNumber: 0
      },
      payoutColumns: [
        {
          name: 'kind',
          label: 'Payout',
          field: 'kind',
          align: 'left',
          format: value =>
            value === 'creator' ? 'Creator fee' : 'Kill winnings'
        },
        {name: 'amount', label: 'Sats', field: 'amount', align: 'right'},
        {name: 'status', label: 'Status', field: 'status', align: 'left'},
        {name: 'error', label: 'Details', field: 'error', align: 'left'},
        {
          name: 'hash',
          label: 'Payment hash',
          field: 'payment_hash',
          align: 'left'
        }
      ],
      columns: [
        {
          name: 'name',
          label: 'Arena',
          field: 'name',
          align: 'left',
          sortable: false
        },
        {
          name: 'map',
          label: 'Map',
          field: 'map',
          align: 'left',
          format: value => this.mapLabel(value)
        },
        {
          name: 'joinAmount',
          label: 'Entry sats',
          field: 'joinAmount',
          align: 'right',
          sortable: false
        },
        {
          name: 'haircut',
          label: 'Fee',
          field: 'haircut',
          align: 'right',
          format: value => value + '%',
          sortable: false
        },
        {
          name: 'players',
          label: 'Alive',
          field: 'playersCount',
          align: 'left',
          format: (value, row) => value + ' / ' + row.maxPlayers,
          sortable: false
        },
        {
          name: 'status',
          label: 'Status',
          field: 'status',
          align: 'left',
          sortable: false
        },
        {
          name: 'actions',
          label: '',
          field: 'id',
          align: 'right',
          sortable: false
        }
      ]
    }
  },
  computed: {
    wallets() {
      return (this.g.user?.wallets || []).filter(
        wallet =>
          wallet.walletType === 'lightning' &&
          wallet.canSendPayments &&
          wallet.canReceivePayments
      )
    },
    walletOptions() {
      return this.wallets.map(wallet => ({
        label: wallet.name,
        value: wallet.id
      }))
    },
    selectedWalletName() {
      return (
        this.wallets.find(wallet => wallet.id === this.effectiveWalletId)
          ?.name || ''
      )
    },
    effectiveWalletId() {
      return this.settings.walletId || this.wallets[0]?.id || ''
    },
    canSave() {
      return (
        !!this.effectiveWalletId &&
        Number.isInteger(this.settings.haircut) &&
        this.settings.haircut >= 0 &&
        this.settings.haircut <= 50
      )
    },
    canCreate() {
      return (
        !this.saving &&
        this.settings.enabled &&
        this.effectiveWalletId &&
        this.gameForm.name &&
        Number.isSafeInteger(Number(this.gameForm.joinAmount)) &&
        Number(this.gameForm.joinAmount) >= 100
      )
    }
  },
  async mounted() {
    if (!this.wallets.length) return
    this.loading = true
    try {
      await Promise.all([this.fetchSettings(), this.fetchGames()])
    } finally {
      this.loading = false
    }
  },
  methods: {
    async requestArenaApi(path, method = 'GET', data) {
      if (!this.wallets.length)
        throw new Error('Choose a Lightning wallet first.')
      const response = await LNbits.api.request(
        method,
        '/quakejs/api/v1/' + path,
        this.wallets[0].adminkey,
        data
      )
      return response.data
    },
    mapLabel(value) {
      return this.maps.find(map => map.value === value)?.label || value
    },
    async fetchSettings() {
      try {
        const response = await this.requestArenaApi('settings')
        this.settings = {...this.settings, ...(response.settings || {})}
        this.maps = response.maps || []
        this.server = response.server || this.server
        if (!this.settings.walletId && this.wallets.length)
          this.settings.walletId = this.wallets[0].id
      } catch (error) {
        this.showError(error)
      }
    },
    async toggleEnabled(enabled) {
      if (this.saving || !this.canSave) return
      const previous = this.settings.enabled
      this.settings.enabled = enabled
      if (!(await this.saveSettings())) this.settings.enabled = previous
    },
    async togglePublicCreation(allowed) {
      if (this.saving || !this.canSave) return
      const previous = this.settings.allowPublicCreation
      this.settings.allowPublicCreation = allowed
      if (!(await this.saveSettings()))
        this.settings.allowPublicCreation = previous
    },
    async saveSettings() {
      if (!this.canSave) return false
      this.saving = true
      try {
        this.settings = (
          await this.requestArenaApi('settings', 'PUT', {
            enabled: this.settings.enabled,
            walletId: this.effectiveWalletId,
            walletName: this.selectedWalletName,
            haircut: Number(this.settings.haircut || 0),
            allowPublicCreation: this.settings.allowPublicCreation
          })
        ).settings
        this.notify('QUAKEJS settings saved.', 'positive')
        return true
      } catch (error) {
        this.showError(error)
        return false
      } finally {
        this.saving = false
      }
    },
    async saveCapacity() {
      if (this.savingCapacity || !this.server.canManage) return
      this.savingCapacity = true
      try {
        const response = await this.requestArenaApi('server-settings', 'PUT', {
          maxMatches: this.server.maxMatches
        })
        this.server = response.server
        this.notify('Match capacity saved. Existing matches continue.', 'positive')
      } catch (error) {
        this.showError(error)
      } finally {
        this.savingCapacity = false
      }
    },
    async createGame() {
      if (!this.canCreate) return
      this.creating = true
      try {
        await this.requestArenaApi('games', 'POST', {
          name: this.gameForm.name,
          joinAmount: Number(this.gameForm.joinAmount),
          map: this.gameForm.map
        })
        this.createDialog = false
        this.pagination.page = 1
        this.notify('Arena created.', 'positive')
        await this.fetchGames()
      } catch (error) {
        this.showError(error)
      } finally {
        this.creating = false
      }
    },
    async showPayouts(game) {
      try {
        const response = await this.requestArenaApi(
          'games/' + game.id + '/payouts'
        )
        this.payoutDialog = {
          show: true,
          title: game.name,
          rows: response.payouts || []
        }
      } catch (error) {
        this.showError(error)
      }
    },
    async fetchGames(props = {}) {
      const pagination = props.pagination || this.pagination
      this.loading = true
      try {
        const response = await this.requestArenaApi(
          'games?' +
            new URLSearchParams({
              page: pagination.page,
              rowsPerPage: pagination.rowsPerPage,
              sortBy: pagination.sortBy,
              descending: pagination.descending
            })
        )
        this.games = response.games || []
        this.pagination = {
          ...pagination,
          rowsNumber: Number(response.total ?? this.games.length)
        }
      } catch (error) {
        this.showError(error)
      } finally {
        this.loading = false
      }
    },
    publicUrl(game) {
      return new URL(
        '/quakejs/games/' + encodeURIComponent(game.id),
        window.location.href
      ).href
    },
    async copyGame(game) {
      LNbits.utils.copyText(this.publicUrl(game))
    },
    requestDeleteGame(game) {
      this.deleteDialog = {show: true, game}
    },
    async deleteGame(game) {
      this.deletingGameId = game.id
      try {
        await this.requestArenaApi('games/' + game.id, 'DELETE')
        this.deleteDialog = {show: false, game: null}
        await this.fetchGames()
      } catch (error) {
        this.showError(error)
      } finally {
        this.deletingGameId = ''
      }
    },
    notify(message, type = 'info') {
      Quasar.Notify.create({type, message})
    },
    showError(error) {
      if (error?.response) LNbits.utils.notifyApiError(error)
      else this.notify(error?.message || String(error), 'negative')
    }
  }
}
