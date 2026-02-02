import sqlite3
from datetime import datetime
import json
from typing import List, Dict, Optional

class Database:
    def __init__(self, db_path: str = "content_system_new.db"):
        self.db_path = db_path
        self.init_database()
    
    def get_connection(self):
        """Get a database connection with proper settings"""
        conn = sqlite3.connect(
            self.db_path, 
            timeout=30,  # Increased timeout
            isolation_level=None,  # Autocommit mode
            check_same_thread=False
        )
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA busy_timeout=30000')  # 30 second busy timeout
        return conn
    
    def init_database(self):
        """Initialize the database with required tables"""
        conn = sqlite3.connect(self.db_path)
        cursor = conn.cursor()
        
        # Create entries table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS entries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                sheet_timestamp TEXT NOT NULL,
                text TEXT NOT NULL,
                edited_text TEXT,
                status TEXT DEFAULT 'pending',
                approved_by TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                updated_at TEXT DEFAULT CURRENT_TIMESTAMP,
                UNIQUE(sheet_timestamp, text)
            )
        ''')
        
        # Create scheduled_posts table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS scheduled_posts (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                entry_id INTEGER,
                text TEXT NOT NULL,
                scheduled_time TEXT NOT NULL,
                published BOOLEAN DEFAULT 0,
                facebook_post_id TEXT,
                created_at TEXT DEFAULT CURRENT_TIMESTAMP,
                FOREIGN KEY (entry_id) REFERENCES entries(id)
            )
        ''')
        
        # Create post_counter table
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS post_counter (
                id INTEGER PRIMARY KEY CHECK (id = 1),
                current_number INTEGER DEFAULT 1
            )
        ''')
        
        # Initialize counter if not exists
        cursor.execute('INSERT OR IGNORE INTO post_counter (id, current_number) VALUES (1, 1)')
        
        # Create processed_timestamps table to track what we've already read
        cursor.execute('''
            CREATE TABLE IF NOT EXISTS processed_timestamps (
                timestamp TEXT PRIMARY KEY,
                processed_at TEXT DEFAULT CURRENT_TIMESTAMP
            )
        ''')
        
        conn.commit()
        conn.close()
    
    def add_entry(self, timestamp: str, text: str) -> bool:
        """Add a new entry from Google Sheets"""
        try:
            conn = self.get_connection()
            cursor = conn.cursor()
            
            cursor.execute('''
                INSERT INTO entries (sheet_timestamp, text, status)
                VALUES (?, ?, 'pending')
            ''', (timestamp, text))
            
            cursor.execute('''
                INSERT INTO processed_timestamps (timestamp)
                VALUES (?)
            ''', (timestamp,))
            
            conn.commit()
            conn.close()
            return True
        except sqlite3.IntegrityError:
            # Entry already exists
            return False
    
    def get_pending_entries(self) -> List[Dict]:
        """Get all entries pending review"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, sheet_timestamp as timestamp, text, created_at
            FROM entries
            WHERE status = 'pending'
            ORDER BY created_at ASC
        ''')
        
        entries = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return entries
    
    def approve_entry(self, entry_id: int, edited_text: str, approved_by: str):
        """Approve an entry"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'approved',
                edited_text = ?,
                approved_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (edited_text, approved_by, entry_id))
        
        conn.commit()
        conn.close()
    
    def deny_entry(self, entry_id: int, denied_by: str):
        """Deny an entry"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'denied',
                approved_by = ?,
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (denied_by, entry_id))
        
        conn.commit()
        conn.close()
    
    def get_next_post_number(self) -> int:
        """Get the next post number and increment counter"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT current_number FROM post_counter WHERE id = 1')
        current = cursor.fetchone()[0]
        
        cursor.execute('UPDATE post_counter SET current_number = current_number + 1 WHERE id = 1')
        
        conn.commit()
        conn.close()
        return current
    
    def get_current_post_number(self) -> int:
        """Get the current post number without incrementing"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT current_number FROM post_counter WHERE id = 1')
        current = cursor.fetchone()[0]
        
        conn.close()
        return current
    
    def reset_post_number(self, number: int):
        """Reset the post counter to a specific number"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('UPDATE post_counter SET current_number = ? WHERE id = 1', (number,))
        
        conn.commit()
        conn.close()
    
    def schedule_post(self, entry_id: int, text: str, scheduled_time: str):
        """Schedule a post for publishing"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            INSERT INTO scheduled_posts (entry_id, text, scheduled_time)
            VALUES (?, ?, ?)
        ''', (entry_id, text, scheduled_time))
        
        # Update entry status
        cursor.execute('''
            UPDATE entries
            SET status = 'scheduled',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = ?
        ''', (entry_id,))
        
        conn.commit()
        conn.close()
    
    def get_scheduled_posts(self) -> List[Dict]:
        """Get all scheduled posts that haven't been published"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, entry_id, text, scheduled_time, created_at
            FROM scheduled_posts
            WHERE published = 0
            ORDER BY scheduled_time ASC
        ''')
        
        posts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return posts
    
    def get_posts_due_for_publishing(self, current_time: str) -> List[Dict]:
        """Get posts that should be published now"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, entry_id, text, scheduled_time
            FROM scheduled_posts
            WHERE published = 0 AND scheduled_time <= ?
            ORDER BY scheduled_time ASC
        ''', (current_time,))
        
        posts = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return posts
    
    def mark_as_published(self, post_id: int, facebook_post_id: str):
        """Mark a scheduled post as published"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE scheduled_posts
            SET published = 1,
                facebook_post_id = ?
            WHERE id = ?
        ''', (facebook_post_id, post_id))
        
        # Update entry status
        cursor.execute('''
            UPDATE entries
            SET status = 'published',
                updated_at = CURRENT_TIMESTAMP
            WHERE id = (SELECT entry_id FROM scheduled_posts WHERE id = ?)
        ''', (post_id,))
        
        conn.commit()
        conn.close()
    
    def reschedule_post(self, post_id: int, new_time: str):
        """Reschedule a post to a new time"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE scheduled_posts
            SET scheduled_time = ?
            WHERE id = ?
        ''', (new_time, post_id))
        
        conn.commit()
        conn.close()
    
    def cancel_scheduled_post(self, post_id: int):
        """Cancel a scheduled post"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        # Get entry_id before deleting
        cursor.execute('SELECT entry_id FROM scheduled_posts WHERE id = ?', (post_id,))
        result = cursor.fetchone()
        
        if result:
            entry_id = result[0]
            
            # Delete the scheduled post
            cursor.execute('DELETE FROM scheduled_posts WHERE id = ?', (post_id,))
            
            # Update entry status back to approved
            cursor.execute('''
                UPDATE entries
                SET status = 'approved',
                    updated_at = CURRENT_TIMESTAMP
                WHERE id = ?
            ''', (entry_id,))
        
        conn.commit()
        conn.close()
    
    def get_statistics(self) -> Dict:
        """Get overall statistics"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT COUNT(*) FROM entries WHERE status = "pending"')
        pending = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM entries WHERE status = "approved"')
        approved = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM scheduled_posts WHERE published = 0')
        scheduled = cursor.fetchone()[0]
        
        cursor.execute('SELECT COUNT(*) FROM entries WHERE status = "published"')
        published = cursor.fetchone()[0]
        
        conn.close()
        
        return {
            'pending': pending,
            'approved': approved,
            'scheduled': scheduled,
            'published': published
        }
    
    def get_recent_activity(self, limit: int = 20) -> List[Dict]:
        """Get recent activity"""
        conn = self.get_connection()
        conn.row_factory = sqlite3.Row
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT sheet_timestamp as timestamp, status, 
                   COALESCE(edited_text, text) as text, updated_at
            FROM entries
            ORDER BY updated_at DESC
            LIMIT ?
        ''', (limit,))
        
        activities = [dict(row) for row in cursor.fetchall()]
        conn.close()
        return activities
    
    def is_timestamp_processed(self, timestamp: str) -> bool:
        """Check if a timestamp has been processed"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('SELECT 1 FROM processed_timestamps WHERE timestamp = ?', (timestamp,))
        result = cursor.fetchone()
        
        conn.close()
        return result is not None
