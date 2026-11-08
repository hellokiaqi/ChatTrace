"""One-shot publication of validated v4 documents through the existing Drive client."""
import io
import re
import json
from .core import ArchiveError, atomic, digest, encode, now
from .drive import GoogleDrive
from .retrieval import START,catalog,validate,records,block

MIME='application/vnd.google-apps.document'

def docs_paragraphs(text):
    return re.sub(r'\n{2,}', lambda match: '\n' * (2 * len(match.group()) - 1), text)

def sync(archive,dry=False,drive_factory=GoogleDrive):
    validate(archive)
    root=archive.root/'v4'
    manifest=json.loads((root/'retrieval/manifest.json').read_bytes())
    docs=manifest['documents']
    lookup={m['id']:m for m in records(archive)}
    memberships={d['key']:d['ids'] for d in docs}
    path=root/'drive-state.json'
    state=json.loads(path.read_bytes()) if path.exists() else {}
    urls={k:'https://docs.google.com/document/d/'+v['id']+'/edit' for k,v in state.items()}
    active={d['key'] for d in docs}|{'00_START_HERE','01_CATALOG'}
    retired=sorted(set(state)-active)
    entries=[(d['key'],d['name'],(root/'retrieval'/d['filename']).read_bytes().decode()) for d in docs]
    entries.append(('00_START_HERE','00_START_HERE',START))
    drive=None if dry else drive_factory(archive.config,archive)
    folder=None if dry else drive.folder('@v4/retrieval','01_Retrieval',drive.contact_root)
    if drive and drive.api.files().get(fileId=folder,fields='shared').execute().get('shared'):
        raise ArchiveError('V4_FOLDER_SHARED')
    plan=[]
    for key,name,body in entries+[('01_CATALOG','01_CATALOG','')]:
        if key=='01_CATALOG':
            body=catalog(docs,urls)
        sha=digest(body.encode())
        row=state.get(key,{})
        action='create' if not row else 'skip' if row.get('sha256')==sha and row.get('name')==name else 'update'
        plan.append([key,action])
        if dry:
            continue
        marker=digest(encode([manifest['namespace'],key]).encode())
        api=drive.api.files()
        remote=row.get('id')
        if not remote:
            found=api.list(q=f"'{folder}' in parents and trashed=false and appProperties has {{key='chatTraceV4' and value='{marker}'}}",fields='files(id)').execute()['files']
            if len(found)>1:
                raise ArchiveError('V4_DUPLICATE_REMOTE_ID')
            remote=found[0]['id'] if found else None
        if remote:
            meta=api.get(fileId=remote,fields='parents,appProperties,trashed,shared,mimeType,name').execute()
            if meta.get('trashed') or meta.get('shared') or folder not in meta['parents'] or meta['mimeType']!=MIME or meta.get('appProperties',{}).get('chatTraceV4')!=marker:
                raise ArchiveError('V4_REMOTE_IDENTITY_MISMATCH')
            if action=='skip' and meta['name']==name:
                continue
        upload=drive.media(io.BytesIO(body.encode()),mimetype='text/plain')
        if remote:
            api.update(fileId=remote,body={'name':name,'mimeType':MIME},media_body=upload).execute()
        else:
            remote=api.create(body={'name':name,'mimeType':MIME,'parents':[folder],'appProperties':{'chatTraceV4':marker}},media_body=upload,fields='id').execute()['id']
        actual=api.export(fileId=remote,mimeType='text/plain').execute().decode('utf-8-sig').replace('\r\n','\n')
        expected=body.replace('\r\n','\n')
        transport = docs_paragraphs(expected.removesuffix('\n'))
        if actual not in (expected,expected+'\n',transport,transport+'\n'):
            raise ArchiveError('V4_NATIVE_CONTENT_MISMATCH')
        for mid in memberships.get(key, []):
            original=block(lookup[mid],manifest['zone'],manifest['namespace']).replace('\r\n','\n')
            if original not in actual and docs_paragraphs(original) not in actual:
                raise ArchiveError('V4_NATIVE_MESSAGE_MISMATCH')
        state[key]={'id':remote,'sha256':sha,'name':name,'verified_at':now()}
        atomic(path,encode(state).encode())
        urls[key]='https://docs.google.com/document/d/'+remote+'/edit'
    return {'status':'dry_run' if dry else 'synced','plan':plan,'pending_retirement':retired,'folder':'https://drive.google.com/drive/folders/'+folder if folder else None}
