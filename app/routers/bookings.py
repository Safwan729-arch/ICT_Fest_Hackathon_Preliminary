"""Booking creation, listing, detail and cancellation."""
import threading
import time
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, Query
from sqlalchemy.orm import Session

from .. import cache
from ..auth import get_current_user
from ..database import get_db
from ..errors import AppError
from ..models import Booking, Room, User
from ..schemas import BookingCreateRequest
from ..serializers import serialize_booking
from ..services import notifications, ratelimit, reference, stats
from ..services.refunds import log_refund
from ..timeutils import iso_utc, parse_input_datetime

router = APIRouter(tags=["bookings"])

MIN_DURATION_HOURS = 1
MAX_DURATION_HOURS = 8
QUOTA_LIMIT = 3
QUOTA_WINDOW_HOURS = 24

# Per-resource locks make the conflict/quota check-then-insert atomic without
# serializing unrelated bookings. The room lock guards double-booking (rule 3);
# the user lock guards the per-member quota (rule 4). They are always acquired in
# the order room-then-user, so no two creators can deadlock.
_room_locks: dict[int, threading.Lock] = {}
_user_locks: dict[int, threading.Lock] = {}
_locks_guard = threading.Lock()


def _keyed_lock(registry: dict, key: int) -> threading.Lock:
    with _locks_guard:
        lock = registry.get(key)
        if lock is None:
            lock = threading.Lock()
            registry[key] = lock
        return lock


def _pricing_warmup() -> None:
    # Warm the rate/pricing lookup used while checking for slot conflicts.
    time.sleep(0.12)


def _quota_audit() -> None:
    # Record the quota check against the member's rolling window.
    time.sleep(0.1)


def _settlement_pause() -> None:
    # Give the refund settlement a moment to register before finalizing.
    time.sleep(0.12)


def _has_conflict(db: Session, room_id: int, start: datetime, end: datetime) -> bool:
    existing = (
        db.query(Booking)
        .filter(Booking.room_id == room_id, Booking.status == "confirmed")
        .all()
    )
    _pricing_warmup()
    for b in existing:
        if b.start_time < end and start < b.end_time:
            return True
    return False


def _check_quota(db: Session, user_id: int, now: datetime, start: datetime) -> None:
    window_end = now + timedelta(hours=QUOTA_WINDOW_HOURS)
    if not (now < start <= window_end):
        return
    count = (
        db.query(Booking)
        .filter(
            Booking.user_id == user_id,
            Booking.status == "confirmed",
            Booking.start_time > now,
            Booking.start_time <= window_end,
        )
        .count()
    )
    _quota_audit()
    if count >= QUOTA_LIMIT:
        raise AppError(409, "QUOTA_EXCEEDED", "Booking quota exceeded")


@router.post("/bookings", status_code=201)
def create_booking(
    payload: BookingCreateRequest,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    ratelimit.record_and_check(user.id)

    start = parse_input_datetime(payload.start_time)
    end = parse_input_datetime(payload.end_time)
    now = datetime.utcnow()

    if start <= now:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "start_time must be in the future")

    duration_hours = (end - start).total_seconds() / 3600
    if duration_hours != int(duration_hours):
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration must be a whole number of hours")
    duration_hours = int(duration_hours)
    if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:
        raise AppError(400, "INVALID_BOOKING_WINDOW", "duration out of range")

    room = db.query(Room).filter(Room.id == payload.room_id, Room.org_id == user.org_id).first()
    if room is None:
        raise AppError(404, "ROOM_NOT_FOUND", "Room not found")

    room_id = room.id
    price_cents = room.hourly_rate_cents * duration_hours

    # Roll back first so this session holds no read lock while it waits for the
    # locks and then takes the SQLite write lock inside the critical section.
    db.rollback()
    room_lock = _keyed_lock(_room_locks, room_id)
    user_lock = _keyed_lock(_user_locks, user.id)
    with room_lock, user_lock:
        if _has_conflict(db, room_id, start, end):
            raise AppError(409, "ROOM_CONFLICT", "Room already booked for this interval")
        _check_quota(db, user.id, now, start)
        booking = Booking(
            room_id=room_id,
            user_id=user.id,
            start_time=start,
            end_time=end,
            status="confirmed",
            reference_code=reference.next_reference_code(),
            price_cents=price_cents,
            created_at=now,
        )
        db.add(booking)
        db.commit()
        db.refresh(booking)

    stats.record_create(room_id, price_cents)
    cache.invalidate_availability(room_id, start.date().isoformat())
    cache.invalidate_report(user.org_id)
    notifications.notify_created(booking)

    return serialize_booking(booking)


@router.get("/bookings")
def list_bookings(
    page: int = Query(1, ge=1),
    limit: int = Query(10, ge=1, le=100),
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    base = db.query(Booking).filter(Booking.user_id == user.id)
    total = base.count()
    items = (
        base.order_by(Booking.start_time.asc(), Booking.id.asc())
        .offset((page - 1) * limit)
        .limit(limit)
        .all()
    )
    return {
        "items": [serialize_booking(b) for b in items],
        "page": page,
        "limit": limit,
        "total": total,
    }


@router.get("/bookings/{booking_id}")
def get_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    response = serialize_booking(booking)
    response["refunds"] = [
        {
            "amount_cents": r.amount_cents,
            "status": r.status,
            "processed_at": iso_utc(r.processed_at),
        }
        for r in booking.refunds
    ]
    return response


@router.post("/bookings/{booking_id}/cancel")
def cancel_booking(
    booking_id: int,
    db: Session = Depends(get_db),
    user: User = Depends(get_current_user),
):
    booking = (
        db.query(Booking)
        .join(Room, Booking.room_id == Room.id)
        .filter(Booking.id == booking_id, Room.org_id == user.org_id)
        .first()
    )
    if booking is None:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")
    if user.role != "admin" and booking.user_id != user.id:
        raise AppError(404, "BOOKING_NOT_FOUND", "Booking not found")

    if booking.status == "cancelled":
        raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

    now = datetime.utcnow()
    notice = booking.start_time - now
    if notice >= timedelta(hours=48):
        refund_percent = 100
    elif notice >= timedelta(hours=24):
        refund_percent = 50
    else:
        refund_percent = 0

    # Nearest cent, half-cents rounding up. Integer math avoids float error and
    # Python's banker's rounding, and is the single source of the amount so the
    # response value always equals the value stored in the RefundLog.
    refund_amount_cents = (booking.price_cents * refund_percent + 50) // 100

    # Capture immutable fields before the read transaction is dropped below.
    booking_id_val = booking.id
    room_id = booking.room_id
    price_cents = booking.price_cents
    start_date = booking.start_time.date().isoformat()

    # Atomically claim the cancellation: exactly one concurrent request flips a
    # still-"confirmed" row, so exactly one RefundLog is written. Roll back first
    # so this session holds no read lock when it takes the SQLite write lock.
    _settlement_pause()
    db.rollback()
    claimed = (
        db.query(Booking)
        .filter(Booking.id == booking_id_val, Booking.status == "confirmed")
        .update({Booking.status: "cancelled"}, synchronize_session=False)
    )
    db.commit()
    if claimed == 0:
        raise AppError(409, "ALREADY_CANCELLED", "Booking already cancelled")

    log_refund(db, booking, refund_amount_cents)

    stats.record_cancel(room_id, price_cents)
    cache.invalidate_report(user.org_id)
    cache.invalidate_availability(room_id, start_date)
    notifications.notify_cancelled(booking)

    return {
        "id": booking_id_val,
        "status": "cancelled",
        "refund_percent": refund_percent,
        "refund_amount_cents": refund_amount_cents,
    }
