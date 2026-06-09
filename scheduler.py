from datetime import datetime, timedelta, time
import pytz
import time as time_module
from typing import List, Dict, Optional
from config import load_config, save_config
import threading

class Scheduler:
    # Class-level lock for thread-safe scheduling
    _schedule_lock = threading.Lock()
    
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
    
    def load_posting_windows(self) -> List[time]:
        """Load posting windows from config"""
        config = load_config()
        windows_str = config.get('posting_windows', ['09:00', '14:00', '19:00'])
        
        windows = []
        for w in windows_str:
            hour, minute = map(int, w.split(':'))
            windows.append(time(hour, minute))
        
        return sorted(windows)
    
    # Default window sets for each posting frequency
    DEFAULT_TIERS = [
        {'max_posts': 5, 'windows': ['14:00']},
        {'max_posts': 10, 'windows': ['10:00', '19:00']},
        {'max_posts': 15, 'windows': ['09:00', '14:00', '19:00']},
        {'max_posts': 20, 'windows': ['09:00', '12:00', '16:00', '20:00']},
        {'max_posts': 999, 'windows': ['08:00', '11:00', '14:00', '17:00', '20:00']},
    ]
    
    def get_dynamic_tiers(self):
        """Get dynamic tiers from config, or use defaults"""
        config = load_config()
        tiers = config.get('dynamic_tiers', self.DEFAULT_TIERS)
        return sorted(tiers, key=lambda t: t['max_posts'])
    
    def get_windows_for_count(self, scheduled_count: int) -> list:
        """Get the posting windows for a given scheduled post count"""
        tiers = self.get_dynamic_tiers()
        for tier in tiers:
            if scheduled_count <= tier['max_posts']:
                return tier['windows']
        return tiers[-1]['windows'] if tiers else ['14:00']
    
    def update_dynamic_windows(self) -> bool:
        """
        Recalculate posting windows based on number of scheduled posts.
        Called after each new post is scheduled.
        Returns True if windows changed.
        On Facebook API failure, retries once after 60 seconds before giving up.
        """
        for attempt in range(2):
            try:
                scheduled_times = self.get_scheduled_times_from_facebook()
                scheduled_count = len(scheduled_times)

                new_windows = self.get_windows_for_count(scheduled_count)

                config = load_config()
                old_windows = config.get('posting_windows', [])
                config['posting_windows'] = new_windows
                save_config(config)

                posts_per_day = len(new_windows)
                changed = old_windows != new_windows
                if changed:
                    print(f"📊 Dynamic windows updated: {scheduled_count} scheduled → {posts_per_day}/day → {new_windows}")
                else:
                    print(f"📊 Dynamic windows unchanged: {scheduled_count} scheduled → {posts_per_day}/day")
                return changed

            except Exception as e:
                if attempt == 0:
                    print(f"⚠️  Error updating dynamic windows (attempt 1), retrying in 60s: {e}")
                    time_module.sleep(60)
                else:
                    print(f"⚠️  Error updating dynamic windows (attempt 2), giving up: {e}")
                    return False
    
    def is_shabbat(self, date: datetime.date) -> bool:
        """Check if a date is Friday or Saturday (Shabbat)"""
        return date.weekday() in [4, 5]
    
    def is_jewish_holiday(self, date: datetime.date) -> bool:
        """Check if a date is a Jewish holiday (non-work day)"""
        date_str = date.strftime('%Y-%m-%d')
        return date_str in self.jewish_holidays
    
    def should_skip_date(self, date: datetime.date) -> bool:
        """Check if a date should be skipped based on config"""
        config = load_config()
        skip_shabbat = config.get('skip_shabbat', True)
        skip_holidays = config.get('skip_jewish_holidays', True)
        
        if skip_shabbat and self.is_shabbat(date):
            return True
        
        if skip_holidays and self.is_jewish_holiday(date):
            return True
        
        return False
    
    def get_scheduled_times_from_facebook(self) -> List[datetime]:
        """Get all scheduled times from Facebook.
        Raises on API/auth failure so callers can distinguish 'no posts' from 'API down'.
        Skips individual posts with malformed timestamps."""
        fb_posts = self.fb.get_scheduled_posts()
        times = []
        for post in fb_posts:
            try:
                times.append(datetime.fromisoformat(post['scheduled_time']))
            except (ValueError, KeyError):
                print(f"⚠️  Skipping post with invalid scheduled_time: {post.get('id')}")
        return times
    
    def get_next_available_slot_local(self, taken_slot_isos, windows=None, append=False) -> datetime:
        """Pick the next available slot using only local data (no Facebook API call).
        taken_slot_isos: iterable of ISO strings of already-desired scheduled times.
        windows: optional list of datetime.time; defaults to the Facebook posting windows.
        append=True: place AFTER the latest scheduled post (don't back-fill earlier
        gaps such as today's already-published slots) — i.e. add to the end of the queue."""
        if windows is None:
            windows = self.load_posting_windows()
        now = datetime.now(self.timezone)

        occupied: set = set()
        posts_per_day: dict = {}
        occupied_dts: list = []
        parse_failures = []
        for iso in taken_slot_isos:
            try:
                dt = datetime.fromisoformat(str(iso))
                if dt.tzinfo is None:
                    dt = self.timezone.localize(dt)
                else:
                    dt = dt.astimezone(self.timezone)
                key = (dt.date(), dt.time().replace(second=0, microsecond=0))
                occupied.add(key)
                occupied_dts.append(dt)
                posts_per_day[dt.date()] = posts_per_day.get(dt.date(), 0) + 1
            except Exception as exc:
                parse_failures.append((iso, str(exc)))
        if parse_failures:
            print(f"[slot-pick] ⚠️  {len(parse_failures)} slot(s) failed to parse "
                  f"(dropped from occupied!): {parse_failures}")
        print(f"[slot-pick] occupied={sorted(str(k) for k in occupied)}")
        print(f"[slot-pick] posts_per_day={ {str(d): n for d, n in sorted(posts_per_day.items())} }")

        num_windows = len(windows)

        # Append mode: never place before the last scheduled post — add to the queue end.
        floor = max(occupied_dts) if (append and occupied_dts) else now
        if floor < now:
            floor = now
        if append and occupied_dts:
            print(f"[slot-pick] append mode — placing after {floor.isoformat()}")

        current_date = floor.date()

        if not self.should_skip_date(current_date):
            if posts_per_day.get(current_date, 0) < num_windows:
                for w in windows:
                    slot = self.timezone.localize(datetime.combine(current_date, w))
                    slot_key = (slot.date(), slot.time().replace(second=0, microsecond=0))
                    if slot > floor and slot_key not in occupied:
                        return slot

        for days in range(1, 365):
            check_date = current_date + timedelta(days=days)
            if self.should_skip_date(check_date):
                continue
            if posts_per_day.get(check_date, 0) >= num_windows:
                continue
            for w in windows:
                slot = self.timezone.localize(datetime.combine(check_date, w))
                slot_key = (slot.date(), slot.time().replace(second=0, microsecond=0))
                if slot > floor and slot_key not in occupied:
                    return slot

        fallback_days = 1
        while fallback_days < 365:
            fallback_date = current_date + timedelta(days=fallback_days)
            if not self.should_skip_date(fallback_date):
                return self.timezone.localize(datetime.combine(fallback_date, windows[0]))
            fallback_days += 1
        return self.timezone.localize(datetime.combine(current_date + timedelta(days=1), windows[0]))

    # ----- Instagram dynamic scheduling (separate windows/tiers) ---------------

    IG_DEFAULT_TIERS = [
        {'max_posts': 3,   'windows': ['18:00']},
        {'max_posts': 7,   'windows': ['12:00', '20:00']},
        {'max_posts': 999, 'windows': ['10:00', '15:00', '20:00']},
    ]

    def get_ig_dynamic_tiers(self):
        config = load_config()
        tiers = config.get('instagram_dynamic_tiers', self.IG_DEFAULT_TIERS)
        return sorted(tiers, key=lambda t: t['max_posts'])

    def get_ig_windows_for_count(self, scheduled_count: int) -> list:
        """Return the IG posting windows (as 'HH:MM' strings) for a given queue size."""
        tiers = self.get_ig_dynamic_tiers()
        for tier in tiers:
            if scheduled_count <= tier['max_posts']:
                return tier['windows']
        return tiers[-1]['windows'] if tiers else ['18:00']

    def load_ig_windows(self, scheduled_count: int) -> List[time]:
        """IG posting windows as datetime.time objects, sized by the current queue."""
        windows = []
        for w in self.get_ig_windows_for_count(scheduled_count):
            hour, minute = map(int, w.split(':'))
            windows.append(time(hour, minute))
        return sorted(windows)

    def get_next_available_ig_slot(self, taken_slot_isos) -> datetime:
        """Next free Instagram slot using IG dynamic-tier windows (queue-size aware).
        Appends to the end of the IG queue (doesn't back-fill today's used slots)."""
        scheduled_count = len([t for t in taken_slot_isos if t]) + 1
        windows = self.load_ig_windows(scheduled_count)
        return self.get_next_available_slot_local(taken_slot_isos, windows=windows, append=True)

    def reschedule_all_to_new_windows(self) -> int:
        """
        Redistribute all future scheduled posts across the new posting windows.
        Posts within 1 hour of publishing are left in place (too close to move safely).
        Posts are sorted by current time and reassigned sequentially so relative order
        is preserved — overflow from days with too many old-tier posts spills to the
        next available day.
        Returns the number of posts actually rescheduled.
        """
        entries = self.db.get_scheduled_entries()
        if not entries:
            return 0

        windows = self.load_posting_windows()
        now = datetime.now(self.timezone)
        cutoff = now + timedelta(hours=1)

        # Parse each entry; posts within cutoff stay put (their slots are locked)
        to_reschedule = []
        locked_slots: set = set()  # minute-resolution times that are already taken
        for entry in entries:
            st = entry.get('scheduled_time', '')
            if not st:
                continue
            try:
                scheduled_dt = datetime.fromisoformat(st)
                if scheduled_dt.tzinfo is None:
                    scheduled_dt = self.timezone.localize(scheduled_dt)
                else:
                    scheduled_dt = scheduled_dt.astimezone(self.timezone)
            except Exception:
                continue
            if scheduled_dt > cutoff:
                entry['_scheduled_dt'] = scheduled_dt
                to_reschedule.append(entry)
            else:
                # This post stays — its slot must not be double-booked
                locked_slots.add(scheduled_dt.replace(second=0, microsecond=0))

        if not to_reschedule:
            return 0

        # Deduplicate by facebook_post_id — keep the highest DB id when two entries
        # share the same FB post (can happen after DB corruption).  Trying to update
        # the same FB post twice causes the second call to fail with a stale ID error.
        seen_fb: dict = {}
        for entry in to_reschedule:
            fb_id = entry.get('facebook_post_id')
            if not fb_id:
                continue
            if fb_id not in seen_fb or entry['id'] > seen_fb[fb_id]['id']:
                seen_fb[fb_id] = entry
        dupes = len(to_reschedule) - len(seen_fb)
        if dupes:
            print(f"   ⚠️  Skipped {dupes} duplicate DB entries (same facebook_post_id)")
        to_reschedule = list(seen_fb.values())

        # Sort by post_number so midnight reschedule always corrects ordering
        to_reschedule.sort(key=lambda e: (e.get('post_number') or 999999))

        # Build an ordered list of available window slots starting from today,
        # excluding any slot already occupied by a near-cutoff post (locked_slots).
        available_slots = []
        day_cursor = now.date()
        days_scanned = 0
        needed = len(to_reschedule) + 5  # a few extra in case some reschedules fail
        while len(available_slots) < needed and days_scanned < 365:
            if not self.should_skip_date(day_cursor):
                for w in windows:
                    slot_dt = self.timezone.localize(datetime.combine(day_cursor, w))
                    if slot_dt > cutoff:
                        slot_key = slot_dt.replace(second=0, microsecond=0)
                        if slot_key not in locked_slots:
                            available_slots.append(slot_dt)
            day_cursor += timedelta(days=1)
            days_scanned += 1

        # Write desired scheduled_time for each post; reconciler pushes to FB via step3 sync.
        rescheduled = 0
        conn = self.db.get_connection()
        try:
            cursor = conn.cursor()
            for i, entry in enumerate(to_reschedule):
                if i >= len(available_slots):
                    print(f"   ⚠️  No more slots — {len(to_reschedule) - i} post(s) not rescheduled")
                    break
                new_time = available_slots[i]
                old_time = entry['_scheduled_dt']
                if abs((new_time - old_time).total_seconds()) < 60:
                    continue
                cursor.execute(
                    'UPDATE entries SET scheduled_time=? WHERE id=?',
                    (new_time.isoformat(), entry['id'])   # ISO+tz — one canonical format
                )
                rescheduled += 1
                print(f"   📅 #{entry.get('post_number')} {old_time.strftime('%d/%m %H:%M')} → {new_time.strftime('%d/%m %H:%M')}")
            conn.commit()
        finally:
            conn.close()

        return rescheduled
    
