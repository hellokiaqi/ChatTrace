"""Build the offline statistics page from current message versions."""
import json
from datetime import datetime
from pathlib import Path

from .core import atomic, digest, encode, tz


def topic_times(path, messages, zone):
    if not path.is_file():
        return None
    manifest = json.loads(path.read_bytes())
    if not manifest.get('llm_ready') or manifest['input_hash'] != digest(encode(messages).encode()):
        return None
    stamps = {m['id']:m['timestamp'] for m in messages}
    changes = []
    previous_block = previous_segment = None
    for doc in manifest['documents']:
        for segment in doc['sessions']:
            current_block, current_segment = segment['block_id'], segment['segment_id']
            if current_block == previous_block and current_segment != previous_segment:
                stamp = stamps[segment['ids'][0]]
                if stamp:
                    local = datetime.fromisoformat(stamp).astimezone(zone)
                    changes.append(local.replace(tzinfo=None).isoformat())
            previous_block, previous_segment = current_block, current_segment
    return changes


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
    changes = topic_times(root.parent/'retrieval/manifest.json',messages,zone)
    data = json.dumps({'events':events,'undated':undated,'topicTimes':changes},separators=(',',':'))
    body = template.replace('/*STATS_DATA*/null',data).encode()
    path = root/'statistics.html'
    if not path.exists() or path.read_bytes()!=body:
        atomic(path,body)
    return path
