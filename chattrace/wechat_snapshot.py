"""Read-only SQLCipher/WAL snapshots. Plain databases remain in memory."""
import hashlib
import hmac
import json
import sqlite3
import struct
from pathlib import Path

from .core import ArchiveError
from .drive import protect
from .vendor import wcdb_readonly as wc


class Snapshot:
    def __init__(self, secret_path):
        self.secret = json.loads(protect(Path(secret_path).read_bytes(), True))
        self.root = Path(self.secret['account_dir'])
        self.account = self.secret['wxid']
        if not self.account or not self.root.name.startswith(self.account + '_'):
            raise ArchiveError('WECHAT_ACCOUNT_IDENTITY_MISMATCH')
        self.used_keys = {}

    def open(self, relative):
        path = self.root / 'db_storage' / relative
        wal_path = path.with_name(path.name + '-wal')
        for attempt in range(3):
            before = signature(path, wal_path)
            raw = path.read_bytes()
            wal = wal_path.read_bytes() if wal_path.exists() else b''
            if before != signature(path, wal_path):
                continue
            key = self.key_for(raw[:4096])
            data = decode_snapshot(raw, wal, key)
            connection = sqlite3.connect(':memory:')
            connection.deserialize(data)
            connection.row_factory = sqlite3.Row
            connection.text_factory = wc._sqlite_text_factory
            connection.execute('PRAGMA query_only=ON')
            if connection.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
                connection.close()
                raise ArchiveError('WECHAT_SNAPSHOT_INVALID')
            return connection
        raise ArchiveError('WECHAT_DATABASE_BUSY_RETRY')

    def key_for(self, page):
        if len(page) != 4096:
            raise ArchiveError('WECHAT_DATABASE_INVALID_SIZE')
        salt = page[:16]
        if salt in self.used_keys:
            return self.used_keys[salt]
        candidates = [bytes.fromhex(value) for value in self.secret.get('keys', [])]
        if self.secret.get('master'):
            candidates.insert(0, hashlib.pbkdf2_hmac('sha512', bytes.fromhex(self.secret['master']), salt, 256000, 32))
        for key in candidates:
            if wc._verify_enc_key(key, page):
                self.used_keys[salt] = key
                return key
        raise ArchiveError('WECHAT_KEY_REFRESH_REQUIRED')


def signature(*paths):
    return [(p.stat().st_size, p.stat().st_mtime_ns) if p.exists() else None for p in paths]


def checksum(data, state, endian):
    a, b = state
    for x, y in struct.iter_unpack(endian + 'II', data):
        a = (a + x + b) & 0xffffffff
        b = (b + y + a) & 0xffffffff
    return a, b


def decode_snapshot(raw, wal, key):
    if not raw or len(raw) % 4096:
        raise ArchiveError('WECHAT_DATABASE_INVALID_SIZE')
    mac_key = hashlib.pbkdf2_hmac('sha512', key, bytes(x ^ 0x3a for x in raw[:16]), 2, 32)

    def page_plain(page, number):
        if not any(page):
            return page
        offset = 16 if number == 1 else 0
        expected = hmac.new(mac_key, page[offset:4032] + struct.pack('<I', number), hashlib.sha512).digest()
        if not hmac.compare_digest(expected, page[4032:4096]):
            raise ArchiveError('WECHAT_PAGE_AUTHENTICATION_FAILED')
        return wc._decrypt_page(key, page, number)

    return assemble_snapshot(raw, wal, page_plain)


def assemble_snapshot(raw, wal, page_plain):
    out = bytearray(len(raw))
    for offset in range(0, len(raw), 4096):
        out[offset:offset + 4096] = page_plain(raw[offset:offset + 4096], offset // 4096 + 1)
    if wal:
        if len(wal) < 32:
            raise ArchiveError('WECHAT_WAL_INCOMPLETE')
        magic, version, size = struct.unpack('>III', wal[:12])
        if magic not in (0x377f0682, 0x377f0683) or size != 4096 or version != 3007000:
            raise ArchiveError('WECHAT_WAL_FORMAT_UNSUPPORTED')
        endian = '<' if magic == 0x377f0682 else '>'
        state = checksum(wal[:24], (0, 0), endian)
        if state != struct.unpack('>II', wal[24:32]):
            raise ArchiveError('WECHAT_WAL_HEADER_CHECKSUM')
        committed = []
        pending = []
        final_size = 0
        for pos in range(32, len(wal) - 4119, 4120):
            frame = wal[pos:pos + 24]
            if frame[8:16] != wal[16:24]:
                break  # Preallocated frames from an earlier WAL generation.
            page = wal[pos + 24:pos + 4120]
            next_state = checksum(frame[:8] + page, state, endian)
            if next_state != struct.unpack('>II', frame[16:24]):
                raise ArchiveError('WECHAT_WAL_FRAME_CHECKSUM')
            state = next_state
            number, db_size = struct.unpack('>II', frame[:8])
            if number == 0:
                raise ArchiveError('WECHAT_WAL_PAGE_NUMBER')
            pending.append((number, page))
            if db_size:
                committed.extend(pending)
                pending.clear()
                final_size = db_size
        if final_size:
            for number, page in committed:
                if number <= final_size:
                    offset = (number - 1) * 4096
                    if len(out) < offset + 4096:
                        out.extend(b'\0' * (offset + 4096 - len(out)))
                    out[offset:offset + 4096] = page_plain(page, number)
            del out[final_size * 4096:]
    # SQLite deserialize has no separate WAL; all committed frames are already applied.
    out[18:20] = b'\x01\x01'
    return bytes(out)
