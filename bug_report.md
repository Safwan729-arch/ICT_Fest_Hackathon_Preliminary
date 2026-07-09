# Bug Report — CoWork Bug-Fix Challenge

Each entry: **file/line**, **the bug + why it violated a rule (root cause)**, **the fix**.
Grouped by difficulty tier. The API contract (paths, status codes, error `code` strings,
JSON field names, JWT claims) was preserved throughout; only broken code was changed.

---

## Concurrency (Hard)

### 1. Double-booking race — `app/routers/bookings.py` (`create_booking`, `_has_conflict`)
- **Bug:** The overlap check read all confirmed bookings, then the new row was inserted, with
  no atomicity between check and insert. Two concurrent requests for the same room could both
  pass `_has_conflict` and both insert → two overlapping confirmed bookings (rule 3). The
  `_pricing_warmup()` sleep widened the window.
- **Fix:** Wrapped the conflict-check → quota-check → insert → commit in a process-wide
  `_booking_write_lock`, making it one atomic critical section. `db.rollback()` is called
  *before* acquiring the lock so the session holds no SQLite read lock while waiting (avoids a
  Python-lock/DB-lock deadlock), and the checks re-query fresh committed state inside the lock.

### 2. Overlap operator also broke back-to-back — `app/routers/bookings.py:55`
- **Bug:** `if b.start_time <= end and start <= b.end_time` used `<=`, so a booking ending
  exactly when another starts counted as a conflict. Rule 3 requires strict overlap
  (`existing.start < new.end AND new.start < existing.end`); back-to-back is allowed.
- **Fix:** Changed both comparisons to strict `<`.

### 3. Booking-quota race — `app/routers/bookings.py` (`_check_quota`)
- **Bug:** Count-confirmed-in-window then insert, non-atomic. Concurrent requests each counted
  the same N<3 and all inserted → member holds >3 in the `(now, now+24h]` window (rule 4).
- **Fix:** The quota count now runs inside the same `_booking_write_lock` critical section as
  the insert, so counts always reflect prior committed bookings.

### 4. Concurrent cancel of the same booking — `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** Read status → compute refund → `log_refund` → set `status="cancelled"` was
  non-atomic. Two concurrent cancels of the same booking both passed the `status=="cancelled"`
  check and both wrote a RefundLog → two refund rows, and the response amount could disagree
  with a stored amount (rule 6 idempotency).
- **Fix:** Replaced the read-modify-write with an atomic conditional
  `UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'`. Only the request
  whose update matches a row (`rowcount == 1`) proceeds to `log_refund` → exactly one RefundLog;
  the loser gets `409 ALREADY_CANCELLED`. `db.rollback()` precedes the write so the two writers
  serialize cleanly on SQLite's write lock.

### 5. Reference-code collision — `app/services/reference.py`
- **Bug:** `current = counter; sleep; counter = current + 1` — read-then-increment with no
  lock. Concurrent bookings could read the same counter value and get identical
  `reference_code`s (rule 7).
- **Fix:** Wrapped read-increment in a module `threading.Lock`.

### 6. Room-stats drift — `app/services/stats.py`
- **Bug:** `record_create` / `record_cancel` did read-modify-write on a shared dict with a sleep
  in the middle → lost updates under concurrent bursts, so stats no longer equalled the
  bookings (rule 14).
- **Fix:** Guarded both mutators (and `get`) with a module `threading.Lock`.

### 7. Rate-limit race — `app/services/ratelimit.py`
- **Bug:** Read bucket → trim → sleep → append → store, unlocked. Concurrent requests read the
  same bucket and each thought it was under 20/60s → more than 20 admitted (rule 5).
- **Fix:** Wrapped the whole read-trim-append-check in a module `threading.Lock`.

### 8. Notification deadlock (liveness) — `app/services/notifications.py`
- **Bug:** `notify_created` acquired `_email_lock` then `_audit_lock`; `notify_cancelled`
  acquired them in the opposite order. A concurrent create + cancel could deadlock and hang the
  service (rule 16).
- **Fix:** Made `notify_cancelled` acquire the locks in the same order as `notify_created`
  (email then audit); consistent global lock ordering cannot deadlock.

---

## Logic / correctness (Medium)

### 9. Datetime offset not converted to UTC — `app/timeutils.py` (`parse_input_datetime`)
- **Bug:** Offset-carrying input did `dt.replace(tzinfo=None)`, dropping the offset without
  converting, so `12:00+02:00` was stored as `12:00` UTC instead of `10:00` (rule 1). This
  corrupts every downstream comparison (overlap, quota, availability, reports).
- **Fix:** `dt.astimezone(timezone.utc).replace(tzinfo=None)`.

### 10. Access-token lifetime was 54000s, not 900s — `app/auth.py` (`create_access_token`)
- **Bug:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)` = `timedelta(minutes=900)` =
  54000s. Rule 8 requires exactly 900s.
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` (15 min = 900s).

### 11. Logout did not actually invalidate the token — `app/auth.py` (`get_token_payload`)
- **Bug:** Logout stored the token's `jti` in `_revoked_tokens`, but the request path checked
  `payload.get("sub") in _revoked_tokens` (user id vs. jti) — the check never matched, so
  logged-out tokens kept working (rule 8).
- **Fix:** Added `is_token_revoked()` that checks `jti`, and used it in `get_token_payload`.

### 12. Refresh tokens were not single-use — `app/routers/auth.py` (`refresh`)
- **Bug:** `/auth/refresh` issued a new pair but never invalidated the presented refresh token,
  so it could be reused indefinitely (rule 8).
- **Fix:** Reject a refresh token whose `jti` is already revoked (`401`), and revoke the
  presented token's `jti` before returning the rotated pair.

### 13. Refund tier boundaries wrong — `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** Tiers used `int(notice_hours) > 48` (so exactly 48h and e.g. 48.5h floored to 48 got
  50% instead of 100%), and the `<24h` branch returned **50%** instead of 0% (rule 6).
- **Fix:** Compared the actual `timedelta`: `>=48h → 100`, `>=24h → 50`, else `0`.

### 14. Refund rounding + response/stored mismatch — `app/routers/bookings.py`, `app/services/refunds.py`
- **Bug:** Response used `round(...)` (Python banker's rounding — `round(500.5)=500`, but rule 6
  requires half-cents **up** → 501), while `log_refund` independently recomputed with `int()`
  truncation. Two computations could disagree (rule 6: response amount must equal stored).
- **Fix:** Compute the amount once with half-up integer math
  `(price_cents * percent + 50) // 100` and pass that exact value into `log_refund` to store.

### 15. Missing minimum-duration / `end > start` check — `app/routers/bookings.py`
- **Bug:** Only `duration > 8` was rejected; `MIN_DURATION_HOURS` was defined but unused, so a
  zero-hour (`end == start`) or negative-duration booking was accepted (rule 2).
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS`.

### 16. Past-start grace window — `app/routers/bookings.py`
- **Bug:** `if start <= now - timedelta(seconds=300)` allowed a start up to 5 minutes in the
  past; rule 2 requires `start` strictly in the future with **no** grace.
- **Fix:** `if start <= now`.

### 17. Pagination broken three ways — `app/routers/bookings.py` (`list_bookings`)
- **Bug:** Sorted `start_time.desc()` (rule 11 requires ascending); offset `page * limit` (page
  1 skipped the first page — should be `(page-1)*limit`); `.limit(10)` hard-coded, ignoring the
  `limit` param.
- **Fix:** `order_by(start_time.asc(), id.asc())`, `.offset((page-1)*limit)`, `.limit(limit)`.

### 18. `GET /bookings/{id}` corrupted `start_time` — `app/routers/bookings.py` (`get_booking`)
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` overwrote the real start time
  with the creation time (wrong field value).
- **Fix:** Removed the overwriting line; `serialize_booking` already sets `start_time`.

---

## Multi-tenancy / visibility (Medium/Easy)

### 19. Member could read another member's booking — `app/routers/bookings.py` (`get_booking`)
- **Bug:** `get_booking` scoped by `org_id` but (unlike `cancel_booking`) never checked that a
  non-admin caller owns the booking, so any member could read a peer's booking by id (rule 10).
- **Fix:** Added `if user.role != "admin" and booking.user_id != user.id → 404
  BOOKING_NOT_FOUND`.

### 20. Cross-org data leak in export — `app/services/export.py` (`generate_export`)
- **Bug:** With `include_all=True` and a `room_id`, the code called `fetch_bookings_raw(room_id)`
  which filtered only by room and **not** by org — an admin could export another org's bookings
  by passing a foreign `room_id` (rule 9).
- **Fix:** Route that branch through `_fetch_scoped(db, org_id, None, room_id)`, which joins
  `Room` and filters `Room.org_id == org_id`.

---

## Cache staleness (Medium)

### 21. Usage report stale after new booking — `app/routers/bookings.py` (`create_booking`)
- **Bug:** Creating a booking invalidated the availability cache but not the report cache, so
  `GET /admin/usage-report` served stale numbers after a new booking (rule 12: reflect current
  state immediately).
- **Fix:** Added `cache.invalidate_report(user.org_id)` on create.

### 22. Availability stale after cancel — `app/routers/bookings.py` (`cancel_booking`)
- **Bug:** Cancelling invalidated the report cache but not the availability cache, so a
  cancelled booking kept showing as a busy interval (rule 13).
- **Fix:** Added `cache.invalidate_availability(room_id, start_date)` on cancel.

---

## Registration (Medium)

### 23. Duplicate username returned 201 instead of 409 — `app/routers/auth.py` (`register`)
- **Bug:** When the username already existed in the org, the endpoint returned the existing user
  (success) instead of rejecting it. Rule 15 requires `409 USERNAME_TAKEN`.
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)`.
