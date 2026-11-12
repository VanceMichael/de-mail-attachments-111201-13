"""合成邮件构造与 MIME 解析测试。"""
import base64
import unittest
from email.parser import BytesParser
from email import policy

from collector.synth import build_mailbox, build_message, _zip_bytes
from collector.mailparse import (MessageUnreadable, PartialGroup,
                                 assemble_partials, parse_message)


class SynthTests(unittest.TestCase):
    def setUp(self):
        import json
        from pathlib import Path
        self.doc = json.loads(Path('fixtures/messages.json').read_text())
        self.limits, self.entries = build_mailbox(self.doc)

    def test_partial_expands_into_fragments(self):
        fragments = [e for e in self.entries if e['message_id'] == 'partial-msg']
        self.assertEqual(len(fragments), 3)
        self.assertEqual([e['partial_number'] for e in fragments], [1, 2, 3])

    def test_partial_reassembles_to_identical_message(self):
        fragments = [e for e in self.entries if e['message_id'] == 'partial-msg']
        assembled = assemble_partials(fragments)
        self.assertEqual(len(assembled), 1)
        parsed = parse_message(assembled[0]['raw'])
        self.assertEqual(parsed.message_id, 'partial-msg@mail.example.invalid')
        self.assertTrue(any(p.filename == 'partial-data.csv' for p in parsed.parts))

    def test_missing_fragment_is_unreadable(self):
        fragments = [e for e in self.entries if e['message_id'] == 'partial-msg']
        with self.assertRaises(MessageUnreadable):
            assemble_partials(fragments[:2])

    def test_duplicate_fragment_rejected(self):
        fragments = [e for e in self.entries if e['message_id'] == 'partial-msg']
        group = PartialGroup()
        group.add(fragments[0]['raw'])
        with self.assertRaises(MessageUnreadable):
            group.add(fragments[0]['raw'])

    def test_encoded_filename_roundtrip(self):
        entry = next(e for e in self.entries if e['message_id'] == 'encoded-names')
        parsed = parse_message(entry['raw'])
        names = [p.filename for p in parsed.parts]
        self.assertEqual(names, ['深度资料.csv', '深度资料.csv'])
        # 同名附件靠部件位置区分
        self.assertEqual([p.location for p in parsed.parts], [(1,), (2,)])

    def test_unknown_cte_marks_message_unreadable(self):
        entry = next(e for e in self.entries if e['message_id'] == 'bad-part')
        with self.assertRaises(MessageUnreadable):
            parse_message(entry['raw'])

    def test_truncated_multipart_is_unreadable(self):
        entry = next(e for e in self.entries if e['message_id'] == 'truncated-mime')
        with self.assertRaises(MessageUnreadable):
            parse_message(entry['raw'])

    def test_empty_bytes_unreadable(self):
        with self.assertRaises(MessageUnreadable):
            parse_message(b'')

    def test_supersedes_header_parsed(self):
        entry = next(e for e in self.entries if e['message_id'] == 'correction-v2')
        parsed = parse_message(entry['raw'])
        self.assertEqual(parsed.supersedes, 'correction-v1@mail.example.invalid')

    def test_forwarded_header_captured(self):
        entry = next(e for e in self.entries if e['message_id'] == 'forward-copy')
        parsed = parse_message(entry['raw'])
        self.assertIn('correction-v2@mail.example.invalid', parsed.forwarded_ids)

    def test_identity_not_from_filename(self):
        # 两个同名附件、相同文件名，部件摘要与位置仍可区分
        entry = next(e for e in self.entries if e['message_id'] == 'encoded-names')
        parsed = parse_message(entry['raw'])
        digests = {p.location: p.data for p in parsed.parts}
        self.assertNotEqual(digests[(1,)], digests[(2,)])

    def test_synth_bytes_are_deterministic(self):
        # 同一规格两次构造必须字节一致，重放才能按原始摘要识别为同一邮件。
        import hashlib
        first = [hashlib.sha256(e['raw']).hexdigest() for e in self.entries]
        _, rebuilt = build_mailbox(self.doc)
        second = [hashlib.sha256(e['raw']).hexdigest() for e in rebuilt]
        self.assertEqual(first, second)


class EncryptedZipSynthTests(unittest.TestCase):
    def test_encryption_flag_set_in_all_headers(self):
        raw = _zip_bytes([dict(name='x.csv', content='a,b\n')], encrypted=True)
        parsed = BytesParser(policy=policy.default)  # 仅用于确保字节独立
        self.assertIn(b'PK\x03\x04', raw)
        import io
        import zipfile
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            self.assertTrue(all(i.flag_bits & 0x1 for i in archive.infolist()))
            with self.assertRaises(RuntimeError):
                archive.read(archive.infolist()[0])


if __name__ == '__main__':
    unittest.main()
