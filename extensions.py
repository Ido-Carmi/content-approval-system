"""
Shared application state — imported by background_tasks, routes_comments, and reconciler.
Initialized once at startup; handlers reinitialised via init_handlers().
"""
import threading
from database import Database
from notifications import NotificationHandler
from job_queue import JobQueue

# Core objects
db = Database()
scheduler = None
facebook_handler = None
facebook_comments_handler = None
sheets_handler = None
notifications = NotificationHandler()

# Reconciler wakeup signal — set() wakes the reconciler immediately
reconcile_event = threading.Event()

# Comment action queue — serialises all FB comment API calls (hide/unhide/delete)
comment_queue = JobQueue(db, queue_name='comments')

# Webhook batch queue
webhook_queue = []
webhook_queue_lock = threading.Lock()
first_queued_time = None


def init_handlers():
    """(Re-)initialise Facebook, Scheduler, and Sheets handlers from config."""
    global scheduler, facebook_handler, facebook_comments_handler, sheets_handler

    from config import load_config
    from facebook_handler import FacebookHandler
    from facebook_comments_handler import FacebookCommentsHandler
    from sheets_handler import SheetsHandler
    from scheduler import Scheduler

    config = load_config()

    if config.get('facebook_page_id') and config.get('facebook_access_token'):
        try:
            facebook_handler = FacebookHandler(
                config['facebook_page_id'],
                config['facebook_access_token']
            )
            facebook_comments_handler = FacebookCommentsHandler(
                access_token=config['facebook_access_token'],
                page_id=config['facebook_page_id']
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
