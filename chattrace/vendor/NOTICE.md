# Read-only upstream components

`wcdb_readonly.py` is generated from fanyuantaier/wechatauto-replica commit
6e603974bbac3bda93d88bbd6995510ae2547f5c, `wechatauto/db.py`, under Apache-2.0.
Run `tools/vendor_readonly.py` against that reviewed checkout to regenerate.
The subset contains read-only process access, SQLCipher cryptography and key scanning;
it excludes package initialization, automation, sending, key-file persistence and database export.
Changes: remove package logger import, bound the module scan, declare CloseHandle argument type,
and retain only three methods in KeyScanner. No upstream entry point is executed.

Message field and media-path mappings follow tqjason/WeChatMsg commit
14b466dcc4c60b43d9d79301e489c8d6029daed4 (`wxManager/db_v4`, MIT).
The two packed_info_data protobuf modules are copied unchanged from that commit.
Image V2 layout and cfg-derived image keys also follow wechatauto-replica `media.py`.
Both licenses are retained here. No AI, reporting or message-sending components are included.

QQNT protobuf field parsing and Windows read-only memory declarations are subsets of
[q957506633/qq-nt-export](https://github.com/q957506633/qq-nt-export/tree/bec1504170f99e2752793320711c5653f3803670), MIT, commit
bec1504170f99e2752793320711c5653f3803670. Sources: scripts/ntqq_msg_parser.py
and scripts/ntqq_key_scan.py. Only the element decoder and API declarations are reused;
the upstream all-chat export and plaintext-key output are not included.
