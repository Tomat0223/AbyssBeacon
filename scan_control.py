import threading

_stop_event = threading.Event()


def stop_scan():
    _stop_event.set()


def should_stop():
    return _stop_event.is_set()


def reset():
    _stop_event.clear()
