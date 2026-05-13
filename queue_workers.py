"""
Comment action worker.

Reads Jobs from extensions.comment_queue, executes the Facebook API call,
and updates hidden_comments.status.  Retries ×3 with 5 / 15 / 45-second
backoff; on permanent failure reverts the DB status to its previous value.

Actions handled:
  hide     – hide comment on FB → set status='hidden'
  unhide   – unhide comment on FB → set status='shown'
  delete   – delete comment on FB → set status='deleted'

The routes guarantee:
  • hidden_comments.status is set to 'queued' (with prev_status saved in payload)
    before the job is enqueued — so the UI reflects the pending state immediately.
  • Only this worker writes the final status back.
"""
import logging
import time
import queue as queue_mod

log = logging.getLogger(__name__)

MAX_RETRIES = 3


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
            log.warning("[comment-worker] token invalid — sending alert")
            _send_token_alert(config, expired=True)
    except Exception as alert_exc:
        log.error("[comment-worker] failed to send token alert: %s", alert_exc)
BACKOFF = [5, 15, 45]   # seconds between retries


# ---------------------------------------------------------------------------
# Entry point — run in a background daemon thread
# ---------------------------------------------------------------------------

def comment_worker_loop():
    """Consume jobs from extensions.comment_queue forever."""
    import extensions
    q = extensions.comment_queue

    log.info("[comment-worker] started")
    while True:
        try:
            job = q.get(timeout=60)
        except queue_mod.Empty:
            continue
        except Exception as exc:
            log.error("[comment-worker] unexpected error getting job: %s", exc)
            continue

        try:
            _handle_job(job)
        except Exception as exc:
            log.error("[comment-worker] unhandled exception for job %d: %s",
                      job.journal_id, exc, exc_info=True)
        finally:
            q.task_done()


# ---------------------------------------------------------------------------
# Job dispatcher
# ---------------------------------------------------------------------------

def _handle_job(job):
    import extensions
    db = extensions.db

    action     = job.action
    payload    = job.payload
    comment_id = payload.get('comment_id', '')
    prev_status = payload.get('prev_status', 'shown')

    log.info("[comment-worker] job %d: action=%s comment=%s retry=%d",
             job.journal_id, action, comment_id[:30], job.retry_count)

    handler = {
        'hide':   _action_hide,
        'unhide': _action_unhide,
        'delete': _action_delete,
    }.get(action)

    if handler is None:
        log.error("[comment-worker] unknown action '%s' in job %d — dropping", action, job.journal_id)
        job.failed(f"unknown action: {action}")
        return

    for attempt in range(MAX_RETRIES):
        try:
            handler(payload)
            # Success — write final DB status (harmless even if comment was dismissed)
            _write_final_status(db, comment_id, action)
            job.done()
            log.info("[comment-worker] job %d done (attempt %d)", job.journal_id, attempt + 1)
            return
        except Exception as exc:
            _maybe_alert_token(exc)
            log.warning("[comment-worker] job %d attempt %d failed: %s",
                        job.journal_id, attempt + 1, exc)
            if attempt < MAX_RETRIES - 1:
                sleep_secs = BACKOFF[attempt]
                log.info("[comment-worker] retrying job %d in %ds", job.journal_id, sleep_secs)
                time.sleep(sleep_secs)
            else:
                # Permanent failure
                log.error("[comment-worker] job %d permanently failed after %d attempts: %s",
                          job.journal_id, MAX_RETRIES, exc)
                if action in ('hide', 'unhide'):
                    try:
                        db.update_comment_status(comment_id, prev_status)
                        log.info("[comment-worker] reverted comment %s status to '%s'",
                                 comment_id[:30], prev_status)
                    except Exception as revert_exc:
                        log.error("[comment-worker] failed to revert status for %s: %s",
                                  comment_id[:30], revert_exc)
                    # If unhide failed, the comment is still hidden on FB but may be dismissed
                    # in DB — undismiss so the admin can see and retry it.
                    if action == 'unhide':
                        try:
                            db.undismiss_comment(comment_id)
                            log.warning("[comment-worker] unhide permanently failed for %s — "
                                        "undismissed so admin can see and retry", comment_id[:30])
                        except Exception as revert_exc:
                            log.error("[comment-worker] failed to undismiss %s: %s",
                                      comment_id[:30], revert_exc)
                elif action == 'delete':
                    try:
                        db.undismiss_comment(comment_id)
                        log.warning("[comment-worker] delete permanently failed for %s — "
                                    "undismissed so admin can see and retry",
                                    comment_id[:30])
                    except Exception as revert_exc:
                        log.error("[comment-worker] failed to undismiss %s: %s",
                                  comment_id[:30], revert_exc)
                job.failed(str(exc))


# ---------------------------------------------------------------------------
# FB API calls
# ---------------------------------------------------------------------------

def _action_hide(payload):
    import extensions
    fh = extensions.facebook_comments_handler
    if not fh:
        raise RuntimeError("facebook_comments_handler not initialised")
    comment_id = payload['comment_id']
    log.debug("[comment-worker] hiding comment %s on FB", comment_id[:30])
    fh.hide_comment(comment_id)


def _action_unhide(payload):
    import extensions
    fh = extensions.facebook_comments_handler
    if not fh:
        raise RuntimeError("facebook_comments_handler not initialised")
    comment_id = payload['comment_id']
    log.debug("[comment-worker] unhiding comment %s on FB", comment_id[:30])
    fh.unhide_comment(comment_id)


def _action_delete(payload):
    import extensions
    fh = extensions.facebook_comments_handler
    if not fh:
        raise RuntimeError("facebook_comments_handler not initialised")
    comment_id = payload['comment_id']
    log.debug("[comment-worker] deleting comment %s on FB", comment_id[:30])
    fh.delete_comment(comment_id)


# ---------------------------------------------------------------------------
# DB status write
# ---------------------------------------------------------------------------

def _write_final_status(db, comment_id: str, action: str):
    status_map = {
        'hide':   'hidden',
        'unhide': 'shown',
        'delete': 'deleted',
    }
    final = status_map.get(action)
    if final:
        db.update_comment_status(comment_id, final)
        log.debug("[comment-worker] wrote status '%s' for comment %s", final, comment_id[:30])
