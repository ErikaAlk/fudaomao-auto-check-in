from contextlib import closing
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

DATA=Path(os.environ['LZU_OTP_DATA'])
ORIGIN=os.environ['LZU_OTP_ORIGIN'].rstrip('/')
BASE=ORIGIN+'/attendance-otp'
TERMINAL=('verified','failed','cancelled','expired')
SCHEMA_LOCK=threading.Lock()
app=FastAPI(docs_url=None,redoc_url=None,openapi_url=None)


def connect():
    conn=sqlite3.connect(DATA/'otp.sqlite3',timeout=30)
    conn.row_factory=sqlite3.Row
    with SCHEMA_LOCK,conn:
        conn.execute("CREATE TABLE IF NOT EXISTS challenges(request_id TEXT PRIMARY KEY,capability TEXT UNIQUE NOT NULL,expires REAL NOT NULL,code TEXT,subject TEXT,operation_id TEXT,state TEXT NOT NULL DEFAULT 'waiting',submitted REAL)")
        columns={row['name'] for row in conn.execute('PRAGMA table_info(challenges)')}
        for name,declaration in (('subject','TEXT'),('operation_id','TEXT'),('state',"TEXT NOT NULL DEFAULT 'waiting'"),('submitted','REAL')):
            if name not in columns:
                conn.execute('ALTER TABLE challenges ADD COLUMN '+name+' '+declaration)
        conn.execute("UPDATE challenges SET state='submitted' WHERE state='waiting' AND code IS NOT NULL")
        conn.execute("UPDATE challenges SET state='expired',code=NULL WHERE expires<=? AND state IN ('waiting','submitted')",(time.time(),))
    return conn


def auth(authorization: str=Header('')):
    path=Path(os.environ.get('LZU_OTP_TOKEN_HASH_FILE','/etc/lzu-attendance-otp/token.sha256'))
    expected=path.read_text('ascii').strip()
    supplied=hashlib.sha256(authorization.removeprefix('Bearer ').encode()).hexdigest()
    if not authorization.startswith('Bearer ') or not secrets.compare_digest(expected,supplied):
        raise HTTPException(401,'访问凭据无效')


class Challenge(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    request_id: str=Field(pattern=r'^[a-f0-9]{64}$')
    subject: str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')
    operation_id: str|None=Field(default=None,pattern=r'^[a-f0-9]{32}$')


class Code(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    code: str=Field(pattern=r'^[0-9]{4,8}$')


class Completion(BaseModel):
    model_config=ConfigDict(extra='forbid',strict=True)
    state: str=Field(pattern=r'^(verified|failed|cancelled)$')


@app.exception_handler(RequestValidationError)
async def validation_error(request,exc):
    return JSONResponse({'detail':'输入格式无效，请检查验证码'},status_code=422)


@app.middleware('http')
async def privacy(request: Request,call_next):
    if int(request.headers.get('content-length','0'))>2048:
        return JSONResponse({'detail':'请求内容过长'},status_code=413)
    response=await call_next(request)
    response.headers.update({'Cache-Control':'no-store','Referrer-Policy':'no-referrer','X-Content-Type-Options':'nosniff','X-Frame-Options':'DENY',
        'Content-Security-Policy':"default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
    return response


@app.get('/health')
def health():
    with closing(connect()) as conn:
        conn.execute('SELECT 1').fetchone()
    return {'ok':True}


@app.post('/api/requests',dependencies=[Depends(auth)])
def create(payload: Challenge):
    with closing(connect()) as conn,conn:
        conn.execute("INSERT OR IGNORE INTO challenges(request_id,capability,expires,subject,operation_id,state) VALUES(?,?,?,?,?,'waiting')",
                     (payload.request_id,secrets.token_urlsafe(32),time.time()+600,payload.subject,payload.operation_id))
        row=conn.execute('SELECT * FROM challenges WHERE request_id=?',(payload.request_id,)).fetchone()
        if (row['subject'],row['operation_id'])!=(payload.subject,payload.operation_id):
            raise HTTPException(409,'验证码请求的用户或操作不匹配')
        if row['state'] in TERMINAL:
            raise HTTPException(410,'验证码填写入口已失效')
    return {'verification_url':BASE+'/verify/'+row['capability'],'expires_at':row['expires']}


@app.get('/api/requests/{request_id}',dependencies=[Depends(auth)])
def poll(request_id: str):
    with closing(connect()) as conn:
        row=conn.execute('SELECT * FROM challenges WHERE request_id=?',(request_id,)).fetchone()
    if row is None or row['state'] in TERMINAL:
        raise HTTPException(410,'验证码填写入口已失效')
    code=Fernet((DATA/'data.key').read_bytes()).decrypt(row['code'].encode()).decode() if row['code'] else None
    return {'state':'received' if code else 'waiting','code':code}


@app.post('/api/requests/{request_id}/finish',dependencies=[Depends(auth)])
def finish(request_id: str,payload: Completion):
    with closing(connect()) as conn,conn:
        conn.execute('BEGIN IMMEDIATE')
        row=conn.execute('SELECT * FROM challenges WHERE request_id=?',(request_id,)).fetchone()
        if row is None:
            if payload.state=='verified':
                raise HTTPException(404,'验证码请求不存在')
            conn.execute('INSERT INTO challenges(request_id,capability,expires,state) VALUES(?,?,?,?)',
                         (request_id,secrets.token_urlsafe(32),time.time(),payload.state))
            return {'state':payload.state}
        if row['state'] not in TERMINAL or (row['state']=='expired' and payload.state=='verified' and row['submitted'] is not None):
            if payload.state=='verified' and row['submitted'] is None:
                raise HTTPException(409,'此请求尚未提交验证码')
            conn.execute('UPDATE challenges SET state=?,code=NULL WHERE request_id=?',(payload.state,request_id))
            state=payload.state
        else:
            state=row['state']
    return {'state':state}


def public_row(conn,capability):
    row=conn.execute('SELECT state FROM challenges WHERE capability=?',(capability,)).fetchone()
    if row is None:
        raise HTTPException(410,'临时链接已失效')
    return row


@app.get('/verify/{capability}/status')
def public_status(capability: str):
    with closing(connect()) as conn:
        row=public_row(conn,capability)
    messages={'waiting':'等待填写学校短信验证码','submitted':'验证码已收到，正在核实学校登录','verified':'登录验证完成，此链接已失效',
              'failed':'登录验证失败，此链接已失效，请重新请求验证码','cancelled':'本次登录已结束，此链接已失效','expired':'临时链接已过期，请重新请求验证码'}
    return JSONResponse({'state':row['state'],'message':messages[row['state']]},status_code=410 if row['state'] in TERMINAL else 200)


@app.get('/verify/{capability}',response_class=HTMLResponse)
def form(capability: str):
    with closing(connect()) as conn:
        row=public_row(conn,capability)
    if row['state'] in TERMINAL:
        raise HTTPException(410,'临时链接已失效')
    return HTMLResponse('''<!doctype html><html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录验证</title>
<style>:root{color-scheme:light dark;--bg:#f0f1f2;--card:#fff;--text:#000000e6;--muted:#0000008a;--fill:#ebebeb;--primary:#0080ff}@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#ffffff1a;--text:#ffffffe6;--muted:#ffffff8a;--fill:#262626;--primary:#1a8cff}}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui}main{max-width:440px;margin:64px auto;padding:16px}section{background:var(--card);border-radius:20px;padding:24px}h1{font-size:24px;font-weight:600;margin:0 0 16px}p{color:var(--muted);font-size:14px;line-height:1.6}label{display:block;margin:24px 0 8px}input,button{width:100%;height:44px;border:0;border-radius:24px;font:inherit}input{background:var(--fill);color:var(--text);padding:0 16px}button{margin-top:16px;background:var(--primary);color:white;cursor:pointer}input:focus-visible,button:focus-visible{outline:2px solid var(--primary);outline-offset:3px}button:disabled{opacity:.5}#status{min-height:24px}</style></head>
<body><main><section><h1>登录验证</h1><p>填写学校发送的短信验证码，程序将继续对应账号的登录。</p><form id="form"><label for="code">短信验证码</label><input id="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{4,8}" minlength="4" maxlength="8" required><button id="submit" type="submit">提交验证码</button></form><p id="status" role="status" aria-live="polite"></p></section></main>
<script>const form=document.getElementById('form'),field=document.getElementById('code'),button=document.getElementById('submit'),status=document.getElementById('status');let stopped=false;
async function check(){const response=await fetch(location.pathname+'/status');const data=await response.json();if(!response.ok&&response.status!==410)throw new Error('查询验证状态失败');status.textContent=data.message||data.detail;if(data.state!=='waiting')form.hidden=true;if(response.status===410){stopped=true;field.value='';form.hidden=true}else setTimeout(check,2000)}
form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;const response=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:field.value})});const data=await response.json();status.textContent=data.message||data.detail;if(response.ok){field.value='';form.hidden=true}else if(!stopped)button.disabled=false});check();</script></body></html>''')


@app.post('/verify/{capability}')
def submit(capability: str,payload: Code,request: Request):
    if request.headers.get('origin')!=ORIGIN:
        raise HTTPException(403,'请从提醒中的链接打开填写页面')
    with closing(connect()) as conn,conn:
        code=Fernet((DATA/'data.key').read_bytes()).encrypt(payload.code.encode()).decode()
        cursor=conn.execute("UPDATE challenges SET code=?,state='submitted',submitted=? WHERE capability=? AND state='waiting' AND expires>?",
                            (code,time.time(),capability,time.time()))
        if cursor.rowcount!=1:
            raise HTTPException(410,'填写入口已过期或已提交')
    return JSONResponse({'state':'submitted','message':'验证码已收到，正在核实学校登录'},status_code=202)
