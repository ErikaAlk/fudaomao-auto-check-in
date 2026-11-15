"""LZU authentication and attendance API from LYCaikano/fudaomao-auto-check-in.
Upstream commit: 35e4a809f089337a2c9fbcb97e90aba66e0db4b8.
Business task selection and submission are implemented in attendance.py.
"""
import http.cookiejar
import json
import os
import re
import ssl
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

BASE_DIR = os.environ.get('LZU_SESSION_DIR', os.path.dirname(os.path.abspath(__file__)))
CONFIG_PATH = os.path.join(BASE_DIR, "config.json")
SESSION_PATH = os.path.join(BASE_DIR, "session.json")
COOKIES_PATH = os.path.join(BASE_DIR, "cookies.txt")
PIC_DIR = os.path.join(BASE_DIR, "pic")

GATEWAY = "https://gateway.lzu.edu.cn:9000"
FDM_BASE = "https://fdm.lzu.edu.cn/wec-counselor-attendance-apps"
FDM_ENTRY = FDM_BASE + "/student/mobileStudent/index.html"
UA = "Mozilla/5.0 (Linux; Android 15; wv) MicroMessenger/8.0.47"

# 兰大 App 硬编码 AES 密钥（key = IV），见 kcb-app util/LzuCrypto.kt
AES_KEY = b"QSfg26hg433BV42a"

OTP_PROVIDER = None
CTX = ssl.create_default_context()

PIC_EXTS = (".jpg", ".jpeg", ".png", ".webp", ".bmp")


def log(msg):
    print(time.strftime("[%H:%M:%S]"), msg, flush=True)


class AuthError(Exception):
    """会话/登录态失效（可重跑认证链或重登录恢复）"""


class ApiError(Exception):
    """业务/网络错误"""


# ---------- AES（AES/CBC/NoPadding，零填充，hex 小写） ----------

def aes_encrypt_hex(plain: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    raw = plain.encode("utf-8")
    if len(raw) % 16:
        raw += b"\x00" * (16 - len(raw) % 16)
    enc = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_KEY)).encryptor()
    return (enc.update(raw) + enc.finalize()).hex()


def aes_decrypt_hex(hexstr: str) -> str:
    from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
    dec = Cipher(algorithms.AES(AES_KEY), modes.CBC(AES_KEY)).decryptor()
    return (dec.update(bytes.fromhex(hexstr)) + dec.finalize()).rstrip(b"\x00").decode("utf-8", "replace")


# ---------- 配置与持久化 ----------

class SessionStore:
    """持久化 device_id + login_token/gateway_token（防每次都要短信验证码）"""

    def __init__(self):
        self.device_id = ""
        self.login_token = ""
        self.gateway_token = ""
        if os.path.exists(SESSION_PATH):
            try:
                with open(SESSION_PATH, encoding="utf-8") as f:
                    d = json.load(f)
                self.device_id = d.get("device_id", "")
                self.login_token = d.get("login_token", "")
                self.gateway_token = d.get("gateway_token", "")
            except Exception as e:
                log("session.json 读取失败（%s），将重新生成" % e)
        if not self.device_id:
            # 与 App 一致的设备标识形态；固定不变可降低二次认证概率
            self.device_id = str(uuid.uuid4())
            self.save()

    @property
    def has_tokens(self):
        return bool(self.login_token and self.gateway_token)

    def save(self):
        tmp = SESSION_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({
                "device_id": self.device_id,
                "login_token": self.login_token,
                "gateway_token": self.gateway_token,
            }, f, ensure_ascii=False, indent=2)
        os.replace(tmp, SESSION_PATH)


# ---------- 兰大网关（登录 / 短信二次认证 / ST 票据） ----------

def _http(req: urllib.request.Request, timeout=15) -> str:
    try:
        with urllib.request.urlopen(req, context=CTX, timeout=timeout) as r:
            return r.read().decode("utf-8", "replace")
    except urllib.error.URLError as e:
        raise ApiError("网络连接失败，请检查学校服务和网络")


def gateway_login(username: str, password: str, store: SessionStore):
    """loginV2 密码登录；code=20001 时短信二次认证（验证码手动输入）。
    成功把 login_token/gateway_token 写入 store。"""
    body = json.dumps({"app_os": 2, "name": username, "pwd": password,
                       "device_id": store.device_id, "divece_system": "15"},
                      separators=(",", ":"))
    req = urllib.request.Request(
        GATEWAY + "/eusp-unify-terminal/app-user/loginV2",
        data=aes_encrypt_hex(body).encode(),
        headers={"Content-Type": "application/json;charset=UTF-8",
                 "Transfer-Encrypt": "true"})
    resp = json.loads(aes_decrypt_hex(_http(req)))
    code = resp.get("code")
    if code == 20001:
        if OTP_PROVIDER is None:
            raise AuthError("需要短信验证，请在配置窗口初始化登录")
        log("需要短信二次认证，正在发送验证码…")
        sid = (resp.get("data") or {}).get("session_id") or ""
        if not sid:
            raise ApiError("登录响应缺少验证会话")
        _gateway_form("/eusp-unify-terminal/app-user/sendPhoneCaptcha",
                      {"deviceId": store.device_id, "sessionId": sid})
        log("验证码已发送到绑定手机")
        try:
            captcha = (OTP_PROVIDER(sid) or "").strip()
            if not captcha:
                raise ApiError("未输入验证码，已取消登录")
        except (EOFError, KeyboardInterrupt):
            raise ApiError("未输入验证码，已取消登录")
        vr = _gateway_form("/eusp-unify-terminal/app-user/verifyPhoneCaptcha",
                           {"deviceId": store.device_id, "sessionId": sid,
                            "appOs": "1", "captcha": captcha})
        data = vr.get("data") or {}
    elif code == 1:
        data = resp.get("data") or {}
    else:
        raise ApiError("登录失败，请检查账号或密码（code=%s）" % code)
    store.login_token = data.get("login_token") or ""
    store.gateway_token = data.get("gateway_token") or ""
    if not store.has_tokens:
        raise ApiError("登录响应缺少令牌")
    store.save()
    log("登录成功（token 已持久化到 session.json）")


def _gateway_form(path: str, params: dict) -> dict:
    req = urllib.request.Request(GATEWAY + path,
                                 data=urllib.parse.urlencode(params).encode(),
                                 headers={"Content-Type": "application/x-www-form-urlencoded"})
    r = json.loads(_http(req))
    if r.get("code") != 1:
        raise ApiError("%s 失败: %s %s" % (path, r.get("code"), r.get("message")))
    return r


def get_st(store: SessionStore, service: str = "") -> str:
    """用 login_token/gateway_token 向网关换 ST 票据（响应 AES 加密）"""
    q = urllib.parse.urlencode({"loginToken": store.login_token,
                                "serviceId": "", "service": service})
    req = urllib.request.Request(
        GATEWAY + "/eusp-unify-terminal/app-user/getSt?" + q,
        headers={"Authorization": store.gateway_token, "Transfer-Encrypt": "true"})
    resp = json.loads(aes_decrypt_hex(_http(req)))
    if resp.get("code") != 1 or not resp.get("data"):
        raise AuthError("登录令牌已失效")
    return resp["data"]


# ---------- 辅导猫客户端 ----------

class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class FdmClient:
    """fdm 会话 = Cookie MOD_AUTH_CAS（LWPCookieJar 落盘 cookies.txt 持久化）"""

    def __init__(self):
        self.jar = http.cookiejar.LWPCookieJar(COOKIES_PATH)
        if os.path.exists(COOKIES_PATH):
            try:
                self.jar.load(ignore_discard=True, ignore_expires=True)
            except Exception as e:
                log("cookies.txt 读取失败（%s），将重新建立会话" % e)
        self.opener = urllib.request.build_opener(
            _NoRedirect, urllib.request.HTTPCookieProcessor(self.jar),
            urllib.request.HTTPSHandler(context=CTX))

    def save_cookies(self):
        try:
            self.jar.save(ignore_discard=True, ignore_expires=True)
        except Exception as e:
            log("cookies 保存失败: %s" % e)

    def has_session(self) -> bool:
        return any(c.name == "MOD_AUTH_CAS" for c in self.jar)

    def clear_session(self):
        self.jar.clear()
        self.save_cookies()

    # --- 认证链（见 FDM_API_REPORT.md §1，复刻 FdmApi.fdmLogin） ---

    def _get(self, url: str):
        """无重定向 GET，返回 (HTTP code, Location)"""
        req = urllib.request.Request(url, headers={"User-Agent": UA})
        try:
            with self.opener.open(req, timeout=15) as r:
                return r.status, r.headers.get("Location")
        except urllib.error.HTTPError as e:
            return e.code, e.headers.get("Location")

    def login_chain(self, store: SessionStore):
        """入口页 302 链 -> 网关 getSt 换 ST -> loginSuccess 落 MOD_AUTH_CAS"""
        self.jar.clear()
        code, loc = self._get(FDM_ENTRY)
        if not loc:
            raise ApiError("查寝服务入口异常(%s)" % code)
        iap = urllib.parse.urljoin(FDM_ENTRY, loc)
        code, loc = self._get(iap)
        if not loc:
            raise ApiError("查寝认证跳转异常(%s)" % code)
        m = re.search(r"[?&]service=([^&]+)", urllib.parse.urljoin(iap, loc))
        if not m:
            raise ApiError("查寝认证参数缺失")
        svc = urllib.parse.unquote(m.group(1))
        st = get_st(store, svc)  # login_token 失效会抛 AuthError
        _, loc = self._get(svc + "&ticket=" + urllib.parse.quote(st))
        if loc:
            self._get(urllib.parse.urljoin(svc, loc))
        if not self.has_session():
            raise AuthError("查寝登录失败（未获得 MOD_AUTH_CAS）")
        self.save_cookies()
        log("fdm 认证链完成，会话已建立")

    # --- 业务接口（信封 {"code":"0","datas":...}） ---

    def api(self, path: str, body: dict) -> dict:
        data = json.dumps(body, separators=(",", ":")).encode()
        req = urllib.request.Request(FDM_BASE + path, data=data, headers={
            "User-Agent": UA,
            "Content-Type": "application/json;charset=UTF-8",
            "X-Requested-With": "XMLHttpRequest"})
        try:
            with self.opener.open(req, timeout=20) as r:
                text = r.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as e:
            if 300 <= e.code < 400:
                raise AuthError("会话失效(HTTP %d)" % e.code)
            raise ApiError("HTTP %d" % e.code)
        obj = json.loads(text) if text.lstrip().startswith("{") else None
        if obj is None:
            raise AuthError("会话失效（非 JSON 响应）")
        datas = obj.get("datas")
        # 会话脱钩金丝雀：服务端返回 code=0 空壳 {"WEC-HASLOGIN":false,...}
        if isinstance(datas, dict) and str(datas.get("WEC-HASLOGIN")).lower() == "false":
            raise AuthError("会话失效（空壳响应）")
        if str(obj.get("code")) != "0":
            msg = obj.get("message") or "操作失败"
            code = str(obj.get("code"))
            if re.fullmatch(r"[0-9A-Fa-f]{32}", code) or any(
                    w in msg for w in ("登录", "权限", "失效")):
                raise AuthError(msg)
            raise ApiError(msg)
        return datas if isinstance(datas, dict) else {"_raw": datas}

    # --- 具体接口 ---

    def interval_months(self):
        return self.api("/student/attendance/getStuIntervalMonths", {})

    def running_tasks(self):
        return self.api("/student/attendance/getStuAttendacesInOneDay", {})

    def detail(self, sign_instance_wid: str, sign_wid: str):
        return self.api("/student/attendance/detailSignInstance",
                        {"signInstanceWid": sign_instance_wid, "signWid": sign_wid})

    def check_validation(self) -> bool:
        d = self.api("/student/attendance/checkValidation",
                     {"deviceId": "", "qrUuid": ""})
        return d.get("validation") is True

    def upload_photo(self, jpeg: bytes) -> str:
        """getUploadPolicy -> OSS multipart 直传 -> previewAttachment 取 URL"""
        policy = self.api("/student/attendance/getUploadPolicy", {"fileType": 1})
        host, form = policy.get("host", ""), policy.get("formData") or {}
        if not host or not form:
            raise ApiError("获取上传凭证失败")
        file_name = policy.get("fileName") or "photo"
        content_type = policy.get("contentType") or "image/jpeg"
        boundary = "----fdm" + uuid.uuid4().hex
        parts = []
        for k, v in form.items():
            parts.append(("--%s\r\nContent-Disposition: form-data; name=\"%s\"\r\n\r\n%s\r\n"
                          % (boundary, k, v)).encode())
        parts.append(("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                      "filename=\"%s.png\"\r\nContent-Type: %s\r\n\r\n"
                      % (boundary, file_name, content_type)).encode())
        parts.append(jpeg)
        parts.append(("\r\n--%s--\r\n" % boundary).encode())
        req = urllib.request.Request(host, data=b"".join(parts), headers={
            "User-Agent": UA,
            "Content-Type": "multipart/form-data; boundary=" + boundary})
        _http(req, timeout=60)
        r = self.api("/student/attendance/previewAttachment", {"ossKey": form.get("key", "")})
        url = r.get("_raw") if r.get("_raw") else r.get("url", "")
        if not url:
            raise ApiError("获取照片地址失败")
        return url

    def submit_sign(self, sign_instance_wid, lng, lat, photo_url, position):
        self.api("/student/attendance/submitSign", {
            "signInstanceWid": sign_instance_wid,
            "longitude": lng, "latitude": lat,
            "isMalposition": 0, "abnormalReason": "",
            "signPhotoUrl": photo_url, "position": position,
            "ticket": "", "uaIsCpadaily": False, "deviceId": "",
            "systemName": "", "systemVersion": "", "model": "",
        })
