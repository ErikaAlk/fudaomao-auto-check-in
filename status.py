"""Print setup and attendance state without exposing credentials or locations."""
import json
from pathlib import Path

ROOT = Path(__file__).resolve().parent
print('学习辅导 · 本机状态\n')
config = ROOT / 'config.json'
if not config.exists():
    print('尚未安装，请先运行 01_安装环境.cmd。')
else:
    cfg = json.loads(config.read_text(encoding='utf-8'))
    loc = cfg.get('location') or {}
    print('账号配置：' + ('已填写' if cfg.get('username') and cfg.get('password_protected') else '未完成'))
    print('实际位置：' + ('已填写' if all(loc.get(k) not in (None, '') for k in ('longitude', 'latitude', 'address')) else '未完成'))
    print('登录初始化：' + ('已有验证记录' if (ROOT / 'auth-ready.json').exists() else '尚无验证记录'))
log = ROOT / 'logs/latest.json'
if log.exists():
    report = json.loads(log.read_text(encoding='utf-8'))
    print('\n最近检查时间：' + str(report.get('checked_at', '')))
    print('状态：' + str(report.get('state', '')))
    print('说明：' + str(report.get('message', '')))
else:
    print('\n尚无运行记录。登录初始化通过后，可以运行 04_只读检查.cmd。')
print('\n服务端确认已签到时，状态为 verified_signed 或 verified_already_signed。')
