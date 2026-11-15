from pathlib import Path
import sys
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from secure_store import discard_authentication

directory = ROOT / '.verification' / ('auth-state-' + uuid.uuid4().hex)
directory.mkdir(parents=True)
state_files = [directory / name for name in ('cookies.txt', 'session.json', 'auth-ready.json')]
for path in state_files:
    path.touch()
configuration = directory / 'config.json'
configuration.write_text('{}', encoding='utf-8')
discard_authentication(directory)
assert not any(path.exists() for path in state_files), '旧学校会话必须全部清理'
assert configuration.read_text('utf-8') == '{}', '校园配置必须保留'
discard_authentication(directory)
print('学校会话清理验证通过。')
