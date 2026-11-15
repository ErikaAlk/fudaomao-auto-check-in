"""Exact-task attendance runner with Beijing scheduling and server verification."""
import argparse
from datetime import datetime, time as clock_time, timedelta, timezone
import hashlib
import json
import math
import os
from pathlib import Path
import sys
import time
import urllib.error

import eviltransform
import lzu_api as api
from secure_store import unprotect

ROOT = Path(os.environ.get('LZU_DATA_DIR', Path(__file__).resolve().parent))
CONFIG = ROOT / 'config.json'
LOGS = ROOT / 'logs'
TZ = timezone(timedelta(hours=8))
TARGET = os.environ.get('LZU_TARGET_NAME', '物理学院2026级本科生晚查寝')


class StopRun(Exception):
    def __init__(self, state, message, code=2):
        self.state, self.message, self.code = state, message, code
        super().__init__(message)


def now_bj():
    return datetime.now(TZ)


def atomic_json(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + '.tmp')
    tmp.write_text(json.dumps(value, ensure_ascii=False, indent=2), encoding='utf-8')
    tmp.replace(path)


def record(state, message, **extra):
    stamp = now_bj()
    report = {'date': stamp.date().isoformat(), 'checked_at': stamp.isoformat(),
              'state': state, 'message': message, **extra}
    LOGS.mkdir(exist_ok=True)
    with (LOGS / (report['date'] + '.jsonl')).open('a', encoding='utf-8') as f:
        f.write(json.dumps(report, ensure_ascii=False) + '\n')
    atomic_json(LOGS / 'latest.json', report)
    print(state + ': ' + message, flush=True)
    return report


def read_config(require_location=True):
    cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
    if cfg.get('target_task_name') != TARGET:
        raise StopRun('configuration_required', '任务名称配置不匹配，请重新配置')
    if not cfg.get('username') or not cfg.get('password_protected'):
        raise StopRun('configuration_required', '请打开配置窗口填写账号并初始化登录')
    if require_location:
        loc = cfg.get('location') or {}
        try:
            lng, lat = float(loc['longitude']), float(loc['latitude'])
        except (KeyError, TypeError, ValueError):
            raise StopRun('configuration_required', '请在配置窗口填写实际住宿经纬度')
        if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180
                and -90 <= lat <= 90 and loc.get('address', '').strip()):
            raise StopRun('configuration_required', '实际住宿地址或经纬度无效')
        if loc.get('coordinate_system') != 'GCJ-02':
            raise StopRun('configuration_required', '请填写高德/腾讯地图 GCJ-02 坐标')
    return cfg


def schedule_gate(cfg, stamp):
    if stamp.date().isoformat() < cfg['start_date']:
        return 'not_started'
    current_time = stamp.time().replace(tzinfo=None)
    if current_time < clock_time.fromisoformat(cfg['run_at']):
        return 'before_window'
    if current_time >= clock_time.fromisoformat(cfg['deadline']):
        return 'window_closed'
    return None


def ensure_auth(fdm, cfg):
    store = api.SessionStore()
    if fdm.has_session():
        try:
            fdm.interval_months()
            return
        except api.AuthError:
            fdm.clear_session()
    if store.has_tokens:
        try:
            fdm.login_chain(store)
            return
        except api.AuthError:
            pass
    api.gateway_login(cfg['username'], unprotect(cfg['password_protected']), store)
    fdm.login_chain(store)


def parse_school_time(value):
    if not isinstance(value, str):
        return None
    for fmt in ('%Y-%m-%d %H:%M', '%Y-%m-%d %H:%M:%S'):
        try:
            return datetime.strptime(value, fmt).replace(tzinfo=TZ)
        except ValueError:
            pass
    return None


def select_target(datas, stamp):
    matches = {}
    # Signed status from the server takes precedence over duplicate unsiged entries.
    for category in ('registerLeaveTasks', 'unSignedTasks', 'signedTasks'):
        for task in datas.get(category) or []:
            if task.get('taskName') != TARGET:
                continue
            if os.environ.get('LZU_SIGN_WID') and str(task.get('signWid')) != os.environ['LZU_SIGN_WID']:
                continue
            begin = parse_school_time(task.get('singleTaskBeginTime'))
            end = parse_school_time(task.get('singleTaskEndTime'))
            if not begin or not end or begin.date() != stamp.date():
                continue
            wid = str(task.get('signInstanceWid') or '')
            if wid and task.get('signWid'):
                matches[wid] = (category, task, begin, end)
    if not matches:
        raise StopRun('target_not_found', '服务端未返回今天的指定任务；稍后补跑', 5)
    if len(matches) != 1:
        raise StopRun('ambiguous_target', '今天有多个同名任务，请手动核实', 5)
    return next(iter(matches.values()))


def query_target(fdm, stamp):
    return select_target(fdm.running_tasks(), stamp)


def verify(fdm, stamp, wid, attempts=3):
    for attempt in range(attempts):
        category, task, _, _ = query_target(fdm, stamp)
        if category == 'signedTasks' and str(task['signInstanceWid']) == wid:
            return True
        if attempt + 1 < attempts:
            time.sleep(2)
    return False


def distance_m(lng1, lat1, lng2, lat2):
    a, b = math.radians(lat1), math.radians(lat2)
    h = math.sin((b-a)/2)**2 + math.cos(a)*math.cos(b)*math.sin(math.radians(lng2-lng1)/2)**2
    return 6371008.8 * 2 * math.asin(math.sqrt(min(1.0, max(0.0, h))))


def school_location(location):
    if location.get('coordinate_system') != 'GCJ-02':
        raise StopRun('configuration_required', '请填写高德/腾讯地图 GCJ-02 坐标')
    latitude, longitude = eviltransform.gcj2bd(float(location['latitude']), float(location['longitude']))
    return {**location, 'longitude': longitude, 'latitude': latitude, 'coordinate_system': 'BD-09'}


def validate_location(detail, location):
    if location.get('coordinate_system') != 'BD-09':
        raise StopRun('configuration_required', '学校签到范围检查需要 BD-09 坐标')
    fences = detail.get('signPlaceSelected') or []
    if not fences:
        raise StopRun('location_rules_unknown', '任务未返回签到范围，请在企业微信核实', 6)
    valid = []
    for fence in fences:
        try:
            lng, lat = float(fence['longitude']), float(fence['latitude'])
            radius = float(fence['radius'])
            if not all(math.isfinite(v) for v in (lng, lat, radius)) or radius <= 0:
                continue
            valid.append(radius-distance_m(float(location['longitude']), float(location['latitude']), lng, lat))
        except (KeyError, TypeError, ValueError):
            continue
    if not valid:
        raise StopRun('location_rules_unknown', '签到范围格式无法确认，请手动核实', 6)
    if max(valid) < 0:
        raise StopRun('outside_fence', '填写的实际位置位于任务范围外，请在企业微信核实', 6)
    return max(valid)


def varied_location(location, variation, clearance):
    radial, bearing = variation['radial_fraction'], variation['bearing_fraction']
    if not all(math.isfinite(value) and 0 <= value < 1 for value in (radial, bearing)):
        raise ValueError('每日位置偏移参数无效')
    distance = min(49.0, max(0.0, clearance-0.1)) * math.sqrt(radial)
    angular = distance / 6371008.8
    direction = bearing * 2 * math.pi
    lat, lng = math.radians(float(location['latitude'])), math.radians(float(location['longitude']))
    shifted_lat = math.asin(math.sin(lat)*math.cos(angular)+math.cos(lat)*math.sin(angular)*math.cos(direction))
    shifted_lng = lng + math.atan2(math.sin(direction)*math.sin(angular)*math.cos(lat),
                                  math.cos(angular)-math.sin(lat)*math.sin(shifted_lat))
    return {**location, 'latitude':math.degrees(shifted_lat),
            'longitude':(math.degrees(shifted_lng)+180)%360-180}


def pending_path(stamp):
    return LOGS / (stamp.date().isoformat() + '-submission.json')


def perform(fdm, cfg, stamp, check_only=False):
    category, task, begin, end = query_target(fdm, stamp)
    wid = str(task['signInstanceWid'])
    if category == 'signedTasks':
        record('verified_already_signed', '服务端确认今天的指定任务已签到', task_instance=wid)
        return 0
    if check_only:
        record('checked_unsigned', '只读检查完成；指定任务尚未签到，未提交', task_instance=wid)
        return 0
    if not begin <= stamp < end:
        raise StopRun('school_window_closed', '当前时间不在学校任务开放时间内', 5)
    if category == 'registerLeaveTasks':
        raise StopRun('unsupported_register_task', '登记制任务需要在企业微信核实', 6)
    pending = pending_path(stamp)
    if pending.exists():
        previous = json.loads(pending.read_text(encoding='utf-8'))
        if previous.get('task_instance') == wid:
            raise StopRun('submission_unconfirmed', '此前已发起提交；服务端尚未确认，停止重复提交，请手动核实', 8)
    detail = fdm.detail(wid, str(task['signWid']))
    if str(detail.get('signMode', '')) != '0':
        raise StopRun('unsupported_mode', '此任务需要其他签到方式，请在企业微信完成', 6)
    if str(detail.get('isPhoto', '')) not in ('0', 'False', 'false'):
        raise StopRun('fresh_photo_required', '此任务要求拍照，请在企业微信拍摄当日照片', 6)
    location = school_location(cfg['location'])
    clearance = validate_location(detail, location)
    if 'location_variation' in cfg:
        location = varied_location(location, cfg['location_variation'], clearance)
        validate_location(detail, location)
    if fdm.check_validation():
        raise StopRun('captcha_required', '学校要求验证码，请在企业微信完成签到', 9)
    # Recheck both windows immediately before the only submission call.
    submit_at = now_bj()
    if schedule_gate(cfg, submit_at) or not begin <= submit_at < end:
        raise StopRun('window_closed', '准备过程中已超出签到时间，未提交', 5)
    atomic_json(pending, {'task_instance': wid, 'attempted_at': submit_at.isoformat(), 'state': 'pending'})
    try:
        fdm.submit_sign(wid, float(location['longitude']), float(location['latitude']), '', location['address'])
    except Exception:
        # A network error can occur after a server commit. Only read back afterwards.
        try:
            if verify(fdm, now_bj(), wid):
                atomic_json(pending, {'task_instance': wid, 'state': 'verified'})
                record('verified_signed', '服务端回查确认今天的指定任务已签到', task_instance=wid)
                return 0
        except Exception:
            pass
        raise StopRun('submission_unconfirmed', '提交结果未能确认；已禁止重复提交，请在企业微信核实', 8)
    if not verify(fdm, now_bj(), wid):
        raise StopRun('submission_unconfirmed', '提交后服务端尚未显示已签到；已禁止重复提交，请手动核实', 8)
    atomic_json(pending, {'task_instance': wid, 'state': 'verified'})
    record('verified_signed', '服务端回查确认今天的指定任务已签到', task_instance=wid)
    return 0


def ask_otp(session_id=None):
    import tkinter as tk
    from tkinter import simpledialog
    root = tk.Tk()
    root.withdraw()
    root.attributes('-topmost', True)
    try:
        return simpledialog.askstring('兰大登录验证', '请输入学校发送到绑定手机的短信验证码：', parent=root)
    finally:
        root.destroy()


def main():
    parser = argparse.ArgumentParser()
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument('--scheduled', action='store_true')
    group.add_argument('--check', action='store_true', help='只读查询，绝不提交签到')
    group.add_argument('--login-only', action='store_true', help='仅登录及验证会话')
    parser.add_argument('--interactive', action='store_true')
    args = parser.parse_args()
    if args.interactive and not args.login_only:
        raise StopRun('invalid_mode', '交互验证码仅可用于初始化登录')
    stamp = now_bj()
    cfg_base = json.loads(CONFIG.read_text(encoding='utf-8'))
    if args.scheduled:
        gate = schedule_gate(cfg_base, stamp)
        if gate:
            record(gate, '本次不在配置的运行日期或时间窗口内；未登录、未提交')
            return 0
    cfg = read_config(require_location=not args.login_only)
    if args.interactive:
        api.OTP_PROVIDER = ask_otp
    elif cfg.get('otp_service_url'):
        from sms_provider import request_otp
        api.OTP_PROVIDER = request_otp
    fdm = api.FdmClient()
    ensure_auth(fdm, cfg)
    if args.login_only:
        fdm.interval_months()
        atomic_json(ROOT / 'auth-ready.json', {'verified_at': now_bj().isoformat(),
                    'credential_digest': hashlib.sha256((cfg['username'] + cfg['password_protected']).encode()).hexdigest()})
        record('login_verified', '学校会话验证通过；未提交签到')
        return 0
    result = perform(fdm, cfg, stamp, check_only=args.check)
    fdm.save_cookies()
    return result


if __name__ == '__main__':
    try:
        sys.exit(main())
    except StopRun as exc:
        record(exc.state, exc.message)
        sys.exit(exc.code)
    except api.AuthError:
        record('login_required', '会话失效或需要登录验证，请打开配置窗口初始化登录')
        sys.exit(3)
    except (api.ApiError, urllib.error.URLError, TimeoutError, ConnectionError):
        record('network_or_api_error', '学校服务或网络请求失败；请检查网络，稍后会补跑')
        sys.exit(4)
    except Exception as exc:
        # Never log request bodies, tokens, account names or unredacted exceptions.
        record('error', '运行失败（%s），请检查配置或日志状态' % type(exc).__name__)
        sys.exit(7)
