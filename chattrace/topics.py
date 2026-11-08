"""Map semantic boundary suggestions to ordered, canonical message identities."""
from datetime import datetime


def topics(rows, semantic):
    material = []
    previous = None
    for m in rows:
        instant = datetime.fromisoformat(m['timestamp']) if m['timestamp'] else None
        gap = int((instant - previous).total_seconds()) if instant and previous else None
        material.append([m['id'], m['timestamp'], gap, m['sender'], m['type'],
                         m.get('reply_to'), m.get('source_id'), m['text']])
        previous = instant
    segments = []
    current_block = None
    offset = 0
    while offset < len(rows):
        end = offset
        size = 0
        while end < len(rows) and end-offset < 128:
            cost = len(str(material[end]))
            if end > offset and size + cost > 80000:
                break
            size += cost
            end += 1
        if end < len(rows) and end-offset > 64:
            # Present both sides of nearby pauses without forcing a semantic boundary.
            end = max(range(offset+64,end+1), key=lambda i: (material[i][2] or 0,i))
        core = material[offset:end]
        window = dict(core=core, left=material[max(0,offset-10):offset],
                      right=material[end:end+10], active=segments[-1]['meta'] if segments else None)
        fallback = []
        for mid, timestamp, gap, sender, kind, reply, source, text in core:
            if (not segments and not fallback) or (gap is not None and gap > 21600 and not reply):
                date = timestamp[:10] if timestamp else '日期未知'
                fallback.append(dict(start=mid,new_block=True,title=date+' 对话',
                                     keywords=[date,'微信',kind],description='按时间整理的离线消息段。',substarts=[]))
        response = semantic.suggest(window, {'segments':fallback})
        starts = {s['start']:s for s in response['segments']}
        for row in rows[offset:end]:
            mid = row['id']
            if mid in starts:
                entry = starts[mid]
                if entry['new_block']:
                    current_block = 'B-'+mid
                meta = {k:entry[k] for k in ('title','keywords','description')}
                segments.append(dict(segment_id='T-'+mid,block_id=current_block,meta=meta,
                                     substarts=entry['substarts'],ids=[]))
            segments[-1]['ids'].append(mid)
        offset = end
    if semantic.ready and len(segments)>1:
        positions = {row[0]:i for i,row in enumerate(material)}
        candidates = []
        for seg in segments[1:]:
            mid = seg['ids'][0]
            pos = positions[mid]
            candidates.append(dict(start=mid,gap_seconds=material[pos][2],title=seg['meta']['title'],
                                   before=material[max(0,pos-4):pos],after=material[pos:pos+4]))
        starts = {segments[0]['ids'][0]}
        for start in range(0,len(candidates),64):
            batch = candidates[start:start+64]
            default = [item['start'] for item in batch if (item['gap_seconds'] or 0)>21600 and not item['after'][0][5]]
            starts.update(semantic.blocks(batch,default))
        for seg in segments:
            mid = seg['ids'][0]
            if mid in starts:
                current_block = 'B-'+mid
            seg['block_id'] = current_block
    return segments
