from concurrent.futures import ThreadPoolExecutor
from contextlib import closing,contextmanager
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import secrets
import tempfile
import time
from urllib.parse import urlsplit
import uuid

from cryptography.fernet import Fernet
from fastapi.testclient import TestClient


@contextmanager
def gateway():
    root=Path(__file__).resolve().parents[1]
    directory=root/'.verification'
    directory.mkdir(exist_ok=True)
    keys=('LZU_OTP_DATA','LZU_OTP_ORIGIN','LZU_OTP_TOKEN_HASH_FILE')
    previous={key:os.environ.get(key) for key in keys}
    with tempfile.TemporaryDirectory(dir=directory) as temporary:
        data=Path(temporary)
        token=secrets.token_urlsafe(32)
        (data/'data.key').write_bytes(Fernet.generate_key())
        (data/'token.sha256').write_text(hashlib.sha256(token.encode()).hexdigest(),'ascii')
        os.environ.update(LZU_OTP_DATA=temporary,LZU_OTP_ORIGIN='https://otp.example.test',LZU_OTP_TOKEN_HASH_FILE=str(data/'token.sha256'))
        spec=importlib.util.spec_from_file_location('otp_protocol_'+uuid.uuid4().hex,root/'t6/otp_gateway.py')
        module=importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        try:
            yield module,TestClient(module.app),{'Authorization':'Bearer '+token}
        finally:
            for key,value in previous.items():
                if value is None:
                    os.environ.pop(key,None)
                else:
                    os.environ[key]=value


def challenge(client,headers):
    request={'request_id':secrets.token_hex(32),'subject':uuid.uuid4().hex,'operation_id':uuid.uuid4().hex}
    response=client.post('/api/requests',headers=headers,json=request)
    assert response.status_code==200
    path=urlsplit(response.json()['verification_url']).path.removeprefix('/attendance-otp')
    return request,path


def test_public_gateway_protocol_waits_for_trusted_acknowledgement():
    with gateway() as (module,client,headers):
        request,path=challenge(client,headers)
        _,other=challenge(client,headers)
        assert other!=path
        assert client.get(path).status_code==200
        assert client.post('/api/requests',json=request).status_code==401
        code=str(secrets.randbelow(900000)+100000)
        assert client.post(path,json={'code':code},headers={'Origin':'https://wrong.example.test'}).status_code==403
        response=client.post(path,json={'code':code},headers={'Origin':module.ORIGIN})
        assert response.status_code==202
        assert client.post(path,json={'code':code},headers={'Origin':module.ORIGIN}).status_code==410
        public=client.get(path+'/status')
        assert public.status_code==200 and public.json()['state']=='submitted'
        assert code not in public.text
        assert client.get('/api/requests/'+request['request_id']).status_code==401
        poll=client.get('/api/requests/'+request['request_id'],headers=headers)
        assert poll.json()=={'state':'received','code':code}
        with closing(module.connect()) as conn:
            row=conn.execute('SELECT * FROM challenges WHERE request_id=?',(request['request_id'],)).fetchone()
            assert row['code']!=code
        assert client.post('/api/requests/'+request['request_id']+'/finish',json={'state':'verified'}).status_code==401
        completed=client.post('/api/requests/'+request['request_id']+'/finish',json={'state':'verified'},headers=headers)
        assert completed.json()['state']=='verified'
        assert client.get(path).status_code==410
        assert client.get(path+'/status').json()['state']=='verified'
        assert client.get('/api/requests/'+request['request_id'],headers=headers).status_code==410
        assert client.post('/api/requests',json=request,headers=headers).status_code==410
        with closing(module.connect()) as conn:
            assert conn.execute('SELECT code FROM challenges WHERE request_id=?',(request['request_id'],)).fetchone()[0] is None


def test_expiry_cancel_identity_and_invalid_code():
    with gateway() as (module,client,headers):
        request,path=challenge(client,headers)
        invalid=client.post(path,json={'code':'not-a-code'},headers={'Origin':module.ORIGIN})
        assert invalid.status_code==422 and 'not-a-code' not in invalid.text
        assert client.post('/api/requests',json={**request,'subject':uuid.uuid4().hex},headers=headers).status_code==409
        assert client.post('/api/requests/'+request['request_id']+'/finish',json={'state':'verified'},headers=headers).status_code==409
        with closing(module.connect()) as conn,conn:
            conn.execute('UPDATE challenges SET expires=? WHERE request_id=?',(time.time()-1,request['request_id']))
        assert client.get(path).status_code==410
        assert client.post('/api/requests',json=request,headers=headers).status_code==410
        cancelled=secrets.token_hex(32)
        assert client.post('/api/requests/'+cancelled+'/finish',json={'state':'cancelled'},headers=headers).json()['state']=='cancelled'
        assert client.post('/api/requests',json={'request_id':cancelled},headers=headers).status_code==410


def test_concurrent_public_submit_is_single_use():
    with gateway() as (module,client,headers):
        request,path=challenge(client,headers)
        def submit():
            return client.post(path,json={'code':'123456'},headers={'Origin':module.ORIGIN}).status_code
        with ThreadPoolExecutor(max_workers=2) as executor:
            statuses=list(executor.map(lambda _:submit(),range(2)))
        assert sorted(statuses)==[202,410]


def test_concurrent_terminal_acknowledgements_cannot_overwrite_each_other():
    with gateway() as (module,client,headers):
        for _ in range(12):
            request,path=challenge(client,headers)
            assert client.post(path,json={'code':'123456'},headers={'Origin':module.ORIGIN}).status_code==202
            def complete(state):
                return client.post('/api/requests/'+request['request_id']+'/finish',json={'state':state},headers=headers).json()['state']
            with ThreadPoolExecutor(max_workers=2) as executor:
                states=list(executor.map(complete,('verified','cancelled')))
            assert states[0]==states[1]
            assert client.get(path+'/status').json()['state']==states[0]
