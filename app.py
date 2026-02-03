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
    locale.setlocale(locale.LC_TIME, 'en_GB.UTF-8')
except:
    pass

# Page configuration
st.set_page_config(
    page_title="מערכת אישור ופרסום תוכן",
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
    
    windows = scheduler.load_posting_windows()
    scheduled_times = scheduler.get_scheduled_times_from_facebook()
    scheduled_slots = set()
    
    for st_dt in scheduled_times:
        scheduled_slots.add((st_dt.date(), st_dt.time().replace(second=0, microsecond=0)))
    
    current_date = now.date()
    for days_ahead in range(2):
        check_date = current_date + timedelta(days=days_ahead)
        
        for window_time in windows:
            window_dt = israel_tz.localize(datetime.combine(check_date, window_time))
            
            if now < window_dt <= tomorrow:
                window_key = (check_date, window_time)
                
                if window_key not in scheduled_slots:
                    return window_dt.strftime('%d/%m/%Y %H:%M')
    
    return None

def main():
    st.title("📝 מערכת אישור ופרסום תוכן")
    
    if 'current_page' not in st.session_state:
        st.session_state.current_page = "review"
    
    query_params = st.query_params
    if 'page' in query_params:
        new_page = query_params['page']
        if new_page != st.session_state.current_page:
            st.session_state.current_page = new_page
            st.rerun()
    
    st.sidebar.markdown("### ניווט")
    
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
    
    pages = [
        ("review", "📥 בדיקת ערכים"),
        ("scheduled", "📅 פוסטים מתוזמנים"),
        ("denied", "🗑️ ערכים שנדחו"),
        ("stats", "📊 סטטיסטיקה"),
        ("settings", "⚙️ הגדרות")
    ]
    
    nav_html = ""
    for page_key, page_label in pages:
        is_active = st.session_state.current_page == page_key
        nav_class = "nav-item-active" if is_active else "nav-item-inactive"
        
        if is_active:
            nav_html += f'<div class="nav-item {nav_class}">{page_label}</div>'
        else:
            nav_html += f'<a href="?page={page_key}" target="_self" style="text-decoration: none;"><div class="nav-item {nav_class}">{page_label}</div></a>'
    
    st.sidebar.markdown(nav_html, unsafe_allow_html=True)
    
    page_map = {
        "review": "📥 בדיקת ערכים",
        "scheduled": "📅 פוסטים מתוזמנים",
        "denied": "🗑️ ערכים שנדחו",
        "stats": "📊 סטטיסטיקה",
        "settings": "⚙️ הגדרות"
    }
    
    page = page_map.get(st.session_state.current_page, "📥 בדיקת ערכים")
    
    init_handlers()
    
    if page == "⚙️ הגדרות":
        show_settings_page()
    elif page == "📥 בדיקת ערכים":
        show_review_page()
    elif page == "📅 פוסטים מתוזמנים":
        show_scheduled_posts_page()
    elif page == "🗑️ ערכים שנדחו":
        show_denied_page()
    elif page == "📊 סטטיסטיקה":
        show_statistics_page()

def calculate_textarea_height(text: str) -> int:
    """Calculate appropriate height for text area based on content"""
    lines = text.count('\n') + 1
    # Count long lines that will wrap (assume 80 chars per line)
    for line in text.split('\n'):
        if len(line) > 80:
            lines += len(line) // 80
    
    # Min 100px, max 400px, ~20px per line
    height = max(100, min(400, lines * 20 + 40))
    return height

def show_review_page():
    st.header("📥 בדיקת ערכים חדשים")
    
    # Cleanup old denied entries
    st.session_state.db.cleanup_old_denied()
    
    # Add sync button at the top
    col1, col2, col3 = st.columns([2, 1, 1])
    with col3:
        if st.button("🔄 סנכרן עכשיו"):
            if st.session_state.sheets_handler:
                with st.spinner("מסנכרן..."):
                    try:
                        config_file = Path("config.json")
                        config = {}
                        if config_file.exists():
                            with open(config_file, 'r', encoding='utf-8') as f:
                                config = json.load(f)
                        
                        start_date_str = config.get('sync_start_date')
                        new_entries = st.session_state.sheets_handler.fetch_new_entries()
                        added_count = 0
                        
                        for entry in new_entries:
                            if start_date_str:
                                try:
                                    entry_date = pd.to_datetime(entry['timestamp'], dayfirst=True).date()
                                    filter_date = datetime.fromisoformat(start_date_str).date()
                                    if entry_date < filter_date:
                                        continue
                                except:
                                    pass
                            
                            if st.session_state.db.add_entry(entry['timestamp'], entry['text']):
                                added_count += 1
                        
                        config['last_sync'] = datetime.now(pytz.timezone('Asia/Jerusalem')).strftime("%Y-%m-%d %H:%M:%S")
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                        
                        st.success(f"✅ נוספו {added_count} ערכים חדשים!")
                        st.rerun()
                    except Exception as e:
                        st.error(f"הסנכרון נכשל: {str(e)}")
            else:
                st.warning("נא להגדיר תחילה את הגדרות Google Sheets")
    
    st.markdown("""
    <style>
        .content-box {
            background-color: #f0f2f6;
            padding: 20px;
            border-radius: 10px;
            margin-bottom: 20px;
        }
        .entry-card {
            margin-bottom: 30px;
        }
        /* Make text areas flexible height */
        textarea {
            min-height: 100px !important;
            max-height: 400px !important;
            height: auto !important;
        }
        /* Green approve button */
        button[kind="primary"] p {
            color: white !important;
        }
        button[kind="primary"] {
            background-color: #28a745 !important;
            border-color: #28a745 !important;
        }
        button[kind="primary"]:hover {
            background-color: #218838 !important;
            border-color: #1e7e34 !important;
        }
        /* Red deny button */
        button[kind="secondary"] p {
            color: white !important;
        }
        button[kind="secondary"] {
            background-color: #dc3545 !important;
            border-color: #dc3545 !important;
        }
        button[kind="secondary"]:hover {
            background-color: #c82333 !important;
            border-color: #bd2130 !important;
        }
        /* Remove gap between button columns */
        div[data-testid="column"] {
            gap: 0 !important;
            padding-left: 2px !important;
            padding-right: 2px !important;
        }
        /* Responsive grid */
        @media (min-width: 1400px) {
            /* 4 columns on very wide screens */
            .stColumn {
                width: 25% !important;
            }
        }
        @media (min-width: 1024px) and (max-width: 1399px) {
            /* 3 columns on wide screens */
            .stColumn {
                width: 33.33% !important;
            }
        }
        @media (min-width: 768px) and (max-width: 1023px) {
            /* 2 columns on medium screens */
            .stColumn {
                width: 50% !important;
            }
        }
        @media (max-width: 767px) {
            /* 1 column on mobile */
            .stColumn {
                width: 100% !important;
            }
        }
    </style>
    """, unsafe_allow_html=True)
    
    pending_entries = st.session_state.db.get_pending_entries()
    
    if not pending_entries:
        st.info("אין ערכים ממתינים לבדיקה")
        return
    
    st.success(f"**{len(pending_entries)} ערכים** ממתינים לבדיקה")
    
    # Create 4 columns for responsive layout
    num_columns = 4
    cols = st.columns(num_columns)
    
    for idx, entry in enumerate(pending_entries):
        col_idx = idx % num_columns
        
        with cols[col_idx]:
            with st.container():
                st.markdown('<div class="entry-card">', unsafe_allow_html=True)
                
                st.markdown(f"### 📅 {entry['timestamp']}")
                
                st.markdown('<div class="content-box">', unsafe_allow_html=True)
                st.markdown(f"**תוכן:**")
                
                edited_text = st.text_area(
                    "ערוך כאן:",
                    value=entry['text'],
                    height=calculate_textarea_height(entry['text']),
                    key=f"text_{entry['id']}",
                    label_visibility="collapsed"
                )
                st.markdown('</div>', unsafe_allow_html=True)
                
                # Buttons side by side
                btn_col1, btn_col2 = st.columns(2)
                
                with btn_col1:
                    if st.button("אשר", key=f"approve_{entry['id']}", type="primary", use_container_width=True):
                        # Approve and assign post number (stored separately in database)
                        st.session_state.db.approve_entry(entry['id'], edited_text, "admin")
                        
                        # Get the assigned post number
                        conn = st.session_state.db.get_connection()
                        cursor = conn.cursor()
                        cursor.execute('SELECT post_number FROM entries WHERE id = ?', (entry['id'],))
                        result = cursor.fetchone()
                        post_number = result['post_number'] if result else 1
                        conn.close()
                        
                        # Format text with number for Facebook only
                        formatted_text = f"#{post_number}\n\n{edited_text}"
                        
                        try:
                            result = st.session_state.scheduler.schedule_post_to_facebook(
                                entry['id'],
                                formatted_text
                            )
                            st.success(f"✅ תוזמן ל-{result['scheduled_time']}")
                            st.rerun()
                        except Exception as e:
                            st.error(f"נכשל בתזמון: {str(e)}")
                
                with btn_col2:
                    if st.button("דחה", key=f"deny_{entry['id']}", type="secondary", use_container_width=True):
                        st.session_state.db.deny_entry(entry['id'], "admin")
                        st.success("הערך נדחה (יישמר ל-24 שעות)")
                        st.rerun()
                
                st.markdown('</div>', unsafe_allow_html=True)

def show_denied_page():
    st.header("🗑️ ערכים שנדחו")
    
    st.info("ערכים נדחים נשמרים ל-24 שעות. לאחר מכן הם נמחקים אוטומטית.")
    
    # Cleanup old entries
    st.session_state.db.cleanup_old_denied()
    
    denied_entries = st.session_state.db.get_denied_entries()
    
    if not denied_entries:
        st.info("אין ערכים נדחים")
        return
    
    st.warning(f"**{len(denied_entries)} ערכים** נדחו ב-24 השעות האחרונות")
    
    # Use same responsive columns as review page
    num_columns = 4
    cols = st.columns(num_columns)
    
    for idx, entry in enumerate(denied_entries):
        col_idx = idx % num_columns
        
        with cols[col_idx]:
            with st.container():
                denied_at = datetime.fromisoformat(entry['denied_at'])
                hours_ago = (datetime.now() - denied_at).total_seconds() / 3600
                hours_remaining = max(0, 24 - hours_ago)
                
                st.markdown(f"### 📅 {entry['timestamp']}")
                st.caption(f"נדחה לפני {hours_ago:.1f} שעות • יימחק בעוד {hours_remaining:.1f} שעות")
                
                st.markdown('<div class="content-box">', unsafe_allow_html=True)
                st.markdown(f"**תוכן:**")
                
                st.text_area(
                    "תוכן:",
                    value=entry['text'],
                    height=calculate_textarea_height(entry['text']),
                    key=f"denied_text_{entry['id']}",
                    disabled=True,
                    label_visibility="collapsed"
                )
                st.markdown('</div>', unsafe_allow_html=True)
                
                if st.button("↩️ החזר להמתנה", key=f"restore_{entry['id']}", use_container_width=True):
                    st.session_state.db.return_denied_to_pending(entry['id'])
                    st.success("הערך הוחזר להמתנה!")
                    st.rerun()

def show_scheduled_posts_page():
    st.header("📅 פוסטים מתוזמנים")
    
    st.markdown("""
    הפוסטים מתוזמנים ישירות ל-Facebook ויפורסמו אוטומטית.
    תוכל לצפות בהם גם ב-[Facebook Creator Studio](https://business.facebook.com/creatorstudio)!
    """)
    
    if not st.session_state.facebook_handler or not st.session_state.scheduler:
        st.warning("נא להגדיר תחילה את הגדרות Facebook")
        return
    
    col1, col2 = st.columns([3, 1])
    with col2:
        if st.button("🔄 סנכרן עם Facebook"):
            with st.spinner("מסנכרן..."):
                st.session_state.scheduler.sync_with_facebook()
                st.success("סונכרן!")
                st.rerun()
    
    try:
        fb_posts = st.session_state.facebook_handler.get_scheduled_posts()
        db_entries = st.session_state.db.get_scheduled_entries()
        
        entry_map = {entry['facebook_post_id']: entry for entry in db_entries}
        
        if not fb_posts:
            st.info("אין פוסטים מתוזמנים כרגע ב-Facebook")
            return
        
        st.success(f"**{len(fb_posts)} פוסטים** מתוזמנים ב-Facebook")
        
        # Sort ALL posts by scheduled time (not grouped by date)
        all_sorted_posts = sorted(fb_posts, key=lambda x: x['scheduled_time'])
        
        # Group by date for display
        posts_by_date = {}
        for post in all_sorted_posts:
            scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
            date_key = scheduled_dt.strftime('%A, %d/%m/%Y')
            
            if date_key not in posts_by_date:
                posts_by_date[date_key] = []
            posts_by_date[date_key].append(post)
        
        # Display by date
        for date_str, posts in sorted(posts_by_date.items()):
            st.subheader(date_str)
            
            for post in posts:
                # Find post's index in ALL posts (not just this date)
                post_idx = next(i for i, p in enumerate(all_sorted_posts) if p['id'] == post['id'])
                
                scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
                time_str = scheduled_dt.strftime('%H:%M')
                
                entry = entry_map.get(post['id'])
                entry_id = entry['id'] if entry else None
                
                with st.container():
                    st.markdown(f"### ⏰ {time_str}")
                    
                    # Check if in edit mode
                    edit_key = f"edit_mode_{post['id']}"
                    if edit_key not in st.session_state:
                        st.session_state[edit_key] = False
                    
                    if st.session_state[edit_key]:
                        # Edit mode - show content without number
                        # Extract content without the #number line
                        message = post['message']
                        if message.startswith('#') and '\n\n' in message:
                            content_only = '\n\n'.join(message.split('\n\n')[1:])
                        else:
                            content_only = message
                        
                        # Get post number from database entry
                        db_post_number = entry.get('post_number') if entry else None
                        
                        new_content = st.text_area(
                            "ערוך תוכן:",
                            value=content_only,
                            height=150,
                            key=f"edit_text_{post['id']}"
                        )
                        
                        col1, col2 = st.columns(2)
                        
                        with col1:
                            if st.button("💾 שמור", key=f"save_{post['id']}", type="primary", use_container_width=True):
                                if entry_id and db_post_number:
                                    # Reconstruct full text with post number from database
                                    full_text = f"#{db_post_number}\n\n{new_content}"
                                    
                                    # Also update the text in database (without number)
                                    st.session_state.db.update_scheduled_post_text(entry_id, new_content)
                                    
                                    with st.spinner("מעדכן ב-Facebook..."):
                                        if st.session_state.scheduler.update_scheduled_post_content(entry_id, full_text):
                                            st.success("✅ הפוסט עודכן!")
                                            st.session_state[edit_key] = False
                                            st.rerun()
                                        else:
                                            st.error("העדכון נכשל")
                        
                        with col2:
                            if st.button("✖️ ביטול", key=f"cancel_{post['id']}", use_container_width=True):
                                st.session_state[edit_key] = False
                                st.rerun()
                    else:
                        # View mode
                        st.text_area(
                            "תוכן הפוסט:",
                            value=post['message'],
                            height=100,
                            key=f"post_{post['id']}",
                            disabled=True
                        )
                        
                        col1, col2, col3, col4 = st.columns(4)
                        
                        with col1:
                            if st.button("✏️ ערוך", key=f"edit_{post['id']}"):
                                st.session_state[edit_key] = True
                                st.rerun()
                        
                        with col2:
                            if st.button("🔙 החזר להמתנה", key=f"unschedule_{post['id']}"):
                                if entry_id:
                                    with st.spinner("מבטל תזמון..."):
                                        if st.session_state.scheduler.unschedule_post(entry_id):
                                            st.success("✅ הוחזר להמתנה!")
                                            st.rerun()
                                        else:
                                            st.error("ביטול התזמון נכשל")
                        
                        with col3:
                            # Move up button (disabled for first post in ALL posts)
                            if post_idx > 0:
                                prev_post = all_sorted_posts[post_idx - 1]
                                prev_entry = entry_map.get(prev_post['id'])
                                if st.button("⬆️", key=f"up_{post['id']}"):
                                    if entry_id and prev_entry:
                                        with st.spinner("מחליף זמנים..."):
                                            if st.session_state.scheduler.swap_post_times(entry_id, prev_entry['id']):
                                                st.success("✅ הזמנים הוחלפו!")
                                                st.rerun()
                                            else:
                                                st.error("ההחלפה נכשלה")
                            else:
                                # Disabled button placeholder
                                st.button("⬆️", key=f"up_{post['id']}", disabled=True)
                        
                        with col4:
                            # Move down button (disabled for last post in ALL posts)
                            if post_idx < len(all_sorted_posts) - 1:
                                next_post = all_sorted_posts[post_idx + 1]
                                next_entry = entry_map.get(next_post['id'])
                                if st.button("⬇️", key=f"down_{post['id']}"):
                                    if entry_id and next_entry:
                                        with st.spinner("מחליף זמנים..."):
                                            if st.session_state.scheduler.swap_post_times(entry_id, next_entry['id']):
                                                st.success("✅ הזמנים הוחלפו!")
                                                st.rerun()
                                            else:
                                                st.error("ההחלפה נכשלה")
                            else:
                                # Disabled button placeholder
                                st.button("⬇️", key=f"down_{post['id']}", disabled=True)
                    
                    st.divider()
        
    except Exception as e:
        st.error(f"שגיאה בטעינת הפוסטים המתוזמנים: {str(e)}")
        st.info("וודא שלטוקן של Facebook יש הרשאה 'pages_manage_posts'")

def show_statistics_page():
    st.header("📊 סטטיסטיקה")
    
    stats = st.session_state.db.get_statistics()
    
    col1, col2, col3, col4 = st.columns(4)
    
    with col1:
        st.metric("ממתינים", stats['pending'])
    
    with col2:
        st.metric("מתוזמנים", stats['scheduled'])
    
    with col3:
        st.metric("פורסמו", stats['published'])
    
    with col4:
        st.metric("נדחו", stats['denied'])
    
    st.divider()
    
    st.subheader("פעילות אחרונה")
    recent = st.session_state.db.get_recent_activity(20)
    
    if recent:
        df = pd.DataFrame(recent)
        st.dataframe(df, use_container_width=True, hide_index=True)
    else:
        st.info("אין פעילות עדיין")

def show_settings_page():
    st.header("⚙️ הגדרות")
    
    config_file = Path("config.json")
    config = {}
    if config_file.exists():
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
    
    with st.expander("📊 הגדרות Google Sheets", expanded=False):
        sheet_id = st.text_input(
            "מזהה גיליון",
            value=config.get('google_sheet_id', ''),
            help="המזהה מכתובת ה-URL של הגיליון",
            key="sheet_id"
        )
        
        credentials_file = st.text_input(
            "קובץ אישורים",
            value=config.get('google_credentials_file', 'credentials.json'),
            help="נתיב לקובץ האישורים של Google API",
            key="creds_file"
        )
        
        st.info("💡 וודא שהאישורים מוגדרים ב-Streamlit Secrets")
    
    with st.expander("📘 הגדרות Facebook", expanded=False):
        fb_page_id = st.text_input(
            "מזהה עמוד",
            value=config.get('facebook_page_id', ''),
            help="מזהה עמוד הפייסבוק שלך",
            key="fb_page"
        )
        
        fb_token = st.text_input(
            "טוקן גישה",
            value=config.get('facebook_access_token', ''),
            type="password",
            help="טוקן הגישה לעמוד הפייסבוק",
            key="fb_token"
        )
    
    with st.expander("⏰ הגדרות תזמון", expanded=False):
        st.markdown("### חלונות פרסום")
        st.write("הגדר את השעות בהן הפוסטים יפורסמו (שעון ישראל)")
        
        windows = config.get('posting_windows', ['09:00', '14:00', '19:00'])
        
        num_windows = st.number_input("מספר חלונות פרסום ביום", min_value=1, max_value=10, value=len(windows), key="num_windows")
        
        new_windows = []
        cols = st.columns(min(num_windows, 3))
        for i in range(num_windows):
            with cols[i % 3]:
                default_time = time(9, 0) if i >= len(windows) else datetime.strptime(windows[i], "%H:%M").time()
                window_time = st.time_input(f"חלון {i+1}", value=default_time, key=f"window_{i}")
                new_windows.append(window_time.strftime("%H:%M"))
        
        st.divider()
        
        st.markdown("### ימים לדילוג")
        skip_shabbat = st.checkbox(
            "דלג על ימי שישי ושבת",
            value=config.get('skip_shabbat', True),
            help="אל תתזמן פוסטים בשבת",
            key="skip_shabbat"
        )
        
        skip_jewish_holidays = st.checkbox(
            "דלג על חגים יהודיים (ימי חג)",
            value=config.get('skip_jewish_holidays', True),
            help="אל תתזמן פוסטים בחגים מרכזיים",
            key="skip_holidays"
        )
        
        if skip_jewish_holidays:
            st.info("📅 חגים שידולגו: ראש השנה, יום כיפור, סוכות, שמחת תורה, פסח, שבועות")
        
        st.divider()
        
        st.markdown("### החל שינויים על פוסטים קיימים")
        st.write("אם שינית את חלונות הפרסום למעלה, לחץ כאן כדי לתזמן מחדש את כל הפוסטים הקיימים:")
        
        if st.button("📅 החל על לוח זמנים קיים", key="apply_windows"):
            if st.session_state.scheduler:
                with st.spinner("מתזמן מחדש את כל הפוסטים ב-Facebook..."):
                    try:
                        count = st.session_state.scheduler.reschedule_all_to_new_windows()
                        st.success(f"✅ תוזמנו מחדש {count} פוסטים!")
                    except Exception as e:
                        st.error(f"שגיאה: {str(e)}")
            else:
                st.warning("המתזמן לא מופעל")
    
    with st.expander("🔢 מספור פוסטים", expanded=False):
        starting_number = st.number_input(
            "מספר התחלתי",
            min_value=1,
            value=config.get('starting_number', 1),
            help="המספר ממנו יתחיל מספור הפוסטים",
            key="start_num"
        )
        
        current_number = st.session_state.db.get_current_post_number()
        st.info(f"מספר פוסט נוכחי: **#{current_number}**")
        
        if st.button("איפוס מספור", key="reset_num"):
            st.session_state.db.reset_post_number(starting_number)
            st.success(f"המספור אופס ל-{starting_number}")
            st.rerun()
    
    with st.expander("🗓️ הגדרות סנכרון", expanded=False):
        default_start_date = datetime.now(pytz.timezone('Asia/Jerusalem')).date()
        if 'sync_start_date' in config and config['sync_start_date']:
            try:
                default_start_date = datetime.fromisoformat(config['sync_start_date']).date()
            except:
                pass

        start_date = st.date_input(
            "התחל לקרוא מתאריך",
            value=default_start_date,
            help="רק ערכים מתאריך זה ואילך יסונכרנו",
            format="DD/MM/YYYY",
            key="start_date"
        )

        if st.button("הגדר תאריך", key="set_date"):
            config['sync_start_date'] = start_date.isoformat()
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            st.success(f"✅ התאריך הוגדר ל-{start_date}")
        
        st.divider()
        
        last_sync = config.get('last_sync', 'אף פעם')
        st.info(f"סנכרון אחרון: **{last_sync}**")
        
        if st.button("🔄 סנכרן עכשיו", key="sync_now"):
            if st.session_state.sheets_handler:
                with st.spinner("מסנכרן עם Google Sheets..."):
                    try:
                        start_date_str = config.get('sync_start_date')
                        
                        new_entries = st.session_state.sheets_handler.fetch_new_entries()
                        added_count = 0
                        skipped_count = 0
                        
                        for entry in new_entries:
                            if start_date_str:
                                try:
                                    entry_date = pd.to_datetime(entry['timestamp'], dayfirst=True).date()
                                    filter_date = datetime.fromisoformat(start_date_str).date()
                                    if entry_date < filter_date:
                                        skipped_count += 1
                                        continue
                                except:
                                    pass
                            
                            if st.session_state.db.add_entry(entry['timestamp'], entry['text']):
                                added_count += 1
                        
                        config['last_sync'] = datetime.now(pytz.timezone('Asia/Jerusalem')).strftime("%Y-%m-%d %H:%M:%S")
                        with open(config_file, 'w', encoding='utf-8') as f:
                            json.dump(config, f, indent=2, ensure_ascii=False)
                        
                        msg = f"✅ סונכרן בהצלחה! נוספו {added_count} ערכים חדשים."
                        if skipped_count > 0:
                            msg += f" דולגו {skipped_count} ערכים לפני {start_date_str}."
                        st.success(msg)
                        
                        if config.get('notifications_enabled', False):
                            pending_count = st.session_state.db.get_statistics()['pending']
                            threshold = config.get('pending_threshold', 20)
                            
                            if pending_count > threshold:
                                notif = NotificationHandler()
                                next_empty = check_for_empty_windows(st.session_state.scheduler)
                                notif.send_pending_threshold_alert(pending_count, next_empty)
                                st.info(f"📧 התראה נשלחה - {pending_count} ערכים ממתינים חורגים מהסף של {threshold}")
                    except Exception as e:
                        st.error(f"הסנכרון נכשל: {str(e)}")
            else:
                st.warning("נא להגדיר תחילה את הגדרות Google Sheets")
    
    with st.expander("📧 הגדרות התראות", expanded=False):
        notifications_enabled = st.checkbox(
            "הפעל התראות",
            value=config.get('notifications_enabled', False),
            help="הפעל התראות אימייל עבור ערכים ממתינים וחלונות ריקים",
            key="notif_enabled"
        )
        
        app_url = st.text_input(
            "כתובת האפליקציה",
            value=config.get('app_url', 'http://localhost:8501'),
            help="כתובת ה-URL של האפליקציה",
            key="app_url"
        )
        
        st.markdown("### הגדרות Gmail")
        st.info("💡 השתמש בכתובת Gmail שלך וב-App Password (לא הסיסמה הרגילה)")
        
        gmail_email = st.text_input(
            "כתובת Gmail",
            value=config.get('gmail_email', ''),
            placeholder="your-email@gmail.com",
            help="כתובת ה-Gmail ממנה יישלחו ההתראות",
            key="gmail_addr"
        )
        
        gmail_app_password = st.text_input(
            "App Password של Gmail",
            value=config.get('gmail_app_password', ''),
            type="password",
            help="צור זאת מהגדרות חשבון Google",
            key="gmail_pass"
        )
        
        st.markdown("""
        <details>
        <summary>📖 איך לקבל Gmail App Password</summary>
        <ol>
        <li>עבור להגדרות חשבון Google</li>
        <li>לחץ על אבטחה → אימות דו-שלבי</li>
        <li>גלול למטה ל-App passwords</li>
        <li>לחץ על App passwords</li>
        <li>בחר Mail ו-Other (Custom name)</li>
        <li>תן לזה שם "Content Approval System"</li>
        <li>לחץ על Generate</li>
        <li>העתק את הסיסמה בת 16 התווים</li>
        <li>הדבק אותה למעלה</li>
        </ol>
        </details>
        """, unsafe_allow_html=True)
        
        pending_threshold = st.number_input(
            "סף ערכים ממתינים",
            min_value=1,
            max_value=1000,
            value=config.get('pending_threshold', 20),
            help="שלח התראה כשהערכים הממתינים עוברים את המספר הזה",
            key="pending_thresh"
        )
        
        st.markdown("### נמעני התראות")
        
        existing_emails = config.get('notification_emails', [])
        
        new_email = st.text_input("הוסף כתובת אימייל", placeholder="email@example.com", key="new_email")
        
        col1, col2 = st.columns([1, 5])
        with col1:
            if st.button("➕ הוסף", key="add_email"):
                if new_email and new_email not in existing_emails:
                    existing_emails.append(new_email)
                    config['notification_emails'] = existing_emails
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(config, f, indent=2, ensure_ascii=False)
                    st.success(f"נוסף {new_email}")
                    st.rerun()
                elif new_email in existing_emails:
                    st.warning("האימייל כבר קיים")
                else:
                    st.warning("נא להזין כתובת אימייל")
        
        if existing_emails:
            st.write(f"**אימיילים מוגדרים ({len(existing_emails)}):**")
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
                        st.success(f"הוסר {email}")
                        st.rerun()
        else:
            st.info("לא הוגדרו כתובות אימייל עדיין")
        
        st.divider()
        if st.button("📧 שלח התראת בדיקה", key="test_notif"):
            if not notifications_enabled:
                st.warning("ההתראות מבוטלות. הפעל אותן תחילה!")
            elif not gmail_email or not gmail_app_password:
                st.warning("פרטי Gmail לא הוגדרו!")
            elif not existing_emails:
                st.warning("לא הוגדרו כתובות אימייל!")
            else:
                try:
                    test_config = config.copy()
                    test_config['notifications_enabled'] = notifications_enabled
                    test_config['gmail_email'] = gmail_email
                    test_config['gmail_app_password'] = gmail_app_password
                    test_config['notification_emails'] = existing_emails
                    test_config['pending_threshold'] = pending_threshold
                    test_config['app_url'] = app_url
                    
                    with open(config_file, 'w', encoding='utf-8') as f:
                        json.dump(test_config, f, indent=2, ensure_ascii=False)
                    
                    notif = NotificationHandler()
                    if notif.send_test_notification():
                        st.success("✅ התראת בדיקה נשלחה בהצלחה! בדוק את האימייל שלך.")
                    else:
                        st.error("❌ שליחת התראת הבדיקה נכשלה. בדוק את פרטי Gmail.")
                except Exception as e:
                    st.error(f"שגיאה בשליחת התראת בדיקה: {str(e)}")
    
    with st.expander("🗑️ ניהול מסד נתונים", expanded=False):
        st.warning("⚠️ פעולות אלה לא ניתנות לביטול!")
        
        col1, col2 = st.columns(2)

        with col1:
            if st.button("🗑️ מחק את כל הערכים הממתינים", type="secondary", key="del_pending"):
                conn = st.session_state.db.get_connection()
                cursor = conn.cursor()
                cursor.execute("DELETE FROM entries WHERE status = 'pending'")
                cursor.execute("DELETE FROM processed_timestamps")
                conn.commit()
                conn.close()
                st.success("✅ כל הערכים הממתינים נמחקו!")
                st.rerun()

        with col2:
            if st.button("⚠️ נקה את כל מסד הנתונים", type="secondary", key="clear_db"):
                if st.checkbox("אני בטוח שאני רוצה למחוק הכל", key="confirm_clear"):
                    conn = st.session_state.db.get_connection()
                    cursor = conn.cursor()
                    cursor.execute("DELETE FROM entries")
                    cursor.execute("DELETE FROM processed_timestamps")
                    conn.commit()
                    conn.close()
                    st.success("✅ מסד הנתונים נוקה!")
                    st.rerun()
    
    st.divider()
    if st.button("💾 שמור את כל ההגדרות", type="primary", use_container_width=True):
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
            'last_sync': config.get('last_sync', 'אף פעם'),
            'sync_start_date': config.get('sync_start_date'),
            'last_empty_window_alert': config.get('last_empty_window_alert')
        }
        
        with open(config_file, 'w', encoding='utf-8') as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        
        st.success("✅ ההגדרות נשמרו בהצלחה!")
        st.info("נא לרענן את הדף כדי להחיל את השינויים")



if __name__ == "__main__":
    main()
