"""Single-contact QQNT source, mapped to the existing ChatTrace batch format."""
import hashlib
import json
import re
import io
import requests
from PIL import Image
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from .core import ArchiveError, atomic, digest, encode, now
from .qq_snapshot import open_db, pids
from .vendor.qqnt_elements import parse_40800


def collect(config, archive):
    pids()
    profile=open_db(config,'profile_info.db')
    try:
        def identities(number):
            return {row[0] for row in profile.execute('SELECT DISTINCT [1000] FROM profile_info_v6 WHERE [1002]=?',(number,)) if row[0]}
        peers=identities(config['contact_id'])
        owners=identities(config['account_id'])
        if len(peers)!=1 or not owners or owners & peers:
            raise ArchiveError('QQ_TARGET_NOT_UNIQUE')
        peer=peers.pop()
    finally:
        profile.close()
    archive.db.execute('CREATE TABLE IF NOT EXISTS reader_state (source TEXT PRIMARY KEY,payload TEXT NOT NULL)')
    saved=archive.db.execute('SELECT payload FROM reader_state WHERE source=?',('qq-db',)).fetchone()
    state=json.loads(saved[0]) if saved else {}
    db=open_db(config,'nt_msg.db')
    try:
        sql='SELECT [40001] AS id,[40002] AS seq,[40020] AS sender,[40030] AS peer_number,[40050] AS ts,[40011] AS kind,[40800] AS body FROM c2c_msg_table WHERE [40021]=?'
        params=[peer]
        if saved and not config.get('backfill'):
            since=state['watermark']-config.get('overlap_hours',72)*3600
            rows=list(db.execute(sql+' AND [40050]>=?',(peer,since)))
            if state.get('full_inventory'):
                known = {str(r[0]) for r in archive.db.execute(
                    "SELECT json_extract(payload,'$.source_id') FROM messages WHERE json_extract(payload,'$.source')='qq-db'")}
                older = db.execute('SELECT [40001] FROM c2c_msg_table WHERE [40021]=? AND ([40050]<? OR [40050] IS NULL)', (peer,since))
                for (source_id,) in older:
                    if str(source_id) not in known:
                        rows.extend(db.execute(sql+' AND [40001]=?', (peer,source_id)))
            pending=[json.loads(row[0]) for row in archive.db.execute("SELECT payload FROM messages WHERE json_extract(payload,'$.needs_review')=1")]
            for old in pending:
                if old['source']=='qq-db':
                    rows.extend(db.execute(sql+' AND [40001]=?',(peer,int(old['source_id']))))
            rows=list({r['id']:r for r in rows}.values())
        else:
            if not config.get('backfill'):
                sql+=' ORDER BY [40050] DESC,[40002] DESC,[40001] DESC LIMIT ?'
                params.append(config.get('initial_message_limit',10))
            rows=list(db.execute(sql,params))
        refs = {}
        rows.sort(key=lambda r:(r['ts'] or 0,r['seq'] or 0,r['id']))
        parsed=[]
        names=set()
        for row in rows:
            if row['peer_number'] and str(row['peer_number']) not in ('0',config['contact_id']):
                raise ArchiveError('QQ_TARGET_NUMBER_MISMATCH')
            try:
                elements,error=parse_40800(row['body'] or b'')
            except (ValueError,IndexError,RecursionError):
                elements,error=[],True
            for element in elements:
                media_hash = element['md5_hex'] or element['sticker_hex']
                if media_hash:
                    names.add(media_hash.lower())
            parsed.append((row,elements,error))
            if row['kind']!=9:
                continue
            for e in elements:
                if e['content_type']==7 and e['reply_msg_seq'] and e['reply_msg_time']:
                    match = db.execute('SELECT [40001] FROM c2c_msg_table WHERE [40021]=? AND [40005]=? AND [40050]=? AND [40033]=?',
                                       (peer,e['reply_msg_seq'],e['reply_msg_time'],e['reply_sender_number'])).fetchall()
                    if len(match)==1:
                        refs[row['id']]=str(match[0][0])
    finally:
        db.close()
    if not rows and not state:
        raise ArchiveError('QQ_TARGET_MESSAGES_NOT_LOCAL')
    base=Path(config['inbox'])
    base.mkdir(parents=True,exist_ok=True)
    media_root=Path(config['qq_account_dir'])/'nt_qq/nt_data'
    files=defaultdict(list)
    if names:
        for path in media_root.rglob('*'):
            prefix=path.name[:32].lower()
            if prefix in names and path.is_file():
                files[prefix].append(path)
    attempted = set() if config.get('qq_media_download', True) else None
    messages=[message(row,elements,error,peer,owners,media_root,files,base,attempted) for row,elements,error in parsed]
    for row,item in zip(rows,messages):
        if item['type']=='quote' and not item['reply_to']:
            item['reply_to']=refs.get(row['id'])
    checksum=digest(encode(messages).encode())
    checkpoint={'watermark':max([state.get('watermark',0)]+[r['ts'] or 0 for r in rows]),'digest':checksum,'full_inventory':bool(config.get('backfill') or state.get('full_inventory'))}
    if messages and checksum!=state.get('digest'):
        batch=dict(schema=1,source='qq-db',account_id=config['account_id'],contact_id=config['contact_id'],
                   captured_at=now(),messages=messages,screenshots=[])
        path=base/('qq-db-'+checksum+'.json')
        if not path.exists():
            atomic(path,encode(batch).encode())
    return checkpoint


def message(row,elements,error,peer,owners,media_root,files,base,attempted=None):
    text=[]
    parts=[]
    media=[]
    kinds=[]
    missing=0
    reference=None
    quoted=None
    for e in elements:
        tag=e['content_type']
        part={'tag':tag}
        if tag==1:
            kind='text'
            text.append(e['text'] or '')
        elif tag==6 and e['video_text']:
            kind='emoji'
            text.append(e['video_text'])
            part['face_index']=e['video_flag']
        elif tag==7:
            kind='quote'
            reference=str(e['reply_msg_id']) if e['reply_msg_id'] else None
            part['reply_seq']=e['reply_msg_seq']
            quoted=e['reply_summary']
        elif tag in (2,3,4,5,9):
            kind={2:'image',3:'file',4:'voice',5:'sticker',9:'video'}[tag]
            if tag==2 and (e['sticker_hex'] or 'emoji' in (e['local_path'] or '').lower()):
                kind='sticker'
            h=(e['md5_hex'] or e['sticker_hex'] or '').lower()
            part['md5']=h
            asset=verified_media(kind,h,e,media_root,files,base,attempted)
            if asset:
                media.append(asset)
                part['asset']=asset['path']
                if asset['kind']=='image_preview':
                    missing+=1
            else:
                missing+=1
        else:
            kind='unknown'
        kinds.append(kind)
        parts.append(part)
    if row['kind']==5:
        kind='system'
        body=''.join(e.get('sys_content') or e.get('text') or '' for e in elements)
    else:
        kind=next((k for k in kinds if k in ('image','sticker','video','file','voice')),None)
        kind=kind or next((k for k in kinds if k!='text'),'text' if kinds else 'unknown')
        body=''.join(text)
    sender='system' if kind=='system' else 'friend' if row['sender']==peer else 'self' if row['sender'] in owners else 'unknown'
    metadata={'platform':'qq','message_type':row['kind'],'parts':parts,'body_sha256':digest(row['body'] or b''),
              'parse_incomplete':bool(error or 'unknown' in kinds),'missing_media_parts':missing,
              'media_status':'local_media_missing' if missing else 'local_md5_verified' if media else 'not_applicable'}
    return dict(source_id=str(row['id']),timestamp=datetime.fromtimestamp(row['ts'],timezone.utc).isoformat() if row['ts'] else None,
                sender=sender,type=kind,text=body,precision='second' if row['ts'] else 'unknown',
                sequence=row['seq'] or row['id'],occurrence=0,media=media,screenshots=[],source_metadata=metadata,
                quoted_text=quoted,reply_to=reference)


def verified_media(kind,md5,element,root,files,base,attempted=None):
    if not re.fullmatch('[a-f0-9]{32}',md5):
        return None
    paths=[Path(element[k]) for k in ('local_path','filepath') if element[k]]+files.get(md5,[])
    recovery = (base/'qq-originals').resolve()
    paths.extend(sorted(recovery.glob(md5+'.*')))
    preview = None
    for path in paths:
        path=path.resolve()
        recovered = path.parent == recovery and path.stem == md5
        if not (recovered or path.is_relative_to(root.resolve())) or not path.is_file():
            continue
        data=path.read_bytes()
        if hashlib.md5(data).hexdigest()!=md5:
            if not recovered and kind in ('sticker','image') and (path.stem.lower().startswith(md5) or str(path)==element['local_path']):
                try:
                    with Image.open(io.BytesIO(data)) as im:
                        ext={'JPEG':'.jpg','PNG':'.png','GIF':'.gif','WEBP':'.webp'}.get(im.format)
                        im.verify()
                    if ext:
                        preview=(data,ext)
                except (OSError,SyntaxError):
                    pass
            continue
        suffix=path.suffix.lower()
        if not re.fullmatch(r'\.[a-z0-9]{1,8}',suffix):
            suffix='.bin'
        rel='assets/'+digest(data)+suffix
        if not (base/rel).exists():
            atomic(base/rel,data)
        return {'kind':kind,'path':rel}
    if attempted is not None and kind in ('image','sticker'):
        if download_original(md5, element, recovery, attempted):
            return verified_media(kind,md5,element,root,files,base)
    if preview:
        data,ext=preview
        rel='assets/'+digest(data)+ext
        if not (base/rel).exists():
            atomic(base/rel,data)
        return {'kind':'image_preview','path':rel}
    return None


def download_original(md5, element, recovery, attempted):
    urls = []
    path = element.get('cdn_url') or ''
    if path.startswith('/download?'):
        urls.append('https://multimedia.nt.qq.com.cn' + path)
    urls.append(f'https://gchat.qpic.cn/gchatpic_new/0/0-0-{md5.upper()}/0')
    for url in urls:
        if url in attempted:
            continue
        attempted.add(url)
        try:
            response = requests.get(url, timeout=(5,15), allow_redirects=False)
            if response.status_code != 200:
                continue
            data = response.content
            if element.get('filesize') and len(data) != element['filesize']:
                continue
            if hashlib.md5(data).hexdigest() != md5:
                continue
            with Image.open(io.BytesIO(data)) as picture:
                suffix = {'JPEG':'.jpg','PNG':'.png','GIF':'.gif','WEBP':'.webp','BMP':'.bmp'}.get(picture.format)
                picture.verify()
        except (requests.RequestException,OSError,SyntaxError):
            continue
        if suffix:
            atomic(recovery/(md5+suffix),data)
            return True
    return False
