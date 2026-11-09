import json
import tempfile
import unittest
from pathlib import Path

from chattrace.core import ArchiveError
from chattrace.start_review import candidates, classify, import_reviews


class StartReviewTests(unittest.TestCase):
    def test_review_roundtrip_context_change_and_offline(self):
        def msg(mid,time,text,role):
            return dict(id=mid,source_id=mid,timestamp='2026-01-01T'+time+'+08:00',sender=role,type='text',text=text)
        rows=[msg('M1','00:00:00','SYNTHETIC question','self'),
              msg('M2','01:30:00','another question','self'),
              msg('M3','03:00:01','answer','friend')]
        rows[-1]['reply_to']='M1'
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)
            b,=classify(root,rows,False)
            self.assertIsNone(b['suggestion'])
            self.assertEqual(b['context']['referenced']['id'],'M1')
            f=root/'import.json'
            change=dict(fingerprint=b['fingerprint'],label='delayed')
            f.write_text(json.dumps(dict(schema=1,reviews={'M3':change})))
            self.assertEqual(import_reviews(root,rows,f),1)
            self.assertEqual(classify(root,rows,False)[0]['review'],'delayed')
            rows[-1]['text']='changed observation'
            with self.assertRaises(ArchiveError):
                import_reviews(root,rows,f)
            self.assertIsNone(classify(root,rows,False)[0]['review'])
            change.update(fingerprint=candidates(rows)[0]['fingerprint'],label=None)
            f.write_text(json.dumps(dict(schema=1,reviews={'M3':change})))
            import_reviews(root,rows,f)
            self.assertEqual(json.loads((root/'start-reviews.json').read_bytes()),{})
