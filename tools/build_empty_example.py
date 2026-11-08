"""Generate an empty archive with the production writer; no invented chat data."""
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from chattrace.core import Archive, run

destination = Path(__file__).resolve().parent.parent / 'examples' / 'empty-archive'
with tempfile.TemporaryDirectory() as temp:
    base = Path(temp)
    inbox = base / 'inbox'
    inbox.mkdir()
    batch = {'schema': 1, 'account_id': 'EMPTY_EXAMPLE', 'contact_id': 'NO_CONTACT_SELECTED',
             'source': 'empty-example-no-real-chat', 'captured_at': '2026-10-29T12:00:00+08:00',
             'messages': [], 'screenshots': []}
    (inbox / 'empty.json').write_text(json.dumps(batch), encoding='utf-8')
    config = {'account_id': batch['account_id'], 'contact_id': batch['contact_id'],
              'contact_name': '尚未选择好友（空档案示例）', 'archive_root': str(base / 'archive'),
              'inbox': str(inbox), 'reader': 'import', 'timezone': 'Asia/Shanghai'}
    run(config, no_upload=True)
    archive = Archive(config)
    try:
        for row in archive.db.execute('SELECT path FROM files'):
            target = destination / row['path']
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes((archive.root / row['path']).read_bytes())
    finally:
        archive.close()
print(destination)
