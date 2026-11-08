"""Original window capture and local OCR anchors; no generated chat graphics."""
import asyncio
import ctypes
import io
import json
import re
import threading
import time
from ctypes import wintypes
from pathlib import Path

from PIL import Image

from .core import ArchiveError, atomic, digest, encode, now


def target_window(config):
    user = ctypes.WinDLL('user32', use_last_error=True)
    user.OpenInputDesktop.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    user.OpenInputDesktop.restype = wintypes.HANDLE
    user.CloseDesktop.argtypes = [wintypes.HANDLE]
    user.GetUserObjectInformationW.argtypes = [wintypes.HANDLE, ctypes.c_int, ctypes.c_void_p, wintypes.DWORD, ctypes.POINTER(wintypes.DWORD)]
    desktop = user.OpenInputDesktop(0, False, 1)
    if not desktop:
        raise ArchiveError('DESKTOP_LOCKED_OR_UNAVAILABLE')
    try:
        name = ctypes.create_unicode_buffer(256)
        needed = wintypes.DWORD()
        if not user.GetUserObjectInformationW(desktop, 2, name, ctypes.sizeof(name), ctypes.byref(needed)) or name.value.lower() != 'default':
            raise ArchiveError('DESKTOP_LOCKED_OR_UNAVAILABLE')
    finally:
        user.CloseDesktop(desktop)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumWindows.argtypes = [callback_type, wintypes.LPARAM]
    user.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    user.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    user.IsIconic.argtypes = [wintypes.HWND]
    user.IsWindowVisible.argtypes = [wintypes.HWND]
    windows = []

    def visit(handle, _):
        size = user.GetWindowTextLengthW(handle)
        text = ctypes.create_unicode_buffer(size + 1)
        user.GetWindowTextW(handle, text, size + 1)
        if text.value == config['contact_name'] and user.IsWindowVisible(handle):
            windows.append(handle)
        return True

    user.EnumWindows(callback_type(visit), 0)
    if len(windows) != 1:
        raise ArchiveError('OPEN_TARGET_INDEPENDENT_CHAT_WINDOW')
    handle = windows[0]
    if user.IsIconic(handle):
        raise ArchiveError('TARGET_CHAT_WINDOW_MINIMIZED')
    kernel = ctypes.WinDLL('kernel32', use_last_error=True)
    user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    pid = wintypes.DWORD()
    user.GetWindowThreadProcessId(handle, ctypes.byref(pid))
    kernel.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel.OpenProcess.restype = wintypes.HANDLE
    kernel.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD, wintypes.LPWSTR, ctypes.POINTER(wintypes.DWORD)]
    kernel.CloseHandle.argtypes = [wintypes.HANDLE]
    process = kernel.OpenProcess(0x1000, False, pid.value)
    try:
        length = wintypes.DWORD(32768)
        exe = ctypes.create_unicode_buffer(length.value)
        if not process or not kernel.QueryFullProcessImageNameW(process, 0, exe, ctypes.byref(length)) or Path(exe.value).name.lower() != 'weixin.exe':
            raise ArchiveError('TARGET_WINDOW_PROCESS_MISMATCH')
    finally:
        if process:
            kernel.CloseHandle(process)
    return user, handle


def capture_window(config, height=None):
    user, handle = target_window(config)
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    user.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int, wintypes.UINT]
    rect = wintypes.RECT()
    user.GetWindowRect(handle, ctypes.byref(rect))
    if height is not None:
        if not 600 <= height <= 2400:
            raise ArchiveError('SCREENSHOT_HEIGHT_OUT_OF_RANGE')
        if not user.SetWindowPos(handle, None, rect.left, rect.top, rect.right - rect.left, height, 0x14):
            raise ArchiveError('WINDOW_RESIZE_FAILED')
        time.sleep(0.5)
    from windows_capture import WindowsCapture
    import cv2
    ready = threading.Event()
    frames = []
    capture = WindowsCapture(cursor_capture=False, window_hwnd=handle)

    @capture.event
    def on_frame_arrived(frame, control):
        if height is not None and frame.height < height - 20:
            return
        ok, encoded = cv2.imencode('.png', frame.frame_buffer)
        if ok:
            frames.append(encoded.tobytes())
        control.stop()
        ready.set()

    @capture.event
    def on_closed():
        ready.set()

    try:
        control = capture.start_free_threaded()
        ready.wait(10)
        control.stop()
    finally:
        if height is not None:
            user.SetWindowPos(handle, None, rect.left, rect.top, rect.right - rect.left, rect.bottom - rect.top, 0x14)
    if not frames:
        raise ArchiveError('WINDOW_CAPTURE_UNAVAILABLE_OR_LOCKED')
    picture = Image.open(io.BytesIO(frames[0]))
    low, high = picture.convert('L').getextrema()
    if picture.width < 300 or picture.height < 300 or high - low < 10:
        raise ArchiveError('WINDOW_CAPTURE_EMPTY_OR_TOO_SMALL')
    stream = io.BytesIO()
    picture.save(stream, format='PNG')
    return stream.getvalue()


def scroll_window(config, delta):
    user, handle = target_window(config)
    callback_type = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user.EnumChildWindows.argtypes = [wintypes.HWND, callback_type, wintypes.LPARAM]
    user.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    children = []

    def visit(child, _):
        name = ctypes.create_unicode_buffer(256)
        user.GetClassNameW(child, name, 256)
        if name.value == 'MMUIRenderSubWindowHW':
            children.append(child)
        return True

    user.EnumChildWindows(handle, callback_type(visit), 0)
    if len(children) > 1:
        raise ArchiveError('CHAT_SCROLL_TARGET_AMBIGUOUS')
    target = children[0] if children else handle
    user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    rect = wintypes.RECT()
    user.GetWindowRect(handle, ctypes.byref(rect))
    x, y = (rect.left + rect.right) // 2, rect.top + 200
    user.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
    step = 120 if delta > 0 else -120
    for _ in range(max(1, abs(delta) // 120)):
        if not user.PostMessageW(target, 0x020A, (step & 0xffff) << 16, ((y & 0xffff) << 16) | (x & 0xffff)):
            raise ArchiveError('CHAT_SCROLL_FAILED')
        time.sleep(0.05)
    time.sleep(0.3)


def capture_series(config, archive, pages=3, height=1200):
    if not 1 <= pages <= 12:
        raise ArchiveError('CAPTURE_PAGE_COUNT_OUT_OF_RANGE')
    captures = []
    moved = 0
    try:
        for index in range(pages):
            capture = propose_capture(config, archive, height=height)
            if captures and capture['capture_id'] == captures[-1]['capture_id']:
                break
            captures.append(capture)
            if index + 1 < pages:
                scroll_window(config, 480)
                moved += 1
    finally:
        for _ in range(moved):
            scroll_window(config, -480)
    captures.reverse()
    sequence = {'schema': 1, 'captured_at': now(), 'direction': 'older_to_newer',
                'status': 'awaiting_range_confirmation', 'height': height,
                'captures': [{'order': i + 1, **c} for i, c in enumerate(captures)],
                'coverage_complete': False}
    destination = Path(config['inbox']) / 'captures' / ('series-' + digest(encode([c['capture_id'] for c in captures]).encode()) + '.json')
    atomic(destination, encode(sequence).encode())
    return {'status': sequence['status'], 'index': str(destination), 'captures': captures}


async def recognize(data):
    from winrt.windows.media.ocr import OcrEngine
    from winrt.windows.globalization import Language
    from winrt.windows.storage.streams import DataWriter, InMemoryRandomAccessStream
    from winrt.windows.graphics.imaging import BitmapDecoder, BitmapPixelFormat, BitmapAlphaMode
    original = Image.open(io.BytesIO(data))
    factor = 2 if original.width <= 1200 else 1
    if factor > 1:
        enlarged = io.BytesIO()
        original.resize((original.width * factor, original.height * factor)).save(enlarged, format='PNG')
        data = enlarged.getvalue()
    stream = InMemoryRandomAccessStream()
    writer = DataWriter(stream)
    try:
        writer.write_bytes(data)
        await writer.store_async()
        writer.detach_stream()
        stream.seek(0)
        decoder = await BitmapDecoder.create_async(stream)
        bitmap = await decoder.get_software_bitmap_async(BitmapPixelFormat.BGRA8, BitmapAlphaMode.IGNORE)
        try:
            engine = OcrEngine.try_create_from_language(Language('zh-Hans-CN'))
            if engine is None:
                raise ArchiveError('CHINESE_OCR_NOT_INSTALLED')
            result = await engine.recognize_async(bitmap)
            lines = []
            for line in result.lines:
                words = list(line.words)
                if not words:
                    continue
                lines.append({'text': line.text, 'x': min(w.bounding_rect.x for w in words) / factor,
                              'y': min(w.bounding_rect.y for w in words) / factor,
                              'right': max(w.bounding_rect.x + w.bounding_rect.width for w in words) / factor,
                              'bottom': max(w.bounding_rect.y + w.bounding_rect.height for w in words) / factor})
            return lines
        finally:
            bitmap.close()
    finally:
        writer.close()
        stream.close()


def propose_capture(config, archive, source_path=None, bottom=None, height=None):
    raw = Path(source_path).read_bytes() if source_path else capture_window(config, height)
    picture = Image.open(io.BytesIO(raw))
    if bottom is None:
        bottom = picture.height - config.get('screenshot_input_height', 140)
    if not 150 < bottom <= picture.height:
        raise ArchiveError('SCREENSHOT_CROP_NEEDS_CALIBRATION')
    cropped = picture.crop((0, 0, picture.width, bottom))
    output = io.BytesIO()
    cropped.save(output, format='PNG')
    data = output.getvalue()
    folder = Path(config['inbox']) / 'captures'
    capture_id = 'S' + digest(data)
    original = folder / (capture_id + '-original' + ('.jpg' if raw.startswith(b'\xff\xd8\xff') else '.png'))
    path = folder / (capture_id + '.png')
    atomic(original, raw)
    atomic(path, data)
    lines = asyncio.run(recognize(data))
    compact = lambda text: re.sub(r'\s+', '', text)
    observed = compact(''.join(line['text'] for line in lines))
    anchors = []
    for row in archive.db.execute('SELECT id,payload FROM messages'):
        message = json.loads(row['payload'])
        text = compact(message['text'])
        if message['type'] == 'text' and len(text) >= 5 and text in observed:
            anchors.append({'id': f"M{row['id']:06d}", 'source_id': message['source_id'], 'sequence': message['sequence']})
    anchors.sort(key=lambda x: x['sequence'])
    record = {'schema': 1, 'id': capture_id, 'captured_at': now(), 'source': 'existing_verified_capture' if source_path else 'Windows_Graphics_Capture',
              'original_path': str(original.relative_to(Path(config['inbox']))), 'path': str(path.relative_to(Path(config['inbox']))),
              'crop': [0, 0, picture.width, bottom], 'original_size': list(picture.size),
              'text_anchors': anchors, 'message_ids': [], 'coverage': 'awaiting_visual_confirmation'}
    atomic(folder / (capture_id + '.json'), encode(record).encode())
    return {'status': 'review_required', 'image': str(path), 'capture_id': capture_id,
            'anchors': [a['id'] for a in anchors], 'automatic_full_range': False}


def confirm_capture(config, archive, capture_id, message_ids, partial_ids=None):
    if not re.fullmatch(r'S[0-9a-f]{64}', capture_id) or not message_ids:
        raise ArchiveError('INVALID_SCREENSHOT_CONFIRMATION')
    path = Path(config['inbox']) / 'captures' / (capture_id + '.json')
    record = json.loads(path.read_text('utf-8'))
    messages = []
    for mid in message_ids:
        if not re.fullmatch(r'M\d{6,}', mid):
            raise ArchiveError('INVALID_MESSAGE_ID')
        row = archive.db.execute('SELECT payload FROM messages WHERE id=?', (int(mid[1:]),)).fetchone()
        if not row:
            raise ArchiveError('SCREENSHOT_MESSAGE_NOT_FOUND')
        message = json.loads(row[0])
        message['screenshots'] = [capture_id]
        message['media'] = [{'kind': a['kind'], 'path': str(Path(config['archive_root']) / a['path'])} for a in message['media']]
        # Ingest requires assets inside the batch directory; copy the existing immutable assets.
        for asset in message['media']:
            original = Path(asset['path'])
            destination = Path(config['inbox']) / 'assets' / original.name
            if not destination.exists():
                atomic(destination, original.read_bytes())
            asset['path'] = 'assets/' + original.name
        messages.append(message)
    sources = {m['source'] for m in messages}
    if len(sources) != 1:
        raise ArchiveError('SCREENSHOT_MIXED_SOURCES')
    metadata = {k: record[k] for k in ('captured_at', 'source', 'crop', 'original_size')}
    metadata['range_verification'] = 'visual_confirmation'
    metadata['partially_visible_message_ids'] = partial_ids or []
    metadata['original_sha256'] = digest((Path(config['inbox']) / record['original_path']).read_bytes())
    batch = {'schema': 1, 'account_id': config['account_id'], 'contact_id': config['contact_id'], 'source': sources.pop(),
             'captured_at': now(), 'messages': messages,
             'screenshots': [{'id': capture_id, 'path': record['path'], 'metadata': metadata}]}
    batch_path = Path(config['inbox']) / ('capture-link-' + capture_id + '.json')
    atomic(batch_path, encode(batch).encode())
    changed = archive.ingest(batch_path)
    archive.materialize()
    record.update(message_ids=message_ids, coverage='visually_confirmed')
    atomic(path, encode(record).encode())
    return {'status': 'linked', 'messages': changed, 'capture_id': capture_id}
