"""Hidden entry for Windows Task Scheduler."""
from pathlib import Path
import runpy
import sys

root = Path(__file__).resolve().parent
(root / 'logs').mkdir(exist_ok=True)
with (root / 'logs/scheduler-console.log').open('a', encoding='utf-8', buffering=1) as output:
    sys.stdout = output
    sys.stderr = output
    sys.argv = [str(root / 'attendance.py'), '--scheduled']
    runpy.run_path(sys.argv[0], run_name='__main__')
