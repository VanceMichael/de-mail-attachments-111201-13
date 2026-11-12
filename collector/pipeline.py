"""处理管线：合成/归档邮件 → 证据入库 → 面向资料员的处理报告。

状态语义（每封邮件一个结论）：
- complete        有效数据已收下；若带可信清单则清单全部满足
- partial_delivery 有数据收下，但清单未全部满足或缺清单核对依据
- rejected_message 整封不能读（畸形 MIME、分段不全）
- rejected_attachment 某附件/成员被拒，无有效数据入库
- no_data         邮件可读但没有可识别的数据附件
所有拒绝同时写 rejections/quarantine，报告可定位到具体部件。
"""
import hashlib

from .extract import UnpackError, unpack_archive
from .mailparse import MessageUnreadable, assemble_partials, parse_message
from .records import (RowError, business_key, canonical_digest, manifest_from_bytes,
                      manifest_satisfied, parse_csv)


MANIFEST_NAMES = ('manifest.json',)


class _DirectMember:
    def __init__(self, path, name, data, depth):
        self.path = path
        self.name = name
        self.data = data
        self.size = len(data)
        self.depth = depth


def _member_for_direct_part(store, part_id, filename, data):
    member = _DirectMember((filename or 'inline',), filename or 'inline', data, 0)
    return store.insert_member(part_id, member)


def digest_bytes(data):
    return hashlib.sha256(data).hexdigest()


def _is_archive_name(filename):
    if not filename:
        return False
    return filename.lower().endswith('.zip')


def process_mailbox(entries, limits, store, source, run_id):
    """处理整个投递序列；entries 来自 synth.build_mailbox 或归档重放。

    每封邮件独立事务；单封失败不影响其他邮件，全部判定落库。
    返回可直接序列化的处理报告。
    """
    entries = assemble_partials(entries)
    report = dict(source=source, run_id=run_id, messages=[])
    totals = dict(messages=0, complete=0, partial_delivery=0, rejected_message=0,
                  rejected_attachment=0, no_data=0, rows_new=0, rows_duplicate=0,
                  rejections=0, supersessions=0)

    for order, entry in enumerate(entries):
        result = _process_one(entry, order, limits, store, run_id)
        report['messages'].append(result)
        totals['messages'] += 1
        totals[result['status']] = totals.get(result['status'], 0) + 1
        totals['rows_new'] += result['rows_new']
        totals['rows_duplicate'] += result['rows_duplicate']
        totals['rejections'] += len(result['rejections'])
        totals['supersessions'] += result['superseded']

    report['totals'] = totals
    return report


def _process_one(entry, order, limits, store, run_id):
    raw = entry['raw']
    message_digest = digest_bytes(raw)
    flags = entry.get('flags', {})
    vendor = flags.get('vendor', '示例供应方')
    base = dict(message_id=entry.get('message_id'),
                digest=message_digest,
                status=None, rows_new=0, rows_duplicate=0,
                superseded=0, rejections=[], accepted_files=[],
                detail=None)

    with store.transaction():
        # 重放短路：同一封邮件（按原始字节摘要）已在历史运行处理过，
        # 直接沿用既有判定，数据行、拒绝与隔离都不会重复写入。
        prior = store.delivery_status(run_id, message_digest)
        if prior is None:
            historical = store.last_delivery(message_digest)
        else:
            historical = dict(status=prior, detail=None)
        if historical is not None:
            store.mark_delivery(run_id, message_digest, historical['status'],
                                historical.get('detail'))
            base['status'] = historical['status']
            base['detail'] = '重放：该邮件此前已处理，沿用既有判定'
            return base

        try:
            parsed = parse_message(raw)
        except MessageUnreadable as error:
            store.quarantine_bytes(raw, str(error))
            # 原始邮件仍作为证据留一行（标识字段允许为空），交付状态可外键到它。
            store.connection.execute(
                'INSERT OR IGNORE INTO messages(digest,message_id,raw,subject,'
                'from_addr,supersedes_ref,forwarded_ids,received_order,is_reassembled) '
                'VALUES (?,?,?,?,?,?,?,?,?)',
                (message_digest, entry.get('message_id'), raw, None, None, None,
                 '[]', order, 1 if flags.get('partial') else 0))
            store.reject(run_id, 'message', [entry.get('message_id')], str(error),
                         message_digest=message_digest, payload_digest=message_digest,
                         order=order)
            store.mark_delivery(run_id, message_digest, 'rejected_message',
                                dict(reason=str(error)))
            base['status'] = 'rejected_message'
            base['rejections'].append(dict(scope='message',
                                           location=[entry.get('message_id')],
                                           reason=str(error)))
            return base

        store.insert_message(message_digest, raw, parsed, order,
                             is_reassembled=bool(flags.get('partial')))
        vendor = _vendor_from_message(parsed, vendor)
        accepted, manifest_expectations, rejection = _process_parts(
            parsed, limits, store, run_id, message_digest, vendor, order, base)

        if rejection and not accepted:
            store.mark_delivery(run_id, message_digest, 'rejected_attachment',
                                dict(reason=rejection))
            base['status'] = 'rejected_attachment'
            base['detail'] = rejection
            return base

        if not accepted:
            store.mark_delivery(run_id, message_digest, 'no_data')
            base['status'] = 'no_data'
            return base

        # 更正件：同供应方同业务键的既有当前行转为被替代，原证据保留。
        superseded = 0
        if parsed.supersedes:
            keys = {key for item in accepted for key in item['row_keys']}
            superseded = store.supersede_vendor_rows(vendor, keys, run_id, order)
        base['superseded'] = superseded

        status, detail = _evaluate_completeness(
            manifest_expectations, accepted, store, run_id, message_digest, order)
        store.mark_delivery(run_id, message_digest, status, detail)
        base['status'] = status
        base['detail'] = detail
        base['accepted_files'] = [item['name'] for item in accepted]
        return base


def _vendor_from_message(parsed, fallback):
    """供应方身份取自可信信封地址域（合成数据固定 example.invalid）。"""
    if parsed.from_addr and '@' in parsed.from_addr:
        domain = parsed.from_addr.rsplit('@', 1)[1].lower()
        return f'供应方:{domain}'
    return fallback


def _process_parts(parsed, limits, store, run_id, message_digest, vendor, order, base):
    """逐部件处理；返回 (收下的文件统计, 清单期望, 致命拒绝原因)。"""
    accepted = []
    manifest_expectations = None
    fatal_reason = None

    for part in parsed.parts:
        part_id, part_digest = store.insert_part(message_digest, part)
        part_location = [parsed.message_id or message_digest[:12],
                         'part', list(part.location)]
        is_manifest = (part.filename in MANIFEST_NAMES
                       or (part.content_type == 'application/json'
                           and (part.filename or '').lower().endswith('.json')))
        if is_manifest:
            try:
                manifest_expectations = manifest_from_bytes(part.data)
            except RowError as error:
                store.reject(run_id, 'part', part_location, str(error),
                             message_digest, part_digest, order)
                base['rejections'].append(dict(scope='part', location=part_location,
                                               reason=str(error)))
                fatal_reason = str(error)
            continue

        if _is_archive_name(part.filename):
            outcome = _process_archive(part, part_id, part_digest, part_location,
                                       limits, store, run_id, message_digest,
                                       vendor, order, base)
            if outcome is None:
                fatal_reason = fatal_reason or base['rejections'][-1]['reason']
            else:
                accepted.extend(outcome)
            continue

        outcome = _process_payload(part.data, part.filename, part_location,
                                   store, run_id, part_id,
                                   part_digest=part_digest,
                                   member_path=(part.filename or 'inline',),
                                   depth=0, vendor=vendor, base=base)
        if outcome is None:
            fatal_reason = fatal_reason or base['rejections'][-1]['reason']
        else:
            accepted.append(outcome)

    return accepted, manifest_expectations, fatal_reason


def _process_archive(part, part_id, part_digest, part_location, limits, store,
                     run_id, message_digest, vendor, order, base):
    try:
        members = unpack_archive(part.data, part.filename or 'archive.zip', limits)
    except UnpackError as error:
        location = part_location + (['member'] + list(error.location or ()))
        store.quarantine_bytes(part.data, error.reason)
        store.reject(run_id, 'archive', location, error.reason,
                     message_digest, part_digest, order)
        base['rejections'].append(dict(scope='archive', location=location,
                                       reason=error.reason))
        return None

    results = []
    for member in members:
        if member.is_archive:
            # 嵌套归档只登记来源链与摘要，不直接当作数据处理。
            store.insert_member(part_id, member)
            continue
        member_id, member_digest = store.insert_member(part_id, member)
        member_location = part_location + ['member'] + list(member.path)
        outcome = _process_payload(
            member.data, member.name, member_location, store, run_id, member_id,
            member_path=member.path, depth=member.depth, vendor=vendor, base=base)
        if outcome is None:
            return None
        results.append(outcome)
    return results


def _process_payload(data, filename, location, store, run_id, source_id,
                     member_path, depth, vendor, base, part_digest=None):
    """解析单个 CSV 成员并幂等入库；失败则留拒绝痕并返回 None。"""
    if not (filename or '').lower().endswith('.csv'):
        reason = f'不支持的数据类型，仅接收 CSV: {filename}'
        store.reject(run_id, 'member', location, reason,
                     payload_digest=digest_bytes(data), order=0)
        base['rejections'].append(dict(scope='member', location=location, reason=reason))
        return None
    try:
        headers, rows = parse_csv(data, location)
    except RowError as error:
        payload_digest = store.quarantine_bytes(data, str(error))
        store.reject(run_id, 'member', location, str(error),
                     payload_digest=payload_digest, order=0)
        base['rejections'].append(dict(scope='member', location=location,
                                       reason=str(error)))
        return None

    if part_digest is not None:
        # 直接 MIME 附件：为来源链补一条“单层成员”，与压缩成员同构。
        member_id, _ = _member_for_direct_part(store, source_id, filename, data)
    else:
        member_id = source_id
    new_count = duplicate_count = 0
    row_keys = []
    for line_no, row in enumerate(rows, start=2):
        payload_digest = canonical_digest(row)
        row_key = business_key(vendor, row)
        verdict = store.upsert_observation(row_key, payload_digest, vendor, row, run_id)
        store.link_source(row_key, payload_digest, member_id, line_no, run_id)
        row_keys.append(row_key)
        if verdict == 'new':
            new_count += 1
        else:
            duplicate_count += 1
    base['rows_new'] += new_count
    base['rows_duplicate'] += duplicate_count
    return dict(name=filename, path=list(member_path),
                sha256=digest_bytes(data), rows=len(rows),
                rows_new=new_count, rows_duplicate=duplicate_count,
                row_keys=row_keys)


def _evaluate_completeness(expectations, accepted, store, run_id, message_digest, order):
    if expectations is None:
        return 'partial_delivery', '邮件未附可信清单，数据已收下但无法核验交付完整性'
    okay, missing = manifest_satisfied(expectations, accepted)
    if okay:
        return 'complete', dict(manifest_files=len(expectations))
    for note in missing:
        store.reject(run_id, 'manifest', [message_digest[:12]], note,
                     message_digest=message_digest, order=order)
    return 'partial_delivery', dict(manifest_unmet=missing)
