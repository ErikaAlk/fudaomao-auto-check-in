from contextlib import closing
from datetime import datetime
from pathlib import Path
import time
import uuid
import subprocess

import attendance
import server_runner as runner

directory = Path(__file__).resolve().parent / '.verification'
directory.mkdir(mode=0o700, exist_ok=True)
runner.DATABASE = directory / ('rules-' + uuid.uuid4().hex + '.sqlite3')
stamp = attendance.now_bj()
start = runner.first_attempt(stamp)
assert start.hour == 21 and start.minute == 5 and start.utcoffset().total_seconds() == 28800
milestones = [number for number in range(1, 21) if runner.notice_payload('2026-11-10', number, 'target_not_found', '尚未找到指定任务')]
assert milestones == [5, 10, 20]
assert runner.notice_payload('2026-11-10', 20, 'verified_signed', '')['event_id'].endswith(':success')
with closing(runner.connect()) as conn:
    with conn:
        conn.execute("INSERT INTO days(day,attempts) VALUES('2026-11-10',20)")
        conn.execute("INSERT INTO attempts VALUES('2026-11-10',20,?,'running','')", (time.time(),))
    before = time.time()
    runner.finalize(conn, '2026-11-10', 20, 'target_not_found', '尚未找到指定任务')
    day = conn.execute('SELECT * FROM days').fetchone()
    assert day['attempts'] == 20 and day['done'] == 1
    assert before + 300 <= day['next_due'] <= time.time() + 300
    assert conn.execute('SELECT COUNT(*) FROM notices').fetchone()[0] == 1
    runner.finalize(conn, '2026-11-10', 20, 'target_not_found', '尚未找到指定任务')
    assert conn.execute('SELECT COUNT(*) FROM notices').fetchone()[0] == 1
print('定向验证通过：北京时间21:05、失败第5/10/20次通知、成功通知、20次上限、失败后300秒等待、通知记录去重。')
subprocess.run(['systemd-analyze', 'calendar', '*-*-* 21:05:00 Asia/Shanghai'], check=True)
