"""Refuse to start the brain container if it can reach an order API.

WHY THIS EXISTS, AND WHY IT MATTERS MORE IN DOCKER THAN ON A LAPTOP

On a developer machine the tier split has two independent layers:

    1. the brain tier imports no order-placing code
    2. Angel rejects order placement from a non-whitelisted IP

Layer 2 is doing real work locally — even a mistaken import cannot place an
order from a home connection, because the broker refuses the packet.

ON THE DROPLET, LAYER 2 IS GONE. The sentinel and the brain share one host, and
that host's IP is the whitelisted one. Angel will happily accept an order from
*any* process on that box. The brain also legitimately needs ANGEL_*
credentials, because market data (chain, VIX, candles) uses the same keys as
order placement — so credential separation is not available either.

That leaves layer 1 as the ONLY protection, in the exact environment where a
mistake is most expensive. A guarantee that thin should not live in a test file
somebody runs occasionally; it should stop the container from booting.

Run as the brain container's entrypoint. Exit 0 to proceed, non-zero to abort.
"""

from __future__ import annotations

import sys

# Modules that can reach the order API. Importing any of these ANYWHERE in the
# brain tier's own package means the tier split has been dissolved.
FORBIDDEN_MODULES = ("nse.broker", "core.brokers", "SmartApi", "smartapi")

# The brain tier's own code. Third-party and stdlib are irrelevant; what matters
# is whether OUR modules reach the broker.
BRAIN_MODULES = (
    "nse.execution.options_runner",
    "nse.execution.live_session",
    "nse.council",
    "nse.journal",
    "nse.brain",
    "nse.selftune",
    "nse.snapshot",
)


def check() -> int:
    failures: list[str] = []

    # 1. Static check: the runner's own imports, parsed rather than grepped.
    try:
        from nse.execution.options_runner import test_no_order_imports
        test_no_order_imports()
        print("  ok   options_runner imports no order-placing code")
    except AssertionError as e:
        failures.append(f"options_runner: {e}")
    except Exception as e:
        failures.append(f"options_runner guard could not run: {e}")

    # 2. Dynamic check: import the brain tier for real, then look at what
    #    actually landed in sys.modules. This catches a transitive import the
    #    AST check on a single file cannot see — the realistic failure mode,
    #    since nobody adds `from nse.broker import AngelBroker` to the runner
    #    directly; they add a helper that does.
    import importlib

    before = set(sys.modules)
    imported_ok = 0
    for m in BRAIN_MODULES:
        try:
            importlib.import_module(m)
            imported_ok += 1
        except Exception as e:
            failures.append(f"{m} failed to import: {e}")
    pulled = set(sys.modules) - before

    leaked = sorted(m for m in pulled
                    if any(m == f or m.startswith(f + ".")
                           for f in FORBIDDEN_MODULES))
    if leaked:
        failures.append(
            "importing the brain tier pulled in order-placing modules: "
            + ", ".join(leaked))
    elif imported_ok == 0:
        # "Nothing imported, therefore nothing forbidden was imported" is
        # vacuously true and reads on screen as a pass. It caught nobody out
        # here only because the import errors were reported separately — but a
        # security check whose happy path fires when the check did not run is
        # one refactor away from being the only thing left saying "ok".
        failures.append(
            "could not import ANY brain module, so the leak check proved "
            "nothing — this is not a pass")
    else:
        print(f"  ok   brain tier imported {imported_ok}/{len(BRAIN_MODULES)} "
              f"modules ({len(pulled)} total), none of them order-placing")

    # 3. The sentinel must be reachable and must NOT be this process.
    import os
    url = os.environ.get("SENTINEL_URL", "")
    if not url:
        failures.append("SENTINEL_URL is unset — the brain has no route to the "
                        "sentinel and would silently never trade")
    elif "127.0.0.1" in url or "localhost" in url:
        # Inside compose the sentinel is another service on the docker network.
        # A loopback URL means the brain is pointed at itself, which on the
        # droplet usually means someone collapsed the two tiers into one
        # container.
        failures.append(f"SENTINEL_URL={url} is loopback; inside compose it "
                        f"should be the sentinel SERVICE name (http://sentinel:8090)")
    else:
        print(f"  ok   SENTINEL_URL points off-box: {url}")

    if os.environ.get("SENTINEL_LIVE_ORDERS") == "1":
        failures.append("SENTINEL_LIVE_ORDERS=1 is set in the BRAIN container. "
                        "That flag belongs to the sentinel alone; its presence "
                        "here means the two services share an env file.")

    if failures:
        print("\nBRAIN TIER GUARD FAILED — refusing to start:", file=sys.stderr)
        for f in failures:
            print(f"  - {f}", file=sys.stderr)
        print("\nOn the droplet the broker's IP whitelist does NOT protect you: "
              "this host is whitelisted, so any process here can place orders. "
              "The import boundary is the only thing left.", file=sys.stderr)
        return 1

    print("brain tier guard passed")
    return 0


if __name__ == "__main__":
    sys.exit(check())
