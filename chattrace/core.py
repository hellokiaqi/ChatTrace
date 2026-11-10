"""Durable import, materialization and upload queue; no WeChat internals."""
import hashlib
import html
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo


class ArchiveError(Exception):
    pass


def now():
    return datetime.now(timezone.utc).isoformat()


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(',', ':'))


def digest(data):
    return hashlib.sha256(data).hexdigest()


def atomic(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temp = path.with_name(path.name + '.tmp')
    with temp.open('wb') as stream:
        stream.write(data)
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temp, path)


def tz(name):
    # Windows Python may lack IANA data; China has no DST in supported modern records.
    if name == 'Asia/Shanghai':
        return timezone(timedelta(hours=8), name)
    return ZoneInfo(name)


def timestamp(value):
    try:
        result = datetime.fromisoformat(value)
        if result.utcoffset() is None:
            raise ValueError()
        return result
    except (TypeError, ValueError):
        raise ArchiveError('INVALID_TIMESTAMP') from None


def load_config(path):
    path = Path(path).resolve()
    data = json.loads(path.read_text(encoding='utf-8-sig'))
    for key in ('account_id', 'contact_id', 'contact_name', 'archive_root', 'inbox'):
        if not isinstance(data.get(key), str) or not data[key].strip():
            raise ArchiveError('CONFIG_REQUIRED_' + key.upper())
    if data.get('identity_confirmed') is not True:
        raise ArchiveError('IDENTITY_NOT_CONFIRMED')
    if data.get('reader') not in ('import', 'wechat-ui', 'wechat-db', 'qq-export','qq-db'):
        raise ArchiveError('UNKNOWN_READER')
    if data['reader'] == 'wechat-db':
        if not all(isinstance(data.get(k), str) and data[k] for k in ('wechat_secret_path', 'contact_username')):
            raise ArchiveError('WECHAT_SETUP_REQUIRED')
        for key, default in (('initial_message_limit', 10), ('overlap_hours', 72), ('initial_media_samples', 0)):
            data.setdefault(key, default)
            if type(data[key]) is not int or data[key] < (0 if key == 'initial_media_samples' else 1):
                raise ArchiveError('INVALID_READER_RANGE')
    if data['reader']=='qq-export' and (not isinstance(data.get('qq_export_path'),str) or not data['qq_export_path'] or data.get('auto_capture')):
        raise ArchiveError('QQ_EXPORT_CONFIGURATION_REQUIRED')
    if data['reader']=='qq-db' and (not all(data.get(k) for k in ('qq_account_dir','qq_secret_path')) or data.get('auto_capture')):
        raise ArchiveError('QQ_DB_CONFIGURATION_REQUIRED')
    tz(data.get('timezone', 'Asia/Shanghai'))
    if data.get('screenshot_policy', 'media') not in ('media', 'all', 'manual'):
        raise ArchiveError('INVALID_SCREENSHOT_POLICY')
    if type(data.get('upload_revisions', False)) is not bool:
        raise ArchiveError('INVALID_UPLOAD_REVISIONS')
    for key in ('qq_secret_path','qq_account_dir','qq_export_path','archive_root', 'inbox', 'token_path', 'client_secret_path', 'wechat_secret_path'):
        if data.get(key):
            data[key] = str((path.parent / data[key]).resolve())
    data['config_path'] = str(path)
    return data


@contextmanager
def exclusive(root):
    root = Path(root)
    root.mkdir(parents=True, exist_ok=True)
    with (root / '.lock').open('a+b') as handle:
        if os.fstat(handle.fileno()).st_size == 0:
            handle.write(b'0')
            handle.flush()
        handle.seek(0)
        try:
            if os.name == 'nt':
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise ArchiveError('ALREADY_RUNNING') from None
        try:
            yield
        finally:
            handle.seek(0)
            if os.name == 'nt':
                msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
            else:
                fcntl.flock(handle, fcntl.LOCK_UN)


class Archive:
    def __init__(self, config):
        self.config = config
        self.root = Path(config['archive_root'])
        self.root.mkdir(parents=True, exist_ok=True)
        marker = self.root / '.chattrace.json'
        identity = {k: config[k] for k in ('account_id', 'contact_id')}
        if config.get('reader') in ('qq-export','qq-db'):
            identity['platform']='qq'
        if config.get('reader')=='qq-db':
            identity['reader']='qq-db'
        if marker.exists():
            if json.loads(marker.read_text('utf-8')) != identity:
                raise ArchiveError('ARCHIVE_IDENTITY_MISMATCH')
        else:
            if any(p.name != '.lock' for p in self.root.iterdir()):
                raise ArchiveError('ARCHIVE_DIRECTORY_NOT_EMPTY')
            atomic(marker, encode(identity).encode())
        self.db = sqlite3.connect(self.root / 'state.sqlite')
        self.db.row_factory = sqlite3.Row
        self.db.execute('PRAGMA journal_mode=WAL')
        self.db.execute('PRAGMA synchronous=FULL')
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS messages (
                id INTEGER PRIMARY KEY, identity TEXT UNIQUE, payload TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS revisions (
                message_id INTEGER, hash TEXT, payload TEXT, captured TEXT,
                UNIQUE(message_id, hash));
            CREATE TABLE IF NOT EXISTS batches (hash TEXT PRIMARY KEY, captured TEXT);
            CREATE TABLE IF NOT EXISTS inputs (path TEXT PRIMARY KEY, size INTEGER, modified INTEGER);
            CREATE TABLE IF NOT EXISTS observations (identity TEXT PRIMARY KEY, observed TEXT);
            CREATE TABLE IF NOT EXISTS files (
                path TEXT PRIMARY KEY, sha256 TEXT NOT NULL, uploaded TEXT,
                remote_id TEXT, error TEXT);
            CREATE TABLE IF NOT EXISTS folders (path TEXT PRIMARY KEY, remote_id TEXT);
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started TEXT, ended TEXT, status TEXT,
                messages INTEGER DEFAULT 0, screenshots INTEGER DEFAULT 0,
                uploads INTEGER DEFAULT 0, error TEXT);
        ''')
        columns = {r[1] for r in self.db.execute('PRAGMA table_info(runs)')}
        for name, declaration in (('capture_pending', 'INTEGER DEFAULT 0'), ('media_pending', 'INTEGER DEFAULT 0'), ('capture_error', 'TEXT'), ('coverage_json', 'TEXT')):
            if name not in columns:
                self.db.execute(f'ALTER TABLE runs ADD COLUMN {name} {declaration}')

    def close(self):
        self.db.close()

    def ingest(self, batch_path):
        batch_path = Path(batch_path).resolve()
        stat = batch_path.stat()
        seen = self.db.execute('SELECT size,modified FROM inputs WHERE path=?', (str(batch_path),)).fetchone()
        if seen and (seen['size'], seen['modified']) == (stat.st_size, stat.st_mtime_ns):
            return 0
        raw = Path(batch_path).read_bytes()
        batch_hash = digest(raw)
        if self.db.execute('SELECT 1 FROM batches WHERE hash=?', (batch_hash,)).fetchone():
            with self.db:
                self.db.execute('INSERT OR REPLACE INTO inputs VALUES (?,?,?)', (str(batch_path), stat.st_size, stat.st_mtime_ns))
            return 0
        batch = json.loads(raw)
        prepared, assets = normalize(batch, Path(batch_path).parent, self.config)
        # Copy assets before the transaction. Unreferenced files are harmless after interruption.
        for relative, content in assets.items():
            destination = self.root / relative
            if destination.exists() and destination.read_bytes() != content:
                raise ArchiveError('ASSET_CONFLICT')
            if not destination.exists():
                atomic(destination, content)
        changed = 0
        with self.db:
            for identity, message in prepared:
                row = self.db.execute('SELECT id,payload FROM messages WHERE identity=?', (identity,)).fetchone()
                observed = timestamp(batch['captured_at']).astimezone(timezone.utc).isoformat()
                previous = self.db.execute('SELECT observed FROM observations WHERE identity=?', (identity,)).fetchone()
                stale = previous is not None and timestamp(observed) < timestamp(previous['observed'])
                if not stale:
                    self.db.execute('INSERT OR REPLACE INTO observations VALUES (?,?)', (identity, observed))
                payload = encode(message)
                if row:
                    old = json.loads(row['payload'])
                    # Acquisition metadata alone must not create a new message revision.
                    message['collected_at'] = old['collected_at']
                    message['screenshots'] = sorted(set(old['screenshots'] + message['screenshots']))
                    if old.get('screenshot_metadata') or message.get('screenshot_metadata'):
                        message['screenshot_metadata'] = {**old.get('screenshot_metadata', {}), **message.get('screenshot_metadata', {})}
                    # A transient media read failure must not discard an already archived resource.
                    if old['media'] and message['type'] == old['type'] and message['type'] in ('image', 'sticker', 'voice', 'video', 'file'):
                        status = message.get('source_metadata', {}).get('media_status', '')
                        previous_status = old.get('source_metadata', {}).get('media_status', '')
                        message['media'] = list({(a['kind'], a['path']): a for a in old['media'] + message['media']}.values())
                        if status.startswith('unavailable:') or (status == 'thumbnail' and previous_status != 'thumbnail'):
                            message.setdefault('source_metadata', {})['media_retry_status'] = status
                            message['source_metadata']['media_status'] = previous_status
                            message['needs_review'] = True
                    payload = encode(message)
                    if payload == row['payload']:
                        continue
                    mid = row['id']
                    if not stale:
                        self.db.execute('UPDATE messages SET payload=? WHERE id=?', (payload, mid))
                else:
                    mid = self.db.execute('INSERT INTO messages(identity,payload) VALUES (?,?)', (identity, payload)).lastrowid
                cursor = self.db.execute('INSERT OR IGNORE INTO revisions VALUES (?,?,?,?)',
                                         (mid, digest(payload.encode()), payload, batch['captured_at']))
                changed += cursor.rowcount
            self.db.execute('INSERT INTO batches VALUES (?,?)', (batch_hash, batch['captured_at']))
            self.db.execute('INSERT OR REPLACE INTO inputs VALUES (?,?,?)', (str(batch_path), stat.st_size, stat.st_mtime_ns))
        return changed

    def materialize(self):
        if any((self.root.parent / name).exists() for name in ('native-state.json', 'cloud-results.json')):
            raise ArchiveError('FROZEN_ARCHIVE')
        from .coverage import message_evidence, partial_screenshot_index
        from .retrieval_semantics import original_block, resource_record, daily_intro
        days = {}
        current_paths = {}
        for row in self.db.execute('SELECT id,payload FROM messages'):
            message = json.loads(row['payload'])
            message['id'] = f"M{row['id']:06d}"
            message['archive_status'] = 'local_committed'
            days.setdefault(message['date'], []).append(message)
            day = message['date']
            current_paths[row['id']] = f'{day[:4]}/{day[5:7]}/{day}/messages.jsonl'
        current = [m for messages in days.values() for m in messages]
        partials = partial_screenshot_index([(m['id'], m) for m in current])
        evidence = {m['id']: message_evidence(self.root, m['id'], m, partials.get(m['id'], ())) for m in current}
        all_screenshot_metadata = {p: meta for m in current for p, meta in m.get('screenshot_metadata', {}).items()}
        source_counts = {day: len(messages) for day, messages in days.items()}
        namespace = digest(encode([self.config['account_id'], self.config['contact_id']]).encode())[:16]
        resources = {}
        for m in current:
            for asset in m['media']:
                if asset['path'] not in resources:
                    resources[asset['path']] = resource_record(self, asset['path'], asset['kind'])
            for p in set(m['screenshots'] + partials.get(m['id'], [])):
                if p not in resources:
                    resources[p] = resource_record(self, p, 'screenshot')
        history = {}
        for row in self.db.execute('SELECT message_id,hash,payload,captured FROM revisions'):
            original = json.loads(row['payload'])
            day = original['date']
            days.setdefault(day, [])
            history.setdefault(day, []).append({'id': f"M{row['message_id']:06d}", 'revision': row['hash'],
                                                'record_kind': 'revision_history', 'use_for_current_conversation': False,
                                                'current_text_source': current_paths[row['message_id']],
                                                'captured_at': row['captured'], 'message': original})
        if not days:
            for row in self.db.execute('SELECT captured FROM batches'):
                days.setdefault(timestamp(row['captured']).astimezone(tz(self.config.get('timezone', 'Asia/Shanghai'))).date().isoformat(), [])
        files = {}
        index = ['# ChatTrace 当前日归档入口', '', f"归档命名空间：{namespace}；角色：{self.config.get('drive_contact_name', 'canonical_daily')}；格式版本：2。",
                 '以下只索引当前消息的每日记录。修订历史保留本地，默认不上传；旧检索试验不是本入口的数据来源。',
                 '来源覆盖完整性：未验证。消息时间未知时按采集日期归档。', '']
        for day, messages in sorted(days.items()):
            messages.sort(key=lambda m: (m['timestamp'] or '', m['sequence'], m['id']))
            prefix = f"{day[:4]}/{day[5:7]}/{day}"
            lines = ['# 与 ' + html.escape(self.config['contact_name']) + ' 的聊天记录', '',
                     f'归档命名空间：{namespace}；[结构化原文](messages.jsonl)；[资源与覆盖清单](manifest.json)。', '']
            intro, metrics = daily_intro(self.config, day, messages, evidence, source_counts.get(day, 0))
            lines += intro
            screenshots = {}
            screenshot_metadata = all_screenshot_metadata
            day_assets = set()
            message_states = []
            for message in messages:
                mid = message['id']
                time_label = '时间未知'
                if message['timestamp']:
                    local_time = timestamp(message['timestamp']).astimezone(tz(self.config.get('timezone', 'Asia/Shanghai')))
                    time_label = local_time.date().isoformat() if message['precision'] == 'day' else local_time.isoformat(timespec='minutes' if message['precision'] == 'minute' else 'seconds')
                lines += [f'<a id="{mid}"></a>', f"## {mid} · {time_label}", '',
                          f"**{ {'self':'我','friend':'好友','system':'系统','unknown':'未知'}[message['sender']]}** · {message['type']} · 精度 {message['precision']}", '',
                          f"归档ID：{namespace}:{mid}；来源ID：{message['source_id']}；sequence：{message['sequence']}。",
                          original_block(message['text']), '']
                e = evidence[mid]
                lines += [f"消息证据状态：视觉 {e['visual']}；媒体 {e['media']}。", '']
                message_states.append({'id': mid, 'visual': e['visual'], 'media': e['media'], 'reply_to': message['reply_to']})
                if message['type'] in ('sticker', 'image', 'voice', 'video', 'file'):
                    label = {'sticker': '表情包', 'image': '图片', 'voice': '语音', 'video': '视频', 'file': '文件'}[message['type']]
                    lines += [f"[{label}：{'已关联媒体文件' if message['media'] else '未取得原始媒体，参见可用截图'}]", '']
                for asset in message['media']:
                    item = resources[asset['path']]
                    day_assets.add(asset['path'])
                    lines += [f"[媒体 {item['resource_id']} · {item['role']} · {asset['kind']}]({item['url'] or '../../../' + asset['path']})", '']
                for shot in sorted(set(message['screenshots'] + e['partial_screenshots'])):
                    item = resources[shot]
                    day_assets.add(shot)
                    state = 'confirmed_full' if shot in e['confirmed_screenshots'] else 'partial_only' if shot in e['partial_screenshots'] else 'linked_unverified'
                    lines += [f"[截图 {item['resource_id']} · 本条 {state}]({item['url'] or '../../../' + shot})", '']
                    screenshots.setdefault(shot, []).append({'id': mid, 'timestamp': message['timestamp'], 'sender': message['sender']})
                lines += ['显式引用来源 ID：' + (message['reply_to'] or 'null（未记录显式引用）'), '']
                if message.get('quoted_text'):
                    lines += [original_block(message['quoted_text'], '被引用原文'), '']
                if message['needs_review']:
                    lines += ['**待核对：类型、时间、发送者或原始媒体不完整。**', '']
            files[prefix + '/conversation.md'] = '\n'.join(lines).encode('utf-8')
            files[prefix + '/messages.jsonl'] = ('\n'.join(encode(m) for m in messages) + '\n').encode('utf-8')
            files[prefix + '/revisions.jsonl'] = ('\n'.join(encode(m) for m in history.get(day, [])) + '\n').encode('utf-8')
            shots = []
            for p, ids in sorted(screenshots.items()):
                meta = screenshot_metadata.get(p, {})
                raw_candidates = list((Path(self.config['inbox']) / 'captures').glob(Path(p).stem + '-original.*'))
                raw_available = bool(meta.get('original_sha256') and any(digest(raw.read_bytes()) == meta['original_sha256'] for raw in raw_candidates))
                shots.append({**resources[p], 'screenshot_id': Path(p).stem, 'messages': ids, 'metadata': meta,
                    'confirmed_message_ids': [m['id'] for m in ids if p in evidence[m['id']]['confirmed_screenshots']],
                    'partial_message_ids': sorted(set(meta.get('partially_visible_message_ids', [])) | {m['id'] for m in ids if p in evidence[m['id']]['partial_screenshots']}),
                    'unverified_message_ids': [m['id'] for m in ids if p in evidence[m['id']]['unverified_screenshots']],
                    'capture_original': {'role': 'original_screenshot', 'sha256': meta.get('original_sha256'),
                        'dimensions': meta.get('original_size'), 'storage': 'local_capture_inbox' if raw_available else 'not_located',
                        'available_locally': raw_available,
                        'drive_file_id': None}})
            manifest = {'schema': 2, 'date': day, 'timezone': self.config.get('timezone', 'Asia/Shanghai'),
                        'namespace': namespace, 'archive_role': self.config.get('drive_contact_name', 'canonical_daily'),
                        'coverage': 'unverified', 'messages': [m['id'] for m in messages], 'metrics': metrics,
                        'message_evidence': message_states, 'resources': [resources[p] for p in sorted(day_assets)], 'screenshots': shots}
            files[prefix + '/manifest.json'] = encode(manifest).encode('utf-8')
            index += [f'- [{day}]({prefix}/conversation.md) · {len(messages)} 条']
        files['index.md'] = ('\n'.join(index) + '\n').encode('utf-8')
        assets = set()
        historical = [[entry['message'] for entries in history.values() for entry in entries]]
        for messages in list(days.values()) + historical:
            for message in messages:
                assets.update(message['screenshots'])
                assets.update(a['path'] for a in message['media'])
        for path in assets:
            files[path] = (self.root / path).read_bytes()
        for relative, data in files.items():
            self.write_derived(relative, data)

    def pending(self):
        return [dict(r) for r in self.db.execute('SELECT * FROM files WHERE uploaded IS NULL OR uploaded<>sha256 ORDER BY path')
                if upload_exclusion(self.config, r['path']) is None]

    def write_derived(self, relative, data):
        target = self.root / relative
        hash_ = digest(data)
        tracked = self.db.execute('SELECT sha256 FROM files WHERE path=?', (relative,)).fetchone()
        if target.exists() and digest(target.read_bytes()) != hash_:
            if not tracked or digest(target.read_bytes()) != tracked['sha256']:
                raise ArchiveError('OUTPUT_MODIFIED_EXTERNALLY')
        if not target.exists() or digest(target.read_bytes()) != hash_:
            atomic(target, data)
        with self.db:
            self.db.execute('INSERT INTO files(path,sha256) VALUES (?,?) ON CONFLICT(path) DO UPDATE SET sha256=excluded.sha256', (relative, hash_))

    def upload(self, drive):
        count = 0
        for row in self.pending():
            relative = row['path']
            try:
                if not row['remote_id']:
                    row['remote_id'] = drive.reserve_id()
                    with self.db:
                        self.db.execute('UPDATE files SET remote_id=? WHERE path=?', (row['remote_id'], relative))
                data = (self.root / relative).read_bytes()
                if digest(data) != row['sha256']:
                    raise ArchiveError('OUTPUT_MODIFIED_EXTERNALLY')
                drive.put(row['remote_id'], relative, data)
                if not drive.verify(row['remote_id'], data):
                    raise ArchiveError('REMOTE_CHECKSUM_MISMATCH')
                with self.db:
                    self.db.execute('UPDATE files SET uploaded=sha256,error=NULL WHERE path=?', (relative,))
                count += 1
            except Exception as exc:
                with self.db:
                    self.db.execute('UPDATE files SET error=? WHERE path=?', (safe_error(exc), relative))
                raise
        return count


def upload_exclusion(config, relative):
    if relative.startswith('retrieval/'):
        return 'local_retrieval_view'
    if re.fullmatch(r'\d{4}/\d{2}/\d{4}-\d{2}-\d{2}/revisions\.jsonl', relative):
        return None if config.get('upload_revisions', False) else 'revision_history'
    if relative == 'index.md' or re.fullmatch(r'\d{4}/\d{2}/\d{4}-\d{2}-\d{2}/(conversation\.md|messages\.jsonl|manifest\.json)', relative):
        return None
    if re.fullmatch(r'(media/A|screenshots/S)[0-9a-f]{64}\.[a-zA-Z0-9]+', relative):
        return None
    return 'not_archive_output'


def normalize(batch, base, config):
    if batch.get('schema') != 1 or batch.get('contact_id') != config['contact_id'] or batch.get('account_id') != config['account_id']:
        raise ArchiveError('BATCH_IDENTITY_MISMATCH')
    if config.get('reader')=='qq-db' and batch.get('source')!='qq-db':
        raise ArchiveError('QQ_BATCH_SOURCE_MISMATCH')
    captured = timestamp(batch.get('captured_at'))
    source = batch.get('source')
    if not isinstance(source, str) or not source:
        raise ArchiveError('SOURCE_REQUIRED')
    screenshots, assets, screenshot_metadata = {}, {}, {}

    def asset(path, category):
        if not isinstance(path, str):
            raise ArchiveError('INVALID_ASSET_PATH')
        local = (base / path).resolve()
        if not local.is_relative_to(base.resolve()) or not local.is_file():
            raise ArchiveError('ASSET_OUTSIDE_BATCH_OR_MISSING')
        data = local.read_bytes()
        suffix = local.suffix.lower()
        if category == 'screenshots':
            valid = (suffix == '.png' and data.startswith(b'\x89PNG\r\n\x1a\n')) or (suffix in ('.jpg', '.jpeg') and data.startswith(b'\xff\xd8\xff'))
            if not valid:
                raise ArchiveError('SCREENSHOT_FORMAT_MISMATCH')
        name = ('S' if category == 'screenshots' else 'A') + digest(data) + suffix
        relative = category + '/' + name
        assets[relative] = data
        return relative

    for shot in batch.get('screenshots', []):
        if shot['id'] in screenshots:
            raise ArchiveError('DUPLICATE_SCREENSHOT_ID')
        screenshots[shot['id']] = asset(shot['path'], 'screenshots')
        if shot.get('metadata'):
            if not isinstance(shot['metadata'], dict):
                raise ArchiveError('INVALID_SCREENSHOT_METADATA')
            screenshot_metadata[screenshots[shot['id']]] = shot['metadata']
    messages, identities = [], set()
    for item in batch['messages']:
        if not isinstance(item.get('text'), str) or item.get('sender') not in ('self', 'friend', 'system', 'unknown'):
            raise ArchiveError('INVALID_MESSAGE')
        if not isinstance(item.get('type'), str) or item.get('precision') not in ('second', 'minute', 'day', 'unknown'):
            raise ArchiveError('INVALID_MESSAGE_TYPE_OR_PRECISION')
        if type(item.get('sequence')) is not int or type(item.get('occurrence')) is not int:
            raise ArchiveError('STABLE_SEQUENCE_REQUIRED')
        time = timestamp(item['timestamp']) if item.get('timestamp') else None
        if (time is None) != (item['precision'] == 'unknown'):
            raise ArchiveError('TIMESTAMP_PRECISION_MISMATCH')
        source_id = item.get('source_id')
        if source_id is not None and (not isinstance(source_id, str) or not source_id):
            raise ArchiveError('INVALID_SOURCE_ID')
        canonical_time = time.astimezone(timezone.utc).isoformat() if time else None
        fields = [config['account_id'], config['contact_id'], source]
        fields += ['id', source_id] if source_id else ['fallback', canonical_time, item['precision'], item['sender'], item['type'], item['text'], item['occurrence']]
        identity = digest(encode(fields).encode())
        if identity in identities:
            raise ArchiveError('AMBIGUOUS_DUPLICATE_IN_BATCH')
        identities.add(identity)
        refs = item.get('screenshots', [])
        if any(ref not in screenshots for ref in refs):
            raise ArchiveError('SCREENSHOT_REFERENCE_MISSING')
        media = [{'kind': m['kind'], 'path': asset(m['path'], 'media')} for m in item.get('media', [])]
        known = ('text', 'image', 'sticker', 'voice', 'video', 'file', 'quote', 'system', 'recalled', 'emoji')
        message = dict(item)
        if screenshot_metadata:
            message['screenshot_metadata'] = {screenshots[ref]: screenshot_metadata[screenshots[ref]] for ref in refs if screenshots[ref] in screenshot_metadata}
        message.update(timestamp=canonical_time, date=(time or captured).astimezone(tz(config.get('timezone', 'Asia/Shanghai'))).date().isoformat(),
                       source_id=source_id, source=source, collected_at=batch['captured_at'],
                       screenshots=sorted(screenshots[ref] for ref in refs), media=media,
                       reply_to=item.get('reply_to'),
                       needs_review=(bool(item.get('source_metadata',{}).get('parse_incomplete') or item.get('source_metadata',{}).get('missing_media_parts')) or item['type'] not in known or time is None or item['sender'] == 'unknown' or
                                     (item['type'] in ('image', 'sticker', 'voice', 'video', 'file') and not media) or
                                     item.get('source_metadata', {}).get('media_status') == 'thumbnail'))
        messages.append((identity, message))
    return messages, assets


def safe_error(exc):
    # Never emit remote response bodies, OAuth tokens, file contents, or message text.
    if isinstance(exc, ArchiveError):
        return str(exc)
    return type(exc).__name__


def run(config, no_upload=False, drive_factory=None):
    with exclusive(config['archive_root']):
        return run_locked(config,no_upload,drive_factory)


def run_locked(config, no_upload=False, drive_factory=None):
    no_upload = no_upload or config.get('upload_enabled') is False
    archive = Archive(config)
    with archive.db:
        archive.db.execute("UPDATE runs SET status='interrupted',ended=?,error='PROCESS_INTERRUPTED' WHERE status='running'", (now(),))
        rid = archive.db.execute("INSERT INTO runs(started,status) VALUES (?,'running')", (now(),)).lastrowid
    try:
        if config['reader'] == 'wechat-ui':
            raise ArchiveError('WECHAT_UI_READER_UNAVAILABLE')
        before_max = archive.db.execute('SELECT coalesce(max(id),0) FROM messages').fetchone()[0]
        checkpoint = None
        if config['reader']=='qq-db':
            from .qq_native import collect as collect_db
            checkpoint=collect_db(config,archive)
        if config['reader']=='qq-export':
            from .qq_reader import collect as collect_qq
            collect_qq(config)
        if config['reader'] == 'wechat-db':
            from .wechat_reader import collect
            _, checkpoint = collect(config, archive)
        inbox = Path(config['inbox'])
        if not inbox.is_dir():
            raise ArchiveError('INBOX_NOT_FOUND')
        paths = sorted(inbox.glob('*.json'))
        if not paths and not archive.db.execute('SELECT 1 FROM batches LIMIT 1').fetchone():
            raise ArchiveError('NO_INPUT_BATCHES')
        for path in paths:
            count = archive.ingest(path)
            with archive.db:
                archive.db.execute('UPDATE runs SET messages=messages+? WHERE id=?', (count, rid))
        capture_error = None
        from .coverage import visual_required
        visual_events = [r['id'] for r in archive.db.execute('SELECT id,payload FROM messages WHERE id>?', (before_max,))
                         if visual_required(config, json.loads(r['payload']))]
        if config.get('auto_capture') and visual_events:
            try:
                from .screenshots import capture_series
                capture_series(config, archive, config.get('capture_pages', 3), config.get('screenshot_height', 1200))
                capture_error = 'SCREENSHOT_RANGES_NEED_REVIEW'
            except Exception as exc:
                capture_error = safe_error(exc)
        archive.materialize()
        if checkpoint is not None:
            with archive.db:
                archive.db.execute('INSERT OR REPLACE INTO reader_state VALUES (?,?)', ('qq-db' if config['reader']=='qq-db' else 'wechat-db-v4', encode(checkpoint)))
        shots = archive.db.execute("SELECT count(*) FROM files WHERE path LIKE 'screenshots/%'").fetchone()[0]
        uploads = 0
        if not no_upload:
            if drive_factory is None:
                from .drive import GoogleDrive
                drive_factory = GoogleDrive
            drive = drive_factory(config, archive)
            uploads = archive.upload(drive)
            archive.materialize()
            uploads += archive.upload(drive)
        from .coverage import report
        coverage = report(archive)
        capture_pending = coverage['visual']['required_pending']
        media_pending = len(coverage['media']['pending_messages'])
        status = 'local_only' if no_upload else 'synced'
        if media_pending or capture_pending:
            status = 'local_partial' if no_upload else 'synced_partial'
        with archive.db:
            archive.db.execute('UPDATE runs SET coverage_json=? WHERE id=?', (encode(coverage), rid))
            archive.db.execute('UPDATE runs SET ended=?,status=?,screenshots=?,uploads=?,capture_pending=?,media_pending=?,capture_error=? WHERE id=?',
                               (now(), status, shots, uploads, capture_pending, media_pending, capture_error, rid))
        return dict(archive.db.execute('SELECT * FROM runs WHERE id=?', (rid,)).fetchone())
    except Exception as exc:
        with archive.db:
            archive.db.execute("UPDATE runs SET ended=?,status='failed',error=? WHERE id=?", (now(), safe_error(exc), rid))
        raise
    finally:
        archive.close()
