# Match capacity: LunaNode m.16

Planning estimates, checked 2026-09-22. The cap is now a **server-admin setting**
(default four, selectable 1–32), independent of these estimates. It persists across
restarts. Raising it allows more engines; lowering it does not stop current games.

For this m.16, **eight full matches (64 players) is a reasonable higher cap to
trial**, provided CPU bursting is available. Four continuously full matches is
the estimate against the included sustained CPU baseline. The simple six-core
arithmetic reaches around 20 matches, but does not establish that the Python
supervisor or the VPS can sustain that number. These are planning estimates, not
measured multi-match VPS capacities.

## VPS allowance

LunaNode lists m.16 as 16 GB RAM, six virtual cores and 4,000 GB monthly transfer:
https://www.lunanode.com/pricing

The plan table specifies **1.5 CPU-points**:
https://dynamic.lunanode.com/plans

Each CPU-point supplies one core-equivalent of sustained CPU. The six vCPUs can
burst above that allowance, but sustained excess can exhaust credits and be
throttled unless paid bursting is enabled. Size continuously active matches
against the 1.5-core baseline, not six dedicated cores:
https://wiki.lunanode.com/index.php/Burstable_Resources

## Local measurement and calculation

Local host: Intel Core Ultra 7 255H. One Kaos 2 match, eight independent
Chromium clients, 30 server updates/second,
production engine settings, fake entry invoices and automated movement/fire:

| Measurement over 60.1 seconds | Result |
| --- | ---: |
| Native referee CPU | 14.8% of one local CPU core |
| Python fixture/WebSocket/ledger supervisor CPU | 10.2% of one local CPU core |
| Combined measured CPU | 0.25 local core-equivalents |
| Native referee resident memory | 22.2 MiB |
| Python fixture resident memory | 166.9 MiB |
| Connected players at each 15-second sample | 8 |

No lives were lost during this sample, so it does not measure a sustained kill/
payout workload. Browser rendering CPU is excluded because real players render
on their own devices. The Python fixture is smaller than a complete production
LNbits deployment and does not execute real outgoing Lightning requests.

Reserve 0.5 sustained cores for LNbits, the OS, payment activity and load spikes:

`floor((1.5 - 0.5) / 0.25) = 4 concurrent matches`

Using the same model with more CPU available:

| Full matches | Players | Estimated match CPU | CPU including 0.5-core reserve |
| --- | ---: | ---: | ---: |
| 4 | 32 | 1 core | 1.5 cores |
| 8 | 64 | 2 cores | 2.5 cores |
| 12 | 96 | 3 cores | 3.5 cores |
| 20 | 160 | 5 cores | 5.5 cores |

The aggregate ceiling is `floor((6 - 0.5) / 0.25) = 22`, rounded down to 20 for
planning. **CPU totals alone are insufficient:** LNbits runs one application
worker here. The measured Python share was about 0.10 cores per match; naive
linear scaling approaches one fully occupied Python core around ten matches.
Some time belongs to I/O threads and some overhead is shared, so only a concurrent
multi-match test can establish the actual supervisor limit. Do not treat 20 as
verified capacity just because total CPU and memory fit.

Try a cap of eight first, observe CPU burst points, process CPU, event-loop
responsiveness and player latency, then test twelve if the results allow it.
Partly filled games may allow more matches. Map combat patterns, VPS processors,
TLS, database work, public visitors and other services affect this estimate.
None of the multi-match rows above was benchmarked on the m.16.

Beyond 1.5 cores continuously, stored burst credits eventually run out. Under
LunaNode's published $0.0037 per five-minute CPU-point price, the eight-match
model's 2.5-core total would add about $31.97/month if sustained for 30 days with
paid bursting and no remaining credits. Twelve matches at 3.5 cores would add
about $63.94/month. Intermittent use can cost less; without paid bursting the
provider may throttle after credits run out. These figures exclude other bills
and depend on actual usage, not the configured cap.

RAM is unlikely to be the first limit. Each native process has a
512 MiB address-space ceiling; even that conservative allowance is about 2 GiB
for four engines or 10 GiB for twenty, in addition to LNbits, filesystem cache
and the OS. The native address-space ceiling is not a measured RSS forecast.

At the configured 25,000-byte/second per-player snapshot rate, eight players are
approximately 200 kB/s of gameplay payload per full arena. Four continuously full
arenas at that rate are roughly 2.07 TB over 30 days, before protocol overhead,
other traffic and asset downloads. The initial game pack is approximately
198 MB per uncached browser. Actual traffic should be measured at the VPS;
this arithmetic is a budget estimate rather than a hard bandwidth limit.

Stored lobby games do not each consume a running engine. Engines start for paid
admission and stop after five empty minutes. Games with unused lives can remain
listed without retaining an engine process after that timeout.
