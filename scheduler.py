from datetime import datetime, timedelta, time
import pytz
from typing import List, Dict, Optional
import json
from pathlib import Path

class Scheduler:
    def __init__(self, database, facebook_handler):
        """
        Initialize the scheduler
        
        Args:
            database: Database instance
            facebook_handler: FacebookHandler instance
        """
        self.db = database
        self.fb = facebook_handler
        self.timezone = pytz.timezone('Asia/Jerusalem')
    
    def load_posting_windows(self) -> List[time]:
        """Load posting windows from config"""
        config_file = Path("config.json")
        
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
                windows_str = config.get('posting_windows', ['09:00', '14:00', '19:00'])
                
                windows = []
                for w in windows_str:
                    hour, minute = map(int, w.split(':'))
                    windows.append(time(hour, minute))
                
                return sorted(windows)
        
        return [time(9, 0), time(14, 0), time(19, 0)]
    
    def get_next_available_slot(self) -> datetime:
        """
        Get the next available posting slot based on configured windows
        
        Returns:
            datetime object of the next available slot (with timezone)
        """
        windows = self.load_posting_windows()
        now = datetime.now(self.timezone)
        
        # Get already scheduled times
        scheduled_posts = self.db.get_scheduled_posts()
        scheduled_times = [datetime.fromisoformat(p['scheduled_time']) for p in scheduled_posts]
        
        # Try to find a slot today
        current_date = now.date()
        for window_time in windows:
            slot = self.timezone.localize(datetime.combine(current_date, window_time))
            
            # Check if this slot is in the future and not already taken
            if slot > now and slot not in scheduled_times:
                return slot
        
        # If no slots available today, start checking tomorrow
        days_checked = 0
        max_days = 365  # Don't look more than a year ahead
        
        while days_checked < max_days:
            days_checked += 1
            check_date = current_date + timedelta(days=days_checked)
            
            for window_time in windows:
                slot = self.timezone.localize(datetime.combine(check_date, window_time))
                
                if slot not in scheduled_times:
                    return slot
        
        # Fallback: if somehow all slots are taken for a year, just use tomorrow at first window
        return self.timezone.localize(
            datetime.combine(current_date + timedelta(days=1), windows[0])
        )
    
    def schedule_post(self, entry_id: int, text: str) -> str:
        """
        Schedule a post for the next available slot
        
        Args:
            entry_id: Database ID of the entry
            text: The formatted text to post
            
        Returns:
            ISO format string of the scheduled time
        """
        next_slot = self.get_next_available_slot()
        self.db.schedule_post(entry_id, text, next_slot.isoformat())
        
        return next_slot.strftime('%Y-%m-%d %H:%M %Z')
    
    def publish_due_posts(self) -> List[Dict]:
        """
        Check for and publish any posts that are due
        
        Returns:
            List of published posts with their results
        """
        if not self.fb:
            return []
        
        now = datetime.now(self.timezone)
        due_posts = self.db.get_posts_due_for_publishing(now.isoformat())
        
        results = []
        
        for post in due_posts:
            try:
                # Publish to Facebook
                fb_result = self.fb.publish_post(post['text'])
                
                # Mark as published in database
                self.db.mark_as_published(post['id'], fb_result['id'])
                
                results.append({
                    'success': True,
                    'post_id': post['id'],
                    'facebook_id': fb_result['id'],
                    'scheduled_time': post['scheduled_time']
                })
                
            except Exception as e:
                results.append({
                    'success': False,
                    'post_id': post['id'],
                    'error': str(e),
                    'scheduled_time': post['scheduled_time']
                })
        
        return results
    
    def get_scheduled_summary(self) -> Dict:
        """Get a summary of scheduled posts by date"""
        scheduled = self.db.get_scheduled_posts()
        
        summary = {}
        for post in scheduled:
            scheduled_dt = datetime.fromisoformat(post['scheduled_time'])
            date_key = scheduled_dt.date().isoformat()
            
            if date_key not in summary:
                summary[date_key] = []
            
            summary[date_key].append({
                'id': post['id'],
                'time': scheduled_dt.strftime('%H:%M'),
                'text_preview': post['text'][:50] + '...' if len(post['text']) > 50 else post['text']
            })
        
        return summary
