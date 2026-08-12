"""REST routes for NSE synthetic-forward runner."""

from __future__ import annotations

import logging
from typing import Optional

from fastapi import APIRouter, Depends, HTTPException, Query

from api.auth import decode_token, oauth2_scheme
from nse.execution.nse_runner import get_nse_runner_state
from nse.risk import is_killed, set_killed
from nse.broker.angel_broker import AngelBroker
from nse.data.option_chain import OptionChainCache
from nse.snapshot import _cached_bars, _normalise_bars
from nse.config import STEP_SIZES, LOT_SIZES, EXCHANGE

logger = logging.getLogger(__name__)
from datetime import datetime as _dt
from datetime import timedelta as _td, timezone as _tz
IST_TZ = _tz(_td(hours=5, minutes=30))

router = APIRouter(prefix="/api/nse")


def _get_current_user(token: str = Depends(oauth2_scheme)):
    try:
        return decode_token(token)
    except Exception as exc:
        raise HTTPException(status_code=401, detail="Invalid token") from exc


@router.get("/status")
def nse_status(user: dict = Depends(_get_current_user)):
    return get_nse_runner_state()


@router.post("/kill")
def nse_kill(user: dict = Depends(_get_current_user)):
    if is_killed():
        return {"killed": True, "message": "already killed"}
    set_killed(True)
    logger.warning("NSE kill switch activated via API by %s", user)
    return {"killed": True, "message": "NSE entries halted"}


@router.post("/unkill")
def nse_unkill(user: dict = Depends(_get_current_user)):
    set_killed(False)
    logger.warning("NSE kill switch cleared via API by %s", user)
    return {"killed": False, "message": "NSE entries resumed"}


@router.get("/chain")
def nse_chain(symbol: str = Query("NIFTY"),
              strikes: int = Query(10, ge=1, le=25),
              user: dict = Depends(_get_current_user)):
    """REST view of the chain.

    Shares one builder with the WebSocket push at /ws/nse/chain so the two can
    never drift into showing different numbers for the same instant.
    """
    try:
        return build_chain_payload(symbol, strikes)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except RuntimeError as e:
        raise HTTPException(status_code=503, detail=str(e))


def build_chain_payload(symbol: str = "NIFTY", strikes: int = 10) -> dict:
    """Live option chain, shaped for the classic calls-strike-puts table.

    Prefers the WebSocket cache — that is the whole point of the socket — and
    falls back to a REST snapshot when the stream has no fresh coverage, which
    is the normal state outside market hours.

    Greeks are COMPUTED here from the current mark, never read from storage.
    Stored Greek vectors are up to 100% wrong inside 2 DTE, so the response
    also carries `greeks_trustworthy` and the UI greys them out when false.
    See docs/OPTIONS_GREEKS_LEARNINGS.md section 3.

    Raises ValueError for a bad request and RuntimeError when the market is
    unreadable, so the REST route can map them to status codes and the socket
    can report them inline without a FastAPI dependency.
    """
    import pandas as pd
    from data.angel_fetcher import AngelFetcher
    from nse.snapshot import MIN_TRUSTWORTHY_DTE, expiry_at_close

    if symbol not in STEP_SIZES:
        raise ValueError(f"Unsupported symbol {symbol}")

    fetcher = AngelFetcher.get()
    cache = OptionChainCache(symbol, fetcher)

    # Hot path. At a 10Hz push cadence there is no room for a REST round-trip:
    # one ltpData call costs ~500ms, so spot + expiry + VIX by REST capped the
    # socket at 0.29 frames/sec — slower than the polling it replaced. Spot now
    # comes off the socket, expiry is cached for the day and VIX for a few
    # seconds. All three fall back to REST when the stream is cold.
    spot = _stream_spot(symbol)
    if spot is None:
        spot = cache.get_underlying_ltp()
    if spot is None or spot <= 0:
        raise RuntimeError(f"{symbol} spot unavailable")

    expiry_d = _cached_expiry(symbol, cache)
    if expiry_d is None:
        raise RuntimeError(f"{symbol} expiry unavailable")

    step = STEP_SIZES[symbol]
    atm = int(round(spot / step)) * step
    expiry_dt = expiry_at_close(expiry_d, symbol)

    df, source = pd.DataFrame(), "rest"
    try:
        from nse.ws.angel_stream import get_stream
        df = get_stream().chain_frame(symbol)
        if not df.empty:
            source = "ws"
    except Exception as e:
        logger.debug("nse_chain: stream unavailable (%s), using REST", e)
    if df.empty:
        df = cache.get_snapshot(expiry_d, atm, strikes_around=strikes, full=True)
    if df is None or df.empty:
        raise RuntimeError(f"{symbol} chain unavailable")

    df = df.copy()
    if "option_type" not in df.columns and "side" in df.columns:
        df["option_type"] = df["side"]
    df["spot"] = spot
    df["expiry"] = pd.to_datetime(expiry_dt)
    df["side"] = df["option_type"].astype(str).str.upper()
    if "mark" not in df.columns:
        df["mark"] = df.get("mid", df.get("ltp", 0))
        df.loc[df["mark"] <= 0, "mark"] = df.get("ltp", 0)

    dte = max(0.0, (expiry_dt - pd.Timestamp.now(tz="UTC").to_pydatetime()).total_seconds() / 86400.0)

    # Value the Greeks against a clock quantised to the MINUTE, not to `now`.
    # add_greeks_to_dataframe derives T from this column, so an unquantised
    # timestamp makes T shrink between every push and every Greek drift in its
    # low decimals — measured: theta/delta/vega/iv/gamma accounted for ~5,000
    # field changes per 15s against ~350 for ltp, so the diff saw all 13 strikes
    # change on every frame purely from clock drift.
    #
    # Quantising is physically honest rather than a fudge: theta at 1 DTE is
    # about -32/day, i.e. -0.00007 over 200ms. Minute resolution is far finer
    # than the number is meaningful to.
    df["timestamp"] = pd.Timestamp.now(tz="Asia/Kolkata").floor("min")
    try:
        from nse.data.greeks_vectorized import add_greeks_to_dataframe
        add_greeks_to_dataframe(df)
    except Exception as e:
        logger.warning("nse_chain: greeks failed for %s: %s", symbol, e)

    def leg(row) -> dict:
        g = lambda k, d=0.0: (float(row[k]) if k in row and pd.notna(row[k]) else d)
        # Rounded deliberately, and not only for readability. Greeks are solved
        # against a time-to-expiry computed from NOW, so T shrinks on every
        # frame and every Greek changes in its tenth decimal — which made the
        # WebSocket diff classify all 13 strikes as "changed" on every push and
        # sent a full 12KB ladder at 5Hz. Rounding to more precision than anyone
        # can read restores a meaningful diff.
        return {
            "ltp": round(g("ltp"), 2), "bid": round(g("bid"), 2),
            "ask": round(g("ask"), 2), "mid": round(g("mid"), 2),
            "spread": round(g("spread"), 2),
            "spread_pct": round(g("spread_pct"), 3),
            "volume": int(g("volume")), "oi": int(g("oi")),
            "oi_change_pct": round(g("oi_change_pct"), 2),
            # The solver returns IV in DECIMAL (0.1209) while Angel reports VIX
            # in PERCENT (12.39). Both render in the same table, so IV is
            # converted here — at the presentation boundary — and every number
            # the UI receives is in percent. The lenses call
            # add_greeks_to_dataframe directly and keep the decimal form.
            "iv": round(g("iv") * 100.0, 2),
            "delta": round(g("delta"), 4), "gamma": round(g("gamma"), 6),
            "theta": round(g("theta"), 3), "vega": round(g("vega"), 3),
            "book_imbalance": round(g("book_imbalance"), 3),
            "tradingsymbol": row.get("tradingsymbol"),
        }

    wanted = {atm + k * step for k in range(-strikes, strikes + 1)}
    rows, ce_oi_tot, pe_oi_tot = [], 0, 0
    for strike in sorted(s for s in df["strike"].dropna().unique() if int(s) in wanted):
        strike = int(strike)
        sel = df[df["strike"] == strike]
        ce = sel[sel["side"] == "CE"]
        pe = sel[sel["side"] == "PE"]
        ce_leg = leg(ce.iloc[0]) if not ce.empty else None
        pe_leg = leg(pe.iloc[0]) if not pe.empty else None
        ce_oi_tot += ce_leg["oi"] if ce_leg else 0
        pe_oi_tot += pe_leg["oi"] if pe_leg else 0
        rows.append({
            "strike": strike,
            "is_atm": strike == atm,
            # Moneyness drives the ITM shading: a call is ITM below spot, a put
            # above it, so the two sides shade in opposite directions.
            "ce_itm": strike < atm,
            "pe_itm": strike > atm,
            "ce": ce_leg,
            "pe": pe_leg,
        })

    return {
        "symbol": symbol,
        "spot": round(float(spot), 2),
        "atm": atm,
        "step": step,
        "lot_size": LOT_SIZES.get(symbol),
        "expiry": expiry_d.isoformat(),
        "dte": round(dte, 3),
        "greeks_trustworthy": dte >= MIN_TRUSTWORTHY_DTE,
        "vix": _safe_vix(fetcher),
        "source": source,
        "rows": rows,
        "totals": {
            "ce_oi": ce_oi_tot,
            "pe_oi": pe_oi_tot,
            "pcr": round(pe_oi_tot / ce_oi_tot, 3) if ce_oi_tot else None,
            "max_pain": _max_pain(rows),
        },
    }


def _stream_spot(symbol: str) -> Optional[float]:
    try:
        from nse.ws.angel_stream import get_stream
        return get_stream().get_spot(symbol)
    except Exception:
        return None


_expiry_cache: dict[tuple, object] = {}


def _cached_expiry(symbol: str, cache):
    """Nearest expiry, resolved once per symbol per day.

    Keyed by date so it re-resolves after a rollover rather than serving an
    expired contract into the next session.
    """
    import datetime as _dt
    key = (symbol, _dt.date.today())
    if key not in _expiry_cache:
        _expiry_cache.clear()
        _expiry_cache[key] = cache.nearest_expiry(min_days=0)
    return _expiry_cache[key]


_vix_cache: dict = {"value": None, "at": 0.0}
_VIX_TTL_SEC = 5.0


def _safe_vix(fetcher) -> Optional[float]:
    """VIX with a short TTL. It is a 30-day index — it does not move in 100ms,
    and re-fetching it per frame was the single biggest cost in the push loop."""
    import time as _t
    if _t.time() - _vix_cache["at"] < _VIX_TTL_SEC:
        return _vix_cache["value"]
    try:
        v = fetcher.fetch_vix()
    except Exception:
        v = None
    if v is not None:
        _vix_cache["value"] = v
    _vix_cache["at"] = _t.time()
    return _vix_cache["value"]


def _max_pain(rows: list[dict]) -> Optional[int]:
    """Strike where option writers lose least — standard on Indian chains.

    For each candidate expiry level, sum what writers pay out: calls below it
    and puts above it, each weighted by open interest.
    """
    strikes = [r["strike"] for r in rows if r["ce"] or r["pe"]]
    if len(strikes) < 3:
        return None
    best, best_loss = None, None
    for at in strikes:
        loss = 0.0
        for r in rows:
            k = r["strike"]
            if r["ce"] and k < at:
                loss += (at - k) * r["ce"]["oi"]
            if r["pe"] and k > at:
                loss += (k - at) * r["pe"]["oi"]
        if best_loss is None or loss < best_loss:
            best, best_loss = at, loss
    return best


@router.get("/stream_health")
def nse_stream_health(user: dict = Depends(_get_current_user)):
    """WebSocket feed diagnostics — is 'live' actually live right now?"""
    try:
        from nse.ws.angel_stream import get_stream
        return get_stream().diagnostics()
    except Exception as e:
        return {"connected": False, "error": str(e)}


@router.post("/test_buy_ce")
def nse_test_buy_ce(user: dict = Depends(_get_current_user)):
    """Place a test LIMIT buy CE order at current NIFTY ATM strike with GTT SL/Target.

    Entry is placed at the current ask price (no entry slippage).  After fill,
    two GTT rules are created:
      - SL   : sell if premium drops 10 points from fill price
      - Target: sell if premium rises 50 points from fill price

    WARNING: This places a real order in live mode. Use only for API testing.
    The order is placed regardless of available funds; Angel One will reject if
    the account is not funded, and the rejection reason is returned.
    """
    from data.angel_fetcher import AngelFetcher
    fetcher = AngelFetcher.get()
    broker = AngelBroker(fetcher)
    cache = OptionChainCache("NIFTY", fetcher)

    # Force a fresh login attempt (bypass scheduler cooldown) for user-initiated tests.
    if not fetcher._ensure_logged_in(force=True):
        logger.error("NSE test buy CE by %s: Angel One login failed", user)
        raise HTTPException(status_code=503, detail="Angel One not logged in")

    rms = fetcher.get_rms() or {}

    spot = cache.get_underlying_ltp()
    if spot is None:
        logger.error("NSE test buy CE by %s: NIFTY spot unavailable", user)
        raise HTTPException(status_code=503, detail="NIFTY spot not available")

    expiry = cache.nearest_expiry(min_days=0)
    if expiry is None:
        logger.error("NSE test buy CE by %s: NIFTY expiry unavailable", user)
        raise HTTPException(status_code=503, detail="NIFTY expiry not available")

    step = STEP_SIZES["NIFTY"]
    atm = int(round(spot / step)) * step
    ts, token = cache.resolve_leg(atm, "CE", expiry)
    if not ts or not token:
        logger.error("NSE test buy CE by %s: could not resolve NIFTY %s CE", user, atm)
        raise HTTPException(status_code=503, detail=f"Could not resolve NIFTY {atm} CE")

    quantity = LOT_SIZES["NIFTY"]

    # Fetch ask price so we can enter via LIMIT and avoid slippage.
    # Fall back to LTP when ask is zero (after-hours / illiquid quote).
    quote = fetcher.get_option_quote(ts, token, EXCHANGE.get("NIFTY", "NFO"))
    if not quote:
        logger.error(
            "NSE test buy CE by %s: option quote unavailable for %s",
            user, ts,
        )
        raise HTTPException(status_code=503, detail="Could not fetch NIFTY option quote")
    limit_price = quote.get("ask") or quote.get("ltp") or 0
    if limit_price <= 0:
        logger.error(
            "NSE test buy CE by %s: option quote has no usable price for %s (quote=%s)",
            user, ts, quote,
        )
        raise HTTPException(status_code=503, detail="NIFTY option quote has no usable price")
    limit_price = round(float(limit_price), 2)

    # Connectivity check only — proves the Angel session, instrument lookup,
    # order permissions and GTT OCO attachment all work. The bracket values
    # are arbitrary and intentionally left alone; this is not a trade.
    result = broker.place_single_order(
        "NIFTY", ts, token, "CE", "BUY", 1,
        limit_price=limit_price,
        sl_points=10.0,
        target_points=50.0,
    )
    logger.warning("NSE test buy CE by %s | spot=%s strike=%s qty=%s limit=%s | rms=%s | result=%s",
                   user, spot, atm, quantity, limit_price, rms, result)
    return {
        "spot": spot,
        "strike": atm,
        "expiry": expiry.isoformat(),
        "tradingsymbol": ts,
        "token": token,
        "lots": 1,
        "quantity": quantity,
        "limit_price": limit_price,
        "available_cash": rms.get("availablecash"),
        "available_limit": rms.get("availablelimitmargin"),
        "net": rms.get("net"),
        "order_response": result,
    }


# ── chart + council transcript ───────────────────────────────────────────────
# The bot does NOT read these endpoints. The vision lens renders its own chart
# server-side (core/chart/render.py) into a PNG, which is what makes it
# deterministic and replayable — a screen-scraped chart could never be
# backtested. These routes exist for the human watching the session.

@router.get("/candles")
def nse_candles(symbol: str = Query("NIFTY"),
                interval: str = Query("5m"),
                user: dict = Depends(_get_current_user)):
    """Index candles for the chart, newest last.

    Live spot is overlaid separately by the client from /status, so a candle
    that has not closed yet is never redrawn as if it had. That distinction is
    the frontend mirror of the fill-at-close rule (RESEARCH_LEARNINGS 1.2):
    the last bar on screen is still forming and must not be read as settled.
    """
    try:
        # OptionChainCache is a plain constructor, not a singleton — there is no
        # .get(). The chart rendered black with
        # "type object 'OptionChainCache' has no attribute 'get'" because this
        # invented an accessor that does not exist.
        cache = OptionChainCache(symbol)
        # Same cached path the snapshot uses: 60s TTL, serves the last good
        # frame on failure. Angel rate-limits the historical endpoint hard and
        # five containers already share the quota, so a chart refreshing every
        # 5s must not add a REST call per poll.
        df = _cached_bars(cache.fetcher, symbol, interval)
    except Exception as exc:
        logger.warning("candles: fetch failed for %s %s: %s", symbol, interval, exc)
        return {"symbol": symbol, "interval": interval, "candles": [],
                "error": str(exc)}

    if df is None or getattr(df, "empty", True):
        return {"symbol": symbol, "interval": interval, "candles": [],
                "error": "no intraday bars available"}

    out = []
    for _, r in df.iterrows():
        ts = r.get("datetime")
        if ts is None:
            continue
        try:
            # Angel bars are IST but tz-naive. lightweight-charts wants epoch
            # seconds; localising first stops the series landing 5h30m adrift.
            epoch = int((ts.tz_localize(IST_TZ) if ts.tzinfo is None
                         else ts).timestamp())
        except Exception:
            continue
        out.append({"time": epoch,
                    "open": float(r["open"]), "high": float(r["high"]),
                    "low": float(r["low"]), "close": float(r["close"]),
                    "volume": float(r.get("volume") or 0)})
    return {"symbol": symbol, "interval": interval, "candles": out,
            "count": len(out)}


@router.get("/council")
def nse_council(limit: int = Query(40, ge=1, le=200),
                user: dict = Depends(_get_current_user)):
    """The council's recent decisions, executed and rejected alike.

    Rejected decisions are returned too, and that is the point: they are the
    control group attribution needs, and on screen they are the more
    informative half — a session where the council stood aside 12 times out of
    13 should LOOK like that, not like an idle system.
    """
    try:
        from core.mongo import get_db
        from nse.council import COUNCIL_COLLECTION
        db = get_db()
        if db is None:
            return {"decisions": [], "error": "mongo unavailable"}
        rows = list(db[COUNCIL_COLLECTION].find({}, {"_id": 0})
                    .sort("ts", -1).limit(limit))
        return {"decisions": rows, "count": len(rows)}
    except Exception as exc:
        logger.warning("council feed failed: %s", exc)
        return {"decisions": [], "error": str(exc)}


@router.get("/lenses")
def nse_lenses(user: dict = Depends(_get_current_user)):
    """Every lens, its brain state, and how close it is to dying.

    Mortality is a first-class on-screen fact: a lens that is one bad review
    from suspension should say so before it happens, not after.
    """
    from nse.brain import load as load_brain
    from nse.lenses import ROSTER
    from nse.lenses.bootstrap import MEASURED

    out = []
    for cls in ROSTER:
        m = MEASURED.get(cls.name)
        b = load_brain(cls.name,
                       backtestable=(m.train_bps is not None) if m else True,
                       bootstrap_weight=m.bootstrap_weight if m else 0.0)
        out.append({
            "lens": cls.name,
            "lifecycle": b.lifecycle.value,
            "weight": round(b.effective_weight(), 4),
            "health": b.health,
            "is_dying": b.is_dying,
            "n_closed": b.n_closed,
            "trades_until_review": b.trades_until_review,
            "abstain_rate": round(b.abstain_rate, 4),
            "train_bps": m.train_bps if m else None,
            "validate_bps": m.validate_bps if m else None,
            "note": (m.note if m else ""),
        })
    return {"lenses": out, "count": len(out)}


# ── one-glance health ────────────────────────────────────────────────────────
@router.get("/health")
def nse_health(user: dict = Depends(_get_current_user)):
    """Everything that can be wrong with the council, in one payload.

    Built for the question "is it working right now?", which until this existed
    could only be answered by SSHing to the droplet and reading logs. Logs tell
    you what happened; they are a poor way to notice that nothing is happening.

    THE CHECK THAT MATTERS MOST IS DECISION AGE. A council that has stopped
    deciding looks identical, from every other angle, to a council that is
    deciding to stand aside — same green containers, same clear sentinel, same
    everything. The only difference is a timestamp getting older. So that is
    surfaced first and is the one thing that goes red on its own.

    Each check returns ok | warn | fail plus the observed value, so the UI never
    has to interpret a raw number to decide what colour to paint.
    """
    import os
    from datetime import datetime, timedelta, timezone

    checks: list[dict] = []

    def add(name, state, detail, value=None):
        checks.append({"name": name, "state": state, "detail": detail,
                       "value": value})

    now = datetime.now(timezone.utc)

    # 1. Is the council still deciding?
    last_ts = None
    try:
        from core.mongo import get_db
        from nse.council import COUNCIL_COLLECTION
        db = get_db()
        if db is None:
            add("database", "fail", "Mongo unreachable — nothing is persisting")
        else:
            add("database", "ok", db.name)
            doc = db[COUNCIL_COLLECTION].find_one({}, {"_id": 0, "ts": 1},
                                                  sort=[("ts", -1)])
            if doc and doc.get("ts"):
                last_ts = datetime.fromisoformat(doc["ts"])
                if last_ts.tzinfo is None:
                    last_ts = last_ts.replace(tzinfo=timezone.utc)
                age = (now - last_ts).total_seconds()
                # Two minutes is two missed 60s cycles: one is a slow snapshot,
                # two is a stopped council.
                #
                # OUTSIDE MARKET HOURS, NOT DECIDING IS CORRECT. The council
                # idles overnight by design, so a decision age of hours is
                # expected and flagging it red trains you to ignore a red panel
                # — the panel then fails to mean anything on the morning it
                # matters.
                try:
                    from nse.execution.live_session import market_open
                    trading = market_open()
                except Exception:
                    trading = True
                if not trading:
                    add("council deciding", "ok",
                        f"idle — market closed (last decision {int(age // 60)}m ago)",
                        round(age, 1))
                else:
                    state = "ok" if age < 120 else "warn" if age < 600 else "fail"
                    add("council deciding", state,
                        f"last decision {int(age)}s ago", round(age, 1))
            else:
                add("council deciding", "fail", "no decisions ever journaled")
    except Exception as e:
        add("database", "fail", str(e)[:120])

    # 2. The sentinel — the only thing that can place an order.
    try:
        import requests
        url = os.environ.get("SENTINEL_URL", "http://sentinel:8090")
        st = requests.get(f"{url.rstrip('/')}/status", timeout=5).json()
        add("sentinel", "ok", f"up {int(st.get('uptime_sec', 0))}s")
        dm = st.get("deadman", {})
        add("dead-man's switch",
            "fail" if dm.get("fired") else "ok",
            "LATCHED — clear it and find out why the brain went dark"
            if dm.get("fired") else f"clear, {dm.get('timeout_sec')}s timeout")
        add("orders armed", "ok" if st.get("live_orders") else "warn",
            "LIVE — real orders" if st.get("live_orders")
            else "disarmed (paper)", bool(st.get("live_orders")))
        add("open positions", "ok", str(st.get("positions", 0)),
            st.get("positions", 0))
        if st.get("intents_rejected"):
            add("intents rejected", "warn",
                f"{st['intents_rejected']} — last: {st.get('last_reject_reason','')[:60]}",
                st["intents_rejected"])
    except Exception as e:
        add("sentinel", "fail", f"unreachable — {str(e)[:80]}")

    # 3. Is the feed actually live?
    #
    # This CANNOT be answered by calling get_stream().diagnostics() here. That
    # is a per-process singleton and the socket belongs to the COUNCIL process,
    # not to whichever worker serves this request. Asking locally returns
    # "socket down" on a perfectly healthy system, permanently — the exact
    # cry-wolf failure that makes people stop reading a status page.
    #
    # So it is inferred from shared state instead: the council cannot build a
    # snapshot at all without a live chain, so a RECENT decision carrying a
    # plausible spot is positive evidence the feed is up, and it is evidence
    # that crosses process boundaries.
    try:
        from core.mongo import get_db
        from nse.council import COUNCIL_COLLECTION
        db = get_db()
        doc = (db[COUNCIL_COLLECTION].find_one({}, {"_id": 0, "ts": 1, "spot": 1},
                                               sort=[("ts", -1)])
               if db is not None else None)
        if not doc or not doc.get("spot"):
            add("market feed", "warn",
                "no decision carrying a spot yet — cannot infer feed state")
        else:
            fts = datetime.fromisoformat(doc["ts"])
            if fts.tzinfo is None:
                fts = fts.replace(tzinfo=timezone.utc)
            fage = (now - fts).total_seconds()
            try:
                from nse.execution.live_session import market_open
                trading = market_open()
            except Exception:
                trading = True
            add("market feed", "ok" if (fage < 300 or not trading) else "warn",
                f"spot {doc['spot']:.1f} seen "
                + (f"{int(fage)}s ago" if fage < 300
                   else f"{int(fage // 60)}m ago (market closed)"),
                round(doc["spot"], 2))
    except Exception as e:
        add("market feed", "warn", str(e)[:80])

    # In-process socket diagnostics, when this worker happens to own one.
    # Reported separately and never as a failure, precisely because its absence
    # means "not observable from here", not "broken".
    try:
        from nse.ws.angel_stream import get_stream
        d = get_stream().diagnostics()
        # Only report a COUNT we actually have. The previous version printed
        # "? fresh contracts" whenever the diagnostics dict lacked the key,
        # putting a literal question mark on the dashboard -- a missing value
        # rendered as though it were a value.
        fresh = d.get("fresh_contracts")
        if d.get("connected") and isinstance(fresh, int):
            add("socket (this process)", "ok", f"{fresh} contracts streaming")
    except Exception:
        pass

    # 4. Who can actually move capital, and is anything dying?
    try:
        from nse.brain import load as load_brain
        from nse.lenses import ROSTER
        from nse.lenses.bootstrap import MEASURED
        weighted, dying = [], []
        for cls in ROSTER:
            m = MEASURED.get(cls.name)
            b = load_brain(cls.name,
                           backtestable=bool(m and m.train_bps is not None),
                           bootstrap_weight=m.bootstrap_weight if m else 0.0)
            if b.effective_weight() > 0:
                weighted.append(f"{cls.name}={b.effective_weight():g}")
            if b.is_dying:
                dying.append(cls.name)
        add("lenses with weight", "ok" if weighted else "fail",
            ", ".join(weighted) or "NONE — the council cannot trade")
        if dying:
            add("lenses dying", "warn", ", ".join(dying))
    except Exception as e:
        add("lenses", "warn", str(e)[:80])

    worst = ("fail" if any(c["state"] == "fail" for c in checks)
             else "warn" if any(c["state"] == "warn" for c in checks) else "ok")
    try:
        from nse.execution.live_session import market_open as _mo
        trading_now = _mo()
    except Exception:
        trading_now = True

    return {
        "overall": worst,
        "checked_at": now.isoformat(),
        "last_decision_at": last_ts.isoformat() if last_ts else None,
        # The UI cannot decide on its own whether a rising decision age is bad;
        # that depends on whether the market is open. Sending the answer stops
        # the panel's headline contradicting the row directly beneath it.
        "market_open": trading_now,
        "checks": checks,
    }


@router.get("/levels")
def nse_levels(symbol: str = Query("NIFTY"),
               user: dict = Depends(_get_current_user)):
    """The structural levels the lenses are actually reading, for the chart.

    Every number here is already computed on every decision and journaled in
    the lens `features` — order blocks and fair-value gaps from ict_smc,
    composite POC/VAH/VAL and naked POCs from composite_profile, the gamma flip
    from gamma_exposure. Until now none of it reached the screen, so the chart
    showed price while the council reasoned about levels the operator could not
    see.

    Read from the LATEST DECISION rather than recomputed here. Recomputing
    would produce numbers that drift from the ones the council actually used —
    the chart would show a level the lens never saw, which is worse than
    showing nothing because it looks authoritative.
    """
    try:
        from core.mongo import get_db
        from nse.council import COUNCIL_COLLECTION
        db = get_db()
        if db is None:
            return {"levels": [], "zones": [], "error": "mongo unavailable"}
        doc = db[COUNCIL_COLLECTION].find_one(
            {"symbol": symbol}, {"_id": 0, "ts": 1, "spot": 1, "round1": 1,
                                 "direction_label": 1, "executed": 1},
            sort=[("ts", -1)])
    except Exception as exc:
        return {"levels": [], "zones": [], "error": str(exc)[:120]}

    if not doc:
        return {"levels": [], "zones": [], "error": "no decision yet"}

    feats = {v.get("lens"): (v.get("features") or {})
             for v in (doc.get("round1") or [])}
    levels: list[dict] = []
    zones: list[dict] = []

    def lvl(price, label, kind):
        if price is None:
            return
        try:
            p = float(price)
        except (TypeError, ValueError):
            return
        if p > 0:
            levels.append({"price": round(p, 2), "label": label, "kind": kind})

    cp = feats.get("composite_profile", {})
    lvl(cp.get("poc"), "Composite POC", "value")
    lvl(cp.get("vah"), "VAH", "value")
    lvl(cp.get("val"), "VAL", "value")
    lvl(cp.get("naked_poc"), "Naked POC (untested)", "magnet")

    vo = feats.get("volume_oi", {})
    lvl(vo.get("call_wall"), "Call wall (OI)", "supply")
    lvl(vo.get("put_wall"), "Put wall (OI)", "demand")
    lvl(vo.get("poc"), "Session POC", "value")
    lvl(vo.get("vah"), "Session VAH", "value")
    lvl(vo.get("val"), "Session VAL", "value")

    lvl(feats.get("gamma_exposure", {}).get("gamma_flip"), "Gamma flip", "pivot")

    vw = feats.get("vwap", {})
    lvl(vw.get("vwap"), "Session VWAP", "value")

    mo = feats.get("momentum", {})
    lvl(mo.get("window_high"), "Range high", "supply")
    lvl(mo.get("window_low"), "Range low", "demand")

    # ict_smc reports its structure in prose; the numeric zone edges are not in
    # `features` today. Surfaced as the rationale so the panel can show WHAT it
    # saw even before the coordinates exist, rather than silently omitting the
    # one lens that reads structure.
    ict = feats.get("ict_smc", {})
    return {
        "symbol": symbol,
        "as_of": doc.get("ts"),
        "spot": doc.get("spot"),
        "direction": doc.get("direction_label"),
        "executed": bool(doc.get("executed")),
        "levels": levels,
        "zones": zones,
        "structure": {
            "trend": ict.get("trend"),
            "order_block": ict.get("order_block"),
            "fvg": ict.get("fvg"),
            "sweep": ict.get("sweep"),
            "atr_pct": ict.get("atr_pct"),
        },
    }


@router.get("/markers")
def nse_markers(symbol: str = Query("NIFTY"),
                limit: int = Query(120, ge=1, le=500),
                user: dict = Depends(_get_current_user)):
    """Every council decision as a chart marker, executed AND declined.

    The declined ones matter most on a chart: seeing WHERE the council stood
    aside, against the price action that followed, is how you judge whether the
    conviction gate is protecting you or costing you. A chart showing only
    fills would make the system look far more active — and far more right —
    than it is.
    """
    try:
        from core.mongo import get_db
        from nse.council import COUNCIL_COLLECTION
        db = get_db()
        if db is None:
            return {"markers": [], "error": "mongo unavailable"}
        rows = list(db[COUNCIL_COLLECTION]
                    .find({"symbol": symbol},
                          {"_id": 0, "ts": 1, "direction": 1, "executed": 1,
                           "conviction": 1, "lead": 1, "reason": 1, "spot": 1})
                    .sort("ts", -1).limit(limit))
    except Exception as exc:
        return {"markers": [], "error": str(exc)[:120]}

    out = []
    for r in rows:
        ts = r.get("ts")
        if not ts:
            continue
        try:
            t = int(_dt.fromisoformat(ts).timestamp())
        except Exception:
            continue
        d = int(r.get("direction") or 0)
        out.append({
            "time": t,
            "executed": bool(r.get("executed")),
            "direction": d,
            "conviction": round(float(r.get("conviction") or 0.0), 3),
            "lead": r.get("lead"),
            "spot": r.get("spot"),
            "text": (f"{'LONG' if d > 0 else 'SHORT' if d < 0 else 'FLAT'} "
                     f"{abs(float(r.get('conviction') or 0)):.2f}"),
            "reason": (r.get("reason") or "")[:120],
        })
    out.sort(key=lambda m: m["time"])

    # What the council EXPECTS from the newest live decision, drawn as a box on
    # the chart: entry, the horizon it must resolve by, and the range the
    # MEASURED edge implies.
    #
    # This is an expectation, NOT a forecast path. The system produces a
    # direction, a conviction and a 60-minute horizon; it does not produce a
    # trajectory. Drawing a zig-zag to a target would imply a precision that
    # nothing here measured -- and would be the single easiest way to make an
    # unproven system look authoritative.
    #
    # The band is volume_oi's measured VALIDATE edge (+1.49 bps) applied to
    # spot, with the observed dispersion as the width. It is deliberately
    # small, because the measured edge IS small: 1.49 bps on 24,600 is under
    # four points. An honest expectation box looks unexciting, and that is the
    # information.
    expectation = None
    live = [m for m in out if m["executed"]]
    if live:
        m = live[-1]
        spot = m.get("spot")
        if spot:
            from nse.execution.options_runner import HOLD_MINUTES
            edge_bps, sd_bps = 1.49, 21.0      # measured VALIDATE mean and sd
            d = m["direction"]
            expectation = {
                "from_time": m["time"],
                "to_time": m["time"] + HOLD_MINUTES * 60,
                "entry": round(float(spot), 2),
                "direction": d,
                "horizon_min": HOLD_MINUTES,
                "target": round(float(spot) * (1 + d * edge_bps / 10_000), 2),
                "band_high": round(float(spot) * (1 + (edge_bps + sd_bps) / 10_000), 2),
                "band_low": round(float(spot) * (1 - (sd_bps - edge_bps) / 10_000), 2),
                "basis": (f"volume_oi measured VALIDATE edge {edge_bps:+.2f} bps "
                          f"over {HOLD_MINUTES}m, sd {sd_bps:.0f} bps"),
            }

    return {"symbol": symbol, "markers": out, "count": len(out),
            "expectation": expectation}
