"""Rebuildable local views of the current archive."""
import html
import json
import os
import re
import shutil
import tempfile
from urllib.parse import quote
from collections import defaultdict
from datetime import datetime
from pathlib import Path

from .core import ArchiveError, atomic, digest, encode, tz
from .semantic import Semantic, PROMPT
from .topics import topics

START="""# ChatTrace 正式检索入口

[打开检索目录](01_CATALOG.md)

先查 01_CATALOG，按时间或话题打开所列正式 Retrieval 文档。每卷可跨日期并包含多个 Topic Segment。标题、关键词及描述用于导航；回答时读取 [原始聊天]，引用时间、发送者与稳定消息 ID，并按需展开相邻上下文。
正式检索的原文来源仅限目录所列正文。Human Archive 和分析记录用于其他用途。检索未命中时报告未找到；已导入消息的导出数量与微信全历史覆盖分别核验。无法确认的事项请明确标注。"""
def write(path, data):
    data=data.encode('utf-8')
    if not path.exists() or path.read_bytes()!=data:
        atomic(path,data)

def current_records(archive):
    records = [{**json.loads(r['payload']), 'id': f"M{r['id']:06d}", 'identity': r['identity']}
               for r in archive.db.execute('SELECT * FROM messages')]
    return sorted(records, key=lambda m: (m['timestamp'] or '', m['sequence'], m['id']))


def records(archive):
    def key(m):
        time=datetime.fromisoformat(m['timestamp']).timestamp() if m['timestamp'] else float('-inf')
        return time,m['sequence'],m['id']
    return sorted(current_records(archive),key=key)

def stamp(m, zone):
    return datetime.fromisoformat(m['timestamp']).astimezone(tz(zone)).isoformat() if m['timestamp'] else '时间未知'

def block(m, zone, namespace=None):
    body=m['text']
    if m['type']!='text':
        ids=' '.join(Path(a['path']).stem for a in m['media'])
        body+=('\n' if body else '')+'['+m['type']+(' '+ids if ids else '')+']'
    if m.get('reply_to'):
        body+='\n[来源引用 '+encode(m['reply_to'])+']'
    identity = namespace + ':' + m['id'] if namespace else m['id']
    return f"[{stamp(m,zone)}][{identity}][{m['sender']}]\n"+body

def human(archive, root, rows,zone):
    months=defaultdict(list)
    for m in rows:
        months[m['date'][:7]].append(m)
    esc=html.escape
    header="""<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width"><style>body{max-width:52rem;margin:auto;background:#f5f5f5;font:16px system-ui;padding:1rem}article{background:white;padding:.7rem;margin:.7rem 20% .7rem 0;border-radius:12px;white-space:pre-wrap;overflow-wrap:anywhere}article.self{margin: .7rem 0 .7rem 20%;background:#def6d6}small{display:block;color:#555}img{max-width:100%;max-height:22rem}nav{position:sticky;top:0;background:#fff;padding:.5rem}</style>"""
    index=[]
    for month,items in months.items():
        days=sorted({m['date'] for m in items})
        parts=[header,'<nav><a href="index.html">月份</a> '+ ' '.join(f'<a href="#d{d}">{d}</a>' for d in days)+' <input placeholder="搜索本月原文或ID" oninput="for(const a of document.querySelectorAll(\'article\'))a.hidden=!a.textContent.includes(this.value)"></nav>']
        day=None
        for m in items:
            if day!=m['date']:
                day=m['date']
                parts.append(f'<h2 id="d{day}">{day}</h2>')
            who={'friend':archive.config['contact_name'],'self':'我'}.get(m['sender'],m['sender'])
            parts.append(f'<article id="{m["id"]}" class="{esc(m["sender"])}"><small>{esc(who)} · {stamp(m,zone)} · {m["id"]}</small>'+esc(m['text']))
            for asset in m['media']:
                path=archive.root/asset['path']
                url=esc(Path(os.path.relpath(path,root)).as_posix(),quote=True)
                parts.append(f'<img loading="lazy" src="{url}" alt="{esc(m["type"])}">' if path.suffix.lower() in ('.png','.jpg','.jpeg','.gif','.webp','.bmp') else f'<a href="{url}">媒体原件</a>')
            if m['type']!='text' and not m['media']:
                parts.append(' ['+esc(m['type'])+']')
            parts.append('</article>')
        write(root/(month+'.html'),''.join(parts))
        index.append(f'<p><a href="{month}.html">{month}（{len(items)} 条）</a></p>')
    from .statistics import statistics_page
    statistics_page(root,rows)
    write(root/'index.html',header+'<h1>聊天归档</h1><p><a href="statistics.html">双方聊天统计</a></p>'+''.join(index))


def document(parts, number, lookup, zone, namespace, contact):
    ids = [mid for part in parts for mid in part['ids']]
    first, last = lookup[ids[0]], lookup[ids[-1]]
    titles = list(dict.fromkeys(s['meta']['title'] for s in parts))
    # First and last themes make the short display title useful in a file list.
    caption = '、'.join(dict.fromkeys([titles[0], titles[-1]]))[:38]
    caption = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '', caption).rstrip(' .') or '对话'
    dates = [r['date'].replace('-','') for r in (first,last)]
    name = f'CT-R-{dates[0]}-{dates[1]}-{number:02}｜{caption}'
    words = list(dict.fromkeys(w for i in range(6) for s in parts for w in s['meta']['keywords'][i:i+1]))[:8]
    return dict(key=f"CT-R-{namespace}-{first['id']}",name=name,filename=name+'.md',namespace=namespace,
                contact=contact,anchor=first['id'],start=stamp(first,zone),end=stamp(last,zone),
                ids=ids,sessions=list(parts),keywords=words)


def render(doc, lookup, zone):
    ids = doc['ids']
    titles = list(dict.fromkeys(s['meta']['title'] for s in doc['sessions']))
    lines = ['# '+doc['name'],'','## 文档信息','',f"文档ID：{doc['key']}",f"namespace：{doc['namespace']}",
             '来源：ChatTrace / WeChat',f"参与者：我、{doc['contact']}",f"起止时间：{doc['start']} — {doc['end']}",
             f'消息数量：{len(ids)}',f'首条消息：{ids[0]}',f'末条消息：{ids[-1]}',
             '覆盖状态：本卷覆盖已导入范围；微信全历史未验证。','','## 文档级检索关键词','',
             '关键词：'+'、'.join(doc['keywords']),'主要话题：'+'；'.join(titles),'','## 话题导航','']
    body = []
    for i,s in enumerate(doc['sessions'],1):
        meta = s['meta']
        first,last = s['ids'][0],s['ids'][-1]
        tag = s['segment_id'] + ('（续）' if s.get('continuation') else '')
        lines.extend([f"{i:02}. {meta['title']} · {tag}",
                      f"时间范围：{stamp(lookup[first],zone)} — {stamp(lookup[last],zone)}",
                      f'消息 ID：{first} — {last}', '关键词：'+'、'.join(meta['keywords']),
                      '简短描述：'+meta['description'],''])
        body.append(f'### Topic Segment {i:02} · {tag}')
        original = '\n\n'.join(block(lookup[mid],zone,doc['namespace']) for mid in s['ids'])
        fence = '`' * max(3,1+max((len(run) for run in re.findall(r'`+',original)),default=0))
        body.append(fence+'text\n'+original+'\n'+fence)
    return '\n'.join(lines)+'\n## 原始聊天\n\n'+'\n\n'.join(body)+'\n'


def pack(segments, lookup, zone, namespace, contact, soft, hard):
    docs, parts = [], []
    def measure(candidate):
        d = document(candidate,len(docs)+1,lookup,zone,namespace,contact)
        d['chars'] = len(render(d,lookup,zone))
        return d
    def finish():
        docs.append(measure(parts))
        parts.clear()
    for seg in segments:
        entry = {k:v for k,v in seg.items() if k!='substarts'}
        if parts:
            current = measure(parts)
            new_block = parts[-1]['block_id'] != entry['block_id']
            combined_size = measure(parts+[entry])['chars']
            if combined_size>hard or (current['chars']>=soft and (new_block or combined_size>12000)):
                finish()
        if measure([entry])['chars'] <= hard:
            parts.append(entry)
            continue
        remaining = entry['ids']
        while remaining:
            lo, hi = 0, len(remaining)
            while lo < hi:
                mid = (lo+hi+1)//2
                if measure([{**entry,'ids':remaining[:mid]}])['chars'] <= hard:
                    lo = mid
                else:
                    hi = mid-1
            if not lo:
                raise ArchiveError('V4_SINGLE_MESSAGE_EXCEEDS_DOCUMENT_LIMIT')
            if lo < len(remaining):
                preferred = [i for i in range(1,lo+1) if remaining[i] in seg['substarts']]
                if preferred:
                    lo = preferred[-1]
            parts.append({**entry,'ids':remaining[:lo]})
            remaining = remaining[lo:]
            if remaining:
                finish()
                entry = {**entry,'continuation':True}
    if parts:
        finish()
    # Keep a short closing exchange with its lead-in when the whole volume still fits.
    if len(docs)>1 and docs[-1]['chars']<soft:
        left,right = docs[-2:]
        if left['end']!='时间未知' and right['start']!='时间未知':
            pause = datetime.fromisoformat(right['start']) - datetime.fromisoformat(left['end'])
            if pause.total_seconds() <= 90*60:
                joined = document(left['sessions']+right['sessions'],len(docs)-1,lookup,zone,namespace,contact)
                joined['chars'] = len(render(joined,lookup,zone))
                if joined['chars']<=hard:
                    docs[-2:] = [joined]
    return docs


def catalog(docs, remote):
    entries = ['# ChatTrace 正式检索目录｜已导入消息；微信历史覆盖未验证']
    for d in docs:
        titles = list(dict.fromkeys(s['meta']['title'] for s in d['sessions']))
        entries.append('\n'.join(['## '+d['name'],'',d['key'],f"{d['start']} — {d['end']} · {len(d['ids'])} 条 · {d['ids'][0]} — {d['ids'][-1]}",
                                  '主要话题：'+'；'.join(titles),'关键词：'+'、'.join(d['keywords']),
                                  '[打开正文]('+remote.get(d['key'],quote(d['filename']))+')','覆盖：已导入范围']))
    return '\n\n'.join(entries)+'\n'


def location(archive, preview):
    return archive.root/('retrieval/v4_preview' if preview else 'v4/retrieval')


def inspect_view(archive, folder):
    manifest = json.loads((folder/'manifest.json').read_bytes())
    rows = records(archive)
    lookup = {r['id']:r for r in rows}
    docs = manifest['documents']
    ids = [i for d in docs for s in d['sessions'] for i in s['ids']]
    if ids!=list(lookup) or len(ids)!=len(rows) or manifest['input_hash']!=digest(encode(rows).encode()):
        raise ArchiveError('V4_SOURCE_CHANGED_OR_COVERAGE_MISMATCH')
    active_block = previous_topic = None
    for d in docs:
        for s in d['sessions']:
            first = s['ids'][0]
            if s.get('continuation'):
                if (s['segment_id'],s['block_id']) != (previous_topic,active_block):
                    raise ArchiveError('V4_CONTINUATION_MISMATCH')
            elif s['segment_id'] != 'T-'+first:
                raise ArchiveError('V4_SEGMENT_MISMATCH')
            if s['block_id'] != active_block and s['block_id'] != 'B-'+first:
                raise ArchiveError('V4_BLOCK_MISMATCH')
            active_block, previous_topic = s['block_id'], s['segment_id']
    names = {'01_CATALOG.md','00_START_HERE.md'}
    for n,d in enumerate(docs,1):
        expected = document(d['sessions'],n,lookup,manifest['zone'],manifest['namespace'],archive.config['contact_name'])
        if any(d[k]!=v for k,v in expected.items()):
            raise ArchiveError('V4_DOCUMENT_METADATA_MISMATCH')
        names.add(d['filename'])
        raw = (folder/d['filename']).read_bytes()
        body = render(d,lookup,manifest['zone'])
        if raw!=body.encode() or len(body)!=d['chars'] or len(body)>manifest['hard_limit'] or d['sha256']!=digest(raw):
            raise ArchiveError('V4_DOCUMENT_MISMATCH')
    if {p.name for p in folder.glob('*.md')}!=names or (folder/'01_CATALOG.md').read_bytes()!=catalog(docs,{}).encode() or (folder/'00_START_HERE.md').read_bytes()!=START.encode():
        raise ArchiveError('V4_NAVIGATION_MISMATCH')
    if any(not (archive.root/a['path']).is_file() for r in rows for a in r['media']):
        raise ArchiveError('V4_MISSING_ASSET')
    return dict(status='validated',source_messages=len(rows),unique_exported=len(set(ids)),documents=len(docs),
                segments=len({s['segment_id'] for d in docs for s in d['sessions']}),history='unverified',
                span_warnings=[d['name'] for d in docs if d['start']!='时间未知' and d['end']!='时间未知' and
                               (datetime.fromisoformat(d['end'])-datetime.fromisoformat(d['start'])).total_seconds()>7*86400])


def validate(archive, preview=False):
    return inspect_view(archive,location(archive,preview))


def install_view(archive, stage, target):
    # A staged package contains both the manifest and documents. Preserve the preceding generation.
    history = archive.root/'retrieval-history'
    history.mkdir(exist_ok=True)
    target.parent.mkdir(parents=True,exist_ok=True)
    if not stage.resolve().is_relative_to(archive.root.resolve()) or target not in (location(archive,True),location(archive,False)):
        raise ArchiveError('V4_INVALID_STAGE')
    previous = None
    if target.exists():
        previous = Path(tempfile.mkdtemp(prefix=target.name+'-',dir=history))/'view'
        target.rename(previous)
    try:
        stage.rename(target)
    except OSError:
        if previous is not None:
            previous.rename(target)
        raise


def promote(archive):
    preview, active = location(archive,True),location(archive,False)
    result = inspect_view(archive,preview)
    manifest = (preview/'manifest.json').read_bytes()
    if not (active/'manifest.json').exists() or (active/'manifest.json').read_bytes()!=manifest:
        stage = Path(tempfile.mkdtemp(dir=archive.root,prefix='publish-'))
        shutil.copytree(preview,stage,dirs_exist_ok=True)
        install_view(archive,stage,active)
    result = inspect_view(archive,active)
    write(archive.root/'v4/manifest.json',manifest.decode())
    return result


def build(archive, enabled=None, refresh=False, preview=False):
    cfg = archive.config
    soft, hard = cfg.get('v4_soft_chars',8000),cfg.get('v4_max_chars',16000)
    if not 0 < soft <= hard <= 16000:
        raise ArchiveError('INVALID_V4_SPLIT_CONFIG')
    rows = records(archive)
    lookup = {m['id']:m for m in rows}
    root = archive.root/'v4'
    root.mkdir(exist_ok=True)
    sem = Semantic(root,enabled,refresh)
    zone = cfg.get('timezone','Asia/Shanghai')
    ns = digest(encode(['wechat',cfg['account_id'],cfg['contact_id']]).encode())[:16]
    docs = pack(topics(rows,sem),lookup,zone,ns,cfg['contact_name'],soft,hard)
    stage = Path(tempfile.mkdtemp(dir=archive.root,prefix='build-'))
    try:
        for d in docs:
            body = render(d,lookup,zone)
            d['sha256'] = digest(body.encode())
            write(stage/d['filename'],body)
        manifest = dict(version=4,schema=2,namespace=ns,zone=zone,soft_limit=soft,hard_limit=hard,
                        input_hash=digest(encode(rows).encode()),model=sem.model,llm_ready=sem.ready,
                        prompt_hash=digest(PROMPT.encode()),documents=docs,
                        navigation_hash=digest(encode([START,catalog(docs,{})]).encode()))
        write(stage/'01_CATALOG.md',catalog(docs,{}))
        write(stage/'00_START_HERE.md',START)
        write(stage/'manifest.json',encode(manifest))
        result = inspect_view(archive,stage)
        target = location(archive,True)
        if not (target/'manifest.json').exists() or (target/'manifest.json').read_bytes()!=(stage/'manifest.json').read_bytes():
            install_view(archive,stage,target)
        else:
            inspect_view(archive,target)
    finally:
        if stage.exists() and stage.resolve().parent==archive.root.resolve():
            shutil.rmtree(stage)
    if not preview:
        result = promote(archive)
        human(archive,root/'human',rows,zone)
    return {**result,'llm':sem.stats,'folder':str(location(archive,preview))}
