"""QQNT protobuf elements; adapted from qq-nt-export (MIT). See NOTICE.md."""
import base64


def _read_varint(buf, i):
    result = 0
    shift = 0
    while True:
        b = buf[i]
        i += 1
        result |= (b & 0x7F) << shift
        if not (b & 0x80):
            return result, i
        shift += 7
        if shift > 70:
            raise ValueError("varint too long")


def decode_fields(buf):
    """返回 {field_no: [(wire_type, value), ...]}；wire 0/1/5 → int/bytes/bytes，2 → bytes。"""
    out = {}
    i, n = 0, len(buf)
    while i < n:
        key, i = _read_varint(buf, i)
        fn, wt = key >> 3, key & 7
        if wt == 0:
            v, i = _read_varint(buf, i)
            if v >= 1 << 63:
                v -= 1 << 64
        elif wt == 1:
            v = buf[i:i + 8]
            i += 8
        elif wt == 2:
            ln, i = _read_varint(buf, i)
            v = buf[i:i + ln]
            i += ln
        elif wt == 5:
            v = buf[i:i + 4]
            i += 4
        else:
            raise ValueError("unsupported wire type %d" % wt)
        out.setdefault(fn, []).append((wt, v))
    return out


def _i(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 0:
            return v
    return default


def _s(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 2:
            return v.decode("utf-8", errors="replace")
    return default


def _b(fields, no, default=None):
    for wt, v in fields.get(no, ()):
        if wt == 2:
            return v
    return default


def _bs(fields, no):
    return [v for wt, v in fields.get(no, ()) if wt == 2]


# ─────────────────────────── 40800 = MsgBody{repeated MsgContent} ───────────────────────────

_MD5 = "md5"
_HEX = "md5_hex"


def parse_content(raw):
    """解析单段 MsgContent。"""
    f = decode_fields(raw)
    c = {
        "msg_id": _i(f, 45001),
        "content_type": _i(f, 45002, 0),
        "media_sub": _i(f, 45003),
        "sender_uid": _s(f, 40020),
        "text": _s(f, 45101),
        "text_attr": _i(f, 45102),
        "f45105": _s(f, 45105),
        "filename": _s(f, 45402),
        "filepath": _s(f, 45403),
        "filesize": _i(f, 45405),
        "md5_hex": (_b(f, 45406) or b"").hex() or None,
        "thumbnail_len": len(_b(f, 45408) or b""),
        "img_width": _i(f, 45411),
        "img_height": _i(f, 45412),
        "f45415": _i(f, 45415),
        "file_ext": _s(f, 45419),
        "file_uuid": _s(f, 45503),
        "cdn_path": (_b(f, 45504) or b"").decode("utf-8", errors="replace") or None,
        "sticker_hex": (_b(f, 45600) or b"").hex() or None,
        "cdn_url": _s(f, 45802) or _s(f, 45803) or _s(f, 45804),
        "local_path": _s(f, 45812),
        "ntos_path": _s(f, 45814),
        "text_fallback": [v.decode("utf-8", errors="replace") for v in _bs(f, 45815)],
        "loc_type": _i(f, 45851),
        "reply_msg_id": _i(f, 47401),
        "reply_msg_seq": _i(f, 47402),
        "reply_sender_number": _i(f, 47403),
        "reply_msg_time": _i(f, 47404),
        "reply_summary": _s(f, 47413),
        "nc_uid_1": _s(f, 47703),
        "nc_nickname_1": _s(f, 47705),
        "nc_remark_1": _s(f, 47706),
        "nc_nickname_2": _s(f, 47707),
        "nc_remark_2": _s(f, 47716),
        "nc_uid_2": _s(f, 47704),
        "nc_extra_text": _s(f, 47713),
        "video_flag": _i(f, 47601),
        "video_text": _s(f, 47602),
        "fwd_meta": _s(f, 47901),
        "fwd_token": _s(f, 47902),
        "fwd_uuid": _s(f, 47904),
        "call_type": _i(f, 48151, 0),
        "call_duration": _i(f, 48152, 0),
        "call_desc": _s(f, 48153),
        "legacy_fwd_resid": _s(f, 48601),
        "legacy_fwd_xml": _s(f, 48602),
        "legacy_fwd_uuid": _s(f, 48603),
        "proto_ver": _s(f, 49154),
        "inner_ts": _i(f, 49155),
        "sys_f80810": _i(f, 80810),
        "sys_content": _s(f, 80900),
        "rich_text": _s(f, 48705),
        "rich_markdown": _s(f, 48701),
    }
    if c["content_type"] == 26 and _i(f, 48172) == 11:
        try:
            card = decode_fields(base64.b64decode(_b(f, 48183) or b"", validate=True))
            header = decode_fields(_b(card, 2) or b"")
            if _b(header, 4) == "推荐联系人".encode():
                labels = [(_b(decode_fields(item), 1) or b"").decode() for item in _bs(card, 3)]
                c["contact_recommendation"] = ["推荐联系人", *labels]
        except (IndexError, ValueError):
            pass
    ref = _b(f, 47710)
    c["ref_msg"] = parse_content(ref) if ref else None
    return c


def parse_40800(blob):
    """解析 40800 消息体，返回 (contents, error)。"""
    try:
        fields = decode_fields(blob)
    except Exception as exc:  # 损坏/非 protobuf
        return [], str(exc)
    segs = [parse_content(v) for wt, v in fields.get(40800, ()) if wt == 2]
    return segs, None


# ─────────────────────────── 段 -> 类型化 content ───────────────────────────
