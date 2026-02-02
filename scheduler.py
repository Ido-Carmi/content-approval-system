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
        
        # Jewish holidays (non-work days only) - dates for 2024-2027
        # These are the first days of multi-day holidays that are non-work days
        self.jewish_holidays = {
            # 2024
            '2024-04-23': 'Passover Day 1',
            '2024-04-29': 'Passover Day 7',
            '2024-06-12': 'Shavuot',
            '2024-10-03': 'Rosh Hashanah Day 1',
            '2024-10-04': 'Rosh Hashanah Day 2',
            '2024-10-12': 'Yom Kippur',
            '2024-10-17': 'Sukkot Day 1',
            '2024-10-24': 'Simchat Torah',
            
            # 2025
            '2025-04-13': 'Passover Day 1',
            '2025-04-19': 'Passover Day 7',
            '2025-06-02': 'Shavuot',
            '2025-09-23': 'Rosh Hashanah Day 1',
            '2025-09-24': 'Rosh Hashanah Day 2',
            '2025-10-02': 'Yom Kippur',
            '2025-10-07': 'Sukkot Day 1',
            '2025-10-14': 'Simchat Torah',
            
            # 2026
            '2026-04-02': 'Passover Day 1',
            '2026-04-08': 'Passover Day 7',
            '2026-05-22': 'Shavuot',
            '2026-09-12': 'Rosh Hashanah Day 1',
            '2026-09-13': 'Rosh Hashanah Day 2',
            '2026-09-21': 'Yom Kippur',
            '2026-09-26': 'Sukkot Day 1',
            '2026-10-03': 'Simchat Torah',
            
            # 2027
            '2027-04-22': 'Passover Day 1',
            '2027-04-28': 'Passover Day 7',
            '2027-06-11': 'Shavuot',
            '2027-10-02': 'Rosh Hashanah Day 1',
            '2027-10-03': 'Rosh Hashanah Day 2',
            '2027-10-11': 'Yom Kippur',
            '2027-10-16': 'Sukkot Day 1',
            '2027-10-23': 'Simchat Torah',
        }
    
    def load_config(self) -> Dict:
        """Load configuration from file"""
        config_file = Path("config.json")
        if config_file.exists():
            with open(config_file, 'r', encoding='utf-8') as f:
                return json.load(f)
        return {}
    
    def load_posting_windows(self) -> List[time]:
        """Load posting windows from config"""
        config = self.load_config()
        windows_str = config.get('posting_windows', ['09:00', '14:00', '19:00'])
        
        windows = []
        for w in windows_str:
            hour, minute = map(int, w.split(':'))
            windows.append(time(hour, minute))
        
        return sorted(windows)
    
    def is_shabbat(self, date: datetime.date) -> bool:
        """Check if a date is Friday or Saturday (Shabbat)"""
        # Friday = 4, Saturday = 5
        return date.weekday() in [4, 5]
    
    def is_jewish_holiday(self, date: datetime.date) -> bool:
        """Check if a date is a Jewish holiday (non-work day)"""
        date_str = date.strftime('%Y-%m-%d')
        return date_str in self.jewish_holidays
    
    def should_skip_date(self, date: datetime.date) -> bool:
        """
        Check if a date should be skipped based on config
        
        Args:
            date: Date to check
            
        Returns:
            True if date should be skipped, False otherwise
        """
        config = self.load_config()
        skip_shabbat = config.get('skip_shabbat', True)
        skip_holidays = config.get('skip_jewish_holidays', True)
        
        if skip_shabbat and self.is_shabbat(date):
            return True
        
        if skip_holidays and self.is_jewish_holiday(date):
            return True
        
        return False
    
    def get_next_available_slot(self) -> datetime:
        """
        Get the next available posting slot based on configured windows,
        skipping Shabbat and Jewish holidays if configured
        
        Returns:
            datetime object of the next available slot (with timezone)
        """
        windows = self.load_posting_windows()
        now = datetime.now(self.timezone)
        
        # Get already scheduled times
        scheduled_posts = self.db.get_scheduled_posts()
        scheduled_times = [datetime.fromisoformat(p['scheduled_time']) for p in scheduled_posts]
        
        # Try to find a slot today (if not skipped)
        current_date = now.date()
        if not self.should_skip_date(current_date):
            for window_time in windows:
                slot = self.timezone.localize(datetime.combine(current_date, window_time))
                
                # Check if this slot is in the future and not already taken
                if slot > now and slot not in scheduled_times:
                    return slot
        
        # If no slots available today, start checking future days
        days_checked = 0
        max_days = 365  # Don't look more than a year ahead
        
        while days_checked < max_days:
            days_checked += 1
            check_date = current_date + timedelta(days=days_checked)
            
            # Skip if this date should be skipped
            if self.should_skip_date(check_date):
                continue
            
            for window_time in windows:
                slot = self.timezone.localize(datetime.combine(check_date, window_time))
                
                if slot not in scheduled_times:
                    return slot
        
        # Fallback: if somehow all slots are taken for a year, just use next valid day at first window
        fallback_days = 1
        while fallback_days < 365:
            fallback_date = current_date + timedelta(days=fallback_days)
            if not self.should_skip_date(fallback_date):
                return self.timezone.localize(
                    datetime.combine(fallback_date, windows[0])
                )
            fallback_days += 1
        
        # Ultimate fallback
        return self.timezone.localize(
            datetime.combine(current_date + timedelta(days=1), windows[0])
        )
    
    def schedule_post(self, entry_id: int, text: str) -> str:
        """
        Schedule a post for the next available window
        
        Args:
            entry_id: Entry ID to schedule
            text: Post text (already formatted with number)
            
        Returns:
            String representation of scheduled time
        """
        scheduled_time = self.get_next_available_slot()
        
        # Add to scheduled_posts table
        self.db.schedule_post(entry_id, text, scheduled_time.isoformat())
        
        return scheduled_time.strftime("%d/%m/%Y %H:%M")
    
    def publish_due_posts(self):
        """
        Publish all posts that are due now
        """
        if not self.fb:
            print("Facebook handler not configured")
            return
        
        now = datetime.now(self.timezone)
        scheduled = self.db.get_scheduled_posts()
        
        for post in scheduled:
            scheduled_time = datetime.fromisoformat(post['scheduled_time'])
            
            # If the scheduled time has passed, publish it
            if scheduled_time <= now:
                try:
                    result = self.fb.publish_post(post['text'])
                    self.db.mark_as_published(post['id'], result['id'])
                    print(f"✓ Published post #{post['id']}: {result['id']}")
                except Exception as e:
                    print(f"✗ Failed to publish post #{post['id']}: {str(e)}")
