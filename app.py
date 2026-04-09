from flask import Flask, render_template, request, redirect, url_for, jsonify, flash, session
import threading
import traceback
import os
import logging
from logging.handlers import RotatingFileHandler
from datetime import datetime, timedelta
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
    extensions.db.approve_entry(entry_id, edited_text, 'admin')

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

        entries_to_remove = [e for e in db_entries if e.get('post_number') and e['post_number'] not in fb_posts_by_number]
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

        if had_orphans and len(fb_posts_sorted) > 0:
            print("   Renumbering remaining posts to fill gaps...")
            conn = extensions.db.get_connection()
            cursor = conn.cursor()

            fb_post_numbers = sorted([
                int(p['message'].split()[0][1:])
                for p in fb_posts_sorted
                if p.get('message', '').startswith('#')
                and p['message'].split()[0][1:].isdigit()
            ])

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
                            extensions.facebook_handler.update_scheduled_post(fb_post['id'], new_text)
                            fb_post['message'] = new_text
                            print(f"   Renumbered #{current_number} → #{expected}")
                        except Exception as e:
                            print(f"   ✗ Error renumbering: {e}")

            conn.commit()
            conn.close()

            print("   Re-fetching from Facebook...")
            fb_posts = extensions.facebook_handler.get_scheduled_posts()
            fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])

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
                    'scheduled_time': fb_post['scheduled_time']
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

        # Fill holes: move posts to earliest available slots
        print("\n5. Checking for holes in schedule...")
        if extensions.scheduler and len(fb_posts) > 0:

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
                    conn = extensions.db.get_connection()
                    cursor = conn.cursor()
                    for fb_post, new_slot in posts_to_update:
                        message = fb_post.get('message', '')
                        post_number = None
                        if message.startswith('#'):
                            try:
                                post_number = int(message.split()[0][1:])
                            except Exception:
                                pass
                        if post_number:
                            cursor.execute('UPDATE entries SET scheduled_time = ? WHERE post_number = ?',
                                           (new_slot.isoformat(), post_number))
                            try:
                                extensions.facebook_handler.update_scheduled_post(fb_post['id'], message, new_slot)
                            except Exception as e:
                                print(f"       ✗ Error: {e}")
                    conn.commit()
                    conn.close()

                    fb_posts = extensions.facebook_handler.get_scheduled_posts()
                    posts_data = []
                    for fb_post in fb_posts:
                        message = fb_post.get('message', '')
                        post_number = None
                        if message.startswith('#'):
                            try:
                                post_number = int(message.split()[0][1:])
                            except Exception:
                                pass
                        if post_number:
                            entry = next((e for e in db_entries if e.get('post_number') == post_number), None)
                            if entry:
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

@app.route('/unschedule/<int:entry_id>', methods=['POST'])
def unschedule_entry(entry_id):
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

        if extensions.facebook_handler and unscheduled_entry.get('facebook_post_id'):
            try:
                extensions.facebook_handler.delete_scheduled_post(unscheduled_entry['facebook_post_id'])
            except Exception as e:
                print(f"ERROR deleting from Facebook: {e}")

        conn = extensions.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE entries
            SET status = 'pending', post_number = NULL, facebook_post_id = NULL, scheduled_time = NULL
            WHERE id = ?
        ''', (entry_id,))
        conn.commit()
        conn.close()

        following_entries = sorted(
            [e for e in entries if e.get('post_number', 999) > unscheduled_number],
            key=lambda x: x['post_number']
        )

        previous_time_str = unscheduled_time
        timezone = pytz.timezone('Asia/Jerusalem')

        for entry in following_entries:
            old_number = entry['post_number']
            new_number = old_number - 1
            current_time_str = entry['scheduled_time']
            new_time_str = previous_time_str
            try:
                new_time_dt = datetime.fromisoformat(new_time_str.replace('+02:00', '').replace('+03:00', ''))
                new_time_dt = timezone.localize(new_time_dt)
                new_text = f"#{new_number} {entry['text']}"
                extensions.facebook_handler.update_scheduled_post(entry['facebook_post_id'], new_text, new_time_dt)
                conn = extensions.db.get_connection()
                cursor = conn.cursor()
                cursor.execute('UPDATE entries SET post_number = ?, scheduled_time = ? WHERE id = ?',
                               (new_number, new_time_str, entry['id']))
                conn.commit()
                conn.close()
                previous_time_str = current_time_str
            except Exception as e:
                print(f"  ✗ Error shifting post {entry['id']}: {e}")
                traceback.print_exc()

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
