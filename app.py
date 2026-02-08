from flask import Flask, render_template, request, redirect, url_for, jsonify, flash
from pathlib import Path
import json
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

# Initialize handlers
db = Database()
scheduler = None
facebook_handler = None
sheets_handler = None
notifications = NotificationHandler()

def load_config():
    """Load configuration from file"""
    config_file = Path("config.json")
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            return json.load(f)
    return {}

def save_config(config):
    """Save configuration to file"""
    config_file = Path("config.json")
    with open(config_file, 'w', encoding='utf-8') as f:
        json.dump(config, f, indent=2, ensure_ascii=False)

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
        import traceback
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
    import threading
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
        import traceback
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
                from datetime import datetime
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
            from datetime import datetime, timedelta
            import pytz
            
            # Get all scheduled times
            scheduled_times = sorted([
                datetime.fromisoformat(p['scheduled_time'].replace('+02:00', '').replace('+03:00', ''))
                for p in fb_posts
            ])
            
            if scheduled_times:
                # Generate all valid slots from now until last scheduled post
                windows = scheduler.load_posting_windows()
                now = datetime.now(scheduler.timezone)
                last_scheduled = max(scheduled_times)
                
                valid_slots = []
                current_date = now.date()
                
                while True:
                    if not scheduler.should_skip_date(current_date):
                        for window_time in windows:
                            slot = scheduler.timezone.localize(datetime.combine(current_date, window_time))
                            if slot > now:
                                valid_slots.append(slot)
                                if len(valid_slots) >= len(scheduled_times):
                                    break
                        if len(valid_slots) >= len(scheduled_times):
                            break
                    current_date += timedelta(days=1)
                    if (current_date - now.date()).days > 365:
                        break
                
                # Take only the slots we need
                valid_slots = valid_slots[:len(scheduled_times)]
                
                # Check if posts need to be moved to fill holes
                posts_moved = False
                posts_to_update = []
                
                for i, (fb_post, target_slot) in enumerate(zip(sorted(fb_posts, key=lambda p: p['scheduled_time']), valid_slots)):
                    current_time = datetime.fromisoformat(fb_post['scheduled_time'].replace('+02:00', '').replace('+03:00', ''))
                    
                    # If post is not at the right slot, mark for update
                    if abs((scheduler.timezone.localize(current_time) - target_slot).total_seconds()) > 60:
                        posts_to_update.append((fb_post, target_slot))
                        posts_moved = True
                
                if posts_moved:
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
                                scheduled_time_str = fb_post['scheduled_time'].split('+')[0] if '+' in fb_post['scheduled_time'] else fb_post['scheduled_time']
                                if 'T' in scheduled_time_str:
                                    try:
                                        dt = datetime.fromisoformat(scheduled_time_str)
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
        import traceback
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
                import traceback
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
                    from datetime import datetime
                    import pytz
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
                    import traceback
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
        import traceback
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
        import traceback
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
        from datetime import datetime
        import pytz
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
            import traceback
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
        import traceback
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
        # Handle post number change
        next_post_number = request.form.get('next_post_number', '')
        if next_post_number:
            try:
                db.reset_post_number(int(next_post_number))
            except:
                pass
        
        # Handle date picker (comes as YYYY-MM-DD)
        read_from_date_iso = request.form.get('read_from_date', '')
        
        # Save all settings
        config = {
            'google_sheet_id': request.form.get('google_sheet_id', ''),
            'google_credentials_file': request.form.get('google_credentials_file', 'credentials.json'),
            'read_from_date': read_from_date_iso,  # Store ISO format from date picker
            'facebook_page_id': request.form.get('facebook_page_id', ''),
            'facebook_access_token': request.form.get('facebook_access_token', ''),
            'posting_windows': [w.strip() for w in request.form.get('posting_windows', '').split(',') if w.strip()],
            'skip_shabbat': request.form.get('skip_shabbat') == 'on',
            'skip_jewish_holidays': request.form.get('skip_jewish_holidays') == 'on',
            'notifications_enabled': request.form.get('notifications_enabled') == 'on',
            'gmail_email': request.form.get('gmail_email', ''),
            'gmail_app_password': request.form.get('gmail_app_password', ''),
            'notification_emails': [e.strip() for e in request.form.get('notification_emails', '').split(',') if e.strip()],
            'pending_threshold': int(request.form.get('pending_threshold', 20)),
            'app_url': request.form.get('app_url', ''),
            'last_sync': load_config().get('last_sync', 'Never')
        }
        
        save_config(config)
        init_handlers()  # Reinitialize with new config
        
        flash('✅ הגדרות נשמרו בהצלחה!', 'success')
        return redirect(url_for('settings_page'))
    
    # GET request
    config = load_config()
    current_number = db.get_current_post_number()
    
    return render_template('settings.html', config=config, current_number=current_number)

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
# BACKGROUND TASKS - Auto-sync and Notifications
# ============================================================================

import schedule
import threading
import time
from datetime import datetime

def midnight_sync_job():
    """Run at midnight: sync from sheets + cleanup + check notifications"""
    try:
        print("=" * 80)
        print(f"🌙 MIDNIGHT SYNC STARTED: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 80)
        
        # 1. Sync from Google Sheets
        if sheets_handler:
            try:
                config = load_config()
                last_timestamp = config.get('read_from_date', '')
                
                print(f"📊 Syncing from Google Sheets (from: {last_timestamp})...")
                new_entries = sheets_handler.get_new_entries(last_timestamp)
                
                added = 0
                for entry in new_entries:
                    db.add_entry(entry['timestamp'], entry['text'])
                    added += 1
                
                print(f"✅ Synced {added} new entries from Google Sheets")
                
                # Update last sync time
                config['last_sync'] = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
                save_config(config)
                
            except Exception as e:
                print(f"❌ Sheets sync error: {e}")
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
        import traceback
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
    """Send email notification"""
    config = load_config()
    
    gmail_email = config.get('gmail_email')
    gmail_password = config.get('gmail_app_password')
    
    if not gmail_email or not gmail_password:
        print("❌ Gmail credentials not configured")
        return
    
    try:
        import smtplib
        from email.mime.text import MIMEText
        from email.mime.multipart import MIMEMultipart
        
        msg = MIMEMultipart()
        msg['From'] = gmail_email
        msg['To'] = ', '.join(recipients)
        msg['Subject'] = subject
        
        msg.attach(MIMEText(body, 'plain', 'utf-8'))
        
        # Connect to Gmail
        server = smtplib.SMTP('smtp.gmail.com', 587)
        server.starttls()
        server.login(gmail_email, gmail_password)
        
        # Send email
        server.send_message(msg)
        server.quit()
        
        print(f"✅ Email sent to: {', '.join(recipients)}")
        
    except Exception as e:
        print(f"❌ Email send error: {e}")
        import traceback
        traceback.print_exc()

def run_scheduler():
    """Background thread for scheduled tasks"""
    print("🔄 Scheduler thread started")
    while True:
        try:
            schedule.run_pending()
            time.sleep(60)  # Check every minute
        except Exception as e:
            print(f"❌ Scheduler error: {e}")
            time.sleep(60)

def start_scheduler():
    """Initialize and start the scheduler"""
    # Schedule midnight sync at 00:00 Israel time
    schedule.every().day.at("00:00").do(midnight_sync_job)
    
    # Also run notifications check every 6 hours
    schedule.every(6).hours.do(check_and_send_notifications)
    
    # Start background scheduler thread
    scheduler_thread = threading.Thread(target=run_scheduler, daemon=True)
    scheduler_thread.start()
    
    print("=" * 80)
    print("✅ Background scheduler started:")
    print("   - Midnight sync at 00:00 (Israel time)")
    print("   - Notifications check every 6 hours")
    print("   - Scheduler thread running in background")
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

