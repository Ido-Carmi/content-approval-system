"""
All background / scheduled jobs.
Import and call start_scheduler() from app.py once at startup.
"""
import json
import schedule
import threading
import time
import traceback
import pytz
from datetime import datetime, timedelta

from config import load_config, save_config
import extensions

# Reference to the scheduler thread — kept here so the watchdog can restart it
_scheduler_thread = None


# ============================================================================
# MIDNIGHT SYNC
# ============================================================================

def midnight_sync_job():
    """Run at midnight Israel time: sheets sync → window update → token check → cleanup → notifications."""
    try:
        print("\n" + "=" * 80)
        print(f"🌙 MIDNIGHT SYNC STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

        import os
        os.makedirs('logs', exist_ok=True)
        with open('logs/midnight_sync.log', 'a') as f:
            f.write(f"\n{datetime.now().isoformat()} - Midnight sync triggered\n")

        # 1. Sync from Google Sheets
        if extensions.sheets_handler:
            try:
                config = load_config()
                read_from_date = config.get('read_from_date', '').strip()
                last_timestamp = None
                if read_from_date:
                    try:
                        dt = datetime.strptime(read_from_date, '%Y-%m-%d')
                        last_timestamp = dt.strftime('%d/%m/%Y 00:00:00')
                    except Exception as e:
                        print(f"⚠️  Date parse error for '{read_from_date}': {e}")

                print(f"📊 Syncing from Google Sheets (from: {last_timestamp or 'ALL'})...")
                new_entries = extensions.sheets_handler.fetch_new_entries(last_timestamp)

                added = 0
                for entry in new_entries:
                    if extensions.db.add_entry(entry['timestamp'], entry['text']):
                        added += 1

                print(f"✅ Synced {added} new entries from Google Sheets")

                israel_tz = pytz.timezone('Asia/Jerusalem')
                config['last_sync'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
                save_config(config)

            except Exception as e:
                print(f"❌ Sheets sync error: {e}")
                traceback.print_exc()
        else:
            print("⚠️ Sheets handler not initialized")

        # 2. Update dynamic posting windows based on current queue size.
        # Always recalculate: posts published throughout the day reduce the count
        # even when no new ones were approved, and the day-boundary check was
        # always zero-width (ran exactly at midnight = start of new day).
        # When the tier changes, redistribute all existing scheduled posts to the
        # new windows so the whole queue runs at the correct cadence.
        if extensions.scheduler:
            try:
                print("📊 Recalculating posting windows for new day...")
                windows_changed = extensions.scheduler.update_dynamic_windows()
                if windows_changed:
                    print("📅 Windows changed — redistributing scheduled posts...")
                    try:
                        moved = extensions.scheduler.reschedule_all_to_new_windows()
                        print(f"✅ Midnight reschedule: {moved} desired slot(s) updated — reconciler will push to FB")
                        if moved:
                            from reconciler import signal_reconciler
                            signal_reconciler()
                    except Exception as e:
                        print(f"❌ Midnight reschedule error: {e}")
                else:
                    print("✅ Windows unchanged — no reschedule needed")
            except Exception as e:
                print(f"❌ Dynamic windows update error: {e}")

        # 3. Facebook token health check
        try:
            check_facebook_token_health()
        except Exception as e:
            print(f"❌ Token health check error: {e}")

        # 4. Cleanup old entries
        try:
            print("🧹 Cleaning up old database entries...")
            deleted = extensions.db.cleanup_old_entries()
            print(f"✅ Cleaned up {deleted} old entries")
        except Exception as e:
            print(f"❌ Cleanup error: {e}")

        # 4b. Cleanup old post_tracking entries (>30 days inactive)
        try:
            extensions.db.cleanup_old_post_tracking(days=30)
        except Exception as e:
            print(f"❌ post_tracking cleanup error: {e}")

        # 5. Check and send notifications
        try:
            print("📧 Checking notifications...")
            check_and_send_notifications()
        except Exception as e:
            print(f"❌ Notification error: {e}")

        # 6. Nightly comment scan (48h lookback — catches anything webhooks missed)
        try:
            config = load_config()
            if config.get('comments_filter_enabled') and config.get('facebook_access_token') and config.get('openai_api_key'):
                print("🔍 Nightly comment scan (48h safety net)...")
                from comments_scanner import create_hourly_job
                job = create_hourly_job(extensions.db, config)
                job()
                config = load_config()
                israel_tz = pytz.timezone('Asia/Jerusalem')
                config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
                save_config(config)
                print("✅ Nightly comment scan complete")
        except Exception as e:
            print(f"❌ Nightly comment scan error: {e}")

        print("=" * 80)
        print(f"✅ MIDNIGHT SYNC COMPLETE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)

    except Exception as e:
        print(f"❌ MIDNIGHT SYNC ERROR: {e}")
        traceback.print_exc()


# ============================================================================
# FACEBOOK TOKEN HEALTH CHECK (Task 2)
# ============================================================================

def check_facebook_token_health():
    """
    Check if the Facebook access token is valid and not expiring soon.
    Uses debug_token API when App Secret is configured (gives expiry date).
    Falls back to a simple test call otherwise.
    Sends email alert if token is invalid or expiring within 7 days.
    """
    import urllib.request
    import urllib.parse

    config = load_config()
    token = config.get('facebook_access_token')
    if not token:
        print("⚠️  No Facebook token to check")
        return

    app_id = '1429459498652769'
    app_secret = config.get('facebook_app_secret')

    try:
        if app_secret:
            # Full check via Graph API debug_token
            app_access_token = f"{app_id}|{app_secret}"
            url = (
                f"https://graph.facebook.com/debug_token"
                f"?input_token={urllib.parse.quote(token)}"
                f"&access_token={urllib.parse.quote(app_access_token)}"
            )
            req = urllib.request.Request(url)
            with urllib.request.urlopen(req, timeout=10) as resp:
                data = json.loads(resp.read())

            token_data = data.get('data', {})
            is_valid = token_data.get('is_valid', False)
            expires_at = token_data.get('expires_at', 0)

            if not is_valid:
                error = token_data.get('error', {})
                print(f"🚨 Facebook token INVALID: {error.get('message', 'Unknown')}")
                _send_token_alert(config, expired=True)
                return

            if expires_at and expires_at > 0:
                expires_dt = datetime.fromtimestamp(expires_at)
                days_left = (expires_dt - datetime.now()).days
                print(f"✅ Facebook token valid — expires {expires_dt.strftime('%Y-%m-%d')} ({days_left} days)")
                if days_left <= 7:
                    _send_token_alert(config, expired=False, days_left=days_left, expires_dt=expires_dt)
            else:
                print("✅ Facebook token valid (never expires)")

        else:
            # Simple validity check — no expiry info without app secret
            url = f"https://graph.facebook.com/v18.0/me?access_token={urllib.parse.quote(token)}"
            try:
                with urllib.request.urlopen(urllib.request.Request(url), timeout=10) as resp:
                    body = json.loads(resp.read())
                if 'error' in body:
                    msg = body['error'].get('message', '')
                    print(f"🚨 Facebook token invalid! {msg}")
                    _send_token_alert(config, expired=True)
                else:
                    # Check estimated expiry from save date (FB long-lived tokens last ~60 days)
                    saved_at = config.get('facebook_token_saved_at')
                    if saved_at:
                        try:
                            saved_dt  = datetime.strptime(saved_at, '%Y-%m-%dT%H:%M:%SZ')
                            expires_dt = saved_dt + timedelta(days=60)
                            days_left  = (expires_dt - datetime.now()).days
                            if days_left <= 0:
                                print(f"🚨 Facebook token likely expired (saved {saved_at}, ~60-day limit)")
                                _send_token_alert(config, expired=True)
                            elif days_left <= 7:
                                print(f"⚠️  Facebook token expires in ~{days_left} day(s) (estimated from save date)")
                                _send_token_alert(config, expired=False, days_left=days_left, expires_dt=expires_dt)
                            else:
                                print(f"✅ Facebook token valid — estimated expiry {expires_dt.strftime('%Y-%m-%d')} (~{days_left} days)")
                        except ValueError:
                            print("✅ Facebook token valid (add App Secret in settings for exact expiry)")
                    else:
                        print("✅ Facebook token valid (save token in settings to track expiry)")
            except urllib.error.HTTPError as e:
                if e.code in (400, 401, 403):
                    print(f"🚨 Facebook token invalid! HTTP {e.code}")
                    _send_token_alert(config, expired=True)

    except Exception as e:
        print(f"❌ Token health check error: {e}")


def _send_token_alert(config, expired, days_left=None, expires_dt=None):
    """Send email alert about Facebook token expiry or invalidity.
    Writes a cooldown timestamp so callers that use _notification_on_cooldown
    won't re-alert for 24 hours."""
    notification_emails = config.get('notification_emails', [])
    if not notification_emails or not config.get('notifications_enabled'):
        return

    app_url = config.get('app_url', 'https://confessions.party')

    if expired:
        subject = "🚨 Facebook Token פג תוקף — יש לחדש מיידית"
        body = f"""<html><body style="font-family:Arial;padding:20px;direction:rtl;">
<h2 style="color:#dc3545;">🚨 Facebook Token פג תוקף</h2>
<p>ה-Access Token <strong>פג תוקף או לא תקין</strong>. האפליקציה לא תוכל לפרסם לפייסבוק.</p>
<p><a href="{app_url}/settings"
   style="background:#dc3545;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;">
   עדכן Token בהגדרות
</a></p>
</body></html>"""
    else:
        subject = f"⚠️ Facebook Token יפוג תוך {days_left} ימים"
        body = f"""<html><body style="font-family:Arial;padding:20px;direction:rtl;">
<h2 style="color:#fd7e14;">⚠️ Facebook Token עומד לפוג</h2>
<p>ה-Token יפוג בתאריך <strong>{expires_dt.strftime('%d/%m/%Y')}</strong> (עוד {days_left} ימים).</p>
<p><a href="{app_url}/settings"
   style="background:#fd7e14;color:white;padding:10px 20px;text-decoration:none;border-radius:4px;">
   חדש Token בהגדרות
</a></p>
</body></html>"""

    send_notification_email(subject, body, notification_emails)

    # Record cooldown so _notification_on_cooldown suppresses repeats for 24 h
    try:
        config['token_alert'] = datetime.now().isoformat()
        save_config(config)
    except Exception as e:
        print(f"⚠️  Could not save token alert cooldown: {e}")


# ============================================================================
# NOTIFICATIONS
# ============================================================================

def _notification_on_cooldown(config: dict, key: str) -> bool:
    """Return True if a notification was sent within the last 24 hours and the user
    hasn't acted since (i.e. reset_key is still set to the sent timestamp)."""
    last_sent = config.get(key)
    if not last_sent:
        return False
    try:
        israel_tz = pytz.timezone('Asia/Jerusalem')
        last_dt = datetime.fromisoformat(last_sent)
        if last_dt.tzinfo is None:
            last_dt = israel_tz.localize(last_dt)
        elapsed_hours = (datetime.now(pytz.utc) - last_dt.astimezone(pytz.utc)).total_seconds() / 3600
        return elapsed_hours < 24
    except Exception:
        return False


def check_and_send_notifications():
    """Check pending entries and queue; send email alerts as needed.
    Each alert fires once, then waits until the user acts in the app OR 24 hours pass."""
    config = load_config()

    if not config.get('notifications_enabled', False):
        print("⚠️ Notifications disabled in settings")
        return

    notification_emails = config.get('notification_emails', [])
    if not notification_emails:
        print("⚠️ No notification emails configured")
        return

    israel_tz = pytz.timezone('Asia/Jerusalem')
    config_dirty = False

    # Check 1: Too many pending entries
    pending_count = len(extensions.db.get_pending_entries())
    threshold = config.get('pending_threshold', 10)

    if pending_count >= threshold:
        if _notification_on_cooldown(config, 'pending_notification_last_sent'):
            print(f"⏸️  Pending notification skipped (cooldown active, {pending_count}/{threshold})")
        else:
            print(f"📧 Sending notification: {pending_count} pending entries (threshold: {threshold})")
            subject = f"⚠️ {pending_count} ערכים ממתינים לבדיקה"
            body = f"""שלום,

יש כרגע {pending_count} ערכים שממתינים לאישור במערכת.

סף ההתראה: {threshold} ערכים

כנסו למערכת לבדיקה: {config.get('app_url', 'http://localhost:5000')}

בברכה,
מערכת ניהול התוכן
"""
            send_notification_email(subject, body, notification_emails)
            config['pending_notification_last_sent'] = datetime.now(israel_tz).isoformat()
            config_dirty = True
    else:
        print(f"✅ Pending entries OK: {pending_count}/{threshold}")
        if config.get('pending_notification_last_sent'):
            config.pop('pending_notification_last_sent', None)
            config_dirty = True

    # Check 2: Queue running out — last scheduled post is within 24 hours
    # (Checking the *last* post time is robust: it doesn't fire false positives
    #  when the tier changes and new "empty" canonical slots appear on existing days.)
    if extensions.scheduler:
        try:
            scheduled_times = extensions.scheduler.get_scheduled_times_from_facebook()
            now = datetime.now(extensions.scheduler.timezone)

            if not scheduled_times:
                hours_until_empty = 0.0
                last_post_str = "אין פוסטים מתוזמנים"
            else:
                last_post = max(
                    t.astimezone(extensions.scheduler.timezone) for t in scheduled_times
                )
                hours_until_empty = (last_post - now).total_seconds() / 3600
                last_post_str = last_post.strftime('%d/%m/%Y %H:%M')

            if hours_until_empty < 24:
                if _notification_on_cooldown(config, 'queue_notification_last_sent'):
                    print(f"⏸️  Queue notification skipped (cooldown active, last post: {last_post_str})")
                else:
                    print(f"📧 Sending notification: Queue runs out in {hours_until_empty:.1f} hours (last post: {last_post_str})")
                    subject = "⏰ התור לפרסום מסתיים בעוד פחות מ-24 שעות"
                    body = f"""שלום,

התור לפרסום עומד להיגמר — הפוסט האחרון המתוזמן הוא בעוד {hours_until_empty:.1f} שעות:

תאריך ושעה: {last_post_str}

אם יש ערכים ממתינים, כדאי לאשר אותם כדי שלא תהיה הפסקה בפרסום.

כנסו למערכת: {config.get('app_url', 'http://localhost:5000')}

בברכה,
מערכת ניהול התוכן
"""
                    send_notification_email(subject, body, notification_emails)
                    config['queue_notification_last_sent'] = datetime.now(israel_tz).isoformat()
                    config_dirty = True
            else:
                print(f"✅ Queue OK: {len(scheduled_times)} posts, last at {last_post_str} (in {hours_until_empty:.1f}h)")
                if config.get('queue_notification_last_sent'):
                    config.pop('queue_notification_last_sent', None)
                    config_dirty = True
        except Exception as e:
            print(f"❌ Error checking queue: {e}")
    else:
        print("⚠️ Scheduler not initialized")

    if config_dirty:
        save_config(config)


def send_notification_email(subject, body, recipients):
    """Send email via Resend API."""
    config = load_config()
    api_key = config.get('resend_api_key')
    from_email = config.get('resend_from_email')

    if not api_key or not from_email:
        print("❌ Resend not configured (missing api_key or from_email)")
        return

    try:
        import resend
        resend.api_key = api_key
        result = resend.Emails.send({
            "from": from_email,
            "to": recipients,
            "subject": subject,
            "html": body,
        })
        print(f"✅ Email sent via Resend to: {', '.join(recipients)} (id: {result.get('id', '?')})")
    except Exception as e:
        print(f"❌ Resend email error: {e}")
        traceback.print_exc()


def check_comment_notification():
    """
    Send email when comment counts exceed configured thresholds.
    Two independent thresholds — each sends once per spike and resets when count drops.
      - comment_flagged_notification_threshold: flagged (hidden) comments only
      - comment_notification_threshold:         all undismissed comments (flagged + clean)
    """
    try:
        config = load_config()

        if not config.get('notifications_enabled', False):
            return

        emails = config.get('notification_emails', [])
        if not emails:
            return

        app_url = config.get('app_url', '')
        changed = False

        # --- Threshold 1: flagged comments only ---
        flagged_threshold = config.get('comment_flagged_notification_threshold', 0)
        if flagged_threshold > 0:
            flagged_count = extensions.db.get_unreviewed_flagged_count()
            if flagged_count >= flagged_threshold:
                if not config.get('comment_flagged_notification_sent', False):
                    subject = f"🚨 {flagged_count} תגובות מסומנות ממתינות לבדיקה"
                    body = f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px; direction: rtl;">
    <h2 style="color: #dc3545;">🚨 תגובות מסומנות ממתינות לבדיקה</h2>
    <p>יש <strong>{flagged_count} תגובות מסומנות</strong> (פוליטי / שנאה / ספאם) שממתינות לבדיקה (סף: {flagged_threshold}).</p>
    <p style="margin-top: 30px;">
        <a href="{app_url}/comments?filter=hidden"
           style="background-color: #dc3545; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 5px; display: inline-block;">
            צפה בתגובות המסומנות
        </a>
    </p>
</body>
</html>"""
                    send_notification_email(subject, body, emails)
                    config['comment_flagged_notification_sent'] = True
                    changed = True
                    print(f"📧 Flagged comment notification sent ({flagged_count} >= {flagged_threshold})")
            else:
                if config.get('comment_flagged_notification_sent', False):
                    config['comment_flagged_notification_sent'] = False
                    changed = True
                    print(f"✅ Flagged count ({flagged_count}) below threshold ({flagged_threshold}), notification reset")

        # --- Threshold 2: all undismissed comments ---
        total_threshold = config.get('comment_notification_threshold', 0)
        if total_threshold > 0:
            total_count = extensions.db.get_unreviewed_comment_count()
            if total_count >= total_threshold:
                if not config.get('comment_notification_sent', False):
                    subject = f"🗨️ {total_count} תגובות ממתינות לבדיקה"
                    body = f"""<html>
<body style="font-family: Arial, sans-serif; padding: 20px; direction: rtl;">
    <h2 style="color: #fd7e14;">🗨️ תגובות ממתינות לבדיקה</h2>
    <p>יש <strong>{total_count} תגובות</strong> (כולל תקינות) שממתינות לבדיקה (סף: {total_threshold}).</p>
    <p style="margin-top: 30px;">
        <a href="{app_url}/comments"
           style="background-color: #007bff; color: white; padding: 12px 24px;
                  text-decoration: none; border-radius: 5px; display: inline-block;">
            צפה בתגובות
        </a>
    </p>
</body>
</html>"""
                    send_notification_email(subject, body, emails)
                    config['comment_notification_sent'] = True
                    changed = True
                    print(f"📧 Total comment notification sent ({total_count} >= {total_threshold})")
            else:
                if config.get('comment_notification_sent', False):
                    config['comment_notification_sent'] = False
                    changed = True
                    print(f"✅ Total count ({total_count}) below threshold ({total_threshold}), notification reset")

        if changed:
            save_config(config)

    except Exception as e:
        print(f"❌ Comment notification check error: {e}")


# ============================================================================
# COMMENT SCAN JOB
# ============================================================================

def comments_scan_job():
    """Scan and filter comments (runs if enabled)."""
    try:
        config = load_config()

        if not config.get('comments_filter_enabled', False):
            print("ℹ️  Comments filter disabled in settings")
            return

        if not config.get('openai_api_key') or not config.get('facebook_access_token'):
            print("⚠️  Missing API keys for comments filter")
            return

        print(f"\n{'='*60}\n🔍 Starting comments scan\n{'='*60}")

        from comments_scanner import create_hourly_job
        job = create_hourly_job(extensions.db, config)
        job()

        config = load_config()
        israel_tz = pytz.timezone('Asia/Jerusalem')
        config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
        save_config(config)

        check_comment_notification()

    except ImportError as e:
        print(f"⚠️  Comments scanner not available: {e}")
    except Exception as e:
        print(f"❌ Comments scan error: {e}")
        traceback.print_exc()


def auto_comment_job():
    """Post a comment on each entry whose scheduled_time passed 2+ min ago (slot 1/2/3).
    Each slot has a group of texts that rotate round-robin across posts."""
    try:
        if not extensions.facebook_handler:
            return

        pending = extensions.db.get_pending_auto_comments()
        if not pending:
            return

        config = load_config()
        config_dirty = False

        print(f"\n💬 Auto-comment job: {len(pending)} post(s) to comment on")
        for entry in pending:
            fb_id        = entry.get('facebook_post_id')
            should_mask  = int(entry.get('should_comment', 0))
            posted_mask  = int(entry.get('comment_posted', 0))
            pending_mask = should_mask & ~posted_mask
            if not fb_id or not pending_mask:
                continue
            for slot in [1, 2, 3]:
                slot_bit = 1 << (slot - 1)   # 1, 2, 4
                if not (pending_mask & slot_bit):
                    continue

                # Build the active text list for this slot (new group format, with old fallback)
                group = [t.strip() for t in config.get(f'auto_comment_group_{slot}', []) if t.strip()]
                if not group:
                    old = config.get(f'auto_comment_text_{slot}', '').strip()
                    if old:
                        group = [old]

                if not group:
                    continue

                idx = config.get(f'auto_comment_group_{slot}_index', 0) % len(group)
                comment_text = group[idx]

                try:
                    extensions.facebook_handler.post_comment(fb_id, comment_text)
                    extensions.db.mark_comment_posted(entry['id'], slot_bit)

                    # Advance rotation index only when there are multiple texts
                    if len(group) > 1:
                        config[f'auto_comment_group_{slot}_index'] = (idx + 1) % len(group)
                        config_dirty = True

                    print(f"   ✓ #{entry.get('post_number')} slot {slot} posted (text {idx + 1}/{len(group)})")
                except Exception as e:
                    err_str = str(e)
                    print(f"   ✗ #{entry.get('post_number')} slot {slot} failed: {err_str}")
                    if '"code":190' in err_str or 'OAuthException' in err_str or 'Cannot parse access token' in err_str:
                        if not _notification_on_cooldown(config, 'token_alert'):
                            print("🚨 Token invalid — sending alert")
                            _send_token_alert(config, expired=True)
                    elif 'does not exist' in err_str or '"error_subcode":33' in err_str or '"code":100' in err_str:
                        # Post was deleted/published — mark slot done so we stop retrying
                        extensions.db.mark_comment_posted(entry['id'], slot_bit)
                        print(f"   ⚠️  Post no longer exists — marking slot {slot} as done")

        if config_dirty:
            save_config(config)

    except Exception as e:
        print(f"❌ Auto-comment job error: {e}")


def cleanup_old_comments_job():
    """Delete comments older than comment_retention_days."""
    try:
        config = load_config()
        days = config.get('comment_retention_days', 7)
        print(f"\n{'='*60}\n🧹 Cleaning up old comments (>{days} days)\n{'='*60}")
        deleted = extensions.db.cleanup_old_comments(days=days)
        print(f"{'✅' if deleted > 0 else 'ℹ️ '} Cleaned up {deleted} old comments")
    except Exception as e:
        print(f"❌ Cleanup error: {e}")
        traceback.print_exc()


# ============================================================================
# SCHEDULER THREAD + WATCHDOG (Task 3)
# ============================================================================

def run_scheduler():
    """Background thread: runs pending scheduled jobs every minute."""
    print("🔄 Scheduler thread started")
    while True:
        try:
            if datetime.now().minute == 0:
                jobs = schedule.get_jobs()
                print(f"\n⏰ [{datetime.now().strftime('%H:%M')}] Scheduler: {len(jobs)} jobs pending")
                for job in jobs:
                    print(f"   - {job}")
            schedule.run_pending()
            time.sleep(60)
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            traceback.print_exc()
            time.sleep(60)


def _watchdog():
    """Monitor scheduler thread; restart it (and send alert) if it dies."""
    global _scheduler_thread
    time.sleep(120)  # Grace period after startup
    while True:
        try:
            if _scheduler_thread and not _scheduler_thread.is_alive():
                print("🚨 WATCHDOG: Scheduler thread died — restarting...")
                _scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
                _scheduler_thread.start()
                print("✅ WATCHDOG: Scheduler thread restarted")
                try:
                    config = load_config()
                    if config.get('notifications_enabled') and config.get('notification_emails'):
                        send_notification_email(
                            "⚠️ Scheduler Thread Restarted",
                            "<p>The background scheduler crashed and was automatically restarted.</p>",
                            config['notification_emails']
                        )
                except Exception:
                    pass
        except Exception as e:
            print(f"❌ Watchdog error: {e}")
        time.sleep(120)


def instagram_daily_job(force_all: bool = False):
    """
    Daily Instagram job.
    force_all=True: ignore date window, pick from ALL available unposted entries (for testing).
    """
    print(f"\n{'='*60}")
    print(f"📸 INSTAGRAM DAILY JOB STARTED")
    print(f"{'='*60}")

    try:
        # ── Config check ────────────────────────────────────────────────
        print(f"\n[ig-job] STEP 1: Config check")
        config = load_config()
        print(f"[ig-job]   instagram_enabled    = {config.get('instagram_enabled', False)}")
        print(f"[ig-job]   instagram_ig_account = {config.get('instagram_ig_account_id', 'NOT SET')}")
        print(f"[ig-job]   instagram_watermark  = {config.get('instagram_watermark', 'NOT SET')}")
        print(f"[ig-job]   instagram_post_time  = {config.get('instagram_post_time', 'NOT SET')}")
        print(f"[ig-job]   instagram_hashtags   = {config.get('instagram_hashtags', '')[:60]}")
        print(f"[ig-job]   cloudinary_cloud     = {config.get('cloudinary_cloud_name', 'NOT SET')}")
        print(f"[ig-job]   fb_token present     = {bool(config.get('facebook_access_token'))}")

        if not config.get('instagram_enabled', False):
            print(f"[ig-job] ⚠️  instagram_enabled is False — skipping")
            return

        if not extensions.instagram_handler:
            print(f"[ig-job] ❌ instagram_handler is None — missing config keys?")
            print(f"[ig-job]    required: instagram_ig_account_id, cloudinary_cloud_name, "
                  f"cloudinary_api_key, cloudinary_api_secret, facebook_access_token")
            return

        print(f"[ig-job] ✅ config OK, handler is ready")

        # ── Date window ──────────────────────────────────────────────────
        print(f"\n[ig-job] STEP 2: Date window")
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now       = datetime.now(israel_tz)
        weekday   = now.weekday()   # 0=Mon … 6=Sun
        DAY_NAMES = ['Monday','Tuesday','Wednesday','Thursday','Friday','Saturday','Sunday']
        print(f"[ig-job]   current Israel time = {now.strftime('%Y-%m-%d %H:%M:%S %Z')}")
        print(f"[ig-job]   weekday = {weekday} ({DAY_NAMES[weekday]})")
        print(f"[ig-job]   force_all = {force_all}")

        if force_all:
            start_iso = '2000-01-01T00:00:00+00:00'
            end_iso   = now.isoformat()
            print(f"[ig-job]   FORCE MODE: picking from all available posts")
        elif weekday == 6:          # Sunday → cover Thu+Fri+Sat
            end_dt   = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_dt = end_dt - timedelta(days=3)
            start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
            print(f"[ig-job]   Sunday mode: window = {start_dt.date()} (Thu) → {end_dt.date()} (Sun)")
        else:
            end_dt   = now.replace(hour=0, minute=0, second=0, microsecond=0)
            start_dt = end_dt - timedelta(days=1)
            start_iso, end_iso = start_dt.isoformat(), end_dt.isoformat()
            print(f"[ig-job]   Daily mode: window = {start_dt.date()} → {end_dt.date()}")

        print(f"[ig-job]   start_iso = {start_iso}")
        print(f"[ig-job]   end_iso   = {end_iso}")

        # ── Fetch candidates ─────────────────────────────────────────────
        print(f"\n[ig-job] STEP 3: Fetch candidates from instagram_post_log")
        candidates = extensions.db.get_best_post_for_instagram(start_iso, end_iso)
        print(f"[ig-job]   found {len(candidates)} candidate(s) (ig_posted=0 in window)")

        for i, c in enumerate(candidates):
            print(f"[ig-job]   [{i}] id={c['id']} post=#{c['post_number']} "
                  f"fb_post_id={c['fb_post_id']} "
                  f"comment_count={c['comment_count']} "
                  f"published_at={c['published_at']}")

        if not candidates:
            print(f"[ig-job] ℹ️  no candidates — nothing to post")
            return

        # ── Score candidates ─────────────────────────────────────────────
        print(f"\n[ig-job] STEP 4: Score candidates (reactions + 2×comments)")
        for c in candidates:
            print(f"[ig-job]   fetching reactions for fb_post_id={c['fb_post_id']}")
            reactions = 0
            if extensions.facebook_handler:
                try:
                    reactions = extensions.facebook_handler.get_post_reactions_count(c['fb_post_id'])
                    print(f"[ig-job]     reactions = {reactions}")
                except Exception as re:
                    print(f"[ig-job]     ⚠️  reactions fetch failed: {re} — using 0")
            else:
                print(f"[ig-job]     ⚠️  facebook_handler not available — reactions = 0")
            c['reactions'] = reactions
            c['score']     = reactions + c['comment_count'] * 2
            print(f"[ig-job]   post #{c['post_number']}: reactions={reactions}, "
                  f"comments={c['comment_count']}, score={c['score']}")

        best = max(candidates, key=lambda x: x['score'])
        print(f"\n[ig-job]   ✅ best post: #{best['post_number']} "
              f"(id={best['id']}, score={best['score']})")
        print(f"[ig-job]   raw text: {best['text'][:120]}")

        # Strip the "#number\n" prefix that post_tracking stores as part of the text
        raw_text = best['text'] or ''
        if raw_text.startswith('#'):
            lines = raw_text.split('\n', 1)
            clean_text = lines[1].strip() if len(lines) > 1 else raw_text
        else:
            clean_text = raw_text.strip()
        print(f"[ig-job]   clean text ({len(clean_text)} chars): {clean_text[:120]}")

        # ── Image generation ─────────────────────────────────────────────
        print(f"\n[ig-job] STEP 5: Generate image slides")
        from image_generator import generate_confession_slides, slides_to_bytes
        watermark = config.get('instagram_watermark', 'וידויים צבאיים')
        print(f"[ig-job]   watermark = '{watermark}'")

        slides = generate_confession_slides(
            text=clean_text,
            post_number=best['post_number'],
            watermark=watermark,
        )
        print(f"[ig-job]   ✅ {len(slides)} slide(s) generated")

        images_bytes = slides_to_bytes(slides)
        total_kb = sum(len(b) for b in images_bytes) // 1024
        print(f"[ig-job]   total size: {total_kb} KB across {len(images_bytes)} image(s)")

        # ── Caption ──────────────────────────────────────────────────────
        print(f"\n[ig-job] STEP 6: Build caption")
        caption  = f"#{best['post_number']}\n{clean_text}"
        hashtags = config.get('instagram_hashtags', '').strip()
        if hashtags:
            caption += f"\n.\n.\n{hashtags}"
            print(f"[ig-job]   hashtags added: {hashtags[:60]}")
        print(f"[ig-job]   caption total length: {len(caption)} chars")
        print(f"[ig-job]   caption preview:\n---\n{caption[:200]}\n---")

        # ── Publish ──────────────────────────────────────────────────────
        print(f"\n[ig-job] STEP 7: Publish to Instagram")
        public_id = f"confessions/post_{best['post_number']}_{int(now.timestamp())}"
        print(f"[ig-job]   public_id = {public_id}")

        result = extensions.instagram_handler.publish_post(
            images_bytes=images_bytes,
            caption=caption,
            base_public_id=public_id,
        )

        # ── Mark as posted ───────────────────────────────────────────────
        print(f"\n[ig-job] STEP 8: Mark log entry {best['id']} as ig_posted")
        extensions.db.mark_ig_posted(best['id'])
        print(f"[ig-job]   ✅ marked")

        # ── Save last-post timestamp ─────────────────────────────────────
        cfg = load_config()
        cfg.pop('instagram_failure_count', None)
        cfg['instagram_last_post'] = now.strftime('%d/%m/%Y %H:%M')
        save_config(cfg)
        print(f"[ig-job]   saved instagram_last_post = {cfg['instagram_last_post']}")

        print(f"\n{'='*60}")
        print(f"✅ INSTAGRAM JOB COMPLETE")
        print(f"   ig_post_id = {result['ig_post_id']}")
        print(f"   slides     = {result['slides']}")
        print(f"{'='*60}\n")

    except Exception as e:
        print(f"\n{'='*60}")
        print(f"❌ INSTAGRAM JOB FAILED: {e}")
        print(f"{'='*60}")
        traceback.print_exc()
        try:
            cfg   = load_config()
            count = cfg.get('instagram_failure_count', 0) + 1
            cfg['instagram_failure_count'] = count
            save_config(cfg)
            if count >= 3 and cfg.get('notifications_enabled') and cfg.get('notification_emails'):
                send_notification_email(
                    f"🚨 Instagram פוסט יומי נכשל {count} פעמים ברצף",
                    f"<html><body style='direction:rtl;font-family:Arial;padding:20px'>"
                    f"<h3>שגיאה בפרסום ל-Instagram</h3>"
                    f"<p><code>{e}</code></p>"
                    f"<p>בדוק הגדרות Instagram ו-Cloudinary בהגדרות המערכת.</p>"
                    f"</body></html>",
                    cfg['notification_emails'],
                )
        except Exception:
            pass


def start_scheduler():
    """Set up scheduled jobs and start the scheduler + watchdog threads."""
    global _scheduler_thread
    import logging

    # Wire up Python logging so reconciler / queue_workers messages appear in journald
    logging.basicConfig(
        level=logging.INFO,
        format='%(asctime)s %(levelname)s %(name)s: %(message)s',
        datefmt='%H:%M:%S',
    )

    israel_tz = pytz.timezone('Asia/Jerusalem')
    now_utc = datetime.now(pytz.utc)
    now_israel = now_utc.astimezone(israel_tz)
    server_now = datetime.now()
    offset = (now_israel.hour - server_now.hour) % 24

    print(f"🕐 Timezone info:")
    print(f"   Server time: {server_now.strftime('%H:%M')}")
    print(f"   Israel time: {now_israel.strftime('%H:%M')}")
    print(f"   Offset: {offset} hours")

    midnight_local = (24 - offset) % 24
    schedule.every().day.at(f"{midnight_local:02d}:00").do(midnight_sync_job)

    cleanup_local = (2 + 24 - offset) % 24
    schedule.every().day.at(f"{cleanup_local:02d}:00").do(cleanup_old_comments_job)

    schedule.every(6).hours.do(check_and_send_notifications)
    schedule.every(1).minutes.do(auto_comment_job)

    ig_cfg = load_config()
    ig_time = ig_cfg.get('instagram_post_time', '12:00')
    try:
        ig_h, ig_m = map(int, ig_time.split(':'))
        ig_local_h = (ig_h - offset) % 24
        schedule.every().day.at(f"{ig_local_h:02d}:{ig_m:02d}").do(instagram_daily_job)
        print(f"   - Instagram daily job at {ig_time} Israel time ({ig_local_h:02d}:{ig_m:02d} server time)")
    except Exception as e:
        print(f"⚠️  Instagram schedule error: {e}")

    # ── Reconciler thread ────────────────────────────────────────────────────
    from reconciler import reconciler_loop
    threading.Thread(target=reconciler_loop, daemon=True, name="reconciler").start()
    print("✅ Reconciler thread started")

    # ── Comment action worker thread ─────────────────────────────────────────
    # Reload any jobs that were pending before last restart
    extensions.comment_queue.reload_pending()

    from queue_workers import comment_worker_loop
    threading.Thread(target=comment_worker_loop, daemon=True, name="comment-worker").start()
    print("✅ Comment worker thread started")

    # Startup comment scan
    def startup_comment_scan():
        try:
            config = load_config()
            if not config.get('comments_filter_enabled') or not config.get('facebook_access_token'):
                return
            print(f"📡 Startup scan: last 48 hours")
            from comments_scanner import create_hourly_job
            job = create_hourly_job(extensions.db, config)
            job()
            config = load_config()
            config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
            save_config(config)
            print("✅ Startup comment scan completed")
        except Exception as e:
            print(f"⚠️  Startup comment scan failed: {e}")

    def delayed_startup():
        time.sleep(5)
        startup_comment_scan()

    threading.Thread(target=delayed_startup, daemon=True).start()

    _scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    _scheduler_thread.start()

    threading.Thread(target=_watchdog, daemon=True).start()

    print("=" * 80)
    print("✅ Background scheduler started:")
    print(f"   - Midnight sync + nightly comment scan (48h) at {midnight_local:02d}:00 server time (00:00 Israel)")
    print(f"   - Auto-comment job every 1 minute")
    print(f"   - Notifications check every 6 hours")
    print(f"   - Old comments cleanup at {cleanup_local:02d}:00 server time (02:00 Israel)")
    print(f"   - Reconciler loop running (30s safety net + on-demand wake)")
    print(f"   - Comment worker loop running (serial FB API calls)")
    print(f"   - Startup comment scan triggered")
    print(f"   - Scheduler thread running with watchdog")
    print("=" * 80)
