import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
import assistlease
from assistlease.__main__ import run_import,audit
from assistlease import credential_crawl
from assistlease.store import connect
from assistlease.report import report
ROOT=Path(__file__).resolve().parents[1]
FIX=ROOT/'fixtures'
def fixture(name):return json.loads((FIX/name).read_text(encoding='utf8'))
def feed(db,kind,data):return run_import(db,kind,json.dumps(data,ensure_ascii=False).encode())

class CredentialCase(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.db=connect(Path(self.tmp.name)/'state.sqlite')
        self.assertEqual(Path(assistlease.__file__).resolve().parent,ROOT/'assistlease')
    def tearDown(self):self.db.close();self.tmp.cleanup()
    def crawl(self,site='credential_site'):
        return run_import(self.db,'credential-crawl',credential_crawl.bundle_bytes(FIX/site))
    def roster(self):feed(self.db,'credential-import',fixture('credential_roster.json'))
    def fit_credential(self):feed(self.db,'fit-import',fixture('fit_credential.json'))
    def attr(self,eid):
        row=self.db.execute('SELECT * FROM opinion_attribution WHERE opinion_event_id=?',(eid,)).fetchone()
        return dict(row) if row else None
    def fit_state(self,rid):return dict(self.db.execute('SELECT * FROM fit_decision WHERE request_id=?',(rid,)).fetchone())
    def binding(self,cert,pid):return dict(self.db.execute('SELECT * FROM credential_state WHERE binding=?',(f'{cert}|{pid}',)).fetchone())
    def write_site(self,name,notices,version=3,crawled='2026-11-05T00:00:00'):
        d=Path(self.tmp.name)/name;d.mkdir()
        (d/'p1.json').write_text(json.dumps({'page_version':version,'notices':notices}),encoding='utf8')
        (d/'manifest.json').write_text(json.dumps({'pages':[{'file':'p1.json','url':'https://registry.example.local/x','cursor':'page=1','crawled_at':crawled}]}),encoding='utf8')
        return d

    def test_pagination_duplicate_and_cache_preserved(self):
        result=self.crawl()
        self.assertEqual(result['pages_crawled'],3)
        # pagination duplicate: notice-t1 appears on two pages, stored once, linked to both
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM credential_notice WHERE event_id=?',('notice-t1',)).fetchone()[0],1)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM notice_page WHERE event_id=?',('notice-t1',)).fetchone()[0],2)
        # raw bytes, source URL, cursor and crawl moment preserved per page
        page=dict(self.db.execute("SELECT * FROM crawl_page WHERE cursor='page=2'").fetchone())
        self.assertEqual(page['source_url'],'https://registry.example.local/disclosures?page=2')
        self.assertEqual(page['crawled_at'],'2026-11-03T09:01:00')
        self.assertEqual(page['page_version'],3)
        self.assertTrue(bytes(page['raw']).startswith(b'<!DOCTYPE html>'))
        # late-crawled old cache must not replace the effective revocation
        cache=dict(self.db.execute('SELECT * FROM crawl_page WHERE page_version=2').fetchone())
        self.assertGreater(cache['crawled_at'],page['crawled_at'])
        revoked=self.binding('CERT-FIT-004','T-4')
        self.assertEqual((revoked['state'],revoked['page_version']),('revoked',3))
        notice=dict(self.db.execute("SELECT * FROM credential_notice WHERE event_id='notice-t4b'").fetchone())
        self.assertEqual(notice['revoked_at'],'2026-10-15T00:00:00')

    def test_same_name_not_merged_and_no_autopick(self):
        self.crawl();self.roster()
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        names=self.db.execute("SELECT DISTINCT person_id FROM credential_notice WHERE name='therapist-a'").fetchall()
        self.assertEqual(sorted(r[0] for r in names),['T-1','T-2'])
        # name-only roster entry must not be resolved by name, first hit or latest page
        attr=self.attr('opinion-1')
        self.assertEqual((attr['state'],attr['person_id']),('unattributed',None))
        self.assertEqual(self.fit_state('req-1')['state'],'pending')

    def test_route_device_witness_attributes_t1(self):
        self.crawl();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_device.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        attr=self.attr('opinion-1')
        self.assertEqual((attr['state'],attr['person_id'],attr['cert_no'],attr['credential_state']),
                         ('attributed','T-1','CERT-FIT-001','valid'))
        self.assertEqual(attr['route'],'device_witness_log')
        self.assertEqual(self.fit_state('req-1')['state'],'approved')
        item=[o for o in report(self.db)['opinions'] if o['opinion_event_id']=='opinion-1'][0]
        self.assertEqual((item['signer']['person_id'],item['signer']['name']),('T-1','therapist-a'))
        self.assertEqual(item['credential']['state'],'valid')
        self.assertEqual(item['credential']['checked_at'],'2026-11-01T10:00:00')
        self.assertTrue(item['consumable']);self.assertFalse(item['needs_reevaluation'])
        self.assertEqual(item['conflicts'],[])
        self.assertEqual({p['cursor'] for p in item['pages']},{'page=1','page=2'})

    def test_route_roster_original_attributes_t2(self):
        self.crawl();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_roster.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        attr=self.attr('opinion-1')
        self.assertEqual((attr['state'],attr['person_id'],attr['cert_no'],attr['credential_state']),
                         ('attributed','T-2','CERT-TRN-002','invalid'))
        self.assertEqual(attr['route'],'roster_original')
        self.assertEqual(attr['reason'],'practice scope does not cover device fit signing')
        decision=self.fit_state('req-1')
        self.assertEqual((decision['state'],decision['reason']),
                         ('blocked','fit opinion signer credential invalid; independent re-evaluation required'))
        self.assertEqual(report(self.db)['reevaluation'],['opinion-1'])

    def test_conflicting_routes_not_merged(self):
        self.crawl();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_device.json'))
        feed(self.db,'credential-import',fixture('credential_attest_roster.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        attr=self.attr('opinion-1')
        self.assertEqual(attr['state'],'conflict')
        self.assertIn('T-1',attr['reason']);self.assertIn('T-2',attr['reason'])
        self.assertEqual(self.fit_state('req-1')['state'],'blocked')
        item=[o for o in report(self.db)['opinions'] if o['opinion_event_id']=='opinion-1'][0]
        self.assertTrue(item['conflicts']);self.assertFalse(item['consumable'])

    def test_wrong_person_number_rejected(self):
        self.crawl();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_wrong.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'))
        attr=self.attr('opinion-1')
        self.assertEqual((attr['state'],attr['person_id']),('invalid','T-99'))
        self.assertNotEqual(self.fit_state('req-1')['state'],'approved')

    def test_attestation_requires_two_witnesses_and_known_route(self):
        base={'event_id':'attest-x','opinion_event_id':'opinion-1','person_id':'T-1','at':'2026-11-03T10:00:00'}
        with self.assertRaisesRegex(ValueError,'two witnesses'):
            feed(self.db,'credential-import',{'attestations':[dict(base,route='device_witness_log',witnesses=['only-one'])]})
        with self.assertRaisesRegex(ValueError,'unknown attestation route'):
            feed(self.db,'credential-import',{'attestations':[dict(base,route=' hearsay ',witnesses=['a','b'])]})

    def test_late_revocation_changes_decision(self):
        self.crawl('credential_site_early');self.roster();self.fit_credential()
        # before the revocation disclosure is crawled, T-4 is active and req-c1 approved
        self.assertEqual(self.binding('CERT-FIT-004','T-4')['state'],'active')
        self.assertEqual(self.fit_state('req-c1')['state'],'approved')
        self.crawl()
        revoked=self.binding('CERT-FIT-004','T-4')
        self.assertEqual((revoked['state'],revoked['page_version']),('revoked',3))
        attr=self.attr('opinion-c1')
        self.assertEqual((attr['state'],attr['credential_state']),('attributed','invalid'))
        self.assertEqual(attr['reason'],'certificate revoked before signing')
        self.assertEqual(self.fit_state('req-c1')['state'],'blocked')
        self.assertIn('opinion-c1',report(self.db)['reevaluation'])
        # an opinion signed while the certificate was still effective stays valid
        self.assertEqual(self.attr('opinion-c4')['credential_state'],'valid')
        self.assertEqual(self.fit_state('req-c4')['state'],'approved')
        self.assertEqual(audit(self.db)['status'],'matched')

    def test_cert_quarantine_and_signed_correction(self):
        self.crawl();self.roster();self.fit_credential()
        # same certificate number disclosed for two person ids: both isolated
        self.assertEqual(self.binding('CERT-FIT-005','T-5')['state'],'quarantined')
        self.assertEqual(self.binding('CERT-FIT-005','T-6')['state'],'quarantined')
        self.assertEqual(self.attr('opinion-c2')['state'],'quarantined')
        self.assertEqual(self.attr('opinion-c3')['state'],'quarantined')
        self.assertEqual(self.fit_state('req-c2')['state'],'blocked')
        # a correction naming the wrong page version does not lift isolation
        feed(self.db,'credential-import',fixture('credential_correction_badversion.json'))
        self.assertEqual(self.attr('opinion-c2')['state'],'quarantined')
        # a signed correction matching the disclosure page version lifts only that binding
        feed(self.db,'credential-import',fixture('credential_correction.json'))
        self.assertEqual(self.binding('CERT-FIT-005','T-5')['state'],'confirmed')
        self.assertEqual(self.binding('CERT-FIT-005','T-6')['state'],'quarantined')
        attr=self.attr('opinion-c2')
        self.assertEqual((attr['state'],attr['credential_state']),('attributed','valid'))
        self.assertEqual(self.fit_state('req-c2')['state'],'approved')
        self.assertEqual(self.attr('opinion-c3')['state'],'quarantined')
        self.assertNotEqual(self.fit_state('req-c3')['state'],'approved')
        self.assertEqual(audit(self.db)['status'],'matched')

    def test_replay_idempotent_and_event_conflict(self):
        first=self.crawl();second=self.crawl()
        self.assertEqual(first,second)
        self.roster();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_device.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'));feed(self.db,'fit-import',fixture('fit_ok.json'))
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM crawl_page').fetchone()[0],3)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM duty_roster').fetchone()[0],7)
        self.assertEqual(self.db.execute('SELECT COUNT(*) FROM opinion_attribution').fetchone()[0],1)
        # same event id with different content is rejected, for notices and roster alike
        notice={'event_id':'notice-x','person_id':'T-1','name':'therapist-a','cert_no':'CERT-FIT-009',
                'scope':['fit'],'models':[],'effective_from':'2026-01-01T00:00:00','revoked_at':None}
        credential_crawl.crawl(self.db,self.write_site('site_a',[notice]))
        changed=dict(notice,scope=['training'])
        with self.assertRaisesRegex(ValueError,'event_id conflict'):
            credential_crawl.crawl(self.db,self.write_site('site_b',[changed]))
        roster_row=dict(fixture('credential_roster.json')['roster'][0],signed_at='2026-11-01T12:00:00')
        with self.assertRaisesRegex(ValueError,'event_id conflict'):
            feed(self.db,'credential-import',{'roster':[roster_row]})

    def test_audit_rebuilds_attribution_from_raw_pages(self):
        self.crawl();self.roster()
        feed(self.db,'credential-import',fixture('credential_attest_device.json'))
        feed(self.db,'fit-import',fixture('fit_ok.json'));self.fit_credential()
        self.assertEqual(audit(self.db)['status'],'matched')
        restored=connect(':memory:')
        try:
            for row in self.db.execute('SELECT kind,raw FROM file_journal ORDER BY id'):
                run_import(restored,row['kind'],row['raw'])
            for table in ('opinion_attribution','credential_state','crawl_page','duty_roster','attestation'):
                mine=[dict(r) for r in self.db.execute(f'SELECT * FROM {table} ORDER BY 1')]
                theirs=[dict(r) for r in restored.execute(f'SELECT * FROM {table} ORDER BY 1')]
                if table=='crawl_page':
                    mine=[{k:v for k,v in r.items() if k!='raw'} for r in mine]
                    theirs=[{k:v for k,v in r.items() if k!='raw'} for r in theirs]
                self.assertEqual(mine,theirs,table)
        finally:restored.close()

    def test_roster_links_each_opinion_event(self):
        self.crawl();self.roster();self.fit_credential()
        linked={r['opinion_event_id'] for r in self.db.execute('SELECT * FROM duty_roster WHERE signer_person_id IS NOT NULL')}
        for eid in ('opinion-c1','opinion-c2','opinion-c3','opinion-c4'):
            self.assertIn(eid,linked)
            self.assertEqual(self.attr(eid)['route'],'roster')
        # the name-only legacy entry stays unresolved until witnessed evidence arrives
        self.assertIsNone(self.attr('opinion-1'))

    def test_cli_credential_flow(self):
        db_path=Path(self.tmp.name)/'cli.sqlite'
        def cli(*args):
            return subprocess.run([sys.executable,'-B','-m','assistlease',*args,'--db',str(db_path)],
                                  cwd=ROOT,text=True,capture_output=True,check=True)
        cli('credential-crawl','--dir',str(FIX/'credential_site'))
        cli('credential-import','--file',str(FIX/'credential_roster.json'))
        cli('credential-import','--file',str(FIX/'credential_attest_device.json'))
        cli('fit-import','--file',str(FIX/'fit_ok.json'))
        decided=json.loads(cli('credential-decide').stdout)
        mine=[d for d in decided if d['opinion_event_id']=='opinion-1'][0]
        self.assertEqual((mine['state'],mine['person_id'],mine['credential_state']),('attributed','T-1','valid'))
        rep=json.loads(cli('report').stdout)
        item=[o for o in rep['opinions'] if o['opinion_event_id']=='opinion-1'][0]
        self.assertEqual(item['signer']['person_id'],'T-1')
        self.assertEqual(rep['requests'][0]['fit']['state'],'approved')
        self.assertEqual(json.loads(cli('audit').stdout)['status'],'matched')

if __name__=='__main__':unittest.main()
