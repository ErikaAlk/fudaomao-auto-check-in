import fcntl
from datetime import datetime, timedelta, timezone
import hashlib
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
    import manager_notifications as notifications

    def failure(exc_type, exc, traceback):
        if isinstance(exc, attendance.StopRun):
            prefix = '配置已保存，' if op['action'] == 'trial' and saved else ''
            update(operation_id, 'failed', prefix + exc.message, {'state': exc.state, 'saved': saved})
        elif isinstance(exc, api.AuthError):
            update(operation_id, 'failed', '学校登录验证失效，请重新登录')
        elif isinstance(exc, store.TaskWindowError):
            update(operation_id, 'failed', str(exc))
        else:
            update(operation_id, 'failed', '学校操作失败，请核实账号、链接和网络（' + exc_type.__name__ + '）')
        print('学校操作终止：' + exc_type.__name__, file=sys.stderr)
        notifications.close_challenge(operation_id,'failed')

    sys.excepthook = failure
    saved = False

    def otp(session_id):
        expires = time.time() + 600
        request_id=hashlib.sha256((user['id']+'\0'+operation_id+'\0'+session_id).encode()).hexdigest()
        with store.connect() as conn:
            conn.execute("INSERT INTO operation_challenges VALUES(?,?,?,'',?,'creating')",(operation_id,user['id'],request_id,expires))
        challenge=notifications.otp_api('/api/requests',{'request_id':request_id,'subject':user['id'],'operation_id':operation_id})
        expires=min(expires,challenge['expires_at'])
        with store.connect() as conn:
            changed=conn.execute("UPDATE operation_challenges SET verification_url=?,expires=?,state='active' WHERE operation_id=? AND state='creating'",(challenge['verification_url'],expires,operation_id))
            if changed.rowcount!=1:
                raise api.AuthError('登录验证请求已结束')
            conn.execute('UPDATE operations SET expires=? WHERE id=?',(expires,operation_id))
            payload={'event_id':'lzu-attendance:sms:'+operation_id,'title':'签到需要短信验证',
                     'text':user['alias']+'：学校已发送短信，请在十分钟内打开专属临时链接填写验证码。登录验证完成后链接立即失效。\n\n'+challenge['verification_url']}
            notifications.queue_notice(conn,user['id'],payload,expires)
        update(operation_id,'waiting_sms','验证码已发送，请使用此用户专属临时链接填写')
        while time.time() < expires:
            with store.connect() as conn:
                row = conn.execute('SELECT state FROM operations WHERE id=?', (operation_id,)).fetchone()
                if row['state'] != 'waiting_sms':
                    raise api.AuthError('登录验证已终止')
            result=notifications.otp_api('/api/requests/'+request_id)
            if result['state']=='received':
                update(operation_id,'running','验证码已收到，正在核实学校登录')
                return result['code']
            time.sleep(2)
        raise api.AuthError('登录验证已过期')

    api.OTP_PROVIDER = otp
    with (store.user_dir(user['id']) / 'account.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        update(operation_id, 'running', '正在连接学校')
        fdm = api.FdmClient()
        cfg = attendance.read_config(require_location=op['action'] in ('run', 'trial'))
        if op['scheduled']:
            day = datetime.fromtimestamp(op['created'], timezone(timedelta(hours=8))).date().isoformat()
            with store.connect() as conn:
                variation = store.daily_variation(conn, job['id'], day)
            cfg['run_at'] = store.varied_start(job, day, variation).strftime('%H:%M:%S')
            cfg['location_variation'] = variation
        attendance.ensure_auth(fdm, cfg)
        with store.connect() as conn:
            challenge=conn.execute("SELECT 1 FROM operation_challenges WHERE operation_id=? AND state='active'",(operation_id,)).fetchone()
        if op['action']=='login' or challenge:
            fdm.interval_months()
        if challenge:
            notifications.close_challenge(operation_id,'verified')
        if op['action'] == 'login':
            with store.connect() as conn:
                conn.execute('UPDATE users SET ready=1 WHERE id=?', (user['id'],))
            update(operation_id, 'success', '学校登录验证通过')
        elif op['action'] == 'detect':
            detail = fdm.detail(job['instance_wid'], job['sign_wid'])
            name = detail.get('taskName')
            if not isinstance(name, str) or not name.strip():
                raise ValueError('学校没有返回任务名称，请检查签到链接')
            window = store.task_window(detail)
            # 任务绑定长期任务编号，每日查询时同时校验名称和编号。
            with store.connect() as conn:
                conn.execute('UPDATE jobs SET name=? WHERE id=?', (name, job['id']))
                store.save_window(conn, job['id'], window)
            result = {'name': name, 'sign_mode': str(detail.get('signMode', '')),
                      'photo_required': str(detail.get('isPhoto', '')) not in ('0', 'False', 'false'), **window}
            update(operation_id, 'success', '任务名称与起止时间已识别', result)
        else:
            if op['action'] == 'trial':
                detail = fdm.detail(job['instance_wid'], job['sign_wid'])
                window = store.task_window(detail)
                if str(detail.get('signMode', '')) != '0' or str(detail.get('isPhoto', '')) not in ('0', 'False', 'false'):
                    raise attendance.StopRun('unsupported_mode', '此任务要求拍照或其他签到方式，请在学校客户端完成')
                attendance.validate_location(detail, attendance.school_location(cfg['location']))
                with store.connect() as conn:
                    store.save_window(conn, job['id'], window)
                    conn.execute('UPDATE jobs SET saved=1,enabled=1 WHERE id=?', (job['id'],))
                    job = store.get_job(conn, job['id'])
                store.write_config(user, job)
                cfg = attendance.read_config()
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
