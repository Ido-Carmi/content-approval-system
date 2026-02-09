"""
Hourly Comment Scanner
Scans Facebook posts for new comments and filters them
"""

import schedule
import time
import threading
from datetime import datetime
import pytz

class CommentScanner:
    def __init__(self, comments_handler, comment_filter, comments_db):
        self.comments_handler = comments_handler
        self.comment_filter = comment_filter
        self.comments_db = comments_db
        self.last_scan = None
        self.is_scanning = False
    
    def scan_and_filter(self):
        """Main scanning function - scans all posts and filters comments"""
        if self.is_scanning:
            print("⚠️  Scan already in progress, skipping...")
            return
        
        self.is_scanning = True
        israel_tz = pytz.timezone('Asia/Jerusalem')
        scan_start = datetime.now(israel_tz)
        
        print(f"🔍 Starting comment scan at {scan_start.strftime('%Y-%m-%d %H:%M:%S')}")
        
        try:
            # Get all posts with comments
            posts_data = self.comments_handler.scan_all_comments()
            
            total_comments = 0
            new_hidden = 0
            already_hidden = 0
            
            for post_data in posts_data:
                post_id = post_data['post_id']
                post_message = post_data['post_message']
                comments = post_data['comments']
                
                total_comments += len(comments)
                
                # Filter out already visible or already processed comments
                comments_to_check = [
                    c for c in comments 
                    if not c.get('is_hidden', False)  # Not already hidden on FB
                    and not self.comments_db.is_comment_already_hidden(c['id'])  # Not in our DB
                ]
                
                if not comments_to_check:
                    continue
                
                print(f"  Checking {len(comments_to_check)} comments on post {post_id}")
                
                # Analyze comments with AI
                analyses = self.comment_filter.batch_analyze(
                    comments_to_check,
                    context=post_message
                )
                
                # Hide comments that should be hidden
                for comment in comments_to_check:
                    comment_id = comment['id']
                    analysis = analyses.get(comment_id)
                    
                    if not analysis or not analysis.get('should_hide'):
                        continue
                    
                    # Hide on Facebook
                    if self.comments_handler.hide_comment(comment_id):
                        # Save to database
                        self.comments_db.add_hidden_comment(
                            comment_id=comment_id,
                            post_id=post_id,
                            post_message=post_message,
                            commenter_name=comment.get('from', {}).get('name', 'Unknown'),
                            commenter_id=comment.get('from', {}).get('id', ''),
                            comment_text=comment.get('message', ''),
                            ai_result=analysis,
                            created_time=comment.get('created_time', '')
                        )
                        new_hidden += 1
                        print(f"    ✓ Hidden comment {comment_id}: {analysis.get('reason')}")
                    else:
                        print(f"    ✗ Failed to hide comment {comment_id}")
            
            self.last_scan = scan_start
            
            scan_end = datetime.now(israel_tz)
            duration = (scan_end - scan_start).total_seconds()
            
            print(f"✓ Scan complete in {duration:.1f}s")
            print(f"  Total comments: {total_comments}")
            print(f"  Newly hidden: {new_hidden}")
            print(f"  Already hidden: {already_hidden}")
            
            return {
                'success': True,
                'total_comments': total_comments,
                'new_hidden': new_hidden,
                'already_hidden': already_hidden,
                'duration': duration,
                'timestamp': scan_start.strftime('%Y-%m-%d %H:%M:%S')
            }
            
        except Exception as e:
            print(f"❌ Error during comment scan: {e}")
            import traceback
            traceback.print_exc()
            return {
                'success': False,
                'error': str(e)
            }
        finally:
            self.is_scanning = False
    
    def start_scheduled_scanning(self):
        """Start the hourly scanning schedule"""
        # Schedule to run every hour
        schedule.every().hour.do(self.scan_and_filter)
        
        # Also run immediately on startup
        print("🚀 Starting comment scanner...")
        self.scan_and_filter()
        
        # Run scheduler in background thread
        def run_schedule():
            while True:
                schedule.run_pending()
                time.sleep(60)  # Check every minute
        
        scanner_thread = threading.Thread(target=run_schedule, daemon=True)
        scanner_thread.start()
        print("✓ Comment scanner started - running every hour")
