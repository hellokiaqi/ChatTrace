"""Import a single QQNT_Export ChatLab conversation without opening the QQ client."""
import json
from datetime import datetime, timezone
from pathlib import Path
from .core import ArchiveError, atomic, digest, encode

KINDS = {0:'text',1:'image',2:'voice',3:'video',4:'file',5:'sticker',25:'quote',80:'system',81:'recalled'}


def collect(config):
    path = Path(config['qq_export_path'])
    data = json.loads(path.read_text('utf-8-sig'))
    header, meta = data['chatlab'], data['meta']
    owner, peer = config['account_id'], config['contact_id']
    if header.get('version')!='0.0.2' or header.get('generator')!='QQNT_Export':
        raise ArchiveError('UNSUPPORTED_QQ_EXPORT_FORMAT')
    if (meta.get('type')!='private' or meta.get('platform','').lower()!='qq' or
        owner==peer or str(meta.get('ownerId',''))!=owner or
        {str(m['platformId']) for m in data['members']}!={owner,peer}):
        raise ArchiveError('QQ_PRIVATE_CONTACT_MISMATCH')
    records = []
    seen = set()
    for position, row in enumerate(data['messages']):
        sender = str(row['sender'])
        if sender not in (owner,peer):
            raise ArchiveError('QQ_UNEXPECTED_SENDER')
        identifier = row.get('platformMessageId')
        if type(identifier) not in (str,int) or str(identifier) in ('','0') or str(identifier) in seen:
            raise ArchiveError('QQ_MISSING_OR_DUPLICATE_MESSAGE_ID')
        seen.add(str(identifier))
        kind = KINDS.get(row['type'],'unknown')
        body = row['content']
        if body is not None and not isinstance(body,str):
            raise ArchiveError('QQ_CONTENT_FORMAT_UNSUPPORTED')
        records.append(dict(source_id=str(identifier), timestamp=datetime.fromtimestamp(row['timestamp'],timezone.utc).isoformat(),
                            sender='system' if kind in ('system','recalled') else 'self' if sender==owner else 'friend',
                            type=kind, text=body or '', precision='second', sequence=position, occurrence=0,
                            media=[], screenshots=[], reply_to=str(row['replyToMessageId']) if row.get('replyToMessageId') else None,
                            source_metadata={'platform':'qq','sender_id':sender,'export_type':row['type']}))
    batch = dict(schema=1,account_id=owner,contact_id=peer,source='qq-chatlab',
                 captured_at=datetime.fromtimestamp(header['exportedAt'],timezone.utc).isoformat(),messages=records,screenshots=[])
    raw = encode(batch).encode()
    dest = Path(config['inbox'])/('qq-'+digest(raw)+'.json')
    if not dest.exists():
        atomic(dest,raw)
