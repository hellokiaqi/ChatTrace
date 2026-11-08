"""Version-gated, read-only WeChat authorization; secrets stay in DPAPI storage."""
import hashlib
import json
import os
import re
import subprocess
from pathlib import Path

from .core import ArchiveError, atomic, encode
from .drive import protect
from .vendor import wcdb_readonly as wc

SUPPORTED = '4.1.15.56'


def process_info():
    script = "@(Get-Process Weixin -ErrorAction SilentlyContinue | Where-Object Path | ForEach-Object { @{pid=$_.Id; path=$_.Path; version=(Get-Item -LiteralPath $_.Path).VersionInfo.FileVersion} }) | ConvertTo-Json -Compress"
    result = subprocess.run(['powershell.exe', '-NoProfile', '-Command', script], capture_output=True, text=True)
    if result.returncode or not result.stdout.strip():
        raise ArchiveError('WECHAT_NOT_RUNNING')
    rows = json.loads(result.stdout)
    rows = rows if isinstance(rows, list) else [rows]
    if not rows:
        raise ArchiveError('WECHAT_NOT_RUNNING')
    if any(row['version'] != SUPPORTED for row in rows):
        raise ArchiveError('WECHAT_VERSION_NOT_VERIFIED')
    return rows


def account_directories():
    config_dir = Path(os.environ['APPDATA']) / 'Tencent/xwechat/config'
    roots = set()
    for path in config_dir.glob('*.ini'):
        text = path.read_text('utf-8-sig').strip('\x00\r\n ')
        if re.match(r'^[A-Za-z]:[\\/]', text):
            roots.add(Path(text) / 'xwechat_files')
    result = []
    for root in roots:
        if root.is_dir():
            result.extend(p for p in root.iterdir() if (p / 'db_storage/contact/contact.db').is_file())
    return result


def live_identity(expected=None):
    identities = []
    for process in process_info():
        info = wc.extract_master_key_from_cfg(process['pid'])
        if info and info[2] and (not expected or info[2] == expected):
            identities.append((process, info))
    if len(identities) != 1:
        raise ArchiveError('WECHAT_LOGIN_IDENTITY_NOT_UNIQUE_OR_UNAVAILABLE')
    return identities[0]


def refresh_secret(config):
    expected = config.get('account_id') if config.get('account_identity_status') == 'database_verified' else None
    process, (master, cfg, wxid) = live_identity(expected)
    accounts = [p for p in account_directories() if p.name.startswith(wxid + '_')]
    if len(accounts) != 1:
        raise ArchiveError('WECHAT_ACCOUNT_DIRECTORY_NOT_UNIQUE')
    root = accounts[0]
    with (root / 'db_storage/contact/contact.db').open('rb') as stream:
        page = stream.read(4096)
    derived = hashlib.pbkdf2_hmac('sha512', bytes.fromhex(master), page[:16], 256000, 32)
    candidates = []
    if not wc._verify_enc_key(derived, page):
        master = None
        candidates = [key for key, salt in wc.KeyScanner()._collect_key_candidates(process['pid'], set()) if salt is None]
        if not any(wc._verify_enc_key(key, page) for key in candidates):
            raise ArchiveError('WECHAT_DATABASE_KEY_UNAVAILABLE')
    necessary = [root / 'db_storage/contact/contact.db', root / 'db_storage/emoticon/emoticon.db']
    necessary.extend(p for p in (root / 'db_storage/message').glob('message_*.db') if re.fullmatch(r'message_\d+\.db', p.name))
    validated = set()
    for path in necessary:
        if not path.is_file():
            continue
        with path.open('rb') as stream:
            page = stream.read(4096)
        for key in candidates:
            if wc._verify_enc_key(key, page):
                validated.add(key.hex())
                break
    secret = {'account_dir': str(root), 'master': master, 'cfg_dword': cfg, 'wxid': wxid,
              'keys': sorted(validated), 'installed_version': process['version']}
    atomic(config['wechat_secret_path'], protect(encode(secret).encode()))
    return {'status': 'authorized', 'version': process['version'], 'key_storage': 'Windows DPAPI'}


def check_live(config):
    live_identity(config['account_id'])

def bind_config(config):
    """Bind a confirmed alias to the single contact row on initial desktop setup."""
    from .wechat_snapshot import Snapshot
    snapshot = Snapshot(config['wechat_secret_path'])
    connection = snapshot.open('contact/contact.db')
    try:
        rows = connection.execute('SELECT username FROM contact WHERE username=? OR alias=?',
                                  (config['contact_id'],config['contact_id'])).fetchall()
    finally:
        connection.close()
    if len(rows)!=1 or rows[0]['username'].endswith('@chatroom'):
        raise ArchiveError('TARGET_CONTACT_NOT_UNIQUE_OR_NOT_LOCAL')
    path = Path(config['config_path'])
    data = json.loads(path.read_text('utf-8-sig'))
    data.update(account_id=snapshot.account,account_identity_status='database_verified',
                contact_username=rows[0]['username'])
    atomic(path,(json.dumps(data,ensure_ascii=False,indent=2)+'\n').encode())
