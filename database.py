import sqlite3
from datetime import datetime
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
        """Mark entry as approved"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'approved',
                text = ?,
                approved_by = ?,
                approved_at = ?
            WHERE id = ?
        ''', (edited_text, approved_by, datetime.now().isoformat(), entry_id))
        
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
        Return a scheduled entry back to pending
        
        Args:
            entry_id: Entry ID to unschedule
        """
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'pending',
                facebook_post_id = NULL,
                scheduled_time = NULL
            WHERE id = ?
        ''', (entry_id,))
        
        conn.commit()
        conn.close()
    
    def get_scheduled_entries(self) -> List[Dict]:
        """Get all scheduled entries with Facebook post IDs"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            SELECT id, text, facebook_post_id, scheduled_time
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
        """Mark entry as denied"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        cursor.execute('''
            UPDATE entries
            SET status = 'denied',
                approved_by = ?
            WHERE id = ?
        ''', (denied_by, entry_id))
        
        conn.commit()
        conn.close()
    
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
    
    def get_statistics(self) -> Dict:
        """Get statistics about entries"""
        conn = self.get_connection()
        cursor = conn.cursor()
        
        stats = {}
        
        for status in ['pending', 'approved', 'scheduled', 'published', 'denied']:
            cursor.execute('SELECT COUNT(*) as count FROM entries WHERE status = ?', (status,))
            stats[status] = cursor.fetchone()['count']
        
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
