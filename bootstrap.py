from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import venv

ROOT = Path(__file__).resolve().parent


def main():
    if os.name != 'nt' or sys.version_info[:2] != (3, 12) or struct.calcsize('P') != 8:
        raise SystemExit('请先安装 Windows 64 位 Python 3.12，再重新运行安装入口。')
    env_dir = ROOT / '.venv'
    print('正在创建本机独立运行环境……', flush=True)
    venv.EnvBuilder(with_pip=True).create(env_dir)
    python = env_dir / 'Scripts/python.exe'
    subprocess.run([str(python), '-m', 'pip', 'install', '-r', str(ROOT / 'requirements.lock.txt')], check=True)
    subprocess.run([str(python), '-m', 'pip', 'check'], check=True)
    subprocess.run([str(python), str(ROOT / 'verify_environment.py')],
                   cwd=ROOT, check=True)
    config = ROOT / 'config.json'
    if not config.exists():
        values = json.loads((ROOT / 'config.example.json').read_text(encoding='utf-8'))
        values['start_date'] = datetime.now(timezone(timedelta(hours=8))).date().isoformat()
        config.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding='utf-8')
    print('安装完成。下一步双击“02_填写配置.cmd”，填写自己的账号和实际住宿位置。', flush=True)


if __name__ == '__main__':
    try:
        main()
    except subprocess.CalledProcessError:
        raise SystemExit('组件安装失败。请检查 Python 版本及依赖下载网络。')
