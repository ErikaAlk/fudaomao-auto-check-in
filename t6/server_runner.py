import argparse
from contextlib import closing
from datetime import datetime, timedelta
import fcntl
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import time
import urllib.request

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))
import attendance

DATABASE = ROOT / 'runner.sqlite3'
LIMIT = 20
INTERVAL = 300
MILESTONES = (5, 10, 20)


def connect():
    conn = sqlite3.connect(DATABASE, timeout=30)
    conn.row_factory = sqlite3.Row
    conn.executescript('''
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS days (
            day TEXT PRIMARY KEY, attempts INTEGER NOT NULL DEFAULT 0,
            done INTEGER NOT NULL DEFAULT 0, next_due REAL NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS attempts (
            day TEXT NOT NULL, number INTEGER NOT NULL, started REAL NOT NULL,
            state TEXT NOT NULL, message TEXT NOT NULL DEFAULT '',
            PRIMARY KEY(day, number)
        );
        CREATE TABLE IF NOT EXISTS notices (
            event_id TEXT PRIMARY KEY, payload TEXT NOT NULL, status TEXT NOT NULL,
            response TEXT, updated REAL NOT NULL
        );
    ''')
    return conn


def first_attempt(stamp):
    return stamp.replace(hour=21, minute=5, second=0, microsecond=0)


def notice_payload(day, number, state, message):
    if state in ('verified_signed', 'verified_already_signed'):
        return {'event_id': f'lzu-attendance:{day}:success', 'title': '晚查寝签到成功',
                'text': f'{day} 物理学院2026级本科生晚查寝：学校已确认签到。T6 本日第 {number} 次检查确认成功。'}
    if number not in MILESTONES:
        return None
    tail = '已达到最多 20 次，今天停止尝试。' if number == LIMIT else '5 分钟后继续尝试，最多 20 次。'
    return {'event_id': f'lzu-attendance:{day}:failure:{number}', 'title': '晚查寝签到失败',
            'text': f'{day} 物理学院2026级本科生晚查寝：第 {number} 次尝试未成功。{message}。{tail}'}


def finalize(conn, day, number, state, message):
    success = state in ('verified_signed', 'verified_already_signed')
    payload = notice_payload(day, number, state, message)
    with conn:
        conn.execute('UPDATE attempts SET state=?,message=? WHERE day=? AND number=?',
                     (state, message, day, number))
        conn.execute('UPDATE days SET done=?,next_due=? WHERE day=?',
                     (int(success or number >= LIMIT), time.time() + INTERVAL, day))
        if payload is not None:
            conn.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,NULL,?)',
                         (payload['event_id'], json.dumps(payload, ensure_ascii=False), 'pending', time.time()))


def school_result(started, check_only=False):
    command = '--check' if check_only else '--scheduled'
    result = subprocess.run([sys.executable, str(ROOT / 'attendance.py'), command], cwd=ROOT)
    latest = ROOT / 'logs/latest.json'
    report = json.loads(latest.read_text('utf-8')) if latest.exists() else {}
    if report.get('checked_at') and datetime.fromisoformat(report['checked_at']).timestamp() >= started:
        state, message = report['state'], report['message']
    else:
        state, message = 'process_failed', '检查进程未返回有效结果'
    if result.returncode != 0 and state in ('verified_signed', 'verified_already_signed'):
        raise RuntimeError('学校检查的退出结果与成功记录冲突')
    return state, message


def deliver(event_id):
    with closing(connect()) as conn:
        row = conn.execute('SELECT payload FROM notices WHERE event_id=?', (event_id,)).fetchone()
    config = json.loads((ROOT / 'notify.json').read_text('utf-8'))
    token = (ROOT / 'notify.token').read_text('utf-8').strip()
    request = urllib.request.Request(config['url'], data=row['payload'].encode('utf-8'),
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=45) as response:
        result = json.load(response)
    if result.get('status') not in ('sent', 'unknown'):
        raise RuntimeError('通知接口返回无法确认的状态')
    print(json.dumps({'ok': result.get('ok'), 'status': result['status'], 'duplicate': result.get('duplicate', False)}), flush=True)


def flush():
    with (ROOT / 'notify.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        with closing(connect()) as conn:
            rows = conn.execute("SELECT event_id FROM notices WHERE status IN ('pending','checking') ORDER BY updated").fetchall()
            for row in rows:
                with conn:
                    conn.execute("UPDATE notices SET status='checking',updated=? WHERE event_id=?", (time.time(), row['event_id']))
                result = subprocess.run([sys.executable, str(Path(__file__).resolve()), '--deliver', row['event_id']],
                                        cwd=ROOT, capture_output=True, text=True)
                if result.returncode != 0:
                    print('通知请求未确认，保留固定事件编号，等待再次向通知接口核实。', flush=True)
                    continue
                response = json.loads(result.stdout)
                with conn:
                    conn.execute('UPDATE notices SET status=?,response=?,updated=? WHERE event_id=?',
                                 (response['status'], json.dumps(response), time.time(), row['event_id']))
                print('通知状态：', row['event_id'], response['status'], flush=True)


def run_day(verify_now=False):
    with (ROOT / 'runner.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        with closing(connect()) as conn:
            stamp = attendance.now_bj()
            if stamp < first_attempt(stamp) and not verify_now:
                print('尚未到北京时间 21:05。', flush=True)
                return
            day = stamp.date().isoformat()
            with conn:
                conn.execute('INSERT OR IGNORE INTO days(day) VALUES(?)', (day,))
            while attendance.now_bj().date().isoformat() == day:
                current = conn.execute('SELECT * FROM days WHERE day=?', (day,)).fetchone()
                previous = conn.execute("SELECT * FROM attempts WHERE day=? AND state='running'", (day,)).fetchone()
                if previous is not None:
                    state, message = school_result(time.time(), check_only=True)
                    finalize(conn, day, previous['number'], state, message)
                    flush()
                    continue
                if current['done'] or current['attempts'] >= LIMIT:
                    flush()
                    return
                remaining = current['next_due'] - time.time()
                if remaining > 0:
                    time.sleep(min(remaining, 30))
                    continue
                number = current['attempts'] + 1
                started = time.time()
                with conn:
                    conn.execute('UPDATE days SET attempts=?,next_due=? WHERE day=?', (number, started + INTERVAL, day))
                    conn.execute("INSERT INTO attempts(day,number,started,state) VALUES(?,?,?,'running')", (day, number, started))
                state, message = school_result(started, check_only=verify_now)
                finalize(conn, day, number, state, message)
                print(f'北京时间 {attendance.now_bj().isoformat()}，本日第 {number} 次：{state}', flush=True)
                flush()
                if verify_now:
                    return


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--flush', action='store_true')
    group.add_argument('--deliver')
    group.add_argument('--verify-now', action='store_true')
    args = parser.parse_args()
    if args.deliver:
        deliver(args.deliver)
    elif args.flush:
        flush()
    else:
        run_day(args.verify_now)


if __name__ == '__main__':
    main()
