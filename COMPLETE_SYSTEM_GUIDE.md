# 📘 IDF Confessions System - Complete Guide

## 🎯 System Overview

**Flask-based content management system for IDF Confessions Facebook page**

**Features:**
- Auto-sync from Google Sheets
- Content review and approval
- Auto-scheduling to Facebook
- Email notifications
- Database cleanup
- Shabbat/holiday awareness

---

## 🚀 Quick Start

### Installation

```bash
# Extract
tar -xzf flask-app-complete.tar.gz
cd flask-app

# Install dependencies
pip install -r requirements.txt

# Run
python app.py
```

### First Time Setup

1. **Configure Google Sheets:**
   - Add `google_sheet_id` in settings
   - Upload `google_credentials_file` JSON

2. **Configure Facebook:**
   - Add `facebook_page_id`
   - Add `facebook_access_token`

3. **Configure Notifications:**
   - Add `gmail_email`
   - Add `gmail_app_password` (from Google App Passwords)
   - Add `notification_emails` (list)
   - Set `pending_threshold` (default: 10)
   - Enable `notifications_enabled`

4. **Configure Posting:**
   - Set `posting_windows` (e.g., ["09:00", "14:00", "19:00"])
   - Enable `skip_shabbat` (true/false)
   - Enable `skip_jewish_holidays` (true/false)

---

## 📋 Core Workflows

### 1. Review & Approve Entries

**Page:** בדיקת ערכים חדשים (Review Page)

**Process:**
1. New entries sync from Google Sheets automatically (midnight)
2. Entries appear in 4-column masonry layout
3. Review text, edit if needed
4. Click **אשר** (Approve):
   - Entry gets next post number (#152, #153, etc.)
   - Schedules to next available Facebook window
   - Renumbers if conflicts
   - **Runs in background** - instant UI response
5. Click **דחה** (Deny):
   - Moves to denied entries
   - Auto-deletes after 24 hours

**Animation:**
- Cards fade and slide up smoothly (300ms)
- Remaining cards flow up to fill space

**Features:**
- 4 columns on desktop (1400px+)
- Textareas fit content (no scrolling)
- Side-by-side buttons (אשר/דחה)
- No confirmation popups
- Masonry layout (cards stack down)

---

### 2. Manage Scheduled Posts

**Page:** פוסטים מתוזמנים (Scheduled Posts)

**Shows:**
- All posts scheduled in Facebook
- Post number, date/time, day name
- Full post text
- **החזר להמתנה** button to return to pending

**Process to Return:**
1. Click **החזר להמתנה**
2. Deletes from Facebook
3. Returns to pending status
4. **Auto-renumbers all following posts** (-1 each)
5. Updates Facebook with new numbers
6. Decrements global counter

**Features:**
- Single column layout
- Shows Facebook scheduled posts
- Matches with database entries
- Shows post numbers

---

### 3. Manage Denied Entries

**Page:** ערכים שנדחו (Denied Entries)

**Shows:**
- Entries denied in last 24 hours
- Time remaining until auto-delete
- 4-column masonry layout

**Process:**
1. Click **החזר** (Restore)
2. Returns to pending status
3. Can be reviewed again

**Auto-Cleanup:**
- Entries auto-delete after 24 hours
- Runs at midnight

---

## 🤖 Automation Features

### Midnight Auto-Sync

**Runs at:** 00:00 every night

**Actions:**
1. Syncs new entries from Google Sheets
2. Cleans up old database entries:
   - Deletes approved >24h old
   - Deletes denied >24h old
   - Deletes published entries
3. Checks and sends notifications

**Terminal Output:**
```
🌙 MIDNIGHT SYNC STARTED: 2026-02-08 00:00:00
📊 Syncing from Google Sheets...
✅ Synced 15 new entries
🧹 Cleaning up old database entries...
✅ Cleaned up 47 old entries
📧 Checking notifications...
✅ MIDNIGHT SYNC COMPLETE
```

### Email Notifications

**Runs:** Every 6 hours + at midnight

**Triggers:**

**1. Too Many Pending:**
```python
if pending_count >= threshold:  # From settings
    send_email(
        subject=f"⚠️ {pending_count} ערכים ממתינים לבדיקה",
        body="יש כרגע X ערכים שממתינים לאישור..."
    )
```

**2. Next Window Soon:**
```python
if hours_until_next_window < 24:
    send_email(
        subject="⏰ החלון הפנוי הבא בעוד פחות מ-24 שעות",
        body=f"החלון הפנוי הבא: {next_slot}..."
    )
```

**Configuration:**
- `gmail_email`: Your Gmail address
- `gmail_app_password`: 16-char app password from Google
- `notification_emails`: ["email1@example.com", "email2@example.com"]
- `pending_threshold`: 10 (or your preference)
- `notifications_enabled`: true

---

## 🔧 Technical Details

### Double-Booking Prevention

**Problem:** Multiple approvals at same time could schedule to same window

**Solution:** Threading lock + cache clearing

```python
# scheduler.py
class Scheduler:
    _schedule_lock = threading.Lock()  # Class-level lock
    
    def schedule_post_to_facebook(self, entry_id, text):
        with self._schedule_lock:  # LOCK - only one at a time
            # Clear cache
            self.scheduled_times_cache = None
            
            # Get next slot with fresh data
            scheduled_time = self.get_next_available_slot()
            
            # Double-check still empty
            current_scheduled = self.get_scheduled_times_from_facebook()
            if slot_taken:
                scheduled_time = self.get_next_available_slot()
            
            # Schedule (still locked)
            result = self.fb.schedule_post(text, scheduled_time)
```

**How it works:**
1. Lock acquired before checking slots
2. Cache cleared to get fresh Facebook data
3. Slot checked for availability
4. If taken, gets next slot
5. Posts while still locked
6. Lock released after database save
7. **Only ONE approval can schedule at a time**

**Result:** IMPOSSIBLE to double-book!

### Auto-Numbering System

**Post Number Assignment:**
```python
# database.py
def get_next_post_number(self):
    cursor.execute('SELECT current_number FROM post_numbers WHERE id = 1')
    result = cursor.fetchone()
    next_number = result['current_number'] + 1
    
    # Increment counter
    cursor.execute('UPDATE post_numbers SET current_number = ? WHERE id = 1', (next_number,))
    
    return next_number
```

**On Approve:**
- Entry gets next number: #152, #153, etc.
- Number stored in database
- Text formatted: `#{number} {text}`
- Scheduled to Facebook

**On Unschedule (Return):**
```python
# Get removed number (e.g., #150)
unscheduled_number = entry.get('post_number')

# Set entry back to pending
UPDATE entries SET status='pending', post_number=NULL WHERE id=X

# Decrement all following numbers
UPDATE entries SET post_number = post_number - 1 
WHERE post_number > unscheduled_number

# Update on Facebook
for post in posts_needing_renumber:
    new_text = f"#{post['post_number']} {post['text']}"
    fb.update_scheduled_post(post_id, new_text)

# Decrement counter
UPDATE post_numbers SET current_number = current_number - 1
```

**Result:** Numbers always sequential with no gaps!

### Database Structure

**Keeps:**
- ✅ Pending entries (status='pending')
- ✅ Denied entries <24h (status='denied', recent)
- ✅ Scheduled entries (status='scheduled', metadata only)

**Deletes at midnight:**
- ❌ Approved entries >24h old
- ❌ Denied entries >24h old
- ❌ Published entries (already on Facebook)

**Why small database:**
- Scheduled posts fetched from Facebook API (real-time)
- Only need local IDs for matching
- Old entries not needed after posting
- Denied entries temporary (24h review window)

**Check size:**
```bash
sqlite3 database.db "SELECT COUNT(*) FROM entries"
sqlite3 database.db "SELECT status, COUNT(*) FROM entries GROUP BY status"
```

### Next Available Slot Algorithm

```python
def get_next_available_slot(self):
    windows = ['09:00', '14:00', '19:00']  # From config
    
    # Get all scheduled times from Facebook
    scheduled_times = self.get_scheduled_times_from_facebook()
    occupied_slots = {(time.date(), time.time()) for time in scheduled_times}
    
    # Try today's windows
    for window in windows:
        slot = combine(today, window)
        if slot > now and slot not in occupied_slots:
            return slot  # Found empty slot!
    
    # Try future days
    for day in range(1, 365):
        check_date = today + timedelta(days=day)
        
        # Skip Shabbat/holidays if configured
        if should_skip_date(check_date):
            continue
        
        for window in windows:
            slot = combine(check_date, window)
            if slot not in occupied_slots:
                return slot  # Found empty slot!
```

**Features:**
- Checks Facebook for occupied slots
- Skips Shabbat (Friday/Saturday) if enabled
- Skips Jewish holidays if enabled
- Returns first available slot
- Never double-books

---

## 🎨 UI/UX Features

### Page Colors

Each page has unique header color:

| Page | Color | Hex |
|------|-------|-----|
| 📥 בדיקת ערכים | Cyan | #0dcaf0 |
| 📅 פוסטים מתוזמנים | Blue | #0d6efd |
| 🗑️ ערכים שנדחו | Yellow | #ffc107 |
| 📊 סטטיסטיקה | Green | #198754 |
| ⚙️ הגדרות | Gray | #6c757d |

**Implementation:**
```html
<nav style="background-color: 
     {% if request.endpoint == 'review_page' %}#0dcaf0
     {% elif request.endpoint == 'scheduled_page' %}#0d6efd
     ...{% endif %} !important;">
```

### Animations

**Card Removal:**
- Fade out: 300ms
- Slide up: 300ms  
- Remaining cards flow up smoothly

**CSS:**
```css
.entry-card.htmx-swapping {
    opacity: 0;
    transform: translateY(-20px);
    transition: opacity 300ms ease-out, transform 300ms ease-out;
}

.entry-card {
    opacity: 1;
    transform: translateY(0);
    transition: opacity 300ms ease-in, transform 300ms ease-in;
}
```

**HTMX:**
```html
hx-swap="outerHTML swap:200ms settle:300ms"
```

### Masonry Layout

**4-Column Grid:**
```css
#entries-grid {
    column-count: 4;  /* 4 columns */
    column-gap: 1rem;
}

.entry-card {
    break-inside: avoid;  /* Don't split cards */
    margin-bottom: 1rem;
}
```

**Responsive:**
- 1400px+: 4 columns
- 992px-1399px: 3 columns
- 576px-991px: 2 columns
- <576px: 1 column

**Benefits:**
- Cards stack DOWN in each column, then across
- No empty spaces between cards
- Natural heights (no forced same-height)
- Tight packing

---

## ⚙️ Configuration Reference

### config.json Structure

```json
{
  "google_sheet_id": "your_sheet_id_here",
  "google_credentials_file": "credentials.json",
  "read_from_date": "01/01/2025 00:00:00",
  
  "facebook_page_id": "your_page_id",
  "facebook_access_token": "your_access_token",
  
  "posting_windows": ["09:00", "14:00", "19:00"],
  "skip_shabbat": true,
  "skip_jewish_holidays": true,
  
  "notifications_enabled": true,
  "gmail_email": "your@gmail.com",
  "gmail_app_password": "xxxx xxxx xxxx xxxx",
  "notification_emails": ["admin@example.com"],
  "pending_threshold": 10,
  "app_url": "http://localhost:5000",
  
  "last_sync": "2026-02-08 00:00:00"
}
```

### Environment Variables (Optional)

```bash
# If you prefer env vars over config.json:
export GOOGLE_SHEET_ID="..."
export FACEBOOK_PAGE_ID="..."
export FACEBOOK_ACCESS_TOKEN="..."
export GMAIL_EMAIL="..."
export GMAIL_APP_PASSWORD="..."
```

---

## 🐛 Troubleshooting

### Issue: All Pages Same Color

**Cause:** Browser cache or old version

**Fix:**
```bash
# Clear browser cache
Ctrl+Shift+R (or Cmd+Shift+R on Mac)

# Verify using new version
grep "background-color:" templates/base.html
# Should show inline styles with colors
```

### Issue: Animation Not Visible

**Cause:** CSS not loaded or HTMX timing wrong

**Fix:**
```bash
# Check templates have transitions
grep "htmx-swapping" templates/review.html
grep "transition:" templates/review.html

# Should show CSS transitions defined
```

### Issue: Double Booking Still Happening

**Cause:** Threading lock not working or cache issue

**Fix:**
```bash
# Check terminal for lock messages
# Should see:
[LOCK ACQUIRED] Scheduling entry 123
[SCHEDULING] Entry 123 to 2026-02-09 09:00:00
[LOCK RELEASED] Entry 123 scheduled successfully

# If not seeing these, check scheduler.py has:
grep "_schedule_lock" scheduler.py
```

### Issue: Scheduled Posts Empty Boxes

**Cause:** Template accessing wrong data field

**Fix:**
```html
<!-- Should use: -->
{{ item.fb_post.message }}  <!-- ✅ -->
{{ item.fb_post.scheduled_time }}  <!-- ✅ -->

<!-- NOT: -->
{{ item.text }}  <!-- ❌ -->
{{ item.scheduled_time }}  <!-- ❌ -->
```

### Issue: Midnight Sync Not Running

**Cause:** Scheduler thread not started

**Fix:**
```bash
# Check terminal on startup:
✅ Background scheduler started:
   - Midnight sync at 00:00
   - Notifications check every 6 hours

# If not showing, check app.py has:
grep "schedule.every" app.py
grep "scheduler_thread.start" app.py
```

### Issue: Emails Not Sending

**Cause:** Gmail credentials wrong or notifications disabled

**Fix:**
1. Check `notifications_enabled: true` in config
2. Get Gmail App Password:
   - Google Account → Security
   - 2-Step Verification → App Passwords
   - Generate for "Mail"
   - Use 16-char password (no spaces)
3. Check `gmail_email` and `gmail_app_password` in config
4. Check `notification_emails` list not empty

---

## 📊 Statistics & Monitoring

### Database Stats

```bash
# Total entries
sqlite3 database.db "SELECT COUNT(*) FROM entries"

# By status
sqlite3 database.db "SELECT status, COUNT(*) as count FROM entries GROUP BY status"

# Pending entries
sqlite3 database.db "SELECT COUNT(*) FROM entries WHERE status='pending'"

# Scheduled entries
sqlite3 database.db "SELECT id, post_number, scheduled_time FROM entries WHERE status='scheduled' ORDER BY scheduled_time"

# Check for duplicates
sqlite3 database.db "SELECT scheduled_time, COUNT(*) FROM entries WHERE status='scheduled' GROUP BY scheduled_time HAVING COUNT(*) > 1"
```

### Terminal Monitoring

**Startup:**
```
✅ Background scheduler started:
   - Midnight sync at 00:00
   - Notifications check every 6 hours
 * Running on http://0.0.0.0:5000
```

**Approval:**
```
[LOCK ACQUIRED] Scheduling entry 123
[SCHEDULING] Entry 123 to 2026-02-09 09:00:00
[LOCK RELEASED] Entry 123 scheduled successfully
```

**Midnight Sync:**
```
🌙 MIDNIGHT SYNC STARTED: 2026-02-08 00:00:00
📊 Syncing from Google Sheets (from: 01/01/2025 00:00:00)...
✅ Synced 15 new entries from Google Sheets
🧹 Cleaning up old database entries...
✅ Cleaned up 47 old entries
📧 Checking notifications...
✅ Pending entries OK: 5/10
✅ Next window OK: in 48.5 hours
✅ MIDNIGHT SYNC COMPLETE: 2026-02-08 00:00:15
```

**Notifications:**
```
📧 Checking notifications...
📧 Sending notification: 15 pending entries (threshold: 10)
✅ Email sent to: admin@example.com
```

---

## 🔐 Security Notes

### Gmail App Password

**Never use regular Gmail password!**

Use App Password instead:
1. Enable 2-Step Verification on Google Account
2. Go to App Passwords
3. Generate for "Mail"
4. Copy 16-character password
5. Use in `gmail_app_password` config
6. **Never commit to git!**

### Facebook Access Token

- Use Page Access Token (not User Token)
- Set appropriate permissions:
  - `pages_manage_posts`
  - `pages_read_engagement`
- Token never expires (Page Token)
- **Never commit to git!**
- **Never expose publicly!**

### Google Credentials

- Service Account JSON file
- **Never commit to git!**
- Keep in `.gitignore`
- Restrict permissions to specific sheet only

---

## 📦 Deployment

### Production Deployment

```bash
# Use gunicorn instead of development server
pip install gunicorn

# Run with gunicorn
gunicorn -w 4 -b 0.0.0.0:5000 app:app

# Or use systemd service
sudo systemctl enable confessions.service
sudo systemctl start confessions.service
```

### systemd Service File

```ini
[Unit]
Description=IDF Confessions Flask App
After=network.target

[Service]
User=www-data
WorkingDirectory=/path/to/flask-app
ExecStart=/path/to/venv/bin/gunicorn -w 4 -b 0.0.0.0:5000 app:app
Restart=always

[Install]
WantedBy=multi-user.target
```

### Nginx Reverse Proxy

```nginx
server {
    listen 80;
    server_name confessions.example.com;

    location / {
        proxy_pass http://127.0.0.1:5000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
    }
}
```

---

## 📝 Summary

**Core Features:**
- ✅ Auto-sync from Google Sheets (midnight)
- ✅ 4-column masonry review interface
- ✅ Auto-numbering (#1, #2, #3...)
- ✅ Auto-scheduling to Facebook
- ✅ Double-booking prevention (threading lock)
- ✅ Auto-renumbering on removal
- ✅ Email notifications (pending + next window)
- ✅ Database auto-cleanup
- ✅ Shabbat/holiday awareness
- ✅ Smooth animations (300ms)
- ✅ Distinct page colors
- ✅ No confirmation popups
- ✅ Background processing (instant UI)

**Automation:**
- 🌙 Midnight: Sync + Cleanup + Notifications
- 📧 Every 6 hours: Notification check
- 🔄 Real-time: Facebook scheduling
- 🧹 24h: Auto-delete old entries

**User Experience:**
- Fast, smooth animations
- No waiting (background threads)
- Clear visual feedback
- Colorful interface
- Responsive layout
- No popups

**System Ready for Production!** 🚀
