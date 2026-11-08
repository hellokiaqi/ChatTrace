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
