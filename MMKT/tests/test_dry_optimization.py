"""BSR cache-aware cutoff and durable small records, through the real DRY CLI."""
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import test_dry_listing_phases as phase_fixture
from test_dry_tail_confirmation import block
from dry import checkpoint, database, run
from dry.detail import CollectionError


class OptimizationTests(unittest.TestCase):
    def execute(self, root, browser, options=()):
        return phase_fixture.ListingPhaseTests().execute(root, browser, options)

    def browser(self):
        return phase_fixture.TimelineBrowser({('main', 1): block((1, 2), 3, more=True),
            ('main', 2): block((3,)), ('main', 3): block((3,)),
            ('bsr', 1): block((2, 1), 4, more=True),
            ('bsr', 2): block((3, 4)), ('bsr', 3): block((3, 4))})

    def test_stop_bsr_after_confirmed_target_without_reading_later_pages(self):
        browser = self.browser()
        with tempfile.TemporaryDirectory() as directory:
            code, manifest, db, _, events = self.execute(Path(directory), browser,
                ('--target', '3', '--bsr-target', '2'))
            self.assertEqual(0, code)
            self.assertEqual([('bsr', 1)], [p for p in browser.requests if p[0]=='bsr'])
            self.assertEqual('confirmed_candidates', manifest['bsr']['listing_stop_reason'])
            self.assertEqual('target_reached', manifest['bsr']['stop_reason'])
            self.assertEqual(6, len(browser.calls))
            self.assertEqual(3, len(db.call_args.args[1]))

    def test_small_journal_preserves_each_detail_without_per_item_full_files(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            code, manifest, _, _, _ = self.execute(root, self.browser(),
                ('--target', '3', '--bsr-target', '2'))
            self.assertEqual(0, code)
            files=list((root/'output/intermediate'/manifest['batch_id']).glob('*'))
            self.assertEqual(2, len([p for p in files if p.suffix=='.jsonl']))
            self.assertEqual(2, len([p for p in files if '_detail_' in p.name]))
            path=root/'output/intermediate'/manifest['batch_id']/'main_journal.jsonl'
            records=[json.loads(line) for line in path.read_text(encoding='utf-8').splitlines()]
            detail=[r for r in records if r['kind']=='detail']
            self.assertEqual(['1','2','3'],[r['result']['item'] for r in detail])
            self.assertTrue(all('rows' not in r and 'progress' not in r for r in detail))

    def test_cached_excluded_product_is_not_counted_and_more_blocks_are_read(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,2,3,4,5),5),
            ('bsr',1):block((2,1),6,more=True), ('bsr',2):block((3,4),6,more=True)},
            detail_types={'2':'Waschtrockner'})
        with tempfile.TemporaryDirectory() as directory:
            code,m,db,_,_=self.execute(Path(directory),browser,('--bsr-target','3'))
            self.assertEqual(0,code)
            self.assertEqual([('bsr',1),('bsr',2)],[x for x in browser.requests if x[0]=='bsr'])
            self.assertEqual((3,1,0),(m['bsr']['cached_eligible_candidates'],
                m['bsr']['cached_excluded_candidates'],m['bsr']['unconfirmed_candidates']))
            self.assertEqual(9,m['api_calls'])
            rows=sorted((x for x in db.call_args.args[1] if x['bsr_rank']),key=lambda x:int(x['bsr_rank']))
            self.assertEqual(['1','3','4'],[x['item'] for x in rows])

    def test_unknown_before_known_candidates_keeps_rank_and_api_waits_for_cutoff(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,2,3),3),
            ('bsr',1):block((9,1),6,more=True),('bsr',2):block((2,3),6,more=True)})
        with tempfile.TemporaryDirectory() as directory:
            code,m,db,_,events=self.execute(Path(directory),browser,('--bsr-target','3'))
            self.assertEqual(0,code)
            self.assertEqual((3,1,2,2),(m['bsr']['cached_eligible_candidates'],
                m['bsr']['unconfirmed_candidates'],m['bsr']['cache_hits'],m['bsr']['new_api_calls']))
            self.assertLess(browser.timeline.index(('listing','bsr',2)),
                            browser.timeline.index(('api','9','GetComparisonTableRecommendations')))
            rows=sorted((x for x in db.call_args.args[1] if x['bsr_rank']),key=lambda x:int(x['bsr_rank']))
            self.assertEqual(['9','1','2'],[x['item'] for x in rows])
            self.assertEqual(8,m['api_calls'])
            self.assertIn('Main 상세 결과로 BSR 목표 확보 가능',
                (Path(directory)/'output/mmkt_dry_email_report.txt').read_text(encoding='utf-8'))

    def test_failing_unknown_does_not_consume_guaranteed_target(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,2,3),3),
            ('bsr',1):block((9,1),6,more=True),('bsr',2):block((2,3),6,more=True)})
        gql=browser.gql
        def call(op,variables):
            if variables.get('ref')=='9':return {'status':404}
            return gql(op,variables)
        browser.gql=call
        with tempfile.TemporaryDirectory() as directory:
            code,m,db,_,_=self.execute(Path(directory),browser,('--bsr-target','3','--page-retries','0'))
            self.assertEqual((1,3,'partial',1),(code,m['bsr']['rows'],m['collection_status'],m['product_failure_count']))
            self.assertEqual('target_reached',m['bsr']['stop_reason'])
            self.assertEqual(3,len(db.call_args.args[1]))
            self.assertFalse(m['notification']['success_report'])

    def test_total_change_does_not_require_a_tail_proof_after_target_is_confirmed(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,2,3,4),4),
            ('bsr',1):block((1,2),9,more=True),('bsr',2):block((3,4),3,more=False)})
        with tempfile.TemporaryDirectory() as directory:
            code,m,_,_,events=self.execute(Path(directory),browser,('--bsr-target','3'))
            self.assertEqual(0,code)
            self.assertEqual('confirmed_candidates',m['bsr']['listing_stop_reason'])
            self.assertFalse(any(e['event']=='tail_confirmation_read' and e.get('sort')=='bsr' for e in events))

    def test_unknown_cache_count_cannot_hide_a_real_listing_failure(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,),1),
            ('bsr',1):block((1,9),5,more=True),('bsr',2):'<html>access denied</html>'})
        with tempfile.TemporaryDirectory() as directory:
            code,m,db,_,_=self.execute(Path(directory),browser,('--bsr-target','2','--page-retries','1'))
            self.assertEqual(1,code)
            self.assertEqual(('complete',1,'failed','not_started'),
                (m['main']['state'],m['main']['rows'],m['bsr']['listing_state'],m['bsr']['detail_state']))
            self.assertEqual(2,browser.counts['bsr',2])
            self.assertEqual(2,len(browser.calls))
            self.assertEqual(2,m['bsr']['candidate_count'])
            db.assert_not_called()
            self.assertTrue(Path(m['main']['candidate_path']).exists())

    def test_early_cutoff_shortfall_is_not_reported_as_last_page(self):
        browser=phase_fixture.TimelineBrowser({('main',1):block((1,2),2),
            ('bsr',1):block((1,2),8,more=True)},detail_types={'2':'Waschtrockner'})
        with tempfile.TemporaryDirectory() as directory,patch.object(run,'cached_candidate_state',return_value='eligible'):
            code,m,db,_,_=self.execute(Path(directory),browser,('--bsr-target','2'))
            self.assertEqual(1,code)
            self.assertEqual('CONFIRMED_CANDIDATES_INSUFFICIENT',m['error'])
            self.assertEqual('failed',m['bsr']['detail_state'])
            self.assertEqual(1,m['bsr']['rows'])
            db.assert_not_called()

    def test_missing_rating_statistics_is_not_successful_eligible_cache(self):
        row={'sku_id':'1','retailer_sku_name':'Trockner'}
        self.assertEqual('unconfirmed',run.cached_candidate_state({'1':{
            '_features':{'Produkttyp':'Wärmepumpentrockner'},'_detail_name':'Trockner'}},row))
        valid={'_features':{},'_detail_name':'Trockner','star_rating':None,
               'count_of_reviews':None,'count_of_star_ratings':None}
        self.assertEqual('eligible',run.cached_candidate_state({'1':valid},row))
        self.assertEqual('excluded',run.cached_candidate_state({'1':valid},dict(row,retailer_sku_name='Portabel Trockner')))

    def test_completed_detail_snapshot_failure_preserves_all_committed_detail_records(self):
        save=checkpoint.write_manifest
        def fail(path,payload):
            if payload.get('phase')=='detail':raise OSError('SYNTHETIC_FAILURE')
            save(path,payload)
        with tempfile.TemporaryDirectory() as directory,patch.object(checkpoint,'write_manifest',side_effect=fail):
            code,m,db,_,_=self.execute(Path(directory),self.browser())
            self.assertEqual((1,'INTERMEDIATE_SAVE_FAILED'),(code,m['error']))
            journal=checkpoint.read_journal(m['intermediate_journals']['main']['path'])
            self.assertEqual(['1','2','3'],[r['result']['item'] for r in journal['records'] if r['kind']=='detail'])
            self.assertEqual('failed',m['main']['detail_state'])
            db.assert_not_called()


class JournalTests(unittest.TestCase):
    def journal(self,root):
        manifest={'batch_id':'m_synthetic','output_rows':0}
        return checkpoint.IntermediateJournal(root,manifest,'main'),manifest

    def test_unterminated_final_record_is_not_committed_even_when_json_is_valid(self):
        with tempfile.TemporaryDirectory() as directory:
            journal,_=self.journal(Path(directory))
            journal.append('detail',result={'item':'1','values':{'capacity':'8kg'}})
            journal.append('detail',result={'item':'2'})
            journal.path.write_bytes(journal.path.read_bytes().rstrip(b'\n'))
            result=checkpoint.read_journal(journal.path)
            self.assertEqual('incomplete_final_record',result['issue'])
            self.assertEqual(['1'],[r['result']['item'] for r in result['records']])
            self.assertFalse(result['loadable'])
            with self.assertRaisesRegex(CollectionError,'INTERMEDIATE_SAVE_FAILED'):journal.verify()

    def test_invalid_interior_record_does_not_skip_to_later_success(self):
        with tempfile.TemporaryDirectory() as directory:
            journal,_=self.journal(Path(directory))
            journal.append('detail',result={'item':'1'})
            first=journal.path.read_bytes()
            journal.append('detail',result={'item':'2'})
            second=journal.path.read_bytes()[len(first):]
            journal.path.write_bytes(first+b'not json\n'+second)
            result=checkpoint.read_journal(journal.path)
            self.assertEqual(('invalid_record',1),(result['issue'],len(result['records'])))

    def test_partial_write_preserves_prior_record_and_marks_storage_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            journal,m=self.journal(Path(directory))
            journal.append('detail',result={'item':'1'})
            def broken(line):
                with journal.path.open('ab') as f:f.write(line[:8])
                raise OSError('SYNTHETIC_DISK_FAILURE')
            with patch.object(journal,'_write_line',side_effect=broken):
                with self.assertRaisesRegex(CollectionError,'INTERMEDIATE_SAVE_FAILED'):
                    journal.append('detail',result={'item':'2'})
            result=checkpoint.read_journal(journal.path)
            self.assertEqual((1,'incomplete_final_record'),(len(result['records']),result['issue']))
            self.assertEqual('failed',m['intermediate_journals']['main']['state'])
            with self.assertRaises(CollectionError):journal.append('failure',error='stop')

    def test_existing_batch_journal_is_never_reused_or_overwritten(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);journal,_=self.journal(root)
            journal.append('detail',result={'item':'1'})
            before=journal.path.read_bytes()
            other,_=self.journal(root)
            with self.assertRaises(CollectionError):other.append('detail',result={'item':'2'})
            self.assertEqual(before,journal.path.read_bytes())

    def test_journal_cannot_be_loaded_as_final_output(self):
        with tempfile.TemporaryDirectory() as directory:
            journal,_=self.journal(Path(directory))
            journal.append('detail',result={'item':'1'})
            with self.assertRaises(CollectionError):database.load_validated_output(journal.path)

    def test_existing_snapshot_is_preserved_on_repeated_name(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);_,manifest=self.journal(root)
            path=checkpoint.save_checkpoint(root,manifest,[],sort='main',page=1,attempt=1,phase='failure')
            before=path.read_bytes()
            with self.assertRaisesRegex(CollectionError,'INTERMEDIATE_SAVE_FAILED'):
                checkpoint.save_checkpoint(root,manifest,[],sort='main',page=1,attempt=1,phase='failure')
            self.assertEqual(before,path.read_bytes())

    def test_malformed_record_identity_is_reported_without_an_unhandled_type_error(self):
        for field in ('kind','batch_id','sort','sequence'):
            with self.subTest(field=field),tempfile.TemporaryDirectory() as directory:
                journal,_=self.journal(Path(directory))
                journal.append('detail',result={'item':'1'})
                value=json.loads(journal.path.read_bytes())
                value[field]=[]
                journal.path.write_text(json.dumps(value)+'\n',encoding='utf-8')
                result=checkpoint.read_journal(journal.path)
                self.assertFalse(result['complete'])
                self.assertEqual([],result['records'])


if __name__=='__main__':
    unittest.main()
