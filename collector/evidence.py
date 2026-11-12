"""结构化证据入库：消息 → 部件 → 压缩成员 → 数据行的完整来源链。

设计要点：
- 原始邮件按字节摘要去重，重放同一归档不会产生第二份证据；
- 数据行按 (供应方业务键, 有效载荷摘要) 判重，转发/重复投递不翻倍；
- 更正件形成 supersedes 替代关系，原证据保留、状态转为被替代，绝不覆盖；
- 拒绝与隔离单独留痕，重放时先查既有判定，拒绝记录不丢失；
- 每封邮件一个事务，中断后可从归档原样重放。
"""
import hashlib
import json
import sqlite3
from contextlib import contextmanager


SCHEMA = """
CREATE TABLE IF NOT EXISTS messages(
    digest TEXT PRIMARY KEY,
    message_id TEXT,
    raw BLOB NOT NULL,
    subject TEXT,
    from_addr TEXT,
    supersedes_ref TEXT,
    forwarded_ids TEXT,
    received_order INTEGER NOT NULL,
    is_reassembled INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS parts(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    message_digest TEXT NOT NULL REFERENCES messages(digest),
    location TEXT NOT NULL,
    filename TEXT,
    content_type TEXT,
    cte TEXT,
    payload_digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    UNIQUE(message_digest, location));
CREATE TABLE IF NOT EXISTS members(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    part_id INTEGER NOT NULL REFERENCES parts(id),
    member_path TEXT NOT NULL,
    member_name TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    size INTEGER NOT NULL,
    depth INTEGER NOT NULL,
    UNIQUE(part_id, member_path));
CREATE TABLE IF NOT EXISTS observations(
    row_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    vendor TEXT NOT NULL,
    row_json TEXT NOT NULL,
    status TEXT NOT NULL DEFAULT 'current',
    superseded_by_run INTEGER,
    first_run INTEGER NOT NULL,
    PRIMARY KEY(row_key, payload_digest));
CREATE TABLE IF NOT EXISTS observation_sources(
    row_key TEXT NOT NULL,
    payload_digest TEXT NOT NULL,
    member_id INTEGER NOT NULL REFERENCES members(id),
    line_no INTEGER NOT NULL,
    run_id INTEGER NOT NULL,
    PRIMARY KEY(row_key, payload_digest, member_id, line_no));
CREATE TABLE IF NOT EXISTS rejections(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_id INTEGER NOT NULL,
    message_digest TEXT,
    scope TEXT NOT NULL,
    location TEXT NOT NULL,
    reason TEXT NOT NULL,
    payload_digest TEXT,
    created_order INTEGER NOT NULL,
    UNIQUE(run_id, scope, location, reason));
CREATE TABLE IF NOT EXISTS quarantine(
    payload_digest TEXT PRIMARY KEY,
    data BLOB NOT NULL,
    reason TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS deliveries(
    run_id INTEGER NOT NULL,
    message_digest TEXT NOT NULL REFERENCES messages(digest),
    status TEXT NOT NULL,
    detail TEXT,
    PRIMARY KEY(run_id, message_digest));
CREATE TABLE IF NOT EXISTS runs(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    summary TEXT);
"""


class EvidenceStore:
    def __init__(self, path):
        self.connection = sqlite3.connect(path, timeout=10, isolation_level=None)
        self.connection.row_factory = sqlite3.Row
        self.connection.execute('PRAGMA foreign_keys=ON')
        self.connection.execute('PRAGMA journal_mode=WAL')
        self.connection.executescript(SCHEMA)

    @contextmanager
    def transaction(self):
        self.connection.execute('BEGIN IMMEDIATE')
        try:
            yield
        except BaseException:
            self.connection.rollback()
            raise
        else:
            self.connection.commit()

    def start_run(self, source):
        from datetime import datetime, timezone
        cursor = self.connection.execute(
            'INSERT INTO runs(source, started_at) VALUES (?,?)',
            (source, datetime.now(timezone.utc).isoformat()))
        return cursor.lastrowid

    def finish_run(self, run_id, summary):
        from datetime import datetime, timezone
        self.connection.execute(
            'UPDATE runs SET finished_at=?, summary=? WHERE id=?',
            (datetime.now(timezone.utc).isoformat(),
             json.dumps(summary, ensure_ascii=False, sort_keys=True), run_id))

    def message_known(self, digest):
        row = self.connection.execute(
            'SELECT 1 FROM messages WHERE digest=?', (digest,)).fetchone()
        return row is not None

    def delivery_status(self, run_id, digest):
        row = self.connection.execute(
            'SELECT status FROM deliveries WHERE run_id=? AND message_digest=?',
            (run_id, digest)).fetchone()
        return row[0] if row else None

    def last_delivery(self, digest):
        """该邮件在历史运行中的最近一次交付判定（重放短路依据）。"""
        row = self.connection.execute(
            'SELECT status, detail FROM deliveries WHERE message_digest=? '
            'ORDER BY run_id DESC LIMIT 1', (digest,)).fetchone()
        if row is None:
            return None
        return dict(status=row[0],
                    detail=json.loads(row[1]) if row[1] else None)

    def insert_message(self, digest, raw, parsed, order, is_reassembled=False):
        self.connection.execute(
            'INSERT OR IGNORE INTO messages(digest,message_id,raw,subject,from_addr,'
            'supersedes_ref,forwarded_ids,received_order,is_reassembled) '
            'VALUES (?,?,?,?,?,?,?,?,?)',
            (digest, parsed.message_id, raw, parsed.subject, parsed.from_addr,
             parsed.supersedes, json.dumps(parsed.forwarded_ids, ensure_ascii=False),
             order, 1 if is_reassembled else 0))

    def insert_part(self, message_digest, part):
        digest = hashlib.sha256(part.data).hexdigest()
        self.connection.execute(
            'INSERT OR IGNORE INTO parts(message_digest,location,filename,content_type,'
            'cte,payload_digest,size) VALUES (?,?,?,?,?,?,?)',
            (message_digest, json.dumps(part.location), part.filename,
             part.content_type, part.cte, digest, len(part.data)))
        row = self.connection.execute(
            'SELECT id FROM parts WHERE message_digest=? AND location=?',
            (message_digest, json.dumps(part.location))).fetchone()
        return row[0], digest

    def insert_member(self, part_id, member):
        digest = hashlib.sha256(member.data).hexdigest()
        self.connection.execute(
            'INSERT OR IGNORE INTO members(part_id,member_path,member_name,payload_digest,'
            'size,depth) VALUES (?,?,?,?,?,?)',
            (part_id, json.dumps(member.path, ensure_ascii=False), member.name,
             digest, member.size, member.depth))
        row = self.connection.execute(
            'SELECT id FROM members WHERE part_id=? AND member_path=?',
            (part_id, json.dumps(member.path, ensure_ascii=False))).fetchone()
        return row[0], digest

    def quarantine_bytes(self, data, reason):
        digest = hashlib.sha256(data).hexdigest()
        self.connection.execute(
            'INSERT OR IGNORE INTO quarantine(payload_digest,data,reason) VALUES (?,?,?)',
            (digest, data, reason))
        return digest

    def reject(self, run_id, scope, location, reason, message_digest=None,
               payload_digest=None, order=0):
        """记录拒绝；同一判定（范围+定位+原因）跨 run 只留一条。

        run_id 取首次做出该判定的运行，保证重放不产生重复拒绝行。
        """
        location_json = json.dumps(location, ensure_ascii=False)
        existing = self.connection.execute(
            'SELECT run_id FROM rejections WHERE scope=? AND location=? AND reason=?',
            (scope, location_json, reason)).fetchone()
        if existing is not None:
            return existing[0]
        cursor = self.connection.execute(
            'INSERT INTO rejections(run_id,message_digest,scope,location,reason,'
            'payload_digest,created_order) VALUES (?,?,?,?,?,?,?)',
            (run_id, message_digest, scope, location_json, reason,
             payload_digest, order))
        return cursor.lastrowid

    def upsert_observation(self, row_key, payload_digest, vendor, row, run_id):
        """幂等写入数据行；返回 'new' 或 'duplicate'。"""
        encoded = json.dumps(row, ensure_ascii=False, sort_keys=True, allow_nan=False)
        existing = self.connection.execute(
            'SELECT status FROM observations WHERE row_key=? AND payload_digest=?',
            (row_key, payload_digest)).fetchone()
        if existing is not None:
            return 'duplicate'
        self.connection.execute(
            'INSERT INTO observations(row_key,payload_digest,vendor,row_json,status,first_run) '
            'VALUES (?,?,?,?,?,?)',
            (row_key, payload_digest, vendor, encoded, 'current', run_id))
        return 'new'

    def link_source(self, row_key, payload_digest, member_id, line_no, run_id):
        self.connection.execute(
            'INSERT OR IGNORE INTO observation_sources(row_key,payload_digest,member_id,'
            'line_no,run_id) VALUES (?,?,?,?,?)',
            (row_key, payload_digest, member_id, line_no, run_id))

    def supersede_vendor_rows(self, vendor, business_keys, run_id, before_order):
        """更正件：把供应方既有同业务键的当前行标记为被替代，原证据保留。

        只替代来源消息接收顺序早于 before_order 的行，更正件自身刚写入
        的新版本保持 current，避免在同一事务/同一 run 内被连带替代。
        """
        changed = 0
        for key in business_keys:
            cursor = self.connection.execute(
                "UPDATE observations SET status='superseded', superseded_by_run=? "
                "WHERE row_key=? AND status='current' AND EXISTS ("
                "SELECT 1 FROM observation_sources os "
                "JOIN members me ON me.id=os.member_id "
                "JOIN parts p ON p.id=me.part_id "
                "JOIN messages m ON m.digest=p.message_digest "
                "WHERE os.row_key=observations.row_key "
                "AND os.payload_digest=observations.payload_digest "
                "AND m.received_order < ?)",
                (run_id, key, before_order))
            changed += cursor.rowcount
        return changed

    def mark_delivery(self, run_id, message_digest, status, detail=None):
        self.connection.execute(
            'INSERT OR REPLACE INTO deliveries(run_id,message_digest,status,detail) '
            'VALUES (?,?,?,?)',
            (run_id, message_digest, status,
             json.dumps(detail, ensure_ascii=False) if detail is not None else None))

    # ---- 查询侧 ----
    def current_rows(self, vendor=None):
        sql = ('SELECT row_key, vendor, row_json FROM observations WHERE status=?')
        params = ['current']
        if vendor:
            sql += ' AND vendor=?'
            params.append(vendor)
        sql += ' ORDER BY row_key'
        return [dict(row_key=row[0], vendor=row[1], **json.loads(row[2]))
                for row in self.connection.execute(sql, params)]

    def row_history(self, row_key):
        return [dict(row_key=row[0], payload_digest=row[1], status=row[2],
                     first_run=row[3], superseded_by_run=row[4],
                     row=json.loads(row[5]))
                for row in self.connection.execute(
                    'SELECT row_key,payload_digest,status,first_run,'
                    'superseded_by_run,row_json FROM observations WHERE row_key=? '
                    'ORDER BY first_run', (row_key,))]

    def lineage(self, row_key, payload_digest):
        """回溯某行的完整来源链：消息 → 部件 → 成员 → 行号。"""
        rows = self.connection.execute(
            'SELECT m.digest AS message_digest, m.message_id, m.subject, '
            'p.location AS part_location, p.filename, p.cte, '
            'me.member_path, me.member_name, os.line_no '
            'FROM observation_sources os '
            'JOIN members me ON me.id=os.member_id '
            'JOIN parts p ON p.id=me.part_id '
            'JOIN messages m ON m.digest=p.message_digest '
            'WHERE os.row_key=? AND os.payload_digest=?',
            (row_key, payload_digest)).fetchall()
        return [dict(row) for row in rows]

    def rejections(self, run_id=None):
        sql = ('SELECT run_id, scope, location, reason, message_digest, payload_digest '
               'FROM rejections')
        params = []
        if run_id is not None:
            sql += ' WHERE run_id=?'
            params.append(run_id)
        sql += ' ORDER BY id'
        result = []
        for row in self.connection.execute(sql, params):
            item = dict(row)
            item['location'] = json.loads(item['location'])
            result.append(item)
        return result

    def close(self):
        self.connection.close()
