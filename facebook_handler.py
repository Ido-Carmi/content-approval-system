import requests
from typing import Dict, Optional

class FacebookHandler:
    def __init__(self, page_id: str, access_token: str):
        """
        Initialize Facebook handler
        
        Args:
            page_id: Facebook Page ID
            access_token: Facebook Page Access Token
        """
        self.page_id = page_id
        self.access_token = access_token
        self.api_version = 'v18.0'
        self.base_url = f'https://graph.facebook.com/{self.api_version}'
    
    def publish_post(self, message: str) -> Dict:
        """
        Publish a text post to the Facebook Page
        
        Args:
            message: The text content to post
            
        Returns:
            Dict with 'id' key containing the Facebook post ID
        """
        try:
            url = f'{self.base_url}/{self.page_id}/feed'
            
            payload = {
                'message': message,
                'access_token': self.access_token
            }
            
            response = requests.post(url, data=payload)
            response.raise_for_status()
            
            result = response.json()
            
            if 'id' not in result:
                raise Exception("Failed to get post ID from Facebook response")
            
            return result
            
        except requests.exceptions.RequestException as e:
            raise Exception(f"Failed to publish to Facebook: {str(e)}")
    
    def test_connection(self) -> bool:
        """
        Test if the Facebook credentials are valid
        
        Returns:
            True if connection is successful, False otherwise
        """
        try:
            url = f'{self.base_url}/{self.page_id}'
            
            params = {
                'fields': 'name,id',
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params)
            response.raise_for_status()
            
            result = response.json()
            
            return 'id' in result and 'name' in result
            
        except:
            return False
    
    def get_page_info(self) -> Optional[Dict]:
        """
        Get information about the Facebook Page
        
        Returns:
            Dict with page information or None if failed
        """
        try:
            url = f'{self.base_url}/{self.page_id}'
            
            params = {
                'fields': 'name,id,username,category',
                'access_token': self.access_token
            }
            
            response = requests.get(url, params=params)
            response.raise_for_status()
            
            return response.json()
            
        except:
            return None
    
    def delete_post(self, post_id: str) -> bool:
        """
        Delete a post from the Facebook Page
        
        Args:
            post_id: The Facebook post ID
            
        Returns:
            True if successful, False otherwise
        """
        try:
            url = f'{self.base_url}/{post_id}'
            
            params = {
                'access_token': self.access_token
            }
            
            response = requests.delete(url, params=params)
            response.raise_for_status()
            
            return True
            
        except:
            return False
