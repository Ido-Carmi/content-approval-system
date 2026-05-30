import sqlite3
import threading
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import time

class Database:
    _post_number_lock = threading.Lock()
    def __init__(self, db_file: str = "content_system.db"):
        """Initialize database connection"""
        self.db_file = db_file
        self.init_database()
    
    def get_connection(self):
        """Get database connection with optimized settings"""
        db_path = Path(self.db_file)
        conn = sqlite3.connect(str(db_path), timeout=30, check_same_thread=False)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=30000")
        conn.row_factory = sqlite3.Row
        return conn
    
    def init_database(self):
        """Create tables if they don't exist"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Entries table - stores all submitted content
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp TEXT NOT NULL,
                text TEXT NOT NULL,
                post_number INTEGER,
                status TEXT DEFAULT 'pending',
                approved_by TEXT,
                approved_at TEXT,
                facebook_post_id TEXT,
                scheduled_time TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Post numbering table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS post_numbers (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_number INTEGER DEFAULT 1
            )
        ''')
        
        # Initialize post number if not exists
        cursor.execute('INSERT OR IGNORE INTO post_numbers (id, current_number) VALUES (1, 1)')
        
        # Processed timestamps to avoid duplicates
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_timestamps (
                timestamp TEXT PRIMARY KEY,
                processed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Hidden comments table - stores auto-hidden comments
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS hidden_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT UNIQUE NOT NULL,
                post_id TEXT NOT NULL,
                post_number INTEGER,
                post_text TEXT,
                comment_text TEXT NOT NULL,
                author_name TEXT,
                author_id TEXT,
                created_at TEXT NOT NULL,
                scanned_at TEXT DEFAULT CURRENT_TIMESTAMP,
                filter_reason TEXT,
                ai_explanation TEXT,
                status TEXT DEFAULT 'shown',
                last_action_at TEXT DEFAULT CURRENT_TIMESTAMP,
                dismissed_at TEXT
            )
        ''')
        
        # Add dismissed_at column if it doesn't exist (for existing databases)
        try:
            cursor.execute('ALTER TABLE hidden_comments ADD COLUMN dismissed_at TEXT')
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Add mentions_page column if it doesn't exist
        try:
            cursor.execute('ALTER TABLE hidden_comments ADD COLUMN mentions_page INTEGER DEFAULT 0')
            conn.commit()
        except sqlite3.OperationalError:
            pass  # Column already exists

        # Auto-comment columns (for time-based comment posting after publish)
        for col_def in [
            'ALTER TABLE entries ADD COLUMN should_comment INTEGER DEFAULT 0',
            'ALTER TABLE entries ADD COLUMN comment_posted INTEGER DEFAULT 0',
        ]:
            try:
                cursor.execute(col_def)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Reconciler actual-state columns — only the reconciler writes these
        for col_def in [
            'ALTER TABLE entries ADD COLUMN fb_scheduled_time TEXT',
            'ALTER TABLE entries ADD COLUMN fb_post_number INTEGER',
            'ALTER TABLE entries ADD COLUMN fb_text_hash TEXT',
            'ALTER TABLE entries ADD COLUMN fb_published_at TEXT',
        ]:
            try:
                cursor.execute(col_def)
                conn.commit()
            except sqlite3.OperationalError:
                pass  # Column already exists

        # Persistent journal for the comment action queue
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS action_queue_journal (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                queue_name TEXT NOT NULL,
                action TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT DEFAULT 'pending',
                retry_count INTEGER DEFAULT 0,
                error TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_aqj_queue_status ON action_queue_journal(queue_name, status)')

        # Create indexes for hidden_comments
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_status ON hidden_comments(status)')
        cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_created ON hidden_comments(created_at)')
        
        # Comment queue for retry when hitting API limits
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comment_queue (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT UNIQUE NOT NULL,
                post_id TEXT NOT NULL,
                comment_data TEXT NOT NULL,
                retry_count INTEGER DEFAULT 0,
                queued_at TEXT DEFAULT CURRENT_TIMESTAMP,
                last_retry TEXT
            )
        ''')

        # Dead-letter queue: comments that exceeded max retries
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS failed_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT UNIQUE NOT NULL,
                post_id TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                author_name TEXT,
                retry_count INTEGER DEFAULT 0,
                error_reason TEXT,
                failed_at TEXT DEFAULT CURRENT_TIMESTAMP,
                comment_data TEXT NOT NULL
            )
        ''')
        
        # Post tracking for sliding window monitoring
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS post_tracking (
                post_id TEXT PRIMARY KEY,
                post_number INTEGER,
                post_text TEXT,
                published_at TEXT NOT NULL,
                last_comment_at TEXT,
                last_checked_at TEXT,
                last_fetch_time TEXT,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # AI feedback table for training
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_feedback (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                ai_prediction TEXT,
                ai_reason TEXT,
                actual_action TEXT NOT NULL,
                correct_reason TEXT,
                feedback_type TEXT NOT NULL,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                sent_to_ai BOOLEAN DEFAULT 0,
                sent_at TEXT
            )
        ''')
        
        # AI examples table - permanent curated examples (max 10 per category)
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS ai_examples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                category TEXT NOT NULL,
                comment_text TEXT NOT NULL,
                original_ai_prediction TEXT,
                explanation TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(category, comment_text)
            )
        ''')
        
        # Index for fast category lookup
        cursor.execute('''
            CREATE INDEX IF NOT EXISTS idx_examples_category 
            ON ai_examples(category)
        ''')
        
        # Training data table - saves ALL marked comments for future AI training
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS comment_training_data (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT NOT NULL,
                post_id TEXT,
                comment_text TEXT NOT NULL,
                admin_label TEXT NOT NULL,
                ai_prediction TEXT,
                ai_explanation TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        # Instagram post log — lightweight record of published posts for the IG daily job.
        # Kept separate so it survives cleanup_old_entries() which deletes published entries.
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS instagram_post_log (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fb_post_id TEXT UNIQUE NOT NULL,
                post_number INTEGER,
                text TEXT NOT NULL,
                published_at TEXT NOT NULL,
                ig_posted INTEGER DEFAULT 0,
                ig_posted_at TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        cursor.execute(
            'CREATE INDEX IF NOT EXISTS idx_ig_log_published ON instagram_post_log(published_at)'
        )

        conn.commit()
        conn.close()

    def add_entry(self, timestamp: str, text: str) -> bool:
        """
        Add a new entry if timestamp hasn't been processed
        
        Returns:
            True if added, False if duplicate
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # Check if already processed
            cursor.execute('SELECT 1 FROM processed_timestamps WHERE timestamp = ?', (timestamp,))
            if cursor.fetchone():
                conn.close()
                return False
            
            # Add entry
            cursor.execute('''
                INSERT INTO entries (timestamp, text, status)
                VALUES (?, ?, 'pending')
            ''', (timestamp, text))
            
            # Mark timestamp as processed
            cursor.execute('''
                INSERT INTO processed_timestamps (timestamp)
                VALUES (?)
            ''', (timestamp,))
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False
    
    def get_pending_entries(self) -> List[Dict]:
        """Get all pending entries"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, timestamp, text
            FROM entries
            WHERE status = 'pending'
            ORDER BY timestamp ASC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def approve_entry(self, entry_id: int, edited_text: str, approved_by: str,
                      scheduled_time: str = None) -> bool:
        """Mark entry as scheduled, assign post number and optional time slot.
        Returns False if entry was already approved/denied (guards against double-submit)."""
        conn = self.get_connection()
        cursor = conn.cursor()

        post_number = self.get_next_post_number()

        cursor.execute('''
            UPDATE entries
            SET status = 'scheduled',
                text = ?,
                post_number = ?,
                approved_by = ?,
                approved_at = ?,
                scheduled_time = ?
            WHERE id = ? AND status = 'pending'
        ''', (edited_text, post_number, approved_by, datetime.now().isoformat(),
              scheduled_time, entry_id))

        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0

    def get_desired_slots(self) -> List[str]:
        """Return all desired scheduled_time values for non-published scheduled entries.
        Used by the approve route for local slot-picking (avoids querying Facebook)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT scheduled_time FROM entries
            WHERE status = 'scheduled'
              AND scheduled_time IS NOT NULL
              AND (fb_published_at IS NULL OR fb_published_at = '')
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [row['scheduled_time'] for row in rows]
    
    def set_should_comment(self, entry_id: int, bitmask: int):
        """Set comment bitmask for a post.
        Bits: slot1=1, slot2=2, slot3=4. 0=none. Resets comment_posted for newly enabled slots."""
        bitmask = max(0, min(7, int(bitmask)))
        conn = self.get_connection()
        cursor = conn.cursor()
        # Preserve comment_posted only for slots that remain enabled
        cursor.execute(
            'UPDATE entries SET should_comment = ?, comment_posted = (comment_posted & ?) WHERE id = ?',
            (bitmask, bitmask, entry_id))
        conn.commit()
        conn.close()

    def get_pending_auto_comments(self) -> List[Dict]:
        """Return entries with at least one unposted comment slot past their scheduled time."""
        import pytz
        conn = self.get_connection()
        cursor = conn.cursor()
        # Fetch candidates without time filter — SQLite's datetime() strips timezone
        # offsets (+03:00) on some versions, causing a 3-hour delay. Filter in Python.
        cursor.execute('''
            SELECT id, facebook_post_id, post_number, text, should_comment, comment_posted,
                   scheduled_time
            FROM entries
            WHERE should_comment > 0
              AND (should_comment & ~comment_posted) > 0
              AND facebook_post_id IS NOT NULL
        ''')
        rows = cursor.fetchall()
        conn.close()

        israel_tz = pytz.timezone('Asia/Jerusalem')
        threshold = datetime.now(pytz.utc) - timedelta(minutes=2)

        result = []
        for row in rows:
            d = dict(row)
            st = d.get('scheduled_time', '')
            if not st:
                continue
            try:
                scheduled_dt = datetime.fromisoformat(st)
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = israel_tz.localize(scheduled_dt)
                if scheduled_dt.astimezone(pytz.utc) <= threshold:
                    result.append(d)
            except Exception:
                pass
        return result

    def mark_comment_posted(self, entry_id: int, slot_bit: int):
        """OR in the slot bit so we know this slot's comment was posted."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET comment_posted = comment_posted | ? WHERE id = ?',
                       (slot_bit, entry_id))
        conn.commit()
        conn.close()

    def remove_auto_comment(self, entry_id: int):
        """Clear comment slot selection for a post."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('UPDATE entries SET should_comment = 0, comment_posted = 0 WHERE id = ?', (entry_id,))
        conn.commit()
        conn.close()

    def schedule_to_facebook(self, entry_id: int, facebook_post_id: str, scheduled_time: str):
        """
        Mark entry as scheduled with Facebook post ID.
        Also mirrors scheduled_time into fb_scheduled_time so the reconciler
        doesn't see spurious time-drift for posts scheduled by legacy paths
        (reschedule_all_to_new_windows, reschedule_canonical, reschedule_today).
        """
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE entries
            SET status = 'scheduled',
                facebook_post_id = ?,
                scheduled_time = ?,
                fb_scheduled_time = ?
            WHERE id = ?
        ''', (facebook_post_id, scheduled_time, scheduled_time, entry_id))

        conn.commit()
        conn.close()
    
    def unschedule_entry(self, entry_id: int) -> dict:
        """Return a scheduled entry to pending.
        Slides time-slots for all following posts (each takes the freed slot above it),
        decrements their post_numbers, and signals the reconciler via caller.
        Does NOT clear fb_* columns — the reconciler owns those.
        Returns {'freed_number': int, 'freed_time': str} or {} if entry not found/already pending."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT post_number, scheduled_time, status, fb_published_at FROM entries WHERE id = ?',
                (entry_id,)
            )
            row = cursor.fetchone()
            if not row or row['status'] != 'scheduled':
                return {}
            if row['fb_published_at']:
                return {'error': 'published'}

            freed_number = row['post_number']
            freed_time   = row['scheduled_time']

            # Desired-state: mark as pending, clear number + time (fb_* columns stay — reconciler clears them)
            cursor.execute('''
                UPDATE entries SET status='pending', post_number=NULL, scheduled_time=NULL
                WHERE id=?
            ''', (entry_id,))

            if freed_number:
                # Slide following posts: each gets the slot of the post before it
                cursor.execute('''
                    SELECT id, post_number, scheduled_time FROM entries
                    WHERE post_number > ? AND status='scheduled' AND (fb_published_at IS NULL OR fb_published_at='')
                    ORDER BY post_number ASC
                ''', (freed_number,))
                following = cursor.fetchall()

                prev_time = freed_time
                for f in following:
                    cursor.execute('''
                        UPDATE entries SET post_number=post_number-1, scheduled_time=?
                        WHERE id=?
                    ''', (prev_time, f['id']))
                    prev_time = f['scheduled_time']

                cursor.execute('UPDATE post_numbers SET current_number=current_number-1 WHERE id=1')

            conn.commit()
            return {'freed_number': freed_number, 'freed_time': freed_time}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def deny_scheduled_entry(self, entry_id: int) -> dict:
        """Deny a scheduled post — same cascade as unschedule but status='denied'.
        For pending posts use deny_entry() instead.
        Returns {'freed_number': int} or {'error': reason}."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            cursor.execute(
                'SELECT post_number, scheduled_time, status, fb_published_at FROM entries WHERE id=?',
                (entry_id,)
            )
            row = cursor.fetchone()
            if not row:
                return {'error': 'not_found'}
            if row['status'] == 'published' or row['fb_published_at']:
                return {'error': 'published'}
            if row['status'] != 'scheduled':
                return {'error': f"wrong_status:{row['status']}"}

            freed_number = row['post_number']
            freed_time   = row['scheduled_time']

            cursor.execute("UPDATE entries SET status='denied' WHERE id=?", (entry_id,))

            if freed_number:
                cursor.execute('''
                    SELECT id, post_number, scheduled_time FROM entries
                    WHERE post_number > ? AND status='scheduled' AND (fb_published_at IS NULL OR fb_published_at='')
                    ORDER BY post_number ASC
                ''', (freed_number,))
                following = cursor.fetchall()
                prev_time = freed_time
                for f in following:
                    cursor.execute('''
                        UPDATE entries SET post_number=post_number-1, scheduled_time=?
                        WHERE id=?
                    ''', (prev_time, f['id']))
                    prev_time = f['scheduled_time']
                cursor.execute('UPDATE post_numbers SET current_number=current_number-1 WHERE id=1')

            conn.commit()
            return {'freed_number': freed_number}
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()
    
    def get_posts_needing_renumber(self, from_number: int) -> List[Dict]:
        """Get all scheduled posts with post_number >= from_number that need updating on Facebook"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, text, post_number, facebook_post_id, scheduled_time
            FROM entries
            WHERE status = 'scheduled' AND post_number >= ?
            ORDER BY post_number ASC
        ''', (from_number,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def get_scheduled_entries(self) -> List[Dict]:
        """Get all scheduled entries with Facebook post IDs"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, text, post_number, facebook_post_id, scheduled_time,
                   should_comment, comment_posted
            FROM entries
            WHERE status = 'scheduled'
            ORDER BY scheduled_time ASC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def mark_as_published(self, entry_id: int):
        """Mark entry as published"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'published'
            WHERE id = ?
        ''', (entry_id,))
        
        conn.commit()
        conn.close()
    
    def deny_entry(self, entry_id: int, denied_by: str) -> bool:
        """Mark entry as denied with timestamp.
        Returns False if entry was already processed (guards against double-submit)."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('''
            UPDATE entries
            SET status = 'denied',
                approved_by = ?,
                approved_at = ?
            WHERE id = ? AND status = 'pending'
        ''', (denied_by, datetime.now().isoformat(), entry_id))

        affected = cursor.rowcount
        conn.commit()
        conn.close()
        return affected > 0
    
    def were_posts_approved_since(self, since_timestamp: str) -> bool:
        """Check if any posts were approved (and scheduled) since the given timestamp"""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT COUNT(*) FROM entries
            WHERE status IN ('scheduled', 'published')
            AND approved_at >= ?
        ''', (since_timestamp,))
        count = cursor.fetchone()[0]
        conn.close()
        return count > 0

    def get_denied_entries(self) -> List[Dict]:
        """Get denied entries from last 24 hours"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get entries denied in last 24 hours
        cutoff_time = (datetime.now() - timedelta(hours=24)).isoformat()
        
        cursor.execute('''
            SELECT id, timestamp, text, approved_at as denied_at
            FROM entries
            WHERE status = 'denied' AND approved_at > ?
            ORDER BY approved_at DESC
        ''', (cutoff_time,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def return_denied_to_pending(self, entry_id: int):
        """Return a denied entry back to pending"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'pending',
                approved_by = NULL,
                approved_at = NULL
            WHERE id = ?
        ''', (entry_id,))
        
        conn.commit()
        conn.close()
    
    def cleanup_old_denied(self):
        """Delete denied entries older than 24 hours"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cutoff_time = (datetime.now() - timedelta(hours=24)).isoformat()
        
        cursor.execute('''
            DELETE FROM entries
            WHERE status = 'denied' AND approved_at < ?
        ''', (cutoff_time,))
        
        conn.commit()
        conn.close()
    
    def update_scheduled_post_text(self, entry_id: int, new_text: str):
        """Update the text of a scheduled post"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET text = ?
            WHERE id = ?
        ''', (new_text, entry_id))
        
        conn.commit()
        conn.close()
    
    def swap_scheduled_times(self, entry_id1: int, entry_id2: int):
        """Swap the scheduled times AND post numbers of two posts"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get both entries' scheduled times, post numbers, and Facebook IDs
        cursor.execute('''
            SELECT id, scheduled_time, post_number, facebook_post_id
            FROM entries
            WHERE id IN (?, ?)
        ''', (entry_id1, entry_id2))
        
        rows = cursor.fetchall()
        if len(rows) != 2:
            conn.close()
            return False
        
        entries = [dict(row) for row in rows]
        
        # Swap scheduled times
        cursor.execute('''
            UPDATE entries
            SET scheduled_time = ?
            WHERE id = ?
        ''', (entries[1]['scheduled_time'], entries[0]['id']))
        
        cursor.execute('''
            UPDATE entries
            SET scheduled_time = ?
            WHERE id = ?
        ''', (entries[0]['scheduled_time'], entries[1]['id']))
        
        # Swap post numbers
        cursor.execute('''
            UPDATE entries
            SET post_number = ?
            WHERE id = ?
        ''', (entries[1]['post_number'], entries[0]['id']))
        
        cursor.execute('''
            UPDATE entries
            SET post_number = ?
            WHERE id = ?
        ''', (entries[0]['post_number'], entries[1]['id']))
        
        conn.commit()
        conn.close()
        
        return {
            'post1_fb_id': entries[0]['facebook_post_id'],
            'post2_fb_id': entries[1]['facebook_post_id'],
            'time1': entries[0]['scheduled_time'],
            'time2': entries[1]['scheduled_time'],
            'number1': entries[0]['post_number'],
            'number2': entries[1]['post_number']
        }
    
    def get_next_post_number(self) -> int:
        """Get and increment the post number. Starts from the counter and skips
        any numbers already in use, so reset_post_number() is always honoured.
        Class-level lock makes the read-modify-write atomic across threads."""
        with Database._post_number_lock:
            conn = self.get_connection()
            cursor = conn.cursor()

            cursor.execute('SELECT current_number FROM post_numbers WHERE id = 1')
            counter = cursor.fetchone()['current_number']

            cursor.execute('SELECT post_number FROM entries WHERE post_number IS NOT NULL')
            used = {row['post_number'] for row in cursor.fetchall()}

            next_number = counter
            while next_number in used:
                next_number += 1

            cursor.execute('UPDATE post_numbers SET current_number = ? WHERE id = 1', (next_number + 1,))
            conn.commit()
            conn.close()

            return next_number

    def get_current_post_number(self) -> int:
        """Get what the next post number would be without incrementing."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cursor.execute('SELECT current_number FROM post_numbers WHERE id = 1')
        counter = cursor.fetchone()['current_number']

        cursor.execute('SELECT post_number FROM entries WHERE post_number IS NOT NULL')
        used = {row['post_number'] for row in cursor.fetchall()}

        conn.close()
        next_number = counter
        while next_number in used:
            next_number += 1
        return next_number
    
    def reset_post_number(self, number: int = 1):
        """Reset post number to specified value"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE post_numbers SET current_number = ? WHERE id = 1', (number,))
        
        conn.commit()
        conn.close()
    
    def increment_post_counter(self):
        """Increment the post counter by 1"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE post_numbers SET current_number = current_number + 1 WHERE id = 1')
        
        conn.commit()
        conn.close()
    
    def decrement_post_counter(self):
        """Decrement the post counter by 1"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE post_numbers SET current_number = current_number - 1 WHERE id = 1')
        
        conn.commit()
        conn.close()
    
    def get_statistics(self) -> Dict:
        """Get statistics about entries and comments"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        stats = {}
        
        # Entry statistics
        for status in ['pending', 'approved', 'scheduled', 'denied']:
            cursor.execute('SELECT COUNT(*) as count FROM entries WHERE status = ?', (status,))
            stats[status] = cursor.fetchone()['count']
        
        # Published count - just count entries with status='published'
        cursor.execute('SELECT COUNT(*) as count FROM entries WHERE status = ?', ('published',))
        stats['published'] = cursor.fetchone()['count']
        
        # Comment statistics
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments')
        stats['comments_total'] = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments WHERE status = ?', ('hidden',))
        stats['comments_hidden'] = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments WHERE filter_reason = ?', ('political',))
        stats['comments_political'] = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments WHERE filter_reason = ?', ('hate',))
        stats['comments_hate'] = cursor.fetchone()['count']
        
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments WHERE filter_reason = ?', ('spam',))
        stats['comments_spam'] = cursor.fetchone()['count']
        
        # AI Examples statistics
        cursor.execute('SELECT COUNT(*) as count FROM ai_examples')
        stats['ai_examples'] = cursor.fetchone()['count']
        
        conn.close()
        return stats
    
    def get_recent_activity(self, limit: int = 20) -> List[Dict]:
        """Get recent entries"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT timestamp, status, text
            FROM entries
            ORDER BY created_at DESC
            LIMIT ?
        ''', (limit,))
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def cleanup_old_entries(self) -> int:
        """
        Remove old entries to keep database small.
        Keep only:
        - Pending entries (waiting for review)
        - Denied entries < 24 hours old
        - Scheduled entries (metadata only)
        
        Remove:
        - Approved entries > 24 hours old
        - Denied entries > 24 hours old
        - Published entries (already on Facebook)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        total_deleted = 0
        
        # Delete approved entries older than 24 hours
        cursor.execute('''
            DELETE FROM entries
            WHERE status = 'approved'
            AND COALESCE(approved_at, created_at) < datetime('now', '-1 day')
        ''')
        approved_deleted = cursor.rowcount
        total_deleted += approved_deleted

        # Delete denied entries older than 24 hours
        cursor.execute('''
            DELETE FROM entries
            WHERE status = 'denied'
            AND COALESCE(approved_at, created_at) < datetime('now', '-1 day')
        ''')
        denied_deleted = cursor.rowcount
        total_deleted += denied_deleted
        
        # Delete published entries (already on Facebook, no longer needed)
        cursor.execute('''
            DELETE FROM entries
            WHERE status = 'published'
        ''')
        published_deleted = cursor.rowcount
        total_deleted += published_deleted
        
        conn.commit()
        conn.close()
        
        if total_deleted > 0:
            print(f"🧹 Database cleanup: Deleted {total_deleted} old entries")
            print(f"   - Approved (>24h): {approved_deleted}")
            print(f"   - Denied (>24h): {denied_deleted}")
            print(f"   - Published: {published_deleted}")
        
        return total_deleted
    
    # ==================== COMMENTS FILTER METHODS ====================
    
    def add_comment(self, comment_data: Dict) -> bool:
        """Add a comment that needs action (only if not already in DB)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            comment_id = comment_data['comment_id']
            
            # Check if comment already exists (dismissed or not)
            cursor.execute('SELECT dismissed_at, status FROM hidden_comments WHERE comment_id = ?', 
                          (comment_id,))
            existing = cursor.fetchone()
            
            if existing:
                dismissed = existing['dismissed_at']
                status = existing['status']
                # Comment exists - don't overwrite it
                # This preserves dismissed_at and prevents re-showing dismissed comments
                if dismissed:
                    print(f"   [DB] Skipping {comment_id[:30]}... (already dismissed)")
                else:
                    print(f"   [DB] Skipping {comment_id[:30]}... (already in DB, status={status})")
                conn.close()
                return False
            
            # Determine initial status based on AI decision
            initial_status = 'hidden' if comment_data.get('should_hide', False) else 'shown'
            
            # Get filter reason - use 'clean' if no violation
            filter_reason = comment_data.get('filter_reason') or 'clean'
            
            print(f"   [DB] Adding new comment {comment_id[:30]}... (status={initial_status})")
            
            cursor.execute('''
                INSERT INTO hidden_comments
                (comment_id, post_id, post_number, post_text, comment_text,
                 author_name, author_id, created_at, filter_reason, ai_explanation, status, mentions_page)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            ''', (
                comment_data['comment_id'],
                comment_data['post_id'],
                comment_data.get('post_number'),
                comment_data.get('post_text', '')[:200],
                comment_data['comment_text'],
                comment_data.get('author_name', ''),
                comment_data.get('author_id', ''),
                comment_data['created_at'],
                filter_reason,
                comment_data.get('ai_explanation', ''),
                initial_status,
                comment_data.get('mentions_page', 0),
            ))
            
            # Update last_comment_at for sliding window (same transaction)
            cursor.execute('''
                UPDATE post_tracking 
                SET last_comment_at = ?, updated_at = ?
                WHERE post_id = ?
            ''', (comment_data['created_at'], datetime.now().isoformat(), comment_data['post_id']))
            
            conn.commit()
            conn.close()
            return True
        except Exception as e:
            print(f"Error adding comment: {e}")
            conn.close()
            return False
    
    def get_all_comments(self, filter_status: Optional[str] = None, days: int = 7) -> List[Dict]:
        """Get all comments from last N days, optionally filtered by status, excluding dismissed"""
        print(f"   [DB] get_all_comments called: filter={filter_status}, days={days}")
        
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        print(f"   [DB] Cutoff date: {cutoff}")
        
        # First, check if table has any data at all
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments')
        total = cursor.fetchone()['count']
        print(f"   [DB] Total comments in table: {total}")
        
        # Count dismissed comments
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments WHERE dismissed_at IS NOT NULL')
        dismissed_count = cursor.fetchone()['count']
        if dismissed_count > 0:
            print(f"   [DB] Dismissed comments (hidden from view): {dismissed_count}")
        
        if filter_status and filter_status != 'all':
            print(f"   [DB] Filtering by status: {filter_status}")
            cursor.execute('''
                SELECT * FROM hidden_comments
                WHERE created_at >= ?
                  AND status = ?
                  AND dismissed_at IS NULL
                ORDER BY mentions_page DESC, created_at DESC
            ''', (cutoff, filter_status))
        else:
            print(f"   [DB] Getting all comments (no status filter, excluding dismissed)")
            cursor.execute('''
                SELECT * FROM hidden_comments
                WHERE created_at >= ?
                  AND dismissed_at IS NULL
                ORDER BY mentions_page DESC, created_at DESC
            ''', (cutoff,))
        
        rows = cursor.fetchall()
        print(f"   [DB] Query returned {len(rows)} rows")
        
        conn.close()
        result = [dict(row) for row in rows]
        
        if result:
            print(f"   [DB] First result: {result[0]}")
        
        return result
    
    def update_comment_status(self, comment_id: str, new_status: str) -> bool:
        """Update comment status (shown/hidden/deleted)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE hidden_comments 
            SET status = ?, last_action_at = ?
            WHERE comment_id = ?
        ''', (new_status, datetime.now().isoformat(), comment_id))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def get_comments_stats(self, days: int = 7) -> Dict:
        """Get statistics about comments (excluding dismissed)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        # Exclude dismissed comments from stats
        cursor.execute('''
            SELECT 
                COUNT(*) as total,
                SUM(CASE WHEN status = 'shown' THEN 1 ELSE 0 END) as shown,
                SUM(CASE WHEN status = 'hidden' THEN 1 ELSE 0 END) as hidden,
                SUM(CASE WHEN status = 'deleted' THEN 1 ELSE 0 END) as deleted,
                SUM(CASE WHEN filter_reason = 'political' THEN 1 ELSE 0 END) as political,
                SUM(CASE WHEN filter_reason = 'hate' THEN 1 ELSE 0 END) as hate,
                SUM(CASE WHEN filter_reason IS NULL THEN 1 ELSE 0 END) as clean
            FROM hidden_comments
            WHERE created_at >= ?
              AND dismissed_at IS NULL
        ''', (cutoff,))
        
        row = cursor.fetchone()
        conn.close()
        
        return {
            'total': row['total'] or 0,
            'shown': row['shown'] or 0,
            'hidden': row['hidden'] or 0,
            'deleted': row['deleted'] or 0,
            'political': row['political'] or 0,
            'hate': row['hate'] or 0,
            'clean': row['clean'] or 0
        }
    
    def clear_all_comments(self) -> int:
        """Delete ALL comments from database (but preserve AI examples)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) as count FROM hidden_comments')
        total = cursor.fetchone()['count']
        
        cursor.execute('DELETE FROM hidden_comments')
        deleted = cursor.rowcount
        
        conn.commit()
        conn.close()
        
        print(f"🗑️ Cleared {deleted} comments from database (AI examples preserved)")
        return deleted
    
    def cleanup_old_comments(self, days: int = 7) -> int:
        """Remove comments older than N days (except those in AI examples table)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        
        # Get comment IDs that are in AI examples (preserve these)
        cursor.execute('SELECT DISTINCT comment_text FROM ai_examples')
        example_texts = {row['comment_text'] for row in cursor.fetchall()}
        
        # Delete old comments NOT in examples
        cursor.execute('''
            DELETE FROM hidden_comments
            WHERE created_at < ?
              AND comment_text NOT IN (SELECT comment_text FROM ai_examples)
        ''', (cutoff,))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            print(f"🧹 Cleaned up {deleted} comments older than {days} days (preserved AI examples)")
        
        return deleted
    
    # Queue methods for retry
    def queue_comment(self, comment_data: Dict) -> bool:
        """Add comment to queue for retry"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            import json
            cursor.execute('''
                INSERT INTO comment_queue 
                (comment_id, post_id, comment_data)
                VALUES (?, ?, ?)
            ''', (
                comment_data['comment_id'],
                comment_data['post_id'],
                json.dumps(comment_data)
            ))
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            conn.close()
            return False
    
    def get_queued_comments(self) -> List[Dict]:
        """Get all queued comments for retry. Auto-promotes exhausted retries to failed_comments."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()

        # Promote exhausted retries to dead-letter queue
        cursor.execute('SELECT * FROM comment_queue WHERE retry_count >= 5')
        exhausted = cursor.fetchall()
        promoted_ids = []
        for row in exhausted:
            try:
                data = json.loads(row['comment_data'])
                cursor.execute('''
                    INSERT OR IGNORE INTO failed_comments
                    (comment_id, post_id, comment_text, author_name, retry_count, error_reason, comment_data)
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                ''', (
                    row['comment_id'],
                    row['post_id'],
                    data.get('comment_text', ''),
                    data.get('author_name', ''),
                    row['retry_count'],
                    'Exceeded 5 retries',
                    row['comment_data']
                ))
                promoted_ids.append(row['comment_id'])
            except Exception as e:
                print(f"⚠️  Failed to promote comment {row['comment_id']} to dead-letter: {e}")
        # Only delete from retry queue entries that were successfully promoted
        for cid in promoted_ids:
            cursor.execute('DELETE FROM comment_queue WHERE comment_id = ?', (cid,))
        conn.commit()

        cursor.execute('''
            SELECT * FROM comment_queue
            WHERE retry_count < 5
            ORDER BY queued_at ASC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        comments = []
        for row in rows:
            data = json.loads(row['comment_data'])
            data['queue_id'] = row['id']
            comments.append(data)
        
        return comments
    
    def remove_from_queue(self, comment_id: str) -> bool:
        """Remove comment from queue after successful processing"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM comment_queue WHERE comment_id = ?', (comment_id,))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def increment_queue_retry(self, comment_id: str) -> bool:
        """Increment retry count for queued comment"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE comment_queue 
            SET retry_count = retry_count + 1, last_retry = ?
            WHERE comment_id = ?
        ''', (datetime.now().isoformat(), comment_id))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    # Dead-letter queue methods
    def get_failed_comments(self) -> List[Dict]:
        """Get all comments in the dead-letter queue."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM failed_comments ORDER BY failed_at DESC')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def requeue_failed_comment(self, comment_id: str) -> bool:
        """Move a failed comment back to comment_queue for retry."""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM failed_comments WHERE comment_id = ?', (comment_id,))
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        try:
            cursor.execute('''
                INSERT OR REPLACE INTO comment_queue (comment_id, post_id, comment_data, retry_count)
                VALUES (?, ?, ?, 0)
            ''', (row['comment_id'], row['post_id'], row['comment_data']))
            cursor.execute('DELETE FROM failed_comments WHERE comment_id = ?', (comment_id,))
            conn.commit()
            return True
        except Exception:
            return False
        finally:
            conn.close()

    def dismiss_failed_comment(self, comment_id: str) -> bool:
        """Remove a comment from the dead-letter queue without retrying."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('DELETE FROM failed_comments WHERE comment_id = ?', (comment_id,))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    # Post tracking methods
    def track_post(self, post_id: str, post_number: int, published_at: str) -> bool:
        """Add post tracking (only if not exists - preserves last_fetch_time)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Use INSERT OR IGNORE to only add new posts, not overwrite existing
        cursor.execute('''
            INSERT OR IGNORE INTO post_tracking 
            (post_id, post_number, published_at, updated_at)
            VALUES (?, ?, ?, ?)
        ''', (post_id, post_number, published_at, datetime.now().isoformat()))
        
        conn.commit()
        conn.close()
        return True
    
    def update_post_comment_activity(self, post_id: str, comment_time: str) -> bool:
        """Update when post last received a comment (for sliding window)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE post_tracking 
            SET last_comment_at = ?, updated_at = ?
            WHERE post_id = ?
        ''', (comment_time, datetime.now().isoformat(), post_id))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def get_posts_to_monitor(self) -> List[Dict]:
        """
        Get posts that need comment monitoring:
        1. Posts from last 2 days
        2. Older posts with comments in last 24h (sliding window)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        now = datetime.now()
        two_days_ago = (now - timedelta(days=2)).isoformat()
        one_day_ago = (now - timedelta(days=1)).isoformat()
        
        # Get posts from last 2 days OR older posts with recent comments
        cursor.execute('''
            SELECT DISTINCT * FROM post_tracking 
            WHERE published_at >= ? 
               OR last_comment_at >= ?
            ORDER BY published_at DESC
        ''', (two_days_ago, one_day_ago))
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def update_post_checked_time(self, post_id: str) -> bool:
        """Update when post was last checked for comments"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE post_tracking 
            SET last_checked_at = ?, updated_at = ?
            WHERE post_id = ?
        ''', (datetime.now().isoformat(), datetime.now().isoformat(), post_id))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success
    
    def get_last_fetch_time(self) -> Optional[str]:
        """Get the last time we fetched comments"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Debug: Check what's in the table
        cursor.execute('SELECT COUNT(*) as count FROM post_tracking')
        count = cursor.fetchone()['count']
        print(f"   [DEBUG] post_tracking has {count} posts")
        
        cursor.execute('SELECT post_id, last_fetch_time FROM post_tracking ORDER BY last_fetch_time DESC LIMIT 3')
        sample = cursor.fetchall()
        for row in sample:
            fetch_time = row['last_fetch_time'] if row['last_fetch_time'] else 'NULL'
            print(f"   [DEBUG] Post {row['post_id'][:20]}...: last_fetch={fetch_time}")
        
        cursor.execute('''
            SELECT MAX(last_fetch_time) as last_fetch
            FROM post_tracking
            WHERE last_fetch_time IS NOT NULL
        ''')
        
        row = cursor.fetchone()
        conn.close()
        
        last_fetch = row['last_fetch'] if row and row['last_fetch'] else None
        print(f"   [DEBUG] Returning last_fetch_time: {last_fetch}")
        return last_fetch
    
    def update_last_fetch_time(self, post_id: str) -> bool:
        """Update when we last fetched comments for a post"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        now = datetime.now().isoformat()
        cursor.execute('''
            UPDATE post_tracking 
            SET last_fetch_time = ?, updated_at = ?
            WHERE post_id = ?
        ''', (now, now, post_id))
        
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        
        if not success:
            print(f"   ⚠️  Failed to update last_fetch_time for {post_id} - post not in tracking table")
        
        return success
    
    def dismiss_comment(self, comment_id: str) -> bool:
        """Mark comment as dismissed (keep for 2 hours to prevent re-fetching)"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE hidden_comments 
            SET dismissed_at = ?, last_action_at = ?
            WHERE comment_id = ?
        ''', (datetime.now().isoformat(), datetime.now().isoformat(), comment_id))
        
        success = cursor.rowcount > 0
        
        conn.commit()
        conn.close()
        
        if success:
            print(f"✅ Marked comment as dismissed: {comment_id}")
        
        return success
    
    def undismiss_comment(self, comment_id: str) -> bool:
        """Clear dismissed_at so the comment reappears in the admin UI."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE hidden_comments
            SET dismissed_at = NULL, last_action_at = ?
            WHERE comment_id = ?
        ''', (datetime.now().isoformat(), comment_id))
        success = cursor.rowcount > 0
        conn.commit()
        conn.close()
        return success

    def cleanup_old_post_tracking(self, days: int = 30) -> int:
        """Remove post_tracking entries older than N days with no recent comment activity."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        cursor.execute('''
            DELETE FROM post_tracking
            WHERE published_at < ?
              AND (last_comment_at IS NULL OR last_comment_at < ?)
        ''', (cutoff, cutoff))
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        if deleted > 0:
            print(f"🗑️  Cleaned up {deleted} old post_tracking entries (>{days} days inactive)")
        return deleted

    def cleanup_old_dismissed_comments(self, days: int = 7) -> int:
        """Remove dismissed comments older than N days (should match comment retention window)."""
        conn = self.get_connection()
        cursor = conn.cursor()

        cutoff = (datetime.now() - timedelta(days=days)).isoformat()

        cursor.execute('''
            DELETE FROM hidden_comments
            WHERE dismissed_at IS NOT NULL
              AND dismissed_at < ?
        ''', (cutoff,))
        deleted = cursor.rowcount

        # Also clean up failed_comments older than N days
        cursor.execute('''
            DELETE FROM failed_comments
            WHERE failed_at < ?
        ''', (cutoff,))
        failed_deleted = cursor.rowcount

        conn.commit()
        conn.close()

        if deleted > 0:
            print(f"🗑️  Cleaned up {deleted} dismissed comments older than {days} days")
        if failed_deleted > 0:
            print(f"🗑️  Cleaned up {failed_deleted} failed comments older than {days} days")

        return deleted
    
    def get_comments_grouped_by_post(self, filter_status: Optional[str] = None, days: int = 7) -> Dict:
        """Get comments grouped by post for better display. Flagged comments and posts show first."""
        comments = self.get_all_comments(filter_status, days)
        
        grouped = {}
        for comment in comments:
            post_id = comment['post_id']
            if post_id not in grouped:
                grouped[post_id] = {
                    'post_id': post_id,
                    'post_number': comment.get('post_number'),
                    'post_text': comment.get('post_text', 'Post text not available'),
                    'comments': []
                }
            grouped[post_id]['comments'].append(comment)
        
        # Sort comments within each post: hidden/flagged first
        for post_data in grouped.values():
            post_data['comments'].sort(
                key=lambda c: (0 if c.get('status') == 'hidden' else 1, c.get('created_at', ''))
            )
            # Track if post has any flagged comments
            post_data['has_flagged'] = any(c.get('status') == 'hidden' for c in post_data['comments'])
        
        # Sort posts: posts with flagged comments first
        sorted_grouped = dict(sorted(
            grouped.items(),
            key=lambda item: (0 if item[1]['has_flagged'] else 1, -(item[1].get('post_number') or 0))
        ))
        
        return sorted_grouped

    def log_ai_feedback(self, comment_id: str, feedback_type: str, correct_reason: str = None) -> bool:
        """
        Log feedback for AI training
        
        feedback_type:
        - 'correct_hide' - AI correctly flagged, admin deleted
        - 'missed' - AI missed violation, admin manually hid/deleted  
        - 'false_positive' - AI wrongly flagged, admin unhid
        
        correct_reason: 'political', 'hate', 'rude' (for missed violations)
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get comment details
        cursor.execute('''
            SELECT comment_text, filter_reason, ai_explanation 
            FROM hidden_comments 
            WHERE comment_id = ?
        ''', (comment_id,))
        
        row = cursor.fetchone()
        if not row:
            conn.close()
            return False
        
        cursor.execute('''
            INSERT INTO ai_feedback 
            (comment_id, comment_text, ai_prediction, ai_reason, actual_action, 
             correct_reason, feedback_type)
            VALUES (?, ?, ?, ?, ?, ?, ?)
        ''', (
            comment_id,
            row['comment_text'],
            row['filter_reason'],
            row['ai_explanation'],
            feedback_type,
            correct_reason,
            feedback_type
        ))
        
        conn.commit()
        conn.close()
        print(f"📝 Logged AI feedback: {feedback_type} for comment {comment_id}")
        return True
    
    def get_unsent_feedback(self) -> List[Dict]:
        """Get all feedback that hasn't been sent to AI yet"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM ai_feedback 
            WHERE sent_to_ai = 0
            ORDER BY created_at ASC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]
    
    def mark_feedback_sent(self, feedback_ids: List[int]) -> bool:
        """Mark feedback as sent to AI"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        placeholders = ','.join('?' * len(feedback_ids))
        cursor.execute(f'''
            UPDATE ai_feedback 
            SET sent_to_ai = 1, sent_at = ?
            WHERE id IN ({placeholders})
        ''', [datetime.now().isoformat()] + feedback_ids)
        
        conn.commit()
        conn.close()
        return True

    def get_best_feedback_examples(self, limit: int = 10) -> Dict:
        """
        Get curated examples from permanent ai_examples table
        Returns all 6 categories with up to 5 examples each
        """
        return self.get_ai_examples_for_learning()
    
    def add_ai_example(self, category: str, comment_text: str, original_ai_prediction: str = None, explanation: str = None) -> bool:
        """
        Add example to permanent AI training set
        Maintains max 10 examples per category using diversity-based replacement
        
        Categories:
        - false_positive_political, false_positive_hate
        - correct_political, correct_hate
        - missed_political, missed_hate
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        try:
            # Check if example already exists
            cursor.execute('''
                SELECT id FROM ai_examples 
                WHERE category = ? AND comment_text = ?
            ''', (category, comment_text))
            
            if cursor.fetchone():
                print(f"   Example already exists in {category}")
                conn.close()
                return False
            
            # Count current examples in this category
            cursor.execute('''
                SELECT COUNT(*) as count FROM ai_examples 
                WHERE category = ?
            ''', (category,))
            
            count = cursor.fetchone()['count']
            
            # If at limit (10), find most similar and replace it
            if count >= 10:
                # Get all current examples
                cursor.execute('''
                    SELECT id, comment_text FROM ai_examples 
                    WHERE category = ?
                ''', (category,))
                
                existing = cursor.fetchall()
                
                # Find most similar example (simple word overlap)
                new_words = set(comment_text.lower().split())
                max_similarity = 0
                most_similar_id = existing[0]['id']
                
                for ex in existing:
                    ex_words = set(ex['comment_text'].lower().split())
                    overlap = len(new_words & ex_words)
                    total = len(new_words | ex_words)
                    similarity = overlap / total if total > 0 else 0
                    
                    if similarity > max_similarity:
                        max_similarity = similarity
                        most_similar_id = ex['id']
                
                # Replace most similar
                cursor.execute('''
                    DELETE FROM ai_examples WHERE id = ?
                ''', (most_similar_id,))
                
                print(f"   Replaced similar example in {category} (similarity: {max_similarity:.2f})")
            
            # Insert new example
            cursor.execute('''
                INSERT INTO ai_examples 
                (category, comment_text, original_ai_prediction, explanation)
                VALUES (?, ?, ?, ?)
            ''', (category, comment_text, original_ai_prediction, explanation))
            
            conn.commit()
            conn.close()
            
            print(f"✅ Added example to {category}")
            return True
            
        except Exception as e:
            print(f"❌ Error adding example: {e}")
            conn.close()
            return False
    
    def get_ai_examples_for_learning(self) -> Dict:
        """Get all curated examples for AI prompt"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT * FROM ai_examples ORDER BY category, created_at DESC')
        rows = cursor.fetchall()
        conn.close()
        
        # Group by category (9 categories now - added spam)
        examples = {
            'false_positive_political': [],
            'false_positive_hate': [],
            'false_positive_spam': [],
            'correct_political': [],
            'correct_hate': [],
            'correct_spam': [],
            'missed_political': [],
            'missed_hate': [],
            'missed_spam': []
        }
        
        for row in rows:
            category = row['category']
            if category in examples:
                examples[category].append(dict(row))
        
        return examples
    
    def get_all_ai_examples_for_admin(self) -> List[Dict]:
        """Get all examples for admin page"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT * FROM ai_examples 
            ORDER BY category, created_at DESC
        ''')
        
        rows = cursor.fetchall()
        conn.close()
        
        return [dict(row) for row in rows]
    
    def delete_ai_example(self, example_id: int) -> bool:
        """Delete an example from the training set"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('DELETE FROM ai_examples WHERE id = ?', (example_id,))
        success = cursor.rowcount > 0
        
        conn.commit()
        conn.close()
        
        return success
    
    def save_training_data(self, comment_id: str, post_id: str, comment_text: str, 
                          admin_label: str, ai_prediction: str = None, ai_explanation: str = None) -> bool:
        """Save a marked comment to the training data table (keeps all, no limit)"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO comment_training_data 
                (comment_id, post_id, comment_text, admin_label, ai_prediction, ai_explanation)
                VALUES (?, ?, ?, ?, ?, ?)
            ''', (comment_id, post_id, comment_text, admin_label, ai_prediction, ai_explanation))
            
            conn.commit()
            conn.close()
            print(f"📊 Saved training data: {admin_label} for {comment_id[:30]}...")
            return True
        except Exception as e:
            print(f"❌ Error saving training data: {e}")
            return False
    
    def get_training_data_stats(self) -> Dict:
        """Get stats on saved training data"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT admin_label, COUNT(*) as count 
            FROM comment_training_data 
            GROUP BY admin_label
        ''')
        
        stats = {row['admin_label']: row['count'] for row in cursor.fetchall()}
        stats['total'] = sum(stats.values())
        conn.close()
        return stats
    
    def get_unreviewed_comment_count(self) -> int:
        """Get count of all comments not yet dismissed (flagged + clean)"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        cursor.execute('''
            SELECT COUNT(*) as count FROM hidden_comments
            WHERE created_at > ? AND dismissed_at IS NULL
        ''', (cutoff,))

        count = cursor.fetchone()['count']
        conn.close()
        return count

    def get_unreviewed_flagged_count(self) -> int:
        """Get count of flagged (hidden) comments not yet dismissed"""
        conn = self.get_connection()
        cursor = conn.cursor()

        cutoff = (datetime.now() - timedelta(days=7)).isoformat()
        cursor.execute('''
            SELECT COUNT(*) as count FROM hidden_comments
            WHERE created_at > ? AND dismissed_at IS NULL AND status = 'hidden'
        ''', (cutoff,))

        count = cursor.fetchone()['count']
        conn.close()
        return count

    # ==================== RECONCILER METHODS ====================

    def get_entries_needing_fb_delete(self) -> List[Dict]:
        """Posts that were denied/returned-to-pending but still have a FB post_id.
        Reconciler deletes them from FB then calls confirm_fb_deleted()."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, facebook_post_id, post_number, status
            FROM entries
            WHERE status IN ('denied', 'pending')
              AND facebook_post_id IS NOT NULL
              AND (fb_published_at IS NULL OR fb_published_at = '')
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_entries_needing_schedule(self) -> List[Dict]:
        """Scheduled posts that have no FB post_id yet — reconciler creates them on FB."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, text, post_number, scheduled_time
            FROM entries
            WHERE status = 'scheduled'
              AND facebook_post_id IS NULL
              AND scheduled_time IS NOT NULL
            ORDER BY post_number ASC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def get_entries_needing_sync(self) -> List[Dict]:
        """Scheduled posts already on FB — reconciler checks desired vs actual state.
        Returns all columns so reconciler can compare fb_* against desired state."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT id, text, post_number, scheduled_time, facebook_post_id,
                   fb_scheduled_time, fb_post_number, fb_text_hash
            FROM entries
            WHERE status = 'scheduled'
              AND facebook_post_id IS NOT NULL
              AND (fb_published_at IS NULL OR fb_published_at = '')
            ORDER BY post_number ASC
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    def mark_published_if_past(self, cutoff_iso: str) -> int:
        """Mark scheduled+linked posts whose scheduled_time < cutoff as published.
        Also logs newly published posts to instagram_post_log.
        Returns count of rows updated."""
        conn = self.get_connection()
        cursor = conn.cursor()
        now_iso = datetime.now().isoformat()

        # Capture rows BEFORE the update so we can log them
        cursor.execute('''
            SELECT facebook_post_id, post_number, text, scheduled_time
            FROM entries
            WHERE status = 'scheduled'
              AND facebook_post_id IS NOT NULL
              AND scheduled_time IS NOT NULL
              AND scheduled_time < ?
        ''', (cutoff_iso,))
        to_publish = cursor.fetchall()

        cursor.execute('''
            UPDATE entries
            SET status = 'published',
                fb_published_at = ?
            WHERE status = 'scheduled'
              AND facebook_post_id IS NOT NULL
              AND scheduled_time IS NOT NULL
              AND scheduled_time < ?
        ''', (now_iso, cutoff_iso))
        count = cursor.rowcount

        # Log to instagram_post_log (INSERT OR IGNORE so restarts don't double-log)
        for row in to_publish:
            cursor.execute('''
                INSERT OR IGNORE INTO instagram_post_log
                    (fb_post_id, post_number, text, published_at)
                VALUES (?, ?, ?, ?)
            ''', (row['facebook_post_id'], row['post_number'], row['text'],
                  row['scheduled_time']))

        conn.commit()
        conn.close()
        return count

    def confirm_scheduled(self, entry_id: int, fb_id: str, slot_iso: str,
                          post_number: int, text_hash: str):
        """Reconciler writes actual-state columns after successfully creating a FB post."""
        conn = self.get_connection()
        cursor = conn.cursor()
        # Clear any stale duplicate fb_id from other entries before writing.
        cursor.execute(
            'UPDATE entries SET facebook_post_id = NULL WHERE facebook_post_id = ? AND id != ?',
            (fb_id, entry_id)
        )
        cursor.execute('''
            UPDATE entries
            SET facebook_post_id = ?,
                fb_scheduled_time = ?,
                fb_post_number = ?,
                fb_text_hash = ?
            WHERE id = ?
        ''', (fb_id, slot_iso, post_number, text_hash, entry_id))
        conn.commit()
        conn.close()

    def confirm_synced(self, entry_id: int, new_fb_id: str, new_time_iso: str,
                       post_number: int, text_hash: str):
        """Reconciler writes actual-state after rescheduling / text-update on FB.
        facebook_post_id changes because FB delete+recreate gives a new id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        # Clear any stale duplicate fb_id from other entries before writing.
        cursor.execute(
            'UPDATE entries SET facebook_post_id = NULL WHERE facebook_post_id = ? AND id != ?',
            (new_fb_id, entry_id)
        )
        cursor.execute('''
            UPDATE entries
            SET facebook_post_id = ?,
                fb_scheduled_time = ?,
                fb_post_number = ?,
                fb_text_hash = ?
            WHERE id = ?
        ''', (new_fb_id, new_time_iso, post_number, text_hash, entry_id))
        conn.commit()
        conn.close()

    def confirm_fb_deleted(self, entry_id: int):
        """Reconciler clears facebook_post_id and all fb_* after deleting post from FB."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE entries
            SET facebook_post_id = NULL,
                fb_scheduled_time = NULL,
                fb_post_number    = NULL,
                fb_text_hash      = NULL
            WHERE id = ?
        ''', (entry_id,))
        conn.commit()
        conn.close()

    def get_taken_fb_slots(self) -> List[str]:
        """Return all fb_scheduled_time values for in-flight scheduled posts.
        Reconciler uses this to avoid double-booking a slot when assigning new posts."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT fb_scheduled_time FROM entries
            WHERE status = 'scheduled'
              AND fb_scheduled_time IS NOT NULL
              AND (fb_published_at IS NULL OR fb_published_at = '')
        ''')
        rows = cursor.fetchall()
        conn.close()
        return [row['fb_scheduled_time'] for row in rows]

    def get_entry_by_facebook_post_id(self, fb_post_id: str) -> Optional[Dict]:
        """Look up an entry by its Facebook post ID (for webhook-driven deletion detection)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT * FROM entries WHERE facebook_post_id = ?', (fb_post_id,))
        row = cursor.fetchone()
        conn.close()
        return dict(row) if row else None

    def return_post_to_pending_from_reconciler(self, fb_post_id: str) -> Optional[int]:
        """FB webhook told us a scheduled post was deleted externally.
        Returns entry to pending, slides following posts up, decrements post counter.
        Returns freed post_number or None if post was not found / already published."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            # If any pending entry still holds this fb_id, the deletion was reconciler-initiated
            # (e.g. unschedule_all set the entry to pending, then step1 deleted from FB).
            # Don't cascade in that case — the entry is already handled.
            cursor.execute(
                "SELECT COUNT(*) as cnt FROM entries WHERE facebook_post_id = ? AND status = 'pending'",
                (fb_post_id,)
            )
            if cursor.fetchone()['cnt'] > 0:
                return None

            cursor.execute(
                'SELECT id, post_number, scheduled_time, status, fb_published_at '
                'FROM entries WHERE facebook_post_id = ?',
                (fb_post_id,)
            )
            row = cursor.fetchone()
            if not row:
                return None
            if row['status'] != 'scheduled' or row['fb_published_at']:
                return None

            entry_id     = row['id']
            freed_number = row['post_number']
            freed_time   = row['scheduled_time']

            # Clear both desired-state and actual-state — post is gone from FB
            cursor.execute('''
                UPDATE entries
                SET status='pending', post_number=NULL, scheduled_time=NULL,
                    facebook_post_id=NULL, fb_scheduled_time=NULL,
                    fb_post_number=NULL, fb_text_hash=NULL, fb_published_at=NULL
                WHERE id=?
            ''', (entry_id,))

            if freed_number:
                cursor.execute('''
                    SELECT id, post_number, scheduled_time FROM entries
                    WHERE post_number > ?
                      AND status='scheduled'
                      AND (fb_published_at IS NULL OR fb_published_at='')
                    ORDER BY post_number ASC
                ''', (freed_number,))
                following = cursor.fetchall()
                prev_time = freed_time
                for f in following:
                    cursor.execute(
                        'UPDATE entries SET post_number=post_number-1, scheduled_time=? WHERE id=?',
                        (prev_time, f['id'])
                    )
                    prev_time = f['scheduled_time']
                cursor.execute('UPDATE post_numbers SET current_number=current_number-1 WHERE id=1')

            conn.commit()
            return freed_number
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    def bulk_update_post_orders(self, updates: List[Dict]) -> bool:
        """Bulk-write post_number + scheduled_time for reorder_posts route.
        Each dict: {'id': int, 'post_number': int, 'scheduled_time': str}."""
        conn = self.get_connection()
        cursor = conn.cursor()
        try:
            for u in updates:
                cursor.execute(
                    'UPDATE entries SET post_number=?, scheduled_time=? WHERE id=?',
                    (u['post_number'], u['scheduled_time'], u['id'])
                )
            conn.commit()
            return True
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

    # ==================== ACTION QUEUE JOURNAL METHODS ====================

    def journal_enqueue(self, queue_name: str, action: str, payload_json: str) -> int:
        """Persist a new job in the action queue journal. Returns the journal row id."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            INSERT INTO action_queue_journal (queue_name, action, payload, status)
            VALUES (?, ?, ?, 'pending')
        ''', (queue_name, action, payload_json))
        row_id = cursor.lastrowid
        conn.commit()
        conn.close()
        return row_id

    def journal_mark_done(self, journal_id: int):
        """Mark a journal job as successfully completed."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            "UPDATE action_queue_journal SET status='done', updated_at=? WHERE id=?",
            (datetime.now().isoformat(), journal_id)
        )
        conn.commit()
        conn.close()

    def journal_mark_failed(self, journal_id: int, error: str, retry_count: int):
        """Mark a journal job as permanently failed."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            UPDATE action_queue_journal
            SET status='failed', error=?, retry_count=?, updated_at=?
            WHERE id=?
        ''', (error, retry_count, datetime.now().isoformat(), journal_id))
        conn.commit()
        conn.close()

    def journal_increment_retry(self, journal_id: int) -> int:
        """Increment retry_count and return the new value."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE action_queue_journal SET retry_count=retry_count+1, updated_at=? WHERE id=?',
            (datetime.now().isoformat(), journal_id)
        )
        cursor.execute('SELECT retry_count FROM action_queue_journal WHERE id=?', (journal_id,))
        row = cursor.fetchone()
        conn.commit()
        conn.close()
        return row['retry_count'] if row else 0

    def journal_get_pending(self, queue_name: str) -> List[Dict]:
        """Return all pending/processing jobs for a queue (used on startup to reload)."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT * FROM action_queue_journal
            WHERE queue_name=? AND status IN ('pending', 'processing')
            ORDER BY id ASC
        ''', (queue_name,))
        rows = cursor.fetchall()
        conn.close()
        return [dict(row) for row in rows]

    # -------------------------------------------------------------------------
    # Instagram helpers
    # -------------------------------------------------------------------------

    def get_best_post_for_instagram(self, start_iso: str, end_iso: str) -> List[Dict]:
        """Return all unposted instagram_post_log entries in [start_iso, end_iso).
        Includes comment_count from hidden_comments.
        Caller scores each entry (reactions + comment_count*2) and picks the max."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute('''
            SELECT l.id, l.fb_post_id, l.post_number, l.text, l.published_at,
                   COUNT(hc.id) AS comment_count
            FROM instagram_post_log l
            LEFT JOIN hidden_comments hc ON hc.post_id = l.fb_post_id
            WHERE l.published_at >= ?
              AND l.published_at <  ?
              AND l.ig_posted = 0
            GROUP BY l.id
            ORDER BY l.published_at ASC
        ''', (start_iso, end_iso))
        rows = cursor.fetchall()
        conn.close()
        return [dict(r) for r in rows]

    def mark_ig_posted(self, log_id: int):
        """Mark an instagram_post_log entry as posted."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cursor.execute(
            'UPDATE instagram_post_log SET ig_posted=1, ig_posted_at=? WHERE id=?',
            (datetime.now().isoformat(), log_id)
        )
        conn.commit()
        conn.close()

    def backfill_instagram_post_log(self, days: int = 7) -> int:
        """Backfill instagram_post_log from existing published entries.
        Copies entries published in the last N days that aren't already logged.
        Returns number of rows inserted."""
        conn = self.get_connection()
        cursor = conn.cursor()
        cutoff = (datetime.now() - timedelta(days=days)).isoformat()
        print(f"[ig-backfill] backfilling from entries published after {cutoff}")
        cursor.execute('''
            INSERT OR IGNORE INTO instagram_post_log (fb_post_id, post_number, text, published_at)
            SELECT facebook_post_id, post_number, text,
                   COALESCE(fb_published_at, approved_at, created_at)
            FROM entries
            WHERE status = 'published'
              AND facebook_post_id IS NOT NULL
              AND post_number IS NOT NULL
              AND COALESCE(fb_published_at, approved_at, created_at) >= ?
        ''', (cutoff,))
        count = cursor.rowcount
        conn.commit()
        conn.close()
        print(f"[ig-backfill] inserted {count} row(s) into instagram_post_log")
        return count

