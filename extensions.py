"""
Shared application state — imported by background_tasks and routes_comments.
Initialized once at startup; handlers reinitialised via init_handlers().
"""
import threading
from database import Database
from notifications import NotificationHandler

# Core objects
db = Database()
scheduler = None
facebook_handler = None
sheets_handler = None
notifications = NotificationHandler()

# Comment-scanning state
scan_in_progress = False

# Webhook batch queue
webhook_queue = []
webhook_queue_lock = threading.Lock()
first_queued_time = None


def init_handlers():
    """(Re-)initialise Facebook, Scheduler, and Sheets handlers from config."""
    global scheduler, facebook_handler, sheets_handler

    from config import load_config
    from facebook_handler import FacebookHandler
    from sheets_handler import SheetsHandler
    from scheduler import Scheduler

    config = load_config()

    if config.get('facebook_page_id') and config.get('facebook_access_token'):
        try:
            facebook_handler = FacebookHandler(
                config['facebook_page_id'],
                config['facebook_access_token']
            )
            scheduler = Scheduler(db, facebook_handler)
        except Exception as e:
            print(f"Failed to init Facebook: {e}")

    if config.get('google_sheet_id'):
        try:
            sheets_handler = SheetsHandler(
                config['google_sheet_id'],
                config.get('google_credentials_file', 'credentials.json')
            )
        except Exception as e:
            print(f"Failed to init Sheets: {e}")
