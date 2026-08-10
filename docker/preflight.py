"""Pre-live readiness check. Run on the droplet before arming anything.

    docker compose --profile council run --rm council python docker/preflight.py
    docker compose --profile council run --rm council python docker/preflight.py --expect-ip 203.0.113.7

Answers one question: if orders were armed right now, would this deployment
behave the way it is supposed to? Every check either passes with the observed
value or fails with what to do about it. Exit 0 means ready.

WHY AN IP CHECK IS THE FIRST THING HERE

Angel authorises order placement by source IP. The failure mode when that is
wrong is not an error at deploy time — it is a rejected order at the exact
moment a position needs to open or close, which reads in the logs like a broker
problem rather than a configuration one. Checking the EGRESS IP (what Angel
actually sees) against the IP you registered turns a mid-session surprise into a
deploy-time failure.

A droplet can egress from an address that is not the one shown in the control
panel — floating IPs, NAT gateways and VPC routing all do this — so the address
is observed from outside rather than read from a local interface.

This never places an order and never arms anything.
"""

from __future__ import annotations

import argparse
import os
import sys
from typing import Optional

# Load .env before reading a single variable. Without this, check_secrets() ran
# before anything imported core.mongo (which loads it as a side effect) and
# reported every Angel credential as missing on a correctly configured host —
# while the live-snapshot check further down succeeded using those same
# credentials. A readiness check that cries wolf is worse than none, because the
# first thing anyone does with it is learn to ignore its failures.
#
# This is the rule from CLAUDE.md: a module that reads env vars loads them.
try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:                                   # pragma: no cover
    pass

PASS, FAIL, WARN = "PASS", "FAIL", "WARN"
_results: list[tuple[str, str, str]] = []


def record(level: str, name: str, detail: str = "") -> None:
    _results.append((level, name, detail))
    mark = {"PASS": "  ok  ", "FAIL": " FAIL ", "WARN": " warn "}[level]
    print(f"{mark} {name}" + (f"  — {detail}" if detail else ""), flush=True)


def egress_ip() -> Optional[str]:
    """The address the outside world sees. None if it cannot be determined."""
    import urllib.request
    for url in ("https://api.ipify.org", "https://checkip.amazonaws.com",
                "https://ifconfig.me/ip"):
        try:
            with urllib.request.urlopen(url, timeout=6) as r:
                ip = r.read().decode().strip()
                if ip and len(ip) <= 45:
                    return ip
        except Exception:
            continue
    return None


def check_ip(expected: Optional[str]) -> None:
    ip = egress_ip()
    if ip is None:
        record(WARN, "egress IP", "could not be determined — check manually "
                                  "that Angel has this host whitelisted")
        return
    if not expected:
        record(WARN, "egress IP", f"{ip} — pass --expect-ip to assert this is "
                                  f"the address registered with Angel")
        return
    if ip == expected:
        record(PASS, "egress IP", f"{ip} matches the registered address")
    else:
        record(FAIL, "egress IP",
               f"this host egresses from {ip}, but {expected} is registered. "
               f"Orders will be REJECTED by Angel. Whitelist {ip} or route "
               f"egress through {expected}")


def check_secrets() -> None:
    secret = os.environ.get("SENTINEL_SECRET", "")
    if not secret:
        record(FAIL, "SENTINEL_SECRET", "unset — the sentinel will reject every intent")
    elif len(secret) < 32:
        record(FAIL, "SENTINEL_SECRET", f"only {len(secret)} chars, needs >= 32")
    else:
        record(PASS, "SENTINEL_SECRET", f"{len(secret)} chars")

    # Names taken from root config.py, not guessed. ANGEL_TOTP_TOKEN is the
    # base32 pyotp secret; an earlier draft looked for ANGEL_TOTP_SECRET and
    # reported it missing on a host where auto-login was working fine.
    for k in ("ANGEL_API_KEY", "ANGEL_CLIENT_ID", "ANGEL_PASSWORD",
              "ANGEL_TOTP_TOKEN"):
        record(PASS if os.environ.get(k) else FAIL, k,
               "present" if os.environ.get(k) else "missing")

    # NOT a readiness failure, and the earlier wording here was wrong.
    #
    # This check originally said the Algo-ID was "required by SEBI for algo
    # order tagging", implying a missing payload field. Angel's actual
    # implementation of the SEBI retail-algo circular (live 1 Aug 2025) is
    # STATIC IP REGISTRATION, a 10 orders/sec throttle, and OAuth — see
    # smartapi.angelone.in forum topic 5254. No per-order algoid field is
    # documented in the Orders API, and nse/broker/angel_broker.py accordingly
    # sends none.
    #
    # Left as informational rather than removed: if Angel later adds a payload
    # field, this is where its absence should surface. Reporting an unverified
    # requirement as a failure trains people to ignore the whole check.
    algo = os.environ.get("ANGEL_ALGO_ID") or os.environ.get("SEBI_ALGO_ID")
    record(PASS, "SEBI Algo-ID",
           f"{algo} (informational — not sent in the order payload; Angel's "
           f"compliance is IP-based)" if algo else
           "not set — no per-order algoid field is documented by Angel; "
           "compliance is via the registered static IP")


def check_mongo() -> None:
    try:
        from core.mongo import get_db
        db = get_db()
        if db is None:
            record(FAIL, "MongoDB",
                   "unreachable — brains, journals and decisions would not "
                   "persist and the council could not read yesterday")
            return
        n = db.nse_lens_brains.estimated_document_count()
        record(PASS if n else WARN, "MongoDB",
               f"{db.name}, {n} lens brains seeded"
               + ("" if n else " — run `python -m nse.lenses.bootstrap`"))
    except Exception as e:
        record(FAIL, "MongoDB", str(e)[:90])


def check_sentinel() -> None:
    url = os.environ.get("SENTINEL_URL", "")
    if not url:
        record(FAIL, "SENTINEL_URL", "unset")
        return
    try:
        import requests
        st = requests.get(f"{url.rstrip('/')}/status", timeout=6).json()
    except Exception as e:
        record(FAIL, "sentinel reachable", f"{url} — {str(e)[:70]}")
        return

    record(PASS, "sentinel reachable", url)
    record(PASS if not st.get("deadman", {}).get("fired") else FAIL,
           "dead-man's switch",
           "clear" if not st.get("deadman", {}).get("fired")
           else "LATCHED — clear it and find out why the brain went dark")
    record(WARN if not st.get("live_orders") else PASS, "sentinel armed",
           "SENTINEL_LIVE_ORDERS=1" if st.get("live_orders")
           else "orders DISARMED (paper) — this is the safe default")
    to = st.get("deadman", {}).get("timeout_sec")
    try:
        from nse.execution.sentinel_client import HEARTBEAT_INTERVAL_SEC as hb
        if to and hb * 2 < to:
            record(PASS, "heartbeat margin",
                   f"{hb:.0f}s beat inside a {to:.0f}s window ({to/hb:.0f}x)")
        else:
            record(FAIL, "heartbeat margin",
                   f"{hb:.0f}s beat vs {to}s timeout — the switch will fire on "
                   f"a healthy brain")
    except Exception as e:
        record(WARN, "heartbeat margin", str(e)[:70])


def check_tier_split() -> None:
    try:
        from docker.brain_guard import check as guard
        record(PASS if guard() == 0 else FAIL, "brain tier import boundary",
               "no path from the council to an order API")
    except Exception as e:
        record(FAIL, "brain tier import boundary", str(e)[:90])


def check_council() -> None:
    try:
        from nse.council import (COUNCIL_ADAPTIVE_QUORUM_BINDING,
                                 COUNCIL_DELIBERATION_BINDING, Council)
        from nse.execution.options_runner import _default_council
        c: Council = _default_council()
        weighted = {l.name: c.weight_of(l.name) for l in c.lenses
                    if c.weight_of(l.name) > 0}
        record(PASS if weighted else FAIL, "lenses with weight",
               ", ".join(f"{k}={v}" for k, v in weighted.items())
               or "NONE — the council cannot trade")
        record(PASS, "gate / deliberation / quorum",
               f"gate {c.min_lead_confidence:.3f}, "
               f"deliberation {'binding' if COUNCIL_DELIBERATION_BINDING else 'shadow'}, "
               f"quorum {'binding' if COUNCIL_ADAPTIVE_QUORUM_BINDING else 'shadow'}")
    except Exception as e:
        record(FAIL, "council", str(e)[:90])


def check_market_data() -> None:
    try:
        from nse.snapshot import build_live
        snap = build_live("NIFTY", strikes_around=10)
        if snap is None:
            record(WARN, "live snapshot",
                   "None — market may be closed, or the chain is unreadable")
            return
        bars = 0 if snap.bars is None else len(snap.bars)
        record(PASS, "live snapshot",
               f"spot {snap.spot:.1f}, {len(snap.chain)} contracts, {bars} bars, "
               f"{snap.dte:.2f} DTE")
        if bars < 40:
            record(WARN, "bar warm-up",
                   f"{bars} bars — ict_smc needs 40, momentum/vwap need 30, so "
                   f"they will abstain until later in the session")
    except Exception as e:
        record(WARN, "live snapshot", str(e)[:90])


def main(argv=None) -> int:
    p = argparse.ArgumentParser()
    p.add_argument("--expect-ip", help="the IP registered with Angel One")
    a = p.parse_args(argv)

    print("PRE-LIVE READINESS\n" + "=" * 62)
    print("\n[ broker reachability ]")
    check_ip(a.expect_ip)
    print("\n[ credentials ]")
    check_secrets()
    print("\n[ persistence ]")
    check_mongo()
    print("\n[ execution path ]")
    check_sentinel()
    check_tier_split()
    print("\n[ decision path ]")
    check_council()
    check_market_data()

    fails = [r for r in _results if r[0] == FAIL]
    warns = [r for r in _results if r[0] == WARN]
    print("\n" + "=" * 62)
    print(f"{len(_results) - len(fails) - len(warns)} passed, "
          f"{len(warns)} warnings, {len(fails)} failures")
    if fails:
        print("\nNOT READY:")
        for _, name, detail in fails:
            print(f"  - {name}: {detail}")
        return 1
    if warns:
        print("\nReady, with warnings worth reading above.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
