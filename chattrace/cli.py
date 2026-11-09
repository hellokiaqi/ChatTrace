import argparse
import importlib.util
import json
import platform
import sqlite3
import subprocess
import sys
from pathlib import Path
from .core import ArchiveError, atomic, encode, load_config, run, safe_error, upload_exclusion


def main(argv=None):
    parser = argparse.ArgumentParser(description='ChatTrace — 微信 4.1.15.56 单好友归档')
    parser.add_argument('--config', default='config.json')
    commands = parser.add_subparsers(dest='command', required=True)
    runner = commands.add_parser('run')
    runner.add_argument('--no-upload', action='store_true', help='提交本地归档但不联网上传')
    runner.add_argument('--backfill', action='store_true', help='补采目标好友全部本机消息，不受初始条数和时间窗口限制')
    for name in ('doctor', 'init', 'auth', 'wechat-auth', 'status', 'pending', 'retrieval', 'update','stats'):
        commands.add_parser(name)
    builder = commands.add_parser('build')
    mode = builder.add_mutually_exclusive_group()
    mode.add_argument('--llm', dest='llm', action='store_true')
    mode.add_argument('--no-llm', dest='llm', action='store_false')
    builder.set_defaults(llm=None)
    builder.add_argument('--refresh-metadata', action='store_true')
    builder.add_argument('--preview', action='store_true')
    commands.add_parser('validate').add_argument('--preview', action='store_true')
    commands.add_parser('promote')
    commands.add_parser('sync').add_argument('--dry-run', action='store_true')
    coverage_parser = commands.add_parser('coverage')
    coverage_parser.add_argument('--details', action='store_true', help='包含每条消息的证据状态')
    search = commands.add_parser('search')
    search.add_argument('keyword')
    capture = commands.add_parser('capture', help='保存已打开的目标独立聊天窗口，输出待核对图片')
    capture.add_argument('--bottom', type=int, help='保留画面的下边界像素；默认裁去底部140像素')
    capture.add_argument('--height', type=int, default=1200, help='临时拉高目标窗口至该像素高度；结束后恢复尺寸')
    series = commands.add_parser('capture-series', help='分段捕获连续聊天上下文，保留重叠与原图')
    series.add_argument('--pages', type=int, default=3)
    series.add_argument('--height', type=int, default=1200)
    link = commands.add_parser('capture-link', help='核对图片后，关联其中实际可见的消息ID')
    link.add_argument('capture_id')
    link.add_argument('message_ids', nargs='+')
    scheduler = commands.add_parser('schedule')
    scheduler.add_argument('action', choices=['export', 'install', 'pause', 'resume', 'remove', 'run', 'status'])
    args = parser.parse_args(argv)
    try:
        if args.command == 'doctor':
            result = doctor()
        elif args.command == 'init':
            result = initialize(Path(args.config))
        else:
            config = load_config(args.config)
            if args.command == 'run':
                if args.backfill and config['reader'] != 'wechat-db':
                    raise ArchiveError('BACKFILL_REQUIRES_WECHAT_DB')
                config['backfill'] = args.backfill
                result = run(config, args.no_upload)
            elif args.command in ('build', 'validate', 'promote', 'sync'):
                from .core import Archive, exclusive
                from .retrieval import build, validate, promote
                from .publication import sync
                with exclusive(config['archive_root']):
                    archive = Archive(config)
                    try:
                        if args.command == 'build':
                            result = build(archive, args.llm, args.refresh_metadata, args.preview)
                        elif args.command == 'validate':
                            result = validate(archive,args.preview)
                        elif args.command == 'promote':
                            result = promote(archive)
                        else:
                            result = sync(archive, args.dry_run)
                    finally:
                        archive.close()
            elif args.command == 'wechat-auth':
                from .wechat_access import refresh_secret
                result = refresh_secret(config)
                if config['account_id'] == config.get('contact_username') == 'BIND_ON_SETUP':
                    from .wechat_access import bind_config
                    bind_config(config)
            elif args.command in ('capture', 'capture-link', 'capture-series'):
                from .core import Archive, exclusive
                from .screenshots import propose_capture, confirm_capture, capture_series
                with exclusive(config['archive_root']):
                    archive = Archive(config)
                    try:
                        if args.command == 'capture':
                            result = propose_capture(config, archive, bottom=args.bottom, height=args.height)
                        elif args.command == 'capture-series':
                            result = capture_series(config, archive, args.pages, args.height)
                        else:
                            result = confirm_capture(config, archive, args.capture_id, args.message_ids)
                    finally:
                        archive.close()
            elif args.command == 'auth':
                from .drive import credentials
                credentials(config, interactive=True)
                result = {'status': 'authorized'}
            elif args.command in ('coverage', 'retrieval','stats'):
                from .core import Archive, exclusive
                from .coverage import report
                with exclusive(config['archive_root']):
                    archive = Archive(config)
                    try:
                        if args.command == 'coverage':
                            result = report(archive)
                        elif args.command == 'stats':
                            from .statistics import statistics_page
                            from .retrieval import records
                            messages=records(archive)
                            path = statistics_page(archive.root/'v4/human',messages)
                            result = {'status':'generated','timezone':'Asia/Shanghai','path':str(path)}
                        else:
                            archive.materialize()
                            result = {'status': 'materialized', 'format': 'daily', 'index': str(archive.root / 'index.md')}
                        if args.command == 'coverage' and not args.details:
                            result['visual'].pop('message_evidence')
                    finally:
                        archive.close()
            elif args.command == 'update':
                from .workflow import update
                result = update(config)
            elif args.command == 'schedule':
                from .scheduler import schedule
                result = schedule(config, args.action)
            else:
                result = inspect(config, args)
        print(json.dumps(result, ensure_ascii=False, indent=2))
        if isinstance(result, dict) and result.get('status') in ('local_partial', 'synced_partial'):
            return 3
        return 0
    except Exception as exc:
        print(json.dumps({'status': 'failed', 'error': safe_error(exc)}, ensure_ascii=False), file=sys.stderr)
        return 2


def inspect(config, args):
    path = Path(config['archive_root']) / 'state.sqlite'
    if not path.exists():
        return {'status': 'not_initialized'}
    db = sqlite3.connect(path.as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    try:
        if args.command == 'status':
            return [dict(r) for r in db.execute('SELECT * FROM runs ORDER BY id DESC LIMIT 10')]
        if args.command == 'pending':
            return [dict(r) for r in db.execute('SELECT path,sha256,error FROM files WHERE uploaded IS NULL OR uploaded<>sha256')
                    if upload_exclusion(config, r['path']) is None]
        results = []
        for row in db.execute('SELECT id,payload FROM messages'):
            message = json.loads(row['payload'])
            if args.keyword.casefold() in message['text'].casefold():
                results.append({'id': f"M{row['id']:06d}", 'date': message['date'], 'text': message['text'], 'screenshots': message['screenshots']})
        return results
    finally:
        db.close()


def doctor():
    report = {'python': sys.version, 'executable': sys.executable, 'windows': platform.platform(),
              'wechat_reader': 'read-only database adapter; exact contact identity, text, timestamps, images and stickers locally verified',
              'verified_wechat_versions': ['4.1.15.56'], 'google_sdk_installed': importlib.util.find_spec('googleapiclient') is not None,
              'screenshot_automation': 'Windows Graphics Capture and overlapping 1200px window segments verified; complete message-range matching still needs visual confirmation',
              'sync_freshness': 'local database only; phone/server completeness not asserted'}
    script = "$p=Get-Process Weixin,WeChat -ErrorAction SilentlyContinue | Where-Object Path | Select-Object -First 1; if($p){$f=Get-Item -LiteralPath $p.Path; @{path=$p.Path;version=$f.VersionInfo.FileVersion} | ConvertTo-Json -Compress}"
    if sys.platform == 'win32':
        result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True)
        report['installed_wechat'] = json.loads(result.stdout) if result.stdout.strip() else {'status': 'not_running; installed version not detected'}
    return report


def initialize(path):
    if path.exists():
        raise ArchiveError('CONFIG_ALREADY_EXISTS')
    print('此向导配置人工导入；数据库读取的首次配置见 docs/setup.md。')
    name = input('好友备注名：').strip()
    contact = input('在好友资料中核对的唯一微信号：').strip()
    account = input('你的账号标识（非密码）：').strip()
    confirmation = input('确认是一对一好友，请再次输入该好友微信号：').strip()
    if not contact or contact != confirmation:
        raise ArchiveError('IDENTITY_NOT_CONFIRMED')
    archive = input('新的空归档目录 [private/archive]：').strip() or 'private/archive'
    inbox = input('导入批次目录 [private/inbox]：').strip() or 'private/inbox'
    client = input('OAuth 桌面客户端 JSON 路径（可稍后配置）：').strip()
    folder = input('Drive 文件夹 ID（空则授权后自动新建私有目录）：').strip()
    config = {'account_id': account, 'contact_id': contact, 'contact_name': name, 'identity_confirmed': True,
              'archive_root': archive, 'inbox': inbox, 'reader': 'import', 'timezone': 'Asia/Shanghai',
              'schedule_time': '04:00', 'client_secret_path': client, 'token_path': 'secrets/token.dpapi',
              'drive_folder_id': folder, 'live_acceptance_confirmed': False}
    if not name or not account:
        raise ArchiveError('CONFIG_REQUIRED_IDENTITY')
    atomic(path, (encode(config) + '\n').encode())
    (path.resolve().parent / inbox).mkdir(parents=True, exist_ok=True)
    return {'status': 'configured', 'next': 'doctor → auth → run --no-upload → 核对原文截图 → run；真实采集未验收前不启用计划任务'}
