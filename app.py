from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session, Response
import threading
import traceback
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta, timezone
import pytz
from werkzeug.security import generate_password_hash, check_password_hash

from config import load_config, save_config
from database import Database
import extensions
from extensions import init_handlers
from utils import calculate_textarea_height, get_hebrew_weekday
import routes_comments

app = Flask(__name__)

# ============================================================================
# APP SETUP
# ============================================================================

os.makedirs('logs', exist_ok=True)
_file_handler = RotatingFileHandler('logs/app.log', maxBytes=5 * 1024 * 1024, backupCount=3)
_file_handler.setLevel(logging.WARNING)
_file_handler.setFormatter(logging.Formatter(
    '%(asctime)s %(levelname)s: %(message)s [in %(pathname)s:%(lineno)d]'
))
app.logger.addHandler(_file_handler)
app.logger.setLevel(logging.WARNING)

def _get_or_create_secret_key():
    config = load_config()
    key = config.get('flask_secret_key')
    if not key:
        key = os.urandom(24).hex()
        config['flask_secret_key'] = key
        save_config(config)
    return key

app.secret_key = _get_or_create_secret_key()
app.permanent_session_lifetime = timedelta(days=30)
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

import time

# Initialise handlers from config
init_handlers()

# Register comment + webhook routes
routes_comments.register(app)

@app.route('/favicon.ico')
def favicon():
    return '', 204

@app.route('/health')
def health_check():
    try:
        conn = extensions.db.get_connection()
        conn.execute('SELECT 1')
        conn.close()
        return jsonify({'status': 'ok'}), 200
    except Exception as e:
        return jsonify({'status': 'error', 'detail': str(e)}), 500

# ============================================================================
# AUTHENTICATION
# ============================================================================

PUBLIC_ROUTES = {'login', 'setup_password', 'webhook_verify', 'webhook_receive', 'favicon', 'static', 'health_check'}

def _get_client_ip():
    """Get real client IP — nginx sets X-Real-IP after Cloudflare processing."""
    return request.headers.get('X-Real-IP') or request.remote_addr

@app.before_request
def require_login():
    if request.endpoint in PUBLIC_ROUTES:
        return
    if not session.get('logged_in'):
        return redirect(url_for('login'))
    # Session IP binding — reject sessions that moved to a different IP
    if session.get('login_ip') and session['login_ip'] != _get_client_ip():
        session.clear()
        flash('פג תוקף הסשן (שינוי כתובת IP). אנא התחבר מחדש.', 'error')
        return redirect(url_for('login'))

@app.route('/login', methods=['GET', 'POST'])
def login():
    config = load_config()
    if not config.get('admin_password_hash'):
        return redirect(url_for('setup_password'))

    # Brute-force lockout check
    locked_until_str = config.get('login_locked_until')
    if locked_until_str:
        locked_until = datetime.fromisoformat(locked_until_str)
        if datetime.now() < locked_until:
            remaining = int((locked_until - datetime.now()).total_seconds() / 60) + 1
            flash(f'חשבון נעול עקב ניסיונות כושלים. נסה שוב בעוד {remaining} דקות.', 'error')
            return render_template('login.html')
        else:
            config['login_locked_until'] = None
            config['login_attempts'] = 0
            save_config(config)

    if request.method == 'POST':
        password = request.form.get('password', '')
        remember = request.form.get('remember') == 'on'
        if check_password_hash(config['admin_password_hash'], password):
            config['login_attempts'] = 0
            config['login_locked_until'] = None
            save_config(config)
            session['logged_in'] = True
            session['login_ip'] = _get_client_ip()
            if remember:
                session.permanent = True
            return redirect(url_for('review_page'))
        # Failed attempt
        attempts = config.get('login_attempts', 0) + 1
        config['login_attempts'] = attempts
        if attempts >= 10:
            config['login_locked_until'] = (datetime.now() + timedelta(minutes=15)).isoformat()
            config['login_attempts'] = 0
            save_config(config)
            flash('יותר מדי ניסיונות כושלים. החשבון נעול למשך 15 דקות.', 'error')
        else:
            save_config(config)
            flash(f'סיסמה שגויה ({attempts}/10)', 'error')
    return render_template('login.html')

@app.route('/logout')
def logout():
    session.clear()
    return redirect(url_for('login'))

@app.route('/setup-password', methods=['GET', 'POST'])
def setup_password():
    config = load_config()
    if config.get('admin_password_hash') and not session.get('logged_in'):
        return redirect(url_for('login'))
    if request.method == 'POST':
        password = request.form.get('password', '')
        confirm = request.form.get('confirm', '')
        if len(password) < 4:
            flash('סיסמה חייבת להיות לפחות 4 תווים', 'error')
        elif password != confirm:
            flash('הסיסמאות לא תואמות', 'error')
        else:
            config['admin_password_hash'] = generate_password_hash(password)
            save_config(config)
            session['logged_in'] = True
            session['login_ip'] = _get_client_ip()
            session.permanent = True
            flash('✅ סיסמה נקבעה בהצלחה', 'success')
            return redirect(url_for('review_page'))
    return render_template('setup_password.html', is_change=bool(config.get('admin_password_hash')))

# ============================================================================
# ROUTES - Review Page
# ============================================================================

@app.route('/')
@app.route('/review')
def review_page():
    print("=" * 50)
    print("REVIEW PAGE - DEBUG START")
    print("=" * 50)

    try:
        print("Step 1: Cleaning up old denied entries...")
        extensions.db.cleanup_old_denied()
        print("✓ Cleanup complete")
    except Exception as e:
        print(f"✗ Cleanup failed: {e}")

    try:
        print("Step 2: Getting pending entries...")
        entries = extensions.db.get_pending_entries()
        print(f"✓ Got {len(entries) if entries else 0} entries")
    except Exception as e:
        print(f"✗ Getting entries failed: {e}")
        entries = []

    try:
        print("Step 3: Calculating heights...")
        for entry in entries:
            entry['height'] = calculate_textarea_height(entry['text'])
        print(f"✓ Heights calculated for {len(entries)} entries")
    except Exception as e:
        print(f"✗ Height calculation failed: {e}")

    try:
        print("Step 4: Loading config...")
        config = load_config()
        print(f"✓ Config loaded, last sync: {config.get('last_sync', 'Never')}")
        # User is active on review page — reset pending notification cooldown
        if config.pop('pending_notification_last_sent', None) is not None:
            save_config(config)
    except Exception as e:
        print(f"✗ Config loading failed: {e}")
        config = {}

    print("=" * 50)
    print("REVIEW PAGE - DEBUG END")
    print("=" * 50)

    try:
        return render_template('review.html', entries=entries, config=config)
    except Exception as e:
        print(f"✗ Template rendering failed: {e}")
        traceback.print_exc()
        return f"Error rendering template: {e}", 500

@app.route('/approve/<int:entry_id>', methods=['POST'])
def approve_entry(entry_id):
    from reconciler import signal_reconciler
    json_body = request.get_json(silent=True) or {}
    edited_text = request.form.get('text', '') or json_body.get('text', '')
    comment_bitmask = int(request.form.get('comment_bitmask', 0) or json_body.get('comment_bitmask', 0) or 0)

    # Pick a slot using local data only (no Facebook query — fast + race-safe).
    # Combine desired (scheduled_time) and actual FB (fb_scheduled_time) slots so
    # that slots freed in desired-state but still live on FB are not double-booked
    # (e.g. after reschedule_canonical runs before reconciler finishes syncing).
    slot_iso = None
    if extensions.scheduler:
        try:
            desired  = set(extensions.db.get_desired_slots())
            fb_live  = set(extensions.db.get_taken_fb_slots())
            taken    = list(desired | fb_live)
            print(f"[approve] slot-pick: {len(desired)} desired + {len(fb_live)} fb_live = {len(taken)} taken")
            print(f"[approve]   desired slots: {sorted(desired)}")
            print(f"[approve]   fb_live slots: {sorted(fb_live)}")
            slot_dt  = extensions.scheduler.get_next_available_slot_local(taken)
            slot_iso = slot_dt.isoformat()
            print(f"[approve]   → chose {slot_iso}")
        except Exception as e:
            print(f"Slot picking error: {e}")

    approved = extensions.db.approve_entry(entry_id, edited_text, 'admin', scheduled_time=slot_iso)
    if not approved:
        # Already approved/denied (double-submit) — return quietly
        if request.headers.get('HX-Request'):
            return '', 200
        return redirect(url_for('review_page'))

    if comment_bitmask > 0:
        extensions.db.set_should_comment(entry_id, comment_bitmask)

    print(f"[approve] entry {entry_id} marked scheduled at {slot_iso} — reconciler will push to FB")
    signal_reconciler()

    if request.headers.get('HX-Request'):
        return '', 200
    return redirect(url_for('review_page'))

@app.route('/deny/<int:entry_id>', methods=['POST'])
def deny_entry(entry_id):
    from reconciler import signal_reconciler
    # Try pending first; if entry is scheduled use deny_scheduled_entry (cascade + signal)
    denied = extensions.db.deny_entry(entry_id, 'admin')
    if not denied:
        result = extensions.db.deny_scheduled_entry(entry_id)
        if 'error' not in result:
            print(f"[deny] scheduled entry {entry_id} denied, freed #{result.get('freed_number')} — reconciler will delete from FB")
            signal_reconciler()
        else:
            print(f"[deny] entry {entry_id} could not be denied: {result}")
    if request.headers.get('HX-Request'):
        return '', 200
    return redirect(url_for('review_page'))


@app.route('/toggle-comment/<int:entry_id>', methods=['POST'])
def toggle_comment(entry_id):
    data = request.get_json(silent=True) or {}
    bitmask = int(data.get('bitmask', 0))
    extensions.db.set_should_comment(entry_id, bitmask)
    return jsonify({'ok': True, 'bitmask': bitmask})

@app.route('/remove-auto-comment/<int:entry_id>', methods=['POST'])
def remove_auto_comment(entry_id):
    extensions.db.remove_auto_comment(entry_id)
    return jsonify({'ok': True})

@app.route('/auto-comment')
def auto_comment_page():
    config = load_config()
    groups = []
    for i in range(1, 4):
        group = config.get(f'auto_comment_group_{i}')
        if group is None:
            # Migrate from old single-text format on first load
            old = config.get(f'auto_comment_text_{i}', '').strip()
            group = [old] if old else []
        groups.append(group)
    indices = [config.get(f'auto_comment_group_{i}_index', 0) for i in range(1, 4)]

    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    cursor.execute('''
        SELECT id, post_number, text, scheduled_time, should_comment, comment_posted
        FROM entries
        WHERE should_comment > 0
        ORDER BY scheduled_time ASC
    ''')
    pending = [dict(row) for row in cursor.fetchall()]
    conn.close()

    return render_template('auto_comment.html', groups=groups, indices=indices, pending=pending)

@app.route('/auto-comment/save', methods=['POST'])
def auto_comment_save():
    config = load_config()
    for i in range(1, 4):
        raw = request.form.getlist(f'group_{i}[]')
        texts = [t.strip() for t in raw if t.strip()]
        config[f'auto_comment_group_{i}'] = texts
        # Clamp rotation index to new group size
        old_idx = config.get(f'auto_comment_group_{i}_index', 0)
        if texts and old_idx >= len(texts):
            config[f'auto_comment_group_{i}_index'] = 0
    save_config(config)
    flash('התגובות האוטומטיות נשמרו', 'success')
    return redirect(url_for('auto_comment_page'))

@app.route('/sync', methods=['POST'])
def sync_now():
    if not extensions.sheets_handler:
        flash('❌ Google Sheets לא מחובר', 'error')
        return redirect(url_for('review_page'))
    try:
        config = load_config()
        read_from_date = config.get('read_from_date', '').strip()
        last_timestamp = None
        if read_from_date:
            try:
                dt = datetime.strptime(read_from_date, '%Y-%m-%d')
                last_timestamp = dt.strftime('%d/%m/%Y 00:00:00')
                print(f"DEBUG: Filtering from date: {last_timestamp}")
            except Exception as e:
                print(f"DEBUG: Date parse error: {e}")
                flash('⚠️ פורמט תאריך שגוי', 'warning')
        else:
            print("DEBUG: No date filter - fetching ALL entries")

        new_entries = extensions.sheets_handler.fetch_new_entries(last_timestamp or '')
        added_count = 0
        for entry in new_entries:
            if extensions.db.add_entry(entry['timestamp'], entry['text']):
                added_count += 1

        israel_tz = pytz.timezone('Asia/Jerusalem')
        config['last_sync'] = datetime.now(israel_tz).strftime("%Y-%m-%d %H:%M:%S")
        save_config(config)

        flash(f'✅ סונכרן! נוספו {added_count} ערכים חדשים', 'success')
    except Exception as e:
        print(f"DEBUG: Sync error: {e}")
        traceback.print_exc()
        flash(f'❌ סנכרון נכשל: {str(e)}', 'error')
    return redirect(url_for('review_page'))

# ============================================================================
# ROUTES - Scheduled Posts
# ============================================================================

@app.route('/scheduled')
def scheduled_page():
    return render_template('scheduled.html', posts=None, had_orphans=False, loading=True)


@app.route('/reconciler-status')
def reconciler_status():
    """Return whether the reconciler has any pending work (entries needing FB sync)."""
    from reconciler import _times_differ, text_hash, format_post_text
    db = extensions.db

    if db.get_entries_needing_fb_delete():
        return jsonify({'busy': True})
    if db.get_entries_needing_schedule():
        return jsonify({'busy': True})

    # get_entries_needing_sync() returns all scheduled+linked entries regardless of
    # drift — the actual drift check is done in Python, same as _step_sync does.
    for entry in db.get_entries_needing_sync():
        desired_hash = text_hash(format_post_text(entry))
        if (entry['post_number']       != entry.get('fb_post_number')
                or desired_hash        != entry.get('fb_text_hash')
                or _times_differ(entry.get('scheduled_time'), entry.get('fb_scheduled_time'))):
            return jsonify({'busy': True})

    return jsonify({'busy': False})

@app.route('/scheduled-content')
def scheduled_content():
    print("=" * 80)
    print("SCHEDULED CONTENT - SYNCING WITH FACEBOOK")
    print("=" * 80)

    # User is active on scheduled page — reset queue notification cooldown
    try:
        _cfg = load_config()
        if _cfg.pop('queue_notification_last_sent', None) is not None:
            save_config(_cfg)
    except Exception:
        pass

    if not extensions.facebook_handler:
        return '<div class="alert alert-danger text-center py-3">❌ Facebook לא מחובר — הגדר בהגדרות</div>'

    try:
        print("\n1. Fetching scheduled posts from Facebook...")
        fb_posts = extensions.facebook_handler.get_scheduled_posts()
        print(f"   Got {len(fb_posts)} posts from Facebook")

        print("\n2. Fetching scheduled entries from database...")
        db_entries = extensions.db.get_scheduled_entries()
        print(f"   Got {len(db_entries)} entries from database")

        fb_post_ids = set(p['id'] for p in fb_posts)

        now_utc = datetime.utcnow()
        def _is_future_entry(e):
            t = e.get('scheduled_time')
            if not t:
                return True
            try:
                dt = datetime.fromisoformat(str(t).replace('Z', '+00:00'))
                if dt.tzinfo is None:
                    dt = dt.replace(tzinfo=timezone.utc)
                return dt.replace(tzinfo=None) > now_utc + timedelta(minutes=5)
            except Exception:
                return True

        print("\n3. Detecting orphans and published entries...")

        # Published: fb_id known but not in FB, scheduled time is past → already published
        entries_published = [
            e for e in db_entries
            if e.get('facebook_post_id')
            and e['facebook_post_id'] not in fb_post_ids
            and not _is_future_entry(e)
        ]
        if entries_published:
            conn = extensions.db.get_connection()
            cursor = conn.cursor()
            for e in entries_published:
                cursor.execute("UPDATE entries SET status='published' WHERE id=?", (e['id'],))
            conn.commit()
            conn.close()
            db_entries = extensions.db.get_scheduled_entries()
            print(f"   ✓ Marked {len(entries_published)} entries as published")

        # Orphans: fb_id known, future-scheduled, but no longer on FB.
        # Do NOT cascade here — the reconciler's step3 already handles this via 404 detection.
        # Cascading from the UI races with step3 (which deletes-old then creates-new per entry).
        orphans = [
            e for e in db_entries
            if e.get('facebook_post_id')
            and e['facebook_post_id'] not in fb_post_ids
            and _is_future_entry(e)
        ]
        had_orphans = len(orphans) > 0
        if orphans:
            print(f"   Found {len(orphans)} orphan(s) — reconciler will handle")
        else:
            print("   ✓ No orphaned entries found")

        print("\n4. Building display data...")
        fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])

        # Index DB entries by facebook_post_id for O(1) lookup.
        # This is robust even when post_number in DB has been cascade-updated but FB
        # still shows the old number (reconciler hasn't pushed the renumber yet).
        entry_by_fb_id = {e['facebook_post_id']: e for e in db_entries if e.get('facebook_post_id')}
        entry_by_post_number = {e['post_number']: e for e in db_entries if e.get('post_number')}

        posts_data = []
        for fb_post in fb_posts_sorted:
            message = fb_post.get('message', '')
            fb_post_number = None
            clean_message = message
            if message.startswith('#'):
                try:
                    first_line = message.split('\n', 1)[0]
                    fb_post_number = int(first_line[1:])
                    clean_message = message.split('\n', 1)[1] if '\n' in message else message
                except Exception:
                    pass

            # Primary match: by facebook_post_id (survives reconciler catch-up window)
            entry = entry_by_fb_id.get(fb_post['id'])
            # Fallback: by post_number (for posts that have no fb_id yet, or created directly on FB)
            if entry is None and fb_post_number:
                entry = entry_by_post_number.get(fb_post_number)

            if entry:
                old_fb_id = entry.get('facebook_post_id')
                new_fb_id = fb_post['id']
                if old_fb_id and old_fb_id != new_fb_id:
                    # FB gave this post a new ID (e.g. after an update) — sync it
                    conn = extensions.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute(
                        'UPDATE entries SET facebook_post_id=?, scheduled_time=?, text=? WHERE id=?',
                        (new_fb_id, fb_post['scheduled_time'], clean_message, entry['id'])
                    )
                    conn.commit()
                    conn.close()
                    entry['facebook_post_id'] = new_fb_id
                    entry['scheduled_time'] = fb_post['scheduled_time']
                    entry['text'] = clean_message

            elif fb_post_number:
                # FB post exists but has no matching DB entry — create one (direct FB post)
                conn = extensions.db.get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    INSERT INTO entries (text, status, post_number, facebook_post_id, scheduled_time, timestamp)
                    VALUES (?, 'scheduled', ?, ?, ?, datetime('now'))
                ''', (clean_message, fb_post_number, fb_post['id'], fb_post['scheduled_time']))
                new_entry_id = cursor.lastrowid
                conn.commit()
                conn.close()
                entry = {
                    'id': new_entry_id,
                    'text': clean_message,
                    'post_number': fb_post_number,
                    'facebook_post_id': fb_post['id'],
                    'scheduled_time': fb_post['scheduled_time'],
                    'should_comment': 0,
                    'comment_posted': 0,
                }

            weekday = get_hebrew_weekday(fb_post['scheduled_time'])
            scheduled_time_str = fb_post['scheduled_time']
            if 'T' in scheduled_time_str:
                try:
                    dt = datetime.fromisoformat(scheduled_time_str.split('+')[0])
                    display_time = dt.strftime('%H:%M %d/%m/%Y')
                except Exception:
                    display_time = scheduled_time_str
            else:
                display_time = scheduled_time_str

            posts_data.append({
                'fb_post': fb_post,
                'entry': entry,
                'weekday': weekday,
                'display_time': display_time,
                'height': calculate_textarea_height(fb_post['message']) if fb_post.get('message') else 80
            })

        posts_data.sort(key=lambda x: x['fb_post']['scheduled_time'])
        print(f"\n5. Rendering template with {len(posts_data)} posts")
        print("=" * 80)
        return render_template('scheduled_content.html', posts=posts_data, had_orphans=had_orphans)

    except Exception as e:
        print(f"\nERROR in scheduled_content: {e}")
        traceback.print_exc()
        print("=" * 80)
        return f'<div class="alert alert-danger text-center py-3">❌ שגיאה: {str(e)}</div>'


@app.route('/unschedule_all', methods=['POST'])
def unschedule_all():
    """Return ALL scheduled posts to pending.
    Desired-state write only — reconciler deletes each FB post via step1."""
    from reconciler import signal_reconciler
    try:
        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            cursor.execute(
                'SELECT COUNT(*) as cnt FROM entries WHERE status IN ("scheduled", "approved")'
            )
            count = cursor.fetchone()['cnt']
            if count == 0:
                return jsonify({'ok': True, 'count': 0})

            cursor.execute(
                'SELECT MIN(post_number) as mn FROM entries '
                'WHERE status IN ("scheduled", "approved") AND post_number IS NOT NULL'
            )
            row = cursor.fetchone()
            min_sched = row['mn'] if row and row['mn'] is not None else None

            # Clear desired-state columns; keep facebook_post_id so the reconciler
            # picks each entry up in step1 (get_entries_needing_fb_delete) and deletes from FB.
            cursor.execute(
                'UPDATE entries '
                'SET status="pending", post_number=NULL, scheduled_time=NULL, '
                '    fb_scheduled_time=NULL, fb_post_number=NULL, fb_text_hash=NULL, fb_published_at=NULL '
                'WHERE status IN ("scheduled", "approved")'
            )
            if min_sched is not None:
                cursor.execute('UPDATE post_numbers SET current_number=? WHERE id=1', (min_sched,))
            conn.commit()
        finally:
            conn.close()

        print(f"[unschedule_all] {count} posts returned to pending — reconciler will delete from FB")
        signal_reconciler()
        return jsonify({'ok': True, 'count': count})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/reschedule_canonical', methods=['POST'])
def reschedule_canonical():
    """Redistribute all scheduled posts to canonical dynamic-tier slots.
    Desired-state write only — reconciler pushes each time change to Facebook."""
    from reconciler import signal_reconciler
    from datetime import timedelta as _td

    if not extensions.scheduler:
        return jsonify({'error': 'Scheduler not available'}), 500
    try:
        extensions.scheduler.update_dynamic_windows()

        raw_entries = extensions.db.get_scheduled_entries()
        if not raw_entries:
            return jsonify({'ok': True, 'updated': 0})

        # Deduplicate by post_number — keep entry with highest ID
        seen_pn: dict = {}
        no_number = []
        for e in raw_entries:
            pn = e.get('post_number')
            if pn is None:
                no_number.append(e)
            elif pn not in seen_pn or e['id'] > seen_pn[pn]['id']:
                seen_pn[pn] = e
        # Sort by post_number so the schedule always reflects numeric order
        # (matches the nightly reschedule_all_to_new_windows logic).
        entries = sorted(seen_pn.values(), key=lambda e: (e.get('post_number') or 999999)) + no_number
        dupes = len(raw_entries) - len(entries)
        if dupes:
            print(f"  ⚠️  Skipped {dupes} duplicate DB entries (same post_number)")

        n = len(entries)
        windows  = extensions.scheduler.load_posting_windows()
        tz       = extensions.scheduler.timezone
        now      = datetime.now(tz)
        min_time = now + _td(minutes=30)

        slots = []
        current_date = now.date()
        while len(slots) < n:
            if not extensions.scheduler.should_skip_date(current_date):
                for w in windows:
                    slot = tz.localize(datetime.combine(current_date, w))
                    if slot > min_time:
                        slots.append(slot)
                        if len(slots) >= n:
                            break
            current_date += _td(days=1)
            if (current_date - now.date()).days > 365:
                break

        slots = slots[:n]

        # Write desired scheduled_time for each entry; reconciler detects drift vs
        # fb_scheduled_time and calls update_scheduled_post on Facebook.
        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            for entry, slot in zip(entries, slots):
                cursor.execute(
                    'UPDATE entries SET scheduled_time=? WHERE id=?',
                    (slot.strftime('%Y-%m-%d %H:%M:%S'), entry['id'])
                )
            conn.commit()
        finally:
            conn.close()

        print(f"[reschedule_canonical] {n} desired slots written — reconciler will push to FB")
        signal_reconciler()
        return jsonify({'ok': True, 'updated': n})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/reschedule_today', methods=['POST'])
def reschedule_today():
    """Distribute the next N scheduled posts evenly in a custom time window today.
    Desired-state write only — reconciler pushes each time change to Facebook."""
    from reconciler import signal_reconciler
    from datetime import timedelta as _td

    data = request.get_json(silent=True) or {}
    try:
        start_minutes = int(data['start_minutes'])
        end_minutes   = int(data['end_minutes'])
        count         = max(1, min(20, int(data['count'])))
    except (KeyError, ValueError, TypeError) as e:
        return jsonify({'error': f'Bad parameters: {e}'}), 400

    if end_minutes <= start_minutes:
        return jsonify({'error': 'End time must be after start time'}), 400

    try:
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now       = datetime.now(israel_tz)
        today     = now.date()

        def make_dt(minutes):
            h, m = divmod(minutes, 60)
            return israel_tz.localize(datetime(today.year, today.month, today.day, h, m))

        start_dt = make_dt(start_minutes)
        end_dt   = make_dt(end_minutes)

        min_dt = now + _td(minutes=30)
        if start_dt < min_dt:
            start_dt = min_dt
        if end_dt < start_dt:
            return jsonify({'error': 'Window is entirely in the past'}), 400

        all_entries = extensions.db.get_scheduled_entries()
        entries = all_entries[:count]
        if not entries:
            return jsonify({'ok': True, 'updated': 0})

        n = len(entries)
        if n == 1:
            target_times = [start_dt]
        else:
            span = (end_dt - start_dt).total_seconds()
            target_times = [start_dt + _td(seconds=i * span / (n - 1)) for i in range(n)]

        # Write desired scheduled_time; reconciler detects drift and pushes to FB.
        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            for entry, new_time_dt in zip(entries, target_times):
                cursor.execute(
                    'UPDATE entries SET scheduled_time=? WHERE id=?',
                    (new_time_dt.strftime('%Y-%m-%d %H:%M:%S'), entry['id'])
                )
            conn.commit()
        finally:
            conn.close()

        print(f"[reschedule_today] {n} desired slots written — reconciler will push to FB")
        signal_reconciler()
        return jsonify({'ok': True, 'updated': n})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/unschedule/<int:entry_id>', methods=['POST'])
def unschedule_entry(entry_id):
    from reconciler import signal_reconciler
    print(f"[unschedule] entry {entry_id}")
    try:
        result = extensions.db.unschedule_entry(entry_id)
        if result.get('error'):
            flash(f'❌ {result["error"]}', 'error')
        elif not result:
            flash('❌ Entry not found or not scheduled', 'error')
        else:
            # Desired state written: status=pending, fb_* untouched — reconciler will delete from FB
            # and slide the following posts' fb_* columns as each sync cycle runs
            print(f"[unschedule] freed #{result.get('freed_number')} — signalling reconciler")
            signal_reconciler()
            flash('✅ הוחזר להמתנה', 'success')
    except Exception as e:
        print(f"[unschedule] error: {e}")
        traceback.print_exc()
        flash(f'❌ שגיאה: {str(e)}', 'error')
    return redirect(url_for('scheduled_page'))

@app.route('/edit_scheduled/<int:entry_id>', methods=['POST'])
def edit_scheduled_post(entry_id):
    from reconciler import signal_reconciler
    new_text = request.json.get('text', '') if request.is_json else request.form.get('text', '')

    if not new_text:
        if request.headers.get('HX-Request'):
            return '', 400
        flash('❌ טקסט ריק', 'error')
        return redirect(url_for('scheduled_page'))

    # Strip leading "#N " prefix if user accidentally included it
    clean_text = new_text
    if clean_text.startswith('#'):
        parts = clean_text.split(' ', 1)
        if len(parts) > 1:
            clean_text = parts[1]

    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT id FROM entries WHERE id=? AND status="scheduled"', (entry_id,))
    if not cursor.fetchone():
        conn.close()
        if request.headers.get('HX-Request'):
            return '', 404
        flash('❌ לא נמצא', 'error')
        return redirect(url_for('scheduled_page'))

    try:
        cursor.execute('UPDATE entries SET text = ? WHERE id = ?', (clean_text, entry_id))
        conn.commit()
        print(f"[edit] entry {entry_id} text updated — reconciler will push to FB")
        signal_reconciler()
        flash('✅ הפוסט עודכן בהצלחה!', 'success')
    except Exception as e:
        conn.rollback()
        traceback.print_exc()
        if request.headers.get('HX-Request'):
            return '', 500
        flash(f'❌ שגיאה: {str(e)}', 'error')
    finally:
        conn.close()

    return redirect(url_for('scheduled_page'))

@app.route('/reorder_posts', methods=['POST'])
def reorder_posts():
    """Reorder scheduled posts by drag-and-drop.
    Body: {"order": [entry_id, entry_id, ...]} — full list in new desired order.
    Writes desired state to DB; reconciler syncs to Facebook asynchronously.
    """
    from reconciler import signal_reconciler

    data = request.get_json(silent=True) or {}
    new_order_ids = [int(x) for x in data.get('order', [])]
    if not new_order_ids:
        return jsonify({'error': 'No order provided'}), 400

    try:
        entries = sorted(extensions.db.get_scheduled_entries(), key=lambda x: x['scheduled_time'])
        entry_map = {e['id']: e for e in entries}

        # Assign time slots and post numbers by visual position (ascending time / ascending number)
        time_slots   = [e['scheduled_time'] for e in entries]
        post_numbers = sorted(e['post_number'] for e in entries if e['post_number'] is not None)

        print(f"[reorder] {len(new_order_ids)} ids, {len(entries)} DB entries")

        updates = []
        for idx, eid in enumerate(new_order_ids):
            entry = entry_map.get(eid)
            if not entry or idx >= len(time_slots):
                continue
            new_time_str = time_slots[idx]
            new_num = post_numbers[idx] if idx < len(post_numbers) else entry['post_number']
            if entry['scheduled_time'] != new_time_str or entry['post_number'] != new_num:
                updates.append({'id': eid, 'post_number': new_num, 'scheduled_time': new_time_str})
                print(f"[reorder]  id={eid} #{entry['post_number']}→{new_num} t→{new_time_str}")

        if updates:
            extensions.db.bulk_update_post_orders(updates)
            print(f"[reorder] {len(updates)} entries updated — signalling reconciler")
            signal_reconciler()

        return jsonify({'ok': True, 'updated': len(updates)})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/swap_posts/<int:entry_id>/<direction>', methods=['POST'])
def swap_posts(entry_id, direction):
    from reconciler import signal_reconciler
    try:
        entries = sorted(extensions.db.get_scheduled_entries(), key=lambda x: x.get('post_number', 999))
        current_idx = next((i for i, e in enumerate(entries) if e['id'] == entry_id), None)

        if current_idx is None:
            flash('❌ Entry not found', 'error')
            return redirect(url_for('scheduled_page'))

        if direction == 'up' and current_idx > 0:
            target_idx = current_idx - 1
        elif direction == 'down' and current_idx < len(entries) - 1:
            target_idx = current_idx + 1
        else:
            return redirect(url_for('scheduled_page'))

        current_entry = entries[current_idx]
        target_entry  = entries[target_idx]

        # Write desired state — reconciler will push both updates to FB
        conn = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET post_number=?, scheduled_time=? WHERE id=?',
                       (target_entry['post_number'], target_entry['scheduled_time'], current_entry['id']))
        cursor.execute('UPDATE entries SET post_number=?, scheduled_time=? WHERE id=?',
                       (current_entry['post_number'], current_entry['scheduled_time'], target_entry['id']))
        conn.commit()
        conn.close()

        print(f"[swap] entries {current_entry['id']} ↔ {target_entry['id']} — signalling reconciler")
        signal_reconciler()
        flash('✅ הפוסטים הוחלפו בהצלחה!', 'success')

    except Exception as e:
        traceback.print_exc()
        flash(f'❌ שגיאה: {str(e)}', 'error')

    return redirect(url_for('scheduled_page'))

# ============================================================================
# ROUTES - Denied Posts
# ============================================================================

@app.route('/denied')
def denied_page():
    extensions.db.cleanup_old_denied()
    entries = extensions.db.get_denied_entries()
    now = datetime.now()
    for entry in entries:
        denied_at = datetime.fromisoformat(entry['denied_at'])
        delete_at = denied_at + timedelta(hours=24)
        remaining = delete_at - now
        hours = int(remaining.total_seconds() // 3600)
        minutes = int((remaining.total_seconds() % 3600) // 60)
        entry['time_remaining'] = f"{hours}:{minutes:02d}"
        entry['height'] = calculate_textarea_height(entry['text'])
    return render_template('denied.html', entries=entries)

@app.route('/restore/<int:entry_id>', methods=['POST'])
def restore_denied(entry_id):
    extensions.db.return_denied_to_pending(entry_id)
    if request.headers.get('HX-Request'):
        return '', 200
    flash('✅ הוחזר להמתנה', 'success')
    return redirect(url_for('denied_page'))

# ============================================================================
# ROUTES - Statistics
# ============================================================================

@app.route('/statistics')
def statistics_page():
    stats = extensions.db.get_statistics()
    recent = extensions.db.get_recent_activity(20)
    current_number = extensions.db.get_current_post_number()
    return render_template('statistics.html', stats=stats, recent=recent, current_number=current_number)

# ============================================================================
# ROUTES - Settings
# ============================================================================

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    if request.method == 'POST':
        config = load_config()

        secret_fields = {
            'facebook_access_token':  request.form.get('facebook_access_token', ''),
            'facebook_app_secret':    request.form.get('facebook_app_secret', ''),
            'resend_api_key':         request.form.get('resend_api_key', ''),
            'openai_api_key':         request.form.get('openai_api_key', ''),
            'cloudinary_cloud_name':  request.form.get('cloudinary_cloud_name', ''),
            'cloudinary_api_key':     request.form.get('cloudinary_api_key', ''),
            'cloudinary_api_secret':  request.form.get('cloudinary_api_secret', ''),
        }

        config.update({
            'google_sheet_id':                request.form.get('google_sheet_id', ''),
            'google_credentials_file':        request.form.get('google_credentials_file', 'credentials.json'),
            'read_from_date':                 request.form.get('read_from_date', ''),
            'facebook_page_id':               request.form.get('facebook_page_id', ''),
            'facebook_page_name':             request.form.get('facebook_page_name', ''),
            'skip_shabbat':                   request.form.get('skip_shabbat') == 'on',
            'skip_jewish_holidays':           request.form.get('skip_jewish_holidays') == 'on',
            'notifications_enabled':          request.form.get('notifications_enabled') == 'on',
            'resend_from_email':              request.form.get('resend_from_email', ''),
            'notification_emails':            [e.strip() for e in request.form.get('notification_emails', '').split(',') if e.strip()],
            'pending_threshold':              int(request.form.get('pending_threshold', 20)),
            'app_url':                        request.form.get('app_url', ''),
            'comments_filter_enabled':        request.form.get('comments_filter_enabled') == 'on',
            'batch_size':                     int(request.form.get('batch_size', 50)),
            'comment_flagged_notification_threshold': int(request.form.get('comment_flagged_notification_threshold', 0)),
            'comment_notification_threshold': int(request.form.get('comment_notification_threshold', 0)),
            'comment_retention_days':         int(request.form.get('comment_retention_days', 7)),
            'webhook_verify_token':           request.form.get('webhook_verify_token', ''),
            'webhook_batch_size':             int(request.form.get('webhook_batch_size', 10)),
            'webhook_batch_timeout_minutes':  int(request.form.get('webhook_batch_timeout_minutes', 5)),
            'instagram_enabled':             request.form.get('instagram_enabled') == 'on',
            'instagram_ig_account_id':       request.form.get('instagram_ig_account_id', '').strip(),
            'instagram_engagement_threshold': int(request.form.get('instagram_engagement_threshold', 150) or 150),
            'instagram_watch_days':          int(request.form.get('instagram_watch_days', 7) or 7),
            'instagram_hashtags':            request.form.get('instagram_hashtags', '').strip(),
            'instagram_watermark':           request.form.get('instagram_watermark', 'וידויים צבאיים').strip(),
        })

        # Instagram dynamic tiers — same format as Facebook: "max_posts | t1,t2"
        ig_tiers_text = request.form.get('instagram_dynamic_tiers', '')
        if ig_tiers_text.strip():
            ig_tiers = []
            for line in ig_tiers_text.strip().split('\n'):
                line = line.strip()
                if not line or '|' not in line:
                    continue
                parts = line.split('|')
                try:
                    max_posts = int(parts[0].strip())
                    windows = []
                    for w in parts[1].strip().split(','):
                        w = w.strip()
                        if not w:
                            continue
                        h, m = w.split(':')
                        if 0 <= int(h) <= 23 and 0 <= int(m) <= 59:
                            windows.append(f"{int(h):02d}:{int(m):02d}")
                    if windows:
                        ig_tiers.append({'max_posts': max_posts, 'windows': windows})
                except ValueError:
                    continue
            if ig_tiers:
                ig_tiers.sort(key=lambda t: t['max_posts'])
                config['instagram_dynamic_tiers'] = ig_tiers

        for key, value in secret_fields.items():
            if value.strip():
                if key == 'facebook_access_token' and value.strip() != config.get('facebook_access_token', ''):
                    config['facebook_token_saved_at'] = datetime.utcnow().strftime('%Y-%m-%dT%H:%M:%SZ')
                config[key] = value.strip()

        tiers_text = request.form.get('dynamic_tiers', '')
        if tiers_text.strip():
            dynamic_tiers = []
            bad_times = []
            for line in tiers_text.strip().split('\n'):
                line = line.strip()
                if not line or '|' not in line:
                    continue
                parts = line.split('|')
                try:
                    max_posts = int(parts[0].strip())
                    raw_windows = [w.strip() for w in parts[1].strip().split(',') if w.strip()]
                    windows = []
                    for w in raw_windows:
                        t_parts = w.split(':')
                        if len(t_parts) != 2:
                            bad_times.append(w)
                            continue
                        h, m = int(t_parts[0]), int(t_parts[1])
                        if not (0 <= h <= 23 and 0 <= m <= 59):
                            bad_times.append(w)
                            continue
                        windows.append(f"{h:02d}:{m:02d}")
                    if windows:
                        dynamic_tiers.append({'max_posts': max_posts, 'windows': windows})
                except ValueError:
                    continue
            if bad_times:
                flash(f'❌ שעות לא חוקיות: {", ".join(bad_times)} — יש להשתמש בפורמט HH:MM (00:00–23:59)', 'danger')
                return redirect(url_for('settings_page'))
            if dynamic_tiers:
                dynamic_tiers.sort(key=lambda t: t['max_posts'])
                config['dynamic_tiers'] = dynamic_tiers

        save_config(config)
        init_handlers()

        flash('✅ הגדרות נשמרו בהצלחה!', 'success')
        return redirect(url_for('settings_page'))

    config = load_config()
    current_number = extensions.db.get_current_post_number()

    secret_keys = ['facebook_access_token', 'facebook_app_secret', 'resend_api_key', 'openai_api_key',
                   'cloudinary_cloud_name', 'cloudinary_api_key', 'cloudinary_api_secret']
    for key in secret_keys:
        if config.get(key):
            config[key + '_set'] = True
            config[key] = ''
        else:
            config[key + '_set'] = False

    return render_template('settings.html', config=config, current_number=current_number)

def _ig_clean_text(raw: str) -> str:
    """Strip a leading '#number\\n' prefix from stored post text."""
    raw = raw or ''
    if raw.startswith('#'):
        parts = raw.split('\n', 1)
        return parts[1].strip() if len(parts) > 1 else raw
    return raw.strip()


@app.route('/instagram-watch')
def instagram_watch_page():
    """Posts currently watched for engagement, with progress toward the threshold."""
    config    = load_config()
    threshold = int(config.get('instagram_engagement_threshold', 150))
    watch_days = int(config.get('instagram_watch_days', 7))
    israel_tz = pytz.timezone('Asia/Jerusalem')
    now = datetime.now(israel_tz)
    # Seed the watch list with any past-week posts not yet tracked, so the page
    # reflects the full week immediately (engagement refreshes hourly via the job).
    try:
        extensions.db.seed_ig_watch((now - timedelta(days=watch_days)).isoformat())
    except Exception as e:
        print(f"[ig-watch] seed error: {e}")
    rows = extensions.db.get_ig_watching()
    items = []
    for r in rows:
        eng = r.get('last_engagement') or 0
        pct = min(100, int(eng * 100 / threshold)) if threshold else 0
        # days left in watch window
        days_left = None
        try:
            pub = datetime.fromisoformat(r['published_at'])
            if pub.tzinfo is None:
                pub = israel_tz.localize(pub)
            days_left = round(watch_days - (now - pub.astimezone(israel_tz)).total_seconds() / 86400, 1)
        except Exception:
            pass
        items.append({
            'id': r['id'], 'post_number': r['post_number'],
            'text': _ig_clean_text(r['text']),
            'engagement': eng, 'pct': pct, 'days_left': days_left,
            'last_checked': r.get('last_checked'),
        })
    return render_template('instagram_watch.html', items=items,
                           threshold=threshold, watch_days=watch_days, config=config)


@app.route('/instagram-scheduled')
def instagram_scheduled_page():
    """Instagram posts queued for publishing (crossed the threshold)."""
    config = load_config()
    rows = extensions.db.get_ig_scheduled()
    israel_tz = pytz.timezone('Asia/Jerusalem')
    items = []
    for r in rows:
        slot = r.get('ig_scheduled_time')
        disp = slot
        weekday = ''
        try:
            dt = datetime.fromisoformat(slot)
            if dt.tzinfo is None:
                dt = israel_tz.localize(dt)
            disp = dt.strftime('%H:%M %d/%m/%Y')
            weekday = get_hebrew_weekday(slot)
        except Exception:
            pass
        items.append({
            'id': r['id'], 'post_number': r['post_number'],
            'text': _ig_clean_text(r['text']),
            'engagement': r.get('last_engagement') or 0,
            'scheduled_time': slot, 'display_time': disp, 'weekday': weekday,
        })
    return render_template('instagram_scheduled.html', items=items, config=config)


@app.route('/instagram-image/<int:log_id>.jpg')
def instagram_image(log_id):
    """Render a preview JPEG (first slide) of a queued/watched IG post."""
    entry = extensions.db.get_ig_entry(log_id)
    if not entry:
        return 'not found', 404
    from image_generator import generate_confession_slides, slides_to_bytes
    config = load_config()
    slides = generate_confession_slides(
        text=_ig_clean_text(entry['text']),
        post_number=entry['post_number'],
        watermark=config.get('instagram_watermark', 'וידויים צבאיים'),
    )
    data = slides_to_bytes(slides)[0]
    return Response(data, mimetype='image/jpeg')


@app.route('/instagram-schedule-now/<int:log_id>', methods=['POST'])
def instagram_schedule_now(log_id):
    """Manually move a watched post into the scheduled queue."""
    taken = extensions.db.get_ig_scheduled_slots()
    slot  = extensions.scheduler.get_next_available_ig_slot(taken)
    extensions.db.set_ig_scheduled(log_id, slot.isoformat())
    return jsonify({'ok': True})


@app.route('/instagram-drop/<int:log_id>', methods=['POST'])
def instagram_drop(log_id):
    """Drop a post from watch or remove it from the schedule."""
    extensions.db.set_ig_status(log_id, 'dropped')
    return jsonify({'ok': True})


@app.route('/instagram-edit/<int:log_id>', methods=['POST'])
def instagram_edit(log_id):
    """Edit the text of a queued IG post (image regenerates from it)."""
    body = request.get_json(silent=True) or {}
    text = request.form.get('text', '') or body.get('text', '')
    if not text.strip():
        return jsonify({'ok': False, 'error': 'empty text'}), 400
    extensions.db.set_ig_text(log_id, text.strip())
    return jsonify({'ok': True})


@app.route('/instagram-reorder', methods=['POST'])
def instagram_reorder():
    """Reorder scheduled IG posts: assign the existing slot times to the new order."""
    body  = request.get_json(silent=True) or {}
    order = body.get('order', [])
    if not order:
        return jsonify({'ok': False, 'error': 'no order'}), 400
    # Existing slots, sorted chronologically, reassigned to the new id order.
    slots = sorted(extensions.db.get_ig_scheduled_slots())
    for log_id, slot in zip(order, slots):
        extensions.db.set_ig_slot(int(log_id), slot)
    return jsonify({'ok': True, 'updated': min(len(order), len(slots))})


@app.route('/instagram-reschedule-canonical', methods=['POST'])
def instagram_reschedule_canonical():
    """Redistribute all scheduled IG posts to canonical IG dynamic-tier slots,
    in post_number order (mirrors the Facebook canonical reschedule)."""
    from datetime import timedelta as _td
    rows = extensions.db.get_ig_scheduled()
    if not rows:
        return jsonify({'ok': True, 'updated': 0})
    rows.sort(key=lambda r: (r.get('post_number') or 999999))
    n        = len(rows)
    tz       = extensions.scheduler.timezone
    now      = datetime.now(tz)
    windows  = extensions.scheduler.load_ig_windows(n)
    min_time = now + _td(minutes=30)

    slots, day = [], now.date()
    while len(slots) < n and (day - now.date()).days <= 365:
        if not extensions.scheduler.should_skip_date(day):
            for w in windows:
                slot = tz.localize(datetime.combine(day, w))
                if slot > min_time:
                    slots.append(slot)
                    if len(slots) >= n:
                        break
        day += _td(days=1)

    for entry, slot in zip(rows, slots):
        extensions.db.set_ig_slot(entry['id'], slot.isoformat())
    return jsonify({'ok': True, 'updated': len(slots)})


@app.route('/instagram-backfill', methods=['POST'])
def instagram_backfill():
    """Backfill instagram_post_log (test aid). reset=1 re-arms everything to 'watching'."""
    days  = int(request.form.get('days', 7))
    reset = request.form.get('reset') == '1'
    count = extensions.db.backfill_instagram_post_log(days=days)
    if reset:
        conn   = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute("UPDATE instagram_post_log SET ig_status='watching', ig_scheduled_time=NULL, "
                       "ig_posted=0, ig_posted_at=NULL, ig_skipped=0")
        reset_count = cursor.rowcount
        conn.commit()
        conn.close()
        return jsonify({'ok': True, 'message': f'✅ {count} נטענו, {reset_count} אופסו למעקב'})
    return jsonify({'ok': True, 'message': f'✅ {count} פוסטים נטענו למעקב Instagram'})


@app.route('/instagram-post-now', methods=['POST'])
def instagram_post_now():
    """Manually publish the earliest scheduled IG post now (test, bypasses timing)."""
    from background_tasks import instagram_publish_job
    import threading

    def _run_forced():
        from config import load_config, save_config
        cfg = load_config()
        was_enabled = cfg.get('instagram_enabled', False)
        cfg['instagram_enabled'] = True
        save_config(cfg)
        try:
            instagram_publish_job(force_one=True)
        finally:
            cfg2 = load_config()
            cfg2['instagram_enabled'] = was_enabled
            save_config(cfg2)

    threading.Thread(target=_run_forced, daemon=True).start()
    flash('Instagram publish started — check logs', 'info')
    return redirect(url_for('settings_page'))


@app.route('/set_post_number', methods=['POST'])
def set_post_number():
    new_number = request.form.get('new_post_number', '').strip()
    if not new_number:
        flash('⚠️ לא הוזן מספר', 'error')
        return redirect(url_for('settings_page'))
    try:
        num = int(new_number)
        if num < 1:
            raise ValueError
        extensions.db.reset_post_number(num)
        effective = extensions.db.get_current_post_number()
        if effective != num:
            flash(f'✅ מספר הפוסט הבא עודכן ל-#{num} — הפוסט הבא יקבל #{effective} (מספרים עד {effective-1} כבר בשימוש)', 'success')
        else:
            flash(f'✅ מספר הפוסט הבא עודכן ל-#{num}', 'success')
    except ValueError:
        flash('⚠️ מספר לא תקין', 'error')
    return redirect(url_for('settings_page'))

@app.route('/clear_pending', methods=['POST'])
def clear_pending():
    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM entries WHERE status = 'pending'")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    flash(f'✅ נמחקו {count} ערכים ממתינים', 'success')
    return redirect(url_for('settings_page'))

@app.route('/clear_comments', methods=['POST'])
def clear_comments():
    count = extensions.db.clear_all_comments()
    flash(f'✅ נמחקו {count} תגובות ממסד הנתונים (דוגמאות AI נשמרו)', 'success')
    return redirect(url_for('settings_page'))

@app.route('/delete_database', methods=['POST'])
def delete_database():
    global db
    try:
        extensions.db.get_connection().close()
        if os.path.exists('content_system.db'):
            os.remove('content_system.db')
        extensions.db = Database()
        flash('✅ מסד הנתונים נמחק ונוצר מחדש', 'success')
    except Exception as e:
        flash(f'❌ שגיאה: {str(e)}', 'error')
    return redirect(url_for('settings_page'))

@app.route('/test_notification', methods=['POST'])
def test_notification():
    if extensions.notifications.send_test_notification():
        flash('✅ התראה נשלחה!', 'success')
    else:
        flash('❌ שליחה נכשלה', 'error')
    return redirect(url_for('settings_page'))

# ============================================================================
# API ENDPOINTS
# ============================================================================

@app.route('/api/entry/<int:entry_id>/height')
def get_entry_height(entry_id):
    entries = extensions.db.get_pending_entries()
    entry = next((e for e in entries if e['id'] == entry_id), None)
    return jsonify({'height': calculate_textarea_height(entry['text']) if entry else 120})

@app.route('/api/scheduler/status')
def scheduler_status():
    import schedule as _schedule
    jobs = _schedule.get_jobs()
    return jsonify({
        'running': len(jobs) > 0,
        'jobs': [str(job) for job in jobs],
        'next_run': str(_schedule.next_run()) if jobs else None
    })

# ============================================================================
# STARTUP
# ============================================================================

try:
    from background_tasks import start_scheduler
    start_scheduler()
except Exception as e:
    print(f"⚠️  Scheduler initialization failed: {e}")

if __name__ == '__main__':
    is_production = os.environ.get('FLASK_ENV') == 'production' or not os.isatty(0)
    if is_production:
        print("🚀 Starting in PRODUCTION mode")
        app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
    else:
        print("🔧 Starting in DEVELOPMENT mode")
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)
