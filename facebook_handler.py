import requests
from typing import Dict, Optional
from datetime import datetime
import pytz

class FacebookHandler:
    def __init__(self, page_id: str, access_token: str):
        """
        Initialize Facebook handler with Page credentials
        
        Args:
            page_id: Facebook Page ID
            access_token: Facebook Page Access Token
        """
        self.page_id = page_id
        self.access_token = access_token
        self.api_base = f"https://graph.facebook.com/v18.0"
    
    def publish_post(self, text: str) -> Dict:
        """
        Publish a post immediately
        
        Args:
            text: Post content
            
        Returns:
            Dictionary with post_id
        """
        url = f"{self.api_base}/{self.page_id}/feed"
        
        payload = {
            'message': text,
            'access_token': self.access_token
        }
        
        response = requests.post(url, data=payload)
        
        if response.status_code == 200:
            return response.json()
        else:
            raise Exception(f"Facebook API error: {response.text}")
    
    def schedule_post(self, text: str, scheduled_time: datetime) -> Dict:
        """
        Schedule a post to Facebook's scheduler
        
        Args:
            text: Post content
            scheduled_time: When to publish (datetime with timezone)
            
        Returns:
            Dictionary with post_id
        """
        url = f"{self.api_base}/{self.page_id}/feed"
        
        # Convert datetime to Unix timestamp
        if scheduled_time.tzinfo is None:
            # Assume Israel timezone if no timezone
            israel_tz = pytz.timezone('Asia/Jerusalem')
            scheduled_time = israel_tz.localize(scheduled_time)
        
        unix_timestamp = int(scheduled_time.timestamp())
        
        payload = {
            'message': text,
            'published': 'false',  # Don't publish immediately
            'scheduled_publish_time': unix_timestamp,
            'access_token': self.access_token
        }
        
        response = requests.post(url, data=payload)
        
        if response.status_code == 200:
            result = response.json()
            return {
                'id': result['id'],
                'scheduled_time': scheduled_time.isoformat()
            }
        else:
            raise Exception(f"Facebook API error: {response.text}")
    
    def get_scheduled_posts(self) -> list:
        """
        Get all scheduled posts from Facebook
        
        Returns:
            List of scheduled posts with id, message, scheduled_time
        """
        url = f"{self.api_base}/{self.page_id}/scheduled_posts"
        
        params = {
            'access_token': self.access_token,
            'fields': 'id,message,scheduled_publish_time,created_time'
        }
        
        response = requests.get(url, params=params)
        
        if response.status_code == 200:
            data = response.json()
            posts = []
            
            for post in data.get('data', []):
                # Convert Unix timestamp to datetime
                timestamp = int(post.get('scheduled_publish_time', 0))
                israel_tz = pytz.timezone('Asia/Jerusalem')
                scheduled_dt = datetime.fromtimestamp(timestamp, tz=israel_tz)
                
                posts.append({
                    'id': post['id'],
                    'message': post.get('message', ''),
                    'scheduled_time': scheduled_dt.isoformat(),
                    'scheduled_time_display': scheduled_dt.strftime('%d/%m/%Y %H:%M')
                })
            
            return posts
        else:
            raise Exception(f"Facebook API error: {response.text}")
    
    def delete_scheduled_post(self, post_id: str) -> bool:
        """
        Delete a scheduled post from Facebook
        
        Args:
            post_id: Facebook post ID
            
        Returns:
            True if successful
        """
        url = f"{self.api_base}/{post_id}"
        
        params = {
            'access_token': self.access_token
        }
        
        response = requests.delete(url, params=params)
        
        if response.status_code == 200:
            return True
        else:
            raise Exception(f"Facebook API error: {response.text}")
    
    def update_scheduled_post(self, post_id: str, new_text: Optional[str] = None, 
                            new_time: Optional[datetime] = None) -> Dict:
        """
        Update a scheduled post's content or time
        
        Args:
            post_id: Facebook post ID
            new_text: New post content (optional)
            new_time: New scheduled time (optional)
            
        Returns:
            Updated post info
        """
        # Facebook doesn't allow direct update of scheduled posts
        # We need to delete and recreate
        
        # Get current post data
        url = f"{self.api_base}/{post_id}"
        params = {
            'access_token': self.access_token,
            'fields': 'message,scheduled_publish_time'
        }
        response = requests.get(url, params=params)
        
        if response.status_code != 200:
            raise Exception(f"Facebook API error: {response.text}")
        
        current_post = response.json()
        
        # Use new values or keep current
        message = new_text if new_text else current_post.get('message', '')
        
        if new_time:
            scheduled_time = new_time
        else:
            timestamp = int(current_post.get('scheduled_publish_time', 0))
            israel_tz = pytz.timezone('Asia/Jerusalem')
            scheduled_time = datetime.fromtimestamp(timestamp, tz=israel_tz)
        
        # Delete old post
        self.delete_scheduled_post(post_id)
        
        # Create new scheduled post
        return self.schedule_post(message, scheduled_time)
    
    def test_connection(self) -> bool:
        """
        Test if the Page Access Token is valid
        
        Returns:
            True if connection successful
        """
        url = f"{self.api_base}/{self.page_id}"
        
        params = {
            'access_token': self.access_token,
            'fields': 'id,name'
        }
        
        response = requests.get(url, params=params)
        
        return response.status_code == 200
