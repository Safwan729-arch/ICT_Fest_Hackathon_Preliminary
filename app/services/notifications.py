"""Side effects that accompany booking lifecycle events.

Each booking change sends a (simulated) notification email and appends an
audit-log entry. Both resources are guarded by locks so their output stays
consistent when many requests are processed at once.
"""
import threading
import time

_email_lock = threading.Lock()
_audit_lock = threading.Lock()


def _send_email(kind: str, booking) -> None:
    # Simulated SMTP round-trip.
    time.sleep(0.12)


def _write_audit(kind: str, booking) -> None:
    # Simulated audit-log formatting/flush.
    time.sleep(0.1)


def notify_created(booking) -> None:
    with _email_lock:
        _send_email("created", booking)
        with _audit_lock:
            _write_audit("created", booking)


def notify_cancelled(booking) -> None:
    # Acquire locks in the same order as notify_created (email then audit) so a
    # concurrent create + cancel can never deadlock and hang the service (rule 16).
    with _email_lock:
        with _audit_lock:
            _write_audit("cancelled", booking)
            _send_email("cancelled", booking)
