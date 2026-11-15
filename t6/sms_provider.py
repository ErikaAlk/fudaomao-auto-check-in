import hashlib
import json
from pathlib import Path
import time
import urllib.request

ROOT = Path(__file__).resolve().parent


def otp_api(path, body=None):
    cfg = json.loads((ROOT / 'config.json').read_text('utf-8'))
    token = (ROOT / 'otp.token').read_text('utf-8').strip()
    request = urllib.request.Request(cfg['otp_service_url'].rstrip('/') + path,
                                     data=None if body is None else json.dumps(body).encode('utf-8'),
                                     headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
    with urllib.request.urlopen(request, timeout=20) as response:
        return json.load(response)


def request_otp(session_id):
    import server_runner as runner
    request_id = hashlib.sha256(session_id.encode('utf-8')).hexdigest()
    challenge = otp_api('/api/requests', {'request_id': request_id})
    payload = {'event_id': 'lzu-attendance:otp:' + request_id, 'title': '晚查寝需要短信验证码',
               'text': '学校登录状态已失效，账密登录需要短信验证。请打开以下链接填写学校发送的验证码，T6 会继续登录并签到。入口有效期为 10 分钟。\n\n' + challenge['verification_url']}
    with runner.closing(runner.connect()) as conn, conn:
        conn.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?,NULL,?)',
                     (payload['event_id'], json.dumps(payload, ensure_ascii=False), 'pending', time.time()))
    runner.flush()
    print('已建立短信验证码填写入口，正在等待本人填写。', flush=True)
    while time.time() < challenge['expires_at']:
        result = otp_api('/api/requests/' + request_id)
        if result['state'] == 'received':
            print('已收到验证码，正在继续学校登录。', flush=True)
            return result['code']
        time.sleep(3)
    raise RuntimeError('短信验证码填写入口已过期')
