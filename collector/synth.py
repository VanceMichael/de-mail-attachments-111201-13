"""把 fixtures/messages.json 的合成描述构造成真实的 MIME 字节流。

本模块只在本地生成邮件，不连接任何真实邮箱；example.invalid 域名只表达
供应方身份。构造结果与真实归档中的邮件同构，管线对二者一视同仁。
"""
import base64
import io
import stat
import zipfile
from email.message import EmailMessage, MIMEPart
from email import policy


class SynthError(ValueError):
    pass


def _zip_bytes(members, encrypted=False):
    """由成员描述生成 ZIP 字节；encrypted 时把加密标志位置入所有头。"""
    import struct

    buffer = io.BytesIO()
    # 固定 ZIP 时间戳，保证同一夹具生成字节确定、可重放判重。
    fixed_time = (2024, 1, 1, 0, 0, 0)
    with zipfile.ZipFile(buffer, 'w', compression=zipfile.ZIP_DEFLATED) as archive:
        for member in members:
            kind = member.get('type', 'file')
            info = zipfile.ZipInfo(member['name'], date_time=fixed_time)
            if kind == 'symlink':
                info.external_attr = (stat.S_IFLNK | 0o777) << 16
                archive.writestr(info, member.get('target', ''))
            elif kind == 'dir':
                info.filename = member['name'].rstrip('/') + '/'
                info.external_attr = (stat.S_IFDIR | 0o755) << 16
                archive.writestr(info, b'')
            elif kind == 'zip':
                # 递归构建嵌套 ZIP，其字节作为普通文件成员写入父归档。
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                archive.writestr(info, _zip_bytes(member.get('members', []),
                                                   member.get('encrypted', False)))
            else:
                info.external_attr = (stat.S_IFREG | 0o644) << 16
                payload = member.get('content', '')
                if isinstance(payload, str):
                    payload = payload.encode('utf-8')
                if member.get('oversize_to'):
                    # 测试夹具：把载荷填充到指定字节数以上，触发单文件上限。
                    target = int(member['oversize_to'])
                    if len(payload) < target:
                        payload += b'x' * (target - len(payload))
                archive.writestr(info, payload)
    raw = bytearray(buffer.getvalue())
    if encrypted:
        # 标准库不支持写加密 ZIP；合成数据直接置位通用标志 0x0001，
        # 解包端必须识别并拒绝（不会尝试任何解压或密码）。
        position = 0
        while True:
            position = raw.find(b'PK\x03\x04', position)
            if position < 0:
                break
            struct.pack_into('<H', raw, position + 6,
                            struct.unpack_from('<H', raw, position + 6)[0] | 0x1)
            position += 4
        position = 0
        while True:
            position = raw.find(b'PK\x01\x02', position)
            if position < 0:
                break
            struct.pack_into('<H', raw, position + 8,
                            struct.unpack_from('<H', raw, position + 8)[0] | 0x1)
            position += 4
    return bytes(raw)


def _member_bytes(member):
    """单个附件成员描述 -> (字节, 文件名, 标记)。"""
    kind = member.get('type', 'file')
    if kind == 'zip':
        return _zip_bytes(member.get('members', []),
                          encrypted=member.get('encrypted', False)), member['name']
    if kind == 'symlink' or kind == 'dir':
        # 顶层 MIME 附件不直接表达符号链接；把这类样本包进 ZIP，
        # 附件名补 .zip 后缀使管线按归档处理并触发安全检查。
        original = member['name']
        zip_name = original if original.lower().endswith('.zip') else f'{original}.zip'
        return _zip_bytes([member]), zip_name
    content = member.get('content', '')
    if isinstance(content, str):
        content = content.encode(member.get('charset', 'utf-8'))
    return content, member['name']


def _add_attachment(message, member, sequence):
    data, name = _member_bytes(member)
    mime = member.get('mime', 'application/octet-stream')
    maintype, _, subtype = mime.partition('/')
    if not subtype:
        maintype, subtype = 'application', 'octet-stream'
    part = MIMEPart(policy.default)
    part.set_content(data, maintype=maintype, subtype=subtype)
    if member.get('encoded_filename'):
        # 手工生成 RFC 2231 续行参数，模拟真实邮件里的编码文件名。
        part.add_header('Content-Disposition', 'attachment',
                        **{'filename*': 'utf-8\'\'' + _percent_encode(name)})
    else:
        part.add_header('Content-Disposition', 'attachment', filename=name)
    if member.get('cid'):
        part['Content-ID'] = member['cid']
    if not message.is_multipart():
        message.make_mixed()
    # 固定多部件边界，保证同一规格生成字节确定、可重放判重。
    boundary = f'===============BOUNDARY{sequence:02d}==============='
    message.set_boundary(boundary)
    message.attach(part)


def _percent_encode(name):
    from urllib.parse import quote
    return quote(name, safe='')


def build_message(spec):
    """构造单封邮件；返回 (message_id, 字节流或 None, 标记字典)。

    raw_base64 / raw 样本直接给出畸形字节，无法构造正常 MIME，
    返回 None 由管线按“整封不能读”处理。
    """
    message_id = spec['message_id']
    flags = dict(forward_of=spec.get('forward_of'),
                 supersedes=spec.get('supersedes'),
                 vendor=spec.get('vendor', '示例供应方'),
                 partial=spec.get('partial', False))
    if spec.get('raw_base64'):
        return message_id, base64.b64decode(spec['raw_base64']), flags
    if spec.get('raw') is not None:
        raw_value = spec['raw']
        if isinstance(raw_value, str):
            raw_value = raw_value.encode('utf-8', errors='surrogateescape')
        return message_id, bytes(raw_value), flags

    message = EmailMessage()
    message['Message-ID'] = f'<{message_id}@mail.example.invalid>'
    message['From'] = spec.get('from', 'supplier@example.invalid')
    message['To'] = 'archive@example.invalid'
    message['Subject'] = spec.get('subject', f'监测资料 {message_id}')
    if spec.get('supersedes'):
        message['Supersedes'] = f'<{spec["supersedes"]}@mail.example.invalid>'
    if spec.get('forward_of'):
        message.replace_header('Subject', f'Fwd: {message["Subject"]}')
        message['X-Forwarded-Message-ID'] = f'<{spec["forward_of"]}@mail.example.invalid>'
    message.set_content(spec.get('body', '合作资料附件，请查收。'))

    for sequence, member in enumerate(spec.get('members', [])):
        _add_attachment(message, member, sequence)

    if spec.get('manifest'):
        manifest = spec['manifest']
        text = manifest if isinstance(manifest, str) else _manifest_text(manifest)
        part = MIMEPart(policy.default)
        part.set_content(text.encode('utf-8'), maintype='application',
                         subtype='json', disposition='attachment',
                         filename='manifest.json')
        if not message.is_multipart():
            message.make_mixed()
        message.set_boundary('===============BOUNDARYMETA===============')
        message.attach(part)

    raw = message.as_bytes()
    if spec.get('partial'):
        return message_id, _split_partial(message, spec['partial']), flags | {'partial': True}
    return message_id, raw, flags


def _manifest_text(manifest):
    import json
    return json.dumps(manifest, ensure_ascii=False, sort_keys=True)


def _split_partial(message, partial_spec):
    """把完整邮件拆成 message/partial 分段（RFC 2046）。

    返回 [(序号, 该封分段字节)]，分段共享同一个 partial id。
    分段载荷按 7bit 通道要求做 CTE 包装，重组时逐段拼回原始字节。
    """
    from email.message import Message
    total = partial_spec.get('total', 2) if isinstance(partial_spec, dict) else 2
    identity = message['Message-ID']
    inner = message.as_bytes()
    boundaries = [len(inner) * i // total for i in range(total)] + [len(inner)]
    chunks = [inner[boundaries[i]:boundaries[i + 1]] for i in range(total)]
    fragments = []
    for number, chunk in enumerate(chunks, start=1):
        # 用经典 Message + 7bit base64 包装，保证分段本身可经任何通道传输。
        envelope = Message()
        envelope['Content-Type'] = (
            f'message/partial; id="{identity}"; number={number}; total={total}')
        envelope['Message-ID'] = identity
        envelope['Subject'] = message['Subject']
        envelope['Content-Transfer-Encoding'] = 'base64'
        envelope.set_payload(base64.b64encode(chunk).decode('ascii'))
        fragments.append((number, envelope.as_bytes()))
    return fragments


def build_mailbox(document):
    """把整份 fixtures 文档展开成投递序列。

    返回 (limits, [条目...])；普通邮件条目为 dict(message_id, raw, flags)，
    分段邮件展开为多条 partial 条目。
    """
    limits = document.get('limits', {})
    entries = []
    for spec in document['messages']:
        message_id, raw, flags = build_message(spec)
        if flags.get('partial'):
            for number, fragment in raw:
                entries.append(dict(message_id=message_id, raw=fragment,
                                    flags=flags, partial_number=number,
                                    partial_total=len(raw),
                                    partial_id=spec['message_id']))
        else:
            entries.append(dict(message_id=message_id, raw=raw, flags=flags,
                                partial_number=None, partial_total=None,
                                partial_id=None))
    return limits, entries
