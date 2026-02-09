"""
Facebook Comments Handler
Fetch, hide, unhide, and delete comments via Facebook Graph API
"""

import requests
from typing import List, Dict, Optional
from datetime import datetime, timedelta
import time

class FacebookCommentsHandler:
    def __init__(self, access_token: str, page_id: str):
        """Initialize Facebook Comments Handler"""
        self.access_token = access_token
        self.page_id = page_id
        self.base_url = "https://graph.facebook.com/v18.0"
    
    def fetch_post_comments(self, post_id: str, since: Optional[datetime] = None, limit: int = 100) -> List[Dict]:
        """
        Fetch comments from a specific post
        
        Args:
            post_id: Facebook post ID
            since: Only fetch comments after this time (for hourly scans)
            limit: Max comments to fetch
        
        Returns:
            List of comment dictionaries
        """
        url = f"{self.base_url}/{post_id}/comments"
        
        params = {
            'access_token': self.access_token,
            'fields': 'id,message,from,created_time,is_hidden',
            'limit': limit
        }
        
        # Add time filter if provided
        if since:
            params['since'] = int(since.timestamp())
        
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            comments = data.get('data', [])
            
            # Format comments
            formatted_comments = []
            for comment in comments:
                # Skip already hidden comments on Facebook
                if comment.get('is_hidden', False):
                    continue
                
                formatted_comments.append({
                    'comment_id': comment['id'],
                    'post_id': post_id,
                    'comment_text': comment.get('message', ''),
                    'author_name': comment.get('from', {}).get('name', 'Unknown'),
                    'author_id': comment.get('from', {}).get('id', ''),
                    'created_at': comment.get('created_time', ''),
                    'is_hidden': comment.get('is_hidden', False)
                })
            
            print(f"✅ Fetched {len(formatted_comments)} new comments from post {post_id}")
            return formatted_comments
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching comments from post {post_id}: {e}")
            return []
    
    def fetch_all_recent_comments(self, post_ids: List[str], since_hours: int = 1) -> List[Dict]:
        """
        Fetch recent comments from multiple posts
        
        Args:
            post_ids: List of Facebook post IDs to check
            since_hours: Only get comments from last N hours
        
        Returns:
            List of all comments from all posts
        """
        since_time = datetime.now() - timedelta(hours=since_hours)
        all_comments = []
        
        for post_id in post_ids:
            comments = self.fetch_post_comments(post_id, since=since_time)
            all_comments.extend(comments)
            
            # Rate limiting - don't hammer API
            time.sleep(0.5)
        
        print(f"📊 Total comments fetched: {len(all_comments)} from {len(post_ids)} posts")
        return all_comments
    
    def hide_comment(self, comment_id: str) -> bool:
        """
        Hide a comment on Facebook
        
        Args:
            comment_id: Facebook comment ID
        
        Returns:
            True if successfully hidden
        """
        url = f"{self.base_url}/{comment_id}"
        
        params = {
            'access_token': self.access_token,
            'is_hidden': 'true'
        }
        
        try:
            response = requests.post(url, params=params, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            if result.get('success', False):
                print(f"✅ Hidden comment: {comment_id}")
                return True
            else:
                print(f"⚠️  Failed to hide comment: {comment_id}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error hiding comment {comment_id}: {e}")
            return False
    
    def unhide_comment(self, comment_id: str) -> bool:
        """
        Unhide a previously hidden comment
        
        Args:
            comment_id: Facebook comment ID
        
        Returns:
            True if successfully unhidden
        """
        url = f"{self.base_url}/{comment_id}"
        
        params = {
            'access_token': self.access_token,
            'is_hidden': 'false'
        }
        
        try:
            response = requests.post(url, params=params, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            if result.get('success', False):
                print(f"✅ Unhidden comment: {comment_id}")
                return True
            else:
                print(f"⚠️  Failed to unhide comment: {comment_id}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error unhiding comment {comment_id}: {e}")
            return False
    
    def delete_comment(self, comment_id: str) -> bool:
        """
        Permanently delete a comment from Facebook
        
        Args:
            comment_id: Facebook comment ID
        
        Returns:
            True if successfully deleted
        """
        url = f"{self.base_url}/{comment_id}"
        
        params = {
            'access_token': self.access_token
        }
        
        try:
            response = requests.delete(url, params=params, timeout=30)
            response.raise_for_status()
            
            result = response.json()
            if result.get('success', False):
                print(f"✅ Deleted comment: {comment_id}")
                return True
            else:
                print(f"⚠️  Failed to delete comment: {comment_id}")
                return False
                
        except requests.exceptions.RequestException as e:
            print(f"❌ Error deleting comment {comment_id}: {e}")
            return False
    
    def get_page_posts(self, limit: int = 100, since_days: int = 2) -> List[Dict]:
        """
        Get recent posts from the Facebook page
        
        Args:
            limit: Max posts to fetch
            since_days: Get posts from last N days
        
        Returns:
            List of post dictionaries with id and created_time
        """
        url = f"{self.base_url}/{self.page_id}/posts"
        
        since_time = datetime.now() - timedelta(days=since_days)
        
        params = {
            'access_token': self.access_token,
            'fields': 'id,message,created_time',
            'limit': limit,
            'since': int(since_time.timestamp())
        }
        
        try:
            response = requests.get(url, params=params, timeout=30)
            response.raise_for_status()
            
            data = response.json()
            posts = data.get('data', [])
            
            formatted_posts = []
            for post in posts:
                formatted_posts.append({
                    'post_id': post['id'],
                    'message': post.get('message', '')[:200],  # First 200 chars
                    'created_time': post.get('created_time', '')
                })
            
            print(f"✅ Fetched {len(formatted_posts)} posts from page")
            return formatted_posts
            
        except requests.exceptions.RequestException as e:
            print(f"❌ Error fetching page posts: {e}")
            return []
    
    def batch_hide_comments(self, comment_ids: List[str]) -> Dict[str, bool]:
        """
        Hide multiple comments (with rate limiting)
        
        Args:
            comment_ids: List of comment IDs to hide
        
        Returns:
            Dictionary mapping comment_id to success status
        """
        results = {}
        
        for comment_id in comment_ids:
            success = self.hide_comment(comment_id)
            results[comment_id] = success
            
            # Rate limiting
            time.sleep(0.3)
        
        successful = sum(1 for v in results.values() if v)
        print(f"📊 Hid {successful}/{len(comment_ids)} comments")
        
        return results
