"""
Database handler for comment filtering system
Stores hidden comments and their analysis
"""

import sqlite3
from datetime import datetime
import pytz

class CommentsDatabase:
    def __init__(self, db_path='data/confessions.db'):
        self.db_path = db_path
        self.init_database()
    
    def init_database(self):
        """Create comments table if it doesn't exist"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            CREATE TABLE IF NOT EXISTS hidden_comments (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                comment_id TEXT UNIQUE NOT NULL,
                post_id TEXT NOT NULL,
                post_message TEXT,
                commenter_name TEXT,
                commenter_id TEXT,
                comment_text TEXT NOT NULL,
                hidden_at TEXT NOT NULL,
                ai_reason TEXT,
                ai_category TEXT,
                ai_confidence REAL,
                status TEXT DEFAULT 'hidden',
                unhidden_at TEXT,
                deleted_at TEXT,
                created_time TEXT
            )
        ''')
        
        # Index for faster queries
        c.execute('CREATE INDEX IF NOT EXISTS idx_post_id ON hidden_comments(post_id)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_status ON hidden_comments(status)')
        c.execute('CREATE INDEX IF NOT EXISTS idx_comment_id ON hidden_comments(comment_id)')
        
        conn.commit()
        conn.close()
    
    def add_hidden_comment(self, comment_id, post_id, post_message, commenter_name, 
                          commenter_id, comment_text, ai_result, created_time):
        """Add a newly hidden comment to the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            c.execute('''
                INSERT OR REPLACE INTO hidden_comments 
                (comment_id, post_id, post_message, commenter_name, commenter_id,
                 comment_text, hidden_at, ai_reason, ai_category, ai_confidence, 
                 status, created_time)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'hidden', ?)
            ''', (
                comment_id, post_id, post_message, commenter_name, commenter_id,
                comment_text, now, ai_result.get('reason'), ai_result.get('category'),
                ai_result.get('confidence'), created_time
            ))
            
            conn.commit()
            return True
        except Exception as e:
            print(f"Error adding hidden comment: {e}")
            return False
        finally:
            conn.close()
    
    def get_all_hidden_comments(self):
        """Get all currently hidden comments, grouped by post"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT comment_id, post_id, post_message, commenter_name, commenter_id,
                   comment_text, hidden_at, ai_reason, ai_category, ai_confidence,
                   created_time
            FROM hidden_comments
            WHERE status = 'hidden'
            ORDER BY post_id, hidden_at DESC
        ''')
        
        rows = c.fetchall()
        conn.close()
        
        # Group by post_id
        posts = {}
        for row in rows:
            (comment_id, post_id, post_message, commenter_name, commenter_id,
             comment_text, hidden_at, ai_reason, ai_category, ai_confidence,
             created_time) = row
            
            if post_id not in posts:
                posts[post_id] = {
                    'post_id': post_id,
                    'post_message': post_message,
                    'comments': []
                }
            
            posts[post_id]['comments'].append({
                'comment_id': comment_id,
                'commenter_name': commenter_name,
                'commenter_id': commenter_id,
                'comment_text': comment_text,
                'hidden_at': hidden_at,
                'ai_reason': ai_reason,
                'ai_category': ai_category,
                'ai_confidence': ai_confidence,
                'created_time': created_time
            })
        
        return list(posts.values())
    
    def unhide_comment(self, comment_id):
        """Mark a comment as unhidden"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            c.execute('''
                UPDATE hidden_comments
                SET status = 'unhidden', unhidden_at = ?
                WHERE comment_id = ?
            ''', (now, comment_id))
            
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            print(f"Error unhiding comment: {e}")
            return False
        finally:
            conn.close()
    
    def delete_comment(self, comment_id):
        """Mark a comment as deleted"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
        
        try:
            c.execute('''
                UPDATE hidden_comments
                SET status = 'deleted', deleted_at = ?
                WHERE comment_id = ?
            ''', (now, comment_id))
            
            conn.commit()
            return c.rowcount > 0
        except Exception as e:
            print(f"Error deleting comment: {e}")
            return False
        finally:
            conn.close()
    
    def is_comment_already_hidden(self, comment_id):
        """Check if a comment is already in the database"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        c.execute('''
            SELECT status FROM hidden_comments WHERE comment_id = ?
        ''', (comment_id,))
        
        result = c.fetchone()
        conn.close()
        
        return result is not None and result[0] == 'hidden'
    
    def get_statistics(self):
        """Get comment filtering statistics"""
        conn = sqlite3.connect(self.db_path)
        c = conn.cursor()
        
        stats = {}
        
        # Total hidden
        c.execute("SELECT COUNT(*) FROM hidden_comments WHERE status = 'hidden'")
        stats['hidden'] = c.fetchone()[0]
        
        # Total unhidden
        c.execute("SELECT COUNT(*) FROM hidden_comments WHERE status = 'unhidden'")
        stats['unhidden'] = c.fetchone()[0]
        
        # Total deleted
        c.execute("SELECT COUNT(*) FROM hidden_comments WHERE status = 'deleted'")
        stats['deleted'] = c.fetchone()[0]
        
        # By category
        c.execute('''
            SELECT ai_category, COUNT(*) 
            FROM hidden_comments 
            WHERE status = 'hidden'
            GROUP BY ai_category
        ''')
        stats['by_category'] = dict(c.fetchall())
        
        conn.close()
        return stats
