"""Durable one-shot device jobs. Claimed jobs are never blindly replayed."""
import json
import logging
import os
from pathlib import Path
import queue
import sqlite3
import threading
import time
import uuid

log = logging.getLogger(__name__)


class AutomationStore:
    def __init__(self, path, overdue_policy='skip', now=None, report_success=False):
        if overdue_policy not in ('skip', 'run'):
            raise ValueError('AUTOMATION_OVERDUE_POLICY must be skip or run')
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.report_success = report_success
        with self.connect() as db:
            db.execute('PRAGMA journal_mode=WAL')
            db.execute('CREATE TABLE IF NOT EXISTS jobs (id TEXT PRIMARY KEY, payload TEXT NOT NULL, '
                       'due REAL, expires REAL, status TEXT NOT NULL, created REAL NOT NULL, updated REAL NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS reports (id INTEGER PRIMARY KEY, text TEXT NOT NULL, delivered INTEGER NOT NULL DEFAULT 0)')
        os.chmod(self.path, 0o600)
        stamp = time.time() if now is None else now
        with self.connect() as db:
            interrupted = db.execute("SELECT id, payload FROM jobs WHERE status='running'").fetchall()
            for job_id, payload in interrupted:
                self._finish(db, job_id, json.loads(payload), 'uncertain', stamp)
            if overdue_policy == 'skip':
                for job_id, payload in db.execute("SELECT id, payload FROM jobs WHERE status='pending' AND due <= ?", (stamp,)).fetchall():
                    self._finish(db, job_id, json.loads(payload), 'skipped', stamp)
        self.expire(stamp)

    def connect(self):
        # A short-lived connection per transaction keeps MQTT and scheduler threads independent.
        import contextlib
        @contextlib.contextmanager
        def connection():
            db = sqlite3.connect(self.path, timeout=10)
            try:
                with db:
                    yield db
            finally:
                db.close()
        return connection()

    def add(self, payload, due=None, expires=None):
        job_id = uuid.uuid4().hex
        now = time.time()
        with self.connect() as db:
            db.execute('INSERT INTO jobs VALUES (?, ?, ?, ?, ?, ?, ?)',
                       (job_id, json.dumps(payload), due, expires, 'pending', now, now))
        return job_id

    def pending(self):
        with self.connect() as db:
            rows = db.execute("SELECT id, payload, due, expires FROM jobs WHERE status='pending'").fetchall()
        return [dict(json.loads(p), id=i, due=d, expires=e) for i, p, d, e in rows]

    def claim(self, job_id, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute("SELECT payload, expires FROM jobs WHERE id=? AND status='pending'", (job_id,)).fetchone()
            if not row:
                return None
            payload = json.loads(row[0])
            if row[1] is not None and row[1] <= now:
                self._finish(db, job_id, payload, 'expired', now)
                return None
            db.execute("UPDATE jobs SET status='running', updated=? WHERE id=?", (now, job_id))
            return payload

    def _finish(self, db, job_id, payload, status, now):
        db.execute('UPDATE jobs SET status=?, updated=? WHERE id=?', (status, now, job_id))
        device = payload.get('device', 'device').replace('_', ' ')
        phrases = {
            'failed': f'Dobby could not confirm sending the scheduled command for {device}, sir.',
            'uncertain': f'Dobby was interrupted sending a command for {device}. Please check the device, sir.',
            'skipped': f'Dobby skipped an overdue command for {device} after restarting, sir.',
            'expired': f'Dobby cancelled the expired sensor request for {device}, sir.',
            'succeeded': f'Dobby sent the scheduled command for {device}, sir.',
        }
        if status != 'succeeded' or self.report_success:
            db.execute('INSERT INTO reports (text) VALUES (?)', (phrases[status],))

    def finish(self, job_id, payload, success):
        with self.connect() as db:
            self._finish(db, job_id, payload, 'succeeded' if success else 'failed', time.time())

    def expire(self, now=None):
        now = time.time() if now is None else now
        with self.connect() as db:
            rows = db.execute("SELECT id, payload FROM jobs WHERE status='pending' AND expires <= ?", (now,)).fetchall()
            for job_id, payload in rows:
                self._finish(db, job_id, json.loads(payload), 'expired', now)

    def reports(self):
        with self.connect() as db:
            return db.execute('SELECT id, text FROM reports WHERE delivered=0 ORDER BY id').fetchall()

    def acknowledge_report(self, report_id):
        with self.connect() as db:
            db.execute('UPDATE reports SET delivered=1 WHERE id=?', (report_id,))


class AutomationRunner:
    def __init__(self, store, publish):
        self.store = store
        self.publish = publish
        self.triggers = queue.Queue()
        self.stop_event = threading.Event()

    def trigger(self, job_id):
        self.triggers.put(job_id)

    def execute(self, job_id):
        payload = self.store.claim(job_id)
        if payload is None:
            return
        try:
            ok = self.publish(payload['device'], payload['action'], payload.get('value'))
        except Exception:
            log.exception('Scheduled device action failed')
            ok = False
        self.store.finish(job_id, payload, bool(ok))

    def tick(self):
        self.store.expire()
        now = time.time()
        for job in self.store.pending():
            if job['due'] is not None and job['due'] <= now:
                self.execute(job['id'])
        while True:
            try:
                job_id = self.triggers.get_nowait()
            except queue.Empty:
                break
            self.execute(job_id)

    def start(self):
        def run():
            while not self.stop_event.wait(0.5):
                try:
                    self.tick()
                except Exception:
                    log.exception('Automation scheduler error')
        threading.Thread(target=run, daemon=True, name='automation').start()
