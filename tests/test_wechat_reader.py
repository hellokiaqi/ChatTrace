"""Synthetic DB records exercise the actual reader; no personal data in fixtures."""
import sqlite3
import hashlib
import xml.etree.ElementTree as ET
from types import SimpleNamespace
import tempfile
import unittest
from pathlib import Path

import zstandard
from chattrace.wechat_reader import read_rows, decode_content, MediaResolver, decode_sticker_cache
from chattrace.core import ArchiveError


class DatabaseSource:
    def __init__(self, root):
        self.root = root

    def open(self, relative):
        db = sqlite3.connect(self.root / 'db_storage' / relative)
        db.row_factory = sqlite3.Row
        return db


class ReaderTests(unittest.TestCase):
    def test_first_setup_binds_only_unique_contact(self):
        import json
        from unittest.mock import patch
        from chattrace.wechat_access import bind_config
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root/'db_storage/contact/contact.db'
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE contact(username,alias)')
                db.execute("INSERT INTO contact VALUES ('contact-1','confirmed-alias')")
            db.close()
            source = DatabaseSource(root)
            source.account = 'test-account'
            config_file = root/'config.json'
            config_file.write_text('{}','utf8')
            config = dict(config_path=str(config_file),contact_id='confirmed-alias',wechat_secret_path='example')
            with patch('chattrace.wechat_snapshot.Snapshot',return_value=source):
                bind_config(config)
                bound = json.loads(config_file.read_bytes())
                self.assertEqual((bound['account_id'],bound['contact_username']),('test-account','contact-1'))
                with sqlite3.connect(path) as db:
                    db.execute("INSERT INTO contact VALUES ('contact-2','confirmed-alias')")
                db.close()
                with self.assertRaisesRegex(ArchiveError,'NOT_UNIQUE'):
                    bind_config(config)
                self.assertEqual(json.loads(config_file.read_bytes()),bound)

    def test_sticker_local_cache_without_download_url(self):
        # Fixed encrypted synthetic 1x1 PNG, not a user's image or account key.
        encrypted = bytes.fromhex('a8d2636b23a9c149e92e830b1d5b4c960a1783869bc66a6b3472e328e5a17d4e0401f285b80ee41db046061d9cb7fccbf864a6d41e72111c70eb30066c8f1fb89e726cdb39f9662c40b48d08c9e109b7')
        md5 = 'ee76702403cd15dbc71587365494cbe5'
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            cache = root / 'cache/2026-10/Emoticon' / md5[:2] / md5
            cache.parent.mkdir(parents=True)
            cache.write_bytes(encrypted)
            source = SimpleNamespace(root=root, account='SYNTHETIC_ACCOUNT', secret={'cfg_dword': 123})
            resolver = MediaResolver(source, 'ONLY_TARGET', root / 'inbox', {'timezone': 'Asia/Shanghai', 'download_stickers': False})
            xml = ET.fromstring(f'<msg><emoji md5="{md5}" /></msg>')
            row = {'create_time': 1793240373}
            assets, status = resolver.resolve('sticker', row, xml)
            self.assertEqual(status, 'local_cache_verified')
            self.assertEqual(hashlib.md5((root / 'inbox' / assets[0]['path']).read_bytes()).hexdigest(), md5)
            self.assertEqual(resolver.resolve('sticker', row, xml), (assets, status))
            self.assertEqual(len(list((root / 'inbox/assets').iterdir())), 1)
            cache.write_bytes(encrypted[:-1])
            self.assertEqual(resolver.resolve('sticker', row, xml), ([], 'network_media_disabled'))
            broken_md5 = 'e3e46d55bc7b897e11f3e64be3fdaf39'
            broken_cache = root / 'cache/2026-10/Emoticon' / broken_md5[:2] / broken_md5
            broken_cache.parent.mkdir(parents=True)
            broken_cache.write_bytes(bytes.fromhex('a8d2636b23a9c149e92e830b1d5b4c96964a49fd79f672aacc546ba9a904145c8245d21a4221ffc739914eab94d566aab35d96d0ad12c0a78c34d0ec7eed49d7520b7a75ea27d869d57d74ab20b69ddc'))
            self.assertEqual(resolver.resolve('sticker', row, ET.fromstring(f'<msg><emoji md5="{broken_md5}" /></msg>')),
                             ([], 'network_media_disabled'))
        with self.assertRaisesRegex(ArchiveError, 'STICKER_MD5_MISMATCH'):
            decode_sticker_cache(encrypted, 123, 'SYNTHETIC_ACCOUNT', '0' * 32)
        with self.assertRaises(ArchiveError):
            decode_sticker_cache(encrypted, 456, 'SYNTHETIC_ACCOUNT', md5)

    def test_text_preserves_whitespace_newlines_and_punctuation(self):
        original = '  SYNTHETIC 测试\n原文🙂  \n'
        self.assertEqual(decode_content(zstandard.ZstdCompressor().compress(original.encode())), (original, None))
        self.assertEqual(decode_content(original), (original, None))

    def test_incremental_boundary_same_second_and_delayed_rows(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'db_storage/message/message_0.db'
            path.parent.mkdir(parents=True)
            db = sqlite3.connect(path)
            table = 'Msg_' + hashlib.md5(b'ONLY_TARGET').hexdigest()
            db.executescript(f'CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES ("ONLY_TARGET"); '
                             f'CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER);')
            for i, time in ((1, 100), (2, 200), (3, 200), (4, 150)):
                db.execute(f'INSERT INTO {table} VALUES (?,?,1,?,1,?)', (i, i, i, time))
            db.commit()
            db.close()
            source = DatabaseSource(root)
            state = {'initial_start_time': 150, 'watermark_time': 200}
            rows = read_rows(source, 'ONLY_TARGET', state, [], {'overlap_hours': 1})
            self.assertEqual([r['local_id'] for r in rows], [2, 3, 4])
            rows = read_rows(source, 'ONLY_TARGET', state, ['server:1'], {'overlap_hours': 1})
            self.assertEqual([r['local_id'] for r in rows], [1, 2, 3, 4])
            self.assertEqual(read_rows(source, 'DIFFERENT_CONTACT', state, [], {}), [])
            for checkpoint in (state, {}):
                options = {'backfill': True, 'initial_message_limit': 1}
                full = read_rows(source, 'ONLY_TARGET', checkpoint, [], options)
                self.assertEqual([r['local_id'] for r in full], [1, 2, 3, 4])
                self.assertEqual(read_rows(source, 'ONLY_TARGET', checkpoint, [], options), full)
                self.assertEqual(read_rows(source, 'DIFFERENT_CONTACT', checkpoint, [], options), [])

    def test_initial_media_does_not_expand_the_text_query(self):
        import hashlib
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            path = root / 'db_storage/message/message_0.db'
            path.parent.mkdir(parents=True)
            db = sqlite3.connect(path)
            table = 'Msg_' + hashlib.md5(b'ONLY_TARGET').hexdigest()
            db.executescript(f'CREATE TABLE Name2Id(user_name TEXT); INSERT INTO Name2Id VALUES ("ONLY_TARGET"); '
                             f'CREATE TABLE {table}(local_id INTEGER,server_id INTEGER,local_type INTEGER,sort_seq INTEGER,real_sender_id INTEGER,create_time INTEGER);')
            db.executemany(f'INSERT INTO {table} VALUES (?,?,?,?,1,?)', [(i, i, 3 if i == 1 else 1, i, i) for i in range(1, 101)])
            db.commit()
            db.close()
            rows = read_rows(DatabaseSource(root), 'ONLY_TARGET', {}, [], {'initial_message_limit': 10, 'initial_media_samples': 1})
            self.assertEqual(len(rows), 11)
            self.assertEqual(min(r['create_time'] for r in rows[-10:]), 91)


if __name__ == '__main__':
    unittest.main()
