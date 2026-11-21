import argparse
import json
from pathlib import Path
from . import fit,asset,rental,lab
from .source import file_record
from .store import connect
from .report import report,dumps
def run_import(db,command,raw):
    data=json.loads(raw);file_record(db,command,raw)
    return {'fit-import':fit.import_data,'asset-import':asset.import_data,'rental-import':rental.import_data,
            'lab-import':lab.import_data}[command](db,data)
def audit(db):
    restored=connect(':memory:')
    try:
        rows=db.execute('SELECT kind,raw FROM file_journal ORDER BY id').fetchall()
        for row in rows:run_import(restored,row['kind'],row['raw'])
        if report(db)!=report(restored):raise ValueError('source replay disagrees with decisions')
        return {'status':'matched','source_files':len(rows)}
    finally:restored.close()
def main(argv=None):
    p=argparse.ArgumentParser(prog='assistlease');p.add_argument('command',choices=['fit-import','asset-import','rental-import','lab-import','report','audit','demo'])
    p.add_argument('--db',required=True);p.add_argument('--file');a=p.parse_args(argv);db=connect(a.db)
    try:
        if a.command=='report':print(dumps(db));return
        if a.command=='audit':print(json.dumps(audit(db),ensure_ascii=False));return
        if a.command=='demo':
            root=Path(__file__).parent.parent/'fixtures'
            for command,name in [('fit-import','fit_ok.json'),('asset-import','asset_ok.json'),('rental-import','rental_ok.json')]:run_import(db,command,(root/name).read_bytes())
            print(dumps(db));return
        if not a.file:p.error('--file required')
        print(json.dumps(run_import(db,a.command,Path(a.file).read_bytes()),ensure_ascii=False,indent=2))
    finally:db.close()
if __name__=='__main__':main()
