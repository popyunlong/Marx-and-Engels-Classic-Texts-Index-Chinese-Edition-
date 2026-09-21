"""A revocable claim gate; an already claimed user stage always finishes."""
import contextlib
import functools


@contextlib.contextmanager
def gate_file(path, *, exclusive=False):
    import fcntl
    with path.with_name('worker-admission.lock').open('rb') as handle:
        fcntl.flock(handle, fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        yield


def install(modules, path):
    def wrap(original):
        @functools.wraps(original)
        def claim(*args, **kwargs):
            with gate_file(path):
                if path.exists():
                    return original(*args, **kwargs)
            return None
        return claim
    for module in modules:
        module.claim_next_job = wrap(module.claim_next_job)


def deactivate(path):
    with gate_file(path, exclusive=True):
        path.unlink(missing_ok=True)
