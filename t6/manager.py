import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, time as clock_time, timedelta, timezone
import fcntl
import json
import os
from pathlib import Path
import secrets
import signal
import subprocess
import sys
import time
from typing import Literal
from urllib.parse import parse_qs, urlsplit
import urllib.request

import eviltransform
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field

import manager_store as store
from secure_store import protect

TZ = timezone(timedelta(hours=8))
BUSY = ('queued', 'running', 'waiting_sms', 'sms_received')
PROCESSES = {}


def now():
    return datetime.now(TZ)


def in_schedule(job, stamp):
    return (stamp.date().isoformat() >= job['start_date']
            and stamp.isoweekday() in json.loads(job['weekdays'])
            and clock_time.fromisoformat(job['run_at']) <= stamp.time().replace(tzinfo=None)
            < clock_time.fromisoformat(job['deadline']))


def require_idle(conn, user_id):
    if conn.execute("SELECT 1 FROM operations WHERE user_id=? AND state IN ('queued','running','waiting_sms','sms_received')", (user_id,)).fetchone():
        raise HTTPException(409, '此用户正在执行操作，请等待完成')


def enqueue(conn, user_id, action, job_id=None, scheduled=False):
    require_idle(conn, user_id)
    op_id = store.identifier()
    conn.execute('INSERT INTO operations(id,user_id,job_id,action,state,created,expires,scheduled) VALUES(?,?,?,?,?,?,?,?)',
                 (op_id, user_id, job_id, action, 'queued', time.time(), time.time() + 600, int(scheduled)))
    return {'operation_id': op_id}


def finish_scheduled(conn, op):
    job = store.get_job(conn, op['job_id'])
    day = datetime.fromtimestamp(op['created'], TZ).date().isoformat()
    result = json.loads(op['result']) if op['result'] else {}
    success = result.get('state') in ('verified_signed', 'verified_already_signed')
    row = conn.execute('SELECT * FROM days WHERE job_id=? AND day=?', (job['id'], day)).fetchone()
    conn.execute('UPDATE days SET done=?,next_due=? WHERE job_id=? AND day=?',
                 (int(success or row['attempts'] >= 20), time.time() + 300, job['id'], day))
    if success or row['attempts'] in (5, 10, 20):
        user = store.get_user(conn, job['user_id'])
        event = f"lzu-attendance:{job['id']}:{day}:" + ('success' if success else f"failure:{row['attempts']}")
        payload = {'event_id': event, 'title': '签到成功' if success else '签到失败',
                   'text': f"{user['alias']} · {job['name']} · {day} · 第 {row['attempts']} 次：{op['message']}"}
        conn.execute('INSERT OR IGNORE INTO notices VALUES(?,?,?)',
                     (event, json.dumps(payload, ensure_ascii=False), 'pending'))
    conn.execute('UPDATE operations SET scheduled=0 WHERE id=?', (op['id'],))


def advance():
    for op_id, process in tuple(PROCESSES.items()):
        if process.poll() is None:
            continue
        with store.connect() as conn:
            op = conn.execute('SELECT * FROM operations WHERE id=?', (op_id,)).fetchone()
            if op['state'] in BUSY:
                conn.execute("UPDATE operations SET state='failed',message='执行进程已终止，请重新操作',code=NULL WHERE id=?", (op_id,))
        del PROCESSES[op_id]
    with store.connect() as conn:
        for op in conn.execute("SELECT * FROM operations WHERE scheduled=1 AND state IN ('success','failed')").fetchall():
            finish_scheduled(conn, op)
        conn.execute('UPDATE operations SET code=NULL WHERE expires<?', (time.time(),))
        stamp = now()
        for row in conn.execute('SELECT jobs.* FROM jobs JOIN users ON users.id=jobs.user_id JOIN job_windows ON job_windows.job_id=jobs.id WHERE jobs.enabled=1 AND jobs.saved=1 AND users.ready=1').fetchall():
            job = dict(row)
            if not in_schedule(job, stamp):
                continue
            if conn.execute("SELECT 1 FROM operations WHERE user_id=? AND state IN ('queued','running','waiting_sms','sms_received')", (job['user_id'],)).fetchone():
                continue
            day = stamp.date().isoformat()
            conn.execute('INSERT OR IGNORE INTO days VALUES(?,?,0,0,0)', (job['id'], day))
            daily = conn.execute('SELECT * FROM days WHERE job_id=? AND day=?', (job['id'], day)).fetchone()
            if daily['done'] or daily['attempts'] >= 20 or daily['next_due'] > time.time():
                continue
            enqueue(conn, job['user_id'], 'run', job['id'], True)
            conn.execute('UPDATE days SET attempts=attempts+1 WHERE job_id=? AND day=?', (job['id'], day))
        for op in conn.execute("SELECT * FROM operations WHERE state='queued' ORDER BY created").fetchall():
            if len(PROCESSES) >= 8:
                break
            conn.execute("UPDATE operations SET state='running',message='正在连接学校' WHERE id=?", (op['id'],))
            PROCESSES[op['id']] = subprocess.Popen([sys.executable, str(store.ROOT / 'manager_worker.py'), op['id']],
                                                  cwd=store.ROOT, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def deliver_notices():
    if not (store.ROOT / 'notify.json').exists():
        return
    with store.connect() as conn:
        rows = conn.execute("SELECT * FROM notices WHERE status='pending'").fetchall()
    config = json.loads((store.ROOT / 'notify.json').read_text('utf-8'))
    token = (store.ROOT / 'notify.token').read_text('utf-8').strip()
    for row in rows:
        request = urllib.request.Request(config['url'], data=row['payload'].encode(),
                                         headers={'Authorization': 'Bearer ' + token, 'Content-Type': 'application/json'})
        with urllib.request.urlopen(request, timeout=30) as response:
            result = json.load(response)
        if result.get('status') not in ('sent', 'unknown'):
            raise ValueError('通知接口状态无效')
        with store.connect() as conn:
            conn.execute('UPDATE notices SET status=? WHERE event_id=?', (result['status'], row['event_id']))


async def supervise():
    while True:
        advance()
        await asyncio.sleep(2)


async def notifications():
    while True:
        # 通知使用独立子进程；网络失败保留固定事件编号，下次核实。
        process = await asyncio.create_subprocess_exec(sys.executable, str(store.ROOT / 'manager.py'), '--notify',
                                                       stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        await process.wait()
        await asyncio.sleep(60)


@asynccontextmanager
async def lifespan(app):
    os.umask(0o077)
    lock = (store.ROOT / 'manager.lock').open('a')
    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    store.initialize()
    # 服务退出会终止所属子进程；提交标记独立持久化，重启后只能回查。
    with store.connect() as conn:
        conn.execute("UPDATE operations SET state='failed',message='服务已重新启动，请重新登录或检查任务',code=NULL WHERE state IN ('queued','running','waiting_sms','sms_received')")
    scheduler = asyncio.create_task(supervise())
    notifier = asyncio.create_task(notifications())
    def stop_on_failure(task):
        if not task.cancelled() and task.exception() is not None:
            os.kill(os.getpid(), signal.SIGTERM)
    scheduler.add_done_callback(stop_on_failure)
    notifier.add_done_callback(stop_on_failure)
    yield
    scheduler.cancel()
    notifier.cancel()
    for process in PROCESSES.values():
        if process.poll() is None:
            process.terminate()
    lock.close()


app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)


def auth(authorization: str = Header('')):
    token = (store.ROOT / 'manager.token').read_text('ascii').strip()
    if not secrets.compare_digest('Bearer ' + token, authorization):
        raise HTTPException(401, '管理服务访问凭据无效')


@app.exception_handler(RequestValidationError)
async def validation_error(request, exc):
    return JSONResponse({'detail': '输入内容无效，请检查必填项和格式'}, status_code=422)


@app.exception_handler(ValueError)
async def value_error(request, exc):
    return JSONResponse({'detail': '操作数据无效，请检查账号、任务与链接'}, status_code=422)


@app.middleware('http')
async def privacy(request: Request, call_next):
    if int(request.headers.get('content-length', '0')) > 16384:
        return JSONResponse({'detail': '请求内容过长'}, status_code=413)
    response = await call_next(request)
    response.headers.update({'Cache-Control': 'no-store', 'X-Content-Type-Options': 'nosniff'})
    return response


class Action(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    action: Literal['create_user', 'login', 'code', 'detect', 'refresh', 'locate', 'configure', 'trial', 'run', 'check', 'toggle', 'alias']
    user_id: str = ''
    job_id: str = ''
    operation_id: str = ''
    alias: str = Field(default='', max_length=80)
    username: str = Field(default='', max_length=80)
    password: str = Field(default='', max_length=512)
    code: str = Field(default='', max_length=8)
    link: str = Field(default='', max_length=4096)
    start_date: str = ''
    weekdays: list[int] = Field(default_factory=lambda: list(range(1, 8)))
    longitude: float = Field(default=0, ge=-180, le=180, allow_inf_nan=False)
    latitude: float = Field(default=0, ge=-90, le=90, allow_inf_nan=False)
    accuracy: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    located_at: float | None = Field(default=None, ge=0, allow_inf_nan=False)
    address: str = Field(default='', max_length=512)
    enabled: bool = False


@app.get('/api/state', dependencies=[Depends(auth)])
def state():
    with store.connect() as conn:
        users = [dict(row) for row in conn.execute('SELECT id,alias,username,ready FROM users ORDER BY rowid')]
        jobs = [dict(row) for row in conn.execute('SELECT jobs.*,job_windows.school_begin,job_windows.school_end FROM jobs LEFT JOIN job_windows ON job_windows.job_id=jobs.id ORDER BY jobs.rowid')]
        operations = [dict(row) for row in conn.execute("SELECT id,user_id,job_id,action,state,message,created,expires,result FROM operations WHERE id IN (SELECT id FROM operations ORDER BY created DESC LIMIT 100) OR state IN ('queued','running','waiting_sms','sms_received') ORDER BY created DESC")]
        days = [dict(row) for row in conn.execute('SELECT * FROM days WHERE day=?', (now().date().isoformat(),))]
    for job in jobs:
        job['location'] = json.loads(job['location'])
        job['weekdays'] = json.loads(job['weekdays'])
    for op in operations:
        op['result'] = json.loads(op['result']) if op['result'] else None
    return {'users': users, 'jobs': jobs, 'operations': operations, 'days': days, 'timezone': 'Asia/Shanghai'}


def task_ids(link):
    parsed = urlsplit(link)
    if parsed.scheme != 'https' or parsed.hostname != 'fdm.lzu.edu.cn' or parsed.username or parsed.password or parsed.port not in (None, 443):
        raise HTTPException(422, '请粘贴兰州大学辅导猫 HTTPS 签到链接')
    params = parse_qs(parsed.query)
    fragment = parse_qs(parsed.fragment.partition('?')[2])
    for key, values in fragment.items():
        if key in params and params[key] != values:
            raise HTTPException(422, '链接包含冲突的任务编号')
        params[key] = values
    result = []
    for key in ('signWid', 'signInstanceWid'):
        values = params.get(key, [])
        if len(values) != 1 or not values[0].isascii() or not values[0].isdigit() or len(values[0]) > 30:
            raise HTTPException(422, '链接缺少有效的任务编号')
        result.append(values[0])
    return result


@app.post('/api/action', dependencies=[Depends(auth)])
def action(payload: Action):
    if payload.action == 'locate':
        if payload.accuracy is None or payload.located_at is None:
            raise HTTPException(422, '定位结果缺少精度或采集时间，请重新获取当前位置')
        latitude, longitude = eviltransform.wgs2gcj(payload.latitude, payload.longitude)
        longitude, latitude = round(longitude, 6), round(latitude, 6)
        return {'location': {'longitude': longitude, 'latitude': latitude, 'coordinate_system': 'GCJ-02',
                             'address': f'当前位置（经度{longitude:.6f}，纬度{latitude:.6f}）',
                             'accuracy': payload.accuracy, 'located_at': payload.located_at}}
    with store.connect() as conn:
        if payload.action == 'create_user':
            if not payload.alias.strip() or not payload.username.strip() or not payload.password:
                raise HTTPException(422, '请填写别名、账号名称和密码')
            if conn.execute('SELECT 1 FROM users WHERE username=?', (payload.username.strip(),)).fetchone():
                raise HTTPException(409, '此账号已经存在，请选择已有用户')
            user_id = store.identifier()
            conn.execute('INSERT INTO users VALUES(?,?,?,?,0)',
                         (user_id, payload.alias.strip(), payload.username.strip(), protect(payload.password)))
            store.user_dir(user_id).mkdir(mode=0o700, parents=True)
            return {'user_id': user_id, **enqueue(conn, user_id, 'login')}
        if payload.action == 'code':
            if not payload.code.isascii() or not payload.code.isdigit() or not 4 <= len(payload.code) <= 8:
                raise HTTPException(422, '请填写四至八位数字验证码')
            changed = conn.execute("UPDATE operations SET state='sms_received',code=? WHERE id=? AND state='waiting_sms' AND expires>? AND code IS NULL",
                                   (protect(payload.code), payload.operation_id, time.time()))
            if changed.rowcount != 1:
                raise HTTPException(409, '验证码填写入口已经过期或已提交')
            return {'ok': True}
        if payload.action in ('refresh', 'configure', 'trial', 'run', 'check', 'toggle'):
            job = store.get_job(conn, payload.job_id)
            user = store.get_user(conn, job['user_id'])
        else:
            user = store.get_user(conn, payload.user_id)
            job = None
        require_idle(conn, user['id'])
        if payload.action == 'alias':
            if not payload.alias.strip():
                raise HTTPException(422, '请填写别名')
            conn.execute('UPDATE users SET alias=? WHERE id=?', (payload.alias.strip(), user['id']))
            return {'ok': True}
        if payload.action == 'login':
            conn.execute('UPDATE users SET ready=0 WHERE id=?', (user['id'],))
            if payload.password:
                conn.execute('UPDATE users SET password=?,ready=0 WHERE id=?', (protect(payload.password), user['id']))
                from secure_store import discard_authentication
                discard_authentication(store.user_dir(user['id']))
            return enqueue(conn, user['id'], 'login')
        if not user['ready']:
            raise HTTPException(409, '请先完成学校登录验证')
        if payload.action == 'refresh':
            return enqueue(conn, user['id'], 'detect', job['id'])
        if payload.action == 'detect':
            sign_wid, instance_wid = task_ids(payload.link)
            existing = conn.execute('SELECT * FROM jobs WHERE user_id=? AND sign_wid=?', (user['id'], sign_wid)).fetchone()
            if existing:
                conn.execute('UPDATE jobs SET instance_wid=? WHERE id=?', (instance_wid, existing['id']))
                return {'job_id': existing['id'], **enqueue(conn, user['id'], 'detect', existing['id'])}
            job_id = store.identifier()
            conn.execute('INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?,?,?,0,0)',
                         (job_id, user['id'], '正在识别', sign_wid, instance_wid, '', '',
                          now().date().isoformat(), json.dumps(list(range(1, 8))), '{}'))
            return {'job_id': job_id, **enqueue(conn, user['id'], 'detect', job_id)}
        if payload.action == 'configure':
            window = conn.execute('SELECT * FROM job_windows WHERE job_id=?', (job['id'],)).fetchone()
            if window is None:
                raise HTTPException(409, '请先读取学校任务的起止时间')
            if datetime.strptime(payload.start_date, '%Y-%m-%d').date().isoformat() != payload.start_date or payload.start_date < window['school_begin'][:10]:
                raise HTTPException(422, '开始日期不能早于学校任务日期')
            if not payload.weekdays or any(day not in range(1, 8) for day in payload.weekdays):
                raise HTTPException(422, '请选择有效的执行星期')
            if not (-180 <= payload.longitude <= 180 and -90 <= payload.latitude <= 90 and payload.address.strip()):
                raise HTTPException(422, '请填写实际地址及 GCJ-02 经纬度')
            location = {'longitude': payload.longitude, 'latitude': payload.latitude,
                        'address': payload.address.strip(), 'coordinate_system': 'GCJ-02'}
            if payload.accuracy is not None and payload.located_at is not None:
                location.update(accuracy=payload.accuracy, located_at=payload.located_at, source='browser')
            conn.execute('UPDATE jobs SET start_date=?,weekdays=?,location=? WHERE id=?',
                         (payload.start_date, json.dumps(sorted(set(payload.weekdays))),
                          json.dumps(location, ensure_ascii=False), job['id']))
            return {'ok': True}
        if payload.action == 'toggle':
            if payload.enabled and not job['saved']:
                raise HTTPException(409, '请先尝试签到并保存任务')
            conn.execute('UPDATE jobs SET enabled=? WHERE id=?', (int(payload.enabled), job['id']))
            return {'ok': True}
        if job['name'] == '正在识别':
            raise HTTPException(409, '请先完成任务识别')
        return enqueue(conn, user['id'], payload.action, job['id'])


if __name__ == '__main__':
    if sys.argv[1:] == ['--notify']:
        deliver_notices()
