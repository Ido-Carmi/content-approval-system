"""
AI Training Feedback System
Aggregates daily feedback and sends to OpenAI for model improvement
"""

from typing import List, Dict
import json
from datetime import datetime


def create_training_prompt(feedback_items: List[Dict]) -> str:
    """Create a training prompt from feedback"""
    
    # Group feedback by type
    correct_hides = [f for f in feedback_items if f['feedback_type'] == 'correct_hide']
    false_positives = [f for f in feedback_items if f['feedback_type'] == 'false_positive']
    missed = [f for f in feedback_items if f['feedback_type'] == 'missed']
    
    prompt = f"""# Comment Moderation Training Feedback - {datetime.now().strftime('%Y-%m-%d')}

You are a comment moderator for an IDF (Israel Defense Forces) confessions page. Here is feedback on your performance from the past 24 hours:

## Summary
- Total feedback items: {len(feedback_items)}
- Correct predictions: {len(correct_hides)}
- False positives (wrongly flagged): {len(false_positives)}
- Missed violations: {len(missed)}

## Correct Predictions ({len(correct_hides)})
These comments were correctly flagged and subsequently deleted by admin:
"""
    
    for item in correct_hides[:10]:  # Max 10 examples
        prompt += f"\n- Comment: \"{item['comment_text']}\""
        prompt += f"\n  Your prediction: {item['ai_prediction']}"
        prompt += f"\n  Your reasoning: {item['ai_reason']}"
        prompt += f"\n  ✅ Admin confirmed by deleting\n"
    
    prompt += f"\n## False Positives ({len(false_positives)})"
    prompt += "\nThese comments were flagged but admin unhid them (you were wrong):\n"
    
    for item in false_positives[:10]:
        prompt += f"\n- Comment: \"{item['comment_text']}\""
        prompt += f"\n  Your prediction: {item['ai_prediction']}"
        prompt += f"\n  Your reasoning: {item['ai_reason']}"
        prompt += f"\n  ❌ Admin disagreed - comment was acceptable\n"
    
    prompt += f"\n## Missed Violations ({len(missed)})"
    prompt += "\nThese comments were NOT flagged but admin manually hid them:\n"
    
    for item in missed[:10]:
        prompt += f"\n- Comment: \"{item['comment_text']}\""
        prompt += f"\n  Your prediction: clean (no violation)"
        prompt += f"\n  Actual reason: {item['correct_reason']}"
        prompt += f"\n  ❌ You missed this violation\n"
    
    prompt += """

## Instructions for Improvement
Based on this feedback:
1. Analyze patterns in your false positives - what made you flag acceptable comments?
2. Analyze patterns in missed violations - what did you overlook?
3. Adjust your understanding of what constitutes political content vs acceptable military/army discussion
4. Be more careful to distinguish between:
   - Political discussion (parties, elections, government) → Flag
   - Military/service discussion (bases, units, experiences) → Don't flag
   
Please provide a brief analysis of what you learned from this feedback."""

    return prompt


def send_feedback_to_ai(feedback_items: List[Dict], openai_api_key: str) -> str:
    """Send aggregated feedback to OpenAI for analysis"""
    
    if not feedback_items:
        return "No feedback to send"
    
    try:
        import openai
        client = openai.OpenAI(api_key=openai_api_key)
        
        training_prompt = create_training_prompt(feedback_items)
        
        response = client.chat.completions.create(
            model="gpt-4o-mini",
            messages=[
                {
                    "role": "system",
                    "content": "You are a learning AI moderator. Analyze feedback and explain what you learned."
                },
                {
                    "role": "user",
                    "content": training_prompt
                }
            ],
            temperature=0.7,
            max_tokens=1000
        )
        
        analysis = response.choices[0].message.content
        
        return f"""📊 AI Training Feedback Analysis
{'='*60}

{training_prompt}

{'='*60}
🤖 AI Response:
{'='*60}

{analysis}

{'='*60}
✅ Feedback sent successfully at {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}
"""
        
    except Exception as e:
        return f"❌ Error sending feedback to AI: {str(e)}"


def aggregate_and_send_daily_feedback(db, config, notifications):
    """
    Daily job to aggregate feedback and send to AI
    Called at midnight
    """
    try:
        print("="*60)
        print(f"🎓 AI TRAINING FEEDBACK - {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
        print("="*60)
        
        # Get unsent feedback
        feedback = db.get_unsent_feedback()
        
        if not feedback:
            print("ℹ️  No feedback to send today")
            return
        
        print(f"📊 Found {len(feedback)} feedback items")
        
        # Send to AI
        openai_key = config.get('openai_api_key')
        if not openai_key:
            print("⚠️  No OpenAI API key configured")
            return
        
        analysis = send_feedback_to_ai(feedback, openai_key)
        print(analysis)
        
        # Mark as sent
        feedback_ids = [f['id'] for f in feedback]
        db.mark_feedback_sent(feedback_ids)
        
        # Send email notification with analysis
        if config.get('notifications_enabled') and config.get('notification_emails'):
            subject = f"🎓 AI Training Feedback - {datetime.now().strftime('%Y-%m-%d')}"
            notifications.send_notification(
                subject=subject,
                body=analysis,
                recipients=config['notification_emails']
            )
            print(f"📧 Email sent to {len(config['notification_emails'])} recipients")
        
        print("="*60)
        print("✅ AI training feedback completed")
        print("="*60)
        
    except Exception as e:
        print(f"❌ Error in AI training feedback: {e}")
        import traceback
        traceback.print_exc()
