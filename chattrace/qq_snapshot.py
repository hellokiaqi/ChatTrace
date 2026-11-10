"""Authenticated QQNT snapshots of the message and contact databases, in memory."""
import hashlib
import hmac
import json
import sqlite3
import struct
from pathlib import Path
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from .core import ArchiveError
from .drive import protect
from .wechat_snapshot import assemble_snapshot, signature


def open_db(config, name):
    root = Path(config['qq_account_dir'])
    if root.name != config['account_id'] or name not in ('nt_msg.db','profile_info.db'):
        raise ArchiveError('QQ_ACCOUNT_DIRECTORY_MISMATCH')
    path = root/'nt_qq/nt_db'/name
    wal = path.with_name(name+'-wal')
    secret=Path(config['qq_secret_path'])
    if not secret.is_file():
        raise ArchiveError('QQ_AUTH_REQUIRED')
    keys = json.loads(protect(secret.read_bytes(),True))
    key = bytes.fromhex(keys[name])
    for attempt in range(3):
        before = signature(path,wal)
        with path.open('rb') as source:
            source.seek(1024)
            raw=source.read()
        log=wal.read_bytes() if wal.exists() else b''
        if before==signature(path,wal):
            break
    else:
        raise ArchiveError('QQ_DATABASE_BUSY')
    if not raw or len(raw)%4096:
        raise ArchiveError('QQ_PAGE_SIZE_INVALID')
    mac = hashlib.pbkdf2_hmac('sha512',key,bytes(b^58 for b in raw[:16]),2,32)
    def decrypt(page,number):
        if not any(page):
            return page
        start = 16 if number==1 else 0
        tag = hmac.digest(mac,page[start:4064]+struct.pack('<I',number),'sha1')
        if not hmac.compare_digest(tag,page[4064:4084]):
            raise ArchiveError('QQ_PAGE_AUTHENTICATION_FAILED')
        cipher = Cipher(algorithms.AES(key),modes.CBC(page[4048:4064])).decryptor()
        prefix=b'SQLite format 3\0' if start else b''
        return prefix+cipher.update(page[start:4048])+cipher.finalize()+bytes(48)
    data=assemble_snapshot(raw,log,decrypt)
    db=sqlite3.connect(':memory:')
    try:
        db.deserialize(data)
        db.execute('pragma query_only=on')
        if db.execute('pragma quick_check').fetchone()[0]!='ok':
            raise ArchiveError('QQ_INVALID_SNAPSHOT')
        db.row_factory=sqlite3.Row
        return db
    except Exception:
        db.close()
        raise

def pids():
    import subprocess
    script = "Get-Process QQ -ErrorAction SilentlyContinue | Where-Object Path | ForEach-Object { if ((Get-Item -LiteralPath $_.Path).VersionInfo.FileVersion -eq '9.9.36.53644') { $_.Id } }"
    output = subprocess.run(['powershell.exe','-NoProfile','-Command',script],capture_output=True,text=True)
    found = [int(v) for v in output.stdout.split() if v.isdigit()]
    if not found:
        raise ArchiveError('QQ_NOT_RUNNING_OR_UNVERIFIED_VERSION')
    return found


def authorize(config):
    import ctypes
    import re
    from .vendor.qqnt_memory import k32, MEMORY_BASIC_INFORMATION64
    from .core import atomic
    root = Path(config['qq_account_dir'])
    if root.name!=config['account_id']:
        raise ArchiveError('QQ_ACCOUNT_DIRECTORY_MISMATCH')
    salts = {}
    for name in ('nt_msg.db','profile_info.db'):
        with (root/'nt_qq/nt_db'/name).open('rb') as file:
            file.seek(1024)
            page = file.read(4096)
        salts[page[:16].hex()] = (name,page)
    pattern = re.compile(rb"x'([0-9a-f]{64})("+b'|'.join(s.encode() for s in salts)+rb")'",re.I)
    keys = {}
    for pid in pids():
        handle = k32.OpenProcess(0x410,False,pid)
        if not handle:
            continue
        try:
            region=MEMORY_BASIC_INFORMATION64()
            address=0
            while len(keys)<len(salts) and k32.VirtualQueryEx(handle,address,ctypes.byref(region),ctypes.sizeof(region)):
                base,size=region.BaseAddress,region.RegionSize
                if region.State==0x1000 and region.Protect&0xFF in (2,4,8,32,64,128) and not region.Protect&0x100:
                    tail=b''
                    for offset in range(0,size,4<<20):
                        buf=ctypes.create_string_buffer(min(4<<20,size-offset))
                        got=ctypes.c_size_t()
                        if not k32.ReadProcessMemory(handle,base+offset,buf,len(buf),ctypes.byref(got)):
                            tail=b''
                            continue
                        data=tail+buf.raw[:got.value]
                        for hit in pattern.finditer(data):
                            name,page=salts[hit[2].decode().lower()]
                            key=bytes.fromhex(hit[1].decode())
                            mac=hashlib.pbkdf2_hmac('sha512',key,bytes(b^58 for b in page[:16]),2,32)
                            if hmac.compare_digest(hmac.digest(mac,page[16:4064]+struct.pack('<I',1),'sha1'),page[4064:4084]):
                                keys[name]=key.hex()
                        tail=data[-100:]
                        if len(keys)==len(salts):
                            break
                if base+size<=address:
                    break
                address=base+size
        finally:
            k32.CloseHandle(handle)
        if len(keys)==len(salts):
            break
    if len(keys)!=len(salts):
        raise ArchiveError('QQ_KEYS_NOT_FOUND')
    atomic(config['qq_secret_path'],protect(json.dumps(keys).encode()))
    return {'status':'authorized','version':'9.9.36.53644','authenticated_databases':len(keys)}
