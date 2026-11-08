"""Metadata contracts for retrieval; never infer conversational meaning."""
from collections import Counter
import re
from pathlib import Path
from .core import ArchiveError, digest


def original_block(text, label='原始正文'):
    fence = '`' * max(3, max((len(m[0]) + 1 for m in re.finditer(r'`+', text)), default=0))
    return f'{label}开始\n{fence}text\n{text}\n{fence}\n{label}结束'


def resource_record(archive, path, kind):
    data = (archive.root / path).read_bytes()
    sha256 = digest(data)
    if Path(path).stem != ('S' if kind == 'screenshot' else 'A') + sha256:
        raise ArchiveError('RESOURCE_ID_HASH_MISMATCH')
    tracked = archive.db.execute('SELECT remote_id,uploaded FROM files WHERE path=?', (path,)).fetchone()
    remote_id = tracked['remote_id'] if tracked else None
    verified = bool(remote_id and tracked['uploaded'] == sha256)
    role = 'linked_screenshot' if kind == 'screenshot' else 'derived_preview' if kind.endswith('_preview') or kind == 'thumbnail' else 'original_media'
    return {'resource_id': Path(path).stem, 'path': path, 'kind': kind, 'role': role,
            'sha256': sha256, 'size': len(data), 'drive_file_id': remote_id,
            'cloud_status': 'last_upload_verified' if verified else 'not_verified',
            'url': f'https://drive.google.com/file/d/{remote_id}/view' if verified else None}


def daily_intro(config, day, messages, evidence, source_count):
    from .coverage import visual_required, screenshot_policy
    from .core import timestamp, tz
    zone = config.get('timezone', 'Asia/Shanghai')
    times = sorted(m['timestamp'] for m in messages if m['timestamp'])
    bounds = [timestamp(t).astimezone(tz(zone)).isoformat() for t in (times[0], times[-1])] if times else ['未知', '未知']
    media = Counter(evidence[m['id']]['media'] for m in messages)
    visual = Counter(evidence[m['id']]['visual'] for m in messages)
    required = [m for m in messages if visual_required(config, m)]
    metrics = {'collection': {'status': 'scope_unverified', 'stored_messages': source_count, 'included_messages': len(messages)},
               'media': dict(media), 'visual': {'policy': screenshot_policy(config), 'required': len(required),
                    'required_pending': sum(evidence[m['id']]['visual'] != 'confirmed_full' for m in required), **dict(visual)}}
    lines = [f"归档角色：{config.get('drive_contact_name', 'canonical_daily')}；格式版本：2。",
             f'日期：{day}；时区：{zone}；消息时间范围：{bounds[0]} 至 {bounds[1]}。',
             f'本日来源清单已存 {source_count} 条；本文件选入 {len(messages)} 条。来源历史覆盖：scope_unverified（尚未核实，不表示已发现遗漏）。',
             'ID为本库持久化入库标识，不是时间序号；编号或时间间隔不记录漏采数量。以时间戳、sequence排序。',
             'reply_to仅记录显式引用目标；null表示未记录引用，不表示没有回应。原文见正文围栏及messages.jsonl，标签和媒体说明不属于原话。',
             '截图未关联不等于消息缺失。linked_unverified=关联未核对；partial_only=仅部分可见；confirmed_full=该消息至少有一张完整确认截图，均不评价整个时段的采集完整性。',
             f"媒体：已提取 {media['extracted']}，待补 {media['pending'] + media['thumbnail_only']}，不适用 {media['not_applicable']}。",
             f"视觉：完整确认 {visual['confirmed_full']}；仅部分 {visual['partial_only']}；关联未核对 {visual['linked_unverified']}；无截图证据 {visual['missing']}。策略 {screenshot_policy(config)} 要求 {len(required)} 条，待补 {metrics['visual']['required_pending']} 条。",
             '媒体文件、衍生预览与聊天截图分别标识；只提供原文件链接，未展示图片不表示媒体丢失。云端映射为最近一次上传验证结果。', '']
    return lines, metrics
