# QuakeJS implementation security review — 2026-09-22

Scope: the Python extension, public/admin routes, player sessions, WebSocket
admission and limits, SQLite life ledger, Lightning outbox, native engine bridge,
process confinement and browser integration. All changes are extension-local.
This is a code review with regression tests, not an independent penetration test
or a guarantee that the upstream C engine has no vulnerabilities.

## Fixed findings

| Finding | Impact and correction |
| --- | --- |
| Unpaid requests started native engines | An invoice request could occupy one of the limited engine slots before payment, including before checking entry eligibility. Invoice requests now perform availability checks only. Only admission with verified funded lives starts an engine. |
| Concurrent WebSocket handshakes bypassed the connection cap | The old cap counted completed player handshakes, after awaits. Capacity is now reserved synchronously before accepting a socket, including incomplete handshakes. Limits are 512 sockets overall and 32 per client IP. Cleanup releases reservations even when player detachment fails. |
| Expired heartbeats freed live engine slots | A delayed heartbeat could allow a second participant to receive a slot that the engine still authorized for another life. Slot allocation now counts every live ledger admission. Only authoritative death, disconnection or server recovery releases it. A live admission in the same run retains its identity. |
| Old server cleanup could affect a replacement | Lifecycle cleanup is serialized with server creation and verifies the match identity. Clearing an arena lease additionally requires its exact run ID, so an old run cannot clear a replacement's lease. |
| Payout reconciliation accepted unrelated payments | A matching hash and amount did not establish which wallet or frag initiated a payment. Used provider invoices are rejected before sending. Both the send result and reconciliation now verify outgoing direction, exact wallet, hash, amount, extension tag, payout ID and victim ID. A mismatch requires review and never credits winnings or triggers an automatic resend. |
| Alternate process-control syscalls remained available | The sandbox blocked kill/ptrace but omitted alternate signal, resource-limit and scheduling syscalls. These are now denied. This hardens containment if an engine vulnerability is exploited; it does not make a compromised referee trustworthy. |
| Redundant game log could exhaust its file limit | Player-controlled chat was also written to an engine game log. That log is now disabled. Bounded startup diagnostics and the separate durable payment journal remain enabled. |
| Malformed messages and public-page hardening | Non-string bearer tokens and excessively nested JSON fail cleanly. The game page restricts framing to the same origin, disables object embedding/base-URL overrides and sends no referrer. |

## Preserved controls

Eight simultaneous paid players; five lives per payment; integer payout amounts;
minimum entry fee of 50 sats; no public kill-report endpoint; native referee
journals; atomic life consumption and payout creation; duplicate-event protection;
one browser connection per admission; arena-scoped bearer-token hashes; owner
checks on management routes; map allowlisting; bounded packet queues; private
engine IPC with no network listener; no wallet keys in the engine environment;
no automatic resend after an ambiguous payment result.

Payment status remains event-driven through LNbits invoice notifications and
WebSockets, with background recovery. Failed payouts do not block respawning.
No database migration, LNbits core change or runtime dependency was added.

## Verification

- `make check smoke sandbox-check`: Python lint/format checks, JavaScript syntax
  checks, 41 tests, all four maps starting with the shipped confined engine, and
  a static native syscall probe using the same sandbox header.
- Regression cases include concurrency at capacity, unpaid startup prevention,
  incomplete handshakes, expired heartbeats, exact payment identity, valid payout
  completion, reused invoices, stale-run cleanup, forged kills, duplicate deaths,
  five-life consumption, payout failures and uncertain sends.
- The confinement probe verifies denied alternate signals, resource-limit and
  scheduling changes, Internet sockets, direct signals and external file reads.
- Two actual browser clients passed entry and respawn checks. A native frag
  created exactly one queued 9-sat payout; WebSocket loss, in-game disconnect and
  timeout each consumed one life without an extra payout. Payments were synthetic
  and the fixture was stopped after testing.
- Bundled native executable SHA256:
  `3091c0f067c647f1db6662587b388682f99659da61716d1874387d02fdb797cb`.

## Remaining boundaries and deployment considerations

- The browser can still be modified for aim assistance, wallhacks or collusion.
  Authoritative kills and paid admission do not prevent every gameplay cheat.
- Native engine code remains a trust boundary: a compromised referee could forge
  events for funded lives. The OS sandbox protects the surrounding host; it does
  not independently verify the referee's game simulation.
- Public invoice reservations still last five minutes and can be occupied by
  unpaid visitors. Removing those reservations trades temporary holds during payment
  for availability; this flow change is pending the owner's preference.
- Application limits do not stop a distributed traffic flood. Enforce TLS and
  appropriate proxy connection/body limits. Shared NAT users share the per-IP
  connection budget; a proxy must provide trustworthy client addressing.
- LNURL requests use LNbits' existing DNS-pinned, redirect-checked network helper.
  Its administrator setting allowing private IP targets remains authoritative;
  leave it disabled when accepting untrusted public Lightning addresses.
- Routing fees are paid by the operator's wallet in addition to winnings. The
  service fee is not a guaranteed upper bound on those costs.
- PostgreSQL, real Lightning settlement, physical phones and an eight-player
  Internet load test were not verified in this review. Upstream C code was not
  exhaustively fuzzed or proven free of CVEs. Upstream references consulted:
  [ioquake3 security policy](https://github.com/ioquake/ioq3/security) and
  [Debian's ioquake3 security tracker](https://security-tracker.debian.org/tracker/source-package/ioquake3).

Restart LNbits to load the patched Python code and rebuilt native executable.
Do not interrupt active matches casually: schedule the restart with players.

## Payout regression follow-up — version 1.0.1

A live failure report exposed an expiry-type bug missed by the earlier numeric
mock: the installed BOLT11 library returns a datetime for `expiry_date`. Invoice
validation now compares numeric issue time plus expiry duration. Tests exercise
the actual LNURL parsing and BOLT11 encoding/decoding, mocking only network and
payment I/O. Fresh pending incoming invoices on the same LNbits instance are
allowed; existing outgoing or settled invoices remain rejected.

Migration 8 retries only lookup failures without a stored invoice or hash, never
potentially sent payments. Pending and failed winnings are shown separately from
paid winnings. `make check` passes 52 tests including these regression cases.
Live provider diagnostics were not run: automatic approval review rejected
sending stored payout addresses and amounts to external diagnostic endpoints.

## Final release pass — version 1.0.4

- Fixed incomplete crash recovery for journals longer than one 256-event batch.
  Shutdown and recovery now drain all batches before marking a run recovered or
  releasing its funded lives. Tests model 512 previously settled events followed
  by an unsettled payable death, including repeated cleanup without double pay.
- Lease renewal now checks the exact run ID as well as worker ownership, so an
  obsolete match cannot renew a replacement's lease.
- The game loader recovers from a cached Git LFS pointer or truncated response
  with one fresh request, retaining the exact size and SHA256 checks. Persistent
  missing game data produces an explicit server-installation error.
- Release packaging rejects LFS pointer files, missing required assets, engine or
  game-pack manifest mismatches, symlinks and accidental environment/key/database/
  log files. It verifies all four maps and three game VMs are present, checks ZIP
  integrity, and replaces the release ZIP only after successful verification.
- `make check` passes 75 Python tests and five JavaScript loader tests; `make smoke`
  starts all four maps, and `make sandbox-check` passes the native syscall probe.
- Two real local browser clients passed entry, respawn, one authoritative 9-sat
  frag payout, pending winnings display and exactly-once disconnect/timeout life
  consumption. This fixture uses synthetic payments and never contacts a payout
  provider. The owner separately reports successful gameplay on a VPS.

No payment formula, payout destination, admission fee or database migration was
changed. The source review is bounded to this extension and its integration;
PostgreSQL, a full eight-player Internet load test and exhaustive upstream engine
vulnerability testing remain outside this pass. For any earlier interrupted run
already marked recovered by an older version with a journal over 256 events,
retain the journal and reconcile its recorded events before manually adjusting
balances; this patch does not rewrite historical financial records.

## Public lobby and creator fees — version 1.1.0

- Public creation is opt-in per owner. Anonymous callers cannot set wallet IDs,
  owner IDs or the admin fee. Creator percentages are strict integers and combined
  fees cannot exceed 100%. Addresses use the existing validated LNURL payout path.
- Arena creation locks the owner's settings row, snapshots its wallet and fees,
  bounds active/recent games and uses a unique owner/nonce constraint for retries.
  HTTP creation is rate-limited and browser cross-origin requests are rejected.
- Each authoritative kill records the life deduction, winner payout and creator
  payout in one transaction. Both payout types have unique victim constraints,
  leased processing, uncertain-payment reconciliation and a shared invoice-hash
  reservation table. Creator payment failures do not block funded respawns.
- Public responses select only public arena fields. Scoreboards deliberately
  disclose full winner addresses, as requested, and include only paid kill
  winnings. The UI states this before entry. Creator destinations, wallet IDs,
  entry tokens, invoice hashes and admin keys are excluded from lobby responses.
- Public text uses DOM textContent and template escaping. Only explicitly marked
  validation errors are returned to visitors; unexpected exceptions produce fixed
  generic errors. WebSockets enforce origin, connection/message limits, bounded
  page sizes, send/read timeouts and cleanup on disconnect or lobby disablement.
- Expiry uses the same arena lock as admission and settlement. It exempts admin
  games and preserves paid lives and live invoices. Financial ledgers are retained
  for outstanding payouts and delayed settlement can restore an expired arena.
- Migration 9 preserves existing payment states and reserves their invoice hashes;
  interrupted migration retries keep existing lobby URLs and records unchanged.

Verification includes SQLite concurrent creation/death events, fee arithmetic,
creator payout timeout reconciliation without resending, cross-outbox invoice
reuse rejection, owner isolation, expiry exemptions, delayed payment recovery,
interrupted migration recovery, error redaction and WebSocket lifecycle tests.
`make check` passes 92 Python tests and five asset-loader JavaScript tests;
`make smoke` starts all four maps. A local Chromium fixture verified the admin
toggle, tooltip and link, anonymous creation, live updates in another lobby tab,
literal rendering of an HTML-injection test title, mobile layout, and disablement.
The lobby downloaded no engine/game packs and stored no wallet keys.
Real creator Lightning transfers, PostgreSQL and Internet load testing remain
unverified; synthetic tests do not establish those deployment limits.

The updated public share metadata and six-map selection pass `make check`
(93 Python tests and five loader tests). `make smoke` starts Aggressor, OA DM7,
OA Minia, Czest1dm, OA Shine and Kaos 2. Chromium also verified OA DM7 entry,
the lobby card URL, six map options and the bottom credit at desktop/mobile sizes.

The wall-logo build adds exactly two instances of each supplied logo per map.
`make check` passes 96 Python tests and five loader tests, including decal counts,
lossless RGBA conversion, bounds validation and preservation of collision and
submodel data. All six branded maps pass `make smoke`. The original map entities,
collision lumps, lighting and visibility data were also compared directly against
the pinned release. Decals add no runtime permissions, endpoints or game rules.

## Public navigation, disclosures and error isolation follow-up

- Game dialogs now link to “Go to lobby/create new game”; invoice dialogs hide
  that link. The lobby exposes a link for every active listed arena, including
  full arenas, with live player counts. Both the listing and the Lightning-address
  entry dialog show admin, creator and combined percentages and net kill winnings.
- Player HTTP/admission paths previously echoed any `ValueError`, including an
  unexpected internal failure. Only explicitly designated fixed `PublicError`
  messages now cross that boundary. Unexpected public HTTP errors (including
  HTML/share rendering) produce a generic, non-cacheable 503 even with debug mode
  enabled. Unexpected admission errors do not echo credentials or provider data.
- Invoice rate limits now apply across arenas per client IP, preventing a visitor
  from bypassing the limit by changing arena IDs. Anonymous game-state reads are
  also limited. Existing socket limits, ownership checks, arena-scoped session
  hashes, atomic admission/death accounting and outbox identity checks remain.
- The lobby connects its WebSocket after an initial HTTP failure and disables
  creation while disconnected, then restores it from a fresh server snapshot.

`make check` passes 110 Python tests and five JavaScript asset tests, including
new debug-mode secret-redaction, cross-arena invoice-limit and in-flight ledger
cancellation regressions.
Chromium verified lobby recovery from an injected 503, literal rendering of an
HTML-injection title, fee disclosures, join/full-arena links, hidden invoice
navigation and a 390px layout. Eight isolated browser sessions joined Kaos 2
using fake invoices; a one-minute movement/fire sample retained all eight
connections with no browser errors. This was a local capacity sample, not an
Internet stress test or real Lightning settlement test. See `CAPACITY.md`.

Remaining boundaries still apply: unpaid invoice reservations can temporarily
occupy arena admission capacity, application limits cannot prevent distributed
floods, native engine compromise is a trust boundary, and modified clients can
use aim assistance or collude. LNURL network safety depends on the existing
LNbits helper and keeping private-IP LNURL requests disabled. No core changes or
new runtime dependencies were needed for this follow-up.

The first eight-browser simultaneous disconnect exposed a SQLite lock during
cleanup. A subsequent unpatched reproduction did not repeat it, so its exact
cause was not conclusively isolated. Disconnect handling now waits for in-flight
state/admission/lobby ledger operations to finish before detachment rather than
cancelling database work. Tests verify that all three paths finish before the
caller observes cancellation. Cleanup failures keep diagnostics generic and
leave durable journal recovery to the supervisor. This strengthens the observed
failure boundary; it is not proof that every possible SQLite contention source
has been eliminated.

The patched eight-browser disconnect run completed without a database error.
Its ledger recorded eight dead lives and 32 remaining lives: exactly one consumed
per player, with four retained each.

## Administrator-selected match cap

Server capacity is now saved in a singleton extension table (migration 10), with
a default of four and validated integer bounds of 1–32. Only authenticated
LNbits server-admin wallet owners may change it; ordinary arena owners and public
visitors cannot allocate a larger server-wide budget. Updates serialize with
engine creation, persist before becoming effective, and never stop existing
matches when a cap is lowered. Startup loads the saved cap. The previous
environment-only setting is replaced by the backend's Server capacity card.

The 110-test suite includes unauthorized updates, strict input validation,
persistence, repeated schema initialization and lowering below the live count.
The m.16 estimates distinguish its 1.5-core baseline, six-vCPU burst ceiling and
the possible single-worker supervisor bottleneck; no multi-match VPS capacity is
claimed as measured. An administrator choosing a higher cap accepts the resource
budget; the limit does not reserve CPU, bandwidth or provider burst credits.

Chromium verified the capacity form inside the normal LNbits admin shell,
changing four to twelve and retaining it after page reload. A fresh supervisor
process also loaded twelve from the isolated test database. Production settings
were not changed by these fixtures.

Files touched in this follow-up: `views.py`, `models.py`, `crud.py`, `payments.py`,
`server.py`, `migrations.py`; `static/admin.js`, `static/index.vue`,
`static/public.js`, `static/lobby.js`; `templates/quakejs/public.html` and
`templates/quakejs/lobby.html`; the existing corresponding-source bundle;
`tests/test_routes.py`, `tests/test_lobby.py`, `tests/test_ledger.py`,
`tests/smoke.py`; `README.md`, `CAPACITY.md` and this review. LNbits core is
unchanged. Browser fixtures and measurements live in the ignored `dev/` folder.

## Payout failure isolation follow-up

Failed payouts no longer add an error message to the public gameplay HUD. The
admin ledger retains the failure, and only confirmed payments count as winnings.
Invoice lookup retries remain bounded; uncertain sends still reconcile their
original hash without automatically sending again.

The worker now postpones overdue work after an unexpected exception or deadline
and prioritizes fresh payouts over due retries. Regression tests cover exhausted
address lookups without blocking funded respawns, redaction of provider secrets,
and an unexpected payout failure yielding to another recipient. These use fake
providers and isolated ledgers; no live Lightning transfers were made.

## Fee cap follow-up

New games now limit combined admin and creator percentages to 50% of each life's
value. Input models reject fees outside 0–50 and non-integer values; the public
creation transaction also checks the sum against the stored admin fee. Settings
writes and admin game creation enforce the admin limit. The forms mirror these
limits, but backend validation does not depend on them. Boundary tests cover
0+50, 5+45 and 50+0, rejecting larger totals without inserting a game. Existing
games and already-funded payment terms are not rewritten.

## Explicit owner closure

The authenticated arena owner can now close a game despite active players,
unused lives or outstanding invoices. Ownership checks are retained. Migration
11 adds an explicit admin-closure flag so late settlements cannot reverse the
decision; automatic expiry still allows late-payment restoration. Closure does
not delete financial records or cancel earned payouts. New admissions and
invoices are rejected, and the existing supervisor stops the engine when its
lease is next checked. The confirmation states that unused lives become
unplayable and there are no automatic refunds. Regression coverage includes
unauthenticated/other-owner rejection, repeated closure, late settlement,
retained entries and both payout outboxes, and admission/lease rejection.

## Security and payment review after admin closure

Reviewed authenticated ownership/capacity controls, anonymous lobby and entry
routes, session tokens, WebSocket origin/size/rate limits, native admission and
journal boundaries, invoice settlement, both payout outboxes, and the local
LNbits LNURL helper. No core files were changed.

Two payment-flow edge cases were patched:

- Invoice creation releases the arena lock while awaiting the wallet. If an
  admin closes the game during that wait, the response now withholds the invoice.
  Closed public state also withholds invoices and auto-admission hints. The
  browser ignores a delayed invoice response after receiving closure state.
  Existing invoice records remain available for settlement and owner review.
- A provider lookup that exceeded the worker's overall deadline previously
  bypassed the invoice retry counter. A separate 15-second lookup deadline now
  converts this into an ordinary bounded retry, ending after eight failures.
  This applies before sending money; uncertain sends retain exact-hash
  reconciliation and are never automatically resubmitted.

Regression cases exercise a suspended invoice provider while an owner closes
the arena, concurrent closure/settlement in both scheduling orders, and repeated
provider timeouts through retry exhaustion without blocking funded respawns.
Invoice and payout I/O use fakes; this review makes no live transfers.

Limits: this is a source review with local regression and confinement checks,
not proof that the native engine or client is exploit-free. Client aim/wall
cheats and collusion are not eliminated by authoritative payment accounting.
Already-issued invoices cannot be revoked by closing a game. Provider limits,
routing failures and uncertain sends can still require owner review; successful
payment delivery is not guaranteed. Existing public creation/socket limits
reduce abuse but do not constitute protection against a distributed flood.

Verification: `make check smoke sandbox-check` passed 130 Python tests, the
existing five asset tests, startup of all six configured maps, and the native
confinement probe. A subsequent combined JavaScript run passed all six tests,
including the new delayed-invoice UI regression now included in `make test`.
That UI regression executes the real handlers with a mocked DOM, not a full
browser or live Lightning provider. The existing three template API deprecation
warnings remain. Changed files for this pass: `payments.py`, `crud.py`,
`static/public.js`, its corresponding-source bundle, `tests/test_ledger.py`,
`tests/test_lobby.py`, `tests/test_public_ui.mjs`, `Makefile`, and this review.
