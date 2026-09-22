<template id="page-quakejs">
  <div class="row q-col-gutter-md">
    <div class="col-12 col-lg-4 q-gutter-y-md">
      <q-card>
        <q-card-section>
          <h6 class="text-subtitle1 q-my-none">QuakeJS settings</h6>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <q-form @submit="saveSettings" class="q-gutter-md">
            <q-toggle
              :model-value="settings.enabled"
              :label="
                settings.enabled ? 'Disable paid arenas' : 'Enable paid arenas'
              "
              :disable="saving || !canSave"
              @update:model-value="toggleEnabled"
            ></q-toggle>
            <q-toggle
              :model-value="settings.allowPublicCreation"
              label="Allow public game creation"
              :disable="saving || !canSave"
              @update:model-value="togglePublicCreation"
              ><q-tooltip
                >creates a public page that can be used to create
                games</q-tooltip
              ></q-toggle
            >
            <div v-if="settings.allowPublicCreation && settings.publicLobbyUrl">
              <q-btn
                flat
                color="primary"
                icon="open_in_new"
                label="Open public lobby"
                :href="settings.publicLobbyUrl"
                target="_blank"
                rel="noopener noreferrer"
              ></q-btn>
            </div>
            <q-select
              v-model="settings.walletId"
              :options="walletOptions"
              label="Wallet"
              filled
              dense
              emit-value
              map-options
              :disable="!wallets.length"
            ></q-select>
            <q-input
              v-model.number="settings.haircut"
              type="number"
              min="0"
              max="50"
              step="1"
              label="Service fee (%)"
              filled
              dense
              :rules="[
                value =>
                  (Number.isInteger(value) && value >= 0 && value <= 50) ||
                  'Enter a whole percentage from 0 to 50'
              ]"
            ></q-input>
            <div v-if="!wallets.length" class="text-caption">
              Create a Lightning wallet with send and receive permissions to set
              up paid arenas.
            </div>
            <q-btn
              type="submit"
              color="primary"
              label="Save settings"
              :loading="saving"
              :disable="!canSave"
            ></q-btn>
          </q-form>
        </q-card-section>
      </q-card>
      <q-card v-if="server.canManage">
        <q-card-section>
          <h6 class="text-subtitle1 q-my-none">Server capacity</h6>
        </q-card-section>
        <q-card-section class="q-pt-none">
          <q-form @submit="saveCapacity" class="q-gutter-md">
            <q-input
              v-model.number="server.maxMatches"
              type="number"
              min="1"
              max="32"
              step="1"
              label="Maximum simultaneous matches"
              filled
              dense
              :rules="[
                value =>
                  (Number.isInteger(value) && value >= 1 && value <= 32) ||
                  'Enter a whole number from 1 to 32'
              ]"
            ></q-input>
            <p class="text-caption">
              {{ server.activeMatches }} running · Up to eight players per match.
              This limit covers all QuakeJS arenas on this server. Lowering it
              leaves existing matches running and prevents new matches from
              starting until capacity is available. Empty servers stop after
              five minutes; stored lobby games do not each occupy a server slot.
            </p>
            <q-btn
              type="submit"
              color="primary"
              label="Save capacity"
              :loading="savingCapacity"
            ></q-btn>
          </q-form>
        </q-card-section>
      </q-card>
      <q-card>
        <q-card-section>
          <h6 class="text-subtitle1 q-mt-none q-mb-sm">QuakeJS arenas</h6>
          <p class="q-mb-sm">
            Create a map, set the entry price and share the arena link. Each
            payment buys five lives, with up to eight players in a match.
          </p>
          <p class="text-caption q-mb-none">
            Each verified frag pays one life’s value less the arena fee, rounded
            down to whole sats. Payouts continue in the background while players
            respawn. Saved settings apply to new arenas.
          </p>
        </q-card-section>
      </q-card>
    </div>
    <div class="col-12 col-lg-8">
      <q-card>
        <q-card-section class="row items-center q-gutter-sm">
          <h6 class="text-subtitle1 q-my-none">Arenas</h6>
          <q-space></q-space>
          <q-toggle
            v-model="showClosed"
            label="Show closed games"
            @update:model-value="pagination.page = 1; fetchGames()"
          ></q-toggle>
          <q-btn
            color="primary"
            label="New arena"
            icon="add"
            @click="createDialog = true"
            :disable="saving || !settings.enabled || !effectiveWalletId"
          ></q-btn>
          <q-btn
            flat
            round
            icon="refresh"
            aria-label="Refresh arenas"
            @click="fetchGames()"
            :loading="loading"
            ><q-tooltip>Refresh arenas</q-tooltip></q-btn
          >
        </q-card-section>
        <q-table
          :rows="games"
          :columns="columns"
          row-key="id"
          v-model:pagination="pagination"
          :rows-per-page-options="[5, 10, 25]"
          :loading="loading"
          @request="fetchGames"
          :grid="$q.screen.lt.md"
          flat
          wrap-cells
          no-data-label="No arenas yet. Create one to get started."
        >
          <template v-slot:body-cell-name="props">
            <q-td
              :props="props"
              style="max-width: 200px; overflow-wrap: anywhere"
            >
              <a
                :href="publicUrl(props.row)"
                target="_blank"
                rel="noopener noreferrer"
                class="text-primary"
                v-text="props.row.name"
              ></a>
            </q-td>
          </template>
          <template v-slot:body-cell-actions="props">
            <q-td :props="props" class="text-right" style="white-space: nowrap">
              <q-btn
                flat
                round
                dense
                icon="open_in_new"
                aria-label="Open arena in new tab"
                :href="publicUrl(props.row)"
                target="_blank"
                rel="noopener noreferrer"
                ><q-tooltip>Open arena in new tab</q-tooltip></q-btn
              >
              <q-btn
                flat
                round
                dense
                icon="receipt_long"
                aria-label="View payouts"
                @click="showPayouts(props.row)"
                ><q-tooltip>Payouts</q-tooltip></q-btn
              >
              <q-btn
                flat
                round
                dense
                icon="content_copy"
                aria-label="Copy arena link"
                @click="copyGame(props.row)"
                ><q-tooltip>Copy arena link</q-tooltip></q-btn
              >
              <q-btn
                flat
                round
                dense
                color="negative"
                icon="close"
                aria-label="Close arena"
                @click="requestDeleteGame(props.row)"
                ><q-tooltip>Close arena</q-tooltip></q-btn
              >
            </q-td>
          </template>
          <template v-slot:item="props">
            <div class="col-12 q-pa-sm">
              <q-card bordered flat>
                <q-card-section>
                  <a
                    :href="publicUrl(props.row)"
                    target="_blank"
                    rel="noopener noreferrer"
                    class="text-primary text-subtitle2"
                    style="overflow-wrap: anywhere"
                    v-text="props.row.name"
                  ></a>
                  <div
                    class="text-caption q-mt-sm"
                    v-text="
                      mapLabel(props.row.map) +
                      ' · ' +
                      props.row.joinAmount +
                      ' sats / 5 lives · ' +
                      props.row.haircut +
                      '% fee'
                    "
                  ></div>
                  <div
                    class="text-caption"
                    v-text="
                      props.row.playersCount +
                      ' / ' +
                      props.row.maxPlayers +
                      ' players · ' +
                      props.row.status
                    "
                  ></div>
                </q-card-section>
                <q-card-actions>
                  <q-btn
                    flat
                    icon="open_in_new"
                    label="Open"
                    aria-label="Open arena in new tab"
                    :href="publicUrl(props.row)"
                    target="_blank"
                    rel="noopener noreferrer"
                  ></q-btn>
                  <q-btn
                    flat
                    label="Payouts"
                    @click="showPayouts(props.row)"
                  ></q-btn>
                  <q-btn
                    flat
                    icon="content_copy"
                    label="Copy link"
                    @click="copyGame(props.row)"
                  ></q-btn>
                  <q-space></q-space>
                  <q-btn
                    flat
                    round
                    color="negative"
                    icon="close"
                    aria-label="Close arena"
                    @click="requestDeleteGame(props.row)"
                  ></q-btn>
                </q-card-actions>
              </q-card>
            </div>
          </template>
        </q-table>
      </q-card>
    </div>
  </div>
  <q-dialog v-model="createDialog" position="top">
    <q-card class="q-pa-lg lnbits__dialog-card">
      <h6 class="text-subtitle1 q-mt-none">New arena</h6>
      <q-form @submit="createGame" class="q-gutter-md">
        <q-input
          v-model.trim="gameForm.name"
          label="Title"
          filled
          dense
          maxlength="80"
          :rules="[value => !!value || 'Enter an arena title']"
        ></q-input>
        <q-input
          v-model.number="gameForm.joinAmount"
          type="number"
          min="100"
          max="1000000"
          step="1"
          label="Entry sats for 5 lives (minimum 100)"
          filled
          dense
          :rules="[
            value =>
              (Number.isInteger(value) && value >= 100 && value <= 1000000) ||
              'Enter 100 to 1,000,000 whole sats'
          ]"
        ></q-input>
        <q-select
          v-model="gameForm.map"
          :options="maps"
          label="Map"
          filled
          dense
          emit-value
          map-options
        ></q-select>
        <div class="row q-gutter-sm">
          <q-btn
            type="submit"
            color="primary"
            label="Create arena"
            :loading="creating"
            :disable="!canCreate"
          ></q-btn>
          <q-btn flat label="Cancel" v-close-popup></q-btn>
        </div>
      </q-form>
    </q-card>
  </q-dialog>
  <q-dialog v-model="payoutDialog.show">
    <q-card style="width: 900px; max-width: 95vw">
      <q-card-section>
        <h6
          class="text-subtitle1 q-my-none"
          v-text="payoutDialog.title + ' payouts'"
        ></h6>
        <p class="text-caption q-mt-sm q-mb-none">
          Pending or failed payouts do not block respawning. Check the payment
          hash in your wallet before resolving an uncertain payment.
        </p>
      </q-card-section>
      <q-table
        flat
        wrap-cells
        :rows="payoutDialog.rows"
        :columns="payoutColumns"
        row-key="id"
        no-data-label="No payouts for this arena yet."
      >
        <template v-slot:body-cell-hash="props"
          ><q-td
            :props="props"
            style="max-width: 240px; overflow-wrap: anywhere"
            v-text="props.value"
          ></q-td
        ></template>
      </q-table>
      <q-card-actions align="right"
        ><q-btn flat label="Close" v-close-popup></q-btn
      ></q-card-actions>
    </q-card>
  </q-dialog>
  <q-dialog v-model="deleteDialog.show">
    <q-card class="lnbits__dialog-card">
      <q-card-section
        ><h6 class="text-subtitle1 q-my-none">Close arena</h6></q-card-section
      >
      <q-card-section class="q-pt-none">
        <p
          v-text="'Close ' + (deleteDialog.game?.name || 'this arena') + '?'"
        ></p>
        <p class="text-caption q-mb-none">
          This ends the match and makes unused lives unplayable. Outstanding
          invoices may still be paid, but will not reopen the arena. There are
          no automatic refunds. Payment records and pending payouts are kept.
        </p>
      </q-card-section>
      <q-card-actions align="right">
        <q-btn flat label="Cancel" v-close-popup></q-btn>
        <q-btn
          color="negative"
          label="Close arena"
          :loading="!!deletingGameId"
          @click="deleteGame(deleteDialog.game)"
        ></q-btn>
      </q-card-actions>
    </q-card>
  </q-dialog>
</template>
