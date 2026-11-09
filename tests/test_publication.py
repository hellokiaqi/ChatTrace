import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from unittest.mock import MagicMock
import requests
import test_archive
from chattrace.core import ArchiveError
from chattrace.retrieval import build, validate, promote
from chattrace.publication import sync
from chattrace.semantic import Semantic, checked


class ViewTests(unittest.TestCase):
    setUp = test_archive.ArchiveTests.setUp
    def tearDown(self):
        self.doCleanups()
        self.temp.cleanup()
    message = test_archive.ArchiveTests.message
    batch = test_archive.ArchiveTests.batch
    open = test_archive.ArchiveTests.open

    def test_cross_midnight_lossless_and_tamper(self):
        a = self.open()
        self.addCleanup(a.close)
        a.ingest(self.batch([self.message('late',timestamp='2026-10-30T00:02:00+08:00'),
                             self.message('early',timestamp='2026-10-29T23:59:00+08:00')]))
        self.assertEqual(build(a,False)['unique_exported'],2)
        root=a.root/'v4'
        manifest=json.loads((root/'manifest.json').read_bytes())
        doc,=manifest['documents']
        self.assertEqual(doc['ids'],['M000002','M000001'])
        self.assertEqual(len(doc['sessions']),1)
        html=(root/'human/2026-10.html').read_text('utf-8')
        self.assertIn('&lt;原文&gt;',html)
        path=root/'retrieval'/doc['filename']
        modified=path.stat().st_mtime_ns
        build(a,False)
        self.assertEqual(path.stat().st_mtime_ns,modified)
        self.assertEqual(sync(a,True)['plan'][0][1],'create')
        path.write_bytes(b'incorrect')
        with self.assertRaisesRegex(ArchiveError,'MISMATCH'):
            validate(a)

    def test_incremental_long_message_and_stable_anchor(self):
        a=self.open()
        self.addCleanup(a.close)
        a.config.update(v4_soft_chars=800,v4_max_chars=1500)
        a.ingest(self.batch([self.message()]))
        build(a,False)
        manifest=a.root/'v4/manifest.json'
        first=json.loads(manifest.read_bytes())['documents'][0]['key']
        a.ingest(self.batch([self.message('2',text='🙂'*640,sequence=2),
                             self.message('3',text='另一个测试'*110,sequence=3)],'new.json'))
        result=build(a,False)
        self.assertEqual((result['unique_exported'],result['documents']),(3,2))
        self.assertEqual(json.loads(manifest.read_bytes())['documents'][0]['key'],first)
        docs=json.loads(manifest.read_bytes())['documents']
        self.assertTrue(docs[-1]['sessions'][0]['continuation'])
        self.assertEqual(docs[0]['sessions'][0]['segment_id'],docs[-1]['sessions'][0]['segment_id'])
        self.assertTrue(all(d['chars']<=1500 for d in docs))
        a.ingest(self.batch([self.message('4',text='x'*16001)],'too-long.json'))
        with self.assertRaisesRegex(ArchiveError,'SINGLE_MESSAGE'):
            build(a,False,preview=True)
        self.assertEqual(json.loads(manifest.read_bytes())['documents'],docs)

    def test_sync_resumes_after_bad_export_and_preserves_paragraphs(self):
        archive = self.open()
        self.addCleanup(archive.close)
        archive.ingest(self.batch([self.message(text='first\n\nsecond')]))
        build(archive, False)
        drive = MagicMock()
        drive.folder.return_value = 'folder'
        drive.media.side_effect = lambda stream, **kwargs: stream.read().decode()
        files = drive.api.files.return_value
        files.get.return_value.execute.return_value = {'shared': False}
        files.list.return_value.execute.return_value = {'files': []}
        uploads = {}
        def create(body, media_body, fields):
            remote = 'id-' + str(len(uploads))
            uploads[remote] = (body, media_body)
            return MagicMock(execute=lambda: {'id': remote})
        files.create.side_effect = create
        files.get_media.return_value.execute.return_value = b'corrupted'
        with self.assertRaisesRegex(ArchiveError, 'MARKDOWN_CONTENT_MISMATCH'):
            sync(archive, drive_factory=lambda *_: drive)
        self.assertFalse((archive.root/'v4/markdown-drive-state.json').exists())
        def search(q, fields):
            matches = [{'id': key} for key, (body, _) in uploads.items()
                       if body['appProperties']['chatTraceV4'] in q]
            return MagicMock(execute=lambda: {'files': matches})
        files.list.side_effect = search
        files.get.side_effect = lambda fileId, **kw: MagicMock(execute=lambda:
            {'shared': False} if fileId == 'folder' else {**uploads[fileId][0],'md5Checksum':__import__('hashlib').md5(uploads[fileId][1].encode()).hexdigest()})
        files.get_media.side_effect = lambda fileId, **kw: MagicMock(execute=lambda:
            uploads[fileId][1].encode())
        result = sync(archive, drive_factory=lambda *_: drive)
        self.assertEqual(result['status'], 'synced')
        self.assertEqual(len(uploads), 3)
        for metadata, body in uploads.values():
            self.assertTrue(metadata['name'].endswith('.md'))
            self.assertEqual(metadata['mimeType'],'text/markdown')
            self.assertTrue(body.startswith('# '))
        again = sync(archive, drive_factory=lambda *_: drive)
        self.assertTrue(all(action == 'skip' for _, action in again['plan']))
        self.assertEqual(files.update.call_count, 1)

    def test_preview_isolation_and_stale_promotion(self):
        archive = self.open()
        self.addCleanup(archive.close)
        archive.ingest(self.batch([self.message()]))
        build(archive,False)
        active = archive.root/'v4/retrieval/manifest.json'
        previous = active.read_bytes()
        archive.ingest(self.batch([self.message('2',sequence=2)],'two.json'))
        build(archive,False,preview=True)
        self.assertEqual(active.read_bytes(),previous)
        archive.ingest(self.batch([self.message('3',sequence=3)],'three.json'))
        with self.assertRaisesRegex(ArchiveError,'SOURCE_CHANGED'):
            promote(archive)
        self.assertEqual(active.read_bytes(),previous)
        build(archive,False,preview=True)
        self.assertEqual(promote(archive)['unique_exported'],3)
        self.assertNotEqual(active.read_bytes(),previous)
        self.assertTrue(any(p.read_bytes()==previous for p in (archive.root/'retrieval-history').rglob('manifest.json')))

    def test_navigation_change_regenerates_existing_view(self):
        archive = self.open()
        self.addCleanup(archive.close)
        archive.ingest(self.batch([self.message()]))
        build(archive,False)
        with patch('chattrace.retrieval.START','New navigation instructions'):
            build(archive,False)
            self.assertEqual((archive.root/'v4/retrieval/00_START_HERE.md').read_text('utf-8'),
                             'New navigation instructions')

    def test_model_validation_retry_and_cache(self):
        env={'CHATTRACE_LLM_API_KEY':'synthetic','CHATTRACE_LLM_BASE_URL':'https://test.invalid','CHATTRACE_LLM_MODEL':'fixture'}
        rows=dict(core=[['M1',None,None,'self','text',None,'source','synthetic']],active=None,left=[],right=[])
        valid=dict(segments=[dict(start='M1',new_block=True,title='topic',description='test',keywords=['a','b','c'],substarts=[])])
        with self.assertRaises(ValueError):
            checked({'segments':[{**valid['segments'][0],'start':'absent'}]},['M1'])
        with patch('chattrace.semantic.settings',return_value=env):
            sem=Semantic(self.base,True)
        with patch('chattrace.semantic.requests.post',side_effect=requests.Timeout):
            self.assertEqual(sem.suggest(rows,{'segments':[]})['segments'],[])
            self.assertEqual(sem.stats['failures'],2)
        with patch('chattrace.semantic.requests.post') as post:
            post.return_value.json.return_value={'choices':[{'message':{'content':json.dumps(valid)}}]}
            self.assertEqual(sem.suggest(rows,None),valid)
            self.assertEqual(sem.suggest(rows,None),valid)
            self.assertEqual(post.call_count,1)
