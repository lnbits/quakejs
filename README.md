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

When installing from a Git clone, Git LFS must be installed and available to your
Git client. Run `git lfs install` and `git lfs pull` inside the extension checkout.
`static/arena/baseoa/arena.pk3` should be about 186 MiB, and
`static/sources/asset-source.js` about 803 MiB. A roughly 134-byte file is an LFS
pointer and cannot be used as a game asset. On NixOS, a temporary `nix-shell`
does not make Git LFS available to an already running desktop Git client; install
it in that client's environment too. After restoring missing files, hard-refresh
the game page. The loader retries invalid cached assets once from the server.

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

Five lives cost the arena's entry fee, with a minimum of 100 sats. Each death
consumes one life. Disconnecting, reloading or timing out also consumes the
current admitted life, without a winner payout; the other unused lives stay saved.
Opening the in-game menu does not disconnect, and the player remains vulnerable.
An operator shutdown or engine crash preserves the current life unless a death
was already journaled. A verified player kill queues a payout of
`floor(entry_fee * (100 - service_fee_percent) / 500)` sats to the killer's
Lightning address. Suicide and world deaths have no winner payout. Routing fees
are charged by LNbits to the arena wallet in addition to the payout amount.
Maintain enough wallet balance to settle outstanding payouts.

For example, a 100-sat entry and 5% service fee pays 19 sats per kill after rounding.
The public page displays this net prize. Existing
arenas retain their own entry price and fee when settings or other arenas change.

Public arena links include server-rendered Open Graph and Twitter card metadata.
The public `/quakejs/games/{arena_id}/share.jpg` image shows the stored entry price
over the supplied artwork. Crawlers need no account or JavaScript. Image rendering
runs outside the event loop, with bounded concurrency and a 32-price memory cache.
The site must be publicly reachable for social services to fetch the preview;
those services can cache old previews. Artwork provenance and the rendering prompt
are in `static/share/README.md`; the bundled font license is alongside it.

Eight players can be active in one match. A full arena refuses admission; existing
unused lives remain in its ledger. The default is four running matches. An
LNbits server admin can change **Server capacity → Maximum simultaneous matches**
to 1–32 in the QuakeJS backend. The saved limit applies across all owners and
survives restarts; it replaces the environment-only cap. Lowering it never ends
existing matches. Ordinary arena owners cannot change this server-wide limit.
This is a resource bound, not a performance promise. See [the m.16 capacity estimate](CAPACITY.md) and benchmark the target
VPS before increasing it. Empty servers stop after
five minutes. A native process is limited to 512 MiB of address space and 64 file
descriptors. Game assets are shared through the operating system's file cache.

Browsers download the compressed game pack through `/quakejs/assets/arena.pk3`.
The extension prevents additional gzip compression inside LNbits and serves the
file in chunks, with ETags, range requests and a one-day public cache lifetime.
The loader includes the pack's SHA-256 in the URL and verifies the downloaded
bytes, so new asset versions use a different cache entry. Browser cache eviction
can still require a fresh download. No Caddy override is needed for this endpoint.

Entry invoices use LNbits' normal invoice service. Creation is bounded to 20
seconds, with a 30-second browser request timeout; uncertain requests retain
their reservation for reconciliation and reuse the same nonce on retry.
Payment updates use the WebSocket without an additional blocking HTTP refresh.

An authenticated arena owner can close a game even with players, unused lives or
unexpired invoices. Closure removes it from the lobby, blocks admission and new
invoices, and stops its engine on the next lease check (within about five seconds).
Unused lives become unplayable; there are no automatic refunds. Late payments
are recorded but cannot reopen an admin-closed game. Payment records are retained
and pending payouts continue. The Payouts button lists payout statuses for review.
Late payments can still restore automatically expired public games.

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
Aggressor, OA DM7, OA Minia, Czest1dm, OA Shine and Kaos 2. Retail Quake III data
is not included.
Each map has two Bitfest and two LNbits wall graphics. Original logo files and
their editable placements are in `engine/logos`; the map build preserves their
proportions and transparency without adding collision geometry.
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
timeouts consuming exactly one life without payouts, all selectable maps starting under
confinement, mobile-emulated touch movement, and the admin map/arena creation UI.
The tests use synthetic payments: a real Lightning wallet/backend payment cycle,
PostgreSQL, physical mobile devices and VPS load limits have not been validated.

Build instructions are in `engine/BUILD.md`. `make package` writes a ZIP and
SHA256 file under `dev/`, excluding local test databases, logs and build trees.
The package includes editable asset sources for license compliance; these are
not downloaded by players during normal gameplay.

## Public lobbies — version 1.1.0

Enable **Allow public game creation** in QuakeJS settings, then use **Open public
lobby** to share the public URL. Visitors can choose a map, title, entry price,
creator fee and Lightning address without an LNbits account. The lobby lists the
owner's active arenas with live player counts over WebSockets, and shows the top
ten Lightning addresses ranked by successfully paid kill winnings. Full winner
addresses are public; creator fees and pending payouts do not count. The normal
game dialog includes a **Create new game** link while the lobby is enabled.
Shared lobby links show a card reading “CREATE QUAKE MATCHES / AND CHARGE A JOIN
FEE”. The game page has a small centered “POWERED BY LNBITS” credit at the bottom.

Admin and creator percentages are each taken from the gross value of one life.
For example, a 100-sat entry buys five 20-sat lives. With a 5% admin fee and 10%
creator fee, each verified kill pays **17 sats to the killer and 2 sats to the
creator**, leaving 1 sat in the arena wallet. Transfers round down to whole sats;
the arena wallet retains rounding remainders. Combined admin and creator fees cannot exceed 50% of each life’s value.
Creator fees can be zero, in which case no creator address is required. Fees and
the funding wallet are fixed when the arena is created. The creator's fee is a
separate durable payout, retried independently of winnings and respawns.

Public games disappear after ten minutes with no connected players, unless they
have unused paid lives or live pending entry invoices. **Games with unused lives
stay listed.** Admin-created games never expire automatically. Financial history
and pending payouts are retained when a game expires, and a delayed successful
payment notification restores the game and its purchased lives. The existing
five-minute idle engine shutdown still applies: retaining lives does not keep a
game server running. Browsing the lobby and creating a game start no engine and
download no game packs.

Anonymous creation is limited to five requests per minute per source IP and fifty
active or recently created public games per owner (one-hour window). Creation is
idempotent and the owner limit is checked under a database lock. Public APIs never
accept wallet IDs or keys. Disabling public creation closes the lobby and removes
its game-dialog link; existing game links and paid lives continue to work.

Restart LNbits after updating to apply migration 9 and load the new background
maintenance task, then refresh browser tabs. Migration 9 preserves existing
arenas and payment records and can resume after an interrupted update. No core
changes, additional dependencies or external services are required.

## Release 1.0.4

This release fixes recovery of long match journals, fences lease renewal by run,
recovers incomplete cached asset downloads, and prevents packaging missing LFS
content or local credentials/data. Payment amounts and the five-life flow are
unchanged. Schedule an LNbits restart after updating and hard-refresh game tabs.
Run `make package` only after `git lfs pull`; it checks the native/game manifests,
required maps and VMs, and ZIP integrity before writing the release checksum.
