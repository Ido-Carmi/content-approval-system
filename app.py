from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from pathlib import Path
import json
import threading
import traceback
import time
import schedule
from datetime import datetime, timedelta
import pytz

# Import your existing handlers
from database import Database
from scheduler import Scheduler
from facebook_handler import FacebookHandler
from sheets_handler import SheetsHandler
from notifications import NotificationHandler

app = Flask(__name__)
app.secret_key = 'your-secret-key-change-this-in-production'

# Disable static file caching for development
app.config['SEND_FILE_MAX_AGE_DEFAULT'] = 0

# Favicon route to prevent 404 errors in browser console
@app.route('/favicon.ico')
def favicon():
    return '', 204  # No Content response

# Initialize handlers
db = Database()
scheduler = None
facebook_handler = None
sheets_handler = None
notifications = NotificationHandler()

from config import load_config, save_config

def init_handlers():
    """Initialize handlers from config"""
    global scheduler, facebook_handler, sheets_handler
    
    config = load_config()
    
    # Facebook handler
    if config.get('facebook_page_id') and config.get('facebook_access_token'):
        try:
            facebook_handler = FacebookHandler(
                config['facebook_page_id'],
                config['facebook_access_token']
            )
            scheduler = Scheduler(db, facebook_handler)
        except Exception as e:
            print(f"Failed to init Facebook: {e}")
    
    # Sheets handler
    if config.get('google_sheet_id'):
        try:
            sheets_handler = SheetsHandler(
                config['google_sheet_id'],
                config.get('google_credentials_file', 'credentials.json')
            )
        except Exception as e:
            print(f"Failed to init Sheets: {e}")

# Initialize on startup
init_handlers()

def calculate_textarea_height(text: str) -> int:
    """Calculate optimal height for textarea based on actual content"""
    if not text:
        return 80
    
    # With 700px+ width cards, we fit ~90-100 chars per line
    # Account for Hebrew text which may wrap differently
    chars_per_line = 85
    
    lines = text.count('\n') + 1
    wrapped_lines = 0
    for line in text.split('\n'):
        if len(line) == 0:
            wrapped_lines += 1
        else:
            # Calculate how many visual lines this text line will take
            wrapped_lines += max(1, (len(line) + chars_per_line - 1) // chars_per_line)
    
    total_lines = max(lines, wrapped_lines)
    
    # Line height: 1.4em × 0.95rem ≈ 21px per line
    # Add padding: 0.4rem top + 0.4rem bottom ≈ 13px
    height = int(total_lines * 21) + 15
    
    return max(80, min(400, height))

def get_hebrew_weekday(date_str: str) -> str:
    """Get Hebrew weekday name from ISO datetime string"""
    try:
        dt = datetime.fromisoformat(date_str)
        weekdays = ['שני', 'שלישי', 'רביעי', 'חמישי', 'שישי', 'שבת', 'ראשון']
        return weekdays[dt.weekday()]
    except:
        return ''

# ============================================================================
# ROUTES - Review Page
# ============================================================================

@app.route('/')
@app.route('/review')
def review_page():
    """Review pending entries"""
    print("=" * 50)
    print("REVIEW PAGE - DEBUG START")
    print("=" * 50)
    
    try:
        print("Step 1: Cleaning up old denied entries...")
        db.cleanup_old_denied()
        print("✓ Cleanup complete")
    except Exception as e:
        print(f"✗ Cleanup failed: {e}")
    
    try:
        print("Step 2: Getting pending entries...")
        entries = db.get_pending_entries()
        print(f"✓ Got {len(entries) if entries else 0} entries")
        if entries:
            print(f"  First entry: {entries[0]}")
    except Exception as e:
        print(f"✗ Getting entries failed: {e}")
        entries = []
    
    try:
        print("Step 3: Calculating heights...")
        for i, entry in enumerate(entries):
            entry['height'] = calculate_textarea_height(entry['text'])
            if i < 3:  # Print first 3
                print(f"  Entry {entry['id']}: height={entry['height']}px, text_length={len(entry['text'])}")
        print(f"✓ Heights calculated for {len(entries)} entries")
    except Exception as e:
        print(f"✗ Height calculation failed: {e}")
    
    try:
        print("Step 4: Loading config...")
        config = load_config()
        print(f"✓ Config loaded: {list(config.keys()) if config else 'None'}")
        if config:
            print(f"  Last sync: {config.get('last_sync', 'Never')}")
    except Exception as e:
        print(f"✗ Config loading failed: {e}")
        config = {}
    
    print("Step 5: Rendering template...")
    print(f"  entries={len(entries) if entries else 0}")
    print(f"  config={'loaded' if config else 'empty'}")
    print("=" * 50)
    print("REVIEW PAGE - DEBUG END")
    print("=" * 50)
    
    try:
        return render_template('review.html', 
                             entries=entries,
                             config=config)
    except Exception as e:
        print(f"✗ Template rendering failed: {e}")
        traceback.print_exc()
        return f"Error rendering template: {e}", 500

@app.route('/approve/<int:entry_id>', methods=['POST'])
def approve_entry(entry_id):
    """Approve an entry and schedule to Facebook"""
    edited_text = request.form.get('text', '') or request.get_json().get('text', '')
    
    # Approve and assign number
    db.approve_entry(entry_id, edited_text, 'admin')
    
    # Get assigned number
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT post_number FROM entries WHERE id = ?', (entry_id,))
    result = cursor.fetchone()
    post_number = result['post_number'] if result else 1
    conn.close()
    
    # Format with number
    formatted_text = f"#{post_number} {edited_text}"
    
    # Schedule to Facebook in background thread
    def schedule_in_background():
        if scheduler:
            try:
                scheduler.schedule_post_to_facebook(entry_id, formatted_text)
            except Exception as e:
                print(f"Background scheduling error: {e}")
    
    # Start background thread
    thread = threading.Thread(target=schedule_in_background, daemon=True)
    thread.start()
    
    # If HTMX request, return empty immediately (doesn't wait for Facebook)
    if request.headers.get('HX-Request'):
        return '', 200
    
    return redirect(url_for('review_page'))

@app.route('/deny/<int:entry_id>', methods=['POST'])
def deny_entry(entry_id):
    """Deny an entry"""
    db.deny_entry(entry_id, 'admin')
    flash('❌ נדחה', 'info')
    
    # If HTMX request, return empty (removes the card)
    if request.headers.get('HX-Request'):
        return '', 200
    
    return redirect(url_for('review_page'))

@app.route('/sync', methods=['POST'])
def sync_now():
    """Manually sync with Google Sheets"""
    if not sheets_handler:
        flash('❌ Google Sheets לא מחובר', 'error')
        return redirect(url_for('review_page'))
    
    try:
        config = load_config()
        read_from_date = config.get('read_from_date', '').strip()
        
        # Convert date picker format (YYYY-MM-DD) to DD/MM/YYYY 00:00:00
        # ONLY if user actually set a date
        last_timestamp = None
        if read_from_date:
            try:
                # Date picker returns YYYY-MM-DD
                dt = datetime.strptime(read_from_date, '%Y-%m-%d')
                # Convert to DD/MM/YYYY 00:00:00 for sheets_handler
                last_timestamp = dt.strftime('%d/%m/%Y 00:00:00')
                print(f"DEBUG: Filtering from date: {last_timestamp}")
            except Exception as e:
                print(f"DEBUG: Date parse error: {e}")
                flash(f'⚠️ פורמט תאריך שגוי', 'warning')
        else:
            print("DEBUG: No date filter - fetching ALL entries")
        
        new_entries = sheets_handler.fetch_new_entries(last_timestamp)
        print(f"DEBUG: Fetched {len(new_entries)} entries from sheets")
        added_count = 0
        
        for entry in new_entries:
            if db.add_entry(entry['timestamp'], entry['text']):
                added_count += 1
                print(f"DEBUG: Added entry: {entry['timestamp']}")
        
        print(f"DEBUG: Added {added_count} new entries to database")
        
        # Update last sync
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
    """View scheduled posts - ALWAYS syncs with Facebook"""
    print("=" * 80)
    print("SCHEDULED PAGE - SYNCING WITH FACEBOOK")
    print("=" * 80)
    
    if not facebook_handler:
        print("ERROR: Facebook handler is None")
        flash('❌ Facebook לא מחובר', 'error')
        return redirect(url_for('review_page'))
    
    try:
        # Track if we made changes
        posts_deleted = False
        holes_filled = False
        
        print("\n1. Fetching scheduled posts from Facebook...")
        fb_posts = facebook_handler.get_scheduled_posts()
        print(f"   Got {len(fb_posts)} posts from Facebook")
        
        print("\n2. Fetching scheduled entries from database...")
        db_entries = db.get_scheduled_entries()
        print(f"   Got {len(db_entries)} entries from database")
        
        # Create lookup of Facebook post IDs
        fb_post_ids = set(fb_post['id'] for fb_post in fb_posts)
        
        print("\n3. Syncing database with Facebook...")
        # Match DB entries with Facebook posts by POST NUMBER, not facebook_post_id
        # This prevents "orphaned entries" when Facebook changes post IDs
        
        fb_posts_by_number = {}
        for fb_post in fb_posts:
            message = fb_post.get('message', '')
            if message.startswith('#'):
                try:
                    num_str = message.split()[0][1:]
                    post_number = int(num_str)
                    fb_posts_by_number[post_number] = fb_post
                except:
                    pass
        
        print(f"   Found {len(fb_posts_by_number)} Facebook posts with post numbers")
        
        # Find truly orphaned entries (post number not on Facebook)
        entries_to_remove = []
        for entry in db_entries:
            post_num = entry.get('post_number')
            if post_num and post_num not in fb_posts_by_number:
                entries_to_remove.append(entry)
        
        had_orphans = len(entries_to_remove) > 0
        
        if entries_to_remove:
            print(f"   Found {len(entries_to_remove)} truly orphaned entries (deleted on Facebook)")
            
            # Sort by post number
            entries_to_remove.sort(key=lambda x: x.get('post_number', 999))
            
            conn = db.get_connection()
            cursor = conn.cursor()
            
            # Collect all deleted post numbers
            deleted_numbers = [entry['post_number'] for entry in entries_to_remove]
            print(f"   Deleted post numbers: {deleted_numbers}")
            
            # Delete all orphaned entries from DB
            for entry in entries_to_remove:
                print(f"     Deleting entry {entry['id']} (post #{entry['post_number']})")
                cursor.execute('DELETE FROM entries WHERE id = ?', (entry['id'],))
            
            # Decrement counter by number of deletions
            cursor.execute(f'UPDATE post_numbers SET current_number = current_number - {len(entries_to_remove)} WHERE id = 1')
            print(f"   ✓ Decremented counter by {len(entries_to_remove)}")
            
            conn.commit()
            conn.close()
            
            # Refresh db_entries
            db_entries = db.get_scheduled_entries()
            
            print(f"   ✓ Deleted {len(entries_to_remove)} orphaned entries")
            print(f"   ✓ {len(db_entries)} entries remain in DB")
            print(f"   ⚠️  Will rebuild post numbers from Facebook...")
        else:
            print("   ✓ No orphaned entries found")
        
        print("\n4. Matching and syncing Facebook posts with database entries...")
        posts_data = []
        
        # Sort Facebook posts by scheduled time to get correct sequence
        fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])
        
        # If we had orphans, renumber remaining posts to fill gaps
        if had_orphans and len(fb_posts_sorted) > 0:
            print("   Renumbering remaining posts to fill gaps...")
            
            conn = db.get_connection()
            cursor = conn.cursor()
            
            # Get the lowest post number that's still on Facebook
            fb_post_numbers = []
            for fb_post in fb_posts_sorted:
                message = fb_post.get('message', '')
                if message.startswith('#'):
                    try:
                        num = int(message.split()[0][1:])
                        fb_post_numbers.append(num)
                    except:
                        pass
            
            if fb_post_numbers:
                fb_post_numbers.sort()
                lowest_fb_number = fb_post_numbers[0]
                print(f"   Lowest post number on Facebook: #{lowest_fb_number}")
                
                # Renumber all posts sequentially starting from lowest
                for i, (fb_post, expected_number) in enumerate(zip(fb_posts_sorted, range(lowest_fb_number, lowest_fb_number + len(fb_posts_sorted)))):
                    message = fb_post.get('message', '')
                    current_number = None
                    clean_message = message
                    
                    if message.startswith('#'):
                        try:
                            num_str = message.split()[0][1:]
                            current_number = int(num_str)
                            clean_message = message.split(' ', 1)[1] if ' ' in message else message
                        except:
                            pass
                    
                    if current_number and current_number != expected_number:
                        print(f"   Renumbering post #{current_number} → #{expected_number}")
                        
                        # Update Facebook with new number
                        new_text = f"#{expected_number} {clean_message}"
                        try:
                            facebook_handler.update_scheduled_post(
                                fb_post['id'],
                                new_text
                            )
                            print(f"     ✓ Updated Facebook")
                            
                            # Update message in fb_post for later matching
                            fb_post['message'] = new_text
                        except Exception as e:
                            print(f"     ✗ Error updating Facebook: {e}")
            
            conn.commit()
            conn.close()
            
            # Re-fetch to get updated post numbers
            print("   Re-fetching from Facebook...")
            fb_posts = facebook_handler.get_scheduled_posts()
            fb_posts_sorted = sorted(fb_posts, key=lambda p: p['scheduled_time'])
        
        # Now match/sync entries with updated Facebook posts
        for idx, fb_post in enumerate(fb_posts_sorted):
            # Extract post number from Facebook message
            message = fb_post.get('message', '')
            fb_post_number = None
            clean_message = message
            
            if message.startswith('#'):
                try:
                    num_str = message.split()[0][1:]
                    fb_post_number = int(num_str)
                    clean_message = message.split(' ', 1)[1] if ' ' in message else message
                except:
                    pass
            
            # Match DB entry by POST NUMBER (not facebook_post_id!)
            entry = None
            if fb_post_number:
                entry = next((e for e in db_entries if e.get('post_number') == fb_post_number), None)
            
            # If entry found, UPDATE its facebook_post_id from Facebook
            if entry:
                old_fb_id = entry.get('facebook_post_id')
                new_fb_id = fb_post['id']
                
                if old_fb_id != new_fb_id:
                    print(f"   Syncing entry {entry['id']}: FB post ID changed")
                    print(f"     Old: {old_fb_id}")
                    print(f"     New: {new_fb_id}")
                    
                    conn = db.get_connection()
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
            
            # If no DB entry exists, create one
            elif fb_post_number:
                print(f"   Creating entry for Facebook post #{fb_post_number}")
                conn = db.get_connection()
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
            
            # Format display time
            weekday = get_hebrew_weekday(fb_post['scheduled_time'])
            scheduled_time_str = fb_post['scheduled_time']
            if '+' in scheduled_time_str:
                scheduled_time_str = scheduled_time_str.split('+')[0]
            if 'T' in scheduled_time_str:
                try:
                    dt = datetime.fromisoformat(scheduled_time_str.split('+')[0])
                    display_time = dt.strftime('%H:%M %d/%m/%Y')
                except:
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
        if scheduler and len(fb_posts) > 0:
            
            def _parse_fb_time(time_str):
                """Parse Facebook scheduled_time to timezone-aware datetime in Israel tz"""
                try:
                    dt = datetime.fromisoformat(time_str)
                    if dt.tzinfo is None:
                        dt = scheduler.timezone.localize(dt)
                    return dt.astimezone(scheduler.timezone)
                except (ValueError, TypeError):
                    return None
            
            # Parse all scheduled times as timezone-aware
            parsed_posts = []
            for p in fb_posts:
                dt = _parse_fb_time(p['scheduled_time'])
                if dt:
                    parsed_posts.append((p, dt))
            
            parsed_posts.sort(key=lambda x: x[1])
            
            if parsed_posts:
                windows = scheduler.load_posting_windows()
                now = datetime.now(scheduler.timezone)
                
                # Generate N valid future slots (where N = number of scheduled posts)
                valid_slots = []
                current_date = now.date()
                
                while len(valid_slots) < len(parsed_posts):
                    if not scheduler.should_skip_date(current_date):
                        for window_time in windows:
                            slot = scheduler.timezone.localize(datetime.combine(current_date, window_time))
                            if slot > now:
                                valid_slots.append(slot)
                                if len(valid_slots) >= len(parsed_posts):
                                    break
                    current_date += timedelta(days=1)
                    if (current_date - now.date()).days > 365:
                        break
                
                valid_slots = valid_slots[:len(parsed_posts)]
                
                # Check if posts need to be moved to fill holes
                posts_to_update = []
                
                for (fb_post, current_time), target_slot in zip(parsed_posts, valid_slots):
                    if abs((current_time - target_slot).total_seconds()) > 60:
                        posts_to_update.append((fb_post, target_slot))
                
                if posts_to_update:
                    print(f"   Found holes! Moving {len(posts_to_update)} posts to fill gaps...")
                    
                    conn = db.get_connection()
                    cursor = conn.cursor()
                    
                    for fb_post, new_slot in posts_to_update:
                        old_time_str = fb_post['scheduled_time']
                        new_time_str = new_slot.isoformat()
                        
                        # Extract post number
                        message = fb_post.get('message', '')
                        post_number = None
                        if message.startswith('#'):
                            try:
                                post_number = int(message.split()[0][1:])
                            except:
                                pass
                        
                        if post_number:
                            print(f"     Post #{post_number}: {old_time_str} → {new_time_str}")
                            
                            # Update database
                            cursor.execute('''
                                UPDATE entries 
                                SET scheduled_time = ? 
                                WHERE post_number = ?
                            ''', (new_time_str, post_number))
                            
                            # Update Facebook
                            try:
                                facebook_handler.update_scheduled_post(
                                    fb_post['id'],
                                    fb_post['message'],
                                    new_slot
                                )
                                print(f"       ✓ Updated")
                            except Exception as e:
                                print(f"       ✗ Error: {e}")
                    
                    conn.commit()
                    conn.close()
                    
                    print(f"   ✓ Holes filled, re-fetching from Facebook...")
                    
                    # Re-fetch to get updated times
                    fb_posts = facebook_handler.get_scheduled_posts()
                    
                    # Rebuild posts_data with new times
                    posts_data = []
                    for fb_post in fb_posts:
                        message = fb_post.get('message', '')
                        post_number = None
                        if message.startswith('#'):
                            try:
                                post_number = int(message.split()[0][1:])
                            except:
                                pass
                        
                        if post_number:
                            entry = next((e for e in db_entries if e.get('post_number') == post_number), None)
                            if entry:
                                weekday = get_hebrew_weekday(fb_post['scheduled_time'])
                                dt = _parse_fb_time(fb_post['scheduled_time'])
                                display_time = dt.strftime('%H:%M %d/%m/%Y') if dt else fb_post['scheduled_time']
                                
                                posts_data.append({
                                    'fb_post': fb_post,
                                    'entry': entry,
                                    'weekday': weekday,
                                    'display_time': display_time,
                                    'height': calculate_textarea_height(fb_post['message']) if fb_post.get('message') else 80
                                })
                else:
                    print("   ✓ No holes found")
        
        # Sort by scheduled time
        posts_data.sort(key=lambda x: x['fb_post']['scheduled_time'])
        
        print(f"\n6. Rendering template with {len(posts_data)} posts")
        if had_orphans:
            print("   ⚠️  Orphaned entries were found and deleted")
        print("=" * 80)
        
        return render_template('scheduled.html', 
                             posts=posts_data, 
                             had_orphans=had_orphans)
        
    except Exception as e:
        print(f"\nERROR in scheduled_page: {e}")
        traceback.print_exc()
        print("=" * 80)
        flash(f'❌ שגיאה: {str(e)}', 'error')
        return redirect(url_for('review_page'))

@app.route('/test-js')
def test_js_page():
    """JavaScript test page for debugging"""
    return render_template('test_js.html')

@app.route('/unschedule/<int:entry_id>', methods=['POST'])
def unschedule_entry(entry_id):
    """Unschedule a post and return to pending, shift all following posts up"""
    print("=" * 80)
    print("UNSCHEDULE ENTRY - START")
    print(f"Entry ID: {entry_id}")
    print("=" * 80)
    
    try:
        # Get the entry being unscheduled
        entries = db.get_scheduled_entries()
        print(f"Total scheduled entries in DB: {len(entries)}")
        
        unscheduled_entry = next((e for e in entries if e['id'] == entry_id), None)
        
        if not unscheduled_entry:
            print(f"ERROR: Entry {entry_id} not found in database")
            flash('❌ Entry not found', 'error')
            return redirect(url_for('scheduled_page'))
        
        print(f"Found entry: #{unscheduled_entry['post_number']} - {unscheduled_entry['text'][:50]}...")
        
        unscheduled_number = unscheduled_entry['post_number']
        unscheduled_time = unscheduled_entry['scheduled_time']
        print(f"Post number: {unscheduled_number}")
        print(f"Scheduled time: {unscheduled_time}")
        
        # Delete from Facebook
        print("Deleting from Facebook...")
        if facebook_handler and unscheduled_entry.get('facebook_post_id'):
            try:
                fb_post_id = unscheduled_entry['facebook_post_id']
                print(f"Facebook post ID: {fb_post_id}")
                facebook_handler.delete_scheduled_post(fb_post_id)
                print("✓ Deleted from Facebook")
            except Exception as e:
                print(f"ERROR deleting from Facebook: {e}")
                traceback.print_exc()
        else:
            print("No Facebook post ID, skipping deletion")
        
        # Update database - set to pending
        print("Setting entry to pending in database...")
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE entries 
            SET status = 'pending', post_number = NULL, facebook_post_id = NULL, scheduled_time = NULL
            WHERE id = ?
        ''', (entry_id,))
        conn.commit()
        conn.close()
        print("✓ Entry set to pending")
        
        # Get all posts scheduled AFTER this one (by post number)
        following_entries = [e for e in entries if e.get('post_number', 999) > unscheduled_number]
        following_entries.sort(key=lambda x: x['post_number'])
        print(f"Found {len(following_entries)} posts scheduled after #{unscheduled_number}")
        
        if following_entries:
            print("Shifting following posts...")
            
            # CASCADE LOGIC:
            # Each post takes the previous post's time slot
            # First post takes the unscheduled post's time slot
            
            # Start with the freed time slot
            previous_time_str = unscheduled_time
            
            for i, entry in enumerate(following_entries):
                old_number = entry['post_number']
                new_number = old_number - 1
                current_time_str = entry['scheduled_time']
                new_time_str = previous_time_str  # Take previous post's slot
                
                print(f"\nShifting post #{old_number} → #{new_number}")
                print(f"  Entry ID: {entry['id']}")
                print(f"  Text: {entry['text'][:50]}...")
                print(f"  Current time: {current_time_str}")
                print(f"  New time: {new_time_str}")
                
                try:
                    # Parse to datetime
                    timezone = pytz.timezone('Asia/Jerusalem')
                    
                    new_time_dt = datetime.fromisoformat(new_time_str.replace('+02:00', '').replace('+03:00', ''))
                    new_time_dt = timezone.localize(new_time_dt)
                    
                    # Update Facebook FIRST (before updating DB)
                    new_text = f"#{new_number} {entry['text']}"
                    print(f"  Updating Facebook with: {new_text[:50]}...")
                    facebook_handler.update_scheduled_post(
                        entry['facebook_post_id'],
                        new_text,
                        new_time_dt
                    )
                    print(f"  ✓ Facebook updated")
                    
                    # Update database (keep same facebook_post_id)
                    conn = db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute('''
                        UPDATE entries 
                        SET post_number = ?, scheduled_time = ?
                        WHERE id = ?
                    ''', (new_number, new_time_str, entry['id']))
                    conn.commit()
                    conn.close()
                    print(f"  ✓ Database updated")
                    
                    # Next post will take this post's current time
                    previous_time_str = current_time_str
                    
                except Exception as e:
                    print(f"  ✗ Error shifting post {entry['id']}: {e}")
                    traceback.print_exc()
        
        # Decrement global counter
        print("\nDecrementing global post counter...")
        db.decrement_post_counter()
        print("✓ Counter decremented")
        
        print("\n⚠️  IMPORTANT: The page will re-sync with Facebook to refresh IDs")
        
        flash('✅ הוחזר להמתנה והפוסטים הבאים הוזזו', 'success')
        print("=" * 80)
        print("UNSCHEDULE ENTRY - SUCCESS")
        print("=" * 80)
        
    except Exception as e:
        print("=" * 80)
        print("UNSCHEDULE ENTRY - ERROR")
        print(f"ERROR: {e}")
        traceback.print_exc()
        print("=" * 80)
        flash(f'❌ שגיאה: {str(e)}', 'error')
    
    return redirect(url_for('scheduled_page'))

@app.route('/edit_scheduled/<int:entry_id>', methods=['POST'])
def edit_scheduled_post(entry_id):
    """Edit a scheduled post - HTMX compatible"""
    print("=" * 80)
    print("EDIT SCHEDULED POST - START")
    print(f"Entry ID: {entry_id}")
    print(f"Request method: {request.method}")
    print(f"Content-Type: {request.content_type}")
    print(f"Is JSON: {request.is_json}")
    print("=" * 80)
    
    # Get new text from request
    new_text = request.json.get('text', '') if request.is_json else request.form.get('text', '')
    print(f"Received text length: {len(new_text)}")
    print(f"Text preview: {new_text[:100]}..." if len(new_text) > 100 else f"Text: {new_text}")
    
    if not new_text:
        print("ERROR: Empty text received")
        if request.headers.get('HX-Request'):
            return '', 400
        flash('❌ טקסט ריק', 'error')
        return redirect(url_for('scheduled_page'))
    
    # Get entry from database
    print("Fetching entry from database...")
    entries = db.get_scheduled_entries()
    print(f"Total scheduled entries: {len(entries)}")
    entry = next((e for e in entries if e['id'] == entry_id), None)
    
    if not entry:
        print(f"ERROR: Entry {entry_id} not found")
        if request.headers.get('HX-Request'):
            return '', 404
        flash('❌ לא נמצא', 'error')
        return redirect(url_for('scheduled_page'))
    
    print(f"Found entry: #{entry['post_number']} - {entry['text'][:50]}...")
    print(f"Facebook post ID: {entry.get('facebook_post_id')}")
    
    # Update database
    try:
        # Strip post number from new_text if it exists
        clean_text = new_text
        if clean_text.startswith('#'):
            print("Text starts with #, stripping post number...")
            parts = clean_text.split(' ', 1)
            if len(parts) > 1:
                clean_text = parts[1]
                print(f"Stripped number, clean text: {clean_text[:50]}...")
        
        # Update text in database (without number)
        print("Updating database...")
        conn = db.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET text = ? WHERE id = ?', (clean_text, entry_id))
        conn.commit()
        conn.close()
        print("✓ Database updated")
        
        # Update on Facebook with post number
        if scheduler and facebook_handler and entry.get('facebook_post_id'):
            full_text = f"#{entry['post_number']} {clean_text}"
            print(f"Updating Facebook...")
            print(f"Facebook post ID: {entry['facebook_post_id']}")
            print(f"Full text: {full_text[:100]}..." if len(full_text) > 100 else f"Full text: {full_text}")
            
            facebook_handler.update_scheduled_post(entry['facebook_post_id'], full_text)
            print("✓ Facebook updated")
        else:
            print("Skipping Facebook update (scheduler/handler not available or no post ID)")
        
        flash('✅ הפוסט עודכן בהצלחה!', 'success')
        print("=" * 80)
        print("EDIT SCHEDULED POST - SUCCESS")
        print("=" * 80)
        
    except Exception as e:
        print("=" * 80)
        print("EDIT SCHEDULED POST - ERROR")
        print(f"ERROR: {e}")
        traceback.print_exc()
        print("=" * 80)
        
        if request.headers.get('HX-Request'):
            return '', 500
        flash(f'❌ שגיאה: {str(e)}', 'error')
    
    # For HTMX, reload the whole card
    if request.headers.get('HX-Request'):
        return redirect(url_for('scheduled_page'))
    
    return redirect(url_for('scheduled_page'))

@app.route('/swap_posts/<int:entry_id>/<direction>', methods=['POST'])
def swap_posts(entry_id, direction):
    """Swap post with previous (up) or next (down) post"""
    print("=" * 80)
    print("SWAP POSTS - START")
    print(f"Entry ID: {entry_id}")
    print(f"Direction: {direction}")
    print("=" * 80)
    
    if not scheduler or not facebook_handler:
        print("ERROR: Scheduler or Facebook handler not available")
        if request.headers.get('HX-Request'):
            return '', 500
        flash('❌ Scheduler not available', 'error')
        return redirect(url_for('scheduled_page'))
    
    try:
        # Get all scheduled entries
        print("Fetching scheduled entries...")
        entries = db.get_scheduled_entries()
        print(f"Total scheduled entries: {len(entries)}")
        
        # Sort by post_number
        entries.sort(key=lambda x: x.get('post_number', 999))
        print(f"Sorted entries by post_number")
        
        # Find current entry index
        current_idx = next((i for i, e in enumerate(entries) if e['id'] == entry_id), None)
        
        if current_idx is None:
            print(f"ERROR: Entry {entry_id} not found")
            flash('❌ Entry not found', 'error')
            return redirect(url_for('scheduled_page'))
        
        print(f"Found entry at index {current_idx}")
        
        # Determine swap target
        if direction == 'up' and current_idx > 0:
            target_idx = current_idx - 1
            print(f"Moving up: swapping with index {target_idx}")
        elif direction == 'down' and current_idx < len(entries) - 1:
            target_idx = current_idx + 1
            print(f"Moving down: swapping with index {target_idx}")
        else:
            print(f"Cannot move {direction} (at boundary)")
            return redirect(url_for('scheduled_page'))
        
        current_entry = entries[current_idx]
        target_entry = entries[target_idx]
        
        print(f"\nCurrent entry:")
        print(f"  ID: {current_entry['id']}")
        print(f"  Number: #{current_entry['post_number']}")
        print(f"  Text: {current_entry['text'][:50]}...")
        print(f"  Time: {current_entry['scheduled_time']}")
        
        print(f"\nTarget entry:")
        print(f"  ID: {target_entry['id']}")
        print(f"  Number: #{target_entry['post_number']}")
        print(f"  Text: {target_entry['text'][:50]}...")
        print(f"  Time: {target_entry['scheduled_time']}")
        
        # Swap post numbers
        current_num = current_entry['post_number']
        target_num = target_entry['post_number']
        print(f"\nSwapping numbers: {current_num} ↔ {target_num}")
        
        # Swap scheduled times (stored as strings in DB)
        current_time_str = current_entry['scheduled_time']
        target_time_str = target_entry['scheduled_time']
        print(f"Swapping times: {current_time_str} ↔ {target_time_str}")
        
        # Convert strings to datetime objects for Facebook API
        timezone = pytz.timezone('Asia/Jerusalem')
        
        try:
            print("Parsing times to datetime objects...")
            # Parse ISO format: 2026-02-12T13:00:00+02:00
            if 'T' in current_time_str:
                current_time_dt = datetime.fromisoformat(current_time_str.replace('+02:00', ''))
                current_time_dt = timezone.localize(current_time_dt)
                target_time_dt = datetime.fromisoformat(target_time_str.replace('+02:00', ''))
                target_time_dt = timezone.localize(target_time_dt)
            else:
                # Parse database format: YYYY-MM-DD HH:MM:SS
                current_time_dt = datetime.strptime(current_time_str, '%Y-%m-%d %H:%M:%S')
                current_time_dt = timezone.localize(current_time_dt)
                target_time_dt = datetime.strptime(target_time_str, '%Y-%m-%d %H:%M:%S')
                target_time_dt = timezone.localize(target_time_dt)
            print("✓ Times parsed successfully")
        except Exception as e:
            print(f"ERROR parsing times: {e}")
            traceback.print_exc()
            flash(f'❌ שגיאת פורמט זמן: {str(e)}', 'error')
            return redirect(url_for('scheduled_page'))
        
        # Update database with string times
        print("\nUpdating database...")
        conn = db.get_connection()
        cursor = conn.cursor()
        
        # Update current entry
        cursor.execute('''
            UPDATE entries 
            SET post_number = ?, scheduled_time = ?
            WHERE id = ?
        ''', (target_num, target_time_str, current_entry['id']))
        print(f"  Updated entry {current_entry['id']}: #{current_num}→#{target_num}")
        
        # Update target entry
        cursor.execute('''
            UPDATE entries 
            SET post_number = ?, scheduled_time = ?
            WHERE id = ?
        ''', (current_num, current_time_str, target_entry['id']))
        print(f"  Updated entry {target_entry['id']}: #{target_num}→#{current_num}")
        
        conn.commit()
        conn.close()
        print("✓ Database updated")
        
        # Update on Facebook with datetime objects
        print("\nUpdating Facebook...")
        # Get current texts
        current_text = current_entry.get('text', '')
        target_text = target_entry.get('text', '')
        
        # Update Facebook posts with swapped data
        if current_entry.get('facebook_post_id'):
            new_text = f"#{target_num} {current_text}"
            print(f"  Updating post {current_entry['facebook_post_id'][:10]}... with: {new_text[:50]}...")
            facebook_handler.update_scheduled_post(
                current_entry['facebook_post_id'], 
                new_text,
                target_time_dt
            )
            print("  ✓ Updated")
        else:
            print(f"  ✗ No Facebook post ID for entry {current_entry['id']}")
        
        if target_entry.get('facebook_post_id'):
            new_text = f"#{current_num} {target_text}"
            print(f"  Updating post {target_entry['facebook_post_id'][:10]}... with: {new_text[:50]}...")
            facebook_handler.update_scheduled_post(
                target_entry['facebook_post_id'],
                new_text,
                current_time_dt
            )
            print("  ✓ Updated")
        else:
            print(f"  ✗ No Facebook post ID for entry {target_entry['id']}")
        
        flash('✅ הפוסטים הוחלפו בהצלחה!', 'success')
        print("=" * 80)
        print("SWAP POSTS - SUCCESS")
        print("=" * 80)
        
    except Exception as e:
        print("=" * 80)
        print("SWAP POSTS - ERROR")
        print(f"ERROR: {e}")
        traceback.print_exc()
        print("=" * 80)
        flash(f'❌ שגיאה: {str(e)}', 'error')
    
    # Reload the scheduled list
    return redirect(url_for('scheduled_page'))

# ============================================================================
# ROUTES - Denied Posts
# ============================================================================

@app.route('/denied')
def denied_page():
    """View denied posts (24hr recovery)"""
    db.cleanup_old_denied()
    entries = db.get_denied_entries()
    
    # Calculate time remaining for each
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
    """Restore denied entry to pending"""
    db.return_denied_to_pending(entry_id)
    
    # If HTMX request, return empty to remove the card
    if request.headers.get('HX-Request'):
        return '', 200
    
    flash('✅ הוחזר להמתנה', 'success')
    return redirect(url_for('denied_page'))

# ============================================================================
# ROUTES - Statistics
# ============================================================================

@app.route('/statistics')
def statistics_page():
    """View statistics"""
    stats = db.get_statistics()
    recent = db.get_recent_activity(20)
    current_number = db.get_current_post_number()
    
    return render_template('statistics.html', 
                         stats=stats, 
                         recent=recent,
                         current_number=current_number)

# ============================================================================
# ROUTES - Settings
# ============================================================================

@app.route('/settings', methods=['GET', 'POST'])
def settings_page():
    """Settings page"""
    if request.method == 'POST':
        # Handle date picker (comes as YYYY-MM-DD)
        read_from_date_iso = request.form.get('read_from_date', '')
        
        # Merge with existing config (preserve keys set programmatically)
        config = load_config()
        config.update({
            'google_sheet_id': request.form.get('google_sheet_id', ''),
            'google_credentials_file': request.form.get('google_credentials_file', 'credentials.json'),
            'read_from_date': read_from_date_iso,  # Store ISO format from date picker
            'facebook_page_id': request.form.get('facebook_page_id', ''),
            'facebook_access_token': request.form.get('facebook_access_token', ''),
            'posting_windows': [w.strip() for w in request.form.get('posting_windows', '').split(',') if w.strip()],
            'skip_shabbat': request.form.get('skip_shabbat') == 'on',
            'skip_jewish_holidays': request.form.get('skip_jewish_holidays') == 'on',
            'notifications_enabled': request.form.get('notifications_enabled') == 'on',
            'resend_api_key': request.form.get('resend_api_key', ''),
            'resend_from_email': request.form.get('resend_from_email', ''),
            'notification_emails': [e.strip() for e in request.form.get('notification_emails', '').split(',') if e.strip()],
            'pending_threshold': int(request.form.get('pending_threshold', 20)),
            'app_url': request.form.get('app_url', ''),
            # Comments filter settings
            'comments_filter_enabled': request.form.get('comments_filter_enabled') == 'on',
            'openai_api_key': request.form.get('openai_api_key', ''),
            'daily_api_limit': int(request.form.get('daily_api_limit', 1000)),
            'batch_size': int(request.form.get('batch_size', 50)),
        })
        
        save_config(config)
        init_handlers()  # Reinitialize with new config
        
        flash('✅ הגדרות נשמרו בהצלחה!', 'success')
        return redirect(url_for('settings_page'))
    
    # GET request
    config = load_config()
    current_number = db.get_current_post_number()
    
    return render_template('settings.html', config=config, current_number=current_number)

@app.route('/set_post_number', methods=['POST'])
def set_post_number():
    """Set the next post number (separate from settings save)"""
    new_number = request.form.get('new_post_number', '').strip()
    if not new_number:
        flash('⚠️ לא הוזן מספר', 'error')
        return redirect(url_for('settings_page'))
    try:
        num = int(new_number)
        if num < 1:
            raise ValueError
        db.reset_post_number(num)
        flash(f'✅ מספר הפוסט הבא עודכן ל-#{num}', 'success')
    except ValueError:
        flash('⚠️ מספר לא תקין', 'error')
    return redirect(url_for('settings_page'))

@app.route('/clear_pending', methods=['POST'])
def clear_pending():
    """Clear all pending entries"""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute("DELETE FROM entries WHERE status = 'pending'")
    count = cursor.rowcount
    conn.commit()
    conn.close()
    flash(f'✅ נמחקו {count} ערכים ממתינים', 'success')
    return redirect(url_for('settings_page'))

@app.route('/clear_comments', methods=['POST'])
def clear_comments():
    """Clear all comments from database (preserve AI examples)"""
    count = db.clear_all_comments()
    flash(f'✅ נמחקו {count} תגובות ממסד הנתונים (דוגמאות AI נשמרו)', 'success')
    return redirect(url_for('settings_page'))

@app.route('/delete_database', methods=['POST'])
def delete_database():
    """Delete entire database and recreate"""
    global db  # Declare global FIRST
    import os
    
    try:
        # Close connections
        db.get_connection().close()
        
        # Delete database file
        if os.path.exists('content_system.db'):
            os.remove('content_system.db')
        
        # Reinitialize database
        db = Database()
        
        flash('✅ מסד הנתונים נמחק ונוצר מחדש', 'success')
    except Exception as e:
        flash(f'❌ שגיאה: {str(e)}', 'error')
    
    return redirect(url_for('settings_page'))

@app.route('/test_notification', methods=['POST'])
def test_notification():
    """Send test notification"""
    if notifications.send_test_notification():
        flash('✅ התראה נשלחה!', 'success')
    else:
        flash('❌ שליחה נכשלה', 'error')
    
    return redirect(url_for('settings_page'))

# ============================================================================
# ROUTES - Comments Management (Comments Filter Feature)
# ============================================================================

@app.route('/comments')
def comments_page():
    """Page to view and manage all comments from past 7 days"""
    print("=" * 60)
    print("COMMENTS PAGE - DEBUG START")
    print("=" * 60)
    
    filter_status = request.args.get('filter', 'all')
    print(f"1. Filter status: {filter_status}")
    
    # Get comments grouped by post
    print("2. Calling db.get_comments_grouped_by_post()...")
    try:
        grouped_comments = db.get_comments_grouped_by_post(
            filter_status=filter_status if filter_status != 'all' else None,
            days=7
        )
        total_comments = sum(len(post['comments']) for post in grouped_comments.values())
        print(f"   ✓ Returned {len(grouped_comments)} posts with {total_comments} comments")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        traceback.print_exc()
        grouped_comments = {}
    
    # Get stats
    print("3. Calling db.get_comments_stats()...")
    try:
        stats = db.get_comments_stats(days=7)
        print(f"   ✓ Stats: {stats}")
    except Exception as e:
        print(f"   ✗ Error: {e}")
        traceback.print_exc()
        stats = {'total': 0, 'hidden': 0, 'shown': 0}
    
    print(f"4. Rendering template with {len(grouped_comments)} post groups")
    print("=" * 60)
    
    return render_template('comments.html',
                         grouped_comments=grouped_comments,
                         stats=stats,
                         filter_status=filter_status,
                         last_scan=load_config().get('last_comment_scan', ''))

@app.route('/scan-comments-now', methods=['POST'])
def scan_comments_now():
    """Manually trigger comment scanning (runs async)"""
    
    # Set global scan status
    global scan_in_progress
    scan_in_progress = True
    
    def async_scan():
        global scan_in_progress
        try:
            print("\n" + "="*60)
            print("🔍 MANUAL COMMENT SCAN TRIGGERED")
            print("="*60)
            
            config = load_config()
            
            # Check if feature is enabled
            if not config.get('comments_filter_enabled', False):
                print("⚠️  Comments filter disabled in settings")
                scan_in_progress = False
                return
            
            # Check API keys
            if not config.get('openai_api_key'):
                print("⚠️  Missing OpenAI API key")
                scan_in_progress = False
                return
            
            if not config.get('facebook_access_token'):
                print("⚠️  Missing Facebook access token")
                scan_in_progress = False
                return
            
            # Run the scanner
            from comments_scanner import create_hourly_job
            
            print("Creating scanner job...")
            job = create_hourly_job(db, config)
            
            print("Running scan...")
            job()
            
            # Save last scan time
            config = load_config()
            israel_tz = pytz.timezone('Asia/Jerusalem')
            config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
            save_config(config)
            
            print("✅ Manual scan completed")
            print("="*60 + "\n")
            
        except ImportError as e:
            print(f"❌ Import error: {e}")
        except Exception as e:
            print(f"❌ Scan error: {e}")
            traceback.print_exc()
        finally:
            scan_in_progress = False
    
    # Run in background thread
    thread = threading.Thread(target=async_scan, daemon=True)
    thread.start()
    
    return '', 200

@app.route('/scan-status', methods=['GET'])
def scan_status():
    """Check if scan is in progress"""
    global scan_in_progress
    return jsonify({'scanning': scan_in_progress if 'scan_in_progress' in globals() else False})

# Initialize scan status
scan_in_progress = False

# ============================================================================
# COMMENT ACTION HELPERS
# ============================================================================

def _get_fb_handler():
    """Create FacebookCommentsHandler from config. Returns None if not configured."""
    from facebook_comments_handler import FacebookCommentsHandler
    config = load_config()
    if not config.get('facebook_access_token') or not config.get('facebook_page_id'):
        return None
    return FacebookCommentsHandler(
        access_token=config['facebook_access_token'],
        page_id=config['facebook_page_id']
    )

def _get_comment(comment_id):
    """Fetch comment from hidden_comments table."""
    conn = db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hidden_comments WHERE comment_id = ?', (comment_id,))
    comment = cursor.fetchone()
    conn.close()
    return comment

def _run_async(fn):
    """Run function in a background daemon thread."""
    threading.Thread(target=fn, daemon=True).start()

# Shared HTML response for buttons that trigger a page reload
_PROCESSING_RESPONSE = '''
    <button class="btn btn-sm btn-secondary" disabled>
        <i class="bi bi-hourglass-split"></i> מעבד...
    </button>
    <script>
        setTimeout(() => location.reload(), 1000);
    </script>
'''

# Label descriptions for AI example explanations
_LABEL_DESCRIPTIONS = {
    'political': 'Political content',
    'hate': 'Hate speech',
    'spam': 'Spam',
}

def _mark_and_delete_comment(comment_id, label):
    """
    Shared logic for mark-political, mark-hate, mark-spam routes.
    Deletes comment from Facebook, adds AI training example, dismisses from DB.
    """
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                return
            
            comment = _get_comment(comment_id)
            if not comment:
                return
            
            success = fb.delete_comment(comment_id)
            
            ai_said = comment['filter_reason']
            category = f'correct_{label}' if ai_said == label else f'missed_{label}'
            description = _LABEL_DESCRIPTIONS.get(label, label.capitalize())
            
            db.add_ai_example(
                category=category,
                comment_text=comment['comment_text'],
                original_ai_prediction=ai_said,
                explanation=f"{description} - admin marked for deletion"
            )
            
            db.dismiss_comment(comment_id)
            
            if success:
                print(f"✅ Marked as {label} and deleted: {comment_id} → {category}")
            else:
                print(f"⚠️  Marked as {label} but Facebook delete failed: {comment_id} → {category}")
            
        except Exception as e:
            print(f"❌ Error marking {label}: {e}")
    
    _run_async(async_action)
    return '', 200

# ============================================================================
# COMMENT ROUTES
# ============================================================================

@app.route('/comment/<comment_id>/mark-political', methods=['POST'])
def mark_comment_political(comment_id):
    """Mark comment as political - deletes from Facebook + adds to examples"""
    return _mark_and_delete_comment(comment_id, 'political')

@app.route('/comment/<comment_id>/mark-hate', methods=['POST'])
def mark_comment_hate(comment_id):
    """Mark comment as hate speech - deletes from Facebook + adds to examples"""
    return _mark_and_delete_comment(comment_id, 'hate')

@app.route('/comment/<comment_id>/mark-ok', methods=['POST'])
def mark_comment_ok(comment_id):
    """Mark comment as OK - unhides on Facebook + adds to examples"""
    
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                return
            
            comment = _get_comment(comment_id)
            if not comment:
                return
            
            # Only unhide if currently hidden
            if comment['status'] == 'hidden':
                success = fb.unhide_comment(comment_id)
            else:
                success = True
            
            if success:
                ai_said = comment['filter_reason']
                
                if ai_said == 'clean':
                    # AI said clean and it is clean - just dismiss
                    db.dismiss_comment(comment_id)
                    print(f"✅ Marked as OK (already clean): {comment_id}")
                    return
                
                category = f'false_positive_{ai_said}'
                
                db.add_ai_example(
                    category=category,
                    comment_text=comment['comment_text'],
                    original_ai_prediction=ai_said,
                    explanation="False positive - actually acceptable"
                )
                
                db.dismiss_comment(comment_id)
                print(f"✅ Marked as OK and unhidden: {comment_id} → {category}")
            
        except Exception as e:
            print(f"❌ Error marking OK: {e}")
    
    _run_async(async_action)
    return '', 200

@app.route('/comment/<comment_id>/mark-spam', methods=['POST'])
def mark_comment_spam(comment_id):
    """Mark comment as spam - deletes from Facebook + adds to examples"""
    return _mark_and_delete_comment(comment_id, 'spam')

@app.route('/comment/<comment_id>/dismiss', methods=['POST'])
def dismiss_comment(comment_id):
    """Dismiss a comment (remove from UI, mark as dismissed)"""
    try:
        success = db.dismiss_comment(comment_id)
        
        if success:
            return '', 200
        else:
            return '<div class="alert alert-danger">❌ תגובה לא נמצאה</div>', 404
            
    except Exception as e:
        print(f"Error dismissing comment: {e}")
        return f'<div class="alert alert-danger">❌ שגיאה: {str(e)}</div>', 500

@app.route('/comments/mark-all-ok', methods=['POST'])
def mark_all_comments_ok():
    """Mark ALL visible comments as OK (batch operation)"""
    filter_status = request.form.get('filter_status')
    
    # Get all current comments
    grouped = db.get_comments_grouped_by_post(
        filter_status=filter_status if filter_status != 'all' else None,
        days=7
    )
    
    comment_ids = []
    for post_data in grouped.values():
        for comment in post_data['comments']:
            comment_ids.append(comment['comment_id'])
    
    if not comment_ids:
        return redirect(url_for('comments_page', filter=filter_status or 'all'))
    
    _batch_mark_ok(comment_ids)
    flash(f'✅ {len(comment_ids)} תגובות סומנו כתקינות', 'success')
    return redirect(url_for('comments_page', filter=filter_status or 'all'))

@app.route('/comments/post/<post_id>/mark-all-ok', methods=['POST'])
def mark_post_comments_ok(post_id):
    """Mark all comments for a specific post as OK"""
    filter_status = request.form.get('filter_status', 'all')
    
    grouped = db.get_comments_grouped_by_post(
        filter_status=filter_status if filter_status != 'all' else None,
        days=7
    )
    
    post_data = grouped.get(post_id)
    if not post_data:
        return redirect(url_for('comments_page', filter=filter_status))
    
    comment_ids = [c['comment_id'] for c in post_data['comments']]
    
    if not comment_ids:
        return redirect(url_for('comments_page', filter=filter_status))
    
    _batch_mark_ok(comment_ids)
    flash(f'✅ {len(comment_ids)} תגובות בפוסט סומנו כתקינות', 'success')
    return redirect(url_for('comments_page', filter=filter_status))

def _batch_mark_ok(comment_ids):
    """Shared batch mark-ok logic. Runs in background thread."""
    def async_batch():
        try:
            fb = _get_fb_handler()
            
            for cid in comment_ids:
                try:
                    comment = _get_comment(cid)
                    if not comment:
                        continue
                    
                    # Unhide on Facebook if hidden
                    if fb and comment['status'] == 'hidden':
                        fb.unhide_comment(cid)
                    
                    # Add AI example if it was a false positive
                    ai_said = comment['filter_reason']
                    if ai_said and ai_said != 'clean':
                        db.add_ai_example(
                            category=f'false_positive_{ai_said}',
                            comment_text=comment['comment_text'],
                            original_ai_prediction=ai_said,
                            explanation="False positive - batch marked as OK"
                        )
                    
                    db.dismiss_comment(cid)
                    
                except Exception as e:
                    print(f"⚠️  Error marking comment {cid} as OK: {e}")
                    # Still try to dismiss even if unhide failed
                    try:
                        db.dismiss_comment(cid)
                    except:
                        pass
            
            print(f"✅ Batch mark-ok complete: {len(comment_ids)} comments processed")
            
        except Exception as e:
            print(f"❌ Batch mark-ok error: {e}")
    
    _run_async(async_batch)

@app.route('/comment/<comment_id>/show', methods=['POST'])
def show_comment(comment_id):
    """Show a comment on Facebook (async action)"""
    
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                print(f"⚠️  Facebook not configured for comment {comment_id}")
                return
            
            success = fb.unhide_comment(comment_id)
            
            if success:
                db.update_comment_status(comment_id, 'shown')
                db.log_ai_feedback(comment_id, 'false_positive')
                print(f"✅ Showed comment {comment_id}")
            else:
                print(f"❌ Failed to show comment {comment_id}")
                
        except Exception as e:
            print(f"❌ Error showing comment {comment_id}: {e}")
    
    _run_async(async_action)
    return _PROCESSING_RESPONSE, 200

@app.route('/comment/<comment_id>/hide', methods=['POST'])
def hide_comment(comment_id):
    """Hide a comment on Facebook"""
    reason = request.form.get('reason')
    
    # Check if comment was flagged by AI
    comment = _get_comment(comment_id)
    
    # If no reason provided and comment was NOT flagged, return buttons to choose reason
    if not reason and comment and comment['filter_reason'] == 'clean':
        return '''
        <div>
            <p class="mb-2"><strong>למה להסתיר?</strong></p>
            <form hx-post="''' + url_for('hide_comment', comment_id=comment_id) + '''" hx-swap="outerHTML">
                <div class="btn-group" role="group">
                    <button type="submit" name="reason" value="political" class="btn btn-sm btn-primary">🏛️ פוליטי</button>
                    <button type="submit" name="reason" value="hate" class="btn btn-sm btn-danger">⚠️ שנאה</button>
                    <button type="submit" name="reason" value="rude" class="btn btn-sm btn-warning">😠 גס רוח</button>
                </div>
            </form>
        </div>
        ''', 200
    
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                print(f"⚠️  Facebook not configured for comment {comment_id}")
                return
            
            success = fb.hide_comment(comment_id)
            
            if success:
                if comment and comment['filter_reason'] == 'clean' and reason:
                    db.log_ai_feedback(comment_id, 'missed', reason)
                
                db.update_comment_status(comment_id, 'hidden')
                print(f"✅ Hid comment {comment_id} (reason: {reason or 'already flagged'})")
            else:
                print(f"❌ Failed to hide comment {comment_id}")
                
        except Exception as e:
            print(f"❌ Error hiding comment {comment_id}: {e}")
    
    _run_async(async_action)
    return _PROCESSING_RESPONSE, 200

@app.route('/comment/<comment_id>/delete', methods=['POST'])
def delete_comment_action(comment_id):
    """Delete a comment permanently from Facebook (async action)"""
    
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                print(f"⚠️  Facebook not configured for comment {comment_id}")
                return
            
            success = fb.delete_comment(comment_id)
            
            if success:
                comment = _get_comment(comment_id)
                
                if comment:
                    if comment['status'] == 'hidden' and comment['filter_reason'] in ['political', 'hate']:
                        db.log_ai_feedback(comment_id, 'correct_hide')
                
                db.update_comment_status(comment_id, 'deleted')
                print(f"✅ Deleted comment {comment_id}")
            else:
                print(f"❌ Failed to delete comment {comment_id}")
                
        except Exception as e:
            print(f"❌ Error deleting comment {comment_id}: {e}")
    
    _run_async(async_action)
    return _PROCESSING_RESPONSE, 200

@app.route('/ai-examples')
def ai_examples_page():
    """Admin page to view and manage AI training examples"""
    examples = db.get_all_ai_examples_for_admin()
    
    # Group by category
    categories = {
        'false_positive_political': {
            'title_he': 'False Positive - פוליטי',
            'icon': '❌🏛️',
            'description': 'תגובות שה-AI סימן בטעות כפוליטיות',
            'examples': []
        },
        'false_positive_hate': {
            'title_he': 'False Positive - שנאה',
            'icon': '❌⚠️',
            'description': 'תגובות שה-AI סימן בטעות כשנאה',
            'examples': []
        },
        'false_positive_spam': {
            'title_he': 'False Positive - ספאם',
            'icon': '❌🚫',
            'description': 'תגובות שה-AI סימן בטעות כספאם',
            'examples': []
        },
        'correct_political': {
            'title_he': 'נכון - פוליטי',
            'icon': '✓🏛️',
            'description': 'תגובות פוליטיות שה-AI זיהה נכון',
            'examples': []
        },
        'correct_hate': {
            'title_he': 'נכון - שנאה',
            'icon': '✓⚠️',
            'description': 'תגובות שנאה שה-AI זיהה נכון',
            'examples': []
        },
        'correct_spam': {
            'title_he': 'נכון - ספאם',
            'icon': '✓🚫',
            'description': 'ספאם שה-AI זיהה נכון',
            'examples': []
        },
        'missed_political': {
            'title_he': 'פספס - פוליטי',
            'icon': '⚠️🏛️',
            'description': 'תגובות פוליטיות שה-AI פספס',
            'examples': []
        },
        'missed_hate': {
            'title_he': 'פספס - שנאה',
            'icon': '⚠️⚠️',
            'description': 'תגובות שנאה שה-AI פספס',
            'examples': []
        },
        'missed_spam': {
            'title_he': 'פספס - ספאם',
            'icon': '⚠️🚫',
            'description': 'ספאם שה-AI פספס',
            'examples': []
        }
    }
    
    for example in examples:
        cat = example['category']
        if cat in categories:
            categories[cat]['examples'].append(example)
    
    # Calculate stats
    stats = {
        'total': len(examples),
        'false_positives': len([e for e in examples if 'false_positive' in e['category']]),
        'correct': len([e for e in examples if 'correct' in e['category']]),
        'missed': len([e for e in examples if 'missed' in e['category']])
    }
    
    return render_template('ai_examples.html', 
                         categories=categories,
                         stats=stats)

@app.route('/ai-examples/<int:example_id>/delete', methods=['POST'])
def delete_ai_example(example_id):
    """Delete an AI example"""
    success = db.delete_ai_example(example_id)
    
    if success:
        return '<div class="alert alert-success">✓ דוגמה נמחקה</div>', 200
    else:
        return '<div class="alert alert-danger">✗ שגיאה</div>', 404

# ============================================================================
# BACKGROUND TASKS - Auto-sync and Notifications
# ============================================================================

def midnight_sync_job():
    """Run at midnight: sync from sheets + cleanup + check notifications"""
    try:
        print("\n" + "=" * 80)
        print(f"🌙 MIDNIGHT SYNC STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        
        # Ensure logs directory exists
        import os
        os.makedirs('logs', exist_ok=True)
        
        with open('logs/midnight_sync.log', 'a') as f:
            f.write(f"\n{datetime.now().isoformat()} - Midnight sync triggered\n")
        
        # 1. Sync from Google Sheets
        if sheets_handler:
            try:
                config = load_config()
                read_from_date = config.get('read_from_date', '').strip()
                
                # Convert date format: YYYY-MM-DD → DD/MM/YYYY 00:00:00
                # (same conversion as the manual sync route)
                last_timestamp = None
                if read_from_date:
                    try:
                        dt = datetime.strptime(read_from_date, '%Y-%m-%d')
                        last_timestamp = dt.strftime('%d/%m/%Y 00:00:00')
                    except Exception as e:
                        print(f"⚠️  Date parse error for '{read_from_date}': {e}")
                
                print(f"📊 Syncing from Google Sheets (from: {last_timestamp or 'ALL'})...")
                new_entries = sheets_handler.fetch_new_entries(last_timestamp)
                
                added = 0
                for entry in new_entries:
                    if db.add_entry(entry['timestamp'], entry['text']):
                        added += 1
                
                print(f"✅ Synced {added} new entries from Google Sheets")
                
                # Update last sync time (use Israel timezone like manual sync)
                israel_tz = pytz.timezone('Asia/Jerusalem')
                config['last_sync'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
                save_config(config)
                
            except Exception as e:
                print(f"❌ Sheets sync error: {e}")
                traceback.print_exc()
        else:
            print("⚠️ Sheets handler not initialized")
        
        # 2. Cleanup old entries
        try:
            print(f"🧹 Cleaning up old database entries...")
            deleted = db.cleanup_old_entries()
            print(f"✅ Cleaned up {deleted} old entries")
        except Exception as e:
            print(f"❌ Cleanup error: {e}")
        
        # 3. Check and send notifications
        try:
            print(f"📧 Checking notifications...")
            check_and_send_notifications()
        except Exception as e:
            print(f"❌ Notification error: {e}")
        
        print("=" * 80)
        print(f"✅ MIDNIGHT SYNC COMPLETE: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        
    except Exception as e:
        print(f"❌ MIDNIGHT SYNC ERROR: {e}")
        traceback.print_exc()

def check_and_send_notifications():
    """Check if notifications should be sent"""
    config = load_config()
    
    # Check if notifications are enabled
    if not config.get('notifications_enabled', False):
        print("⚠️ Notifications disabled in settings")
        return
    
    notification_emails = config.get('notification_emails', [])
    if not notification_emails:
        print("⚠️ No notification emails configured")
        return
    
    # Check 1: Too many pending entries
    pending_entries = db.get_pending_entries()
    pending_count = len(pending_entries)
    threshold = config.get('pending_threshold', 10)
    
    if pending_count >= threshold:
        print(f"📧 Sending notification: {pending_count} pending entries (threshold: {threshold})")
        subject = f"⚠️ {pending_count} ערכים ממתינים לבדיקה"
        body = f"""
שלום,

יש כרגע {pending_count} ערכים שממתינים לאישור במערכת.

סף ההתראה: {threshold} ערכים

כנסו למערכת לבדיקה: {config.get('app_url', 'http://localhost:5000')}

בברכה,
מערכת ניהול התוכן
"""
        send_notification_email(subject, body, notification_emails)
    else:
        print(f"✅ Pending entries OK: {pending_count}/{threshold}")
    
    # Check 2: Next empty window within 24 hours
    if scheduler:
        try:
            next_slot = scheduler.get_next_available_slot()
            now = datetime.now(scheduler.timezone)
            hours_until = (next_slot - now).total_seconds() / 3600
            
            if hours_until < 24:
                print(f"📧 Sending notification: Next window in {hours_until:.1f} hours")
                subject = "⏰ החלון הפנוי הבא בעוד פחות מ-24 שעות"
                body = f"""
שלום,

החלון הפנוי הבא לפרסום הוא בעוד {hours_until:.1f} שעות:

תאריך ושעה: {next_slot.strftime('%d/%m/%Y %H:%M')}

אם יש ערכים ממתינים, כדאי לאשר אותם כדי למלא את החלון.

כנסו למערכת: {config.get('app_url', 'http://localhost:5000')}

בברכה,
מערכת ניהול התוכן
"""
                send_notification_email(subject, body, notification_emails)
            else:
                print(f"✅ Next window OK: in {hours_until:.1f} hours")
        except Exception as e:
            print(f"❌ Error checking next window: {e}")
    else:
        print("⚠️ Scheduler not initialized")

def send_notification_email(subject, body, recipients):
    """Send email notification via Resend API"""
    config = load_config()
    
    api_key = config.get('resend_api_key')
    from_email = config.get('resend_from_email')
    
    if not api_key or not from_email:
        print("❌ Resend not configured (missing api_key or from_email)")
        return
    
    try:
        import resend
        resend.api_key = api_key
        
        params = {
            "from": from_email,
            "to": recipients,
            "subject": subject,
            "html": body,
        }
        
        result = resend.Emails.send(params)
        print(f"✅ Email sent via Resend to: {', '.join(recipients)} (id: {result.get('id', '?')})")
        
    except Exception as e:
        print(f"❌ Resend email error: {e}")
        traceback.print_exc()

def comments_scan_job():
    """
    Hourly job to scan and filter comments
    Only runs if comments filter is enabled in settings
    """
    try:
        config = load_config()
        
        # Check if feature is enabled
        if not config.get('comments_filter_enabled', False):
            print("ℹ️  Comments filter disabled in settings")
            return
        
        # Check if we have required API keys
        if not config.get('openai_api_key') or not config.get('facebook_access_token'):
            print("⚠️  Missing API keys for comments filter")
            return
        
        print(f"\n{'='*60}")
        print(f"🔍 Starting hourly comments scan")
        print(f"{'='*60}")
        
        # Import here to avoid errors if module doesn't exist
        from comments_scanner import create_hourly_job
        
        # Create and run the scanner
        job = create_hourly_job(db, config)
        job()
        
        # Save last scan time
        config = load_config()
        israel_tz = pytz.timezone('Asia/Jerusalem')
        config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
        save_config(config)
        
    except ImportError as e:
        print(f"⚠️  Comments scanner not available: {e}")
    except Exception as e:
        print(f"❌ Comments scan error: {e}")
        traceback.print_exc()

def cleanup_old_comments_job():
    """
    Daily job to cleanup comments older than 7 days
    Run at 02:00 to avoid conflicts with other jobs
    """
    try:
        print(f"\n{'='*60}")
        print(f"🧹 Cleaning up old comments (>7 days)")
        print(f"{'='*60}")
        
        deleted = db.cleanup_old_comments(days=7)
        
        if deleted > 0:
            print(f"✅ Cleaned up {deleted} old comments")
        else:
            print("ℹ️  No old comments to clean up")
            
    except Exception as e:
        print(f"❌ Cleanup error: {e}")
        traceback.print_exc()

def run_scheduler():
    """Background thread for scheduled tasks"""
    print("🔄 Scheduler thread started")
    while True:
        try:
            # Log pending jobs every hour for debugging
            pending = schedule.get_jobs()
            if datetime.now().minute == 0:  # Log on the hour
                print(f"\n⏰ [{datetime.now().strftime('%H:%M')}] Scheduler status: {len(pending)} jobs pending")
                for job in pending:
                    print(f"   - {job}")
            
            schedule.run_pending()
            time.sleep(60)  # Check every minute
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            traceback.print_exc()
            time.sleep(60)

def start_scheduler():
    """Initialize and start the scheduler with Israel timezone"""
    
    # Get current server timezone offset to Israel
    israel_tz = pytz.timezone('Asia/Jerusalem')
    now_utc = datetime.now(pytz.utc)
    now_israel = now_utc.astimezone(israel_tz)
    server_now = datetime.now()
    
    # Calculate offset (hours difference between server and Israel)
    offset = (now_israel.hour - server_now.hour) % 24
    
    print(f"🕐 Timezone info:")
    print(f"   Server time: {server_now.strftime('%H:%M')}")
    print(f"   Israel time: {now_israel.strftime('%H:%M')}")
    print(f"   Offset: {offset} hours")
    
    # Schedule midnight sync at 00:00 Israel time
    # Convert to server local time
    midnight_local = (24 - offset) % 24
    schedule.every().day.at(f"{midnight_local:02d}:00").do(midnight_sync_job)
    
    # Cleanup at 02:00 Israel time
    cleanup_local = (2 + 24 - offset) % 24
    schedule.every().day.at(f"{cleanup_local:02d}:00").do(cleanup_old_comments_job)
    
    # Also run notifications check every 6 hours
    schedule.every(6).hours.do(check_and_send_notifications)
    
    # Comments scanner - run every hour
    schedule.every().hour.do(comments_scan_job)
    
    # Start background scheduler thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    print("=" * 80)
    print("✅ Background scheduler started:")
    print(f"   - Midnight sync at {midnight_local:02d}:00 server time (00:00 Israel)")
    print(f"   - Notifications check every 6 hours")
    print(f"   - Comments scanner every hour")
    print(f"   - Old comments cleanup at {cleanup_local:02d}:00 server time (02:00 Israel)")
    print(f"   - Scheduler thread running in background")
    print("=" * 80)

# Initialize scheduler at module level (runs when app starts)
try:
    start_scheduler()
except Exception as e:
    print(f"⚠️  Scheduler initialization failed: {e}")

# ============================================================================
# API ENDPOINTS (for HTMX)
# ============================================================================

@app.route('/api/entry/<int:entry_id>/height')
def get_entry_height(entry_id):
    """Get calculated height for an entry"""
    entries = db.get_pending_entries()
    entry = next((e for e in entries if e['id'] == entry_id), None)
    
    if entry:
        height = calculate_textarea_height(entry['text'])
        return jsonify({'height': height})
    
    return jsonify({'height': 120})

@app.route('/api/scheduler/status')
def scheduler_status():
    """Check if scheduler is running"""
    jobs = schedule.get_jobs()
    return jsonify({
        'running': len(jobs) > 0,
        'jobs': [str(job) for job in jobs],
        'next_run': str(schedule.next_run()) if jobs else None
    })

if __name__ == '__main__':
    import os
    # Check if running in production (via systemd) or development
    is_production = os.environ.get('FLASK_ENV') == 'production' or not os.isatty(0)
    
    if is_production:
        print("🚀 Starting in PRODUCTION mode")
        # Production mode: no debug, scheduler will run
        app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
    else:
        print("🔧 Starting in DEVELOPMENT mode")
        # Development mode: debug enabled
        app.run(debug=True, host='0.0.0.0', port=5000, use_reloader=False)

