"""
Comments Scanner
Nightly/startup scan: fetches recent comments, classifies with AI, saves to DB for admin review.
All moderation actions (hide/unhide/delete) are handled exclusively by the webhook + job queue.
"""

from datetime import datetime, timedelta, timezone
from typing import List, Dict
import time
from config import get_auto_comment_texts

class CommentsScanner:
    def __init__(self, db, facebook_handler, ai_filter, retention_days: int = 7, lookback_hours: int = 48):
        self.db = db
        self.facebook = facebook_handler
        self.ai_filter = ai_filter
        self.retention_days = retention_days
        self.lookback_hours = lookback_hours
    
    def scan_and_filter_comments(self) -> Dict:
        """
        Fetch recent comments, classify with AI, save to DB for admin review.
        Does NOT hide anything — moderation is handled by the webhook + job queue.
        """
        print("\n" + "=" * 60)
        print(f"🔍 Starting comment scan at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("=" * 60)

        stats = {
            'posts_checked': 0,
            'comments_found': 0,
            'comments_filtered': 0,
            'errors': 0,
        }

        try:
            posts = self._get_posts_to_monitor()
            stats['posts_checked'] = len(posts)

            if not posts:
                print("ℹ️  No posts to monitor")
                return stats

            print(f"📊 Monitoring {len(posts)} posts")

            comments = self._fetch_recent_comments(posts)
            stats['comments_found'] = len(comments)

            if not comments:
                print("ℹ️  No new comments found")
                return stats

            print(f"📝 Found {len(comments)} new comments")

            # Skip already-known comments and the page's own auto-comments
            new_comments = []
            existing_count = 0
            auto_count = 0

            auto_texts = get_auto_comment_texts()
            page_id = str(getattr(self.facebook, 'page_id', '') or '')

            conn = self.db.get_connection()
            cursor = conn.cursor()
            for comment in comments:
                if page_id and comment.get('author_id', '') == page_id:
                    auto_count += 1
                    continue
                if comment.get('comment_text', '').strip() in auto_texts:
                    auto_count += 1
                    continue
                cursor.execute('SELECT id FROM hidden_comments WHERE comment_id = ?',
                               (comment['comment_id'],))
                if cursor.fetchone():
                    existing_count += 1
                else:
                    new_comments.append(comment)
            conn.close()

            if auto_count:
                print(f"   [Skip] {auto_count} auto-comment(s) ignored")
            if existing_count:
                print(f"   [Skip] {existing_count} already in database")
            if not new_comments:
                print("ℹ️  No new comments to classify")
                return stats

            print(f"   [New] {len(new_comments)} new comments — classifying with AI...")
            filter_results = self._filter_comments_with_ai(new_comments)
            stats['comments_filtered'] = len(filter_results)

            self._save_filter_results(filter_results, new_comments)

            print("\n" + "=" * 60)
            print(f"✅ Scan complete: {stats['posts_checked']} posts, "
                  f"{stats['comments_found']} comments, "
                  f"{stats['comments_filtered']} classified")
            print("=" * 60 + "\n")

        except Exception as e:
            print(f"❌ Error in comment scan: {e}")
            stats['errors'] += 1
            import traceback
            traceback.print_exc()

        return stats
    
    def _get_posts_to_monitor(self) -> List[Dict]:
        """
        Get posts that need monitoring:
        1. Posts from last 2 days
        2. Older posts with comments in last 24h (sliding window)
        """
        # First, sync recent posts from Facebook to tracking table
        self._sync_recent_posts()
        
        # Get posts from database tracking
        posts = self.db.get_posts_to_monitor()
        
        return posts
    
    def _sync_recent_posts(self):
        """Sync recent posts from Facebook to post_tracking table"""
        try:
            # Get posts from last 3 days (buffer for sliding window)
            fb_posts = self.facebook.get_page_posts(limit=100, since_days=3)
            
            for post in fb_posts:
                # Try to extract post number from message
                post_number = self._extract_post_number(post.get('message', ''))
                
                self.db.track_post(
                    post_id=post['post_id'],
                    post_number=post_number,
                    published_at=post['created_time']
                )
            
            print(f"✅ Synced {len(fb_posts)} posts to tracking")
            
        except Exception as e:
            print(f"⚠️  Error syncing posts: {e}")
    
    def _extract_post_number(self, message: str) -> int:
        """Try to extract post number like #15262 from message"""
        import re
        match = re.search(r'#(\d+)', message)
        if match:
            return int(match.group(1))
        return None
    
    def _fetch_recent_comments(self, posts: List[Dict]) -> List[Dict]:
        """
        Fetch comments from last hour for all posts
        
        Args:
            posts: List of posts to check
        
        Returns:
            List of comments with metadata
        """
        post_ids = [post['post_id'] for post in posts]
        
        # Get last fetch time from database (for logging only)
        last_fetch = self.db.get_last_fetch_time()
        
        if last_fetch:
            last_fetch_dt = datetime.fromisoformat(last_fetch)
            now = datetime.now()
            seconds_since = (now - last_fetch_dt).total_seconds()
            
            if seconds_since < 120:
                time_str = f"{int(seconds_since)} seconds ago"
            elif seconds_since < 7200:
                time_str = f"{int(seconds_since / 60)} minutes ago"
            else:
                time_str = f"{int(seconds_since / 3600)} hours ago"
            
            print(f"📅 Last fetch: {last_fetch} ({time_str})")
        else:
            print(f"📅 First fetch")
        
        # Fetch all comments (no server-side time filter — Facebook ignores it)
        # Client-side 1.5h filter is applied below
        comments = self.facebook.fetch_all_recent_comments(
            post_ids=post_ids
        )
        
        # Log newest comment from Facebook API
        if comments:
            newest_time = max([c.get('created_at', '') for c in comments if c.get('created_at')])
            print(f"   📅 Newest comment from Facebook API: {newest_time}")
        else:
            print(f"   ⚠️  Facebook returned 0 comments")
        
        # Client-side time filter. lookback_hours is 48h for normal scans,
        # longer for deep scans. DB deduplicates by comment_id so no risk of
        # re-showing dismissed comments (as long as dismissed records are kept
        # for at least retention_days days).
        cutoff_time = datetime.now(timezone.utc) - timedelta(hours=self.lookback_hours)
        before_time_filter = len(comments)
        
        filtered_comments = []
        for c in comments:
            created = c.get('created_at', '')
            if not created:
                filtered_comments.append(c)  # Keep if no timestamp (safety)
                continue
            try:
                # Facebook returns ISO format: 2026-02-11T14:30:00+0000
                comment_time = datetime.fromisoformat(created.replace('+0000', '+00:00').replace('Z', '+00:00'))
                # Ensure timezone-aware comparison (both UTC)
                if comment_time.tzinfo is None:
                    comment_time = comment_time.replace(tzinfo=timezone.utc)
                if comment_time >= cutoff_time:
                    filtered_comments.append(c)
            except (ValueError, TypeError):
                filtered_comments.append(c)  # Keep if parsing fails (safety)
        
        comments = filtered_comments
        time_filtered_out = before_time_filter - len(comments)
        if time_filtered_out > 0:
            print(f"   [Time Filter] Removed {time_filtered_out} comments older than {self.lookback_hours} hours")
        
        
        # Clean up dismissed comments older than the retention window
        retention_days = self.retention_days
        self.db.cleanup_old_dismissed_comments(days=retention_days)
        
        # Filter out already-dismissed comments
        dismissed_ids = set()
        conn = self.db.get_connection()
        cursor = conn.cursor()
        cursor.execute('SELECT comment_id FROM hidden_comments WHERE dismissed_at IS NOT NULL')
        dismissed_ids = {row['comment_id'] for row in cursor.fetchall()}
        conn.close()
        
        if dismissed_ids:
            before_filter = len(comments)
            comments = [c for c in comments if c['comment_id'] not in dismissed_ids]
            filtered_out = before_filter - len(comments)
            if filtered_out > 0:
                print(f"   [Filter] Removed {filtered_out} already-dismissed comments")
        
        print(f"📝 Found {len(comments)} new comments (after all filters)")
        
        # DON'T update last_fetch_time anymore!
        # We rely on the dismissed system to prevent re-showing comments
        # This allows Facebook's delayed comments to appear when they're finally indexed
        
        # Just log for reference
        print(f"   [Scan completed at {datetime.now().isoformat()}]")
        
        # Enrich comments with post metadata
        post_map = {p['post_id']: p for p in posts}
        
        for comment in comments:
            post_data = post_map.get(comment['post_id'], {})
            comment['post_number'] = post_data.get('post_number')
            comment['post_text'] = self._get_post_text(comment['post_id'])
            
            # Update post activity for sliding window
            self.db.update_post_comment_activity(
                post_id=comment['post_id'],
                comment_time=comment['created_at']
            )
        
        return comments
    
    def _get_post_text(self, post_id: str) -> str:
        """Get post text from database or Facebook"""
        # Try to get from post_tracking table first
        conn = self.db.get_connection()
        cursor = conn.cursor()
        
        # Check post_tracking
        cursor.execute('''
            SELECT post_text FROM post_tracking 
            WHERE post_id = ?
            LIMIT 1
        ''', (post_id,))
        row = cursor.fetchone()
        
        if row and row['post_text']:
            conn.close()
            return row['post_text'][:200]
        
        # Try entries table
        cursor.execute('''
            SELECT text FROM entries 
            WHERE facebook_post_id = ?
            LIMIT 1
        ''', (post_id,))
        row = cursor.fetchone()
        conn.close()
        
        if row:
            return row['text'][:200]
        
        # Last resort: fetch from Facebook
        try:
            import requests
            url = f"https://graph.facebook.com/v18.0/{post_id}"
            params = {
                'access_token': self.facebook.access_token,
                'fields': 'message'
            }
            r = requests.get(url, params=params, timeout=10)
            if r.ok:
                data = r.json()
                message = data.get('message', 'Post text not available')[:200]
                
                # Update post_tracking with the text
                conn = self.db.get_connection()
                cursor = conn.cursor()
                cursor.execute('''
                    UPDATE post_tracking 
                    SET post_text = ?
                    WHERE post_id = ?
                ''', (message, post_id))
                conn.commit()
                conn.close()
                
                return message
        except Exception as e:
            print(f"⚠️  Error fetching post text: {e}")
        
        return "Post text not available"
    
    def _filter_comments_with_ai(self, comments: List[Dict]) -> List[Dict]:
        """Classify comments with AI. Returns results or [] on error."""
        if not comments:
            return []
        try:
            return self.ai_filter.filter_comments_batch(comments=comments, batch_size=30)
        except Exception as e:
            print(f"❌ AI filtering error: {e}")
            return []

    def _save_filter_results(self, filter_results: List[Dict], comments: List[Dict]) -> None:
        """Save AI classification results to DB for admin review. No FB actions."""
        if not filter_results:
            return
        comment_map = {c['comment_id']: c for c in comments}
        for result in filter_results:
            comment_data = comment_map.get(result['comment_id'])
            if not comment_data:
                continue
            comment_data['should_hide'] = result['should_hide']
            comment_data['filter_reason'] = result['reason']
            comment_data['ai_explanation'] = result['explanation']
            comment_data['ai_confidence'] = result.get('confidence', 0.0)
            self.db.add_comment(comment_data)


def create_hourly_job(db, config, lookback_hours: int = 48):
    """Create and return a comment scanning job."""
    from facebook_comments_handler import FacebookCommentsHandler
    from ai_comment_filter import CommentFilter

    facebook = FacebookCommentsHandler(
        access_token=config.get('facebook_access_token'),
        page_id=config.get('facebook_page_id')
    )
    ai_filter = CommentFilter(
        api_key=config.get('openai_api_key'),
        db=db
    )
    retention_days = config.get('comment_retention_days', 7)
    scanner = CommentsScanner(
        db=db,
        facebook_handler=facebook,
        ai_filter=ai_filter,
        retention_days=retention_days,
        lookback_hours=lookback_hours,
    )

    def job():
        try:
            scanner.scan_and_filter_comments()
        except Exception as e:
            print(f"❌ Comment scan error: {e}")
            import traceback
            traceback.print_exc()

    return job
