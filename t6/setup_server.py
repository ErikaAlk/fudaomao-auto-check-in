import argparse
import fcntl
import json
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parent


def main():
    os.umask(0o077)
    parser = argparse.ArgumentParser()
    parser.add_argument('action', choices=('configure', 'install', 'enable', 'status'))
    args = parser.parse_args()
    if args.action == 'configure':
        from secure_store import protect, unprotect
        data = json.load(sys.stdin)
        cfg = data['config']
        lock = (ROOT / 'runner.lock').open('a')
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        cfg['password_protected'] = protect(data['password'])
        if unprotect(cfg['password_protected']) != data['password']:
            raise RuntimeError('Linux 凭据加密检查失败')
        cfg.update(run_at='21:05', deadline='23:50')
        (ROOT / 'config.json').write_text(json.dumps(cfg, ensure_ascii=False, indent=2), 'utf-8')
        for name in ('cookies.txt', 'session.json'):
            if name in data:
                (ROOT / name).write_text(data[name], 'utf-8')
            elif (ROOT / name).exists():
                (ROOT / name).unlink()
        logs = ROOT / 'logs'
        logs.mkdir(exist_ok=True)
        for name, content in data.get('submissions', {}).items():
            if Path(name).name != name or not name.endswith('-submission.json'):
                raise ValueError('提交记录文件名无效')
            destination = logs / name
            if not destination.exists():
                destination.write_text(content, 'utf-8')
        (ROOT / 'notify.token').write_text(data['notify_token'], 'utf-8')
        (ROOT / 'otp.token').write_text(data['otp_token'], 'utf-8')
        (ROOT / 'notify.json').write_text(json.dumps({'url': data['notify_url']}), 'utf-8')
        for name in ('config.json', 'cookies.txt', 'session.json', 'notify.token', 'notify.json', 'otp.token'):
            path = ROOT / name
            if path.exists():
                path.chmod(0o600)
        print('Linux 凭据加密与配置保存通过；未输出凭据。')
        lock.close()
    elif args.action in ('install', 'enable'):
        units = Path.home() / '.config/systemd/user'
        units.mkdir(parents=True, exist_ok=True)
        for name in ('lzu-attendance.service', 'lzu-attendance.timer', 'lzu-attendance-notify.service', 'lzu-attendance-notify.timer'):
            (units / name).write_bytes((ROOT / name).read_bytes())
        subprocess.run(['sudo', '-n', 'loginctl', 'enable-linger', os.environ['USER']], check=True)
        subprocess.run(['systemctl', '--user', 'daemon-reload'], check=True)
        subprocess.run(['systemd-analyze', '--user', 'verify', str(units / 'lzu-attendance.service'), str(units / 'lzu-attendance-notify.service')], check=True)
        if args.action == 'enable':
            subprocess.run(['systemctl', '--user', 'enable', '--now', 'lzu-attendance.timer', 'lzu-attendance-notify.timer'], check=True)
    else:
        subprocess.run(['systemctl', '--user', 'list-timers', '--all', 'lzu-attendance*'], check=True)
        subprocess.run(['loginctl', 'show-user', os.environ['USER'], '-p', 'Linger'], check=True)
        import server_runner
        with server_runner.closing(server_runner.connect()) as conn:
            for row in conn.execute('SELECT * FROM days ORDER BY day DESC LIMIT 2'):
                print(dict(row))
            for row in conn.execute('SELECT event_id,status,response FROM notices ORDER BY updated DESC LIMIT 5'):
                print(dict(row))


if __name__ == '__main__':
    main()
