"""Small logging gate for noisy provider scanners."""
import builtins
import threading
from settings_manager import load_settings


def verbose_enabled():
    try:
        return bool(load_settings().get("preferences", {}).get("verbose_scan_logging", False))
    except Exception:
        return False


def verbose_print(*args, **kwargs):
    if verbose_enabled():
        builtins.print(*args, **kwargs)


_NOTICE_LOCK = threading.Lock()
_NOTICES = set()


def reset_scan_notices():
    """Start a new scan's once-only user-facing notices."""
    with _NOTICE_LOCK:
        _NOTICES.clear()


def notice_once(key, message):
    """Retain actionable notices without one copy per architecture/worker."""
    with _NOTICE_LOCK:
        if key in _NOTICES:
            return
        _NOTICES.add(key)
    builtins.print(message)
