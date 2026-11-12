"""端到端管线测试：来源链、判重、替代关系、清单核验与重放。"""
import json
import tempfile
import unittest
from pathlib import Path

from collector.evidence import EvidenceStore
from collector.pipeline import process_mailbox
from collector.synth import build_mailbox


FIXTURE = Path('fixtures/messages.json')


class PipelineTestBase(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.db_path = Path(self.temporary.name) / 'evidence.sqlite3'
        doc = json.loads(FIXTURE.read_text())
        self.limits, self.entries = build_mailbox(doc)
        self.store = EvidenceStore(self.db_path)

    def tearDown(self):
        self.store.close()
        self.temporary.cleanup()

    def run_once(self, source='fixture'):
        run_id = self.store.start_run(source)
        report = process_mailbox(self.entries, self.limits, self.store,
                                 source, run_id)
        self.store.finish_run(run_id, report['totals'])
        return run_id, report

    def statuses(self, report):
        return {m['message_id']: m['status'] for m in report['messages']}


class DeliveryStatusTests(PipelineTestBase):
    def test_expected_status_for_each_message(self):
        _, report = self.run_once()
        statuses = self.statuses(report)
        self.assertEqual(statuses['bad-part'], 'rejected_message')
        self.assertEqual(statuses['truncated-mime'], 'rejected_message')
        self.assertEqual(statuses['symlink'], 'rejected_attachment')
        self.assertEqual(statuses['encrypted-zip'], 'rejected_attachment')
        self.assertEqual(statuses['same-id'], 'rejected_attachment')
        self.assertEqual(statuses['oversize-file'], 'rejected_attachment')
        self.assertEqual(statuses['nested-zip'], 'complete')
        self.assertEqual(statuses['manifest-missing'], 'partial_delivery')
        self.assertEqual(statuses['forward-id'], 'no_data')

    def test_manifest_complete_only_when_all_files_met(self):
        _, report = self.run_once()
        nested = next(m for m in report['messages']
                      if m['message_id'] == 'nested-zip')
        self.assertEqual(nested['detail'], {'manifest_files': 2})
        missing = next(m for m in report['messages']
                       if m['message_id'] == 'manifest-missing')
        self.assertIn('manifest_unmet', missing['detail'])

    def test_report_locates_specific_part(self):
        _, report = self.run_once()
        symlink = next(m for m in report['messages']
                       if m['message_id'] == 'symlink')
        rejection = symlink['rejections'][0]
        self.assertEqual(rejection['scope'], 'archive')
        self.assertIn('link', rejection['location'][-1])

    def test_rejections_persisted_and_quarantined(self):
        self.run_once()
        rejections = self.store.rejections()
        scopes = {item['scope'] for item in rejections}
        self.assertIn('message', scopes)
        self.assertIn('archive', scopes)
        quarantine_count = self.store.connection.execute(
            'SELECT count(*) FROM quarantine').fetchone()[0]
        self.assertGreaterEqual(quarantine_count, 3)


class DedupTests(PipelineTestBase):
    def test_forwarded_payload_does_not_double_rows(self):
        _, report = self.run_once()
        forward = next(m for m in report['messages']
                       if m['message_id'] == 'forward-copy')
        self.assertEqual(forward['rows_new'], 0)
        self.assertGreaterEqual(forward['rows_duplicate'], 1)

    def test_replay_is_fully_idempotent(self):
        self.run_once()
        before = {table: self.store.connection.execute(
            f'SELECT count(*) FROM {table}').fetchone()[0]
            for table in ('messages', 'parts', 'members', 'observations',
                          'observation_sources', 'rejections', 'quarantine')}
        self.run_once(source='replay')
        after = {table: self.store.connection.execute(
            f'SELECT count(*) FROM {table}').fetchone()[0]
            for table in before}
        self.assertEqual(before, after)

    def test_replay_keeps_rejection_records(self):
        self.run_once()
        first = self.store.rejections()
        self.run_once(source='replay')
        second = self.store.rejections()
        self.assertEqual(first, second)

    def test_interrupted_then_resumed_matches_full_run(self):
        # 先处理前一半，再补后一半，最终行数与一次全量一致；
        # 已处理的那一半在续跑时不会重复入库。
        half = len(self.entries) // 2
        run_id = self.store.start_run('partial-run')
        first = process_mailbox(self.entries[:half], self.limits, self.store,
                                'partial-run', run_id)
        self.store.finish_run(run_id, first['totals'])
        rows_after_first = self.store.connection.execute(
            'SELECT count(*) FROM observations').fetchone()[0]

        _, resumed_report = self.run_once(source='resumed')
        rows_after_resume = self.store.connection.execute(
            'SELECT count(*) FROM observations').fetchone()[0]

        with tempfile.TemporaryDirectory() as other_dir:
            other = EvidenceStore(Path(other_dir) / 'o.sqlite3')
            rid = other.start_run('full')
            process_mailbox(self.entries, self.limits, other, 'full', rid)
            expected = other.connection.execute(
                'SELECT count(*) FROM observations').fetchone()[0]
            other.close()

        self.assertEqual(rows_after_resume, expected)
        # 续跑报告里前半封邮件全部命中重放短路，未新增任何行。
        replayed = resumed_report['messages'][:half]
        self.assertTrue(all('重放' in (m['detail'] or '') for m in replayed))
        self.assertEqual(rows_after_first, rows_after_resume
                         - sum(m['rows_new'] for m in resumed_report['messages'][half:]))


class SupersessionTests(PipelineTestBase):
    def test_correction_supersedes_without_overwriting(self):
        self.run_once()
        key = '供应方:example.invalid::id=20'
        history = self.store.row_history(key)
        statuses = [version['status'] for version in history]
        self.assertEqual(sorted(statuses), ['current', 'superseded'])
        values = {version['row']['value'] for version in history}
        self.assertEqual(values, {'100', '108'})

    def test_original_evidence_lineage_intact(self):
        self.run_once()
        key = '供应方:example.invalid::id=20'
        old = next(v for v in self.store.row_history(key)
                   if v['status'] == 'superseded')
        lineage = self.store.lineage(key, old['payload_digest'])
        self.assertTrue(any('correction-v1' in link['message_id']
                            for link in lineage))

    def test_current_version_has_two_sources_after_forward(self):
        self.run_once()
        key = '供应方:example.invalid::id=20'
        current = next(v for v in self.store.row_history(key)
                       if v['status'] == 'current')
        lineage = self.store.lineage(key, current['payload_digest'])
        message_ids = {link['message_id'] for link in lineage}
        self.assertIn('correction-v2@mail.example.invalid', message_ids)
        self.assertIn('forward-copy@mail.example.invalid', message_ids)


class ProvenanceChainTests(PipelineTestBase):
    def test_nested_member_path_is_queryable(self):
        self.run_once()
        row = self.store.connection.execute(
            "SELECT member_path, depth FROM members WHERE member_name='station3.csv'"
        ).fetchone()
        self.assertIsNotNone(row)
        path = json.loads(row[0])
        self.assertEqual(path, ['外层.zip', '内层.zip', 'station3.csv'])
        self.assertEqual(row[1], 2)

    def test_duplicate_attachment_names_distinguished_by_part(self):
        self.run_once()
        rows = self.store.connection.execute(
            "SELECT p.location FROM parts p JOIN messages m ON m.digest=p.message_digest "
            "WHERE m.message_id LIKE 'encoded-names%' ORDER BY p.location").fetchall()
        self.assertEqual([r[0] for r in rows], ['[1]', '[2]'])

    def test_partial_message_marked_reassembled(self):
        self.run_once()
        row = self.store.connection.execute(
            "SELECT is_reassembled FROM messages WHERE message_id LIKE 'partial-msg%'"
        ).fetchone()
        self.assertEqual(row[0], 1)


if __name__ == '__main__':
    unittest.main()
