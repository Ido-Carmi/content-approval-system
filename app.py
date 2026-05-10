from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
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
_last_renumber_time = None  # cooldown: prevent renumber cascade

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
    json_body = request.get_json(silent=True) or {}
    edited_text = request.form.get('text', '') or json_body.get('text', '')
    comment_bitmask = int(request.form.get('comment_bitmask', 0) or json_body.get('comment_bitmask', 0) or 0)
    approved = extensions.db.approve_entry(entry_id, edited_text, 'admin')
    if not approved:
        # Already approved/denied (double-submit) — return quietly
        if request.headers.get('HX-Request'):
            return '', 200
        return redirect(url_for('review_page'))

    if comment_bitmask > 0:
        extensions.db.set_should_comment(entry_id, comment_bitmask)

    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT post_number FROM entries WHERE id = ?', (entry_id,))
    result = cursor.fetchone()
    post_number = result['post_number'] if result else 1
    conn.close()

    formatted_text = f"#{post_number} {edited_text}"

    def schedule_in_background():
        if extensions.scheduler:
            try:
                extensions.scheduler.schedule_post_to_facebook(entry_id, formatted_text)
            except Exception as e:
                print(f"Background scheduling error: {e}")

    threading.Thread(target=schedule_in_background, daemon=True).start()

    if request.headers.get('HX-Request'):
        return '', 200
    return redirect(url_for('review_page'))

@app.route('/deny/<int:entry_id>', methods=['POST'])
def deny_entry(entry_id):
    extensions.db.deny_entry(entry_id, 'admin')
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

@app.route('/scheduled-content')
def scheduled_content():
    print("=" * 80)
    print("SCHEDULED CONTENT - SYNCING WITH FACEBOOK")
    print("=" * 80)

    if not extensions.facebook_handler:
        return '<div class="alert alert-danger text-center py-3">❌ Facebook לא מחובר — הגדר בהגדרות</div>'

    try:
        posts_deleted = False
        holes_filled = False

        print("\n1. Fetching scheduled posts from Facebook...")
        fb_posts = extensions.facebook_handler.get_scheduled_posts()
        print(f"   Got {len(fb_posts)} posts from Facebook")

        print("\n2. Fetching scheduled entries from database...")
        db_entries = extensions.db.get_scheduled_entries()
        print(f"   Got {len(db_entries)} entries from database")

        fb_post_ids = set(p['id'] for p in fb_posts)

        print("\n3. Syncing database with Facebook...")
        fb_posts_by_number = {}
        for fb_post in fb_posts:
            message = fb_post.get('message', '')
            if message.startswith('#'):
                try:
                    post_number = int(message.split()[0][1:])
                    fb_posts_by_number[post_number] = fb_post
                except Exception:
                    pass

        print(f"   Found {len(fb_posts_by_number)} Facebook posts with post numbers")

        now_utc = datetime.utcnow()
        # Only treat as orphaned if scheduled time is in the future — past entries are simply published
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

        entries_to_remove = [
            e for e in db_entries
            if e.get('post_number') and e['post_number'] not in fb_posts_by_number
            and _is_future_entry(e)
        ]
        # Entries missing from Facebook but with a past scheduled time were published — mark them
        entries_published = [
            e for e in db_entries
            if e.get('post_number') and e['post_number'] not in fb_posts_by_number
            and not _is_future_entry(e)
        ]
        if entries_published:
            conn = extensions.db.get_connection()
            cursor = conn.cursor()
            for e in entries_published:
                cursor.execute("UPDATE entries SET status = 'published' WHERE id = ?", (e['id'],))
            conn.commit()
            conn.close()
            db_entries = extensions.db.get_scheduled_entries()
            print(f"   ✓ Marked {len(entries_published)} entries as published (past scheduled time)")
        had_orphans = len(entries_to_remove) > 0

        if entries_to_remove:
            entries_to_remove.sort(key=lambda x: x.get('post_number', 999))
            conn = extensions.db.get_connection()
            cursor = conn.cursor()
            deleted_numbers = [e['post_number'] for e in entries_to_remove]
            print(f"   Deleted post numbers: {deleted_numbers}")
            for entry in entries_to_remove:
                cursor.execute('DELETE FROM entries WHERE id = ?', (entry['id'],))
            cursor.execute('UPDATE post_numbers SET current_number = current_number - ? WHERE id = 1', (len(entries_to_remove),))
            conn.commit()
            conn.close()
            db_entries = extensions.db.get_scheduled_entries()
            print(f"   ✓ Deleted {len(entries_to_remove)} orphaned entries")
        else:
            print("   ✓ No orphaned entries found")

        print("\n4. Matching and syncing Facebook posts with database entries...")
        fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])

        global _last_renumber_time
        _renumber_cooldown_secs = 300  # 5 minutes
        _now = time.time()
        _cooldown_ok = (_last_renumber_time is None or (_now - _last_renumber_time) > _renumber_cooldown_secs)

        if had_orphans and len(fb_posts_sorted) > 0 and _cooldown_ok:
            _last_renumber_time = _now
            print("   Renumbering remaining posts to fill gaps...")

            fb_post_numbers = sorted([
                int(p['message'].split()[0][1:])
                for p in fb_posts_sorted
                if p.get('message', '').startswith('#')
                and p['message'].split()[0][1:].isdigit()
            ])

            renumber_results = []
            if fb_post_numbers:
                lowest = fb_post_numbers[0]
                for fb_post, expected in zip(fb_posts_sorted, range(lowest, lowest + len(fb_posts_sorted))):
                    message = fb_post.get('message', '')
                    current_number = None
                    clean_message = message
                    if message.startswith('#'):
                        try:
                            current_number = int(message.split()[0][1:])
                            clean_message = message.split(' ', 1)[1] if ' ' in message else message
                        except Exception:
                            pass
                    if current_number and current_number != expected:
                        new_text = f"#{expected} {clean_message}"
                        try:
                            old_fb_id = fb_post['id']
                            _dt = datetime.fromisoformat(fb_post['scheduled_time'])
                            if _dt.tzinfo is None:
                                _dt = pytz.timezone('Asia/Jerusalem').localize(_dt)
                            result = extensions.facebook_handler.update_scheduled_post(old_fb_id, new_text, _dt)
                            new_fb_id = result['id']
                            fb_post['message'] = new_text
                            fb_post['id'] = new_fb_id
                            # Store old FB ID so we can target the exact DB row (post_number is not unique)
                            renumber_results.append((old_fb_id, expected, new_fb_id))
                            print(f"   Renumbered #{current_number} → #{expected}")
                        except Exception as e:
                            print(f"   ✗ Error renumbering: {e}")

            if renumber_results:
                conn = extensions.db.get_connection()
                cursor = conn.cursor()
                for old_fb_id, new_num, new_fb_id in renumber_results:
                    # Update by Facebook post ID (unique) — not by post_number (may have duplicates)
                    cursor.execute(
                        'UPDATE entries SET post_number = ?, facebook_post_id = ? WHERE facebook_post_id = ?',
                        (new_num, new_fb_id, old_fb_id)
                    )
                conn.commit()
                conn.close()
                print(f"   Updated {len(renumber_results)} DB entries with new post IDs")

            print("   Re-fetching from Facebook...")
            fb_posts = extensions.facebook_handler.get_scheduled_posts()
            fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])

        elif had_orphans and not _cooldown_ok:
            secs_left = int(_renumber_cooldown_secs - (_now - _last_renumber_time))
            print(f"   ⏳ Skipping renumber — cooldown active ({secs_left}s remaining)")

        posts_data = []
        for fb_post in fb_posts_sorted:
            message = fb_post.get('message', '')
            fb_post_number = None
            clean_message = message
            if message.startswith('#'):
                try:
                    fb_post_number = int(message.split()[0][1:])
                    clean_message = message.split(' ', 1)[1] if ' ' in message else message
                except Exception:
                    pass

            entry = None
            if fb_post_number:
                entry = next((e for e in db_entries if e.get('post_number') == fb_post_number), None)

            if entry:
                old_fb_id = entry.get('facebook_post_id')
                new_fb_id = fb_post['id']
                if old_fb_id != new_fb_id:
                    conn = extensions.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE entries
                        SET facebook_post_id = ?, scheduled_time = ?, text = ?
                        WHERE id = ?
                    ''', (new_fb_id, fb_post['scheduled_time'], clean_message, entry['id']))
                    conn.commit()
                    conn.close()
                    entry['facebook_post_id'] = new_fb_id
                    entry['scheduled_time'] = fb_post['scheduled_time']
                    entry['text'] = clean_message

            elif fb_post_number:
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

        # Fill holes: move posts to earliest available slots.
        # Only runs when had_orphans=True (a post was deleted and left a genuine gap).
        # Skipping otherwise preserves any custom scheduling the user applied.
        print("\n5. Checking for holes in schedule...")
        if had_orphans and not _cooldown_ok:
            secs_left = int(_renumber_cooldown_secs - (time.time() - _last_renumber_time))
            print(f"   ⏳ Skipping hole-fill — cooldown active ({secs_left}s remaining)")
        elif had_orphans and _cooldown_ok and extensions.scheduler and len(fb_posts) > 0:

            def _parse_fb_time(time_str):
                try:
                    dt = datetime.fromisoformat(time_str)
                    if dt.tzinfo is None:
                        dt = extensions.scheduler.timezone.localize(dt)
                    return dt.astimezone(extensions.scheduler.timezone)
                except (ValueError, TypeError):
                    return None

            parsed_posts = sorted(
                [(p, t) for p in fb_posts if (t := _parse_fb_time(p['scheduled_time']))],
                key=lambda x: x[1]
            )

            if parsed_posts:
                windows = extensions.scheduler.load_posting_windows()
                now = datetime.now(extensions.scheduler.timezone)
                valid_slots = []
                current_date = now.date()
                while len(valid_slots) < len(parsed_posts):
                    if not extensions.scheduler.should_skip_date(current_date):
                        for window_time in windows:
                            slot = extensions.scheduler.timezone.localize(datetime.combine(current_date, window_time))
                            if slot > now:
                                valid_slots.append(slot)
                                if len(valid_slots) >= len(parsed_posts):
                                    break
                    current_date += timedelta(days=1)
                    if (current_date - now.date()).days > 365:
                        break

                valid_slots = valid_slots[:len(parsed_posts)]
                posts_to_update = [
                    (fb_post, target)
                    for (fb_post, current_time), target in zip(parsed_posts, valid_slots)
                    if abs((current_time - target).total_seconds()) > 60
                ]

                if posts_to_update:
                    print(f"   Found holes! Moving {len(posts_to_update)} posts...")
                    moved_count = 0
                    for fb_post, new_slot in posts_to_update:
                        message = fb_post.get('message', '')
                        post_number = None
                        if message.startswith('#'):
                            try:
                                post_number = int(message.split()[0][1:])
                            except Exception:
                                pass
                        if post_number:
                            try:
                                result = extensions.facebook_handler.update_scheduled_post(fb_post['id'], message, new_slot)
                                new_fb_id = result.get('id') if result else None
                                print(f"       ✓ #{post_number}: moved → new FB ID {new_fb_id}")
                                # Update DB immediately so concurrent page loads see the new ID
                                # and don't falsely detect this post as an orphan during a long hole-fill
                                conn = extensions.db.get_connection()
                                cursor = conn.cursor()
                                if new_fb_id:
                                    cursor.execute(
                                        'UPDATE entries SET scheduled_time = ?, facebook_post_id = ? WHERE post_number = ?',
                                        (new_slot.isoformat(), new_fb_id, post_number))
                                else:
                                    cursor.execute('UPDATE entries SET scheduled_time = ? WHERE post_number = ?',
                                                   (new_slot.isoformat(), post_number))
                                conn.commit()
                                conn.close()
                                moved_count += 1
                            except Exception as e:
                                print(f"       ✗ Error: {e}")
                    print(f"   Updated {moved_count} DB entries with new FB IDs after hole-fill")

                    fb_posts = extensions.facebook_handler.get_scheduled_posts()
                    # Re-fetch db_entries so new FB IDs are visible
                    db_entries = extensions.db.get_scheduled_entries()
                    posts_data = []
                    for fb_post in sorted(fb_posts, key=lambda p: p['scheduled_time']):
                        message = fb_post.get('message', '')
                        fb_post_number = None
                        clean_message = message
                        if message.startswith('#'):
                            try:
                                fb_post_number = int(message.split()[0][1:])
                                clean_message = message.split(' ', 1)[1] if ' ' in message else message
                            except Exception:
                                pass
                        entry = None
                        if fb_post_number:
                            entry = next((e for e in db_entries if e.get('post_number') == fb_post_number), None)
                        dt = _parse_fb_time(fb_post['scheduled_time'])
                        posts_data.append({
                            'fb_post': fb_post,
                            'entry': entry,
                            'weekday': get_hebrew_weekday(fb_post['scheduled_time']),
                            'display_time': dt.strftime('%H:%M %d/%m/%Y') if dt else fb_post['scheduled_time'],
                            'height': calculate_textarea_height(message) if message else 80
                        })
                else:
                    print("   ✓ No holes found")

        posts_data.sort(key=lambda x: x['fb_post']['scheduled_time'])
        print(f"\n6. Rendering template with {len(posts_data)} posts")
        print("=" * 80)
        return render_template('scheduled_content.html', posts=posts_data, had_orphans=had_orphans)

    except Exception as e:
        print(f"\nERROR in scheduled_content: {e}")
        traceback.print_exc()
        print("=" * 80)
        return f'<div class="alert alert-danger text-center py-3">❌ שגיאה: {str(e)}</div>'

def _fb_reschedule(entry, new_text, new_time_dt):
    """Update a scheduled Facebook post, creating a fresh one if the original is gone.
    Returns (new_fb_id, error_string) — error_string is None on success."""
    fb_post_id = entry.get('facebook_post_id')
    try:
        if fb_post_id:
            result = extensions.facebook_handler.update_scheduled_post(fb_post_id, new_text, new_time_dt)
        else:
            result = extensions.facebook_handler.schedule_post(new_text, new_time_dt)
        return result['id'], None
    except Exception as e:
        err_str = str(e)
        if '(#10)' in err_str or 'does not exist' in err_str.lower():
            # Post gone from Facebook — create a fresh one
            try:
                result = extensions.facebook_handler.schedule_post(new_text, new_time_dt)
                print(f"  ✓ Post {entry['id']} re-created on Facebook (was orphaned)")
                return result['id'], None
            except Exception as e2:
                print(f"  ✗ Post {entry['id']} re-creation failed: {e2}")
                return None, str(e2)
        return None, err_str


@app.route('/unschedule_all', methods=['POST'])
def unschedule_all():
    """Return ALL scheduled posts to pending in one go."""
    from concurrent.futures import ThreadPoolExecutor
    print("=" * 80)
    print("UNSCHEDULE ALL")
    print("=" * 80)
    try:
        entries = extensions.db.get_scheduled_entries()
        if not entries:
            return jsonify({'ok': True, 'count': 0})

        # Step 1: delete all from Facebook in parallel
        def fb_delete(entry):
            if not extensions.facebook_handler or not entry.get('facebook_post_id'):
                return
            try:
                extensions.facebook_handler.delete_scheduled_post(entry['facebook_post_id'])
            except Exception as e:
                err_str = str(e)
                if '(#10)' in err_str or 'does not exist' in err_str.lower():
                    print(f"  ⚠ Post {entry['id']} already gone from Facebook")
                else:
                    print(f"  ✗ Error deleting post {entry['id']}: {e}")

        with ThreadPoolExecutor(max_workers=5) as pool:
            list(pool.map(fb_delete, entries))

        # Step 2: mark all as pending and reset counter in a single DB transaction
        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            # Read the MIN post_number before NULLing — that's the first freed slot
            cursor.execute(
                'SELECT MIN(post_number) as mn FROM entries WHERE status IN ("scheduled", "approved") AND post_number IS NOT NULL'
            )
            row = cursor.fetchone()
            min_sched = row['mn'] if row and row['mn'] is not None else None
            cursor.execute(
                'UPDATE entries SET status = "pending", post_number = NULL, facebook_post_id = NULL, scheduled_time = NULL WHERE status IN ("scheduled", "approved")'
            )
            if min_sched is not None:
                cursor.execute('UPDATE post_numbers SET current_number = ? WHERE id = 1', (min_sched,))
                print(f"  Counter reset to {min_sched} (first freed post number)")
            else:
                print("  No post numbers to free, counter unchanged")
            conn.commit()
        finally:
            conn.close()

        count = len(entries)
        print(f"✅ Unscheduled all: {count} posts returned to pending")
        return jsonify({'ok': True, 'count': count})

    except Exception as e:
        print(f"UNSCHEDULE ALL ERROR: {e}")
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/reschedule_canonical', methods=['POST'])
def reschedule_canonical():
    """Redistribute all scheduled posts to canonical dynamic-tier slots.
    Safe version: generates only future slots, runs Facebook calls in parallel,
    uses _fb_reschedule so orphaned entries get re-created instead of lost."""
    from concurrent.futures import ThreadPoolExecutor
    from datetime import timedelta as _td

    if not extensions.scheduler or not extensions.facebook_handler:
        return jsonify({'error': 'Scheduler not available'}), 500
    try:
        extensions.scheduler.update_dynamic_windows()

        raw_entries = extensions.db.get_scheduled_entries()   # sorted by scheduled_time ASC
        if not raw_entries:
            return jsonify({'ok': True, 'updated': 0})

        # Deduplicate by post_number — older corruption can leave multiple DB rows for the
        # same post number.  Keep the entry with the highest ID (most recently written).
        seen_pn: dict = {}
        no_number = []
        for e in raw_entries:
            pn = e.get('post_number')
            if pn is None:
                no_number.append(e)
            elif pn not in seen_pn or e['id'] > seen_pn[pn]['id']:
                seen_pn[pn] = e
        entries = sorted(seen_pn.values(), key=lambda e: e.get('scheduled_time') or '') + no_number
        dupes = len(raw_entries) - len(entries)
        if dupes:
            print(f"  ⚠️  Skipped {dupes} duplicate DB entries (same post_number)")

        n = len(entries)
        windows  = extensions.scheduler.load_posting_windows()
        tz       = extensions.scheduler.timezone
        now      = datetime.now(tz)
        min_time = now + _td(minutes=30)

        # Build N future slots using the canonical window algorithm
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

        def fb_update(args):
            entry, new_time_dt = args
            new_text   = f"#{entry['post_number']} {entry['text']}"
            new_time_s = new_time_dt.strftime('%Y-%m-%d %H:%M:%S')
            new_fb_id, err = _fb_reschedule(entry, new_text, new_time_dt)
            return (entry['id'], new_time_s, new_fb_id, err)

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(fb_update, zip(entries, slots)))

        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            for (eid, new_time_s, new_fb_id, err) in results:
                if new_fb_id:
                    cursor.execute(
                        'UPDATE entries SET scheduled_time = ?, facebook_post_id = ? WHERE id = ?',
                        (new_time_s, new_fb_id, eid)
                    )
                elif err:
                    print(f"  ✗ Canonical reschedule post {eid} failed: {err}")
            conn.commit()
        finally:
            conn.close()

        updated = sum(1 for r in results if r[2])
        print(f"✅ Canonical reschedule: {updated}/{n} posts redistributed")
        return jsonify({'ok': True, 'updated': updated})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/reschedule_today', methods=['POST'])
def reschedule_today():
    """Distribute the next N scheduled posts evenly in a custom time window today."""
    from concurrent.futures import ThreadPoolExecutor
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

        # Facebook requires scheduling at least 10 minutes in the future; use 30 as safety margin.
        min_dt = now + _td(minutes=30)
        if start_dt < min_dt:
            start_dt = min_dt
        if end_dt < start_dt:
            return jsonify({'error': 'Window is entirely in the past'}), 400

        # Get the first `count` scheduled entries (sorted by scheduled_time ASC = soonest first)
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

        def fb_update(args):
            entry, new_time_dt = args
            new_text   = f"#{entry['post_number']} {entry['text']}"
            new_time_s = new_time_dt.strftime('%Y-%m-%d %H:%M:%S')
            new_fb_id, err = _fb_reschedule(entry, new_text, new_time_dt)
            return (entry['id'], new_time_s, new_fb_id, err)

        with ThreadPoolExecutor(max_workers=5) as pool:
            results = list(pool.map(fb_update, zip(entries, target_times)))

        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            for (eid, new_time_s, new_fb_id, err) in results:
                if new_fb_id:
                    cursor.execute(
                        'UPDATE entries SET scheduled_time = ?, facebook_post_id = ? WHERE id = ?',
                        (new_time_s, new_fb_id, eid)
                    )
                elif err:
                    print(f"  ✗ Post {eid} failed: {err}")
            conn.commit()
        finally:
            conn.close()

        updated = sum(1 for r in results if r[2])
        print(f"✅ Reschedule today: {updated}/{n} posts updated")
        return jsonify({'ok': True, 'updated': updated})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/unschedule/<int:entry_id>', methods=['POST'])
def unschedule_entry(entry_id):
    from concurrent.futures import ThreadPoolExecutor
    print("=" * 80)
    print(f"UNSCHEDULE ENTRY {entry_id}")
    print("=" * 80)
    try:
        entries = extensions.db.get_scheduled_entries()
        unscheduled_entry = next((e for e in entries if e['id'] == entry_id), None)

        if not unscheduled_entry:
            flash('❌ Entry not found', 'error')
            return redirect(url_for('scheduled_page'))

        unscheduled_number = unscheduled_entry['post_number']
        unscheduled_time = unscheduled_entry['scheduled_time']

        # Step 1: delete removed post from Facebook
        if extensions.facebook_handler and unscheduled_entry.get('facebook_post_id'):
            try:
                extensions.facebook_handler.delete_scheduled_post(unscheduled_entry['facebook_post_id'])
            except Exception as e:
                print(f"ERROR deleting from Facebook: {e}")

        # Step 2: mark entry as pending in DB
        conn = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE entries
            SET status = 'pending', post_number = NULL, facebook_post_id = NULL, scheduled_time = NULL
            WHERE id = ?
        ''', (entry_id,))
        conn.commit()
        conn.close()

        # Step 3: pre-compute new numbers and times for all following posts.
        # Times chain: each post slides into the timeslot of the removed post.
        following_entries = sorted(
            [e for e in entries if e.get('post_number', 999) > unscheduled_number],
            key=lambda x: x['post_number']
        )

        timezone = pytz.timezone('Asia/Jerusalem')
        updates = []
        prev_time = unscheduled_time
        for entry in following_entries:
            updates.append({
                'entry': entry,
                'new_number': entry['post_number'] - 1,
                'new_time_str': prev_time,
            })
            prev_time = entry['scheduled_time']

        # Step 4: call Facebook API for all posts IN PARALLEL (max 5 at a time)
        # to avoid N sequential HTTP calls timing out the request.
        def fb_update(upd):
            entry       = upd['entry']
            new_time_dt = datetime.fromisoformat(
                upd['new_time_str'].replace('+02:00', '').replace('+03:00', '')
            )
            new_time_dt = timezone.localize(new_time_dt)
            new_text    = f"#{upd['new_number']} {entry['text']}"
            new_fb_id, err = _fb_reschedule(entry, new_text, new_time_dt)
            return (entry['id'], upd['new_number'], upd['new_time_str'], new_fb_id, err)

        if updates and extensions.facebook_handler:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(fb_update, updates))

            # Step 5: update DB in a single transaction
            conn = extensions.db.get_connection()
            try:
                cursor = conn.cursor()
                for (eid, new_num, new_time, new_fb_id, err) in results:
                    if new_fb_id:
                        cursor.execute(
                            'UPDATE entries SET post_number = ?, scheduled_time = ?, facebook_post_id = ? WHERE id = ?',
                            (new_num, new_time, new_fb_id, eid)
                        )
                    else:
                        # Facebook update failed — still shift number/time so list stays consistent
                        cursor.execute(
                            'UPDATE entries SET post_number = ?, scheduled_time = ?, facebook_post_id = NULL WHERE id = ?',
                            (new_num, new_time, eid)
                        )
                conn.commit()
            finally:
                conn.close()

        extensions.db.decrement_post_counter()
        flash('✅ הוחזר להמתנה והפוסטים הבאים הוזזו', 'success')

    except Exception as e:
        print(f"UNSCHEDULE ERROR: {e}")
        traceback.print_exc()
        flash(f'❌ שגיאה: {str(e)}', 'error')

    return redirect(url_for('scheduled_page'))

@app.route('/edit_scheduled/<int:entry_id>', methods=['POST'])
def edit_scheduled_post(entry_id):
    new_text = request.json.get('text', '') if request.is_json else request.form.get('text', '')

    if not new_text:
        if request.headers.get('HX-Request'):
            return '', 400
        flash('❌ טקסט ריק', 'error')
        return redirect(url_for('scheduled_page'))

    entries = extensions.db.get_scheduled_entries()
    entry = next((e for e in entries if e['id'] == entry_id), None)

    if not entry:
        if request.headers.get('HX-Request'):
            return '', 404
        flash('❌ לא נמצא', 'error')
        return redirect(url_for('scheduled_page'))

    try:
        clean_text = new_text
        if clean_text.startswith('#'):
            parts = clean_text.split(' ', 1)
            if len(parts) > 1:
                clean_text = parts[1]

        conn = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET text = ? WHERE id = ?', (clean_text, entry_id))
        conn.commit()
        conn.close()

        if extensions.facebook_handler and entry.get('facebook_post_id'):
            full_text = f"#{entry['post_number']} {clean_text}"
            extensions.facebook_handler.update_scheduled_post(entry['facebook_post_id'], full_text)

        flash('✅ הפוסט עודכן בהצלחה!', 'success')

    except Exception as e:
        traceback.print_exc()
        if request.headers.get('HX-Request'):
            return '', 500
        flash(f'❌ שגיאה: {str(e)}', 'error')

    return redirect(url_for('scheduled_page'))

@app.route('/reorder_posts', methods=['POST'])
def reorder_posts():
    """Reorder scheduled posts by drag-and-drop.
    Body: {"order": [entry_id, entry_id, ...]} — full list in new desired order.
    Redistributes existing time slots to the new positions and updates Facebook in parallel.
    """
    from concurrent.futures import ThreadPoolExecutor

    if not extensions.facebook_handler:
        return jsonify({'error': 'Facebook handler not available'}), 500

    data = request.get_json(silent=True) or {}
    new_order_ids = [int(x) for x in data.get('order', [])]
    if not new_order_ids:
        return jsonify({'error': 'No order provided'}), 400

    try:
        # Sort by scheduled_time — this matches the visual order the user sees.
        # Sorting by post_number instead would be wrong if times and numbers are
        # out of sync (which happens after swap/unschedule operations).
        entries = sorted(extensions.db.get_scheduled_entries(), key=lambda x: x['scheduled_time'])
        entry_map = {e['id']: e for e in entries}

        # Time slots in visual (time-ascending) order
        time_slots = [e['scheduled_time'] for e in entries]

        # Post numbers sorted independently — slot 0 always gets the lowest number.
        # This is robust to any prior DB inconsistency between numbers and times.
        post_numbers = sorted(e['post_number'] for e in entries if e['post_number'] is not None)

        timezone = pytz.timezone('Asia/Jerusalem')

        def parse_time(s):
            if 'T' in s:
                return timezone.localize(datetime.fromisoformat(s.replace('+02:00', '').replace('+03:00', '')))
            return timezone.localize(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))

        print(f"🔀 Reorder: {len(new_order_ids)} ids, {len(entries)} DB entries")
        for i, e in enumerate(entries):
            print(f"   slot {i}: id={e['id']} #={e['post_number']} t={e['scheduled_time']}")

        # Build changes: entry at new position idx gets time_slots[idx] and post_numbers[idx]
        changes = []
        for idx, eid in enumerate(new_order_ids):
            entry = entry_map.get(eid)
            if not entry or idx >= len(time_slots):
                continue
            new_time_str = time_slots[idx]
            new_num = post_numbers[idx] if idx < len(post_numbers) else entry['post_number']
            if entry['scheduled_time'] != new_time_str or entry['post_number'] != new_num:
                changes.append((entry, new_num, new_time_str, parse_time(new_time_str)))
                print(f"   change: id={entry['id']} #{entry['post_number']}→{new_num} {entry['scheduled_time']}→{new_time_str}")

        print(f"   {len(changes)} posts need Facebook update")

        # Update Facebook in parallel for changed entries only
        def fb_update(args):
            entry, new_num, new_time_str, new_time_dt = args
            new_text  = f"#{new_num} {entry['text']}"
            new_fb_id, err = _fb_reschedule(entry, new_text, new_time_dt)
            return (entry['id'], new_num, new_time_str, new_fb_id, err)

        results = []
        if changes:
            with ThreadPoolExecutor(max_workers=5) as pool:
                results = list(pool.map(fb_update, changes))

        # Update DB in a single transaction
        conn = extensions.db.get_connection()
        try:
            cursor = conn.cursor()
            for (eid, new_num, new_time_str, new_fb_id, err) in results:
                if new_fb_id:
                    cursor.execute(
                        'UPDATE entries SET post_number = ?, scheduled_time = ?, facebook_post_id = ? WHERE id = ?',
                        (new_num, new_time_str, new_fb_id, eid)
                    )
                else:
                    cursor.execute(
                        'UPDATE entries SET post_number = ?, scheduled_time = ?, facebook_post_id = NULL WHERE id = ?',
                        (new_num, new_time_str, eid)
                    )
            conn.commit()
        finally:
            conn.close()

        failed = [r for r in results if r[4] is not None]
        print(f"✅ Reorder complete: {len(results) - len(failed)} updated, {len(failed)} failed")
        return jsonify({'ok': True, 'updated': len(results) - len(failed), 'failed': len(failed)})

    except Exception as e:
        traceback.print_exc()
        return jsonify({'error': str(e)}), 500


@app.route('/swap_posts/<int:entry_id>/<direction>', methods=['POST'])
def swap_posts(entry_id, direction):
    if not extensions.scheduler or not extensions.facebook_handler:
        if request.headers.get('HX-Request'):
            return '', 500
        flash('❌ Scheduler not available', 'error')
        return redirect(url_for('scheduled_page'))

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
        target_entry = entries[target_idx]
        current_num = current_entry['post_number']
        target_num = target_entry['post_number']
        current_time_str = current_entry['scheduled_time']
        target_time_str = target_entry['scheduled_time']

        timezone = pytz.timezone('Asia/Jerusalem')

        def parse_time(s):
            if 'T' in s:
                return timezone.localize(datetime.fromisoformat(s.replace('+02:00', '').replace('+03:00', '')))
            return timezone.localize(datetime.strptime(s, '%Y-%m-%d %H:%M:%S'))

        current_time_dt = parse_time(current_time_str)
        target_time_dt = parse_time(target_time_str)

        conn = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET post_number = ?, scheduled_time = ? WHERE id = ?',
                       (target_num, target_time_str, current_entry['id']))
        cursor.execute('UPDATE entries SET post_number = ?, scheduled_time = ? WHERE id = ?',
                       (current_num, current_time_str, target_entry['id']))
        conn.commit()
        conn.close()

        if current_entry.get('facebook_post_id'):
            extensions.facebook_handler.update_scheduled_post(
                current_entry['facebook_post_id'],
                f"#{target_num} {current_entry['text']}",
                target_time_dt
            )
        if target_entry.get('facebook_post_id'):
            extensions.facebook_handler.update_scheduled_post(
                target_entry['facebook_post_id'],
                f"#{current_num} {target_entry['text']}",
                current_time_dt
            )

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
            'facebook_access_token': request.form.get('facebook_access_token', ''),
            'facebook_app_secret':   request.form.get('facebook_app_secret', ''),
            'resend_api_key':        request.form.get('resend_api_key', ''),
            'openai_api_key':        request.form.get('openai_api_key', ''),
        }

        config.update({
            'google_sheet_id':                request.form.get('google_sheet_id', ''),
            'google_credentials_file':        request.form.get('google_credentials_file', 'credentials.json'),
            'read_from_date':                 request.form.get('read_from_date', ''),
            'facebook_page_id':               request.form.get('facebook_page_id', ''),
            'skip_shabbat':                   request.form.get('skip_shabbat') == 'on',
            'skip_jewish_holidays':           request.form.get('skip_jewish_holidays') == 'on',
            'notifications_enabled':          request.form.get('notifications_enabled') == 'on',
            'resend_from_email':              request.form.get('resend_from_email', ''),
            'notification_emails':            [e.strip() for e in request.form.get('notification_emails', '').split(',') if e.strip()],
            'pending_threshold':              int(request.form.get('pending_threshold', 20)),
            'app_url':                        request.form.get('app_url', ''),
            'comments_filter_enabled':        request.form.get('comments_filter_enabled') == 'on',
            'daily_api_limit':                int(request.form.get('daily_api_limit', 1000)),
            'batch_size':                     int(request.form.get('batch_size', 50)),
            'comment_flagged_notification_threshold': int(request.form.get('comment_flagged_notification_threshold', 0)),
            'comment_notification_threshold': int(request.form.get('comment_notification_threshold', 0)),
            'comment_retention_days':         int(request.form.get('comment_retention_days', 7)),
            'webhook_verify_token':           request.form.get('webhook_verify_token', ''),
            'webhook_batch_size':             int(request.form.get('webhook_batch_size', 10)),
            'webhook_batch_timeout_minutes':  int(request.form.get('webhook_batch_timeout_minutes', 5)),
        })

        for key, value in secret_fields.items():
            if value.strip():
                config[key] = value.strip()

        tiers_text = request.form.get('dynamic_tiers', '')
        if tiers_text.strip():
            dynamic_tiers = []
            for line in tiers_text.strip().split('\n'):
                line = line.strip()
                if not line or '|' not in line:
                    continue
                parts = line.split('|')
                try:
                    max_posts = int(parts[0].strip())
                    windows = [w.strip() for w in parts[1].strip().split(',') if w.strip()]
                    if windows:
                        dynamic_tiers.append({'max_posts': max_posts, 'windows': windows})
                except ValueError:
                    continue
            if dynamic_tiers:
                dynamic_tiers.sort(key=lambda t: t['max_posts'])
                config['dynamic_tiers'] = dynamic_tiers

        save_config(config)
        init_handlers()

        flash('✅ הגדרות נשמרו בהצלחה!', 'success')
        return redirect(url_for('settings_page'))

    config = load_config()
    current_number = extensions.db.get_current_post_number()

    secret_keys = ['facebook_access_token', 'facebook_app_secret', 'resend_api_key', 'openai_api_key']
    for key in secret_keys:
        if config.get(key):
            config[key + '_set'] = True
            config[key] = ''
        else:
            config[key + '_set'] = False

    return render_template('settings.html', config=config, current_number=current_number)

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
