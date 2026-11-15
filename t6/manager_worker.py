import fcntl
import json
import os
from pathlib import Path
import sys
import time

sys.path.insert(0, str(Path(__file__).resolve().parent))
import manager_store as store


def update(operation_id, state, message='', result=None):
    with store.connect() as conn:
        conn.execute('UPDATE operations SET state=?,message=?,result=?,code=NULL WHERE id=?',
                     (state, message, json.dumps(result, ensure_ascii=False) if result else None, operation_id))


def main():
    operation_id = sys.argv[1]
    with store.connect() as conn:
        op = dict(conn.execute('SELECT * FROM operations WHERE id=?', (operation_id,)).fetchone())
        user = store.get_user(conn, op['user_id'])
        job = store.get_job(conn, op['job_id']) if op['job_id'] else None
    directory = store.job_dir(user['id'], job['id']) if job else store.user_dir(user['id'])
    os.environ.update(LZU_SESSION_DIR=str(store.user_dir(user['id'])), LZU_DATA_DIR=str(directory),
                      LZU_TARGET_NAME=job['name'] if job else '登录验证')
    if job:
        os.environ['LZU_SIGN_WID'] = job['sign_wid']
    store.write_config(user, job)
    import attendance
    import lzu_api as api
    from secure_store import unprotect

    def failure(exc_type, exc, traceback):
        if isinstance(exc, attendance.StopRun):
            prefix = '配置已保存，' if op['action'] == 'trial' and saved else ''
            update(operation_id, 'failed', prefix + exc.message, {'state': exc.state, 'saved': saved})
        elif isinstance(exc, api.AuthError):
            update(operation_id, 'failed', '学校登录验证失效，请重新登录')
        else:
            update(operation_id, 'failed', '学校操作失败，请核实账号、链接和网络（' + exc_type.__name__ + '）')
        print('学校操作终止：' + exc_type.__name__, file=sys.stderr)

    sys.excepthook = failure
    saved = False

    def otp(session_id):
        expires = time.time() + 600
        update(operation_id, 'waiting_sms', '验证码已发送，请在十分钟内填写')
        with store.connect() as conn:
            conn.execute('UPDATE operations SET expires=? WHERE id=?', (expires, operation_id))
            settings = store.ROOT / 'manager-settings.json'
            if settings.exists():
                url = json.loads(settings.read_text('utf-8'))['ha_url'].rstrip('/') + '/attendance'
                payload = {'event_id': 'lzu-attendance:sms:' + operation_id, 'title': '签到需要短信验证',
                           'text': user['alias'] + '：学校已发送短信，请在十分钟内打开 Home Assistant 的签到页面填写。' + url}
                conn.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)',
                             (payload['event_id'], json.dumps(payload, ensure_ascii=False), 'pending'))
        while time.time() < expires:
            with store.connect() as conn:
                row = conn.execute('SELECT state,code FROM operations WHERE id=?', (operation_id,)).fetchone()
                if row['state'] == 'sms_received':
                    value = unprotect(row['code'])
                    conn.execute("UPDATE operations SET state='running',code=NULL WHERE id=?", (operation_id,))
                    return value
                if row['state'] != 'waiting_sms':
                    raise api.AuthError('登录验证已终止')
            time.sleep(1)
        raise api.AuthError('登录验证已过期')

    api.OTP_PROVIDER = otp
    with (store.user_dir(user['id']) / 'account.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        update(operation_id, 'running', '正在连接学校')
        fdm = api.FdmClient()
        cfg = attendance.read_config(require_location=op['action'] in ('run', 'trial'))
        attendance.ensure_auth(fdm, cfg)
        if op['action'] == 'login':
            fdm.interval_months()
            with store.connect() as conn:
                conn.execute('UPDATE users SET ready=1 WHERE id=?', (user['id'],))
            update(operation_id, 'success', '学校登录验证通过')
        elif op['action'] == 'detect':
            detail = fdm.detail(job['instance_wid'], job['sign_wid'])
            name = detail.get('taskName')
            if not isinstance(name, str) or not name.strip():
                raise ValueError('学校没有返回任务名称，请检查签到链接')
            # 任务绑定长期任务编号，每日查询时同时校验名称和编号。
            with store.connect() as conn:
                conn.execute('UPDATE jobs SET name=? WHERE id=?', (name, job['id']))
            result = {'name': name, 'sign_mode': str(detail.get('signMode', '')),
                      'photo_required': str(detail.get('isPhoto', '')) not in ('0', 'False', 'false')}
            update(operation_id, 'success', '任务已识别', result)
        else:
            if op['action'] == 'trial':
                detail = fdm.detail(job['instance_wid'], job['sign_wid'])
                if str(detail.get('signMode', '')) != '0' or str(detail.get('isPhoto', '')) not in ('0', 'False', 'false'):
                    raise attendance.StopRun('unsupported_mode', '此任务要求拍照或其他签到方式，请在学校客户端完成')
                attendance.validate_location(detail, attendance.school_location(cfg['location']))
                with store.connect() as conn:
                    conn.execute('UPDATE jobs SET saved=1,enabled=1 WHERE id=?', (job['id'],))
                saved = True
            # 每日名称匹配完成后，还要确认学校返回的长期任务编号。
            category, task, begin, end = attendance.query_target(fdm, attendance.now_bj())
            if str(task['signWid']) != job['sign_wid']:
                raise ValueError('学校任务编号与保存的任务不一致')
            attendance.perform(fdm, cfg, attendance.now_bj(), check_only=op['action'] == 'check')
            result = json.loads((directory / 'logs/latest.json').read_text('utf-8'))
            update(operation_id, 'success', result['message'], result)
        fdm.save_cookies()


if __name__ == '__main__':
    os.umask(0o077)
    main()
