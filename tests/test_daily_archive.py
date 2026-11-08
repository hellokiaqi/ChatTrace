"""Daily outputs and upload policy; external desktop/network boundaries are isolated."""
import hashlib
import json
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import test_archive
from chattrace.core import ArchiveError, run, upload_exclusion
from chattrace.coverage import report
from chattrace.drive import GoogleDrive


class DailyArchiveTests(unittest.TestCase):
    setUp = test_archive.ArchiveTests.setUp
    tearDown = test_archive.ArchiveTests.tearDown
    message = test_archive.ArchiveTests.message
    batch = test_archive.ArchiveTests.batch
    open = test_archive.ArchiveTests.open

    def test_event_capture_only_for_new_visual_messages_and_failure_is_visible(self):
        self.config.update(auto_capture=True, screenshot_policy='media')
        (self.inbox / 'media.png').write_bytes(test_archive.PNG)
        with patch('chattrace.screenshots.capture_series', side_effect=ArchiveError('DESKTOP_LOCKED_OR_UNAVAILABLE')) as capture:
            self.batch([self.message()])
            text_result = run(self.config, True)
            self.assertEqual(text_result['status'], 'local_only')
            self.assertEqual(text_result['capture_pending'], 0)
            capture.assert_not_called()
            self.batch([self.message('2', type='image', media=[{'kind': 'image', 'path': 'media.png'}]),
                        self.message('3', type='sticker', media=[{'kind': 'sticker', 'path': 'media.png'}])], 'visual.json')
            result = run(self.config, True)
            self.assertEqual(result['status'], 'local_partial')
            self.assertEqual(result['capture_pending'], 2)
            self.assertEqual(result['capture_error'], 'DESKTOP_LOCKED_OR_UNAVAILABLE')
            capture.assert_called_once()
            self.assertEqual(run(self.config, True)['capture_pending'], 2)
            capture.assert_called_once()

    def test_manual_and_legacy_all_policies_preserve_visual_observations(self):
        self.batch([self.message()])
        self.config['require_screenshots'] = True
        self.assertEqual(run(self.config, True)['capture_pending'], 1)
        self.config['screenshot_policy'] = 'manual'
        result = run(self.config, True)
        self.assertEqual(result['capture_pending'], 0)
        self.assertEqual(json.loads(result['coverage_json'])['visual']['missing'], 1)

    def test_revisions_and_old_views_stay_local_and_real_correction_is_a_message(self):
        self.batch([self.message('1', text='SYNTHETIC before')])
        run(self.config, True)
        self.batch([self.message('1', text='SYNTHETIC parsed revision'),
                    self.message('2', text='SYNTHETIC subsequent correction')], 'new.json', captured_at='2026-10-29T12:00:00+08:00')
        run(self.config, True)
        archive = self.open()
        try:
            archive.write_derived('retrieval/old.md', b'SYNTHETIC historical view')
            archive.write_derived('accidental.sqlite', b'SYNTHETIC non-output')
            self.assertEqual(archive.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 3)
            self.assertEqual(archive.db.execute('SELECT count(*) FROM messages').fetchone()[0], 2)
            paths = [r['path'] for r in archive.pending()]
            self.assertFalse(any(p.endswith('revisions.jsonl') or p.startswith('retrieval/') or p.endswith('.sqlite') for p in paths))
            archive.config['upload_revisions'] = True
            self.assertTrue(any(p.endswith('revisions.jsonl') for p in (r['path'] for r in archive.pending())))
            self.assertEqual(report(archive)['cloud']['local_only_files'], 2)
            self.assertEqual(upload_exclusion({}, 'secrets/token.dpapi'), 'not_archive_output')
        finally:
            archive.close()

    def test_daily_raw_text_timezone_scopes_and_manifest_mapping(self):
        text = 'SYNTHETIC <pre>literal</pre>\n```\nraw & punctuation\n'
        (self.inbox / 'raw.png').write_bytes(test_archive.PNG)
        (self.inbox / 'preview.png').write_bytes(test_archive.PNG + b'SYNTHETIC preview')
        (self.inbox / 'shot.png').write_bytes(test_archive.PNG + b'SYNTHETIC screenshot')
        self.batch([self.message('1', text=text, timestamp='2026-10-28T16:30:00+00:00', screenshots=['s']),
                    self.message('2', type='image', media=[{'kind': 'image_original_wxgf', 'path': 'raw.png'},
                                                         {'kind': 'image_preview', 'path': 'preview.png'}])],
                   screenshots=[{'id': 's', 'path': 'shot.png', 'metadata': {'range_verification': 'visual_confirmation',
                       'partially_visible_message_ids': ['M000002']}}])
        run(self.config, True)
        a = self.open()
        try:
            before = [tuple(r) for r in a.db.execute('SELECT * FROM messages')]
            md = a.root / '2026/10/2026-10-29/conversation.md'
            self.assertIn('2026-10-29T00:30:00+08:00', md.read_text('utf-8'))
            self.assertIn('````text\n' + text + '\n````', md.read_text('utf-8'))
            self.assertNotIn('<pre>SYNTHETIC', md.read_text('utf-8'))
            remote = test_archive.DirectoryRemote(self.base / 'remote')
            a.upload(remote)
            a.materialize()
            manifest = json.loads((md.parent / 'manifest.json').read_text('utf-8'))
            roles = {r['role'] for r in manifest['resources']}
            self.assertEqual(roles, {'original_media', 'derived_preview', 'linked_screenshot'})
            for resource in manifest['resources']:
                data = (a.root / resource['path']).read_bytes()
                self.assertEqual(resource['sha256'], hashlib.sha256(data).hexdigest())
                self.assertEqual(resource['size'], len(data))
                self.assertIn(resource['drive_file_id'], resource['url'])
                self.assertEqual(resource['cloud_status'], 'last_upload_verified')
            shot = manifest['screenshots'][0]
            self.assertEqual(shot['confirmed_message_ids'], ['M000001'])
            self.assertEqual(shot['partial_message_ids'], ['M000002'])
            self.assertEqual(shot['unverified_message_ids'], [])
            self.assertEqual(manifest['metrics']['visual']['required_pending'], 1)
            self.assertEqual([tuple(r) for r in a.db.execute('SELECT * FROM messages')], before)
            a.upload(remote)
            a.materialize()
            self.assertEqual(a.pending(), [])
            self.assertEqual(report(a)['local']['status'], 'consistent')
        finally:
            a.close()

    def test_unverified_screenshot_not_promoted_and_hash_mismatch_rejected(self):
        (self.inbox / 'shot.png').write_bytes(test_archive.PNG)
        self.batch([self.message(screenshots=['s'])], screenshots=[{'id': 's', 'path': 'shot.png'}])
        run(self.config, True)
        a = self.open()
        try:
            manifest = json.loads((a.root / '2026/10/2026-10-29/manifest.json').read_text('utf-8'))
            self.assertEqual(manifest['screenshots'][0]['confirmed_message_ids'], [])
            self.assertEqual(manifest['screenshots'][0]['unverified_message_ids'], ['M000001'])
            self.assertEqual(manifest['message_evidence'][0]['visual'], 'linked_unverified')
            path = a.root / manifest['screenshots'][0]['path']
            path.write_bytes(test_archive.PNG + b'SYNTHETIC damaged')
            with self.assertRaisesRegex(ArchiveError, 'RESOURCE_ID_HASH_MISMATCH'):
                a.materialize()
        finally:
            a.close()

    def test_drive_verification_rejects_wrong_remote_identity_and_bytes(self):
        self.batch([self.message()])
        run(self.config, True)
        a = self.open()
        try:
            data = (a.root / 'index.md').read_bytes()
            with a.db:
                a.db.execute("UPDATE files SET remote_id='remote-index' WHERE path='index.md'")
            meta = {'id': 'remote-index', 'name': 'index.md', 'mimeType': 'text/markdown', 'parents': ['contact-root'],
                    'md5Checksum': hashlib.md5(data).hexdigest(), 'size': str(len(data)), 'trashed': False}
            # Only the Google metadata response is substituted; verification logic is real.
            endpoint = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(execute=lambda **kwargs: dict(meta)))
            drive = GoogleDrive.__new__(GoogleDrive)
            drive.api = SimpleNamespace(files=lambda: endpoint)
            drive.archive, drive.contact_root = a, 'contact-root'
            self.assertTrue(drive.verify('remote-index', data))
            for field, bad in [('id', 'another-id'), ('name', 'another.md'), ('parents', ['wrong-parent']),
                               ('mimeType', 'application/octet-stream'), ('trashed', True)]:
                original = meta[field]
                meta[field] = bad
                with self.assertRaisesRegex(ArchiveError, 'REMOTE_FILE_IDENTITY_MISMATCH'):
                    drive.verify('remote-index', data)
                meta[field] = original
            meta['md5Checksum'] = 'invalid'
            self.assertFalse(drive.verify('remote-index', data))
        finally:
            a.close()

    def test_drive_folder_reuse_checks_parent_and_type(self):
        self.batch([self.message()])
        run(self.config, True)
        a = self.open()
        try:
            with a.db:
                a.db.execute("INSERT INTO folders VALUES ('/media','folder-id')")
            meta = {'id': 'folder-id', 'name': 'media', 'parents': ['contact'], 'mimeType': 'application/vnd.google-apps.folder'}
            endpoint = SimpleNamespace(get=lambda **kwargs: SimpleNamespace(execute=lambda **kwargs: dict(meta)))
            drive = GoogleDrive.__new__(GoogleDrive)
            drive.api, drive.archive = SimpleNamespace(files=lambda: endpoint), a
            self.assertEqual(drive.folder('/media', 'media', 'contact'), 'folder-id')
            for field, wrong in [('parents', ['another-contact']), ('mimeType', 'text/plain'), ('name', 'renamed')]:
                saved = meta[field]
                meta[field] = wrong
                with self.assertRaisesRegex(ArchiveError, 'REMOTE_FOLDER_IDENTITY_MISMATCH'):
                    drive.folder('/media', 'media', 'contact')
                meta[field] = saved
        finally:
            a.close()


if __name__ == '__main__':
    unittest.main()
