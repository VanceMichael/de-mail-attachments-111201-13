"""安全解包器测试：层数、成员数、字节预算与危险成员拒绝。"""
import io
import unittest
import zipfile

from collector.extract import UnpackError, unpack_archive
from collector.synth import _zip_bytes


LIMITS = dict(depth=3, members=20, expanded_bytes=4096, file_bytes=1024)


def nested_zip(levels, leaf_name='leaf.csv', leaf_content=b'id,value\n1,1\n'):
    members = [dict(name=leaf_name, content=leaf_content.decode())]
    for level in range(levels):
        members = [dict(name=f'inner{level}.zip', type='zip', members=members)]
    return _zip_bytes(members)


class UnpackSafetyTests(unittest.TestCase):
    def test_flat_zip_members(self):
        raw = _zip_bytes([dict(name='a.csv', content='id\n1\n'),
                          dict(name='b.csv', content='id\n2\n')])
        members = unpack_archive(raw, 'x.zip', LIMITS)
        files = [m for m in members if not m.is_archive]
        self.assertEqual([m.name for m in files], ['a.csv', 'b.csv'])

    def test_nested_within_depth_accepted(self):
        raw = nested_zip(2)  # 顶层 + 2 层 = 深度 3，恰好等于上限
        members = unpack_archive(raw, 'top.zip', LIMITS)
        leaf = [m for m in members if m.name == 'leaf.csv']
        self.assertEqual(len(leaf), 1)
        self.assertEqual(leaf[0].depth, 3)

    def test_depth_exceeded_rejected(self):
        raw = nested_zip(3)  # 深度 4，超出上限 3
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'top.zip', LIMITS)
        self.assertIn('嵌套深度', context.exception.reason)
        self.assertIsNotNone(context.exception.location)

    def test_path_traversal_rejected(self):
        raw = _zip_bytes([dict(name='../outside.csv', content='x')])
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', LIMITS)
        self.assertIn('越界路径', context.exception.reason)

    def test_absolute_path_rejected(self):
        raw = _zip_bytes([dict(name='/etc/passwd', content='x')])
        with self.assertRaises(UnpackError):
            unpack_archive(raw, 'x.zip', LIMITS)

    def test_symlink_rejected(self):
        raw = _zip_bytes([dict(name='link', type='symlink', target='../escape')])
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', LIMITS)
        self.assertIn('符号链接', context.exception.reason)
        self.assertEqual(context.exception.location[-1], 'link')

    def test_encrypted_archive_rejected(self):
        raw = _zip_bytes([dict(name='secret.csv', content='x')], encrypted=True)
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', LIMITS)
        self.assertIn('加密', context.exception.reason)

    def test_file_size_limit_enforced(self):
        raw = _zip_bytes([dict(name='big.csv', content='x' * 5,
                               oversize_to=2048)])
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', dict(LIMITS, file_bytes=1024))
        self.assertIn('单文件', context.exception.reason)

    def test_member_count_limit_enforced(self):
        raw = _zip_bytes([dict(name=f'm{i}.csv', content='a\n') for i in range(5)])
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', dict(LIMITS, members=3))
        self.assertIn('成员数量', context.exception.reason)

    def test_expanded_bytes_limit_enforced(self):
        raw = _zip_bytes([dict(name='a.csv', content='x' * 100),
                          dict(name='b.csv', content='y' * 100)])
        with self.assertRaises(UnpackError) as context:
            unpack_archive(raw, 'x.zip', dict(LIMITS, expanded_bytes=150))
        self.assertIn('累计展开', context.exception.reason)

    def test_bad_zip_raises_with_location(self):
        with self.assertRaises(UnpackError) as context:
            unpack_archive(b'not a zip', 'broken.zip', LIMITS)
        self.assertIn('ZIP', context.exception.reason)
        self.assertEqual(context.exception.location, ('broken.zip',))

    def test_no_filesystem_access(self):
        # 解包全程在内存：不存在任何写出路径，危险名字只产生异常
        raw = _zip_bytes([dict(name='safe.csv', content='id\n1\n')])
        members = unpack_archive(raw, 'x.zip', LIMITS)
        self.assertTrue(all(isinstance(m.data, bytes) for m in members))


if __name__ == '__main__':
    unittest.main()
