from contextlib import contextmanager
import json
import os
from pathlib import Path
import sqlite3
import uuid

ROOT = Path(os.environ.get('LZU_MANAGER_HOME', Path(__file__).resolve().parent))
DATA = ROOT / 'accounts'
DATABASE = ROOT / 'manager.sqlite3'


@contextmanager
def connect():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    try:
        with conn:
            yield conn
    finally:
        conn.close()


def initialize():
    DATA.mkdir(mode=0o700, parents=True, exist_ok=True)
    with connect() as conn:
        conn.executescript('''
            PRAGMA journal_mode=WAL;
            CREATE TABLE IF NOT EXISTS users (
                id TEXT PRIMARY KEY, alias TEXT NOT NULL, username TEXT UNIQUE NOT NULL,
                password TEXT NOT NULL, ready INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, name TEXT NOT NULL,
                sign_wid TEXT NOT NULL, instance_wid TEXT NOT NULL,
                run_at TEXT NOT NULL, deadline TEXT NOT NULL, start_date TEXT NOT NULL,
                weekdays TEXT NOT NULL, location TEXT NOT NULL,
                enabled INTEGER NOT NULL DEFAULT 0, saved INTEGER NOT NULL DEFAULT 0,
                UNIQUE(user_id, sign_wid)
            );
            CREATE TABLE IF NOT EXISTS operations (
                id TEXT PRIMARY KEY, user_id TEXT NOT NULL, job_id TEXT,
                action TEXT NOT NULL, state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
                created REAL NOT NULL, expires REAL NOT NULL, code TEXT, result TEXT,
                scheduled INTEGER NOT NULL DEFAULT 0
            );
            CREATE UNIQUE INDEX IF NOT EXISTS busy_user ON operations(user_id)
                WHERE state IN ('queued','running','waiting_sms','sms_received');
            CREATE TABLE IF NOT EXISTS days (
                job_id TEXT NOT NULL, day TEXT NOT NULL, attempts INTEGER NOT NULL,
                done INTEGER NOT NULL, next_due REAL NOT NULL,
                PRIMARY KEY(job_id, day)
            );
            CREATE TABLE IF NOT EXISTS notices (
                event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL
            );
        ''')


def identifier():
    return uuid.uuid4().hex


def user_dir(user_id):
    uuid.UUID(hex=user_id)
    return DATA / user_id


def job_dir(user_id, job_id):
    uuid.UUID(hex=job_id)
    return user_dir(user_id) / 'jobs' / job_id


def get_user(conn, user_id):
    row = conn.execute('SELECT * FROM users WHERE id=?', (user_id,)).fetchone()
    if row is None:
        raise ValueError('用户不存在')
    return dict(row)


def get_job(conn, job_id):
    row = conn.execute('SELECT * FROM jobs WHERE id=?', (job_id,)).fetchone()
    if row is None:
        raise ValueError('任务不存在')
    return dict(row)


def write_config(user, job=None):
    from attendance import atomic_json
    directory = job_dir(user['id'], job['id']) if job else user_dir(user['id'])
    directory.mkdir(mode=0o700, parents=True, exist_ok=True)
    config = {'username': user['username'], 'password_protected': user['password'],
              'target_task_name': job['name'] if job else '登录验证'}
    if job:
        config.update(run_at=job['run_at'], deadline=job['deadline'],
                      start_date=job['start_date'], location=json.loads(job['location']))
    atomic_json(directory / 'config.json', config)
    return directory
