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
