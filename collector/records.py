"""CSV 行结构化、供应方业务键与有效载荷摘要。

判重依据不是文件名，而是：
1. 行有效载荷的规范化摘要（canonical digest）；
2. 供应方业务键（vendor + 行内主键列）。
二者都相同的数据行，无论来自原件、转发还是重放，都只入库一次。
"""
import csv
import hashlib
import io
import json


KEY_COLUMNS = ('id', 'record_id', '业务编号', '监测编号')


class RowError(ValueError):
    pass


def canonical_digest(row):
    """对一行数据做与列序、空格无关的规范化 SHA-256 摘要。"""
    canonical = json.dumps(row, ensure_ascii=False, sort_keys=True,
                           separators=(',', ':'), allow_nan=False)
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


def business_key(vendor, row):
    """供应方业务键：vendor 域 + 行内主键列；无主键列时退回载荷摘要。"""
    for column in KEY_COLUMNS:
        if column in row and str(row[column]).strip() != '':
            return f'{vendor}::{column}={str(row[column]).strip()}'
    return f'{vendor}::digest:{canonical_digest(row)}'


def parse_csv(data, location):
    """把字节 CSV 解析为 dict 行；编码失败或无表头视为坏部件。"""
    try:
        text = data.decode('utf-8-sig')
    except UnicodeDecodeError:
        try:
            text = data.decode('gb18030')
        except UnicodeDecodeError as error:
            raise RowError(f'CSV 编码无法识别: {error}')
    reader = csv.DictReader(io.StringIO(text))
    if reader.fieldnames is None:
        raise RowError('CSV 缺少表头')
    headers = [name.strip() for name in reader.fieldnames]
    if any(name == '' for name in headers):
        raise RowError('CSV 表头存在空列名')
    rows = []
    for line_number, raw_row in enumerate(reader, start=2):
        if raw_row is None:
            continue
        if None in raw_row:
            raise RowError(f'第 {line_number} 行列数超过表头')
        row = {(key.strip() if key else key): (value.strip() if isinstance(value, str) else value)
               for key, value in raw_row.items() if key is not None}
        if not any(value not in (None, '') for value in row.values()):
            continue
        rows.append(row)
    return headers, rows


def manifest_from_bytes(data):
    """解析可信清单（JSON）。清单自身坏了按坏部件处理。"""
    try:
        manifest = json.loads(data.decode('utf-8'))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise RowError(f'清单无法解析: {error}')
    if not isinstance(manifest, dict):
        raise RowError('清单必须是 JSON 对象')
    files = manifest.get('files')
    if not isinstance(files, list) or not files:
        raise RowError('清单缺少非空 files 列表')
    expectations = []
    for item in files:
        if not isinstance(item, dict) or 'name' not in item:
            raise RowError('清单条目缺少 name')
        entry = dict(name=item['name'])
        if 'sha256' in item:
            entry['sha256'] = str(item['sha256']).lower()
        if 'rows' in item:
            entry['rows'] = int(item['rows'])
        expectations.append(entry)
    return expectations


def manifest_satisfied(expectations, accepted):
    """可信清单是否全部满足：每个文件都被收下且行数/摘要一致。

    accepted: dict 文件名 -> dict(sha256, rows)，只统计真正入库的有效数据。
    返回 (是否完整, 缺失说明列表)。
    """
    missing = []
    by_name = {item['name']: item for item in accepted}
    for expected in expectations:
        actual = by_name.get(expected['name'])
        if actual is None:
            missing.append(f"清单文件未收讫: {expected['name']}")
            continue
        if 'sha256' in expected and actual['sha256'] != expected['sha256']:
            missing.append(
                f"{expected['name']} 摘要不符: 期望 {expected['sha256']} 实际 {actual['sha256']}")
        if 'rows' in expected and actual['rows'] != expected['rows']:
            missing.append(
                f"{expected['name']} 行数不符: 期望 {expected['rows']} 实际 {actual['rows']}")
    return not missing, missing
