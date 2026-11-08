"""Google-supported OAuth/client libraries; loaded only for network operations."""
import ctypes
import hashlib
import io
import json
import mimetypes
import os
from ctypes import wintypes
from pathlib import Path, PurePosixPath
from .core import ArchiveError, atomic

SCOPES = ['https://www.googleapis.com/auth/drive.file']


def protect(data, decrypt=False):
    if os.name != 'nt':
        raise ArchiveError('WINDOWS_DPAPI_REQUIRED')

    class Blob(ctypes.Structure):
        _fields_ = [('size', wintypes.DWORD), ('data', ctypes.POINTER(ctypes.c_byte))]

    buffer = ctypes.create_string_buffer(data)
    source = Blob(len(data), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_byte)))
    output = Blob()
    crypt = ctypes.WinDLL('crypt32', use_last_error=True)
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    kernel.LocalFree.argtypes = [ctypes.c_void_p]
    kernel.LocalFree.restype = ctypes.c_void_p
    if decrypt:
        ok = crypt.CryptUnprotectData(ctypes.byref(source), None, None, None, None, 1, ctypes.byref(output))
    else:
        ok = crypt.CryptProtectData(ctypes.byref(source), 'ChatTrace OAuth', None, None, None, 1, ctypes.byref(output))
    if not ok:
        raise ArchiveError('TOKEN_DPAPI_ERROR')
    try:
        return ctypes.string_at(output.data, output.size)
    finally:
        kernel.LocalFree(output.data)


def credentials(config, interactive=False):
    try:
        from google.oauth2.credentials import Credentials
        from google.auth.transport.requests import Request
        from google.auth.exceptions import RefreshError, TransportError
        from google_auth_oauthlib.flow import InstalledAppFlow
    except ImportError:
        raise ArchiveError('GOOGLE_DEPENDENCIES_MISSING') from None
    path = Path(config.get('token_path', 'secrets/token.dpapi'))
    if interactive:
        client = config.get('client_secret_path')
        if not client or not Path(client).is_file():
            raise ArchiveError('OAUTH_DESKTOP_CLIENT_JSON_REQUIRED')
        flow = InstalledAppFlow.from_client_secrets_file(client, SCOPES, autogenerate_code_verifier=True)
        creds = flow.run_local_server(host='127.0.0.1', port=0, access_type='offline', prompt='consent',
                                      authorization_prompt_message='请在浏览器完成 Google 授权。',
                                      success_message='授权完成，可以关闭此窗口。', timeout_seconds=300)
    else:
        if not path.exists():
            raise ArchiveError('DRIVE_AUTH_REQUIRED')
        creds = Credentials.from_authorized_user_info(json.loads(protect(path.read_bytes(), True)), SCOPES)
        if not creds.valid:
            if not creds.refresh_token:
                raise ArchiveError('DRIVE_AUTH_REQUIRED')
            try:
                creds.refresh(Request())
            except RefreshError as exc:
                if any(isinstance(detail, dict) and detail.get('error') == 'invalid_grant' for detail in exc.args):
                    raise ArchiveError('DRIVE_AUTH_REQUIRED') from None
                raise ArchiveError('DRIVE_REFRESH_FAILED_REAUTHORIZE_OR_CHECK_NETWORK') from None
            except TransportError:
                raise ArchiveError('DRIVE_AUTH_NETWORK_UNAVAILABLE') from None
            except Exception:
                raise ArchiveError('DRIVE_REFRESH_FAILED_REAUTHORIZE_OR_CHECK_NETWORK') from None
    atomic(path, protect(creds.to_json().encode()))
    return creds


class GoogleDrive:
    def __init__(self, config, archive):
        try:
            from googleapiclient.discovery import build
            from googleapiclient.errors import HttpError
            from googleapiclient.http import MediaIoBaseUpload
        except ImportError:
            raise ArchiveError('GOOGLE_DEPENDENCIES_MISSING') from None
        self.api = build('drive', 'v3', http=RequestsTransport(credentials(config)), cache_discovery=False)
        self.http_error = HttpError
        self.media = MediaIoBaseUpload
        self.archive = archive
        self.root = config.get('drive_folder_id')
        if self.root:
            meta = self.api.files().get(fileId=self.root, fields='id,mimeType,trashed,shared,capabilities(canAddChildren)').execute(num_retries=3)
            if meta.get('trashed') or meta['mimeType'] != 'application/vnd.google-apps.folder' or meta.get('shared') or not meta.get('capabilities', {}).get('canAddChildren'):
                raise ArchiveError('DRIVE_FOLDER_MUST_BE_PRIVATE_WRITABLE')
        else:
            self.root = self.folder('@root', 'WeChatArchive', 'root')
            meta = self.api.files().get(fileId=self.root, fields='shared').execute(num_retries=3)
            if meta.get('shared'):
                raise ArchiveError('DRIVE_FOLDER_MUST_BE_PRIVATE_WRITABLE')
        contact_key = hashlib.sha256((config['account_id'] + '\0' + config['contact_id']).encode()).hexdigest()[:12]
        self.contact_root = self.folder('@contact', config.get('drive_contact_name', 'contact-' + contact_key), self.root)

    def reserve_id(self):
        return self.api.files().generateIds(count=1, space='drive', type='files').execute(num_retries=3)['ids'][0]

    def exists(self, remote_id):
        try:
            return self.api.files().get(fileId=remote_id, fields='id,name,trashed,parents,mimeType').execute(num_retries=3)
        except self.http_error as exc:
            if exc.resp.status == 404:
                return None
            raise

    def folder(self, key, name, parent):
        row = self.archive.db.execute('SELECT remote_id FROM folders WHERE path=?', (key,)).fetchone()
        remote_id = row[0] if row else self.reserve_id()
        if not row:
            with self.archive.db:
                self.archive.db.execute('INSERT INTO folders VALUES (?,?)', (key, remote_id))
        existing = self.exists(remote_id)
        if existing and existing.get('trashed'):
            raise ArchiveError('REMOTE_FOLDER_TRASHED')
        if existing and (existing.get('id') != remote_id or existing.get('mimeType') != 'application/vnd.google-apps.folder'
                         or existing.get('name') != name or (parent != 'root' and parent not in existing.get('parents', []))):
            raise ArchiveError('REMOTE_FOLDER_IDENTITY_MISMATCH')
        if not existing:
            self.api.files().create(body={'id': remote_id, 'name': name, 'mimeType': 'application/vnd.google-apps.folder',
                                         'parents': [parent]}, fields='id').execute(num_retries=3)
        return remote_id

    def put(self, remote_id, relative, data):
        path = PurePosixPath(relative)
        mime = 'text/plain' if path.suffix == '.jsonl' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        parent, key = self.contact_root, ''
        for part in path.parts[:-1]:
            key += '/' + part
            parent = self.folder(key, part, parent)
        existing = self.exists(remote_id)
        if existing and (existing.get('trashed') or parent not in existing.get('parents', [])):
            raise ArchiveError('REMOTE_FILE_MOVED_OR_TRASHED')
        if existing and (existing.get('id') != remote_id or existing.get('name') != path.name or existing.get('mimeType') != mime):
            raise ArchiveError('REMOTE_FILE_IDENTITY_MISMATCH')
        if existing and self.verify(remote_id, data, relative):
            return
        media = self.media(io.BytesIO(data), mimetype=mime,
                           chunksize=1024 * 1024, resumable=True)
        if existing:
            request = self.api.files().update(fileId=remote_id, body={'mimeType': mime}, media_body=media, fields='id')
        else:
            request = self.api.files().create(body={'id': remote_id, 'name': path.name, 'parents': [parent], 'mimeType': mime}, media_body=media, fields='id')
        response = None
        while response is None:
            _, response = request.next_chunk(num_retries=3)

    def verify(self, remote_id, data, relative=None):
        if relative is None:
            rows = list(self.archive.db.execute('SELECT path FROM files WHERE remote_id=?', (remote_id,)))
            if len(rows) != 1:
                raise ArchiveError('REMOTE_FILE_MAPPING_NOT_UNIQUE')
            relative = rows[0]['path']
        path = PurePosixPath(relative)
        parent = self.contact_root
        if len(path.parts) > 1:
            folder = self.archive.db.execute('SELECT remote_id FROM folders WHERE path=?', ('/' + str(path.parent),)).fetchone()
            if not folder:
                raise ArchiveError('REMOTE_PARENT_MAPPING_MISSING')
            parent = folder[0]
        mime = 'text/plain' if path.suffix == '.jsonl' else mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        meta = self.api.files().get(fileId=remote_id, fields='id,name,parents,mimeType,trashed,md5Checksum,size').execute(num_retries=3)
        if (meta.get('id') != remote_id or meta.get('name') != path.name or meta.get('mimeType') != mime
                or meta.get('trashed') or parent not in meta.get('parents', [])):
            raise ArchiveError('REMOTE_FILE_IDENTITY_MISMATCH')
        return meta.get('md5Checksum') == hashlib.md5(data).hexdigest() and meta.get('size') == str(len(data))


class RequestsTransport:
    """Bridge the discovery client to the same proxy-aware transport used by OAuth."""
    def __init__(self, creds):
        from google.auth.transport.requests import AuthorizedSession
        self.session = AuthorizedSession(creds)
        self.timeout = 30

    def request(self, uri, method='GET', body=None, headers=None, **kwargs):
        import httplib2
        import requests
        import socket
        try:
            response = self.session.request(method, uri, data=body, headers=headers,
                                            timeout=self.timeout, allow_redirects=False)
        except requests.exceptions.ConnectionError:
            raise ConnectionResetError(104, 'DRIVE_CONNECTION_RESET') from None
        except requests.exceptions.Timeout:
            raise socket.timeout('DRIVE_REQUEST_TIMEOUT') from None
        return httplib2.Response({**response.headers, 'status': str(response.status_code)}), response.content

    def close(self):
        self.session.close()
