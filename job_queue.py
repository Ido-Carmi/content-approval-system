"""
Persistent job queue backed by a real queue.Queue plus SQLite journal.

On enqueue:  journal row written first (status='pending'), then item placed in queue.
On startup:  reload_pending() re-enqueues any pending/processing rows from last run.
On success:  worker calls job.done()   → journal row marked 'done'.
On failure:  worker calls job.failed() → journal row marked 'failed'.
"""
import json
import queue
import logging
from dataclasses import dataclass, field
from typing import Any, Dict

log = logging.getLogger(__name__)


@dataclass
class Job:
    journal_id: int
    action: str
    payload: Dict[str, Any]
    retry_count: int = 0
    _queue: Any = field(default=None, repr=False)   # back-reference for requeue
    _db: Any = field(default=None, repr=False)

    def done(self):
        self._db.journal_mark_done(self.journal_id)
        log.debug("[queue] job %d (%s) done", self.journal_id, self.action)

    def failed(self, error: str):
        self._db.journal_mark_failed(self.journal_id, error, self.retry_count)
        log.warning("[queue] job %d (%s) permanently failed: %s", self.journal_id, self.action, error)

    def requeue_after_retry(self):
        """Increment retry count in journal and put back in the in-memory queue."""
        self.retry_count = self._db.journal_increment_retry(self.journal_id)
        self._queue.put(self)
        log.info("[queue] job %d (%s) requeued for retry #%d",
                 self.journal_id, self.action, self.retry_count)


class JobQueue:
    """
    Thread-safe, restart-safe job queue.

    Usage:
        q = JobQueue(db, queue_name='comments')
        q.reload_pending()          # call once at startup
        q.put({'action': 'hide', 'comment_id': '...'})
        job = q.get()               # blocks until available
    """

    def __init__(self, db, queue_name: str):
        self._db = db
        self._name = queue_name
        self._q: queue.Queue = queue.Queue()

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def put(self, payload: Dict[str, Any]) -> Job:
        """Persist job to journal then enqueue. Returns the Job object."""
        action = payload.get('action', 'unknown')
        journal_id = self._db.journal_enqueue(self._name, action, json.dumps(payload))
        job = self._make_job(journal_id, action, payload)
        self._q.put(job)
        log.info("[queue:%s] enqueued job %d action=%s", self._name, journal_id, action)
        return job

    def get(self, timeout: float = None) -> Job:
        """Block until a job is available. Raises queue.Empty on timeout."""
        return self._q.get(timeout=timeout)

    def task_done(self):
        self._q.task_done()

    def reload_pending(self):
        """Re-enqueue any pending/processing journal rows from before last restart."""
        rows = self._db.journal_get_pending(self._name)
        if rows:
            log.info("[queue:%s] reloading %d pending jobs from journal", self._name, len(rows))
        for row in rows:
            try:
                payload = json.loads(row['payload'])
                job = self._make_job(row['id'], row['action'], payload,
                                     retry_count=row['retry_count'])
                self._q.put(job)
                log.debug("[queue:%s] reloaded job %d action=%s retry=%d",
                          self._name, row['id'], row['action'], row['retry_count'])
            except Exception as exc:
                log.error("[queue:%s] failed to reload journal row %d: %s",
                          self._name, row['id'], exc)

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _make_job(self, journal_id: int, action: str, payload: Dict,
                  retry_count: int = 0) -> Job:
        return Job(
            journal_id=journal_id,
            action=action,
            payload=payload,
            retry_count=retry_count,
            _queue=self._q,
            _db=self._db,
        )
