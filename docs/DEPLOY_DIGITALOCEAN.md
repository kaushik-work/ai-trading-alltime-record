# Deploying the council on DigitalOcean

## The thing that changes on a droplet

Locally the tier split has two independent layers:

1. the brain tier imports no order-placing code
2. **Angel rejects order placement from a non-whitelisted IP**

Layer 2 does real work on a laptop. Even a mistaken import cannot place an
order from a home connection — the broker refuses the packet.

**On the droplet, layer 2 is gone.** The sentinel and the council share one
host, and that host's IP is the whitelisted one. Angel will accept an order
from *any* process on that box. The council also legitimately needs `ANGEL_*`
credentials, because market data uses the same keys as order placement — so
credential separation is not available either.

That leaves the import boundary as the only protection, in the environment
where a mistake is most expensive. `docker/brain_guard.py` therefore runs as
the council container's entrypoint and **refuses to boot** if:

- `options_runner` imports order-placing code (AST check)
- importing the brain tier pulls a broker module into `sys.modules`
  (catches transitive imports the per-file check cannot see)
- `SENTINEL_URL` is unset, or is loopback — inside compose it must be the
  service name, and loopback usually means the two tiers were collapsed
- `SENTINEL_LIVE_ORDERS=1` leaked into the council's environment, which means
  the two services share an env file and the split is being kept by luck

Verified exit codes: good config `0`, loopback `1`, unset `1`, flag leak `1`.

## One-time, before anything trades

1. Create the droplet and note its **static IP** (reserved IP, not ephemeral).
2. Register that IP with Angel One for order placement, and obtain the
   **SEBI Algo-ID**. This has lead time and blocks live trading regardless of
   how the code is configured.
3. Generate a sentinel secret — **not** an Angel key:
   ```bash
   openssl rand -hex 32
   ```

## `.env` additions

```bash
SENTINEL_SECRET=<the 32-byte hex from above>
SENTINEL_DEADMAN_SEC=30
SENTINEL_LIVE_ORDERS=0        # flip to 1 only after the IP is registered
ENABLE_OPTIONS_RUNNER=false   # flip to true only when you mean it
COUNCIL_SYMBOL=NIFTY
COUNCIL_EVERY=60
COUNCIL_LIVE_FLAG=            # set to --live for real intents
```

## Bring it up

```bash
docker compose --profile council up -d --build
docker compose logs -f council
```

The council waits for the sentinel's healthcheck before starting, so a
sentinel that cannot boot stops the council rather than letting it run
blind into a dead endpoint.

**The sentinel is never published to the host.** It uses `expose`, not
`ports`, so it is reachable at `http://sentinel:8090` on the compose network
and nowhere else. Publishing it would put an order API on the public internet
behind one shared secret, on a host the broker trusts.

## Going live, in order

```bash
# 1. paper first — no orders, full journal, real attribution
docker compose --profile council up -d

# 2. arm the sentinel only after the IP is whitelisted
SENTINEL_LIVE_ORDERS=1 docker compose --profile council up -d sentinel

# 3. arm the council last
ENABLE_OPTIONS_RUNNER=true COUNCIL_LIVE_FLAG=--live \
  docker compose --profile council up -d council
```

Three separate switches on purpose. Any one of them left unset means no order
reaches the exchange.

## Rehearse the dead-man's switch on the droplet

Do this before real capital, and after any change to the sentinel:

```bash
docker compose exec sentinel python - <<'PY'
import requests
print(requests.get("http://127.0.0.1:8090/status", timeout=5).json())
PY

# stop the council and watch the sentinel flatten
docker compose stop council
docker compose logs -f sentinel     # expect: DEAD-MAN'S SWITCH ... Flattening
```

With `SENTINEL_LIVE_ORDERS=0` the switch still arms, fires and latches against
paper positions, so the drill is safe and costs nothing. Verified locally:
fired at t+8s with an 8s timeout, flattened, then refused new intents *and did
not un-latch when the heartbeat resumed*.

Clear the latch deliberately, only after finding out why the brain went dark:

```bash
docker compose exec sentinel curl -s -XPOST http://127.0.0.1:8090/deadman/clear
```

## Watching it

- council transcript: `docker compose logs -f council`
- decisions, executed and declined: `GET /api/nse/council`
- every lens, its weight, health, and whether it is dying: `GET /api/nse/lenses`
- chart + transcript side by side: the NSE tab of the dashboard

## Gotcha worth writing down

`pkill -f uvicorn` does not reliably kill the sentinel on Windows during local
testing, and a stale process holding port 8090 makes a new one fail to bind
**silently** — the drill then tests the old code and can report the dead-man's
switch as broken when it is not. Free the port explicitly:

```powershell
Get-NetTCPConnection -LocalPort 8090 -State Listen | Stop-Process -Id {$_.OwningProcess} -Force
```
