import sqlite3
from datetime import datetime, timedelta
from pathlib import Path
from typing import List, Dict, Optional
import time

class Database:
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
        
        # AI examples table - permanent curated examples (max 5 per category)
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
    
    def approve_entry(self, entry_id: int, edited_text: str, approved_by: str):
        """Mark entry as approved and assign post number"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get next post number
        post_number = self.get_next_post_number()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'approved',
                text = ?,
                post_number = ?,
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
        ''', (edited_text, post_number, approved_by, datetime.now().isoformat(), entry_id))
        
        conn.commit()
        conn.close()
    
    def schedule_to_facebook(self, entry_id: int, facebook_post_id: str, scheduled_time: str):
        """
        Mark entry as scheduled with Facebook post ID
        
        Args:
            entry_id: Local entry ID
            facebook_post_id: Facebook's post ID
            scheduled_time: ISO format scheduled time
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'scheduled',
                facebook_post_id = ?,
                scheduled_time = ?
            WHERE id = ?
        ''', (facebook_post_id, scheduled_time, entry_id))
        
        conn.commit()
        conn.close()
    
    def unschedule_entry(self, entry_id: int):
        """
        Return a scheduled entry back to pending and renumber all following posts
        
        Args:
            entry_id: Entry ID to unschedule
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get the post number of the entry being unscheduled
        cursor.execute('SELECT post_number FROM entries WHERE id = ?', (entry_id,))
        result = cursor.fetchone()
        
        if result:
            unscheduled_number = result['post_number']
            
            # Set entry back to pending
            cursor.execute('''
                UPDATE entries
                SET status = 'pending',
                    facebook_post_id = NULL,
                    scheduled_time = NULL,
                    post_number = NULL
                WHERE id = ?
            ''', (entry_id,))
            
            # Decrement post numbers for all posts after this one
            if unscheduled_number:
                cursor.execute('''
                    UPDATE entries
                    SET post_number = post_number - 1
                    WHERE post_number > ? AND post_number IS NOT NULL
                ''', (unscheduled_number,))
                
                # Decrement the counter
                cursor.execute('UPDATE post_numbers SET current_number = current_number - 1 WHERE id = 1')
        
        conn.commit()
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
            SELECT id, text, post_number, facebook_post_id, scheduled_time
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
    
    def deny_entry(self, entry_id: int, denied_by: str):
        """Mark entry as denied with timestamp"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'denied',
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
        ''', (denied_by, datetime.now().isoformat(), entry_id))
        
        conn.commit()
        conn.close()
    
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
        """Get and increment the post number"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT current_number FROM post_numbers WHERE id = 1')
        current = cursor.fetchone()['current_number']
        
        cursor.execute('UPDATE post_numbers SET current_number = current_number + 1 WHERE id = 1')
        
        conn.commit()
        conn.close()
        
        return current
    
    def get_current_post_number(self) -> int:
        """Get current post number without incrementing"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT current_number FROM post_numbers WHERE id = 1')
        current = cursor.fetchone()['current_number']
        
        conn.close()
        return current
    
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
            AND updated_at < datetime('now', '-1 day')
        ''')
        approved_deleted = cursor.rowcount
        total_deleted += approved_deleted
        
        # Delete denied entries older than 24 hours
        cursor.execute('''
            DELETE FROM entries
            WHERE status = 'denied'
            AND updated_at < datetime('now', '-1 day')
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
                 author_name, author_id, created_at, filter_reason, ai_explanation, status)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
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
                initial_status
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
                ORDER BY created_at DESC
            ''', (cutoff, filter_status))
        else:
            print(f"   [DB] Getting all comments (no status filter, excluding dismissed)")
            cursor.execute('''
                SELECT * FROM hidden_comments 
                WHERE created_at >= ?
                  AND dismissed_at IS NULL
                ORDER BY created_at DESC
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
        """Get all queued comments for retry"""
        import json
        conn = self.get_connection()
        cursor = conn.cursor()
        
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
    
    def cleanup_old_dismissed_comments(self) -> int:
        """Remove dismissed comments older than 2 hours"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        two_hours_ago = (datetime.now() - timedelta(hours=2)).isoformat()
        
        cursor.execute('''
            DELETE FROM hidden_comments 
            WHERE dismissed_at IS NOT NULL 
            AND dismissed_at < ?
        ''', (two_hours_ago,))
        
        deleted = cursor.rowcount
        conn.commit()
        conn.close()
        
        if deleted > 0:
            print(f"🗑️  Cleaned up {deleted} dismissed comments older than 2 hours")
        
        return deleted
    
    def get_comments_grouped_by_post(self, filter_status: Optional[str] = None, days: int = 7) -> Dict:
        """Get comments grouped by post for better display"""
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
        
        return grouped

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
        Maintains max 5 examples per category using diversity-based replacement
        
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
            
            # If at limit (5), find most similar and replace it
            if count >= 5:
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

