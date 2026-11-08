import hashlib
import hmac
import struct
import unittest

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from chattrace.core import ArchiveError
from chattrace.wechat_snapshot import checksum, decode_snapshot


class SnapshotTests(unittest.TestCase):
    def setUp(self):
        self.key = bytes(range(32))
        self.salt = bytes(range(16, 32))
        self.mac = hashlib.pbkdf2_hmac('sha512', self.key, bytes(b ^ 0x3a for b in self.salt), 2, 32)

    def encrypted_page(self, number, marker):
        first = number == 1
        plain = bytearray(4000 if first else 4016)
        plain[100:100 + len(marker)] = marker
        iv = bytes([number]) * 16
        encryptor = Cipher(algorithms.AES(self.key), modes.CBC(iv)).encryptor()
        encrypted = encryptor.update(bytes(plain)) + encryptor.finalize()
        auth = hmac.new(self.mac, encrypted + iv + struct.pack('<I', number), hashlib.sha512).digest()
        return (self.salt if first else b'') + encrypted + iv + auth

    def wal(self, frames):
        header = struct.pack('>IIII', 0x377f0682, 3007000, 4096, 0) + b'TESTSALT'
        state = checksum(header, (0, 0), '<')
        result = header + struct.pack('>II', *state)
        for number, commit_size, page in frames:
            first = struct.pack('>II', number, commit_size)
            state = checksum(first + page, state, '<')
            result += first + b'TESTSALT' + struct.pack('>II', *state) + page
        return result

    def test_authenticated_pages_and_tampering(self):
        raw = self.encrypted_page(1, b'BASE')
        plain = decode_snapshot(raw, b'', self.key)
        self.assertEqual(plain[:16], b'SQLite format 3\0')
        self.assertEqual(plain[116:120], b'BASE')
        broken = bytearray(raw)
        broken[128] ^= 1
        with self.assertRaisesRegex(ArchiveError, 'PAGE_AUTHENTICATION'):
            decode_snapshot(bytes(broken), b'', self.key)

    def test_only_committed_wal_frames_are_applied(self):
        raw = self.encrypted_page(1, b'BASE')
        frames = [(1, 1, self.encrypted_page(1, b'COMMITTED')),
                  (1, 0, self.encrypted_page(1, b'UNCOMMITTED'))]
        plain = decode_snapshot(raw, self.wal(frames), self.key)
        self.assertEqual(plain[116:125], b'COMMITTED')

    def test_invalid_wal_is_reported(self):
        raw = self.encrypted_page(1, b'BASE')
        wal = bytearray(self.wal([(1, 1, self.encrypted_page(1, b'COMMIT'))]))
        wal[-1] ^= 1
        with self.assertRaisesRegex(ArchiveError, 'WAL_FRAME_CHECKSUM'):
            decode_snapshot(raw, bytes(wal), self.key)


if __name__ == '__main__':
    unittest.main()
