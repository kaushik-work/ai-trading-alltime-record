"""
Runtime strategy/instrument toggle persistence.

Stores enable/disable flags in db/flags/strategy_toggles.json so the dashboard
can turn strategies (and instruments within them) on/off without restarting the
bot. The file is read on every decision tick; writes are atomic (write-then-rename)
to avoid corruption.

Default state for any strategy/instrument is ENABLED. Disabling is explicit.
"""

from __future__ import annotations

import json
import os
import threading
from copy import deepcopy
from pathlib import Path
logger = __import__("logging").getLogger(__name__)

_FLAG_DIR = Path(__file__).parent.parent / "db" / "flags"
_FLAG_FILE = _FLAG_DIR / "strategy_toggles.json"
_lock = threading.Lock()

_DEFAULTS = {
    "version": 1,
    "strategies": {
        "eth_price_action_sr": {
            "enabled": True,
            "instruments": {"ETHUSD": True},
        },
        "eth_short_straddle": {
            "enabled": True,
            "instruments": {"ETH": True},
        },
        "btc_short_straddle": {
            "enabled": False,
            "instruments": {"BTC": False},
        },
    },
}


def _ensure_dir() -> None:
    _FLAG_DIR.mkdir(parents=True, exist_ok=True)


def _load() -> dict:
    _ensure_dir()
    if not _FLAG_FILE.exists():
        return deepcopy(_DEFAULTS)
    try:
        with open(_FLAG_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        if not isinstance(data, dict) or "strategies" not in data:
            return deepcopy(_DEFAULTS)
        return data
    except Exception as e:
        logger.warning("strategy_toggles load failed: %s; using defaults", e)
        return deepcopy(_DEFAULTS)


def _save(data: dict) -> None:
    _ensure_dir()
    tmp = _FLAG_FILE.with_suffix(".tmp")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, indent=2)
        tmp.replace(_FLAG_FILE)
    except Exception as e:
        logger.error("strategy_toggles save failed: %s", e)
        raise


def get_toggles() -> dict:
    """Return full toggle state (read-only copy)."""
    with _lock:
        return deepcopy(_load())


def list_strategies() -> list[dict]:
    """Flatten toggle state for the dashboard."""
    data = get_toggles()
    out = []
    for name, cfg in data.get("strategies", {}).items():
        instruments = [
            {"name": k, "enabled": bool(v)}
            for k, v in cfg.get("instruments", {}).items()
        ]
        out.append({
            "name": name,
            "enabled": bool(cfg.get("enabled", True)),
            "instruments": instruments,
        })
    return out


def is_strategy_enabled(name: str) -> bool:
    """Default True if unknown."""
    data = get_toggles()
    return bool(data.get("strategies", {}).get(name, {}).get("enabled", True))


def is_instrument_enabled(strategy_name: str, instrument: str) -> bool:
    """Default True if unknown. If strategy is disabled, instruments are disabled too."""
    if not is_strategy_enabled(strategy_name):
        return False
    data = get_toggles()
    inst = data.get("strategies", {}).get(strategy_name, {}).get("instruments", {})
    return bool(inst.get(instrument, True))


def set_strategy_enabled(name: str, enabled: bool) -> dict:
    with _lock:
        data = _load()
        strategies = data.setdefault("strategies", {})
        cfg = strategies.setdefault(name, {"enabled": True, "instruments": {}})
        cfg["enabled"] = bool(enabled)
        _save(data)
        return deepcopy(cfg)


def set_instrument_enabled(strategy_name: str, instrument: str, enabled: bool) -> dict:
    with _lock:
        data = _load()
        strategies = data.setdefault("strategies", {})
        cfg = strategies.setdefault(strategy_name, {"enabled": True, "instruments": {}})
        cfg.setdefault("instruments", {})[instrument] = bool(enabled)
        _save(data)
        return deepcopy(cfg)


def reset_to_defaults() -> dict:
    with _lock:
        data = deepcopy(_DEFAULTS)
        _save(data)
        return deepcopy(data)
