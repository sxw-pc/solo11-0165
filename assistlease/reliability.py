from collections import defaultdict
from datetime import date


def report(db):
    """Simple model-level field exposure and observed fault report."""
    models = defaultdict(lambda: {'units': set(), 'loan_days': 0, 'faults': 0})
    for row in db.execute('SELECT unit,model FROM asset_unit ORDER BY event_id'):
        models[row['model']]['units'].add(row['unit'])
    for row in db.execute("SELECT a.model,i.unit FROM inspection i JOIN asset_unit a ON a.unit=i.unit WHERE i.state='fault'"):
        models[row['model']]['faults'] += 1
    for row in db.execute("""SELECT a.model,d.at AS started,r.at AS ended
                             FROM rental_decision x
                             JOIN rental_term t ON t.request_id=x.request_id
                             JOIN asset_unit a ON a.unit=t.unit
                             JOIN delivery d ON d.request_id=x.request_id AND d.unit=t.unit
                             JOIN return_receipt r ON r.request_id=x.request_id AND r.unit=t.unit
                             WHERE x.state='closed'"""):
        models[row['model']]['loan_days'] += max(0, (date.fromisoformat(row['ended'][:10]) - date.fromisoformat(row['started'][:10])).days)
    return [{'model': model, 'units': len(item['units']), 'loan_days': item['loan_days'],
             'faults': item['faults']} for model, item in sorted(models.items())]
