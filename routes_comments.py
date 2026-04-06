"""
All comment-related routes and webhook handling.
Call register(app) from app.py once after Flask app is created.
"""
import json
import threading
import traceback
import pytz
from datetime import datetime

from flask import request, jsonify, redirect, url_for, render_template, flash

from config import load_config, save_config
import extensions
from background_tasks import check_comment_notification, send_notification_email


# ============================================================================
# HELPERS
# ============================================================================

_PROCESSING_RESPONSE = '''
    <button class="btn btn-sm btn-secondary" disabled>
        <i class="bi bi-hourglass-split"></i> מעבד...
    </button>
    <script>
        setTimeout(() => location.reload(), 1000);
    </script>
'''

_LABEL_DESCRIPTIONS = {
    'political': 'Political content',
    'hate': 'Hate speech',
    'spam': 'Spam',
}


def _get_fb_handler():
    from facebook_comments_handler import FacebookCommentsHandler
    config = load_config()
    if not config.get('facebook_access_token') or not config.get('facebook_page_id'):
        return None
    return FacebookCommentsHandler(
        access_token=config['facebook_access_token'],
        page_id=config['facebook_page_id']
    )


def _get_comment(comment_id):
    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    cursor.execute('SELECT * FROM hidden_comments WHERE comment_id = ?', (comment_id,))
    comment = cursor.fetchone()
    conn.close()
    return comment


def _run_async(fn):
    threading.Thread(target=fn, daemon=True).start()


def _mark_and_delete_comment(comment_id, label):
    """Shared logic: delete from Facebook + save AI training data + dismiss."""
    def async_action():
        try:
            fb = _get_fb_handler()
            if not fb:
                return
            comment = _get_comment(comment_id)
            if not comment:
                return

            success = fb.delete_comment(comment_id)

            ai_said = comment['filter_reason']
            category = f'correct_{label}' if ai_said == label else f'missed_{label}'

            extensions.db.add_ai_example(
                category=category,
                comment_text=comment['comment_text'],
                original_ai_prediction=ai_said,
                explanation=f"{_LABEL_DESCRIPTIONS.get(label, label)} - admin marked for deletion"
            )
            extensions.db.save_training_data(
                comment_id=comment_id,
                post_id=comment.get('post_id', ''),
                comment_text=comment['comment_text'],
                admin_label=label,
                ai_prediction=ai_said,
                ai_explanation=comment.get('ai_explanation', '')
            )
            extensions.db.dismiss_comment(comment_id)

            status = "✅" if success else "⚠️  Facebook delete failed —"
            print(f"{status} Marked as {label}: {comment_id} → {category}")

        except Exception as e:
            print(f"❌ Error marking {label}: {e}")

    _run_async(async_action)
    return '', 200


def _batch_mark_ok(comment_ids):
    """Unhide + train + dismiss a list of comments in a background thread."""
    def async_batch():
        try:
            fb = _get_fb_handler()
            for cid in comment_ids:
                try:
                    comment = _get_comment(cid)
                    if not comment:
                        continue
                    if fb and comment['status'] == 'hidden':
                        fb.unhide_comment(cid)
                    ai_said = comment['filter_reason']
                    if ai_said and ai_said != 'clean':
                        extensions.db.add_ai_example(
                            category=f'false_positive_{ai_said}',
                            comment_text=comment['comment_text'],
                            original_ai_prediction=ai_said,
                            explanation="False positive - batch marked as OK"
                        )
                    extensions.db.dismiss_comment(cid)
                except Exception as e:
                    print(f"⚠️  Error marking comment {cid} as OK: {e}")
                    try:
                        extensions.db.dismiss_comment(cid)
                    except Exception:
                        pass
            print(f"✅ Batch mark-ok complete: {len(comment_ids)} comments")
        except Exception as e:
            print(f"❌ Batch mark-ok error: {e}")

    _run_async(async_batch)


# ============================================================================
# WEBHOOK BATCH QUEUE
# ============================================================================

def process_webhook_batch():
    """Process all queued webhook comments through AI in one batch."""
    with extensions.webhook_queue_lock:
        if not extensions.webhook_queue:
            return
        batch = list(extensions.webhook_queue)
        extensions.webhook_queue.clear()
        extensions.first_queued_time = None

    config = load_config()

    if not config.get('comments_filter_enabled') or not config.get('openai_api_key'):
        for cd in batch:
            cd['should_hide'] = False
            cd['filter_reason'] = 'clean'
            cd['ai_explanation'] = ''
            extensions.db.add_comment(cd)
        print(f"📝 Saved {len(batch)} comments without AI filter")
        return

    print(f"\n🤖 Batch processing {len(batch)} webhook comments with AI...")

    try:
        from ai_comment_filter import CommentFilter
        ai_filter = CommentFilter(config['openai_api_key'], extensions.db)
        results = ai_filter.filter_comments_batch(batch, batch_size=30)
        result_map = {r['comment_id']: r for r in results}

        hidden_count = 0
        for cd in batch:
            result = result_map.get(cd['comment_id'])
            if result:
                cd['should_hide'] = result['should_hide']
                cd['filter_reason'] = result['reason']
                cd['ai_explanation'] = result['explanation']
            else:
                cd['should_hide'] = False
                cd['filter_reason'] = 'clean'
                cd['ai_explanation'] = ''

            extensions.db.add_comment(cd)

            if cd['should_hide']:
                hidden_count += 1
                try:
                    from facebook_comments_handler import FacebookCommentsHandler
                    fb = FacebookCommentsHandler(config['facebook_access_token'], config['facebook_page_id'])
                    fb.hide_comment(cd['comment_id'])
                    print(f"   🚫 Hidden: {cd['comment_id'][:30]}... ({cd['filter_reason']})")
                except Exception as e:
                    print(f"   ❌ Error hiding: {e}")

        print(f"✅ Batch complete: {len(batch)} comments, {hidden_count} hidden")

        check_comment_notification()

        config = load_config()
        config['last_webhook_comment_time'] = datetime.now(pytz.timezone('Asia/Jerusalem')).isoformat()
        save_config(config)

    except Exception as e:
        print(f"❌ Batch processing error: {e}")
        traceback.print_exc()
        for cd in batch:
            extensions.db.queue_comment(cd)


def _webhook_batch_checker():
    """Background thread: flush queue when count or age threshold is reached."""
    import time
    while True:
        time.sleep(30)
        try:
            config = load_config()
            batch_size = config.get('webhook_batch_size', 10)
            batch_timeout = config.get('webhook_batch_timeout_minutes', 5)

            with extensions.webhook_queue_lock:
                queue_size = len(extensions.webhook_queue)
                queued_since = extensions.first_queued_time

            if queue_size == 0:
                continue

            if queue_size >= batch_size:
                print(f"⏰ Batch trigger: {queue_size} comments >= {batch_size} threshold")
                process_webhook_batch()
            elif queued_since:
                minutes_waiting = (datetime.now() - queued_since).total_seconds() / 60
                if minutes_waiting >= batch_timeout:
                    print(f"⏰ Batch trigger: {minutes_waiting:.0f} min since first comment ({queue_size} in queue)")
                    process_webhook_batch()
        except Exception as e:
            print(f"❌ Batch checker error: {e}")


# ============================================================================
# ROUTE REGISTRATION
# ============================================================================

def register(app):
    """Register all comment + webhook routes on the Flask app."""

    # Start the batch checker thread
    threading.Thread(target=_webhook_batch_checker, daemon=True).start()

    # ---- Comments page ----

    @app.route('/comments')
    def comments_page():
        filter_status = request.args.get('filter', 'all')
        try:
            grouped_comments = extensions.db.get_comments_grouped_by_post(
                filter_status=filter_status if filter_status != 'all' else None,
                days=7
            )
        except Exception as e:
            print(f"❌ comments_page error: {e}")
            traceback.print_exc()
            grouped_comments = {}

        try:
            stats = extensions.db.get_comments_stats(days=7)
        except Exception:
            stats = {'total': 0, 'hidden': 0, 'shown': 0}

        try:
            failed_comments = extensions.db.get_failed_comments()
        except Exception:
            failed_comments = []

        return render_template('comments.html',
                               grouped_comments=grouped_comments,
                               stats=stats,
                               filter_status=filter_status,
                               last_scan=load_config().get('last_comment_scan', ''),
                               failed_comments=failed_comments)

    # ---- Manual scan ----

    @app.route('/scan-comments-now', methods=['POST'])
    def scan_comments_now():
        extensions.scan_in_progress = True

        def async_scan():
            try:
                print("\n" + "="*60 + "\n🔍 MANUAL COMMENT SCAN TRIGGERED\n" + "="*60)
                config = load_config()

                if not config.get('comments_filter_enabled', False):
                    print("⚠️  Comments filter disabled")
                    return
                if not config.get('openai_api_key'):
                    print("⚠️  Missing OpenAI API key")
                    return
                if not config.get('facebook_access_token'):
                    print("⚠️  Missing Facebook access token")
                    return

                from comments_scanner import create_hourly_job
                job = create_hourly_job(extensions.db, config)
                job()

                config = load_config()
                israel_tz = pytz.timezone('Asia/Jerusalem')
                config['last_comment_scan'] = datetime.now(israel_tz).strftime('%Y-%m-%d %H:%M:%S')
                save_config(config)

                check_comment_notification()
                print("✅ Manual scan completed\n" + "="*60)

            except Exception as e:
                print(f"❌ Scan error: {e}")
                traceback.print_exc()
            finally:
                extensions.scan_in_progress = False

        threading.Thread(target=async_scan, daemon=True).start()
        return '', 200

    @app.route('/scan-status', methods=['GET'])
    def scan_status():
        return jsonify({'scanning': extensions.scan_in_progress})

    # ---- Webhook ----

    @app.route('/webhook', methods=['GET'])
    def webhook_verify():
        config = load_config()
        verify_token = config.get('webhook_verify_token', '')
        mode = request.args.get('hub.mode')
        token = request.args.get('hub.verify_token')
        challenge = request.args.get('hub.challenge')
        if mode == 'subscribe' and token == verify_token and verify_token:
            print("✅ Webhook verified successfully")
            return challenge, 200
        print("❌ Webhook verification failed (token mismatch)")
        return 'Forbidden', 403

    @app.route('/webhook', methods=['POST'])
    def webhook_receive():
        try:
            data = request.get_json()
            print(f"\n📩 WEBHOOK RECEIVED:")
            print(f"   Raw data: {json.dumps(data, indent=2, default=str)[:500]}")

            if not data or data.get('object') != 'page':
                return 'OK', 200

            for entry in data.get('entry', []):
                for change in entry.get('changes', []):
                    field = change.get('field')
                    value = change.get('value', {})
                    item = value.get('item')
                    verb = change.get('value', {}).get('verb')

                    print(f"   Change: field={field}, item={item}, verb={verb}")

                    if field != 'feed' or item != 'comment' or verb != 'add':
                        continue

                    comment_id = value.get('comment_id')
                    post_id = value.get('post_id')
                    message = value.get('message', '')
                    sender_name = value.get('sender_name', 'Unknown')
                    sender_id = value.get('sender_id', '')
                    created_time = value.get('created_time', '')
                    parent_id = value.get('parent_id', '')

                    if not comment_id or not message:
                        continue

                    print(f"\n📩 New comment on post {post_id} from {sender_name}")
                    print(f"   Text: {message[:100]}...")

                    comment_data = {
                        'comment_id': comment_id,
                        'post_id': post_id,
                        'comment_text': message,
                        'author_name': sender_name,
                        'author_id': str(sender_id),
                        'created_at': str(created_time),
                        'is_hidden': False,
                        'parent_id': parent_id if parent_id != post_id else None
                    }

                    with extensions.webhook_queue_lock:
                        if not extensions.webhook_queue:
                            extensions.first_queued_time = datetime.now()
                        extensions.webhook_queue.append(comment_data)
                        queue_size = len(extensions.webhook_queue)

                    print(f"   📥 Queued for batch ({queue_size} in queue)")

            return 'OK', 200

        except Exception as e:
            print(f"❌ Webhook error: {e}")
            traceback.print_exc()
            return 'OK', 200

    # ---- Comment actions ----

    @app.route('/comment/<comment_id>/mark-political', methods=['POST'])
    def mark_comment_political(comment_id):
        return _mark_and_delete_comment(comment_id, 'political')

    @app.route('/comment/<comment_id>/mark-hate', methods=['POST'])
    def mark_comment_hate(comment_id):
        return _mark_and_delete_comment(comment_id, 'hate')

    @app.route('/comment/<comment_id>/mark-spam', methods=['POST'])
    def mark_comment_spam(comment_id):
        return _mark_and_delete_comment(comment_id, 'spam')

    @app.route('/comment/<comment_id>/mark-ok', methods=['POST'])
    def mark_comment_ok(comment_id):
        def async_action():
            try:
                fb = _get_fb_handler()
                if not fb:
                    return
                comment = _get_comment(comment_id)
                if not comment:
                    return

                if comment['status'] == 'hidden':
                    success = fb.unhide_comment(comment_id)
                else:
                    success = True

                if success:
                    ai_said = comment['filter_reason']
                    extensions.db.save_training_data(
                        comment_id=comment_id,
                        post_id=comment.get('post_id', ''),
                        comment_text=comment['comment_text'],
                        admin_label='ok',
                        ai_prediction=ai_said,
                        ai_explanation=comment.get('ai_explanation', '')
                    )
                    if ai_said == 'clean':
                        extensions.db.dismiss_comment(comment_id)
                        print(f"✅ Marked as OK (already clean): {comment_id}")
                        return
                    extensions.db.add_ai_example(
                        category=f'false_positive_{ai_said}',
                        comment_text=comment['comment_text'],
                        original_ai_prediction=ai_said,
                        explanation="False positive - actually acceptable"
                    )
                    extensions.db.dismiss_comment(comment_id)
                    print(f"✅ Marked as OK and unhidden: {comment_id}")

            except Exception as e:
                print(f"❌ Error marking OK: {e}")

        _run_async(async_action)
        return '', 200

    @app.route('/comment/<comment_id>/delete', methods=['POST'])
    def delete_comment_only(comment_id):
        """Delete from Facebook without saving to any training list."""
        def async_action():
            try:
                fb = _get_fb_handler()
                if not fb:
                    return
                fb.delete_comment(comment_id)
                extensions.db.dismiss_comment(comment_id)
                print(f"🗑️ Deleted comment (no training): {comment_id}")
            except Exception as e:
                print(f"❌ Error deleting comment: {e}")

        _run_async(async_action)
        return '', 200

    @app.route('/comment/<comment_id>/dismiss', methods=['POST'])
    def dismiss_comment(comment_id):
        try:
            success = extensions.db.dismiss_comment(comment_id)
            if success:
                return '', 200
            return '<div class="alert alert-danger">❌ תגובה לא נמצאה</div>', 404
        except Exception as e:
            print(f"Error dismissing comment: {e}")
            return f'<div class="alert alert-danger">❌ שגיאה: {str(e)}</div>', 500

    @app.route('/comment/<comment_id>/show', methods=['POST'])
    def show_comment(comment_id):
        def async_action():
            try:
                fb = _get_fb_handler()
                if not fb:
                    return
                success = fb.unhide_comment(comment_id)
                if success:
                    extensions.db.update_comment_status(comment_id, 'shown')
                    extensions.db.log_ai_feedback(comment_id, 'false_positive')
                    print(f"✅ Showed comment {comment_id}")
                else:
                    print(f"❌ Failed to show comment {comment_id}")
            except Exception as e:
                print(f"❌ Error showing comment {comment_id}: {e}")

        _run_async(async_action)
        return _PROCESSING_RESPONSE, 200

    @app.route('/comment/<comment_id>/hide', methods=['POST'])
    def hide_comment(comment_id):
        reason = request.form.get('reason')
        comment = _get_comment(comment_id)

        if not reason and comment and comment['filter_reason'] == 'clean':
            return '''
            <div>
                <p class="mb-2"><strong>למה להסתיר?</strong></p>
                <form hx-post="''' + url_for('hide_comment', comment_id=comment_id) + '''" hx-swap="outerHTML">
                    <div class="btn-group" role="group">
                        <button type="submit" name="reason" value="political" class="btn btn-sm btn-primary">🏛️ פוליטי</button>
                        <button type="submit" name="reason" value="hate" class="btn btn-sm btn-danger">⚠️ שנאה</button>
                        <button type="submit" name="reason" value="rude" class="btn btn-sm btn-warning">😠 גס רוח</button>
                    </div>
                </form>
            </div>
            ''', 200

        def async_action():
            try:
                fb = _get_fb_handler()
                if not fb:
                    return
                success = fb.hide_comment(comment_id)
                if success:
                    if comment and comment['filter_reason'] == 'clean' and reason:
                        extensions.db.log_ai_feedback(comment_id, 'missed', reason)
                    extensions.db.update_comment_status(comment_id, 'hidden')
                    print(f"✅ Hid comment {comment_id} (reason: {reason or 'already flagged'})")
                else:
                    print(f"❌ Failed to hide comment {comment_id}")
            except Exception as e:
                print(f"❌ Error hiding comment {comment_id}: {e}")

        _run_async(async_action)
        return _PROCESSING_RESPONSE, 200

    # ---- Batch operations ----

    @app.route('/comments/mark-all-ok', methods=['POST'])
    def mark_all_comments_ok():
        filter_status = request.form.get('filter_status')
        grouped = extensions.db.get_comments_grouped_by_post(
            filter_status=filter_status if filter_status != 'all' else None,
            days=7
        )
        comment_ids = [c['comment_id'] for p in grouped.values() for c in p['comments']]
        if not comment_ids:
            return redirect(url_for('comments_page', filter=filter_status or 'all'))
        _batch_mark_ok(comment_ids)
        flash(f'✅ {len(comment_ids)} תגובות סומנו כתקינות', 'success')
        return redirect(url_for('comments_page', filter=filter_status or 'all'))

    @app.route('/comments/post/<post_id>/mark-all-ok', methods=['POST'])
    def mark_post_comments_ok(post_id):
        filter_status = request.form.get('filter_status', 'all')
        grouped = extensions.db.get_comments_grouped_by_post(
            filter_status=filter_status if filter_status != 'all' else None,
            days=7
        )
        post_data = grouped.get(post_id)
        if not post_data:
            return redirect(url_for('comments_page', filter=filter_status))
        comment_ids = [c['comment_id'] for c in post_data['comments']]
        if not comment_ids:
            return redirect(url_for('comments_page', filter=filter_status))
        _batch_mark_ok(comment_ids)
        flash(f'✅ {len(comment_ids)} תגובות בפוסט סומנו כתקינות', 'success')
        return redirect(url_for('comments_page', filter=filter_status))

    # ---- AI Examples ----

    @app.route('/ai-examples')
    def ai_examples_page():
        examples = extensions.db.get_all_ai_examples_for_admin()
        categories = {
            'false_positive_political': {'title_he': 'False Positive - פוליטי', 'icon': '❌🏛️', 'description': 'תגובות שה-AI סימן בטעות כפוליטיות', 'examples': []},
            'false_positive_hate':      {'title_he': 'False Positive - שנאה',   'icon': '❌⚠️', 'description': 'תגובות שה-AI סימן בטעות כשנאה',    'examples': []},
            'false_positive_spam':      {'title_he': 'False Positive - ספאם',   'icon': '❌🚫', 'description': 'תגובות שה-AI סימן בטעות כספאם',    'examples': []},
            'correct_political':        {'title_he': 'נכון - פוליטי',           'icon': '✓🏛️', 'description': 'תגובות פוליטיות שה-AI זיהה נכון',  'examples': []},
            'correct_hate':             {'title_he': 'נכון - שנאה',             'icon': '✓⚠️', 'description': 'תגובות שנאה שה-AI זיהה נכון',      'examples': []},
            'correct_spam':             {'title_he': 'נכון - ספאם',             'icon': '✓🚫', 'description': 'ספאם שה-AI זיהה נכון',              'examples': []},
            'missed_political':         {'title_he': 'פספס - פוליטי',           'icon': '⚠️🏛️', 'description': 'תגובות פוליטיות שה-AI פספס',      'examples': []},
            'missed_hate':              {'title_he': 'פספס - שנאה',             'icon': '⚠️⚠️', 'description': 'תגובות שנאה שה-AI פספס',          'examples': []},
            'missed_spam':              {'title_he': 'פספס - ספאם',             'icon': '⚠️🚫', 'description': 'ספאם שה-AI פספס',                  'examples': []},
        }
        for example in examples:
            cat = example['category']
            if cat in categories:
                categories[cat]['examples'].append(example)
        stats = {
            'total': len(examples),
            'false_positives': sum(1 for e in examples if 'false_positive' in e['category']),
            'correct':         sum(1 for e in examples if 'correct'         in e['category']),
            'missed':          sum(1 for e in examples if 'missed'          in e['category']),
        }
        return render_template('ai_examples.html', categories=categories, stats=stats)

    @app.route('/ai-examples/<int:example_id>/delete', methods=['POST'])
    def delete_ai_example(example_id):
        success = extensions.db.delete_ai_example(example_id)
        if success:
            return '<div class="alert alert-success">✓ דוגמה נמחקה</div>', 200
        return '<div class="alert alert-danger">✗ שגיאה</div>', 404

    # ---- Dead-letter queue ----

    @app.route('/comment/<comment_id>/retry-failed', methods=['POST'])
    def retry_failed_comment(comment_id):
        """Move a failed comment back to the retry queue."""
        success = extensions.db.requeue_failed_comment(comment_id)
        if request.headers.get('HX-Request'):
            return '', 200
        flash('✅ התגובה הוחזרה לתור הניסיון' if success else '❌ תגובה לא נמצאה', 'success' if success else 'error')
        return redirect(url_for('comments_page'))

    @app.route('/comment/<comment_id>/dismiss-failed', methods=['POST'])
    def dismiss_failed_comment(comment_id):
        """Permanently remove a comment from the dead-letter queue."""
        extensions.db.dismiss_failed_comment(comment_id)
        if request.headers.get('HX-Request'):
            return '', 200
        return redirect(url_for('comments_page'))
