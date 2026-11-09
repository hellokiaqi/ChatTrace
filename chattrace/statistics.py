"""Build the offline statistics page from current message versions."""
import json
from datetime import datetime
from pathlib import Path

from .core import atomic, digest, encode, tz


def topic_activity(path, messages, zone):
    if not path.is_file():
        return None
    manifest = json.loads(path.read_bytes())
    if not manifest.get('llm_ready') or manifest['input_hash'] != digest(encode(messages).encode()):
        return None
    by_id = {m['id']:m for m in messages}
    topics = []
    active = None
    changes = []
    previous_block = previous_segment = None
    for doc in manifest['documents']:
        for segment in doc['sessions']:
            current_block, current_segment = segment['block_id'], segment['segment_id']
            if current_block == previous_block and current_segment != previous_segment:
                stamp = by_id[segment['ids'][0]]['timestamp']
                if stamp:
                    local = datetime.fromisoformat(stamp).astimezone(zone)
                    changes.append(local.replace(tzinfo=None).isoformat())
            if current_segment != previous_segment:
                active = {'time':None,'role':None,'otherTurns':0,'switch':current_block==previous_block}
                topics.append(active)
                last_role = None
            for mid in segment['ids']:
                message = by_id[mid]
                role = message['sender']
                if role not in ('self','friend') or not message['timestamp']:
                    continue
                if active['role'] is None:
                    active['role'] = role
                    active['time'] = datetime.fromisoformat(message['timestamp']).astimezone(zone).replace(tzinfo=None).isoformat()
                if role != active['role'] and role != last_role:
                    active['otherTurns'] += 1
                last_role = role
            previous_block, previous_segment = current_block, current_segment
    return {'changes':changes,'topics':[t for t in topics[1:] if t['role']]}


def statistics_page(root, messages):
    zone = tz('Asia/Shanghai')
    events = []
    undated = 0
    for message in messages:
        if not message.get('timestamp'):
            undated += 1
            continue
        instant = datetime.fromisoformat(message['timestamp']).astimezone(zone)
        sender = message['sender']
        if sender not in ('self', 'friend', 'system'):
            sender = 'unknown'
        chars = 0
        if sender in ('self','friend') and message['type'] in ('text','quote'):
            chars = sum(not char.isspace() for char in message['text'])
        events.append([instant.replace(tzinfo=None).isoformat(),sender,chars])
    events.sort(key=lambda event:event[0])
    template = Path(__file__).with_name('statistics.html').read_text('utf-8')
    activity = topic_activity(root.parent/'retrieval/manifest.json',messages,zone)
    payload = {'events':events,'undated':undated,'topicTimes':activity['changes'] if activity else None,
               'topicActivity':activity['topics'] if activity else None}
    safe_json = json.dumps(payload,separators=(',',':')).replace('<','\\u003c')
    body = template.replace('/*STATS_DATA*/null',safe_json).encode()
    path = root/'statistics.html'
    if not path.exists() or path.read_bytes()!=body:
        atomic(path,body)
    return path
