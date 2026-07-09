# Bug Report — CoWork Bug-Fix Challenge

For each bug: **file & line(s)**, **what the bug was and why it caused incorrect behavior**
(tied to the numbered business rule it violates), and **how it was fixed**.

> **Line numbers refer to the original (buggy) source** as shipped in the initial commit
> `5bb6f56`, so a reviewer diffing against the starting code finds each bug exactly. Grouped by
> difficulty tier. The API contract (paths, status codes, error `code` strings, JSON field
> names, JWT claims) was preserved throughout; only broken code was changed.

---

## Concurrency (Hard)

### 1. Double-booking race — `app/routers/bookings.py:42-52`, `100-118` (`_has_conflict`, `create_booking`)
- **Bug:** `_has_conflict` reads all confirmed bookings (lines 43-47) and `create_booking` then
  inserts (lines 116-117) with no atomicity between the check and the insert. Two concurrent
  requests for the same room can both pass `_has_conflict` and both insert → two overlapping
  confirmed bookings, violating rule 3 ("holds under concurrency"). The `_pricing_warmup()`
  sleep at line 48 widens the race window.
- **Fix:** Made the conflict-check → quota-check → insert → commit one atomic critical section
  guarded by a lock, with `db.rollback()` *before* acquiring the lock so the session holds no
  SQLite read lock while waiting (avoids a Python-lock/DB-lock deadlock) and re-queries fresh
  committed state inside the lock.

### 2. Overlap operator rejects legal back-to-back bookings — `app/routers/bookings.py:50`
- **Bug:** `if b.start_time <= end and start <= b.end_time:` uses `<=`, so a booking ending
  exactly when another starts is flagged as a conflict. Rule 3 defines overlap with strict `<`
  (`existing.start < new.end AND new.start < existing.end`); back-to-back is allowed.
- **Fix:** Changed both comparisons to strict `<`: `if b.start_time < end and start < b.end_time:`.

### 3. Booking-quota race — `app/routers/bookings.py:59-71`, `103` (`_check_quota`)
- **Bug:** `_check_quota` counts confirmed bookings in the window (lines 59-68) and the insert
  follows (line 116-117) non-atomically. Concurrent requests each count the same `N < 3` and all
  insert → a member holds more than 3 confirmed bookings in `(now, now+24h]`, violating rule 4.
  The `_quota_audit()` sleep at line 69 widens the window.
- **Fix:** The quota count now runs inside the same locked critical section as the insert, so the
  count always reflects prior committed bookings.

### 4. Concurrent cancel of the same booking → duplicate refunds — `app/routers/bookings.py:195-214` (`cancel_booking`)
- **Bug:** The already-cancelled check (line 195), `log_refund` (line 210) and
  `booking.status = "cancelled"` (line 213) are not atomic. Two concurrent cancels of the same
  booking both pass line 195 and both call `log_refund` → two RefundLog rows, and the returned
  amount can disagree with a stored amount, violating rule 6 (exactly one RefundLog;
  response amount == stored amount; holds under concurrent cancels). The `_settlement_pause()`
  at line 212 widens the window.
- **Fix:** Replaced the read-modify-write with an atomic conditional
  `UPDATE bookings SET status='cancelled' WHERE id=? AND status='confirmed'`; only the request
  whose update matches a row (`rowcount == 1`) writes the refund → exactly one RefundLog, and the
  loser gets `409 ALREADY_CANCELLED`. The amount is computed once and both returned and stored.

### 5. Reference-code collision — `app/services/reference.py:17-21` (`next_reference_code`)
- **Bug:** `current = counter` (line 18) → `sleep` (line 19) → `counter = current + 1` (line 20)
  is an unlocked read-then-increment. Concurrent callers read the same value and produce
  identical `reference_code`s, violating rule 7 (unique including under concurrency).
- **Fix:** Guarded the read-increment with a module `threading.Lock`.

### 6. Room-stats drift under concurrent bursts — `app/services/stats.py:15-26` (`record_create`, `record_cancel`)
- **Bug:** Both functions do read (line 16/23) → `sleep` (line 18/25) → write (line 19/26) on a
  shared dict with no lock, so concurrent updates lose increments/decrements and the stats drift
  from the actual bookings, violating rule 14 (always consistent, including after concurrent
  bursts).
- **Fix:** Guarded both mutators (and `get`) with a module `threading.Lock`.

### 7. Rate limit leaks under load — `app/services/ratelimit.py:18-26` (`record_and_check`)
- **Bug:** Read bucket (line 20) → trim (line 21) → `sleep` (line 22) → append/store (lines
  23-24) runs without a lock. Concurrent requests read the same bucket and each believe they are
  under 20/60s, so more than 20 are admitted, violating rule 5 ("holds under concurrency").
- **Fix:** Wrapped the whole read-trim-append-check in a module `threading.Lock`.

### 8. Notification deadlock hangs the service — `app/services/notifications.py:24-35` (`notify_created`, `notify_cancelled`)
- **Bug:** `notify_created` acquires `_email_lock` then `_audit_lock` (lines 25-27) while
  `notify_cancelled` acquires them in the opposite order, `_audit_lock` then `_email_lock`
  (lines 32-34). A concurrent create + cancel can deadlock and hang the service, violating rule
  16 (no combination of concurrent valid requests may hang it).
- **Fix:** Made `notify_cancelled` acquire the locks in the same order as `notify_created`
  (email then audit); consistent global lock ordering cannot deadlock.

---

## Logic / correctness (Medium)

### 9. Datetime offset not converted to UTC — `app/timeutils.py:12-13` (`parse_input_datetime`)
- **Bug:** For tz-aware input the code does `dt = dt.replace(tzinfo=None)` (line 13), which drops
  the offset **without converting**, so `12:00+02:00` is stored as `12:00` UTC instead of
  `10:00`, violating rule 1. This corrupts every downstream comparison (overlap, quota,
  availability, reports).
- **Fix:** `dt = dt.astimezone(timezone.utc).replace(tzinfo=None)`.

### 10. Access-token lifetime is 54000s, not 900s — `app/auth.py:50` (`create_access_token`)
- **Bug:** `lifetime = timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES * 60)`. With
  `ACCESS_TOKEN_EXPIRE_MINUTES = 15` this is `timedelta(minutes=900)` = 54000s, but rule 8
  requires access tokens to expire in exactly 900s.
- **Fix:** `timedelta(minutes=ACCESS_TOKEN_EXPIRE_MINUTES)` (15 min = 900s).

### 11. Logout never invalidates the token — `app/auth.py:86`, `97` (`revoke_access_token`, `get_token_payload`)
- **Bug:** `revoke_access_token` stores `payload["jti"]` (line 86) but `get_token_payload` checks
  `payload.get("sub") in _revoked_tokens` (line 97) — user id vs. token id. The two never match,
  so a logged-out token keeps working, violating rule 8 (logout must immediately invalidate the
  presented token → 401 on reuse).
- **Fix:** Added `is_token_revoked()` that checks `jti`, and used it in `get_token_payload`.

### 12. Refresh tokens are not single-use — `app/routers/auth.py:81-93` (`refresh`)
- **Bug:** `/auth/refresh` decodes the refresh token and issues a new pair but never invalidates
  the presented token, so the same refresh token can be replayed indefinitely, violating rule 8
  (refresh is single-use; reuse → 401).
- **Fix:** Reject a refresh token whose `jti` is already revoked (`401`), and revoke the
  presented token's `jti` before returning the rotated pair.

### 13. Refund tier boundaries wrong — `app/routers/bookings.py:200-206` (`cancel_booking`)
- **Bug:** Tiers use `notice_hours = int(notice.total_seconds() // 3600)` then `if notice_hours >
  48` (line 201), so exactly 48h — and e.g. 48.5h floored to 48 — get 50% instead of 100%; and
  the `else` branch (lines 205-206) returns **50%** where rule 6 requires **0%** for notice < 24h.
- **Fix:** Compare the actual `timedelta`: `>= 48h → 100`, `>= 24h → 50`, else `0`.

### 14. Refund rounding + response/stored mismatch — `app/routers/bookings.py:208`, `app/services/refunds.py:14-17`
- **Bug:** The response uses `round(price_cents * (percent/100.0))` (bookings.py line 208) —
  Python banker's rounding, e.g. `round(500.5) == 500`, but rule 6 requires half-cents to round
  **up** (→ 501). Separately, `log_refund` recomputes with `int(refund_dollars * 100)`
  (refunds.py lines 15-17, truncation). The two independent computations can disagree, violating
  rule 6 (response amount must equal the stored RefundLog amount).
- **Fix:** Compute the amount **once** with half-up integer math
  `(price_cents * percent + 50) // 100` and pass that exact value into `log_refund`, which now
  just stores the passed-in `amount_cents`.

### 15. Missing minimum-duration / `end > start` check — `app/routers/bookings.py:93-94` (`create_booking`)
- **Bug:** Only `if duration_hours > MAX_DURATION_HOURS` (line 93) is checked;
  `MIN_DURATION_HOURS` (defined line 21) is never used, so a zero-hour (`end == start`) or
  negative-duration booking is accepted, violating rule 2 (duration whole hours, min 1, max 8;
  `end` strictly after `start`).
- **Fix:** `if duration_hours < MIN_DURATION_HOURS or duration_hours > MAX_DURATION_HOURS:`.

### 16. Past-start grace window — `app/routers/bookings.py:86` (`create_booking`)
- **Bug:** `if start <= now - timedelta(seconds=300):` allows a `start_time` up to 5 minutes in
  the past, but rule 2 requires `start` strictly in the future with **no** grace window.
- **Fix:** `if start <= now:`.

### 17. Pagination broken three ways — `app/routers/bookings.py:137-139` (`list_bookings`)
- **Bug:** `order_by(Booking.start_time.desc(), Booking.id.asc())` (line 137) sorts descending —
  rule 11 requires ascending; `.offset(page * limit)` (line 138) skips the first page's rows —
  should be `(page-1)*limit`; `.limit(10)` (line 139) is hardcoded, ignoring the `limit` query
  param. Together these skip/repeat items and return wrong page sizes.
- **Fix:** `order_by(start_time.asc(), id.asc())`, `.offset((page-1)*limit)`, `.limit(limit)`.

### 18. `GET /bookings/{id}` corrupts `start_time` — `app/routers/bookings.py:166` (`get_booking`)
- **Bug:** `response["start_time"] = iso_utc(booking.created_at)` (line 166) overwrites the real
  start time with the creation time, so the detail response returns the wrong value for the
  `start_time` field.
- **Fix:** Removed the overwriting line; `serialize_booking` already sets `start_time` correctly.

---

## Multi-tenancy / visibility (Medium)

### 19. Member can read another member's booking — `app/routers/bookings.py:156-163` (`get_booking`)
- **Bug:** `get_booking` scopes only by `org_id` (lines 156-161) and, unlike `cancel_booking`
  (which has the check at lines 192-193), never verifies a non-admin caller owns the booking, so
  any member can read a peer's booking by id, violating rule 10 (another member's id → 404
  `BOOKING_NOT_FOUND`).
- **Fix:** Added `if user.role != "admin" and booking.user_id != user.id:` → 404
  `BOOKING_NOT_FOUND`.

### 20. Cross-org data leak in export — `app/services/export.py:22-29`, `48-50` (`fetch_bookings_raw`, `generate_export`)
- **Bug:** With `include_all=True` and a `room_id`, `generate_export` calls
  `fetch_bookings_raw(db, room_id)` (line 50), which filters only by `room_id` (lines 24-28) with
  **no org check**, so an admin can export another tenant's bookings by passing a foreign
  `room_id`, violating rule 9 (cross-org IDs behave as non-existent).
- **Fix:** Route that branch through `_fetch_scoped(db, org_id, None, room_id)`, which joins
  `Room` and filters `Room.org_id == org_id`; a cross-org `room_id` now yields an empty
  (header-only) CSV.

---

## Cache staleness (Medium)

### 21. Usage report stale after new booking — `app/routers/bookings.py:120-122` (`create_booking`)
- **Bug:** After a create, only `cache.invalidate_availability(...)` is called (line 121);
  `cache.invalidate_report(...)` is missing, so `GET /admin/usage-report` serves stale numbers
  after a new booking, violating rule 12 (reflect current state immediately).
- **Fix:** Added `cache.invalidate_report(user.org_id)` on create.

### 22. Availability stale after cancel — `app/routers/bookings.py:216-218` (`cancel_booking`)
- **Bug:** After a cancel, only `cache.invalidate_report(...)` is called (line 217);
  `cache.invalidate_availability(...)` is missing, so a cancelled booking keeps showing as a busy
  interval, violating rule 13 (availability reflects current state immediately).
- **Fix:** Added `cache.invalidate_availability(room_id, start_date)` on cancel.

---

## Registration (Medium)

### 23. Duplicate username returns 201 instead of 409 — `app/routers/auth.py:37-43` (`register`)
- **Bug:** When the username already exists in the org, `register` returns the existing user
  (lines 37-43, a 201-shaped success) instead of rejecting the request, violating rule 15
  (duplicate username within an org → `409 USERNAME_TAKEN`).
- **Fix:** Raise `AppError(409, "USERNAME_TAKEN", ...)`. (The DB `UniqueConstraint("org_id",
  "username")` remains as a backstop.)
