import streamlit as st
import pandas as pd
from datetime import datetime, time, timedelta
import sqlite3
import json
from pathlib import Path
import pytz

# Import our custom modules
from sheets_handler import SheetsHandler
from facebook_handler import FacebookHandler
from database import Database
from scheduler import Scheduler
from notifications import NotificationHandler


import locale
try:
    locale.setlocale(locale.LC_TIME, 'en_GB.UTF-8')  # DD/MM/YYYY format
except:
    pass

# Page configuration
st.set_page_config(
    page_title="Content Approval System",
    page_icon="✅",
    layout="wide"
)

# Initialize session state
if 'db' not in st.session_state:
    st.session_state.db = Database()

if 'sheets_handler' not in st.session_state:
    st.session_state.sheets_handler = None

if 'facebook_handler' not in st.session_state:
    st.session_state.facebook_handler = None

if 'scheduler' not in st.session_state:
    st.session_state.scheduler = None

def init_handlers():
    """Initialize handlers with credentials"""
    config_file = Path("config.json")
    
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
            
        if st.session_state.sheets_handler is None:
            try:
                st.session_state.sheets_handler = SheetsHandler(
                    config.get('google_sheet_id'),
                    config.get('google_credentials_file', 'credentials.json')
                )
            except Exception as e:
                st.error(f"Failed to initialize Google Sheets: {str(e)}")
        
        if st.session_state.facebook_handler is None:
            try:
                st.session_state.facebook_handler = FacebookHandler(
                    config.get('facebook_page_id'),
                    config.get('facebook_access_token')
                )
            except Exception as e:
                st.error(f"Failed to initialize Facebook: {str(e)}")
        
        if st.session_state.scheduler is None:
            st.session_state.scheduler = Scheduler(
                st.session_state.db,
                st.session_state.facebook_handler
            )

def check_for_empty_windows(scheduler):
    """Check if there are empty windows in the next 24 hours"""
    if not scheduler:
        return None
    
    israel_tz = pytz.timezone('Asia/Jerusalem')
    now = datetime.now(israel_tz)
    tomorrow = now + timedelta(hours=24)
    
    # Get posting windows
    windows = scheduler.load_posting_windows()
    
    # Get scheduled posts
    scheduled = scheduler.db.get_scheduled_posts()
    scheduled_times = set()
    for post in scheduled:
        post_dt = datetime.fromisoformat(post['scheduled_time'])
        # Store as date + time for comparison
        scheduled_times.add((post_dt.date(), post_dt.time().replace(second=0, microsecond=0)))
    
    # Check each window in the next 24 hours
    current_date = now.date()
    for days_ahead in range(2):  # Check today and tomorrow
        check_date = current_date + timedelta(days=days_ahead)
        
        for window_time in windows:
            window_dt = israel_tz.localize(datetime.combine(check_date, window_time))
            
            # Only check windows in the next 24 hours
            if now < window_dt <= tomorrow:
                window_key = (check_date, window_time)
                
                if window_key not in scheduled_times:
                    # Found an empty window!
                    return window_dt.strftime('%d/%m/%Y %H:%M')
    
    return None

def main():
    st.title("📝 Content Approval & Publishing System")
    
    # Initialize session state for page if not exists
    if 'current_page' not in st.session_state:
        st.session_state.current_page = "review"
    
    # Check for page change via query params (for navigation)
    query_params = st.query_params
    if 'page' in query_params:
        new_page = query_params['page']
        if new_page != st.session_state.current_page:
            st.session_state.current_page = new_page
            st.rerun()
    
    # Sidebar navigation
    st.sidebar.markdown("### Navigation")
    
    # Custom CSS for navigation
    st.sidebar.markdown("""
    <style>
    .nav-item {
        display: block;
        padding: 14px 16px;
        margin: 10px 0;
        border-radius: 8px;
        text-align: center;
        font-size: 16px;
        font-weight: 500;
        text-decoration: none;
        cursor: pointer;
        transition: all 0.3s ease;
        border: 2px solid transparent;
        min-height: 50px;
        line-height: 22px;
    }
    .nav-item-active {
        background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
        color: white;
        box-shadow: 0 4px 6px rgba(0,0,0,0.1);
    }
    .nav-item-inactive {
        background-color: #f0f2f6;
        color: #31333F;
        border: 2px solid #e0e0e0;
    }
    .nav-item-inactive:hover {
        background-color: #e8eaf0;
        border-color: #667eea;
    }
    </style>
    """, unsafe_allow_html=True)
    
    # Navigation items
    pages = [
        ("review", "📥 Review Entries"),
        ("scheduled", "📅 Scheduled Posts"),
        ("stats", "📊 Statistics"),
        ("settings", "⚙️ Settings")
    ]
    
    # Create navigation
    nav_html = ""
    for page_key, page_label in pages:
        is_active = st.session_state.current_page == page_key
        nav_class = "nav-item-active" if is_active else "nav-item-inactive"
        
        if is_active:
            nav_html += f'<div class="nav-item {nav_class}">{page_label}</div>'
        else:
            nav_html += f'<a href="?page={page_key}" target="_self" style="text-decoration: none;"><div class="nav-item {nav_class}">{page_label}</div></a>'
    
    st.sidebar.markdown(nav_html, unsafe_allow_html=True)
    
    # Map page keys to page names for the rest of the app
    page_map = {
        "review": "📥 Review Entries",
        "scheduled": "📅 Scheduled Posts",
        "stats": "📊 Statistics",
        "settings": "⚙️ Settings"
    }
    
    page = page_map.get(st.session_state.current_page, "📥 Review Entries")
    
    init_handlers()
    
    if page == "⚙️ Settings":
        show_settings_page()
    elif page == "📥 Review Entries":
        show_review_page()
    elif page == "📅 Scheduled Posts":
        show_scheduled_posts_page()
    elif page == "📊 Statistics":
        show_statistics_page()


def show_settings_page():
    st.header("⚙️ Settings")
    
    # Load existing config
    config_file = Path("config.json")
    config = {}
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
    
    # === Google Sheets Configuration ===
    with st.expander("📊 Google Sheets Configuration", expanded=False):
        sheet_id = st.text_input(
            "Google Sheet ID",
            value=config.get('google_sheet_id', ''),
            help="The ID from your Google Sheets URL",
            key="sheet_id"
        )
        
        credentials_file = st.text_input(
            "Google Credentials File",
            value=config.get('google_credentials_file', 'credentials.json'),
            help="Path to your Google API credentials JSON file",
            key="creds_file"
        )
        
        st.info("💡 Make sure your Google Sheets credentials are configured in Streamlit Secrets")
    
    # === Facebook Configuration ===
    with st.expander("📘 Facebook Configuration", expanded=False):
        fb_page_id = st.text_input(
            "Facebook Page ID",
            value=config.get('facebook_page_id', ''),
            help="Your Facebook Page ID",
            key="fb_page"
        )
        
        fb_token = st.text_input(
            "Facebook Access Token",
            value=config.get('facebook_access_token', ''),
            type="password",
            help="Your Facebook Page Access Token",
            key="fb_token"
        )
    
    # === Scheduling Configuration ===
    with st.expander("⏰ Scheduling Configuration", expanded=False):
        st.markdown("### Posting Time Windows")
        st.write("Configure the times when posts should be published (Israel timezone)")
        
        # Get existing windows
        windows = config.get('posting_windows', ['09:00', '14:00', '19:00'])
        
        # Allow editing windows
        num_windows = st.number_input("Number of posting windows per day", min_value=1, max_value=10, value=len(windows), key="num_windows")
        
        new_windows = []
        cols = st.columns(min(num_windows, 3))
        for i in range(num_windows):
            with cols[i % 3]:
                default_time = time(9, 0) if i >= len(windows) else datetime.strptime(windows[i], "%H:%M").time()
                window_time = st.time_input(f"Window {i+1}", value=default_time, key=f"window_{i}")
                new_windows.append(window_time.strftime("%H:%M"))
        
        st.divider()
        
        st.markdown("### Skip Days")
        skip_shabbat = st.checkbox(
            "Skip Fridays and Saturdays",
            value=config.get('skip_shabbat', True),
            help="Don't schedule posts on Shabbat (Friday and Saturday)",
            key="skip_shabbat"
        )
        
        skip_jewish_holidays = st.checkbox(
            "Skip Jewish Holidays (non-work days)",
            value=config.get('skip_jewish_holidays', True),
            help="Don't schedule posts on major Jewish holidays that are non-work days",
            key="skip_holidays"
        )
        
        if skip_jewish_holidays:
            st.info("📅 Skipped holidays: Rosh Hashanah, Yom Kippur, Sukkot, Simchat Torah, Passover, Shavuot")
    
    # === Post Numbering ===
    with st.expander("🔢 Post Numbering", expanded=False):
        starting_number = st.number_input(
            "Starting Number",
            min_value=1,
            value=config.get('starting_number', 1),
            help="The starting number for post numbering",
            key="start_num"
        )
        
        current_number = st.session_state.db.get_current_post_number()
        st.info(f"Current post number: **#{current_number}**")
        
        if st.button("Reset Post Number", key="reset_num"):
            st.session_state.db.reset_post_number(starting_number)
            st.success(f"Post number reset to {starting_number}")
            st.rerun()
    
    # === Sync Configuration ===
    with st.expander("🗓️ Sync Configuration", expanded=False):
        # Get start date from config or use today as default
        default_start_date = datetime.now(pytz.timezone('Asia/Jerusalem')).date()
        if 'sync_start_date' in config and config['sync_start_date']:
            try:
                default_start_date = datetime.fromisoformat(config['sync_start_date']).date()
            except:
                pass  # Use default if parsing fails

        start_date = st.date_input(
            "Start reading from date",
            value=default_start_date,
            help="Only sync entries from this date forward. Older entries will be ignored.",
            format="DD/MM/YYYY",
            key="start_date"
        )

        if st.button("Set Start Date", key="set_date"):
            config['sync_start_date'] = start_date.isoformat()
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            st.success(f"✅ Start date set to {start_date}")
        
        st.divider()
        
        # Last sync info
        last_sync = config.get('last_sync', 'Never')
        st.info(f"Last sync: **{last_sync}**")
        
        if st.button("🔄 Sync Now", key="sync_now"):
            if st.session_state.sheets_handler:
                with st.spinner("Syncing with Google Sheets..."):
                    try:
                        # Get start date filter
                        start_date_str = config.get('sync_start_date')
                        
                        new_entries = st.session_state.sheets_handler.fetch_new_entries()
                        added_count = 0
                        skipped_count = 0
                        
                        for entry in new_entries:
                            # Skip entries before start date
                            if start_date_str:
                                try:
                                    entry_date = pd.to_datetime(entry['timestamp'], dayfirst=True).date()
                                    filter_date = datetime.fromisoformat(start_date_str).date()
                                    if entry_date < filter_date:
                                        skipped_count += 1
                                        continue
                                except:
                                    pass  # If parsing fails, include the entry
                            
                            if st.session_state.db.add_entry(entry['timestamp'], entry['text']):
                                added_count += 1
                        
                        config['last_sync'] = datetime.now(pytz.timezone('Asia/Jerusalem')).strftime("%Y-%m-%d %H:%M:%S")
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                        
                        msg = f"✅ Synced successfully! Added {added_count} new entries."
                        if skipped_count > 0:
                            msg += f" Skipped {skipped_count} entries before {start_date_str}."
                        st.success(msg)
                        
                        # Check if we should send notification
                        if config.get('notifications_enabled', False):
                            pending_count = st.session_state.db.get_statistics()['pending']
                            threshold = config.get('pending_threshold', 20)
                            
                            if pending_count > threshold:
                                notif = NotificationHandler()
                                
                                # Check for empty windows
                                next_empty = check_for_empty_windows(st.session_state.scheduler)
                                notif.send_pending_threshold_alert(pending_count, next_empty)
                                st.info(f"📧 Notification sent - {pending_count} pending entries exceed threshold of {threshold}")
                    except Exception as e:
                        st.error(f"Sync failed: {str(e)}")
            else:
                st.warning("Please configure Google Sheets settings first")
    
    # === Notification Settings ===
    with st.expander("📧 Notification Settings", expanded=False):
        notifications_enabled = st.checkbox(
            "Enable Notifications",
            value=config.get('notifications_enabled', False),
            help="Enable email notifications for pending entries and empty windows",
            key="notif_enabled"
        )
        
        # App URL
        app_url = st.text_input(
            "App URL",
            value=config.get('app_url', 'http://localhost:8501'),
            help="URL of this app (will be included in notification emails)",
            key="app_url"
        )
        
        # Gmail Configuration
        st.markdown("### Gmail Configuration")
        st.info("💡 Use your Gmail address and an App Password (not your regular password)")
        
        gmail_email = st.text_input(
            "Gmail Address",
            value=config.get('gmail_email', ''),
            placeholder="your-email@gmail.com",
            help="Your Gmail address that will send the notifications",
            key="gmail_addr"
        )
        
        gmail_app_password = st.text_input(
            "Gmail App Password",
            value=config.get('gmail_app_password', ''),
            type="password",
            help="Generate this from Google Account settings (not your regular password)",
            key="gmail_pass"
        )
        
        st.markdown("""
        <details>
        <summary>📖 How to get Gmail App Password (click to expand)</summary>
        <ol>
        <li>Go to <a href="https://myaccount.google.com/">Google Account Settings</a></li>
        <li>Click <strong>Security</strong> → <strong>2-Step Verification</strong> (enable if not already)</li>
        <li>Scroll down to <strong>App passwords</strong></li>
        <li>Click <strong>App passwords</strong></li>
        <li>Select <strong>Mail</strong> and <strong>Other (Custom name)</strong></li>
        <li>Name it "Content Approval System"</li>
        <li>Click <strong>Generate</strong></li>
        <li>Copy the 16-character password</li>
        <li>Paste it above</li>
        </ol>
        </details>
        """, unsafe_allow_html=True)
        
        # Pending Threshold
        pending_threshold = st.number_input(
            "Pending Threshold",
            min_value=1,
            max_value=1000,
            value=config.get('pending_threshold', 20),
            help="Send notification when pending entries exceed this number",
            key="pending_thresh"
        )
        
        # Email Addresses
        st.markdown("### Notification Recipients")
        
        # Get existing emails
        existing_emails = config.get('notification_emails', [])
        
        # Input for new email
        new_email = st.text_input("Add email address", placeholder="email@example.com", key="new_email")
        
        col1, col2 = st.columns([1, 5])
        with col1:
            if st.button("➕ Add Email", key="add_email"):
                if new_email and new_email not in existing_emails:
                    existing_emails.append(new_email)
                    config['notification_emails'] = existing_emails
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, indent=2, ensure_ascii=False)
                    st.success(f"Added {new_email}")
                    st.rerun()
                elif new_email in existing_emails:
                    st.warning("Email already exists")
                else:
                    st.warning("Please enter an email address")
        
        # Display existing emails with delete option
        if existing_emails:
            st.write(f"**Configured emails ({len(existing_emails)}):**")
            for idx, email in enumerate(existing_emails):
                col1, col2 = st.columns([5, 1])
                with col1:
                    st.text(email)
                with col2:
                    if st.button("🗑️", key=f"delete_email_{idx}"):
                        existing_emails.remove(email)
                        config['notification_emails'] = existing_emails
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                        st.success(f"Removed {email}")
                        st.rerun()
        else:
            st.info("No email addresses configured yet")
        
        # Test Notification Button
        st.divider()
        if st.button("📧 Send Test Notification", key="test_notif"):
            if not notifications_enabled:
                st.warning("Notifications are disabled. Enable them first!")
            elif not gmail_email or not gmail_app_password:
                st.warning("Gmail credentials not configured!")
            elif not existing_emails:
                st.warning("No email addresses configured!")
            else:
                try:
                    # Temporarily save config for test
                    test_config = config.copy()
                    test_config['notifications_enabled'] = notifications_enabled
                    test_config['gmail_email'] = gmail_email
                    test_config['gmail_app_password'] = gmail_app_password
                    test_config['notification_emails'] = existing_emails
                    test_config['pending_threshold'] = pending_threshold
                    test_config['app_url'] = app_url
                    
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(test_config, f, indent=2, ensure_ascii=False)
                    
                    # Send test notification
                    notif = NotificationHandler()
                    if notif.send_test_notification():
                        st.success("✅ Test notification sent successfully! Check your email.")
                    else:
                        st.error("❌ Failed to send test notification. Check your Gmail credentials.")
                except Exception as e:
                    st.error(f"Error sending test notification: {str(e)}")
    
    # === Database Management ===
    with st.expander("🗑️ Database Management", expanded=False):
        st.warning("⚠️ These actions cannot be undone!")
        
        col1, col2 = st.columns(2)

        with col1:
            if st.button("🗑️ Delete All Pending Entries", type="secondary", key="del_pending"):
                conn = st.session_state.db.get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM entries WHERE status = 'pending'")
                cursor.execute("DELETE FROM processed_timestamps")
                conn.commit()
                conn.close()
                st.success("✅ All pending entries deleted!")
                st.rerun()

        with col2:
            if st.button("⚠️ Clear Entire Database", type="secondary", key="clear_db"):
                if st.checkbox("I'm sure I want to delete everything", key="confirm_clear"):
                    conn = st.session_state.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM entries")
                    cursor.execute("DELETE FROM scheduled_posts")
                    cursor.execute("DELETE FROM processed_timestamps")
                    conn.commit()
                    conn.close()
                    st.success("✅ Database cleared!")
                    st.rerun()
    
    # Save configuration button at the bottom
    st.divider()
    if st.button("💾 Save All Configuration", type="primary", use_container_width=True):
        config = {
            'google_sheet_id': sheet_id,
            'google_credentials_file': credentials_file,
            'facebook_page_id': fb_page_id,
            'facebook_access_token': fb_token,
            'starting_number': starting_number,
            'posting_windows': new_windows,
            'skip_shabbat': skip_shabbat,
            'skip_jewish_holidays': skip_jewish_holidays,
            'notifications_enabled': notifications_enabled,
            'gmail_email': gmail_email,
            'gmail_app_password': gmail_app_password,
            'notification_emails': existing_emails,
            'pending_threshold': pending_threshold,
            'app_url': app_url,
            'last_sync': config.get('last_sync', 'Never'),
            'sync_start_date': config.get('sync_start_date'),
            'last_empty_window_alert': config.get('last_empty_window_alert')
        }
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        st.success("✅ Configuration saved successfully!")
        st.info("Please refresh the page to apply changes")

def show_review_page():
    st.header("📥 Review New Entries")
    
    st.markdown("""
    <style>
        button[kind="primary"] {
            background-color: #28a745 !important;
            border-color: #28a745 !important;
        }
        button[kind="primary"]:hover {
            background-color: #218838 !important;
            border-color: #1e7e34 !important;
        }
        button[kind="secondary"] {
            background-color: #dc3545 !important;
            border-color: #dc3545 !important;
            color: white !important;
        }
        button[kind="secondary"]:hover {
            background-color: #c82333 !important;
            border-color: #bd2130 !important;
        }
        textarea {
            direction: rtl !important;
            text-align: right !important;
        }
    </style>
    """, unsafe_allow_html=True)
    
    # Get pending entries
    pending = st.session_state.db.get_pending_entries()
    
    if not pending:
        st.info("🎉 No pending entries to review!")
        return
    
    st.write(f"**{len(pending)} entries** waiting for review")
    
    # Create 3 columns for desktop, 1 for mobile (responsive)
    cols = st.columns([1, 1, 1])
    
    # Display entries in columns
    for idx, entry in enumerate(pending):
        # Cycle through columns (0, 1, 2, 0, 1, 2, ...)
        col_idx = idx % 3
        
        with cols[col_idx]:
            with st.container(border=True):
                st.caption(f"Entry from {entry['timestamp']}")
                
                # Editable text area with dynamic height
                text_lines = entry['text'].count('\n') + 1
                text_length = len(entry['text'])
                # Estimate lines: prioritize newlines, then character count
                estimated_lines_from_chars = text_length // 40
                # Give more weight to actual newlines (multiply by 1.2 to account for line spacing)
                estimated_lines = max(int(text_lines * 1.5), estimated_lines_from_chars)
                # Calculate height: 40px per line, minimum 250px, maximum 2000px
                calculated_height = min(max(150, estimated_lines * 31), 2000)

                edited_text = st.text_area(
                    "Content",
                    value=entry['text'],
                    height=calculated_height,
                    key=f"text_{entry['id']}",
                    label_visibility="collapsed"
                )
                
                col1, col2 = st.columns(2)
                
                with col1:
                    if st.button("Approve", key=f"approve_{entry['id']}", type="primary", use_container_width=True):
                        # Get next post number
                        post_number = st.session_state.db.get_next_post_number()
                        
                        # Format the post with number
                        formatted_text = f"#{post_number}\n\n{edited_text}"
                        
                        # Approve and schedule
                        st.session_state.db.approve_entry(entry['id'], edited_text, 'admin')
                        
                        # Schedule for next available window
                        if st.session_state.scheduler:
                            scheduled_time = st.session_state.scheduler.schedule_post(
                                entry['id'],
                                formatted_text
                            )
                            st.success(f"✅ Approved and scheduled for {scheduled_time}")
                        else:
                            st.success("✅ Approved!")
                        
                        st.rerun()
                
                with col2:
                    if st.button("Deny", key=f"deny_{entry['id']}", type="secondary", use_container_width=True):
                        st.session_state.db.deny_entry(entry['id'], 'admin')
                        st.success("❌ Entry denied")
                        st.rerun()

def show_scheduled_posts_page():
    st.header("📅 Scheduled Posts")
    
    # Get scheduled posts
    scheduled = st.session_state.db.get_scheduled_posts()
    
    if not scheduled:
        st.info("No posts scheduled for publishing")
        return
    
    st.write(f"**{len(scheduled)} posts** scheduled")
    
    # Group by date
    posts_by_date = {}
    for post in scheduled:
        scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
        date_key = scheduled_dt.date()
        if date_key not in posts_by_date:
            posts_by_date[date_key] = []
        posts_by_date[date_key].append(post)
    
    # Display by date
    for date_key in sorted(posts_by_date.keys()):
        st.subheader(f"📆 {date_key.strftime('%A, %B %d, %Y')}")
        
        for post in sorted(posts_by_date[date_key], key=lambda x: x['scheduled_time']):
            scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
            
            with st.expander(f"🕐 {scheduled_dt.strftime('%H:%M')} - Post #{post['id']}"):
                st.text_area("Content", value=post['text'], height=100, disabled=True, key=f"view_{post['id']}")
                
                col1, col2, col3 = st.columns([2, 2, 6])
                
                with col1:
                    # Allow rescheduling
                    new_time = st.time_input(
                        "Reschedule to",
                        value=scheduled_dt.time(),
                        key=f"reschedule_time_{post['id']}"
                    )
                    
                    if st.button("📅 Reschedule", key=f"reschedule_{post['id']}"):
                        new_datetime = datetime.combine(date_key, new_time)
                        israel_tz = pytz.timezone('Asia/Jerusalem')
                        new_datetime = israel_tz.localize(new_datetime)
                        
                        st.session_state.db.reschedule_post(post['id'], new_datetime.isoformat())
                        st.success("Rescheduled!")
                        st.rerun()
                
                with col2:
                    if st.button("🗑️ Cancel", key=f"cancel_{post['id']}"):
                        st.session_state.db.cancel_scheduled_post(post['id'])
                        st.success("Post cancelled")
                        st.rerun()
    
    # Manual posting
    st.divider()
    st.subheader("📤 Post Immediately")
    
    if st.button("🚀 Publish Next Scheduled Post Now"):
        if scheduled:
            next_post = scheduled[0]
            if st.session_state.facebook_handler:
                with st.spinner("Publishing..."):
                    try:
                        result = st.session_state.facebook_handler.publish_post(next_post['text'])
                        st.session_state.db.mark_as_published(next_post['id'], result['id'])
                        st.success(f"✅ Published successfully! Post ID: {result['id']}")
                        st.rerun()
                    except Exception as e:
                        st.error(f"Failed to publish: {str(e)}")
            else:
                st.error("Facebook handler not configured")

def show_statistics_page():
    st.header("📊 Statistics")
    
    stats = st.session_state.db.get_statistics()
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("Pending Review", stats['pending'])
    
    with col2:
        st.metric("Approved", stats['approved'])
    
    with col3:
        st.metric("Scheduled", stats['scheduled'])
    
    with col4:
        st.metric("Published", stats['published'])
    
    st.divider()
    
    # Recent activity
    st.subheader("📋 Recent Activity")
    
    recent = st.session_state.db.get_recent_activity(20)
    
    if recent:
        df = pd.DataFrame(recent)
        df['timestamp'] = pd.to_datetime(df['timestamp'])
        df = df.sort_values('timestamp', ascending=False)
        
        st.dataframe(
            df[['timestamp', 'status', 'text']],
            use_container_width=True,
            hide_index=True
        )
    else:
        st.info("No activity yet")

if __name__ == "__main__":
    main()
