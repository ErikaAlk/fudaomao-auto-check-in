import base64
import ctypes
from ctypes import wintypes
import os
from pathlib import Path

from cryptography.fernet import Fernet


def discard_authentication(directory):
    for name in ('cookies.txt', 'session.json', 'auth-ready.json'):
        (Path(directory) / name).unlink(missing_ok=True)


def linux_cipher(create=False):
    path = Path(__file__).resolve().parent / '.credential-key'
    if create and not path.exists():
        descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, 'wb') as stream:
            stream.write(Fernet.generate_key())
    if path.stat().st_mode & 0o077:
        raise PermissionError('凭据密钥只允许运行账户访问')
    return Fernet(path.read_bytes())


class Blob(ctypes.Structure):
    _fields_ = [("size", wintypes.DWORD), ("data", ctypes.POINTER(ctypes.c_ubyte))]


def _convert(data, decrypt=False):
    buf = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buf, ctypes.POINTER(ctypes.c_ubyte)))
    dest = Blob()
    crypt = ctypes.WinDLL("crypt32", use_last_error=True)
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        fn = crypt.CryptUnprotectData
        fn.argtypes = [ctypes.POINTER(Blob), ctypes.c_void_p, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(dest))
    else:
        fn = crypt.CryptProtectData
        fn.argtypes = [ctypes.POINTER(Blob), wintypes.LPCWSTR, ctypes.c_void_p,
                       ctypes.c_void_p, ctypes.c_void_p, wintypes.DWORD,
                       ctypes.POINTER(Blob)]
        ok = fn(ctypes.byref(source), "LZU attendance", None, None, None, 1,
                ctypes.byref(dest))
    if not ok:
        raise OSError("Windows 无法加密或解密本机凭据，请使用同一 Windows 账户重新配置")
    try:
        return ctypes.string_at(dest.data, dest.size)
    finally:
        kernel.LocalFree(ctypes.cast(dest.data, ctypes.c_void_p))


def protect(text):
    if os.name == 'posix':
        return linux_cipher(create=True).encrypt(text.encode('utf-8')).decode('ascii')
    return base64.b64encode(_convert(text.encode("utf-8"))).decode("ascii")


def unprotect(text):
    if os.name == 'posix':
        return linux_cipher().decrypt(text.encode('ascii')).decode('utf-8')
    return _convert(base64.b64decode(text), decrypt=True).decode("utf-8")
