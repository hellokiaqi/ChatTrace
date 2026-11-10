"""One-shot publication of validated v4 documents through the existing Drive client."""
import io
import hashlib
import json
from .core import ArchiveError, atomic, digest, encode, now
from .drive import GoogleDrive
from .retrieval import start_here,catalog,validate

MIME='text/markdown'

def sync(archive,dry=False,drive_factory=GoogleDrive):
    validate(archive)
    root=archive.root/'v4'
    manifest=json.loads((root/'retrieval/manifest.json').read_bytes())
    docs=manifest['documents']
    intro=start_here(archive.config)
    path=root/'markdown-drive-state.json'
    state=json.loads(path.read_bytes()) if path.exists() else {}
    urls={k:'https://drive.google.com/file/d/'+v['id']+'/view' for k,v in state.items()}
    active={d['key'] for d in docs}|{'00_START_HERE','01_CATALOG'}
    retired=sorted(set(state)-active)
    entries=[(d['key'],d['filename'],(root/'retrieval'/d['filename']).read_bytes().decode()) for d in docs]
    drive=None if dry else drive_factory(archive.config,archive)
    folder=None if dry else drive.folder('@v4/markdown','01_Retrieval_Markdown',drive.contact_root)
    if drive and drive.api.files().get(fileId=folder,fields='shared').execute().get('shared'):
        raise ArchiveError('V4_FOLDER_SHARED')
    plan=[]
    for key,name,body in entries+[('01_CATALOG','01_CATALOG.md',''),('00_START_HERE','00_START_HERE.md',intro)]:
        if key=='01_CATALOG':
            body=catalog(docs,urls)
        elif key=='00_START_HERE' and '01_CATALOG' in urls:
            body=intro.replace('(01_CATALOG.md)','('+urls['01_CATALOG']+')')
        sha=digest(body.encode())
        row=state.get(key,{})
        action='create' if not row else 'skip' if row.get('sha256')==sha and row.get('name')==name else 'update'
        plan.append([key,action])
        if dry:
            continue
        marker=digest(encode([manifest['namespace'],'markdown',key]).encode())
        api=drive.api.files()
        remote=row.get('id')
        if not remote:
            found=api.list(q=f"'{folder}' in parents and trashed=false and appProperties has {{key='chatTraceV4' and value='{marker}'}}",fields='files(id)').execute()['files']
            if len(found)>1:
                raise ArchiveError('V4_DUPLICATE_REMOTE_ID')
            remote=found[0]['id'] if found else None
        if remote:
            meta=api.get(fileId=remote,fields='parents,appProperties,trashed,shared,mimeType,name,md5Checksum').execute()
            if meta.get('trashed') or meta.get('shared') or folder not in meta['parents'] or meta['mimeType']!=MIME or meta.get('appProperties',{}).get('chatTraceV4')!=marker:
                raise ArchiveError('V4_REMOTE_IDENTITY_MISMATCH')
            if action=='skip' and meta['name']==name and meta.get('md5Checksum')==hashlib.md5(body.encode()).hexdigest():
                continue
        if remote and action=='skip':
            plan[-1][1]='update'
        upload=drive.media(io.BytesIO(body.encode()),mimetype=MIME)
        if remote:
            api.update(fileId=remote,body={'name':name,'mimeType':MIME},media_body=upload).execute()
        else:
            remote=api.create(body={'name':name,'mimeType':MIME,'parents':[folder],'appProperties':{'chatTraceV4':marker}},media_body=upload,fields='id').execute()['id']
        if api.get_media(fileId=remote).execute()!=body.encode():
            raise ArchiveError('MARKDOWN_CONTENT_MISMATCH')
        state[key]={'id':remote,'sha256':sha,'name':name,'verified_at':now()}
        atomic(path,encode(state).encode())
        urls[key]='https://drive.google.com/file/d/'+remote+'/view'
    return {'status':'dry_run' if dry else 'synced','plan':plan,'pending_retirement':retired,'folder':'https://drive.google.com/drive/folders/'+folder if folder else None}
