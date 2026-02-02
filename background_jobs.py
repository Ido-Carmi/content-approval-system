import schedule
import time
import json
from pathlib import Path
from datetime import datetime, timedelta
import pytz
from sheets_handler import SheetsHandler
from facebook_handler import FacebookHandler
from database import Database
from scheduler import Scheduler
from notifications import NotificationHandler

class BackgroundJobs:
    def __init__(self):
        self.db = Database()
        self.sheets_handler = None
        self.facebook_handler = None
        self.scheduler = None
        self.notifications = NotificationHandler()
        self.load_config()
    
    def load_config(self):
        """Load configuration and initialize handlers"""
        config_file = Path("config.json")
        
        if not config_file.exists():
            print("No configuration file found. Please set up the application first.")
            return
        
        with open(config_file, 'r', encoding='utf-8') as f:
            config = json.load(f)
        
        # Initialize Google Sheets handler
        try:
            self.sheets_handler = SheetsHandler(
                config.get('google_sheet_id'),
                config.get('google_credentials_file', 'credentials.json')
            )
            print("✓ Google Sheets handler initialized")
        except Exception as e:
            print(f"✗ Failed to initialize Google Sheets: {e}")
        
        # Initialize Facebook handler
        try:
            self.facebook_handler = FacebookHandler(
                config.get('facebook_page_id'),
                config.get('facebook_access_token')
            )
            print("✓ Facebook handler initialized")
        except Exception as e:
            print(f"✗ Failed to initialize Facebook: {e}")
        
        # Initialize scheduler
        if self.facebook_handler:
            self.scheduler = Scheduler(self.db, self.facebook_handler)
            print("✓ Scheduler initialized")
    
    def check_empty_windows(self):
        """Check for empty windows in the next 24 hours"""
        if not self.scheduler:
            return None
        
        israel_tz = pytz.timezone('Asia/Jerusalem')
        now = datetime.now(israel_tz)
        tomorrow = now + timedelta(hours=24)
        
        # Get posting windows
        windows = self.scheduler.load_posting_windows()
        
        # Get scheduled posts
        scheduled = self.scheduler.db.get_scheduled_posts()
        scheduled_times = set()
        for post in scheduled:
            post_dt = datetime.fromisoformat(post['scheduled_time'])
            scheduled_times.add((post_dt.date(), post_dt.time().replace(second=0, microsecond=0)))
        
        # Check each window in the next 24 hours
        current_date = now.date()
        for days_ahead in range(2):  # Check today and tomorrow
            check_date = current_date + timedelta(days=days_ahead)
            
            for window_time in windows:
                window_dt = israel_tz.localize(datetime.combine(check_date, window_time))
                
                # Only check windows in the next 24 hours
                if now < window_dt <= tomorrow:
                    window_key = (check_date, window_time)
                    
                    if window_key not in scheduled_times:
                        # Found an empty window!
                        return window_dt.strftime('%d/%m/%Y %H:%M')
        
        return None
    
    def sync_google_sheets(self):
        """Sync new entries from Google Sheets"""
        if not self.sheets_handler:
            print("Google Sheets handler not available")
            return
        
        try:
            print(f"[{datetime.now()}] Starting Google Sheets sync...")
            
            new_entries = self.sheets_handler.fetch_new_entries()
            added_count = 0
            
            for entry in new_entries:
                if self.db.add_entry(entry['timestamp'], entry['text']):
                    added_count += 1
            
            # Update last sync time in config
            config_file = Path("config.json")
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            israel_tz = pytz.timezone('Asia/Jerusalem')
            config['last_sync'] = datetime.now(israel_tz).strftime("%Y-%m-%d %H:%M:%S")
            
            with open(config_file, 'w', encoding='utf-8') as f:
                json.dump(config, f, indent=2, ensure_ascii=False)
            
            print(f"✓ Sync completed. Added {added_count} new entries.")
            
            # Check if we should send notification
            if config.get('notifications_enabled', False):
                pending_count = self.db.get_statistics()['pending']
                threshold = config.get('pending_threshold', 20)
                
                if pending_count > threshold:
                    print(f"Pending count ({pending_count}) exceeds threshold ({threshold})")
                    next_empty = self.check_empty_windows()
                    self.notifications.send_pending_threshold_alert(pending_count, next_empty)
                    print("✓ Notification sent")
            
        except Exception as e:
            print(f"✗ Sync failed: {e}")
    
    def check_notifications(self):
        """Check if we need to send empty window notifications"""
        try:
            config_file = Path("config.json")
            with open(config_file, 'r', encoding='utf-8') as f:
                config = json.load(f)
            
            if not config.get('notifications_enabled', False):
                return
            
            # Check for empty windows
            next_empty = self.check_empty_windows()
            
            if next_empty:
                pending_count = self.db.get_statistics()['pending']
                print(f"Found empty window: {next_empty}, pending: {pending_count}")
                self.notifications.send_empty_window_alert(next_empty, pending_count)
                print("✓ Empty window notification sent")
        
        except Exception as e:
            print(f"✗ Notification check failed: {e}")
    
    def publish_due_posts(self):
        """Check and publish any posts that are due"""
        if not self.scheduler:
            print("Scheduler not available")
            return
        
        try:
            print(f"[{datetime.now()}] Checking for due posts...")
            
            results = self.scheduler.publish_due_posts()
            
            if results:
                for result in results:
                    if result['success']:
                        print(f"✓ Published post {result['post_id']} (Facebook ID: {result['facebook_id']})")
                    else:
                        print(f"✗ Failed to publish post {result['post_id']}: {result['error']}")
            else:
                print("No posts due for publishing")
                
        except Exception as e:
            print(f"✗ Publishing check failed: {e}")
    
    def run_scheduler(self):
        """Run the background scheduler"""
        print("Background job scheduler started")
        print("=" * 50)
        
        # Schedule daily sync at midnight Israel time
        schedule.every().day.at("00:00").do(self.sync_google_sheets)
        
        # Check for posts to publish every 5 minutes
        schedule.every(5).minutes.do(self.publish_due_posts)
        
        # Check for empty windows every hour
        schedule.every().hour.do(self.check_notifications)
        
        # Run initial checks
        print("Running initial sync and publish check...")
        self.sync_google_sheets()
        self.publish_due_posts()
        self.check_notifications()
        
        print("\nScheduled jobs:")
        print("- Sync Google Sheets: Daily at 00:00 (Israel time)")
        print("- Publish due posts: Every 5 minutes")
        print("- Check for empty windows: Every hour")
        print("\nPress Ctrl+C to stop")
        print("=" * 50)
        
        # Keep running
        while True:
            schedule.run_pending()
            time.sleep(30)  # Check every 30 seconds

if __name__ == "__main__":
    jobs = BackgroundJobs()
    jobs.run_scheduler()
