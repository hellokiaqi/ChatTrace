import json
import tempfile
import unittest
from pathlib import Path
from chattrace.core import Archive, ArchiveError, load_config, run
from chattrace.retrieval import build
from chattrace.publication import sync

class QQImportTests(unittest.TestCase):
    def test_private_export_incremental_and_wrong_identity(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            config = json.loads((Path(__file__).resolve().parents[1]/'config.qq.example.json').read_bytes())
            config.update(account_id='owner',contact_id='friend',identity_confirmed=True,qq_export_path='in.json')
            path = root/'config.json'
            path.write_text(json.dumps(config),'utf8')
            config = load_config(path)
            body = '  synthetic\nmessage  '
            export = dict(chatlab=dict(version='0.0.2',exportedAt=200,generator='QQNT_Export'),
                          meta=dict(type='private',platform='qq',ownerId='owner'),
                          members=[dict(platformId=k) for k in ('owner','friend')],
                          messages=[dict(platformMessageId='100',sender='owner',timestamp=100,type=0,content=body)])
            source = root/'in.json'
            source.write_text(json.dumps(export),'utf8')
            self.assertEqual(run(config,True)['messages'],1)
            self.assertEqual(run(config,True)['messages'],0)
            export['messages'].append(dict(platformMessageId='101',sender='friend',timestamp=100,type=25,content='reply',replyToMessageId='100'))
            source.write_text(json.dumps(export),'utf8')
            self.assertEqual(run(config,True)['messages'],1)
            a=Archive(config)
            try:
                rows=[json.loads(r[0]) for r in a.db.execute('select payload from messages order by id')]
                self.assertEqual((rows[0]['text'],rows[1]['sender'],rows[1]['reply_to']),(body,'friend','100'))
                self.assertEqual(build(a,False)['unique_exported'],2)
                folder=a.root/'v4/retrieval'
                self.assertIn('ChatTrace / QQ',next(folder.glob('CT-R*.md')).read_text('utf8'))
                self.assertEqual(sync(a,True)['status'],'dry_run')
            finally:
                a.close()
            for field,bad in [('ownerId','wrong'),('type','group')]:
                invalid={**export,'meta':{**export['meta'],field:bad}}
                source.write_text(json.dumps(invalid),'utf8')
                with self.assertRaisesRegex(ArchiveError,'CONTACT_MISMATCH'):
                    run(config,True)
