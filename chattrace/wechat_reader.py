"""Single-contact reader for Windows WeChat 4.1.15.56; no UI or send operations."""
import base64
import hashlib
import io
import json
import re
import struct
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from itertools import chain
from pathlib import Path

import zstandard
from PIL import Image
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .core import ArchiveError, atomic, digest, encode, now, tz
from .wechat_snapshot import Snapshot
from .vendor.packed_info_data_img2_pb2 import PackedInfoDataImg2
from .vendor.packed_info_data_img_pb2 import PackedInfoDataImg


def collect(config, archive):
    from .wechat_access import check_live
    check_live(config)
    source = Snapshot(config['wechat_secret_path'])
    if source.account != config['account_id']:
        raise ArchiveError('WECHAT_ACCOUNT_IDENTITY_MISMATCH')
    contact = resolve_contact(source, config)
    archive.db.execute('CREATE TABLE IF NOT EXISTS reader_state (source TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    saved = archive.db.execute('SELECT payload FROM reader_state WHERE source=?', ('wechat-db-v4',)).fetchone()
    state = json.loads(saved[0]) if saved else {}
    pending = []
    for row in archive.db.execute('SELECT payload FROM messages'):
        message = json.loads(row[0])
        if message['source'] == 'wechat-db-v4' and message['needs_review']:
            pending.append(message['source_id'])
    rows = read_rows(source, contact['username'], state, pending, config)
    base = Path(config['inbox'])
    base.mkdir(parents=True, exist_ok=True)
    resolver = MediaResolver(source, contact['username'], base, config)
    try:
        messages = [convert_message(row, source.account, contact['username'], resolver, config) for row in rows]
    finally:
        resolver.close()
    if not rows:
        if not state:
            raise ArchiveError('TARGET_MESSAGES_NOT_LOCAL')
        return None, state
    payload_hash = digest(encode(messages).encode())
    new_state = {'watermark_time': max(state.get('watermark_time', 0), max(r['create_time'] for r in rows)),
                 'initial_start_time': state.get('initial_start_time', min(r['create_time'] for r in rows[-config.get('initial_message_limit', 10):])),
                 'payload_hash': payload_hash, 'last_success': now(), 'version': source.secret['installed_version']}
    if config.get('backfill'):
        new_state['initial_start_time'] = min(state.get('initial_start_time', rows[0]['create_time']),
                                            min(r['create_time'] for r in rows))
    if payload_hash == state.get('payload_hash'):
        return None, new_state
    batch = {'schema': 1, 'account_id': config['account_id'], 'contact_id': config['contact_id'],
             'source': 'wechat-db-v4', 'captured_at': now(), 'messages': messages, 'screenshots': []}
    path = base / ('batch-' + payload_hash + '.json')
    if not path.exists():
        atomic(path, encode(batch).encode())
    return path, new_state


def resolve_contact(source, config):
    db = source.open('contact/contact.db')
    try:
        rows = db.execute('SELECT username,alias,remark,nick_name FROM contact WHERE username=? OR alias=?',
                          (config['contact_id'], config['contact_id'])).fetchall()
        if len(rows) != 1 or rows[0]['username'].endswith('@chatroom'):
            raise ArchiveError('TARGET_CONTACT_NOT_UNIQUE_OR_NOT_LOCAL')
        target = dict(rows[0])
        if target['username'] != config['contact_username']:
            raise ArchiveError('TARGET_CONTACT_ID_CHANGED')
        return target
    finally:
        db.close()


def read_rows(source, username, state, pending, config):
    table = 'Msg_' + hashlib.md5(username.encode()).hexdigest()
    limit = config.get('initial_message_limit', 10)
    results = []
    for path in sorted((source.root / 'db_storage/message').glob('message_*.db')):
        if not re.fullmatch(r'message_\d+\.db', path.name):
            continue
        db = source.open('message/' + path.name)
        try:
            if not db.execute('SELECT 1 FROM sqlite_master WHERE type=? AND name=?', ('table', table)).fetchone():
                continue
            sql = f'SELECT m.*,n.user_name AS sender_username FROM "{table}" m LEFT JOIN Name2Id n ON m.real_sender_id=n.rowid'
            if config.get('backfill'):
                found = db.execute(sql + ' ORDER BY sort_seq,local_id').fetchall()
            elif state:
                since = max(state['initial_start_time'], state['watermark_time'] - config.get('overlap_hours', 72) * 3600)
                found = db.execute(sql + ' WHERE create_time>=? ORDER BY sort_seq,local_id', (since,)).fetchall()
                for sid in pending:
                    if sid.startswith('server:'):
                        found += db.execute(sql + ' WHERE server_id=?', (int(sid.split(':')[1]),)).fetchall()
                    elif sid.startswith(path.name + ':'):
                        found += db.execute(sql + ' WHERE local_id=?', (int(sid.split(':')[1]),)).fetchall()
            else:
                found = db.execute(sql + ' ORDER BY sort_seq DESC,local_id DESC LIMIT ?', (limit,)).fetchall()
                for kind in (3, 47):
                    found += db.execute(sql + ' WHERE local_type=? ORDER BY sort_seq DESC LIMIT ?',
                                        (kind, config.get('initial_media_samples', 0))).fetchall()
            results.extend([{**dict(row), 'shard': path.name} for row in found])
        finally:
            db.close()
    unique = {(r['shard'], r['local_id']): r for r in results}
    results = sorted(unique.values(), key=lambda r: (r['sort_seq'], r['local_id'], r['shard']))
    if not config.get('backfill') and not state and len(results) > limit + 2 * config.get('initial_media_samples', 0):
        results = results[-(limit + 2 * config.get('initial_media_samples', 0)):]
    return results


def decode_content(value):
    if value is None:
        return '', None
    if isinstance(value, str):
        return value, None
    raw = value
    try:
        if raw.startswith(b'\x28\xb5\x2f\xfd'):
            raw = zstandard.ZstdDecompressor().decompress(raw, max_output_size=64 * 1024 * 1024)
        return raw.decode('utf-8'), None
    except (UnicodeDecodeError, zstandard.ZstdError):
        return '', base64.b64encode(value).decode()


def convert_message(row, account, contact, resolver, config):
    content, binary = decode_content(row['message_content'])
    if not content and not binary and row.get('compress_content'):
        content, binary = decode_content(row['compress_content'])
    kind = {1: 'text', 3: 'image', 47: 'sticker', 34: 'voice', 43: 'video',
            10000: 'system', 244813135921: 'quote', 25769803825: 'file'}.get(row['local_type'], 'unknown')
    sender = 'self' if row['sender_username'] == account else 'friend' if row['sender_username'] == contact else 'unknown'
    if kind == 'system':
        sender = 'system'
    metadata = {k: row[k] for k in ('shard', 'local_id', 'local_type', 'status', 'sender_username')}
    metadata['media_status'] = 'not_applicable'
    message = {'source_id': 'server:' + str(row['server_id']) if row['server_id'] else row['shard'] + ':' + str(row['local_id']),
               'timestamp': datetime.fromtimestamp(row['create_time'], timezone.utc).isoformat(), 'precision': 'second',
               'sender': sender, 'type': kind, 'text': content if kind in ('text', 'system') else '',
               'sequence': row['sort_seq'], 'occurrence': row['local_id'], 'media': [], 'screenshots': [],
               'reply_to': None, 'source_metadata': metadata}
    if binary:
        message['raw_binary_base64'] = binary
        message['type'] = 'unknown'
    if kind == 'text':
        return message
    try:
        xml = ET.fromstring(content)
    except ET.ParseError:
        xml = None
    if kind == 'quote' and xml is not None:
        message['text'] = xml.findtext('.//appmsg/title', '')
        quoted = xml.find('.//refermsg')
        if quoted is not None:
            message['quoted_text'] = quoted.findtext('content', '')
            sid = quoted.findtext('svrid')
            message['reply_to'] = 'server:' + sid if sid and sid != '0' else None
    if kind in ('image', 'sticker', 'video', 'file', 'voice'):
        try:
            media, status = resolver.resolve(kind, row, xml)
            message['media'] = media
            metadata['media_status'] = status
        except Exception as exc:
            metadata['media_status'] = 'unavailable:' + (str(exc) if isinstance(exc, ArchiveError) else type(exc).__name__)
    if xml is not None:
        for node in xml.iter():
            for key in list(node.attrib):
                if any(word in key.lower() for word in ('aeskey', 'aes_key', 'auth', 'url', 'fileid')):
                    del node.attrib[key]
            if any(word in node.tag.lower() for word in ('aeskey', 'aes_key', 'authkey', 'fileid', 'sechashinfo')):
                node.text = None
        message['raw_content'] = ET.tostring(xml, encoding='unicode')
    else:
        message['raw_content'] = content
    return message


class MediaResolver:
    def __init__(self, source, contact, base, config):
        self.source, self.base, self.config = source, base, config
        self.attach = source.root / 'msg/attach' / hashlib.md5(contact.encode()).hexdigest()
        self.emoticons = None

    def close(self):
        if self.emoticons is not None:
            self.emoticons.close()

    def resolve(self, kind, row, xml):
        if kind == 'image':
            packed = PackedInfoDataImg2()
            try:
                packed.ParseFromString(row['packed_info_data'] or b'')
                name = packed.imageInfo.filename.strip().strip('"')
            except Exception:
                name = ''
            if not name:
                old = PackedInfoDataImg()
                old.ParseFromString(row['packed_info_data'] or b'')
                name = old.filename.strip().strip('"') or f"{row['local_id']}_{row['create_time']}"
            if not re.fullmatch(r'[a-zA-Z0-9_-]+', name):
                raise ArchiveError('MEDIA_FILENAME_UNSUPPORTED')
            month = datetime.fromtimestamp(row['create_time'], tz(self.config['timezone'])).strftime('%Y-%m')
            folder = self.attach / month / 'Img'
            candidates = [(folder / (name + suffix), status) for suffix, status in
                          (('_h.dat', 'local_hd'), ('.dat', 'local_image'), ('_W.dat', 'local_image'), ('_t.dat', 'thumbnail'))]
            for path, status in candidates:
                if path.exists():
                    data = decode_image(path.read_bytes(), self.source.secret['cfg_dword'], self.source.account)
                    if data.startswith(b'wxgf'):
                        import av
                        original = 'assets/' + digest(data) + '.wxgf'
                        atomic(self.base / original, data)
                        start = data.find(b'\x00\x00\x00\x01')
                        if start < 0:
                            raise ArchiveError('WXGF_STREAM_NOT_FOUND')
                        with av.open(io.BytesIO(data[start:]), format='hevc') as container:
                            frame = next(container.decode(video=0))
                            rendered = io.BytesIO()
                            frame.to_image().save(rendered, format='PNG')
                        return [{'kind': 'image_original_wxgf', 'path': original},
                                self.save_image(rendered.getvalue(), 'image_preview')], status + '_wxgf_with_png_preview'
                    return [self.save_image(data, kind)], status
            return [], 'not_downloaded_locally'
        if kind == 'sticker' and xml is not None:
            node = xml.find('.//emoji')
            md5 = node.get('md5', '') if node is not None else ''
            if not re.fullmatch(r'[0-9a-fA-F]{32}', md5):
                return [], 'sticker_md5_missing'
            cached = self.base / 'media-cache' / (md5 + '.bin')
            if cached.exists():
                return [self.save_image(cached.read_bytes(), kind)], 'downloaded_verified'
            month = datetime.fromtimestamp(row['create_time'], tz(self.config['timezone'])).strftime('%Y-%m')
            cache_root = self.source.root / 'cache'
            relative = Path('Emoticon') / md5[:2] / md5
            preferred = cache_root / month / relative
            alternatives = (p for p in cache_root.glob('*/' + relative.as_posix()) if p != preferred)
            for local in chain((preferred,), alternatives):
                if not local.is_file():
                    continue
                try:
                    data = decode_sticker_cache(local.read_bytes(), self.source.secret['cfg_dword'], self.source.account, md5)
                    asset = self.save_image(data, kind)
                except (ArchiveError, OSError, SyntaxError):
                    pass  # A stale cache must not prevent the existing CDN fallback.
                else:
                    return [asset], 'local_cache_verified'
            if not self.config.get('download_stickers', True):
                return [], 'network_media_disabled'
            if self.emoticons is None:
                self.emoticons = self.source.open('emoticon/emoticon.db')
            row = self.emoticons.execute('SELECT cdn_url FROM kNonStoreEmoticonTable WHERE md5=?', (md5,)).fetchone()
            url = row['cdn_url'] if row and row['cdn_url'] else node.get('cdnurl', '')
            data = fetch_sticker(url)
            if hashlib.md5(data).hexdigest().lower() != md5.lower():
                raise ArchiveError('STICKER_MD5_MISMATCH')
            asset = self.save_image(data, kind)
            atomic(cached, data)
            return [asset], 'downloaded_verified'
        return [], 'media_type_not_implemented'

    def save_image(self, data, kind):
        with Image.open(io.BytesIO(data)) as picture:
            fmt = picture.format
            picture.verify()
        suffix = {'JPEG': '.jpg', 'PNG': '.png', 'GIF': '.gif', 'WEBP': '.webp', 'BMP': '.bmp'}.get(fmt)
        if suffix is None:
            raise ArchiveError('MEDIA_FORMAT_UNSUPPORTED')
        relative = 'assets/' + digest(data) + suffix
        if not (self.base / relative).exists():
            atomic(self.base / relative, data)
        return {'kind': kind, 'path': relative}


def decode_image(data, cfg, account):
    if data[:6] == b'\x07\x08V2\x08\x07':
        aes_size, xor_size = struct.unpack_from('<II', data, 6)
        encrypted_size = (aes_size // 16 + 1) * 16
        if 15 + encrypted_size + xor_size > len(data):
            raise ArchiveError('IMAGE_CONTAINER_TRUNCATED')
        key = hashlib.md5((str(cfg) + account).encode()).hexdigest()[:16].encode()
        decryptor = Cipher(algorithms.AES(key), modes.ECB()).decryptor()
        first = decryptor.update(data[15:15 + encrypted_size]) + decryptor.finalize()
        padding = first[-1]
        if not 1 <= padding <= 16 or first[-padding:] != bytes([padding]) * padding:
            raise ArchiveError('IMAGE_KEY_NOT_COMPATIBLE')
        tail_start = len(data) - xor_size
        return first[:-padding] + data[15 + encrypted_size:tail_start] + bytes(v ^ (cfg & 255) for v in data[tail_start:])
    if data.startswith((b'\xff\xd8\xff', b'\x89PNG', b'GIF8', b'RIFF')):
        return data
    raise ArchiveError('IMAGE_FORMAT_NOT_COMPATIBLE')


def decode_sticker_cache(data, cfg, account, expected_md5):
    if not data or len(data) % 16:
        raise ArchiveError('STICKER_CACHE_INVALID_SIZE')
    key = hashlib.md5((str(cfg) + account + 'EMOTICON').encode()).digest()
    decryptor = Cipher(algorithms.AES(key), modes.CBC(key)).decryptor()
    decoded = decryptor.update(data) + decryptor.finalize()
    padding = decoded[-1]
    if not 1 <= padding <= 16 or decoded[-padding:] != bytes([padding]) * padding:
        raise ArchiveError('STICKER_CACHE_INVALID_PADDING')
    decoded = decoded[:-padding]
    if hashlib.md5(decoded).hexdigest() != expected_md5.lower():
        raise ArchiveError('STICKER_MD5_MISMATCH')
    return decoded


def fetch_sticker(url):
    parsed = urllib.parse.urlsplit(url)
    hostname = parsed.hostname or ''
    if parsed.scheme not in ('http', 'https') or not (hostname.endswith('.qq.com') or hostname.endswith('.qpic.cn')):
        raise ArchiveError('STICKER_SOURCE_UNAVAILABLE')
    # Do not follow a redirect to an unrelated service or expose its response in logs.
    class NoRedirect(urllib.request.HTTPRedirectHandler):
        def redirect_request(self, *args, **kwargs):
            return None
    opener = urllib.request.build_opener(NoRedirect())
    for attempt in range(3):
        try:
            with opener.open(url, timeout=15) as response:
                data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024:
                    raise ArchiveError('STICKER_TOO_LARGE')
                return data
        except (OSError, TimeoutError):
            if attempt == 2:
                raise ArchiveError('STICKER_DOWNLOAD_FAILED') from None
