"""
All comment-related routes and webhook handling.
Call register(app) from app.py once after Flask app is created.
"""
import hashlib
import hmac as hmac_module
import json
import threading
import traceback
import pytz
from datetime import datetime

from flask import request, jsonify, redirect, url_for, render_template, flash

from config import load_config, save_config, get_auto_comment_texts
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
    row = cursor.fetchone()
    conn.close()
    return dict(row) if row else None


def _enqueue_fb_action(action: str, comment_id: str, prev_status: str, **extra):
    """Put an FB API action in the persistent serialised comment queue."""
    extensions.comment_queue.put({
        'action': action,
        'comment_id': comment_id,
        'prev_status': prev_status,
        **extra,
    })


def _enrich_webhook_comments(comments):
    """
    Fill in post_number and post_text for a batch of webhook comments.
    Looks up post_tracking first, then entries table as fallback.
    Modifies comments in-place.
    """
    post_ids = list({c['post_id'] for c in comments})
    post_meta = {}  # post_id -> {post_number, post_text}

    conn = extensions.db.get_connection()
    cursor = conn.cursor()
    for post_id in post_ids:
        cursor.execute(
            'SELECT post_number, post_text FROM post_tracking WHERE post_id = ?', (post_id,)
        )
        row = cursor.fetchone()
        if row and (row['post_number'] or row['post_text']):
            post_meta[post_id] = {
                'post_number': row['post_number'],
                'post_text': (row['post_text'] or '')[:200],
            }
        else:
            # Fallback: entries table
            cursor.execute(
                'SELECT post_number, text FROM entries WHERE facebook_post_id = ? LIMIT 1', (post_id,)
            )
            row = cursor.fetchone()
            if row:
                post_meta[post_id] = {
                    'post_number': row['post_number'],
                    'post_text': (row['text'] or '')[:200],
                }
    conn.close()

    for c in comments:
        meta = post_meta.get(c['post_id'], {})
        if 'post_number' not in c or c.get('post_number') is None:
            c['post_number'] = meta.get('post_number')
        if not c.get('post_text'):
            c['post_text'] = meta.get('post_text', '')


def _mark_and_delete_comment(comment_id, label):
    """Save training data + dismiss immediately; queue FB delete."""
    comment = _get_comment(comment_id)
    if not comment:
        return '', 200

    prev_status = comment['status']
    ai_said = comment['filter_reason']
    category = f'correct_{label}' if ai_said == label else f'missed_{label}'

    # DB ops are synchronous (fast) — training data recorded before FB action
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
    # Dismiss immediately so comment leaves the UI now (FB delete follows async)
    extensions.db.dismiss_comment(comment_id)

    # FB delete is fire-and-forget — comment is already dismissed; log on failure only
    _enqueue_fb_action('delete', comment_id, prev_status)
    print(f"[comment] queued delete for {comment_id[:30]}... label={label} → {category}")
    return '', 200


def _batch_mark_ok(comment_ids):
    """Dismiss + train immediately; queue FB unhide for each hidden comment."""
    queued = 0
    for cid in comment_ids:
        try:
            comment = _get_comment(cid)
            if not comment:
                continue
            ai_said = comment['filter_reason']
            if ai_said and ai_said != 'clean':
                extensions.db.add_ai_example(
                    category=f'false_positive_{ai_said}',
                    comment_text=comment['comment_text'],
                    original_ai_prediction=ai_said,
                    explanation="False positive - batch marked as OK"
                )
            extensions.db.dismiss_comment(cid)
            if comment['status'] == 'hidden':
                _enqueue_fb_action('unhide', cid, 'hidden')
                queued += 1
        except Exception as e:
            print(f"⚠️  _batch_mark_ok error for {cid}: {e}")
    print(f"[comment] batch mark-ok: {len(comment_ids)} dismissed, {queued} FB unhides queued")


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

    # Drop auto-comments — the page's own replies don't need AI filtering or storage.
    # Filter primarily by author_id (reliable), secondarily by text (belt-and-suspenders).
    page_id = str(config.get('facebook_page_id', ''))
    before = len(batch)
    if page_id:
        batch = [cd for cd in batch if cd.get('author_id', '') != page_id]
    auto_texts = get_auto_comment_texts(config)
    if auto_texts:
        batch = [cd for cd in batch if cd.get('comment_text', '').strip() not in auto_texts]
    skipped = before - len(batch)
    if skipped:
        print(f"   ⏭️  Skipped {skipped} auto-comment(s)")
    if not batch:
        return

    if not config.get('comments_filter_enabled') or not config.get('openai_api_key'):
        for cd in batch:
            cd['should_hide'] = False
            cd['filter_reason'] = 'clean'
            cd['ai_explanation'] = ''
            extensions.db.add_comment(cd)
        print(f"📝 Saved {len(batch)} comments without AI filter")
        return

    print(f"\n🤖 Batch processing {len(batch)} webhook comments with AI...")

    # Enrich each comment with post_number and post_text from DB before saving
    _enrich_webhook_comments(batch)

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
                # Enqueue to the serial comment worker (retries, journalled, non-blocking)
                _enqueue_fb_action('hide', cd['comment_id'], 'shown')
                print(f"   🚫 Queued hide: {cd['comment_id'][:30]}... ({cd['filter_reason']})")

        print(f"✅ Batch complete: {len(batch)} comments, {hidden_count} queued to hide")

        check_comment_notification()

        config = load_config()
        config['last_webhook_comment_time'] = datetime.now(pytz.timezone('Asia/Jerusalem')).isoformat()
        save_config(config)

    except Exception as e:
        print(f"❌ Batch processing error: {e}")
        traceback.print_exc()


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
        config = load_config()
        retention_days = config.get('comment_retention_days', 7)
        try:
            grouped_comments = extensions.db.get_comments_grouped_by_post(
                filter_status=filter_status if filter_status != 'all' else None,
                days=retention_days
            )
        except Exception as e:
            print(f"❌ comments_page error: {e}")
            traceback.print_exc()
            grouped_comments = {}

        try:
            stats = extensions.db.get_comments_stats(days=retention_days)
        except Exception:
            stats = {'total': 0, 'hidden': 0, 'shown': 0}

        return render_template('comments.html',
                               grouped_comments=grouped_comments,
                               stats=stats,
                               filter_status=filter_status,
                               last_scan=config.get('last_comment_scan', ''))

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
            # Load config once for the entire webhook request
            wh_config = load_config()
            app_secret = wh_config.get('facebook_app_secret', '')
            if app_secret:
                signature = request.headers.get('X-Hub-Signature-256', '')
                if not signature:
                    print("❌ Webhook POST rejected: missing X-Hub-Signature-256")
                    return 'Forbidden', 403
                raw_body = request.get_data()
                expected = 'sha256=' + hmac_module.new(
                    app_secret.encode('utf-8'), raw_body, hashlib.sha256
                ).hexdigest()
                if not hmac_module.compare_digest(signature, expected):
                    print("❌ Webhook POST rejected: invalid signature")
                    return 'Forbidden', 403
            else:
                print("⚠️  Webhook signature not verified (set facebook_app_secret in settings)")

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

                    # Detect external deletion of a scheduled post
                    if field == 'feed' and item in ('status', 'post') and verb in ('remove', 'delete'):
                        post_id = value.get('post_id') or value.get('story_id')
                        if post_id:
                            print(f"   🗑️  Post {post_id} deleted on Facebook — checking if it was scheduled")
                            try:
                                from reconciler import signal_reconciler
                                freed = extensions.db.return_post_to_pending_from_reconciler(post_id)
                                if freed is not None:
                                    print(f"   ↩️  Entry returned to pending (freed #{freed}) — signalling reconciler")
                                    signal_reconciler()
                                else:
                                    print(f"   ℹ️  Post {post_id} not found as scheduled entry — ignoring")
                            except Exception as _e:
                                print(f"   ❌ Error handling post deletion: {_e}")
                        continue

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

                    # Facebook webhook sends created_time as Unix timestamp (int).
                    # Convert to ISO so SQLite date comparisons work correctly.
                    try:
                        from datetime import timezone as _tz
                        created_at_iso = datetime.fromtimestamp(int(created_time), tz=_tz.utc).strftime('%Y-%m-%dT%H:%M:%S+00:00')
                    except (ValueError, TypeError):
                        created_at_iso = str(created_time)

                    # Check if the page is mentioned: comment starts with the page name
                    page_name = (wh_config.get('facebook_page_name') or '').strip()
                    mentions_page = 1 if (page_name and message.lower().startswith(page_name.lower())) else 0

                    comment_data = {
                        'comment_id': comment_id,
                        'post_id': post_id,
                        'comment_text': message,
                        'author_name': sender_name,
                        'author_id': str(sender_id),
                        'created_at': created_at_iso,
                        'is_hidden': False,
                        'parent_id': parent_id if parent_id != post_id else None,
                        'mentions_page': mentions_page,
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
        comment = _get_comment(comment_id)
        if not comment:
            return '', 200
        ai_said = comment['filter_reason']
        prev_status = comment['status']
        extensions.db.save_training_data(
            comment_id=comment_id,
            post_id=comment.get('post_id', ''),
            comment_text=comment['comment_text'],
            admin_label='ok',
            ai_prediction=ai_said,
            ai_explanation=comment.get('ai_explanation', '')
        )
        if ai_said and ai_said != 'clean':
            extensions.db.add_ai_example(
                category=f'false_positive_{ai_said}',
                comment_text=comment['comment_text'],
                original_ai_prediction=ai_said,
                explanation="False positive - actually acceptable"
            )
        extensions.db.dismiss_comment(comment_id)
        if prev_status == 'hidden':
            _enqueue_fb_action('unhide', comment_id, 'hidden')
            print(f"[comment] queued unhide for {comment_id[:30]}...")
        return '', 200

    @app.route('/comment/<comment_id>/delete', methods=['POST'])
    def delete_comment_only(comment_id):
        """Delete from Facebook without saving to any training list."""
        comment = _get_comment(comment_id)
        prev_status = comment['status'] if comment else 'shown'
        extensions.db.dismiss_comment(comment_id)
        _enqueue_fb_action('delete', comment_id, prev_status)
        print(f"[comment] queued delete (no training) for {comment_id[:30]}...")
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
        comment = _get_comment(comment_id)
        if not comment:
            return '', 404
        extensions.db.log_ai_feedback(comment_id, 'false_positive')
        # Optimistic: set shown now; worker reverts to 'hidden' on permanent failure
        extensions.db.update_comment_status(comment_id, 'shown')
        _enqueue_fb_action('unhide', comment_id, 'hidden')
        print(f"[comment] queued unhide for {comment_id[:30]}...")
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

        prev_status = comment['status'] if comment else 'shown'
        if comment and comment['filter_reason'] == 'clean' and reason:
            extensions.db.log_ai_feedback(comment_id, 'missed', reason)
        # Optimistic: set hidden now; worker reverts on permanent failure
        extensions.db.update_comment_status(comment_id, 'hidden')
        _enqueue_fb_action('hide', comment_id, prev_status)
        print(f"[comment] queued hide for {comment_id[:30]}... reason={reason}")
        return _PROCESSING_RESPONSE, 200

    # ---- Batch operations ----

    @app.route('/comments/mark-all-ok', methods=['POST'])
    def mark_all_comments_ok():
        filter_status = request.form.get('filter_status')
        retention_days = load_config().get('comment_retention_days', 7)
        grouped = extensions.db.get_comments_grouped_by_post(
            filter_status=filter_status if filter_status != 'all' else None,
            days=retention_days
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
        retention_days = load_config().get('comment_retention_days', 7)
        grouped = extensions.db.get_comments_grouped_by_post(
            filter_status=filter_status if filter_status != 'all' else None,
            days=retention_days
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

