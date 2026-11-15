import json
import http.client
from pathlib import Path
import time
from urllib.parse import urlsplit, urlunsplit
import urllib.request

import manager_store as store
from secure_store import protect, unprotect


def robin(path, payload=None, method=None):
    config = json.loads((store.ROOT / 'notify.json').read_text('utf-8'))
    parsed = urlsplit(config['url'])
    suffix = '/api/notifications'
    if not parsed.path.endswith(suffix):
        raise ValueError('Robin通知接口地址格式无效')
    url = urlunsplit((parsed.scheme, parsed.netloc, parsed.path[:-len(suffix)] + path, '', ''))
    token = (store.ROOT / 'notify.token').read_text('utf-8').strip()
    connection_type=http.client.HTTPSConnection if parsed.scheme=='https' else http.client.HTTPConnection
    connection=connection_type(parsed.hostname,parsed.port,timeout=25)
    connection.request(method or ('GET' if payload is None else 'POST'),urlsplit(url).path,
                       None if payload is None else json.dumps(payload).encode(),
                       {'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    response=connection.getresponse()
    result=json.loads(response.read())
    status=response.status
    connection.close()
    if status==409 and path=='/api/attendance/notifications':
        return {'status':'blocked'}
    if status!=200:
        raise RuntimeError('Robin通知接口请求失败，HTTP '+str(status))
    return result


def otp_api(path, payload=None):
    config = json.loads((store.ROOT / 'config.json').read_text('utf-8'))
    token = (store.ROOT / 'otp.token').read_text('utf-8').strip()
    request = urllib.request.Request(config['otp_service_url'].rstrip('/') + path,
                                     data=None if payload is None else json.dumps(payload).encode(),
                                     headers={'Authorization':'Bearer '+token,'Content-Type':'application/json'})
    with urllib.request.urlopen(request, timeout=25) as response:
        return json.load(response)


def initialize(conn):
    conn.executescript('''
        CREATE TABLE IF NOT EXISTS user_routes (
            user_id TEXT PRIMARY KEY, mode TEXT NOT NULL, bound INTEGER NOT NULL DEFAULT 0,
            version INTEGER, code TEXT, expires REAL, code_version INTEGER NOT NULL DEFAULT 0
        );
        CREATE TABLE IF NOT EXISTS notice_targets (
            event_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, mode TEXT NOT NULL,
            version INTEGER, expires REAL
        );
        CREATE TABLE IF NOT EXISTS operation_challenges (
            operation_id TEXT PRIMARY KEY, user_id TEXT NOT NULL, request_id TEXT UNIQUE NOT NULL,
            verification_url TEXT NOT NULL, expires REAL NOT NULL, state TEXT NOT NULL
        );
    ''')
    config = store.ROOT / 'config.json'
    owner = json.loads(config.read_text('utf-8'))['username'] if config.exists() else None
    with conn:
        conn.execute("INSERT OR IGNORE INTO user_routes(user_id,mode,bound) SELECT id,CASE WHEN username=? THEN 'owner' ELSE 'recipient' END,CASE WHEN username=? THEN 1 ELSE 0 END FROM users", (owner, owner))
        conn.execute('INSERT OR IGNORE INTO notice_targets SELECT n.event_id,j.user_id,r.mode,NULL,NULL FROM notices n JOIN jobs j ON n.event_id LIKE ?||j.id||? JOIN user_routes r ON r.user_id=j.user_id', ('lzu-attendance:', ':%'))
        conn.execute('INSERT OR IGNORE INTO notice_targets SELECT n.event_id,o.user_id,r.mode,NULL,o.expires FROM notices n JOIN operations o ON n.event_id=?||o.id JOIN user_routes r ON r.user_id=o.user_id', ('lzu-attendance:sms:',))


def route(conn, user_id):
    row = conn.execute('SELECT * FROM user_routes WHERE user_id=?', (user_id,)).fetchone()
    if row is None:
        raise ValueError('签到用户缺少明确的通知路由')
    return dict(row)


def queue_notice(conn, user_id, payload, expires=None):
    target = route(conn, user_id)
    if target['mode'] == 'off':
        return
    conn.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)', (payload['event_id'],json.dumps(payload,ensure_ascii=False),'pending'))
    conn.execute('INSERT OR IGNORE INTO notice_targets VALUES(?,?,?,?,?)',
                 (payload['event_id'],user_id,target['mode'],target['version'],expires))


def create_binding(user_id):
    with store.connect() as conn:
        current = route(conn,user_id)
        if current['mode']=='owner':
            raise ValueError('本人提醒使用既有绑定，无需重新绑定')
    result = robin('/api/attendance/bindings',{'subject':user_id})
    with store.connect() as conn:
        conn.execute("UPDATE user_routes SET mode='recipient',code=?,expires=?,code_version=? WHERE user_id=?",
                     (protect(result['code']),result['expires_at'],result['version'],user_id))
    return result


def check_binding(user_id):
    result = robin('/api/attendance/bindings/'+user_id)
    with store.connect() as conn:
        current=route(conn,user_id)
        redeemed=result['bound'] and result['version']>current['code_version']
        conn.execute('UPDATE user_routes SET bound=?,version=?,code=CASE WHEN ? THEN NULL ELSE code END WHERE user_id=?',
                     (int(result['bound']),result['version'],int(redeemed),user_id))
        result['pending']=bool(current['code'] and current['expires']>time.time() and not redeemed)
    return result


def public_routes(conn):
    result = {}
    for row in conn.execute('SELECT * FROM user_routes'):
        result[row['user_id']] = {'mode':row['mode'],'bound':bool(row['bound']), 'expires_at':row['expires'],
                                  'code':unprotect(row['code']) if row['code'] and row['expires']>time.time() else None}
    return result


def close_challenge(operation_id, outcome):
    with store.connect() as conn:
        row = conn.execute("SELECT * FROM operation_challenges WHERE operation_id=? AND state IN ('creating','active')",(operation_id,)).fetchone()
    if row is None:
        return
    result = otp_api('/api/requests/'+row['request_id']+'/finish',{'state':outcome})
    with store.connect() as conn:
        conn.execute('UPDATE operation_challenges SET state=? WHERE operation_id=?',(result['state'],operation_id))
        conn.execute("UPDATE notices SET status='expired' WHERE event_id=? AND status='pending'",('lzu-attendance:sms:'+operation_id,))


def revoke_finished():
    with store.connect() as conn:
        rows=conn.execute("SELECT c.operation_id FROM operation_challenges c JOIN operations o ON o.id=c.operation_id WHERE c.state IN ('creating','active') AND (o.state IN ('success','failed') OR c.expires<=?)",(time.time(),)).fetchall()
    for row in rows:
        close_challenge(row['operation_id'],'cancelled')


def deliver():
    revoke_finished()
    with store.connect() as conn:
        rows=conn.execute("SELECT n.*,t.user_id,t.mode,t.version,t.expires FROM notices n JOIN notice_targets t ON t.event_id=n.event_id WHERE n.status='pending'").fetchall()
    for row in rows:
        if row['expires'] is not None and row['expires']<=time.time():
            with store.connect() as conn:
                conn.execute("UPDATE notices SET status='expired' WHERE event_id=?",(row['event_id'],))
            continue
        payload=json.loads(row['payload'])
        if row['mode']=='owner':
            result=robin('/api/notifications',payload)
        elif row['mode']=='recipient':
            status=check_binding(row['user_id'])
            if not status['bound']:
                continue
            version=row['version']
            if version is None:
                version=status['version']
                with store.connect() as conn:
                    conn.execute('UPDATE notice_targets SET version=? WHERE event_id=? AND version IS NULL',(version,row['event_id']))
            result=robin('/api/attendance/notifications',{**payload,'subject':row['user_id'],'version':version})
        else:
            continue
        if result.get('status') not in ('sent','unknown','blocked'):
            raise ValueError('通知接口状态无效')
        with store.connect() as conn:
            conn.execute('UPDATE notices SET status=? WHERE event_id=?',(result['status'],row['event_id']))
