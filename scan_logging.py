"""Small logging gate for noisy provider scanners."""
import builtins
from settings_manager import load_settings


def verbose_enabled():
    try:
        return bool(load_settings().get("preferences", {}).get("verbose_scan_logging", False))
    except Exception:
        return False


def verbose_print(*args, **kwargs):
    if verbose_enabled():
        builtins.print(*args, **kwargs)
