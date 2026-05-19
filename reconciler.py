"""
Post reconciler — desired-state loop for scheduled Facebook posts.

The entries table is the desired state.  This reconciler makes Facebook
match it.  It wakes on extensions.reconcile_event (signalled by any route
that changes desired state) and also runs a safety-net poll every 30 s.

Reconciler steps (in priority order every cycle):
  Step 0 – mark_published : scheduled posts whose time has passed → status=published
  Step 1 – fb_delete      : denied/pending posts with a FB post_id → delete from FB
  Step 2 – schedule_new   : scheduled posts with no FB post_id → create on FB
  Step 3 – sync           : scheduled+on-FB posts whose desired state drifted → update FB

Step 3 handles both time-drift and text/number drift in one pass.  It also
catches 404 from FB (post deleted externally) and returns that post to pending.

Slot assignment uses only local DB data (get_taken_fb_slots()), never queries FB.
"""
import hashlib
import logging
import threading
import time
from datetime import datetime, timedelta
from typing import Optional

import pytz

log = logging.getLogger(__name__)

ISRAEL_TZ = pytz.timezone('Asia/Jerusalem')

# fb_ids currently being delete+recreated by step3.
# The webhook checks this to avoid cascading reconciler-initiated deletions.
_reconciling_fb_ids: set = set()
PUBLISH_LAG_MINUTES = 2        # posts are considered published this many minutes after scheduled_time
SAFETY_NET_INTERVAL = 30       # seconds between safety-net polls
MAX_FB_RETRIES = 3
FB_RETRY_BACKOFF = [2, 5, 15]  # seconds


# ---------------------------------------------------------------------------
# Public helpers (imported by routes)
# ---------------------------------------------------------------------------

def signal_reconciler():
    """Wake the reconciler immediately.  Call after any desired-state change."""
    import extensions
    extensions.reconcile_event.set()


def format_post_text(entry: dict) -> str:
    """Build the final post text that goes to Facebook."""
    return f"#{entry['post_number']}\n{entry['text']}"


def text_hash(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()[:16]


# ---------------------------------------------------------------------------
# Reconciler loop — run in a daemon thread
# ---------------------------------------------------------------------------

def reconciler_loop():
    import extensions
    event: threading.Event = extensions.reconcile_event
    db = extensions.db

    log.info("[reconciler] started")

    while True:
        # Wait for a signal or the safety-net timeout
        event.wait(timeout=SAFETY_NET_INTERVAL)
        event.clear()

        try:
            _run_cycle(db)
        except Exception as exc:
            log.error("[reconciler] unhandled exception in cycle: %s", exc, exc_info=True)


# ---------------------------------------------------------------------------
# One reconciliation cycle
# ---------------------------------------------------------------------------

def _run_cycle(db):
    import extensions
    fb = extensions.facebook_handler

    log.debug("[reconciler] cycle start")

    # ── Step 0: mark published ───────────────────────────────────────────
    cutoff = (datetime.now(ISRAEL_TZ) - timedelta(minutes=PUBLISH_LAG_MINUTES)).isoformat()
    published = db.mark_published_if_past(cutoff)
    if published:
        log.info("[reconciler] step0 marked %d post(s) as published", published)

    if not fb:
        log.debug("[reconciler] no facebook_handler — skipping FB steps")
        return

    # ── Step 1: delete FB posts that should no longer exist ─────────────
    for entry in db.get_entries_needing_fb_delete():
        _step_delete(db, fb, entry)

    # ── Step 2: schedule new posts ───────────────────────────────────────
    # Build in-memory set of already-taken slots so we don't double-book
    # within a single cycle (DB slots are written by confirm_scheduled).
    taken_slots: set = set(db.get_taken_fb_slots())
    for entry in db.get_entries_needing_schedule():
        slot = _step_schedule(db, fb, entry, taken_slots)
        if slot:
            taken_slots.add(slot)

    # ── Step 3: sync drifted posts ───────────────────────────────────────
    for entry in db.get_entries_needing_sync():
        _step_sync(db, fb, entry)

    log.debug("[reconciler] cycle end")


# ---------------------------------------------------------------------------
# Step implementations
# ---------------------------------------------------------------------------

def _step_delete(db, fb, entry):
    eid   = entry['id']
    fb_id = entry['facebook_post_id']
    log.info("[reconciler] step1 deleting FB post %s (entry %d, status=%s)",
             fb_id, eid, entry['status'])

    for attempt in range(MAX_FB_RETRIES):
        try:
            fb.delete_scheduled_post(fb_id)
            db.confirm_fb_deleted(eid)
            log.info("[reconciler] step1 deleted FB post %s for entry %d", fb_id, eid)
            return
        except Exception as exc:
            if _is_404(exc):
                # Already gone from FB — just clear our record
                log.warning("[reconciler] step1 FB post %s not found (404) — clearing entry %d",
                            fb_id, eid)
                db.confirm_fb_deleted(eid)
                return
            _maybe_alert_token(exc)
            log.warning("[reconciler] step1 attempt %d failed for entry %d: %s",
                        attempt + 1, eid, exc)
            if attempt < MAX_FB_RETRIES - 1:
                time.sleep(FB_RETRY_BACKOFF[attempt])

    log.error("[reconciler] step1 permanently failed for entry %d (fb_id=%s)", eid, fb_id)


def _step_schedule(db, fb, entry, taken_slots: set) -> Optional[str]:
    eid  = entry['id']
    text = format_post_text(entry)
    slot = entry.get('scheduled_time')

    if not slot:
        log.warning("[reconciler] step2 entry %d has no scheduled_time — skipping", eid)
        return None

    # If the desired slot is already taken (two concurrent approvals picked the same slot),
    # reassign a new slot and write it back to the desired-state column so the entry
    # is not stuck waiting forever.
    if slot in taken_slots:
        log.warning("[reconciler] step2 slot %s conflict for entry %d — reassigning", slot, eid)
        new_slot = _pick_next_free_slot(taken_slots)
        if not new_slot:
            log.error("[reconciler] step2 no free slots available — entry %d skipped", eid)
            return None
        slot = new_slot
        conn = db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE entries SET scheduled_time=? WHERE id=?', (slot, eid))
            conn.commit()
        finally:
            conn.close()
        log.info("[reconciler] step2 entry %d reassigned to %s", eid, slot)

    # Reassign if the desired slot is in the past — FB rejects past timestamps
    now = datetime.now(ISRAEL_TZ)
    slot_dt = _parse_slot(slot)
    if slot_dt is not None and slot_dt <= now:
        log.warning("[reconciler] step2 entry %d slot %s is in the past — reassigning", eid, slot)
        new_slot = _pick_next_free_slot(taken_slots)
        if not new_slot:
            log.error("[reconciler] step2 no free slots available — entry %d skipped", eid)
            return None
        slot = new_slot
        conn = db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute('UPDATE entries SET scheduled_time=? WHERE id=?', (slot, eid))
            conn.commit()
        finally:
            conn.close()
        log.info("[reconciler] step2 entry %d reassigned to future slot %s", eid, slot)

    log.info("[reconciler] step2 scheduling entry %d (#%d) at %s",
             eid, entry['post_number'], slot)

    slot_dt = _parse_slot(slot)
    if slot_dt is None:
        log.error("[reconciler] step2 entry %d has unparseable scheduled_time %r — skipping", eid, slot)
        return None

    for attempt in range(MAX_FB_RETRIES):
        try:
            result = fb.schedule_post(text, slot_dt)
            fb_id      = result['id']
            fb_slot    = result.get('scheduled_time', slot)
            post_num   = entry['post_number']
            t_hash     = text_hash(text)
            db.confirm_scheduled(eid, fb_id, fb_slot, post_num, t_hash)
            log.info("[reconciler] step2 scheduled entry %d → FB id=%s slot=%s",
                     eid, fb_id, fb_slot)
            return fb_slot
        except Exception as exc:
            _maybe_alert_token(exc)
            log.warning("[reconciler] step2 attempt %d failed for entry %d: %s",
                        attempt + 1, eid, exc)
            if attempt < MAX_FB_RETRIES - 1:
                time.sleep(FB_RETRY_BACKOFF[attempt])

    log.error("[reconciler] step2 permanently failed for entry %d", eid)
    return None


def _step_sync(db, fb, entry):
    eid    = entry['id']
    fb_id  = entry['facebook_post_id']

    desired_text = format_post_text(entry)
    desired_slot = entry.get('scheduled_time')
    desired_num  = entry['post_number']
    desired_hash = text_hash(desired_text)

    actual_slot  = entry.get('fb_scheduled_time')
    actual_num   = entry.get('fb_post_number')
    actual_hash  = entry.get('fb_text_hash')

    time_drift = _times_differ(desired_slot, actual_slot)
    text_drift = desired_hash != actual_hash
    num_drift  = desired_num  != actual_num

    if not (time_drift or text_drift or num_drift):
        return  # already in sync

    log.info("[reconciler] step3 entry %d drift: time=%s text=%s num=%s",
             eid, time_drift, text_drift, num_drift)

    _reconciling_fb_ids.add(fb_id)
    try:
        for attempt in range(MAX_FB_RETRIES):
            try:
                result = fb.update_scheduled_post(
                    fb_id,
                    new_text=desired_text if (text_drift or num_drift) else None,
                    new_time=_parse_slot(desired_slot) if time_drift else None,
                )
                new_fb_id   = result['id']
                new_fb_slot = result.get('scheduled_time', desired_slot)
                db.confirm_synced(eid, new_fb_id, new_fb_slot, desired_num, desired_hash)
                log.info("[reconciler] step3 synced entry %d old_fb=%s new_fb=%s",
                         eid, fb_id, new_fb_id)
                return
            except Exception as exc:
                if _is_404(exc):
                    # Post was deleted from FB externally — return it to pending
                    log.warning(
                        "[reconciler] step3 FB post %s not found (404) — "
                        "returning entry %d to pending", fb_id, eid
                    )
                    freed_num = db.return_post_to_pending_from_reconciler(fb_id)
                    log.info("[reconciler] step3 entry %d returned to pending, freed slot #%s",
                             eid, freed_num)
                    return
                _maybe_alert_token(exc)
                log.warning("[reconciler] step3 attempt %d failed for entry %d: %s",
                            attempt + 1, eid, exc)
                if attempt < MAX_FB_RETRIES - 1:
                    time.sleep(FB_RETRY_BACKOFF[attempt])

        log.error("[reconciler] step3 permanently failed for entry %d (fb_id=%s)", eid, fb_id)
    finally:
        _reconciling_fb_ids.discard(fb_id)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _parse_slot(slot: str) -> Optional[datetime]:
    """Parse a slot string in either strftime or isoformat to a tz-aware datetime."""
    if not slot:
        return None
    try:
        dt = datetime.fromisoformat(slot)
        if dt.tzinfo is None:
            dt = ISRAEL_TZ.localize(dt)
        return dt
    except ValueError:
        pass
    try:
        return ISRAEL_TZ.localize(datetime.strptime(slot, '%Y-%m-%d %H:%M:%S'))
    except ValueError:
        return None


def _times_differ(desired: Optional[str], actual: Optional[str]) -> bool:
    """Return True if two slot strings represent meaningfully different times (>=60 s apart).
    Tolerates format differences between strftime output and isoformat."""
    if desired == actual:
        return False
    if not desired or not actual:
        return desired != actual
    a = _parse_slot(desired)
    b = _parse_slot(actual)
    if a is None or b is None:
        return desired != actual
    return abs((a - b).total_seconds()) >= 60


def _is_404(exc: Exception) -> bool:
    """Return True if the exception represents a Facebook 'not found' error."""
    msg = str(exc).lower()
    return '404' in msg or 'not found' in msg or 'does not exist' in msg


def _maybe_alert_token(exc: Exception):
    """Fire a token-invalid email alert if exc looks like a 190 OAuthException."""
    err = str(exc)
    if '"code":190' not in err and 'OAuthException' not in err and 'Cannot parse access token' not in err:
        return
    try:
        from background_tasks import _send_token_alert, _notification_on_cooldown
        from config import load_config
        config = load_config()
        if not _notification_on_cooldown(config, 'token_alert'):
            log.warning("[reconciler] token invalid — sending alert")
            _send_token_alert(config, expired=True)
    except Exception as alert_exc:
        log.error("[reconciler] failed to send token alert: %s", alert_exc)


def _pick_next_free_slot(taken_slots: set) -> Optional[str]:
    """Find the next available posting slot not already in taken_slots.
    Delegates to the scheduler instance if available; falls back to a simple daily walk."""
    import extensions
    scheduler = extensions.scheduler
    if scheduler:
        try:
            return scheduler.get_next_available_slot_local(taken_slots).isoformat()
        except Exception as exc:
            log.warning("[reconciler] slot picker error: %s", exc)

    # Fallback: walk forward from now at 14:00 each day until we find a free slot
    now = datetime.now(ISRAEL_TZ)
    for days in range(1, 365):
        candidate = (now + timedelta(days=days)).replace(
            hour=14, minute=0, second=0, microsecond=0
        )
        iso = candidate.isoformat()
        if iso not in taken_slots:
            return iso
    return None
