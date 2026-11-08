"""Exercise actual ingestion, coverage and derived files with synthetic fixtures."""
import json
import unittest
import test_archive
from chattrace.core import Archive, run, ArchiveError, atomic, encode, load_config
from chattrace.coverage import report


class RetrievalTests(unittest.TestCase):
    setUp = test_archive.ArchiveTests.setUp
    tearDown = test_archive.ArchiveTests.tearDown
    message = test_archive.ArchiveTests.message
    batch = test_archive.ArchiveTests.batch
    open = test_archive.ArchiveTests.open

    def test_visual_partial_not_full_and_plain_text_not_exempt(self):
        (self.inbox / 'shot.png').write_bytes(test_archive.PNG)
        meta = {'range_verification': 'visual_confirmation', 'partially_visible_message_ids': ['M000001']}
        self.batch([self.message(screenshots=['s']), self.message('2', sequence=2)],
                   screenshots=[{'id': 's', 'path': 'shot.png', 'metadata': meta}])
        run(self.config, True)
        a = self.open()
        try:
            r = report(a)
            self.assertEqual(r['visual']['partial_only'], 1)
            self.assertEqual(r['visual']['missing'], 1)
            self.assertEqual(r['visual']['uncovered_plain_text'], 2)
            self.assertIsNone(r['collection']['coverage_ratio'])
            self.assertEqual(r['local']['status'], 'consistent')
        finally:
            a.close()

    def test_other_confirmed_segment_completes_message(self):
        (self.inbox / 'one.png').write_bytes(test_archive.PNG)
        (self.inbox / 'two.png').write_bytes(test_archive.PNG + b'other synthetic bytes')
        self.batch([self.message(screenshots=['a', 'b'])], screenshots=[
            {'id': 'a', 'path': 'one.png', 'metadata': {'range_verification': 'visual_confirmation', 'partially_visible_message_ids': ['M000001']}},
            {'id': 'b', 'path': 'two.png', 'metadata': {'range_verification': 'visual_confirmation'}}])
        run(self.config, True)
        a = self.open()
        try:
            self.assertEqual(report(a)['visual']['confirmed_full'], 1)
        finally:
            a.close()

    def test_reverse_partial_reference_preserves_records_and_other_full_coverage(self):
        (self.inbox / 'one.png').write_bytes(test_archive.PNG)
        (self.inbox / 'two.png').write_bytes(test_archive.PNG + b'second synthetic image')
        self.batch([self.message('1', screenshots=['a']), self.message('2'), self.message('3', screenshots=['b'])], screenshots=[
            {'id': 'a', 'path': 'one.png', 'metadata': {'range_verification': 'visual_confirmation', 'partially_visible_message_ids': ['M000002', 'M000003', 'M999999']}},
            {'id': 'b', 'path': 'two.png', 'metadata': {'range_verification': 'visual_confirmation'}}])
        run(self.config, True)
        a = self.open()
        try:
            original = list(a.db.execute('SELECT id,payload FROM messages'))
            e = report(a)['visual']['message_evidence']
            self.assertEqual(e['M000002']['visual'], 'partial_only')
            self.assertEqual(e['M000003']['visual'], 'confirmed_full')
            self.assertNotIn('M999999', e)
            self.assertEqual(len(e['M000002']['partial_screenshots']), 1)
            self.assertEqual([tuple(r) for r in original], [tuple(r) for r in a.db.execute('SELECT id,payload FROM messages')])
            (a.root / e['M000002']['partial_screenshots'][0]).unlink()
            self.assertEqual(report(a)['visual']['message_evidence']['M000002']['visual'], 'missing')
        finally:
            a.close()

    def test_failed_media_retry_keeps_existing_resource(self):
        (self.inbox / 'media.gif').write_bytes(b'SYNTHETIC_MEDIA')
        self.batch([self.message(type='sticker', media=[{'kind': 'sticker', 'path': 'media.gif'}],
                                 source_metadata={'media_status': 'downloaded_verified'})])
        run(self.config, True)
        self.batch([self.message(type='sticker', source_metadata={'media_status': 'unavailable:NETWORK'})],
                   'retry.json', captured_at='2026-10-29T11:00:00+08:00')
        run(self.config, True)
        a = self.open()
        try:
            m = json.loads(a.db.execute('SELECT payload FROM messages').fetchone()[0])
            self.assertEqual(len(m['media']), 1)
            self.assertEqual(m['source_metadata']['media_retry_status'], 'unavailable:NETWORK')
            self.assertEqual(report(a)['media']['extracted'], 1)
        finally:
            a.close()

    def test_current_text_only_no_revision_replay_and_rebuild_stable(self):
        self.batch([self.message(text='SYNTHETIC OLD')])
        run(self.config, True)
        self.batch([self.message(text='SYNTHETIC CURRENT\n<keep>')], 'new.json', captured_at='2026-10-29T11:00:00+08:00')
        run(self.config, True)
        a = self.open()
        try:
            path = a.root/'2026/10/2026-10-29/conversation.md'
            before = path.read_bytes()
            content = path.read_text('utf-8')
            self.assertNotIn('SYNTHETIC OLD', content)
            self.assertEqual(content.count('SYNTHETIC CURRENT\n<keep>'), 1)
            self.assertEqual(a.db.execute('SELECT count(*) FROM revisions').fetchone()[0], 2)
            revisions = [json.loads(line) for line in (a.root / '2026/10/2026-10-29/revisions.jsonl').read_text('utf-8').splitlines()]
            self.assertTrue(all(r['record_kind'] == 'revision_history' and not r['use_for_current_conversation'] for r in revisions))
            a.materialize()
            self.assertEqual(before,path.read_bytes())
            self.assertEqual(report(a)['local']['status'], 'consistent')
        finally:
            a.close()

    def test_local_corruption_is_separate_from_cloud(self):
        self.batch([self.message()])
        run(self.config, True)
        a = self.open()
        try:
            (a.root / 'index.md').write_text('SYNTHETIC CORRUPTION')
            r = report(a)
            self.assertEqual(r['local']['bad_files'], ['index.md'])
            self.assertEqual(r['cloud']['verified_files'], 0)
            self.assertEqual(r['local']['status'], 'inconsistent')
        finally:
            a.close()

    def test_media_pending_independent_of_screenshot_requirement(self):
        self.config['require_screenshots'] = False
        self.batch([self.message(type='sticker')])
        result = run(self.config, True)
        self.assertEqual(result['status'], 'local_partial')
        self.assertIn('coverage_json', result)
