import resend
from typing import List, Optional
from config import load_config, save_config
from datetime import datetime
import pytz

class NotificationHandler:
    def __init__(self):
        """Initialize notification handler"""
        self.config = load_config()
        self.resend_api_key = self.config.get('resend_api_key', '')
        self.resend_from_email = self.config.get('resend_from_email', '')
        self.notification_emails = self.config.get('notification_emails', [])
        self.notifications_enabled = self.config.get('notifications_enabled', False)
        self.pending_threshold = self.config.get('pending_threshold', 20)
        self.app_url = self.config.get('app_url', 'http://localhost:8501')
        self.last_empty_window_alert = self.config.get('last_empty_window_alert')
    
    def save_last_alert_time(self):
        """Save the last time empty window alert was sent"""
        config = load_config()
        israel_tz = pytz.timezone('Asia/Jerusalem')
        config['last_empty_window_alert'] = datetime.now(israel_tz).isoformat()
        save_config(config)
    
    def should_send_empty_window_alert(self) -> bool:
        """Check if enough time has passed since last empty window alert (1 hour)"""
        if not self.last_empty_window_alert:
            return True
        
        try:
            israel_tz = pytz.timezone('Asia/Jerusalem')
            last_alert = datetime.fromisoformat(self.last_empty_window_alert)
            if last_alert.tzinfo is None:
                last_alert = israel_tz.localize(last_alert)
            
            now = datetime.now(israel_tz)
            hours_since_last = (now - last_alert).total_seconds() / 3600
            
            return hours_since_last >= 1
        except:
            return True
    
    def send_email(self, subject: str, body: str) -> bool:
        """
        Send email notification using Resend API (HTTPS, no SMTP needed)
        """
        # Reload config each time (settings may have changed since init)
        config = load_config()
        api_key = config.get('resend_api_key', '')
        from_email = config.get('resend_from_email', '')
        notification_emails = config.get('notification_emails', [])
        notifications_enabled = config.get('notifications_enabled', False)
        
        if not notifications_enabled:
            print("Notifications are disabled")
            return False
        
        if not api_key:
            print("❌ Resend API key not configured")
            return False
        
        if not from_email:
            print("❌ Resend from email not configured")
            return False
        
        if not notification_emails:
            print("No notification email addresses configured")
            return False
        
        try:
            resend.api_key = api_key
            
            params = {
                "from": from_email,
                "to": notification_emails,
                "subject": subject,
                "html": body,
            }
            
            result = resend.Emails.send(params)
            print(f"✅ Notification sent via Resend to {', '.join(notification_emails)} (id: {result.get('id', '?')})")
            return True
            
        except Exception as e:
            print(f"❌ Failed to send notification via Resend: {str(e)}")
            return False
    
    def send_pending_threshold_alert(self, pending_count: int, next_empty_window: Optional[str] = None):
        """Send alert when pending entries exceed threshold"""
        subject = f"⚠️ {pending_count} Pending Entries - Action Required"
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #dc3545;">⚠️ High Number of Pending Entries</h2>
            <p>Your content approval system has <strong>{pending_count} pending entries</strong> waiting for review.</p>
            <p>This exceeds your configured threshold of <strong>{self.pending_threshold}</strong> entries.</p>
            
            {f'<p style="color: #856404; background: #fff3cd; padding: 10px; border-radius: 5px;">⏰ <strong>Next empty window:</strong> {next_empty_window}</p>' if next_empty_window else ''}
            
            <p style="margin-top: 30px;">
                <a href="{self.app_url}" 
                   style="background-color: #28a745; color: white; padding: 12px 24px; 
                          text-decoration: none; border-radius: 5px; display: inline-block;">
                    Review Entries Now
                </a>
            </p>
            
            <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
            <p style="color: #666; font-size: 12px;">
                Sent by Content Approval & Publishing System<br>
                {datetime.now(pytz.timezone('Asia/Jerusalem')).strftime('%d/%m/%Y %H:%M')} (Israel Time)
            </p>
        </body>
        </html>
        """
        
        self.send_email(subject, body)
    
    def send_empty_window_alert(self, next_empty_window: str, pending_count: int):
        """Send alert when there's an empty window in the next 24 hours"""
        if not self.should_send_empty_window_alert():
            print("Skipping empty window alert - sent less than 1 hour ago")
            return
        
        subject = f"⏰ Empty Posting Window - {next_empty_window}"
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #856404;">⏰ Empty Posting Window Alert</h2>
            <p>There is a posting window in the next 24 hours with <strong>no scheduled content</strong>:</p>
            
            <div style="background: #fff3cd; padding: 15px; border-radius: 5px; margin: 20px 0;">
                <h3 style="margin: 0; color: #856404;">📅 {next_empty_window}</h3>
            </div>
            
            <p>You currently have <strong>{pending_count} pending entries</strong> waiting for review.</p>
            
            <p style="margin-top: 30px;">
                <a href="{self.app_url}" 
                   style="background-color: #007bff; color: white; padding: 12px 24px; 
                          text-decoration: none; border-radius: 5px; display: inline-block;">
                    Review & Schedule Content
                </a>
            </p>
            
            <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
            <p style="color: #666; font-size: 12px;">
                Sent by Content Approval & Publishing System<br>
                {datetime.now(pytz.timezone('Asia/Jerusalem')).strftime('%d/%m/%Y %H:%M')} (Israel Time)<br>
                <em>This alert is sent once per hour to avoid spam</em>
            </p>
        </body>
        </html>
        """
        
        if self.send_email(subject, body):
            self.save_last_alert_time()
    
    def send_test_notification(self) -> bool:
        """Send a test notification to verify configuration"""
        subject = "✅ Test Notification - Content Approval System"
        
        body = f"""
        <html>
        <body style="font-family: Arial, sans-serif; padding: 20px;">
            <h2 style="color: #28a745;">✅ Test Notification Successful!</h2>
            <p>Your notification system is configured correctly and working.</p>
            
            <div style="background: #d4edda; padding: 15px; border-radius: 5px; margin: 20px 0;">
                <p><strong>Configuration:</strong></p>
                <ul>
                    <li>Notifications: <strong>Enabled</strong></li>
                    <li>Pending Threshold: <strong>{self.pending_threshold}</strong></li>
                    <li>Email Addresses: <strong>{len(self.notification_emails)}</strong> configured</li>
                    <li>From: <strong>{self.resend_from_email}</strong></li>
                </ul>
            </div>
            
            <p>You will receive notifications when:</p>
            <ul>
                <li>Pending entries exceed {self.pending_threshold}</li>
                <li>A posting window in the next 24 hours has no scheduled content</li>
            </ul>
            
            <p style="margin-top: 30px;">
                <a href="{self.app_url}" 
                   style="background-color: #007bff; color: white; padding: 12px 24px; 
                          text-decoration: none; border-radius: 5px; display: inline-block;">
                    Go to App
                </a>
            </p>
            
            <hr style="margin-top: 30px; border: none; border-top: 1px solid #ddd;">
            <p style="color: #666; font-size: 12px;">
                Test sent: {datetime.now(pytz.timezone('Asia/Jerusalem')).strftime('%d/%m/%Y %H:%M')} (Israel Time)
            </p>
        </body>
        </html>
        """
        
        return self.send_email(subject, body)
