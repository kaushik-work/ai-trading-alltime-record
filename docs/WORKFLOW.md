# How this system works, and how to work on it

The architecture, the daily loop, the deployment, and — most importantly — the
rule that decides what is allowed to touch money.

> **The one rule.** Nothing gets weight without measurement on data it was not
> fitted to. Every mechanism in here either has a number behind it or is pinned
> at zero. That is not caution; it is the only thing separating this from the
> strategies that were deleted in `62f89c9` after each one measured profitable
> and traded negative.

---

## 1. The decision path

```
Angel SmartWebSocketV2  ─┐
option chain / VIX       ├──>  MarketSnapshot  ──> the ONE shared observation
Delta perp bars         ─┘         │
                                   │
   ROUND 0 ── each lens reads it ALONE ─────────────────┐
     greeks  volume_oi  vwap  ict_smc  smile            │  attribution
     momentum  liquidity  composite_profile             │  scores THIS
     gamma_exposure  candle_flow  vision                │  round only
                                   │                    │
   ROUND 1 ── now they hear each other ─────────────────┘
     hold · revise · defer
     (vwap defers to volume_oi: at −0.769 correlation it is
      an echo, not a second vote)
                                   │
   ROUND 2 ── the council resolves ONE call
     lead lens · objections · stand aside
     peers may OBJECT, never CONFIRM
                                   │
        journal EVERY decision, executed or declined
                                   │
                        sentinel_client.submit_intent()
                                   │  signed HMAC intent
                    ┌──────────────┴──────────────┐
                    │  SENTINEL (droplet, static  │  the ONLY holder of order
                    │  IP whitelisted with Angel) │  credentials
                    │  AngelBroker · dead-man's   │
                    │  switch · latch             │
                    └─────────────────────────────┘
```

### Why two rounds

A purely deliberative council cannot be measured. Once lens B has heard lens A,
B's opinion is partly A's, and "what is B worth?" stops having an answer — while
the brain's entire promote/suspend machinery runs on exactly that number.

Round 0 stays independent so attribution works. Round 1 is where they argue.

### Why deliberation is allowed to bind

It never cleared its statistical control (p=0.2125 against a random subset of
its own parent). It binds on an **invariant** instead: every lens's round-1 logic
is **monotone downward** — cut, defer, or hold; never raise, never flip. Verified
on 502 real lens-decisions (324 held, 115 cut, 63 deferred, 0 raised, 0 flipped)
and enforced by `assert_deliberation_monotone()`, which runs on the first live
snapshot of every session.

A mechanism that can only make the council trade **less** is safe to run
unproven: being wrong costs missed trades, not losses. The adaptive quorum is
equally unproven and stays in shadow precisely because it gates *both* ways.

---

## 2. How a lens earns weight

```
SHADOW ──> PROBATION ──> ACTIVE
             ^              │
             └── SUSPENDED <┘ ──> RETIRED
```

Scored on **expectancy contribution** — `E[pnl | voted FOR] − E[pnl | voted
AGAINST]` in rupees — never win rate. A 50%-WR lens with small wins loses to a
29%-WR one with big ones, so weighting on win rate actively selects the wrong
lens.

| guard | value | why |
|---|---|---|
| minimum closed trades | 30 | below this the weight cannot move off bootstrap |
| refit window | rolling trades, not sessions | daily refits on one session fit noise |
| refit timing | once daily, out of hours | a threshold that moves intraday makes the day's decisions incomparable |
| suspend | −100 contribution at 15 trades | benched faster than promoted |
| promote | +150 contribution at 30 trades | asymmetric on purpose |

**Cold start** uses `nse/lenses/bootstrap.py`, which records the measurement
that earned each weight. Change those numbers only from a run, never from an
expectation.

### The measurement protocol

390 NIFTY sessions — TRAIN 2021–23, VALIDATE 2024, **TEST 2025–26 unspent**.
30-minute decision grid, 60-minute forward horizon, identical `MarketSnapshot`
for every lens, edge measured against a **mix-matched baseline** (same
long/short mix, timing removed) so a lens cannot score by being directionally
lucky.

Crypto has its **own** split (TRAIN 2025-07→2026-03, VALIDATE →06, TEST sealed)
because a different dataset does not inherit NSE's dates. XAUTUSD has its own
again — it lists 2026-04-17 and has *zero* bars in the shared TRAIN window.

---

## 3. The daily loop

| time (IST) | what happens |
|---|---|
| **09:00** | council wakes: subscribes, builds bars, lenses read and journal |
| 09:00–09:30 | decisions journaled, **no trading** |
| **09:30** | trading opens |
| every 60s | snapshot → 3 rounds → journal → at most one intent |
| every 5s | heartbeat on its own thread |
| entry + 60m | position exits at the **measured horizon** |
| close − 10m | everything flattened |
| after close | journal written; council idles until tomorrow |

**Why 09:30 and not 09:15.** §3.10 measured the `open 09:15–09:30` bucket
separately *because it behaves differently*, and `volume_oi`'s 0.70% break-even
was computed against the calmer distribution. Trading the open is trading
outside the regime the edge was measured in.

**Why exit at 60 minutes.** That is the horizon the edge was measured at. Hold
longer and you are trading a strategy nobody measured — the number says nothing
about minute 61.

---

## 4. The guards, and the bug each one exists for

Every one of these was written after something broke. None is theoretical.

| guard | prevents |
|---|---|
| exit at the measured horizon | positions rode to expiry — there was **no exit path at all** |
| position reconciliation from the sentinel | in-memory book wiped on restart → **re-entered the same setup** |
| frozen-feed rejection | index stopped ticking at 15:30, NFO ran to 15:40 → **ten identical decisions** |
| premium floor | percentage spread explodes on cheap options (deep-OTM p90 2.13% vs 0.70% break-even) |
| 09:30 trade gate | opening spreads are outside the measured regime |
| `brain_guard.py` at container boot | on the droplet the broker's IP whitelist does **not** protect you |
| unreconciled → refuse entry | trading blind on an unknown position count doubles you up |
| indeterminate close keeps the position | forgetting a position you may hold makes a "flat" book short a leg |
| heartbeat on its own thread | 60s decision loop vs 30s dead-man = switch fired every cycle |
| journal `source` tag | a 2024 **backtest** was being read as "yesterday" by the live council |

### The tier split

The brain tier **cannot place an order**: it imports no broker code and its only
outbound trading path is `submit_intent`. `test_no_order_imports()` parses its
imports via AST and fails the build otherwise.

**On the droplet that guard is the only protection left.** Locally, Angel's IP
whitelist also stops a mistaken import — but the droplet's IP *is* whitelisted,
and the council legitimately needs `ANGEL_*` credentials for market data. So
`docker/brain_guard.py` refuses to boot the container if it can reach an order
API.

---

## 5. Deploying

```bash
docker compose --profile council up -d --build
docker compose --profile council run --rm council \
  python docker/preflight.py --expect-ip <your registered IP>
```

Preflight checks the **egress IP Angel actually sees** first, because a wrong
one fails as a rejected order at the moment a position needs to open — and reads
in the logs like a broker problem.

Three independent arming switches, none defaulted on:

```bash
SENTINEL_LIVE_ORDERS=1        # on the sentinel
ENABLE_OPTIONS_RUNNER=true    # on the council
COUNCIL_LIVE_FLAG=--live      # on the council
```

**Rehearse the dead-man's switch before real capital**, and after any sentinel
change. It is free with orders disarmed — the switch still arms, fires and
latches against paper positions:

```bash
docker compose stop council
docker compose logs -f sentinel   # expect: DEAD-MAN'S SWITCH ... Flattening
```

Two operational gotchas: `docker compose up --force-recreate` **reuses the
existing image**, so `build` first after any pull; and `pkill -f uvicorn` does
not reliably kill the sentinel on Windows, so a stale process on port 8090 makes
a new one fail to bind *silently*.

---

## 6. Watching it

- **Council health** (top of the NSE tab) — decision age rendered large, ticking
  on its own timer independent of the fetch. If the API dies the number keeps
  climbing and goes red; a monitor that fails silent reads as "fine".
- **Chart** — levels the lenses are reading (OI walls, POC/VAH/VAL, naked POCs,
  gamma flip), decision arrows **executed and declined**, and the measured
  expectation band.
- `GET /api/nse/health` · `/council` · `/lenses` · `/levels` · `/markers`

The declined decisions are the informative half. Currently **67 executed against
133 declined** — a fills-only view would hide two-thirds of the behaviour and
make the system look far more active, and far more right, than it is.

---

## 7. Adding a lens

1. Subclass `BaseLens`, implement `_evaluate(snapshot) -> LensVerdict`.
2. **Declare the direction convention in the docstring before measuring.** A
   negative result is then one bit of information, not a licence to flip the
   sign — that is how `train_signed` produced a fake winner.
3. **Measure the pivot, never assume zero.** Two lenses have been broken by
   centring a normalised quantity on 0.0 when the market sat elsewhere
   (`greeks`: `SKEW_NEUTRAL=-0.2098`; `smile`: butterfly positive 97.7% of the
   time, producing n=34 across three years).
4. No absolute price constants. ATR-relative or percentile-relative only.
5. Add to `ROSTER` — it joins at weight 0, which is why adding one is cheap.
6. Measure on TRAIN/VALIDATE, record in `bootstrap.py` with the run behind it.
7. **Check pairwise correlation before weighting.** `vwap` measured −0.769
   against `volume_oi` — one opinion wearing two hats.

Optionally implement `_deliberate(...)` for round 1. It **must** be monotone
downward or `assert_deliberation_monotone()` fails the session.

---

## 8. Where it actually stands

**11 lenses built. One with a measured edge.**

| lens | verdict |
|---|---|
| **`volume_oi`** | TRAIN +1.66 / VALIDATE +1.49 bps — **PROBATION 0.50** |
| ↳ `value_area_position` **alone** | **+1.66 / +2.01** — the volume profile is where the edge lives |
| ↳ `oi_build` alone | +1.40 / +1.17 |
| ↳ `wall_position` alone | +0.26 / **−0.06** — the OI walls contribute **nothing** |
| `vwap` | −2.31 / −1.06 — and a −0.769 echo of `volume_oi` |
| `ict_smc` | −1.41 / −0.15 |
| `greeks` | −0.54 / −1.08 |
| `smile` | +0.53 / +0.18 |
| `momentum` | −1.06 / −0.73 |
| `composite_profile` | −1.93 / −0.78 |
| `liquidity` | context lens; splits contradict |
| `gamma_exposure` | **not measurable** on ±10 strikes — degenerate both ways |
| `candle_flow` | built, unmeasured |
| `vision` | unmeasurable by replay; pinned at 0 |

Also measured and rejected: **combining them** (equal weighting scored +0.22 vs
+1.49 alone), **rejected lenses as filters** (`ict_smc` was the best gate on
TRAIN at bootstrap p=0.0010 and collapsed to p=0.75 on VALIDATE), and
**deliberation and the journal** as edge sources.

What survived: **trade only `volume_oi`'s top confidence tercile.** VALIDATE
+3.70 bps on n=503.

Adding a lens is meant to be cheap, and it is — ten of them have cost a journal
entry each. The architecture's real payoff has been making failures *cheap to
find and cheap to bench*, not producing edge.

---

## 9. Open, and honest about it

- **TEST is unspent** on both venues. One candidate, once, at the end.
- **`volume_oi` is marginal** — VALIDATE p=0.0527, and the edge moved 1.80→1.66
  bps on a change of bar construction alone. That is why it sits at PROBATION.
- **Live attribution is empty.** Brains are at `n_closed = 0`; nothing has been
  promoted or suspended by evidence yet.
- **NSE backtest and live see different volume.** `replay.py` sums option volume
  into index bars; the live index feed has none. Any volume-reading lens
  measured on NIFTY overstates what production runs. `has_volume` is journaled
  so the two can be separated.
- **Crypto has no weighted lens**, so the council correctly refuses every entry
  there — and now for a measured reason: fifteen lens-symbol measurements
  across ETHUSD, BTCUSD and XAUTUSD, none clearing (§3.19).
- **`volume_oi`'s OI-wall component is dead weight** and dilutes the two live
  components. Removing it is supported by a null on both splits, but it changes
  the only lens that trades, so it is a decision rather than a cleanup (§3.18).
- **`gamma_exposure` needs a wider chain**, not a third formula.
