"""MIME 解析：把原始邮件展开为带定位路径的附件部件。

身份不依赖文件名或 Message-ID；每个部件保留在邮件树中的位置路径，
message/partial 先按 id 重组、message/rfc822 转发件递归展开。
解析层不做任何业务判重，只负责“这封邮件里有什么、各在什么位置”。
"""
import base64
from dataclasses import dataclass
from email import policy
from email.parser import BytesParser
from email.utils import getaddresses


# RFC 2045 允许的内容传输编码；其余值意味着部件无法按声明解码。
_KNOWN_CTE = {'7bit', '8bit', 'binary', 'quoted-printable', 'base64', ''}


class MessageUnreadable(ValueError):
    """整封邮件无法解析（畸形字节、分段不全等）。"""


@dataclass
class Part:
    location: tuple          # 部件定位，如 (1, 2, 0)；嵌套转发追加子路径
    filename: str | None
    content_type: str
    data: bytes
    cte: str | None
    nested_message_id: str | None = None  # 该部件来自哪封内嵌转发邮件


@dataclass
class ParsedMessage:
    message_id: str | None
    subject: str
    from_addr: str
    supersedes: str | None
    forwarded_ids: list
    parts: list
    raw: bytes


def _clean_message_id(value):
    if not value:
        return None
    value = value.strip()
    if value.startswith('<') and value.endswith('>'):
        value = value[1:-1]
    return value or None


def parse_message(raw):
    """解析单封完整邮件；无法解析时抛 MessageUnreadable。"""
    if not isinstance(raw, (bytes, bytearray)) or not raw:
        raise MessageUnreadable('邮件内容为空')
    try:
        message = BytesParser(policy=policy.default).parsebytes(bytes(raw))
    except (ValueError, TypeError, IndexError) as error:
        raise MessageUnreadable(f'MIME 解析失败: {error}')
    if message.is_multipart():
        payload = message.get_payload()
        if not payload:
            raise MessageUnreadable('声明为多部件邮件但没有任何部件')
        # 截断邮件：解析器把缺失闭合边界留在 epilogue，且最后一个部件不完整。
        boundary = message.get_boundary()
        if boundary and message.get_payload(decode=True) is None:
            raw_text = raw.decode('latin1', errors='replace')
            if f'--{boundary}--' not in raw_text:
                raise MessageUnreadable('多部件邮件缺少闭合边界，内容可能被截断')
    message_id = _clean_message_id(message.get('Message-ID'))
    from_addr = message.get('From', '')
    parsed_addresses = getaddresses([from_addr])
    from_addr = parsed_addresses[0][1] if parsed_addresses and parsed_addresses[0][1] else from_addr
    forwarded_ids = []
    for header in message.get_all('X-Forwarded-Message-ID', []):
        cleaned = _clean_message_id(header)
        if cleaned:
            forwarded_ids.append(cleaned)
    parts = []
    _walk(message, (), parts)
    return ParsedMessage(
        message_id=message_id,
        subject=message.get('Subject', ''),
        from_addr=from_addr,
        supersedes=_clean_message_id(message.get('Supersedes')),
        forwarded_ids=forwarded_ids,
        parts=parts,
        raw=bytes(raw))


def _walk(message, prefix, sink):
    for index, part in enumerate(message.iter_parts()):
        location = prefix + (index,)
        content_type = part.get_content_type()
        if content_type in ('message/rfc822', 'message/global'):
            nested = part.get_payload(0)
            nested_id = _clean_message_id(nested.get('Message-ID')) if nested is not None else None
            if nested is not None:
                _walk(nested, location, sink)
                for previous in sink:
                    if previous.nested_message_id is None and len(previous.location) > len(location) \
                            and previous.location[:len(location)] == location:
                        previous.nested_message_id = nested_id
            continue
        if part.is_multipart():
            _walk(part, location, sink)
            continue
        if part.get_content_disposition() != 'attachment':
            continue
        cte = (part.get('Content-Transfer-Encoding') or '').lower()
        if cte not in _KNOWN_CTE:
            raise MessageUnreadable(
                f'部件 {location} 使用不支持的传输编码: {cte}')
        data = part.get_payload(decode=True)
        if data is None:
            raise MessageUnreadable(f'部件 {location} 无法按传输编码解码')
        sink.append(Part(
            location=location,
            filename=part.get_filename(),
            content_type=content_type,
            data=data,
            cte=part.get('Content-Transfer-Encoding'),
            nested_message_id=None))


def _parse_header_params(header_value):
    """从 Content-Type 头值中粗解析 key=value 参数（值可带引号）。"""
    params = {}
    for token in header_value.split(';')[1:]:
        if '=' not in token:
            continue
        key, _, value = token.partition('=')
        params[key.strip().lower()] = value.strip().strip('"')
    return params


class PartialGroup:
    """同一 message/partial id 的分段收集与重组。"""

    def __init__(self):
        self._fragments = {}
        self.total = None
        self.first_raw = None
        self.partial_id = None

    def add(self, raw):
        # message/* 会被各策略递归解析，这里直接从原始字节取头与载荷，
        # 保证分段内容按线路原样拿到，再由 assemble() 拼回完整邮件。
        header_blob, separator, body_blob = raw.partition(b'\n\n')
        if not separator:
            header_blob, _, body_blob = raw.partition(b'\r\n\r\n')
        # 展开 RFC 5322 折行续行（CRLF/WSP 起始的行）。
        unfolded = []
        for line in header_blob.splitlines():
            if line[:1] in (b' ', b'\t') and unfolded:
                unfolded[-1] += b' ' + line.lstrip()
            else:
                unfolded.append(line)
        headers = {}
        for line in unfolded:
            if b':' in line:
                name, _, value = line.partition(b':')
                headers[name.decode('ascii').lower()] = b' '.join(value.split()).decode('ascii', 'replace')
        content_type = headers.get('content-type', '')
        if 'message/partial' not in content_type:
            raise MessageUnreadable('预期的 message/partial 分段内容类型不符')
        params = _parse_header_params(content_type)
        try:
            number = int(params['number'])
            total = int(params['total'])
        except (KeyError, TypeError, ValueError):
            raise MessageUnreadable('分段缺少有效的 number/total 参数')
        partial_id = params.get('id', '').strip('"')
        if self.total is None:
            self.total = total
        elif self.total != total:
            raise MessageUnreadable('分段 total 参数不一致')
        if number in self._fragments:
            raise MessageUnreadable(f'分段 {number} 重复到达')
        payload = body_blob
        if headers.get('content-transfer-encoding', '').lower() == 'base64':
            try:
                payload = base64.b64decode(b''.join(payload.split()))
            except (ValueError, TypeError) as error:
                raise MessageUnreadable(f'分段 {number} base64 解码失败: {error}')
        self._fragments[number] = payload
        self.first_raw = raw
        self.partial_id = partial_id
        return number, total

    def complete(self):
        return self.total is not None and len(self._fragments) == self.total

    def assemble(self):
        if not self.complete():
            missing = sorted(set(range(1, self.total + 1)) - set(self._fragments))
            raise MessageUnreadable(f'分段不完整，缺少: {missing}')
        return b''.join(self._fragments[n] for n in sorted(self._fragments))


def assemble_partials(entries):
    """把投递序列中的分段邮件合并为完整邮件条目；非分段原样返回。"""
    groups = {}
    order = []
    plain = []
    for entry in entries:
        if entry.get('partial_total'):
            key = entry.get('partial_id') or entry['message_id']
            if key not in groups:
                groups[key] = PartialGroup()
                order.append(key)
            groups[key].add(entry['raw'])
        else:
            plain.append(entry)
    assembled = []
    for key in order:
        group = groups[key]
        assembled.append(dict(
            message_id=key,
            raw=group.assemble(),
            flags=dict(partial=True, partial_id=group.partial_id),
            partial_number=None, partial_total=None, partial_id=None))
    return plain + assembled
