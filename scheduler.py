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
        return date.weekday() in [4, 5]
    
    def is_jewish_holiday(self, date: datetime.date) -> bool:
        """Check if a date is a Jewish holiday (non-work day)"""
        date_str = date.strftime('%Y-%m-%d')
        return date_str in self.jewish_holidays
    
    def should_skip_date(self, date: datetime.date) -> bool:
        """Check if a date should be skipped based on config"""
        config = self.load_config()
        skip_shabbat = config.get('skip_shabbat', True)
        skip_holidays = config.get('skip_jewish_holidays', True)
        
        if skip_shabbat and self.is_shabbat(date):
            return True
        
        if skip_holidays and self.is_jewish_holiday(date):
            return True
        
        return False
    
    def get_scheduled_times_from_facebook(self) -> List[datetime]:
        """Get all scheduled times from Facebook"""
        try:
            fb_posts = self.fb.get_scheduled_posts()
            times = []
            for post in fb_posts:
                dt = datetime.fromisoformat(post['scheduled_time'])
                times.append(dt)
            return times
        except:
            return []
    
    def get_next_available_slot(self) -> datetime:
        """
        Get the next available posting slot
        Checks Facebook's scheduler to find empty slot
        """
        windows = self.load_posting_windows()
        now = datetime.now(self.timezone)
        
        # Get already scheduled times from Facebook
        scheduled_times = self.get_scheduled_times_from_facebook()
        scheduled_slots = set()
        for st in scheduled_times:
            # Store as (date, time) tuple for comparison
            scheduled_slots.add((st.date(), st.time().replace(second=0, microsecond=0)))
        
        # Try to find a slot today
        current_date = now.date()
        if not self.should_skip_date(current_date):
            for window_time in windows:
                slot = self.timezone.localize(datetime.combine(current_date, window_time))
                slot_key = (slot.date(), slot.time())
                
                if slot > now and slot_key not in scheduled_slots:
                    return slot
        
        # Look for future slots
        days_checked = 0
        max_days = 365
        
        while days_checked < max_days:
            days_checked += 1
            check_date = current_date + timedelta(days=days_checked)
            
            if self.should_skip_date(check_date):
                continue
            
            for window_time in windows:
                slot = self.timezone.localize(datetime.combine(check_date, window_time))
                slot_key = (slot.date(), slot.time())
                
                if slot_key not in scheduled_slots:
                    return slot
        
        # Fallback
        fallback_days = 1
        while fallback_days < 365:
            fallback_date = current_date + timedelta(days=fallback_days)
            if not self.should_skip_date(fallback_date):
                return self.timezone.localize(datetime.combine(fallback_date, windows[0]))
            fallback_days += 1
        
        return self.timezone.localize(datetime.combine(current_date + timedelta(days=1), windows[0]))
    
    def schedule_post_to_facebook(self, entry_id: int, text: str) -> Dict:
        """
        Schedule a post to Facebook's native scheduler
        
        Args:
            entry_id: Entry ID
            text: Post text (already formatted with number)
            
        Returns:
            Dict with scheduled_time and facebook_post_id
        """
        scheduled_time = self.get_next_available_slot()
        
        # Schedule to Facebook
        result = self.fb.schedule_post(text, scheduled_time)
        
        # Save to database
        self.db.schedule_to_facebook(
            entry_id,
            result['id'],
            result['scheduled_time']
        )
        
        return {
            'scheduled_time': scheduled_time.strftime("%d/%m/%Y %H:%M"),
            'facebook_post_id': result['id']
        }
    
    def reschedule_post(self, entry_id: int, new_time: datetime) -> bool:
        """
        Reschedule a post to a new time on Facebook
        
        Args:
            entry_id: Entry ID
            new_time: New scheduled time
            
        Returns:
            True if successful
        """
        # Get entry with Facebook post ID
        entries = self.db.get_scheduled_entries()
        entry = next((e for e in entries if e['id'] == entry_id), None)
        
        if not entry or not entry['facebook_post_id']:
            return False
        
        try:
            # Update on Facebook
            result = self.fb.update_scheduled_post(
                entry['facebook_post_id'],
                new_time=new_time
            )
            
            # Update in database
            self.db.schedule_to_facebook(
                entry_id,
                result['id'],
                result['scheduled_time']
            )
            
            return True
        except Exception as e:
            print(f"Failed to reschedule: {str(e)}")
            return False
    
    def unschedule_post(self, entry_id: int) -> bool:
        """
        Remove post from Facebook scheduler, return to pending, and renumber all following posts
        
        Args:
            entry_id: Entry ID
            
        Returns:
            True if successful
        """
        # Get entry with Facebook post ID
        entries = self.db.get_scheduled_entries()
        entry = next((e for e in entries if e['id'] == entry_id), None)
        
        if not entry or not entry['facebook_post_id']:
            return False
        
        unscheduled_number = entry.get('post_number')
        
        try:
            # Delete from Facebook
            self.fb.delete_scheduled_post(entry['facebook_post_id'])
            
            # Update database - return to pending and renumber
            self.db.unschedule_entry(entry_id)
            
            # Renumber all posts after this one on Facebook
            if unscheduled_number:
                posts_to_update = self.db.get_posts_needing_renumber(unscheduled_number)
                
                for post in posts_to_update:
                    # Get scheduled time
                    scheduled_time = datetime.fromisoformat(post['scheduled_time'])
                    
                    # Create new text with updated number (space, not newlines)
                    new_text = f"#{post['post_number']} {post['text']}"
                    
                    # Update on Facebook
                    result = self.fb.update_scheduled_post(
                        post['facebook_post_id'],
                        new_text=new_text,
                        new_time=scheduled_time
                    )
                    
                    # Update database with new Facebook post ID
                    self.db.schedule_to_facebook(
                        post['id'],
                        result['id'],
                        result['scheduled_time']
                    )
            
            return True
        except Exception as e:
            print(f"Failed to unschedule: {str(e)}")
            return False
    
    def reschedule_all_to_new_windows(self) -> int:
        """
        Reschedule all scheduled posts to match new posting windows
        
        Returns:
            Number of posts rescheduled
        """
        # Get all scheduled entries
        entries = self.db.get_scheduled_entries()
        
        if not entries:
            return 0
        
        # Get current windows
        windows = self.load_posting_windows()
        
        # Group entries by date
        entries_by_date = {}
        for entry in entries:
            scheduled_dt = datetime.fromisoformat(entry['scheduled_time'])
            date_key = scheduled_dt.date()
            
            if date_key not in entries_by_date:
                entries_by_date[date_key] = []
            entries_by_date[date_key].append(entry)
        
        rescheduled_count = 0
        
        # For each date, redistribute posts across windows
        for date, date_entries in sorted(entries_by_date.items()):
            if self.should_skip_date(date):
                # This date should be skipped now - reschedule to next valid date
                for entry in date_entries:
                    new_time = self.get_next_available_slot()
                    if self.reschedule_post(entry['id'], new_time):
                        rescheduled_count += 1
            else:
                # Redistribute across windows for this date
                for idx, entry in enumerate(date_entries):
                    window_idx = idx % len(windows)
                    new_time = self.timezone.localize(
                        datetime.combine(date, windows[window_idx])
                    )
                    
                    if self.reschedule_post(entry['id'], new_time):
                        rescheduled_count += 1
        
        return rescheduled_count
    
    def update_scheduled_post_content(self, entry_id: int, new_text_with_number: str) -> bool:
        """
        Update the content of a scheduled post on Facebook
        NOTE: Database text should already be updated by caller (without number)
        
        Args:
            entry_id: Entry ID
            new_text_with_number: Full post text including number for Facebook
            
        Returns:
            True if successful
        """
        entries = self.db.get_scheduled_entries()
        entry = next((e for e in entries if e['id'] == entry_id), None)
        
        if not entry or not entry['facebook_post_id']:
            return False
        
        try:
            # Get current scheduled time
            scheduled_time = datetime.fromisoformat(entry['scheduled_time'])
            
            # Update on Facebook (delete and recreate with new text)
            result = self.fb.update_scheduled_post(
                entry['facebook_post_id'],
                new_text=new_text_with_number,
                new_time=scheduled_time
            )
            
            # Update Facebook post ID in database
            self.db.schedule_to_facebook(
                entry_id,
                result['id'],
                result['scheduled_time']
            )
            
            return True
        except Exception as e:
            print(f"Failed to update post: {str(e)}")
            return False
    
    def swap_post_times(self, entry_id1: int, entry_id2: int) -> bool:
        """
        Swap the scheduled times and post numbers of two posts
        
        Args:
            entry_id1: First entry ID
            entry_id2: Second entry ID
            
        Returns:
            True if successful
        """
        # First get the entries to know their texts (without numbers)
        entries = self.db.get_scheduled_entries()
        entry1 = next((e for e in entries if e['id'] == entry_id1), None)
        entry2 = next((e for e in entries if e['id'] == entry_id2), None)
        
        if not entry1 or not entry2:
            return False
        
        # Swap in database (swaps times AND post_numbers)
        swap_info = self.db.swap_scheduled_times(entry_id1, entry_id2)
        
        if not swap_info:
            return False
        
        try:
            # After swap: entry1 has number2 at time2, entry2 has number1 at time1
            time1 = datetime.fromisoformat(swap_info['time1'])
            time2 = datetime.fromisoformat(swap_info['time2'])
            number1 = swap_info['number1']
            number2 = swap_info['number2']
            
            # Build new texts with swapped numbers
            text1_with_new_number = f"#{number2} {entry1['text']}"  # entry1 gets number2
            text2_with_new_number = f"#{number1} {entry2['text']}"  # entry2 gets number1
            
            # Update post 1: new time (time2), new number (number2)
            result1 = self.fb.update_scheduled_post(
                swap_info['post1_fb_id'],
                new_text=text1_with_new_number,
                new_time=time2
            )
            
            # Update post 2: new time (time1), new number (number1)
            result2 = self.fb.update_scheduled_post(
                swap_info['post2_fb_id'],
                new_text=text2_with_new_number,
                new_time=time1
            )
            
            # Update database with new Facebook post IDs
            self.db.schedule_to_facebook(entry_id1, result1['id'], result1['scheduled_time'])
            self.db.schedule_to_facebook(entry_id2, result2['id'], result2['scheduled_time'])
            
            return True
        except Exception as e:
            print(f"Failed to swap times: {str(e)}")
            return False
    
    def sync_with_facebook(self):
        """
        Sync local database with Facebook's scheduler
        Mark posts as published if they're no longer in Facebook's scheduled list
        """
        try:
            # Get scheduled posts from Facebook
            fb_posts = self.fb.get_scheduled_posts()
            fb_post_ids = {post['id'] for post in fb_posts}
            
            # Get scheduled posts from database
            db_entries = self.db.get_scheduled_entries()
            
            # Check which posts are no longer scheduled (were published)
            for entry in db_entries:
                if entry['facebook_post_id'] not in fb_post_ids:
                    # Post was published
                    self.db.mark_as_published(entry['id'])
                    print(f"✓ Post #{entry['id']} marked as published")
        except Exception as e:
            print(f"Sync error: {str(e)}")
