import threading

_stop_event = threading.Event()


def stop_scan():
    _stop_event.set()


def should_stop():
    return _stop_event.is_set()


def reset():
    _stop_event.clear()


class ScanCancelled(RuntimeError):
    """Cooperative cancellation, distinct from a source failure."""


def check_cancelled():
    if should_stop():
        raise ScanCancelled("Scan stopped by user")


def wait_or_cancel(seconds):
    if _stop_event.wait(max(0.0, seconds)):
        raise ScanCancelled("Scan stopped by user")


# HTTP helpers are also used for model reloads outside the scan worker.
_context = threading.local()


def cancellation_enabled():
    return bool(getattr(_context, "cancellable", False))


def cancellation_scope():
    from contextlib import contextmanager

    @contextmanager
    def scope():
        previous = cancellation_enabled()
        _context.cancellable = True
        try:
            yield
        finally:
            _context.cancellable = previous
    return scope()
