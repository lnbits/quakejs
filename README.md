# QuakeJS for LNbits

OpenArena deathmatch with a dedicated game server managed by the Python
extension. The existing QuakeJS page, map chooser, fullscreen HUD, touch
controls and Lightning address memory are retained. No LNbits core changes.

## Installation and operation

The Linux x86-64 package contains the engine executable and assets.
LNbits starts and stops engine processes automatically. Players need a browser;
operators do not run Node, Docker, another network service or a separate game
server command. The only public connection is the existing LNbits HTTP/WebSocket
endpoint. HTTPS is required for normal internet deployment.

This implementation targets LNbits 1.6.2-rc1's invoice listener and wallet APIs.
Use one LNbits application worker and one shared data directory. An OS file lock
prevents duplicate game supervisors. Linux must support Landlock and seccomp;
the engine fails closed if its confinement cannot be installed. The bundled
static musl executable needs no Nix, system libc or compiler at runtime. Use
Linux 5.13 or newer with Landlock enabled and seccomp available. The extension
verifies the binary checksum and restores its execute bit after ZIP installation.

1. Install/enable the Python extension after the WASM transition below.
2. Select an owned Lightning wallet and enable paid arenas in Settings.
3. Set the service fee, then create an arena with a title, entry price and map.
4. Share the arena link. Payment confirmation arrives over the player WebSocket.

Five lives cost the arena's entry fee, with a minimum of 50 sats. Each death
consumes one life. Disconnecting, reloading or timing out also consumes the
current admitted life, without a winner payout; the other unused lives stay saved.
Opening the in-game menu does not disconnect, and the player remains vulnerable.
An operator shutdown or engine crash preserves the current life unless a death
was already journaled. A verified player kill queues a payout of
`floor(entry_fee * (100 - service_fee_percent) / 500)` sats to the killer's
Lightning address. Suicide and world deaths have no winner payout. Routing fees
are charged by LNbits to the arena wallet in addition to the payout amount.
Maintain enough wallet balance to settle outstanding payouts.

For example, a 50-sat entry and 5% service fee pays 9 sats per kill after rounding;
a 100-sat entry pays 19 sats. The public page displays this net prize. Existing
arenas retain their own entry price and fee when settings or other arenas change.

Public arena links include server-rendered Open Graph and Twitter card metadata.
The public `/quakejs/games/{arena_id}/share.jpg` image shows the stored entry price
over the supplied artwork. Crawlers need no account or JavaScript. Image rendering
runs outside the event loop, with bounded concurrency and a 32-price memory cache.
The site must be publicly reachable for social services to fetch the preview;
those services can cache old previews. Artwork provenance and the rendering prompt
are in `static/share/README.md`; the bundled font license is alongside it.

Eight players can be active in one match. A full arena refuses admission; existing
unused lives remain in its ledger. Four engine processes may run by default;
`QUAKEJS_MAX_MATCHES` accepts 1–32. This is a resource bound, not a performance
promise. Benchmark the target VPS before increasing it. Empty servers stop after
five minutes. A native process is limited to 512 MiB of address space and 64 file
descriptors. Game assets are shared through the operating system's file cache.

Closing an arena is refused while it has unused paid lives or unexpired invoices.
Disable new payments in Settings, allow existing players to finish, then close it.
The Payouts button lists the latest payout statuses and payment hashes for review.
A delayed confirmation for an already paid invoice reopens a closed arena so
those purchased lives can still be used.

## Payment and game trust boundaries

- Only LNbits core's successful incoming payment record funds an entry. Wallet,
  amount, payment hash and entry identity must match.
- Entry creation is idempotent. Life consumption and payout creation share an
  explicit database transaction. Duplicate death events do not spend another
  life or create another payout.
- Browser sessions are random bearer secrets stored locally and hashed in the
  database. They are sent in an Authorization header or the first WebSocket
  message, never in a URL. Same-origin WebSockets are required.
- The server assigns a connection's peer number and paid life. Clients cannot
  choose another peer or submit payable kill reports. Repeated native connection
  attempts cannot create another character for the same admission.
- The native server decides movement, damage and death. Private IPC and an
  fsynced death journal carry results to the extension. Chat and stdout are not
  payment inputs. Projectiles are removed when their owner leaves, before the
  engine's entity slot can be reused by another life.
- The native process receives no LNbits payment credentials. Landlock restricts
  files to game assets and its own working directory. Seccomp blocks opening
  network sockets, executing processes and accessing other processes. Normal
  operation uses a private inherited Unix socket, with bounded packet queues.
- Lightning payouts run outside the game loop. A failed or pending payout does
  not prevent the next funded life. The exact invoice and sending state are
  recorded before calling LNbits. An uncertain payment is reconciled by its hash;
  it is never automatically replaced or sent again.

These controls do not eliminate aimbots, wallhacks, collusion or trust in the
LNbits operator. This build has not received an independent security audit.
The implementation review, fixes and remaining boundaries are recorded in
[SECURITY_AUDIT.md](SECURITY_AUDIT.md). `make sandbox-check` uses the pinned build
toolchain to verify the native confinement rules.

## Recovery and backups

Back up `ext_quakejs.sqlite3` (or the extension schema in PostgreSQL) together
with the `quakejs/` directory under the LNbits data folder. That directory contains
private run journals and engine logs. Do not edit or delete unresolved journals.
A missing, malformed or out-of-sequence journal stops admissions for owner review.
Recorded events are replayed idempotently before restarting a match. Process
restart resets the map; unused funded lives are retained.

If a payout is pending, inspect its payment hash in the LNbits wallet. Do not send
a replacement invoice simply because a timeout occurred. A failed LNURL invoice
lookup is retried with backoff; after eight failures it requires owner review.
An ambiguous send remains pending until LNbits can establish its outcome.

Version 1.0.1 fixes BOLT11 expiry validation. Migration 8 requeues invoice lookup
failures only when no invoice or payment hash was ever stored; it does not reset
potentially sent payments. The HUD shows paid winnings, pending payouts and
amounts needing review separately. Restart LNbits and refresh game pages after
updating so the recovery migration and new HUD are loaded.

LNURL invoice preparation uses the same LNbits helper as Coinflip, passing the
payout amount in millisatoshis. Version 1.0.2 records known LNURL failure reasons
in the payout log and owner payout table, including provider amount limits,
invalid responses and blocked endpoints. Unknown provider messages are not
logged verbatim because they can contain private URLs or credentials. A 9-sat
frag payout can fail a provider minimum even if a larger Coinflip payout works.
Invoice preparation failures retain the unpaid amount and retry with backoff;
after eight attempts they require owner review. No payment is sent on this path.

The Python extension uses new native tables and migration 7. It leaves the old
WASM settings, games, players and payout records untouched. Old arenas and browser
session tokens are not imported. Finish or reconcile old WASM payments before
replacing that extension; then disable the WASM extension before enabling the
Python extension with the same `quakejs` identifier. Create new native arenas.

## Sources and development

The engine fork and asset versions are pinned in `engine/source.json`.
`engine/patch.mjs` contains the existing browser/game modifications;
`engine/patch-native.mjs`, `engine/native.c` and `engine/sandbox.h` implement the
private dedicated server transport and confinement. OpenArena assets contain
Aggressor, OA DM1, OA DM2 and Kaos 2. Retail Quake III data is not included.
Corresponding engine and editable asset source archives and licenses are supplied
under `static/sources` and `static/licenses`; see the in-game Credits panel.

Run `make check` from this extension folder for isolated fake-payment tests,
Python lint/format checks and JavaScript syntax checks. `make smoke` starts the
actual confined engine against a separate test database. The development browser
fixture uses fake payments only and is never imported by the extension.

Verified so far: SQLite concurrency across independent connections, duplicate
invoices and death events, five-life exhaustion and repurchase, payout timeout
recovery without resending, owner checks, map/amount validation, cross-origin
WebSocket rejection, two real browser clients joining and respawning, an actual
native frag creating one payout, WebSocket and engine disconnects and native
timeouts consuming exactly one life without payouts, all four maps starting under
confinement, mobile-emulated touch movement, and the admin map/arena creation UI.
The tests use synthetic payments: a real Lightning wallet/backend payment cycle,
PostgreSQL, physical mobile devices and VPS load limits have not been validated.

Build instructions are in `engine/BUILD.md`. `make package` writes a ZIP and
SHA256 file under `dev/`, excluding local test databases, logs and build trees.
The package includes editable asset sources for license compliance; these are
not downloaded by players during normal gameplay.
