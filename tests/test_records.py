"""业务键、载荷摘要与可信清单测试。"""
import unittest

from collector.records import (RowError, business_key, canonical_digest,
                               manifest_from_bytes, manifest_satisfied, parse_csv)


class CanonicalTests(unittest.TestCase):
    def test_digest_independent_of_column_order(self):
        left = canonical_digest({'id': '1', 'value': '20'})
        right = canonical_digest({'value': '20', 'id': '1'})
        self.assertEqual(left, right)

    def test_digest_changes_with_value(self):
        self.assertNotEqual(canonical_digest({'id': '1', 'v': '1'}),
                            canonical_digest({'id': '1', 'v': '2'}))

    def test_business_key_uses_vendor_and_id_column(self):
        key = business_key('供应方:example.invalid', {'id': '20', 'v': '1'})
        self.assertEqual(key, '供应方:example.invalid::id=20')

    def test_business_key_falls_back_to_digest(self):
        row = {'station': 'S1', 'depth': '100'}
        key = business_key('v', row)
        self.assertTrue(key.startswith('v::digest:'))
        self.assertEqual(key, f'v::digest:{canonical_digest(row)}')

    def test_same_payload_same_key_across_forward(self):
        row = {'id': '9', 'value': '99'}
        self.assertEqual(business_key('v', row), business_key('v', dict(row)))


class CsvTests(unittest.TestCase):
    def test_parses_utf8_with_bom(self):
        headers, rows = parse_csv('id,value\n1,20\n'.encode('utf-8-sig'),
                                  ('f.csv',))
        self.assertEqual(headers, ['id', 'value'])
        self.assertEqual(rows, [{'id': '1', 'value': '20'}])

    def test_parses_gb18030(self):
        headers, rows = parse_csv('id,名称\n1,深度\n'.encode('gb18030'),
                                  ('f.csv',))
        self.assertEqual(rows[0]['名称'], '深度')

    def test_unknown_encoding_is_row_error(self):
        with self.assertRaises(RowError):
            parse_csv(b'\xff\xfe\x00id', ('f.csv',))

    def test_extra_column_rejected(self):
        with self.assertRaises(RowError):
            parse_csv(b'id\n1,2,3\n', ('f.csv',))

    def test_blank_lines_skipped(self):
        _, rows = parse_csv(b'id\n1\n\n2\n', ('f.csv',))
        self.assertEqual(rows, [{'id': '1'}, {'id': '2'}])


class ManifestTests(unittest.TestCase):
    def test_manifest_parse_and_satisfaction(self):
        data = b'{"files":[{"name":"a.csv","rows":1,"sha256":"abc"}]}'
        expectations = manifest_from_bytes(data)
        self.assertEqual(expectations[0]['name'], 'a.csv')
        okay, missing = manifest_satisfied(
            expectations, [dict(name='a.csv', rows=1, sha256='abc')])
        self.assertTrue(okay)
        self.assertEqual(missing, [])

    def test_missing_file_unmet(self):
        expectations = manifest_from_bytes(
            b'{"files":[{"name":"a.csv"},{"name":"b.csv"}]}')
        okay, missing = manifest_satisfied(
            expectations, [dict(name='a.csv', rows=1, sha256='x')])
        self.assertFalse(okay)
        self.assertTrue(any('b.csv' in note for note in missing))

    def test_digest_mismatch_unmet(self):
        expectations = manifest_from_bytes(
            b'{"files":[{"name":"a.csv","sha256":"want"}]}')
        okay, missing = manifest_satisfied(
            expectations, [dict(name='a.csv', rows=1, sha256='got')])
        self.assertFalse(okay)
        self.assertTrue(any('摘要不符' in note for note in missing))

    def test_broken_manifest_is_row_error(self):
        with self.assertRaises(RowError):
            manifest_from_bytes(b'{not json')

    def test_manifest_without_files_rejected(self):
        with self.assertRaises(RowError):
            manifest_from_bytes(b'{"other":1}')


if __name__ == '__main__':
    unittest.main()
