"""Install a recipient-local Windows task after login has been verified."""
import argparse
import csv
from datetime import date, datetime
import hashlib
import json
from pathlib import Path
import subprocess
import xml.etree.ElementTree as ET
from xml.sax.saxutils import escape

from attendance import StopRun, read_config

ROOT = Path(__file__).resolve().parent
NAME = '学习辅导-每日签到'


def render_xml(cfg, sid):
    day = date.fromisoformat(cfg['start_date']).isoformat()
    at = datetime.strptime(cfg['run_at'], '%H:%M').strftime('%H:%M')
    if at != '20:31' or cfg.get('deadline') != '22:30':
        raise ValueError('此安装入口对应每天 20:31，22:30 截止，请恢复模板时间设置。')
    pythonw = escape(str(ROOT / '.venv/Scripts/pythonw.exe'))
    arguments = escape('"' + str(ROOT / 'scheduled.pyw') + '"')
    directory = escape(str(ROOT))
    xml = f'''<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>Learning support: LZU off-campus attendance, Beijing 20:31, catch-up before 22:30.</Description></RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <Repetition><Interval>PT5M</Interval><Duration>PT1H55M</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{day}T{at}:00+08:00</StartBoundary><Enabled>true</Enabled>
      <ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>
    </CalendarTrigger>
    <LogonTrigger>
      <Repetition><Interval>PT5M</Interval><Duration>PT1H55M</Duration><StopAtDurationEnd>false</StopAtDurationEnd></Repetition>
      <StartBoundary>{day}T00:00:00+08:00</StartBoundary><Enabled>true</Enabled>
      <UserId>{escape(sid)}</UserId><Delay>PT30S</Delay>
    </LogonTrigger>
  </Triggers>
  <Principals><Principal id="CurrentUser"><UserId>{escape(sid)}</UserId><LogonType>InteractiveToken</LogonType><RunLevel>LeastPrivilege</RunLevel></Principal></Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries><StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate><StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings><StopOnIdleEnd>false</StopOnIdleEnd><RestartOnIdle>false</RestartOnIdle></IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand><Enabled>true</Enabled><Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle><WakeToRun>true</WakeToRun>
    <ExecutionTimeLimit>PT4M</ExecutionTimeLimit><Priority>7</Priority>
  </Settings>
  <Actions Context="CurrentUser"><Exec><Command>{pythonw}</Command><Arguments>{arguments}</Arguments><WorkingDirectory>{directory}</WorkingDirectory></Exec></Actions>
</Task>
'''
    ET.fromstring(xml)
    return xml


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--preview', action='store_true', help='仅生成 XML，不注册任务、不登录、不提交')
    args = parser.parse_args()
    cfg = json.loads((ROOT / 'config.json').read_text(encoding='utf-8'))
    if not args.preview:
        cfg = read_config()
        auth_file = ROOT / 'auth-ready.json'
        if not auth_file.exists():
            raise SystemExit('请先打开配置窗口，完成“保存并初始化登录”，再启用定时任务。')
        auth = json.loads(auth_file.read_text(encoding='utf-8'))
        digest = hashlib.sha256((cfg['username'] + cfg['password_protected']).encode()).hexdigest()
        if auth.get('credential_digest') != digest:
            raise SystemExit('账号配置已变化，请先重新初始化登录，再启用定时任务。')
    identity = subprocess.check_output(['whoami', '/user', '/fo', 'csv', '/nh'], text=True, encoding='mbcs')
    sid = next(csv.reader([identity.strip()]))[1]
    xml = render_xml(cfg, sid)
    path = ROOT / 'scheduled-task.xml'
    path.write_text(xml, encoding='utf-16')
    if args.preview:
        print('计划任务 XML 已生成并通过格式检查；未注册任务、未登录、未提交签到。')
        return
    subprocess.run(['schtasks', '/Create', '/TN', NAME, '/XML', str(path), '/F'], check=True)
    print('“学习辅导-每日签到”已启用。每天北京时间 20:31 运行，22:30 前补跑。')
    print('若启用时已经进入当日运行窗口，系统可能随即补跑。')


if __name__ == '__main__':
    try:
        main()
    except StopRun as exc:
        raise SystemExit(exc.message)
    except (ValueError, OSError, subprocess.CalledProcessError):
        raise SystemExit('任务安装失败，请检查配置、文件位置以及 Windows 任务计划程序的权限。')
