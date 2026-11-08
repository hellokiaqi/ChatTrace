"""The manual and scheduled end-to-end archive update."""
import json
from pathlib import Path

from .core import Archive, atomic, exclusive, now, run_locked, safe_error
from .retrieval import build
from .publication import sync


def update(config):
    root = Path(config['archive_root'])
    with exclusive(root):
        outcome = {'started': now(), 'status': 'running', 'stage': 'collection'}
        history = root / 'update-runs'
        log = history / (outcome['started'].replace(':','-') + '.json')
        try:
            atomic(log,json.dumps(outcome).encode())
            collected = run_locked(config)
            outcome.update(run_id=collected['id'], collection=collected['status'],
                           archive_uploads=collected['uploads'], stage='build')
            atomic(log,json.dumps(outcome).encode())
            archive = Archive(config)
            try:
                built = build(archive)
                outcome.update(messages=built['source_messages'],documents=built['documents'],stage='upload')
                atomic(log,json.dumps(outcome).encode())
                published = sync(archive)
                outcome.update(changed_documents=sum(action!='skip' for _,action in published['plan']),
                               folder=published['folder'],stage='complete',
                               status='synced_partial' if collected['status'].endswith('_partial') else 'synced')
                return outcome
            finally:
                archive.close()
        except Exception as exc:
            outcome.update(status='failed',error=safe_error(exc))
            raise
        finally:
            outcome['ended'] = now()
            atomic(log,json.dumps(outcome,ensure_ascii=False).encode())
