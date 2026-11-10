import hashlib
import io
import tempfile
import unittest
from pathlib import Path
from PIL import Image
from chattrace.qq_native import message
from chattrace.vendor.qqnt_elements import parse_content


class QQNativeTests(unittest.TestCase):
    def test_image_hash_distinguishes_original_and_local_preview(self):
        with tempfile.TemporaryDirectory() as name:
            root=Path(name)
            stream=io.BytesIO()
            Image.new('RGB',(2,2)).save(stream,'PNG')
            data=stream.getvalue()
            md5=hashlib.md5(data).hexdigest()
            raw=root/(md5+'.png')
            raw.write_bytes(data)
            element=parse_content(b'')
            element.update(content_type=2,md5_hex=md5,local_path=str(raw))
            row=dict(id=4,seq=20,ts=100,body=b'test',kind=2,sender='u-peer')
            result=message(row,[element],None,'u-peer',{'u-self'},root,{md5:[raw]},root)
            self.assertEqual(result['sender'],'friend')
            self.assertEqual(result['media'][0]['kind'],'image')
            self.assertEqual(result['source_metadata']['missing_media_parts'],0)
            element['md5_hex']='0'*32
            result=message(row,[element],None,'u-peer',{'u-self'},root,{},root)
            self.assertEqual(result['media'][0]['kind'],'image_preview')
            self.assertEqual(result['source_metadata']['missing_media_parts'],1)
            result=message(row,[element],None,'u-peer',{'u-self'},root/'unrelated',{},root)
            self.assertFalse(result['media'])

    def test_native_embedded_face_preserves_label_and_mixed_text(self):
        face=parse_content(b'');face.update(content_type=6,video_text='[表情]',video_flag=1)
        text=parse_content(b'');text.update(content_type=1,text='  原文\n')
        row=dict(id=4,seq=1,ts=100,body=b'test',kind=2,sender='u-self')
        result=message(row,[text,face],None,'u-peer',{'u-self'},Path('.'),{},Path('.'))
        self.assertEqual(result['text'],'  原文\n[表情]')
        self.assertEqual(result['type'],'emoji')
        self.assertEqual(result['sender'],'self')

    def test_incremental_collect_finds_older_target_row_after_backfill(self):
        import json
        import sqlite3
        from types import SimpleNamespace
        from unittest.mock import patch
        from chattrace.qq_native import collect
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            profile = sqlite3.connect(root/'profile_info.db')
            profile.execute('CREATE TABLE profile_info_v6 ([1000], [1002])')
            profile.executemany('INSERT INTO profile_info_v6 VALUES (?,?)', [('owner','1'),('peer','2')])
            profile.commit()
            profile.close()
            source = sqlite3.connect(root/'nt_msg.db')
            source.execute('CREATE TABLE c2c_msg_table ([40001] INTEGER, [40002], [40020], [40021], [40030], [40050] INTEGER, [40011], [40800])')
            source.executemany('INSERT INTO c2c_msg_table VALUES (?,1,?, ?,NULL,?,1,NULL)',
                               [(1,'peer','peer',1000000),(2,'peer','peer',5),(3,'other','other',5),(4,'peer','peer',6)])
            source.commit()
            source.close()
            archive = SimpleNamespace(db=sqlite3.connect(':memory:'))
            archive.db.execute('CREATE TABLE reader_state (source TEXT PRIMARY KEY, payload TEXT)')
            archive.db.execute('INSERT INTO reader_state VALUES (?,?)',('qq-db',json.dumps(dict(watermark=1000000,full_inventory=True))))
            archive.db.execute('CREATE TABLE messages (payload TEXT)')
            archive.db.execute('INSERT INTO messages VALUES (?)',(json.dumps(dict(source='qq-db',source_id='4')),))
            def open_fixture(config, name):
                db = sqlite3.connect(root/name)
                db.row_factory = sqlite3.Row
                return db
            config = dict(account_id='1',contact_id='2',qq_account_dir=directory,inbox=root/'inbox')
            with patch('chattrace.qq_native.pids'), patch('chattrace.qq_native.open_db',open_fixture):
                checkpoint = collect(config,archive)
            batch = json.loads(next(config['inbox'].glob('*.json')).read_bytes())
            self.assertEqual([m['source_id'] for m in batch['messages']],['2','1'])
            self.assertTrue(checkpoint['full_inventory'])
            archive.db.close()

    def test_recovered_original_rejects_wrong_content(self):
        from chattrace.qq_native import verified_media
        with tempfile.TemporaryDirectory() as name:
            base = Path(name)
            recovery = base/'qq-originals'
            recovery.mkdir()
            data = b'recovered test file'
            md5 = hashlib.md5(data).hexdigest()
            path = recovery/(md5+'.bin')
            path.write_bytes(b'wrong')
            element = parse_content(b'')
            self.assertIsNone(verified_media('file',md5,element,base/'qq',{},base))
            path.write_bytes(data)
            asset = verified_media('file',md5,element,base/'qq',{},base)
            self.assertEqual((base/asset['path']).read_bytes(),data)
