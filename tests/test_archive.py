"""Synthetic fixtures only. These tests never open WeChat or connect to Drive."""
import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
import xml.etree.ElementTree as ET
from pathlib import Path
from chattrace.core import Archive, ArchiveError, exclusive, normalize, run, safe_error
from chattrace.scheduler import task_xml

PNG = base64.b64decode('iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl6qFAAAAAASUVORK5CYII=')


class DirectoryRemote:
    """Deterministic local transport for testing the real durable upload queue."""
    def __init__(self, root):
        self.root = root
        self.root.mkdir()
        self.count = 0
        self.fail_after_write = False

    def reserve_id(self):
        self.count += 1
        return str(self.count)

    def put(self, remote_id, relative, data):
        (self.root / remote_id).write_bytes(data)
        if self.fail_after_write:
            raise ConnectionError('synthetic lost response')

    def verify(self, remote_id, data):
        return (self.root / remote_id).read_bytes() == data


class ArchiveTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.base = Path(self.temp.name)
        self.inbox = self.base / 'inbox'
        self.inbox.mkdir()
        self.config = {'archive_root': str(self.base / 'archive'), 'inbox': str(self.inbox),
                       'account_id': 'TEST_ACCOUNT', 'contact_id': 'TEST_CONTACT', 'contact_name': 'SYNTHETIC_TEST',
                       'identity_confirmed': True, 'reader': 'import', 'timezone': 'Asia/Shanghai',
                       'config_path': str(self.base / 'config.json')}

    def tearDown(self):
        self.temp.cleanup()

    def message(self, identifier='1', **changes):
        value = {'source_id': identifier, 'timestamp': '2026-10-29T08:30:12+08:00', 'precision': 'second',
                 'sender': 'self', 'type': 'text', 'text': 'SYNTHETIC TEST ONLY\n<原文>&🙂',
                 'sequence': 1, 'occurrence': 1, 'media': [], 'screenshots': []}
        value.update(changes)
        return value

    def batch(self, messages, name='batch.json', **changes):
        value = {'schema': 1, 'account_id': 'TEST_ACCOUNT', 'contact_id': 'TEST_CONTACT',
                 'source': 'synthetic-tests', 'captured_at': '2026-10-29T10:00:00+08:00',
                 'messages': messages, 'screenshots': []}
        value.update(changes)
        path = self.inbox / name
        path.write_text(json.dumps(value, ensure_ascii=False), encoding='utf-8')
        return path

    def open(self):
        return Archive(self.config)

    def test_first_repeat_alternating_same_second_and_late(self):
        self.batch([self.message('1'), self.message('2', sender='friend', sequence=2)])
        self.assertEqual(run(self.config, True)['messages'], 2)
        self.assertEqual(run(self.config, True)['messages'], 0)
        self.batch([self.message('3', timestamp='2026-10-28T23:59:00+08:00')], 'late.json')
        self.assertEqual(run(self.config, True)['messages'], 1)
        archive = self.open()
        try:
            self.assertEqual(archive.db.execute('SELECT count(*) FROM messages').fetchone()[0], 3)
            path = archive.root / '2026/10/2026-10-29/messages.jsonl'
            messages = [json.loads(line) for line in path.read_text('utf-8').splitlines()]
            self.assertEqual(messages[0]['text'], self.message()['text'])
            self.assertNotEqual(messages[0]['id'], messages[1]['id'])
        finally:
            archive.close()

    def test_fallback_occurrences_preserve_identical_messages(self):
        self.batch([self.message(None), self.message(None, occurrence=2, sequence=2)])
        self.assertEqual(run(self.config, True)['messages'], 2)
        self.batch([self.message(None, occurrence=2, sequence=2)], 'overlap.json')
        self.assertEqual(run(self.config, True)['messages'], 0)

    def test_ambiguous_duplicate_rejected(self):
        self.batch([self.message(None), self.message(None)])
        with self.assertRaisesRegex(ArchiveError, 'AMBIGUOUS_DUPLICATE'):
            run(self.config, True)

    def test_recall_and_date_revision_preserved(self):
        self.batch([self.message()])
        run(self.config, True)
        self.batch([self.message(type='recalled', sender='system', text='SYNTHETIC RECALL',
                                 timestamp='2026-10-30T08:30:12+08:00')], 'revision.json')
        self.assertEqual(run(self.config, True)['messages'], 1)
        archive = self.open()
        try:
            self.assertEqual(archive.db.execute('SELECT count(*) FROM messages').fetchone()[0], 1)
            self.assertEqual(archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
            self.assertEqual((archive.root / '2026/10/2026-10-29/messages.jsonl').read_text().strip(), '')
        finally:
            archive.close()

    def test_screenshot_links_sticker_and_unknown(self):
        (self.inbox / 'shot.png').write_bytes(PNG)
        self.batch([self.message(type='sticker', text='', screenshots=['s']),
                    self.message('2', type='future_type', sequence=2, screenshots=['s'])],
                   screenshots=[{'id': 's', 'path': 'shot.png'}])
        run(self.config, True)
        archive = self.open()
        try:
            directory = archive.root / '2026/10/2026-10-29'
            manifest = json.loads((directory / 'manifest.json').read_text('utf-8'))
            shot = manifest['screenshots'][0]
            self.assertEqual([m['id'] for m in shot['messages']], ['M000001', 'M000002'])
            self.assertEqual((archive.root / shot['path']).read_bytes(), PNG)
            for line in (directory / 'messages.jsonl').read_text('utf-8').splitlines():
                self.assertIn(shot['path'], json.loads(line)['screenshots'])
                self.assertTrue(json.loads(line)['needs_review'])
        finally:
            archive.close()

    def test_wrong_contact_and_path_rejected(self):
        path = self.batch([self.message()], contact_id='UNAUTHORIZED')
        with self.assertRaisesRegex(ArchiveError, 'IDENTITY'):
            normalize(json.loads(path.read_text('utf-8')), self.inbox, self.config)
        (self.base / 'outside.png').write_bytes(PNG)
        path = self.batch([self.message()], screenshots=[{'id': 's', 'path': '../outside.png'}])
        with self.assertRaisesRegex(ArchiveError, 'ASSET_OUTSIDE'):
            normalize(json.loads(path.read_text('utf-8')), self.inbox, self.config)

    def test_upload_response_loss_reuses_id_and_pending(self):
        self.batch([self.message()])
        run(self.config, True)
        archive = self.open()
        remote = DirectoryRemote(self.base / 'remote')
        remote.fail_after_write = True
        try:
            with self.assertRaises(ConnectionError):
                archive.upload(remote)
            count = len(archive.pending())
            self.assertGreater(count, 0)
            remote.fail_after_write = False
            self.assertEqual(archive.upload(remote), count)
            self.assertEqual(archive.pending(), [])
            self.assertEqual(archive.upload(remote), 0)
            self.assertEqual(len(list(remote.root.iterdir())), count)
        finally:
            archive.close()

    def test_process_exit_after_sqlite_commit_recovers_outputs(self):
        path = self.batch([self.message()])
        config = self.base / 'config.json'
        config.write_text(json.dumps(self.config), encoding='utf-8')
        code = 'import json,sys,os; from pathlib import Path; from chattrace.core import Archive; a=Archive(json.loads(Path(sys.argv[1]).read_text())); a.ingest(Path(sys.argv[2])); os._exit(17)'
        process = subprocess.run([sys.executable, '-c', code, str(config), str(path)], capture_output=True)
        self.assertEqual(process.returncode, 17)
        self.assertEqual(run(self.config, True)['messages'], 0)
        self.assertTrue((Path(self.config['archive_root']) / '2026/10/2026-10-29/manifest.json').exists())

    def test_cross_process_lock(self):
        with exclusive(self.config['archive_root']):
            code = 'import sys; from chattrace.core import exclusive;\nwith exclusive(sys.argv[1]): pass'
            process = subprocess.run([sys.executable, '-c', code, self.config['archive_root']], capture_output=True)
            self.assertNotEqual(process.returncode, 0)
            self.assertIn(b'ALREADY_RUNNING', process.stderr)

    def test_unavailable_reader_records_failure(self):
        self.config['reader'] = 'wechat-ui'
        with self.assertRaisesRegex(ArchiveError, 'WECHAT_UI_READER_UNAVAILABLE'):
            run(self.config, True)
        archive = self.open()
        try:
            self.assertEqual(archive.db.execute('SELECT status FROM runs').fetchone()[0], 'failed')
        finally:
            archive.close()

    def test_no_input_is_not_success(self):
        with self.assertRaisesRegex(ArchiveError, 'NO_INPUT_BATCHES'):
            run(self.config, True)

    def test_modified_output_preserved(self):
        self.batch([self.message()])
        run(self.config, True)
        path = Path(self.config['archive_root']) / 'index.md'
        path.write_text('USER EDIT', encoding='utf-8')
        with self.assertRaisesRegex(ArchiveError, 'OUTPUT_MODIFIED_EXTERNALLY'):
            run(self.config, True)
        self.assertEqual(path.read_text(), 'USER EDIT')

    def test_no_overwrite_existing_archive(self):
        root = Path(self.config['archive_root'])
        root.mkdir()
        (root / 'existing.txt').write_text('KEEP')
        with self.assertRaisesRegex(ArchiveError, 'NOT_EMPTY'):
            run(self.config, True)

    def test_task_xml_and_error_privacy(self):
        root = ET.fromstring(task_xml(self.config))
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertTrue(root.find('.//t:StartBoundary', ns).text.endswith('T04:00:00'))
        self.assertEqual(root.find('.//t:LogonType', ns).text, 'InteractiveToken')
        self.assertTrue(root.find('.//t:Arguments', ns).text.endswith(' update'))
        self.assertEqual(root.find('.//t:DaysInterval', ns).text,'1')
        self.assertEqual(safe_error(ConnectionError('SECRET_CHAT_TOKEN')), 'ConnectionError')

    def test_late_snapshot_does_not_revert_newer_state(self):
        self.batch([self.message(text='SYNTHETIC NEW')], captured_at='2026-10-29T12:00:00+08:00')
        run(self.config, True)
        self.batch([self.message(text='SYNTHETIC OLD')], 'late.json', captured_at='2026-10-29T11:00:00+08:00')
        run(self.config, True)
        archive = self.open()
        try:
            self.assertEqual(json.loads(archive.db.execute('SELECT payload FROM messages').fetchone()[0])['text'], 'SYNTHETIC NEW')
            self.assertEqual(archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
        finally:
            archive.close()

    def test_bad_checksum_keeps_pending(self):
        self.batch([self.message()])
        run(self.config, True)
        archive = self.open()
        remote = DirectoryRemote(self.base / 'remote')
        remote.verify = lambda remote_id, data: False
        try:
            with self.assertRaisesRegex(ArchiveError, 'REMOTE_CHECKSUM_MISMATCH'):
                archive.upload(remote)
            self.assertTrue(archive.pending())
        finally:
            archive.close()

    @unittest.skipUnless(os.name == 'nt', 'Windows DPAPI')
    def test_dpapi_roundtrip(self):
        from chattrace.drive import protect
        data = b'SYNTHETIC_NOT_A_REAL_TOKEN'
        encrypted = protect(data)
        self.assertNotIn(data, encrypted)
        self.assertEqual(protect(encrypted, True), data)


if __name__ == '__main__':
    unittest.main()
