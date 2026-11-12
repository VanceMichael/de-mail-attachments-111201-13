"""安全解包：只在内存中展开 ZIP，全程不触碰文件系统。

硬性边界（由可信清单/运行参数提供，缺省值偏保守）：
- depth：嵌套归档最大层数；
- members：一次投递内累计成员数；
- expanded_bytes：一次投递内累计展开字节；
- file_bytes：单个成员大小。
另拒绝：越界路径、符号链接/设备节点、加密包、非 ZIP 归档。
任何成员越界，整个归档拒绝并给出可定位到成员的原因；不执行任何成员。
"""
import io
import stat
import zipfile
from dataclasses import dataclass


DEFAULT_LIMITS = dict(depth=3, members=20, expanded_bytes=4096, file_bytes=1024)


class UnpackError(ValueError):
    """归档整体不可接受。location 指向具体成员路径。"""

    def __init__(self, reason, location=None):
        super().__init__(reason)
        self.reason = reason
        self.location = location


@dataclass
class Member:
    path: tuple                      # 成员在归档嵌套中的完整路径
    name: str                        # 归一化后的相对名
    data: bytes
    size: int
    depth: int                       # 所在嵌套层（顶层附件为 1）
    is_archive: bool = False         # 该成员本身是否为嵌套归档


@dataclass
class _Budget:
    limits: dict
    members_seen: int = 0
    bytes_seen: int = 0

    def check_member(self, name, size, location):
        self.members_seen += 1
        if self.members_seen > self.limits['members']:
            raise UnpackError(f"成员数量超出上限 {self.limits['members']}", location)
        if size > self.limits['file_bytes']:
            raise UnpackError(f"单文件 {size} 字节超出上限 {self.limits['file_bytes']}",
                              location)
        self.bytes_seen += size
        if self.bytes_seen > self.limits['expanded_bytes']:
            raise UnpackError(
                f"累计展开 {self.bytes_seen} 字节超出上限 {self.limits['expanded_bytes']}",
                location)


def _safe_name(name):
    """归一化成员名并拒绝越界路径；返回相对路径片段元组。"""
    if not name or name.strip() == '':
        raise UnpackError('成员名为空')
    if name.startswith(('/', '\\')):
        raise UnpackError(f"绝对路径成员: {name}")
    normalized = name.replace('\\', '/')
    parts = []
    for fragment in normalized.split('/'):
        if fragment in ('', '.'):
            continue
        if fragment == '..':
            raise UnpackError(f"越界路径成员: {name}")
        parts.append(fragment)
    if not parts:
        raise UnpackError(f"无有效相对路径: {name}")
    return tuple(parts)


def unpack_archive(data, archive_name, limits):
    """展开顶层归档字节，返回扁平 Member 列表（含嵌套归档及其内部文件）。

    嵌套归档自身也作为一个 is_archive=True 的成员返回，调用方可据此
    构建“附件→归档→成员”的完整来源链；预算在整棵树内统一累计。
    """
    merged = dict(DEFAULT_LIMITS)
    merged.update(limits or {})
    budget = _Budget(merged)
    members = []
    _expand(data, (archive_name,), 1, budget, merged, members)
    return members


def _expand(data, location, depth, budget, limits, sink):
    if depth > limits['depth']:
        raise UnpackError(f"嵌套深度超出上限 {limits['depth']}", location)
    try:
        archive = zipfile.ZipFile(io.BytesIO(data))
    except zipfile.BadZipFile as error:
        raise UnpackError(f'不是有效的 ZIP 归档: {error}', location)
    with archive:
        for info in archive.infolist():
            member_location = location + (info.filename,)
            mode = info.external_attr >> 16
            if stat.S_ISLNK(mode):
                raise UnpackError('拒绝符号链接成员', member_location)
            if stat.S_ISCHR(mode) or stat.S_ISBLK(mode) or stat.S_ISFIFO(mode) \
                    or stat.S_ISSOCK(mode):
                raise UnpackError('拒绝设备/管道成员', member_location)
            if info.flag_bits & 0x1:
                raise UnpackError('拒绝加密压缩包（不支持的加密成员）', member_location)
            name_parts = _safe_name(info.filename)
            if info.is_dir():
                continue
            try:
                content = archive.read(info)
            except RuntimeError as error:
                # 头标志未置位但数据确需密码等情形
                raise UnpackError(f'成员无法解压: {error}', member_location)
            budget.check_member(info.filename, len(content), member_location)
            nested = zipfile.is_zipfile(io.BytesIO(content))
            sink.append(Member(path=member_location, name='/'.join(name_parts),
                               data=content, size=len(content), depth=depth,
                               is_archive=nested))
            if nested:
                _expand(content, member_location, depth + 1, budget, limits, sink)
