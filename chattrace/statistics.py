"""Build the offline statistics page from current message versions."""
import json
from datetime import datetime
from pathlib import Path

from .core import atomic, tz


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
    data = json.dumps({'events':events,'undated':undated},separators=(',',':'))
    body = template.replace('/*STATS_DATA*/null',data).encode()
    path = root/'statistics.html'
    if not path.exists() or path.read_bytes()!=body:
        atomic(path,body)
    return path
