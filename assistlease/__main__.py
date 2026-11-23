import argparse
import json
from pathlib import Path

from . import fit, asset, rental, lab
from .source import file_record
from .store import connect
from .report import report, dumps, process_report

COMMANDS = {
    'fit-import': fit.import_data,
    'asset-import': asset.import_data,
    'rental-import': rental.import_data,
    'lab-import': lab.import_data,
}


def run_import(db, command, raw):
    data = json.loads(raw)
    file_record(db, command, raw)
    return COMMANDS[command](db, data)


def audit(db):
    restored = connect(':memory:')
    try:
        rows = db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:
            run_import(restored, row['kind'], row['raw'])
        if report(db) != report(restored):
            raise ValueError('source replay disagrees with decisions')
        return {'status': 'matched', 'source_files': len(rows)}
    finally:
        restored.close()


def main(argv=None):
    parser = argparse.ArgumentParser(prog='assistlease')
    parser.add_argument('command', choices=[*COMMANDS, 'report', 'process-report', 'audit', 'demo'])
    parser.add_argument('--db', required=True)
    parser.add_argument('--file')
    parser.add_argument('--process', help='sanitation event_id for process-report')
    args = parser.parse_args(argv)
    db = connect(args.db)
    try:
        if args.command == 'report':
            print(dumps(db))
            return
        if args.command == 'process-report':
            if not args.process:
                parser.error('--process required')
            item = process_report(db, args.process)
            if item is None:
                parser.error('unknown sanitation process: ' + args.process)
            print(json.dumps(item, ensure_ascii=False, indent=2))
            return
        if args.command == 'audit':
            print(json.dumps(audit(db), ensure_ascii=False))
            return
        if args.command == 'demo':
            root = Path(__file__).parent.parent / 'fixtures'
            for command, name in [('fit-import', 'fit_ok.json'), ('asset-import', 'asset_ok.json'),
                                  ('rental-import', 'rental_ok.json'), ('lab-import', 'lab_case.json')]:
                run_import(db, command, (root / name).read_bytes())
            print(dumps(db))
            return
        if not args.file:
            parser.error('--file required')
        print(json.dumps(run_import(db, args.command, Path(args.file).read_bytes()),
                         ensure_ascii=False, indent=2))
    finally:
        db.close()


if __name__ == '__main__':
    main()
