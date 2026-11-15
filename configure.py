"""Local user-operated setup. Credentials never pass through the chat."""
import json
import math
import os
from pathlib import Path
import subprocess
import sys
import tkinter as tk
from tkinter import messagebox, ttk
import webbrowser

from attendance import CONFIG, ROOT, TARGET, atomic_json
from secure_store import discard_authentication, protect

cfg = json.loads(CONFIG.read_text(encoding='utf-8'))
root = tk.Tk()
root.title('学习辅导 · 本机配置')
root.geometry('650x565')
root.minsize(650, 565)
frame = ttk.Frame(root, padding=22)
frame.pack(fill='both', expand=True)
frame.columnconfigure(1, weight=1)

ttk.Label(frame, text=TARGET, font=('Microsoft YaHei UI', 17, 'bold')).grid(row=0, column=0, columnspan=2, sticky='w', pady=(0, 9))
ttk.Label(frame, text='每天北京时间 20:31；22:30 前补跑；先初始化登录，再启用定时任务。').grid(row=1, column=0, columnspan=2, sticky='w', pady=(0, 12))

fields = {}
def entry(row, key, label, value='', secret=False):
    ttk.Label(frame, text=label).grid(row=row, column=0, sticky='w', padx=(0, 15), pady=7)
    var = tk.StringVar(value=str(value))
    fields[key] = var
    item = ttk.Entry(frame, textvariable=var, show='●' if secret else '')
    item.grid(row=row, column=1, sticky='ew', pady=7)
    return item

entry(2, 'username', '校园卡号', cfg.get('username', ''))
entry(3, 'password', '校园密码', secret=True)
ttk.Label(frame, text='已有加密密码时可留空；密码由 Windows 本机账户加密保存。', foreground='#555555').grid(row=4, column=0, columnspan=2, sticky='w')
location = cfg.get('location') or {}
entry(5, 'address', '实际住宿地址', location.get('address', ''))
entry(6, 'longitude', '经度', location.get('longitude') if location.get('longitude') is not None else '')
entry(7, 'latitude', '纬度', location.get('latitude') if location.get('latitude') is not None else '')
ttk.Label(frame, text='填写高德/腾讯地图 GCJ-02 实际住宿坐标，经度在前、纬度在后。\n程序会转换为学校使用的百度 BD-09 坐标进行范围检查和签到。', foreground='#555555').grid(row=8, column=0, columnspan=2, sticky='w', pady=(5, 10))
ttk.Button(frame, text='打开高德坐标拾取工具', command=lambda: webbrowser.open('https://lbs.amap.com/tools/picker')).grid(row=9, column=0, columnspan=2, sticky='w')

status = tk.StringVar(value='填写后点击“保存并初始化登录”。如需短信验证，会单独弹出输入框。')
ttk.Label(frame, textvariable=status, wraplength=600, foreground='#1b5678').grid(row=11, column=0, columnspan=2, sticky='w', pady=15)
buttons = ttk.Frame(frame)
buttons.grid(row=10, column=0, columnspan=2, sticky='w', pady=(17, 0))

def save(show=True):
    global cfg
    username = fields['username'].get().strip()
    pwd = fields['password'].get()
    try:
        lng = float(fields['longitude'].get().strip())
        lat = float(fields['latitude'].get().strip())
        if not (math.isfinite(lng) and math.isfinite(lat) and -180 <= lng <= 180 and -90 <= lat <= 90):
            raise ValueError()
    except ValueError:
        messagebox.showerror('坐标未填完整', '请填写有效的经度和纬度。', parent=root)
        return False
    address = fields['address'].get().strip()
    if not username or not address:
        messagebox.showerror('信息未填完整', '请填写校园卡号和实际住宿地址。', parent=root)
        return False
    if not pwd and (not cfg.get('password_protected') or username != cfg.get('username')):
        messagebox.showerror('请填写密码', '首次配置或更换账号时需要填写校园密码。', parent=root)
        return False
    try:
        protected = protect(pwd) if pwd else cfg['password_protected']
        if username != cfg.get('username') or pwd:
            discard_authentication(ROOT)
        cfg.update(username=username, password_protected=protected, target_task_name=TARGET,
                   location={'longitude': lng, 'latitude': lat, 'address': address,
                             'coordinate_system': 'GCJ-02'})
        atomic_json(CONFIG, cfg)
        fields['password'].set('')
    except Exception:
        messagebox.showerror('保存失败', 'Windows 加密或文件写入失败，请使用当前 Windows 账户重试。', parent=root)
        return False
    if show:
        status.set('配置已保存；请初始化登录以验证学校会话。此操作不会提交签到。')
    return True

def initialize():
    if not save(show=False):
        return
    init_button.configure(state='disabled')
    status.set('正在验证学校登录……如需短信验证码，请在弹出的窗口中填写。不会提交签到。')
    (ROOT / 'logs').mkdir(exist_ok=True)
    output = (ROOT / 'logs' / 'login-initialization.log').open('a', encoding='utf-8')
    env = dict(os.environ, PYTHONIOENCODING='utf-8')
    process = subprocess.Popen([sys.executable, str(ROOT / 'attendance.py'), '--login-only', '--interactive'],
                               cwd=ROOT, stdout=output, stderr=output, env=env,
                               creationflags=subprocess.CREATE_NO_WINDOW)
    def poll():
        result = process.poll()
        if result is None:
            root.after(1000, poll)
            return
        output.close()
        init_button.configure(state='normal')
        if result == 0:
            status.set('学校登录会话验证通过。关闭此窗口后，双击 03_启用每日签到.cmd。')
            messagebox.showinfo('登录配置完成', '学校会话验证通过；本次初始化没有提交签到。\n下一步：关闭此窗口，双击 03_启用每日签到.cmd。', parent=root)
        else:
            try:
                report = json.loads((ROOT / 'logs/latest.json').read_text(encoding='utf-8'))
                status.set('登录尚未完成：' + report['message'])
            except Exception:
                status.set('登录尚未完成，请检查网络和本机登录日志。')
    root.after(1000, poll)

ttk.Button(buttons, text='仅保存', command=save).pack(side='left', padx=(0, 10))
init_button = ttk.Button(buttons, text='保存并初始化登录', command=initialize)
init_button.pack(side='left', padx=(0, 10))
ttk.Button(buttons, text='查看运行状态', command=lambda: os.startfile(str(ROOT / 'logs/latest.json')) if (ROOT / 'logs/latest.json').exists() else messagebox.showinfo('运行状态', '尚无运行记录。', parent=root)).pack(side='left')
root.mainloop()
