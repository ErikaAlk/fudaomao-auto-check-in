import argparse
import json
import os
import secrets
import shutil

import manager_store as store


def import_legacy():
    import attendance
    import lzu_api as api
    cfg = json.loads((store.ROOT / 'config.json').read_text('utf-8'))
    fdm = api.FdmClient()
    attendance.ensure_auth(fdm, cfg)
    category, task, begin, end = attendance.select_target(fdm.running_tasks(), attendance.now_bj())
    store.initialize()
    with store.connect() as conn:
        if conn.execute('SELECT 1 FROM users WHERE username=?', (cfg['username'],)).fetchone():
            raise ValueError('原有账号已经迁入')
        user_id, job_id = store.identifier(), store.identifier()
        conn.execute('INSERT INTO users VALUES(?,?,?,?,1)',
                     (user_id, '本人', cfg['username'], cfg['password_protected']))
        conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,1,1)',
                     (job_id, user_id, cfg['target_task_name'], str(task['signWid']), str(task['signInstanceWid']),
                      cfg['run_at'], cfg['deadline'], cfg['start_date'], json.dumps(list(range(1, 8))),
                      json.dumps(cfg['location'], ensure_ascii=False)))
        account = store.user_dir(user_id)
        account.mkdir(mode=0o700)
        for name in ('session.json', 'cookies.txt'):
            if (store.ROOT / name).exists():
                shutil.copy2(store.ROOT / name, account / name)
        job_root = store.job_dir(user_id, job_id)
        job_root.mkdir(mode=0o700, parents=True)
        if (store.ROOT / 'logs').exists():
            shutil.copytree(store.ROOT / 'logs', job_root / 'logs')
        # 旧调度状态通过 SQLite 原生读取，保留当天成功与尝试次数。
        if (store.ROOT / 'runner.sqlite3').exists():
            conn.execute('ATTACH DATABASE ? AS legacy', (str(store.ROOT / 'runner.sqlite3'),))
            conn.execute('INSERT INTO days SELECT ?,day,attempts,done,next_due FROM legacy.days', (job_id,))
        print('原有账号、学校会话、提交记录和调度状态已迁入。')


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('init', 'import-legacy', 'status'))
    args = parser.parse_args()
    store.initialize()
    if args.action == 'init':
        path = store.ROOT / 'manager.token'
        if not path.exists():
            path.write_text(secrets.token_urlsafe(48), 'ascii')
        path.chmod(0o600)
        print('管理数据目录与访问凭据已准备。')
    elif args.action == 'import-legacy':
        import_legacy()
    else:
        with store.connect() as conn:
            print('用户数量：', conn.execute('SELECT count(*) FROM users').fetchone()[0])
            print('任务数量：', conn.execute('SELECT count(*) FROM jobs').fetchone()[0])
            for row in conn.execute('SELECT action,state,message FROM operations ORDER BY created DESC LIMIT 5'):
                print(dict(row))


if __name__ == '__main__':
    main()
