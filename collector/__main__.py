import argparse
import json
from pathlib import Path
import tempfile

from .domain import collect
from .evidence import EvidenceStore
from .pipeline import process_mailbox
from .storage import Store
from .synth import build_mailbox
from .transport import Client, replay_server


def _load_mailbox(input_path):
    document = json.loads(Path(input_path).read_text(encoding='utf-8'))
    return build_mailbox(document)


def main():
    parser = argparse.ArgumentParser(description='本地采集与凭据查看')
    parser.add_argument('command',
                        choices=['demo', 'import', 'show', 'serve',
                                 'mailbox', 'report', 'lineage'])
    parser.add_argument('--input', default='fixtures/sample.json')
    parser.add_argument('--messages', default='fixtures/messages.json')
    parser.add_argument('--db', default='var/records.sqlite3')
    parser.add_argument('--port', type=int, default=8080)
    parser.add_argument('--row-key', help='lineage 命令使用的业务行键')
    arguments = parser.parse_args()

    if arguments.command == 'serve':
        from .api import serve
        serve(arguments.db, arguments.port)
        return

    if arguments.command == 'mailbox':
        Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
        limits, entries = _load_mailbox(arguments.messages)
        store = EvidenceStore(arguments.db)
        try:
            run_id = store.start_run(str(Path(arguments.messages)))
            report = process_mailbox(entries, limits, store,
                                     str(Path(arguments.messages)), run_id)
            store.finish_run(run_id, report['totals'])
        finally:
            store.close()
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    if arguments.command == 'report':
        store = EvidenceStore(arguments.db)
        try:
            latest = store.connection.execute(
                'SELECT id, source, summary FROM runs ORDER BY id DESC LIMIT 1'
            ).fetchone()
            if latest is None:
                print(json.dumps(dict(message='尚无处理记录'), ensure_ascii=False))
            else:
                deliveries = [dict(row) for row in store.connection.execute(
                    'SELECT message_digest, status, detail FROM deliveries '
                    'WHERE run_id=? ORDER BY message_digest', (latest['id'],))]
                for item in deliveries:
                    item['detail'] = json.loads(item['detail']) if item['detail'] else None
                print(json.dumps(
                    dict(run_id=latest['id'], source=latest['source'],
                         summary=json.loads(latest['summary']),
                         deliveries=deliveries,
                         rejections=store.rejections(latest['id'])),
                    ensure_ascii=False, indent=2))
        finally:
            store.close()
        return

    if arguments.command == 'lineage':
        if not arguments.row_key:
            raise SystemExit('lineage 需要 --row-key 参数')
        store = EvidenceStore(arguments.db)
        try:
            history = store.row_history(arguments.row_key)
            if not history:
                raise SystemExit(f'未找到业务行: {arguments.row_key}')
            result = []
            for version in history:
                digest = version['payload_digest']
                result.append(dict(version=version,
                                   lineage=store.lineage(arguments.row_key, digest)))
            print(json.dumps(result, ensure_ascii=False, indent=2))
        finally:
            store.close()
        return

    if arguments.command == 'demo':
        with tempfile.TemporaryDirectory() as directory:
            payload = Path(arguments.input).read_bytes()
            with replay_server({'/source': [dict(body=payload.decode('utf-8'))]}) as (base, calls):
                status, headers, raw = Client().request(base + '/source')
                if status != 200:
                    raise RuntimeError('本地来源未返回成功响应')
                store = Store(Path(directory) / 'demo.sqlite3')
                rows = collect(json.loads(raw))
                digest = store.ingest('本地演示来源', raw, rows)
                print(json.dumps(dict(digest=digest, records=store.records(), requests=len(calls)), ensure_ascii=False, indent=2))
                store.close()
        return

    Path(arguments.db).parent.mkdir(parents=True, exist_ok=True)
    store = Store(arguments.db)
    try:
        if arguments.command == 'import':
            raw = Path(arguments.input).read_bytes()
            store.ingest(str(Path(arguments.input)), raw, collect(json.loads(raw)))
        print(json.dumps(store.records(), ensure_ascii=False, indent=2))
    finally:
        store.close()


if __name__ == '__main__':
    main()
