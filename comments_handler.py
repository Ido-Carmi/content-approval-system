"""
Facebook Comments Filter Handler
Scans posts for comments, filters them via AI, and manages hidden comments
"""

import requests
import json
import time
from datetime import datetime
import pytz

class CommentsHandler:
    def __init__(self, page_id, access_token):
        self.page_id = page_id
        self.access_token = access_token
        self.base_url = "https://graph.facebook.com/v18.0"
        
    def get_page_posts(self, limit=50):
        """Get recent posts from the page"""
        url = f"{self.base_url}/{self.page_id}/posts"
        params = {
            'access_token': self.access_token,
            'fields': 'id,message,created_time',
            'limit': limit
        }
        
        try:
            response = requests.get(url, params=params)
            response.raise_for_status()
            data = response.json()
            return data.get('data', [])
        except Exception as e:
            print(f"Error getting posts: {e}")
            return []
    
    def get_post_comments(self, post_id):
        """Get all comments for a specific post"""
        url = f"{self.base_url}/{post_id}/comments"
        params = {
            'access_token': self.access_token,
            'fields': 'id,from,message,created_time,is_hidden',
            'limit': 100,
            'filter': 'stream'  # Get all comments including hidden
        }
        
        comments = []
        
        try:
            while url:
                response = requests.get(url, params=params)
                response.raise_for_status()
                data = response.json()
                
                comments.extend(data.get('data', []))
                
                # Check for next page
                url = data.get('paging', {}).get('next')
                params = {}  # Next page URL already has params
                
            return comments
        except Exception as e:
            print(f"Error getting comments for post {post_id}: {e}")
            return []
    
    def hide_comment(self, comment_id):
        """Hide a comment on Facebook"""
        url = f"{self.base_url}/{comment_id}"
        params = {
            'access_token': self.access_token,
            'is_hidden': 'true'
        }
        
        try:
            response = requests.post(url, params=params)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"Error hiding comment {comment_id}: {e}")
            return False
    
    def unhide_comment(self, comment_id):
        """Unhide a comment on Facebook"""
        url = f"{self.base_url}/{comment_id}"
        params = {
            'access_token': self.access_token,
            'is_hidden': 'false'
        }
        
        try:
            response = requests.post(url, params=params)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"Error unhiding comment {comment_id}: {e}")
            return False
    
    def delete_comment(self, comment_id):
        """Permanently delete a comment from Facebook"""
        url = f"{self.base_url}/{comment_id}"
        params = {
            'access_token': self.access_token
        }
        
        try:
            response = requests.delete(url, params=params)
            response.raise_for_status()
            return True
        except Exception as e:
            print(f"Error deleting comment {comment_id}: {e}")
            return False
    
    def scan_all_comments(self):
        """
        Scan all posts and collect their comments
        Returns: List of dicts with post info and comments
        """
        posts = self.get_page_posts()
        results = []
        
        for post in posts:
            post_id = post['id']
            post_message = post.get('message', '')[:100]  # First 100 chars
            
            comments = self.get_post_comments(post_id)
            
            # Only include posts that have comments
            if comments:
                results.append({
                    'post_id': post_id,
                    'post_message': post_message,
                    'post_created': post.get('created_time'),
                    'comments': comments,
                    'comment_count': len(comments)
                })
        
        return results
