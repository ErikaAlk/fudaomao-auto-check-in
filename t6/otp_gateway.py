from contextlib import closing
import hashlib
import os
from pathlib import Path
import secrets
import sqlite3
import time

from cryptography.fernet import Fernet
from fastapi import Depends, FastAPI, Header, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse
from pydantic import BaseModel, ConfigDict, Field

DATA = Path(os.environ['LZU_OTP_DATA'])
ORIGIN = os.environ['LZU_OTP_ORIGIN'].rstrip('/')
BASE = ORIGIN + '/attendance-otp'
app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)


def connect():
    conn = sqlite3.connect(DATA / 'otp.sqlite3', timeout=30)
    conn.row_factory = sqlite3.Row
    conn.execute('CREATE TABLE IF NOT EXISTS challenges (request_id TEXT PRIMARY KEY, capability TEXT UNIQUE NOT NULL, expires REAL NOT NULL, code TEXT)')
    with conn:
        conn.execute('UPDATE challenges SET code=NULL WHERE expires<=? AND code IS NOT NULL', (time.time(),))
    return conn


def auth(authorization: str = Header('')):
    expected = Path('/etc/lzu-attendance-otp/token.sha256').read_text('ascii').strip()
    supplied = hashlib.sha256(authorization.removeprefix('Bearer ').encode()).hexdigest()
    if not authorization.startswith('Bearer ') or not secrets.compare_digest(expected, supplied):
        raise HTTPException(401, '访问凭据无效')


class Challenge(BaseModel):
    model_config = ConfigDict(extra='forbid')
    request_id: str = Field(pattern=r'^[a-f0-9]{64}$')


class Code(BaseModel):
    model_config = ConfigDict(extra='forbid', strict=True)
    code: str = Field(pattern=r'^[0-9]{4,8}$')


@app.middleware('http')
async def privacy_headers(request: Request, call_next):
    if int(request.headers.get('content-length', '0')) > 2048:
        return JSONResponse({'detail': '请求内容过长'}, status_code=413)
    response = await call_next(request)
    response.headers.update({'Cache-Control': 'no-store', 'Referrer-Policy': 'no-referrer',
                             'X-Content-Type-Options': 'nosniff', 'X-Frame-Options': 'DENY',
                             'Content-Security-Policy': "default-src 'none'; style-src 'unsafe-inline'; script-src 'unsafe-inline'; connect-src 'self'; form-action 'self'; base-uri 'none'; frame-ancestors 'none'"})
    return response


@app.get('/health')
def health():
    with closing(connect()) as conn:
        conn.execute('SELECT 1').fetchone()
    return {'ok': True}


@app.post('/api/requests', dependencies=[Depends(auth)])
def create(payload: Challenge):
    with closing(connect()) as conn, conn:
        conn.execute('INSERT OR IGNORE INTO challenges VALUES(?,?,?,NULL)',
                     (payload.request_id, secrets.token_urlsafe(32), time.time() + 600))
        row = conn.execute('SELECT * FROM challenges WHERE request_id=?', (payload.request_id,)).fetchone()
        if row['expires'] <= time.time():
            raise HTTPException(410, '验证码填写入口已过期')
    return {'verification_url': BASE + '/verify/' + row['capability'], 'expires_at': row['expires']}


@app.get('/api/requests/{request_id}', dependencies=[Depends(auth)])
def poll(request_id: str):
    with closing(connect()) as conn:
        row = conn.execute('SELECT code FROM challenges WHERE request_id=? AND expires>?', (request_id, time.time())).fetchone()
    if row is None:
        raise HTTPException(410, '验证码填写入口已过期')
    code = Fernet((DATA / 'data.key').read_bytes()).decrypt(row['code'].encode()).decode() if row['code'] else None
    return {'state': 'received' if code else 'waiting', 'code': code}


@app.get('/verify/{capability}', response_class=HTMLResponse)
def form(capability: str):
    with closing(connect()) as conn:
        row = conn.execute('SELECT code FROM challenges WHERE capability=? AND expires>?', (capability, time.time())).fetchone()
    if row is None:
        raise HTTPException(410, '验证码填写入口已过期')
    if row['code']:
        return HTMLResponse('<!doctype html><html lang="zh-CN"><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>验证码已提交</title><p>验证码已提交，T6 正在继续登录。签到结果将通过 QQ 通知。</p></html>')
    return HTMLResponse('''<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>登录验证</title>
<style>:root{color-scheme:light dark;--bg:#f0f1f2;--card:#fff;--text:#000000e6;--muted:#0000008a;--fill:#ebebeb;--primary:#0080ff}@media(prefers-color-scheme:dark){:root{--bg:#000;--card:#ffffff1a;--text:#ffffffe6;--muted:#ffffff8a;--fill:#262626;--primary:#1a8cff}}*{box-sizing:border-box}body{margin:0;background:var(--bg);color:var(--text);font:16px system-ui}main{max-width:440px;margin:64px auto;padding:16px}section{background:var(--card);border-radius:20px;padding:24px}h1{font-size:24px;font-weight:600;margin:0 0 16px}p{color:var(--muted);font-size:14px;line-height:1.6}label{display:block;margin:24px 0 8px}input,button{width:100%;height:44px;border:0;border-radius:24px;font:inherit}input{background:var(--fill);color:var(--text);padding:0 16px}button{margin-top:16px;background:var(--primary);color:white;cursor:pointer}input:focus-visible,button:focus-visible{outline:2px solid var(--primary);outline-offset:3px}button:disabled{opacity:.5;cursor:wait}#status{min-height:24px}</style></head>
<body><main><section><h1>登录验证</h1><p>填写学校发送的短信验证码。</p><form id="form"><label for="code">短信验证码</label><input id="code" name="code" inputmode="numeric" autocomplete="one-time-code" pattern="[0-9]{4,8}" minlength="4" maxlength="8" required><button id="submit" type="submit">提交验证码</button></form><p id="status" role="status" aria-live="polite"></p></section></main>
<script>const form=document.getElementById('form'),field=document.getElementById('code'),button=document.getElementById('submit'),status=document.getElementById('status');form.addEventListener('submit',async event=>{event.preventDefault();button.disabled=true;status.textContent='正在提交验证码';const response=await fetch(location.pathname,{method:'POST',headers:{'Content-Type':'application/json'},body:JSON.stringify({code:field.value})});const result=await response.json();status.textContent=result.message||result.detail||'验证码提交失败';if(response.ok){field.value='';form.hidden=true}else{button.disabled=false}});</script></body></html>''')


@app.post('/verify/{capability}')
def submit(capability: str, payload: Code, request: Request):
    if request.headers.get('origin') != ORIGIN:
        raise HTTPException(403, '请从通知中的链接打开填写页面')
    with closing(connect()) as conn, conn:
        encrypted = Fernet((DATA / 'data.key').read_bytes()).encrypt(payload.code.encode()).decode()
        cursor = conn.execute('UPDATE challenges SET code=? WHERE capability=? AND code IS NULL AND expires>?',
                              (encrypted, capability, time.time()))
        if cursor.rowcount != 1:
            raise HTTPException(410, '填写入口已过期或已提交')
    return {'message': '验证码已提交，T6 将继续登录。签到结果会通过 QQ 通知。'}
