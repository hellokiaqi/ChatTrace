"""Independent evidence metrics derived from current records, never inferred from totals."""
import json
from collections import Counter
from .core import digest, encode, upload_exclusion

MEDIA_TYPES = {'image', 'sticker', 'voice', 'video', 'file'}


def screenshot_policy(config):
    if 'screenshot_policy' in config:
        return config['screenshot_policy']
    if 'require_screenshots' in config:
        return 'all' if config['require_screenshots'] else 'manual'
    return 'media'


def visual_required(config, message):
    policy = screenshot_policy(config)
    return policy == 'all' or (policy == 'media' and message['type'] in ('image', 'sticker', 'video'))


def partial_screenshot_index(records):
    known = {mid for mid, _ in records}
    index = {}
    for _, message in records:
        for path in message['screenshots']:
            meta = message.get('screenshot_metadata', {}).get(path, {})
            if meta.get('range_verification') == 'visual_confirmation':
                for mid in meta.get('partially_visible_message_ids', []):
                    if mid in known:
                        index.setdefault(mid, set()).add(path)
    return {mid: sorted(paths) for mid, paths in index.items()}


def message_evidence(root, mid, message, partial_paths=()):
    full, partial, unverified = [], [], []
    for path in message['screenshots']:
        if not (root / path).is_file():
            continue
        meta = message.get('screenshot_metadata', {}).get(path, {})
        if meta.get('range_verification') != 'visual_confirmation':
            unverified.append(path)
        elif mid in meta.get('partially_visible_message_ids', []):
            partial.append(path)
        else:
            full.append(path)
    partial = sorted(set(partial) | {p for p in partial_paths if (root / p).is_file()})
    visual = 'confirmed_full' if full else 'partial_only' if partial else 'linked_unverified' if unverified else 'missing'
    status = message.get('source_metadata', {}).get('media_status', '')
    if message['type'] not in MEDIA_TYPES:
        media = 'not_applicable'
    elif not message['media'] or any(not (root / a['path']).is_file() for a in message['media']):
        media = 'pending'
    elif message.get('source_metadata',{}).get('missing_media_parts'):
        media = 'pending'
    elif status == 'thumbnail':
        media = 'thumbnail_only'
    elif status.startswith('unavailable:'):
        media = 'pending'
    else:
        media = 'extracted'
    return {'visual': visual, 'confirmed_screenshots': full, 'partial_screenshots': partial, 'unverified_screenshots': unverified, 'media': media,
            'media_reason': status, 'plain_text': message['type'] == 'text'}


def report(archive):
    db, root = archive.db, archive.root
    records = [(f"M{r['id']:06d}", json.loads(r['payload'])) for r in db.execute('SELECT id,payload FROM messages')]
    partials = partial_screenshot_index(records)
    evidence = {mid: message_evidence(root, mid, m, partials.get(mid, ())) for mid, m in records}
    visual = Counter(x['visual'] for x in evidence.values())
    media = Counter(x['media'] for x in evidence.values())
    files = list(db.execute('SELECT * FROM files'))
    bad = [r['path'] for r in files if not (root / r['path']).is_file() or digest((root / r['path']).read_bytes()) != r['sha256']]
    refs = {p for _, m in records for p in m['screenshots']} | {a['path'] for _, m in records for a in m['media']}
    missing = sorted(p for p in refs if not (root / p).is_file())
    expected = {}
    for mid, m in records:
        expected[mid] = {**m, 'id': mid, 'archive_status': 'local_committed'}
    actual, duplicates = {}, []
    for path in sorted(root.glob('*/*/*/messages.jsonl')):
        for line in path.read_text('utf-8').splitlines():
            if not line.strip():
                continue
            m = json.loads(line)
            if m['id'] in actual:
                duplicates.append(m['id'])
            actual[m['id']] = m
    mismatch = sorted(k for k in expected.keys() | actual.keys() if encode(expected.get(k)) != encode(actual.get(k)))
    integrity = db.execute('PRAGMA integrity_check').fetchone()[0]
    reader = {}
    if db.execute("SELECT 1 FROM sqlite_master WHERE name='reader_state'").fetchone():
        reader = {r['source']: json.loads(r['payload']) for r in db.execute('SELECT * FROM reader_state')}
    times = sorted(m['timestamp'] for _, m in records if m['timestamp'])
    eligible = [r for r in files if upload_exclusion(archive.config, r['path']) is None]
    exclusions = Counter(upload_exclusion(archive.config, r['path']) for r in files if upload_exclusion(archive.config, r['path']))
    verified = sum(r['uploaded'] == r['sha256'] and bool(r['remote_id']) for r in eligible)
    required = [mid for mid, m in records if visual_required(archive.config, m)]
    return {'schema': 1,
            'collection': {'status': 'scope_unverified', 'stored_messages': len(records),
                           'server_id_messages': sum((m['source_id'] or '').startswith('server:') for _, m in records),
                           'time_start': times[0] if times else None, 'time_end': times[-1] if times else None,
                           'expected_messages': None, 'coverage_ratio': None, 'reader_state': reader,
                           'unknown_type_messages': [mid for mid, m in records if m['type'] == 'unknown'],
                           'unknown_sender_messages': [mid for mid, m in records if m['sender'] == 'unknown'],
                           'phone_server_history': 'unverified'},
            'media': {'required': sum(m['type'] in MEDIA_TYPES for _, m in records), **dict(media),
                      'unclassified_messages': sum(m['type'] == 'unknown' for _, m in records),
                      'pending_messages': [{'id': mid, 'reason': e['media_reason'], 'recoverable': True}
                                           for mid, e in evidence.items() if e['media'] in ('pending', 'thumbnail_only')]},
            'visual': {'total': len(records), **dict(visual),
                       'policy': screenshot_policy(archive.config), 'required_messages': len(required),
                       'required_pending': sum(evidence[mid]['visual'] != 'confirmed_full' for mid in required),
                       'optional_messages': len(records) - len(required),
                       'full_coverage_ratio': visual['confirmed_full'] / len(records) if records else None,
                       'uncovered_plain_text': sum(e['plain_text'] and e['visual'] != 'confirmed_full' for e in evidence.values()),
                       'message_evidence': evidence},
            'local': {'status': 'consistent' if integrity == 'ok' and not (bad or missing or mismatch or duplicates) else 'inconsistent',
                      'sqlite_integrity': integrity, 'tracked_files': len(files), 'bad_files': bad,
                      'missing_references': missing, 'export_mismatches': mismatch, 'duplicate_export_ids': duplicates},
            'cloud': {'enabled': archive.config.get('upload_enabled', False), 'tracked_files': len(files),
                      'eligible_files': len(eligible), 'local_only_files': len(files) - len(eligible), 'local_only_reasons': dict(exclusions),
                      'verified_files': verified, 'pending_files': len(eligible) - verified,
                      'error_files': sum(bool(r['error']) for r in eligible),
                      'verification': 'last_upload_md5_size', 'connector_retrieval': 'unverified'}}
