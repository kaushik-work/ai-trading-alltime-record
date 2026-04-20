# Compatibility shim — all imports of zerodha_error_log now route to angel_error_log.
from core.angel_error_log import log_error, get_all, clear, ERROR_LOG_PATH  # noqa: F401

__all__ = ["log_error", "get_all", "clear", "ERROR_LOG_PATH"]
