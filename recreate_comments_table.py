#!/usr/bin/env python3
"""
Recreate tables for comments filter
Updates both hidden_comments and post_tracking tables
"""

import sqlite3

print("="*60)
print("UPDATING TABLES FOR COMMENTS FILTER")
print("="*60)

# Connect to database
conn = sqlite3.connect('content_system.db')
cursor = conn.cursor()

# 1. Recreate hidden_comments table
print("\n1. Recreating hidden_comments table...")
cursor.execute('DROP TABLE IF EXISTS hidden_comments')
cursor.execute('''
    CREATE TABLE hidden_comments (
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
        last_action_at TEXT DEFAULT CURRENT_TIMESTAMP
    )
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_status ON hidden_comments(status)')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_comments_created ON hidden_comments(created_at)')
print("   ✓ hidden_comments recreated")

# Show schema
cursor.execute("PRAGMA table_info(hidden_comments)")
columns = cursor.fetchall()
print("   Columns:")
for col in columns:
    print(f"      - {col[1]} ({col[2]})")

# 2. Recreate post_tracking with new columns
print("\n2. Recreating post_tracking table...")
cursor.execute('DROP TABLE IF EXISTS post_tracking')
cursor.execute('''
    CREATE TABLE post_tracking (
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
print("   ✓ post_tracking recreated")

# Show schema
cursor.execute("PRAGMA table_info(post_tracking)")
columns = cursor.fetchall()
print("   Columns:")
for col in columns:
    print(f"      - {col[1]} ({col[2]})")

# 3. Create ai_feedback table for training
print("\n3. Creating ai_feedback table...")
cursor.execute('DROP TABLE IF EXISTS ai_feedback')
cursor.execute('''
    CREATE TABLE ai_feedback (
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
print("   ✓ ai_feedback created")

# Show schema
cursor.execute("PRAGMA table_info(ai_feedback)")
columns = cursor.fetchall()
print("   Columns:")
for col in columns:
    print(f"      - {col[1]} ({col[2]})")

# 4. Create ai_examples table for permanent curated examples
print("\n4. Creating ai_examples table...")
cursor.execute('DROP TABLE IF EXISTS ai_examples')
cursor.execute('''
    CREATE TABLE ai_examples (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        category TEXT NOT NULL,
        comment_text TEXT NOT NULL,
        original_ai_prediction TEXT,
        explanation TEXT,
        created_at TEXT DEFAULT CURRENT_TIMESTAMP,
        UNIQUE(category, comment_text)
    )
''')
cursor.execute('CREATE INDEX IF NOT EXISTS idx_examples_category ON ai_examples(category)')
print("   ✓ ai_examples created (max 5 per category)")

# Show schema
cursor.execute("PRAGMA table_info(ai_examples)")
columns = cursor.fetchall()
print("   Columns:")
for col in columns:
    print(f"      - {col[1]} ({col[2]})")

conn.commit()
conn.close()

print("\n" + "="*60)
print("✅ Done! Restart the app.")
print("   - AI examples table created (6 categories, 5 each)")
print("   - View at: /ai-examples")
print("   - Comments now use 3-button system")
print("="*60)
