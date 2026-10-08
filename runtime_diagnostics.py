"""Opt-in thread snapshots for the Linux production watchdog; no request data."""
import faulthandler
import signal
import threading


def enable_thread_dumps():
    if not hasattr(signal, "SIGUSR1") or threading.current_thread() is not threading.main_thread():
        return False
    # Register before corpus loading as startup itself can be slow. No periodic
    # dumps and no locals: the watchdog requests one only after repeated failure.
    faulthandler.register(signal.SIGUSR1, all_threads=True, chain=False)
    return True
