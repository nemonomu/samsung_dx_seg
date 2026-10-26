"""Offline regressions for comparison omissions and explicit PDF/title evidence."""
import unittest
import io
import json
import tempfile
from pathlib import Path
from urllib.error import HTTPError
from unittest.mock import MagicMock, patch

import test_dry as base
from test_dry_fourth import parse_pages, AIMAX_TITLE
from dry import client, config, fields, pipeline, storage


def comparison_html(ids, values):
    headers = ''.join(f'<a data-variation-id="{v}" href="/p/dryer/?variationId={v}">'
                      f'<h3>Wäschetrockner DR{v[1:]}000</h3></a>' for v in ids)
    labels = list(dict.fromkeys(k for v in ids for k in values.get(v, {})))
    rows = []
    for label in labels:
        cells = ''.join(f'<div data-variation-id="{v}">{values[v][label]}</div>'
                        for v in reversed(ids) if label in values.get(v, {}))
        rows.append(f'<section><span>{label}</span><div class="pcp_content__column-list">{cells}</div></section>')
    return (headers + ''.join(rows)).encode()


class SixthPdfTests(base.Offline):
    def test_explicit_form_and_english_product_heading(self):
        for value in ('Form: Frontlader Farbe: Weiß', 'Front loaded TD', 'Top loaded TD'):
            with self.subTest(value=value):
                result, pages, _ = parse_pages([value, 'must not read'],
                    need_sku=False, need_capacity=False, need_loading=True)
                self.assertEqual(result.get('loading_type'), 'Top-loading' if value.startswith('Top') else 'Front loader')
                self.assertNotIn('Farbe', result['loading_type_raw'])
                pages[-1].extract_text.assert_not_called()

    def test_capacity_label_variations(self):
        for value, expected in (('Nennkapazität in kg 8,0', '8 kg'),
                                ('Nennkapazität Baumwollwäsche: 7,0 kg', '7 kg')):
            with self.subTest(value=value):
                result, pages, _ = parse_pages([value, 'skip'], need_sku=False, need_capacity=True)
                self.assertEqual(result.get('capacity'), expected)
                pages[-1].extract_text.assert_not_called()

    def test_range_is_not_truncated_and_next_candidate_is_checked(self):
        for value in ('Nennkapazität: 1 - 7 kg', 'Rated capacity 1–7 kg', 'Rated capacity 2 5 kg'):
            with self.subTest(value=value):
                result, _, log = parse_pages([value], need_sku=False, need_capacity=True)
                self.assertIsNone(result.get('capacity'))
                self.assertIn('capacity', result['_missing'])
                candidate = next(v for e, v in log.records if e == 'pdf_capacity_candidate')
                self.assertEqual(candidate['reason'], 'ambiguous_numeric_capacity')
                result, pages, _ = parse_pages([value + '; Rated capacity 2.5 kg', 'skip'],
                    need_sku=False, need_capacity=True)
                self.assertEqual(result['capacity'], '2.5 kg')
                pages[-1].extract_text.assert_not_called()

    def test_ranges_with_units_and_normal_numbers(self):
        for raw in ('1 - 7 kg', '1kg-7kg', '1 kg / 7 kg', '1 bis 7 kg', '1 to 7 kg'):
            with self.subTest(raw=raw):
                self.assertIsNone(fields.capacity(raw, labelled=True))
                self.assertIsNone(fields.capacity(raw))
                result, _, _ = parse_pages(['Rated capacity ' + raw], need_sku=False, need_capacity=True)
                self.assertIsNone(result.get('capacity'))
        for raw, expected in (('2.5 kg','2.5 kg'),('2,5 kg','2.5 kg'),('9 kg','9 kg'),
                               ('Rated capacity (a) (kg): 9','9 kg')):
            line = raw if raw.startswith('Rated') else 'Rated capacity ' + raw
            result, _, _ = parse_pages([line],need_sku=False,need_capacity=True)
            self.assertEqual(result['capacity'],expected)

    def test_instructions_negations_and_non_capacity_units_are_not_fields(self):
        for value in ('kompatibel mit Frontlader', 'Form: kein Frontlader',
                      'Form: Frontlader compatible with Waschmaschine', 'Open the front door',
                      'Not front loaded TD', 'Front loaded TD compatible with other products',
                      'Bauart: Frontlader und Toplader'):
            with self.subTest(value=value):
                result, _, _ = parse_pages([value],need_sku=False,need_capacity=False,need_loading=True)
                self.assertFalse(result.get('loading_type'))
        for value in ('Nennkapazität Waschen: 9 kg', 'Produktgewicht: 50 kg',
                      'Energieverbrauch: 1.47 kWh', 'Nennkapazität: 1.47 kWh'):
            result, _, _ = parse_pages([value],need_sku=False,need_capacity=True)
            self.assertFalse(result.get('capacity'))

    def test_rejected_range_continues_pages_and_preserves_other_fields(self):
        result, pages, _ = parse_pages([
            'Modellkennung: GOOD900\nForm: Frontlader Farbe: Weiß\nRated capacity 1 - 7 kg',
            'Nennkapazität in kg 8,0', 'skip'], need_sku=True,need_capacity=True,need_loading=True)
        self.assertEqual((result['sku'],result['capacity'],result['loading_type']),('GOOD900','8 kg','Front loader'))
        pages[-1].extract_text.assert_not_called()
        result, _, _ = parse_pages(['Modellkennung: GOOD900\nForm: Frontlader\nRated capacity 1 - 7 kg',
                                    ValueError('PRIVATE_TEXT')], need_sku=True,need_capacity=True,need_loading=True)
        self.assertEqual(result['sku'],'GOOD900')
        self.assertEqual(result['loading_type'],'Front loader')
        self.assertFalse(result.get('capacity'))
        self.assertNotIn('PRIVATE_TEXT',str(result))


class SixthSelectionTests(base.Offline):
    def test_explicit_portable_body_after_feature_clause(self):
        for title in (AIMAX_TITLE, 'Wäschetrockner DR900 mit Timer, Portabler Wäschetrockner',
                      'Wäschetrockner mit App; Tragbarer kleiner Wärmepumpentrockner'):
            with self.subTest(title=title):
                self.assertFalse(fields.classify(title)[0])

    def test_feature_and_compatibility_mentions_do_not_describe_the_body(self):
        for title in ('Wärmepumpentrockner DR900 mit tragbarer Fernbedienung',
                      'Wärmepumpentrockner DR900, portable App-Steuerung',
                      'Wärmepumpentrockner DR900, kompatibel mit tragbarem Wäschetrockner',
                      'Wäschetrockner DR900 mit Ersatzfilter für tragbare Wäschetrockner',
                      'Wäschetrockner DR900 mit nicht portablem Wäschetrockner',
                      'Wäschetrockner DR900 mit Timer, nicht tragbarer Wäschetrockner'):
            with self.subTest(title=title):
                self.assertTrue(fields.classify(title)[0])

    def test_portable_exclusion_refills_name_duplicates_without_detail_requests(self):
        titles = [AIMAX_TITLE,'Wäschetrockner DR900, mobile Fernsteuerung',
                  'Wäschetrockner DR900, mobile Fernsteuerung','Mini Wäschetrockner DR901']
        source = base.Source([[base.target(i,title) for i,title in enumerate(titles,1)]])
        run = dict(target=2,batch_id='test',calendar_week='w43',crawl_datetime='2026-10-26 00:00:00')
        rows = pipeline.collect(source,base.Log(),run,lambda rows:None)
        self.assertEqual([r['item'] for r in rows],['2','4'])
        self.assertEqual([r['main_rank'] for r in rows],[1,2])
        self.assertEqual([r['bsr_rank'] for r in rows],[1,2])
        self.assertEqual((run['counts']['excluded'],run['counts']['duplicates']),(1,1))
        self.assertNotIn('v1',[v for c in source.calls if c[0]=='tiles' for v in c[1]])
        self.assertEqual([c[1] for c in source.calls if c[0]=='rating'],['2','4'])


class SixthComparisonTests(base.Offline):
    def make_client(self, items, log=None):
        log = log or base.Log()
        c, source = client.Client(log), base.Source([items])
        for name in ('page', 'tiles', 'pdf', 'rating'):
            setattr(c, name, getattr(source, name))
        return c, source, log

    def run_pipeline(self, c, log, count):
        run = dict(target=count,batch_id='test',calendar_week='w43',crawl_datetime='2026-10-26 00:00:00')
        return pipeline.collect(c,log,run,lambda rows:None),run

    def test_pipeline_recovers_five_missing_options_in_two_group_requests(self):
        items = [base.target(i) for i in range(1, 11)]
        c, source, log = self.make_client(items)
        requests = []
        def fetch(url, endpoint, **kwargs):
            self.assertEqual(endpoint, 'comparison')
            ids = base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            requests.append(ids)
            values = {v: {'Modellbezeichnung': 'GOOD' + v[1:].zfill(4)} for v in ids}
            if ids[0] == 'v1':
                for v in ids: values[v]['Bauart'] = 'Toplader' if v == 'v7' else 'Frontlader'
            return comparison_html(ids, values), {'status': 200}
        run = dict(target=10, batch_id='test', calendar_week='w43', crawl_datetime='2026-10-26 00:00:00')
        with patch.object(c, 'fetch', side_effect=fetch):
            rows = pipeline.collect(c, log, run, lambda rows: None)
        self.assertEqual([r['loading_type'] for r in rows], ['Front loader'] * 6 + ['Top-loading'] + ['Front loader'] * 3)
        self.assertEqual(requests, [['v1','v2','v3','v4','v5'], ['v6','v7','v8','v9','v10'],
                                    ['v1','v6','v7','v8','v9'], ['v1','v10']])
        self.assertEqual([r['sku'] for r in rows], ['GOOD' + str(i).zfill(4) for i in range(1, 11)])
        self.assertEqual([r['capacity'] for r in rows], ['9 kg'] * 10)
        self.assertEqual([r['main_rank'] for r in rows], list(range(1, 11)))
        self.assertEqual([r['count_of_reviews'] for r in rows], [r['count_of_star_ratings'] for r in rows])
        self.assertFalse(any(call[0] == 'pdf' for call in source.calls))
        self.assertEqual(sum(call[0] == 'rating' for call in source.calls), 10)

    def test_presence_and_confirmed_title_values_do_not_trigger_supplements(self):
        items=[base.target(i) for i in range(1,11)]
        c,source,log=self.make_client(items)
        def fetch(url,endpoint,**kw):
            ids=base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            values={v:{'Modellbezeichnung':'GOOD900','Bauart':('Frontlader' if v=='v1' else '-')}
                    for v in ids}
            values['v7']={'Modellbezeichnung':'GOOD900','Bauart':''} if 'v7' in ids else values.get('v7',{})
            return comparison_html(ids,values),{'status':200}
        with patch.object(c,'fetch',side_effect=fetch) as request:
            rows,_=self.run_pipeline(c,log,10)
        self.assertEqual(request.call_count,2)
        self.assertIsNone(rows[6]['loading_type'])
        self.assertIn('Bauart',c.compare_cache['v7']['_present_labels'])
        self.assertFalse(any(e=='comparison_supplement_start' for e,_ in log.records))

    def test_reference_unavailable_and_already_confirmed_value_use_existing_flow(self):
        for name, expected_pdf in (('Wäschetrockner DR900, 9 kg',1),
                                    ('Wäschetrockner DR900, 9 kg, Frontlader',0)):
            c,source,log=self.make_client([base.target(1,name)])
            body=comparison_html(['v1'],{'v1':{'Modellbezeichnung':'GOOD900'}})
            if expected_pdf==0:body=body.replace(b'</h3>',b', Frontlader</h3>')
            with patch.object(c,'fetch',return_value=(body,{'status':200})) as request:
                rows,_=self.run_pipeline(c,log,1)
            self.assertEqual(request.call_count,1)
            self.assertEqual(sum(x[0]=='pdf' for x in source.calls),expected_pdf)

    def test_cached_reference_is_sent_and_normal_values_and_copy_boundaries_survive(self):
        c=client.Client(base.Log())
        c.compare_cache={'r':{'Bauart':'Frontlader','_present_labels':['Bauart']},
                         'v2':{'Modellbezeichnung':'LONG900 Active','Nennkapazität':'9 kg',
                               '_present_labels':['Modellbezeichnung','Nennkapazität']}}
        html=comparison_html(['r','v2'],{'r':{'Bauart':'Frontlader'},
             'v2':{'Bauart':'Toplader','Modellbezeichnung':'WRONG900','Nennkapazität':'1 kg'}})
        with patch.object(c,'fetch',return_value=(html,{'status':200})) as request, \
                patch.object(client,'comparison_names',side_effect=AssertionError('UNNEEDED_TITLE_PARSE')), \
                patch.object(client.compare,'_row_values',wraps=client.compare._row_values) as parsed:
            result=c.comparison(['r','v2'],supplement={'v2':['loading_type']})
            again=c.comparison(['r','v2'],supplement={'v2':['loading_type']})
            self.assertEqual(request.call_count,1)
            sent=base.parse_qs(base.urlsplit(request.call_args.args[0]).query)['variationIds'][0]
            self.assertEqual(sent,'r,v2')
            self.assertEqual([call.args[1] for call in parsed.call_args_list],['Beladung','Bauart'])
        self.assertEqual(result['v2']['Bauart'],'Toplader')
        self.assertEqual(result['v2']['Modellbezeichnung'],'LONG900 Active')
        self.assertEqual(result['v2']['Nennkapazität'],'9 kg')
        result['v2']['Bauart']='WRONG'
        self.assertEqual(again['v2']['Bauart'],'Toplader')
        self.assertEqual(c.compare_cache['r']['Bauart'],'Frontlader')

    def test_bounded_failed_supplement_keeps_values_and_request_error(self):
        log=base.Log();c=client.Client(log,pause=0)
        c.compare_cache={'r':{'Bauart':'Frontlader'},
                         'v2':{'Modellbezeichnung':'GOOD900','_present_labels':['Modellbezeichnung']}}
        with patch.object(client,'urlopen',side_effect=HTTPError('https://www.otto.de/',503,'unavailable',{},None)) as request, \
                patch.object(client.time,'sleep'):
            result=c.comparison(['r','v2'],supplement={'v2':['loading_type']})
            c.comparison(['r','v2'],supplement={'v2':['loading_type']})
        self.assertEqual(request.call_count,3)
        self.assertEqual(result['v2']['Modellbezeichnung'],'GOOD900')
        self.assertIn('_error',result['v2'])
        self.assertEqual([v['attempt'] for e,v in log.records if e=='request_start'],[1,2,3])

    def test_failure_in_recovery_is_counted_without_losing_completed_rows(self):
        c,source,log=self.make_client([base.target(i) for i in range(1,7)])
        def fetch(url,endpoint,**kw):
            ids=base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            if ids==['v1','v6']:raise client.RequestFailure('request_failed:comparison:timeout')
            values={v:{'Modellbezeichnung':'GOOD'+v[1:].zfill(4)} for v in ids}
            if ids[0]=='v1':
                for v in ids:values[v]['Bauart']='Frontlader'
            return comparison_html(ids,values),{'status':200}
        with patch.object(c,'fetch',side_effect=fetch):
            rows,run=self.run_pipeline(c,log,6)
        self.assertEqual(len(rows),6)
        self.assertEqual(rows[-1]['sku'],'GOOD0006')
        self.assertEqual(run['counts']['request_error_items'],1)
        self.assertTrue(any(e=='product_result' and v['request_error_items'] for e,v in log.records))


class FollowupCapacityTests(base.Offline):
    def test_model_separator_preserves_normal_capacity_and_title_priority(self):
        for title, expected in (
            ('Wärmepumpentrockner Serie 6 - 9 kg','9 kg'),
            ('BOMANN Wärmepumpentrockner WPT 7155 - 8 kg','8 kg'),
            ('Trockner Modell 1200 - 9 kg','9 kg')):
            with self.subTest(title=title):
                for labelled in (False,True):
                    self.assertEqual(fields.capacity(title,labelled=labelled),expected)
                target=base.target(1,title);target['top_infos']={'Kapazität Trocknen':'7 kg'}
                log=base.Log()
                self.assertEqual(fields.choose(target,{},events=log)['capacity'],expected)
                result=next(v for e,v in log.records if v['field']=='capacity')
                self.assertEqual(result['source'],'listing_title')
                self.assertTrue(result['conflict'])

    def test_model_word_does_not_allow_actual_range_or_weight(self):
        for raw in ('Trockner Modell DR900, 1 - 7 kg','Serie 6, 1-7 kg',
                    'Modell 1200, 1kg-7kg','Trockner DR900, 1 bis 7 kg',
                    'Rated capacity 2 5 kg','Produktgewicht 50 kg','Waschkapazität 8 kg'):
            for labelled in (False,True):
                with self.subTest(raw=raw,labelled=labelled):
                    self.assertIsNone(fields.capacity(raw,labelled=labelled))
        result,_,_=parse_pages(['Modellkennung: GOOD900\nNennkapazität: 1 - 7 kg\nRated capacity 2 5 kg',
                               'Nennkapazität in kg 8,0'],need_sku=True,need_capacity=True)
        self.assertEqual((result['sku'],result['capacity']),('GOOD900','8 kg'))


class FollowupLoadingTests(base.Offline):
    def test_same_field_conflicts_are_not_truncated(self):
        for raw in ('Bauart: Frontlader / Toplader','Bauart: Frontlader, Toplader',
                    'Form: Frontlader / Toplader Farbe: Weiß',
                    'Form: Toplader oder Frontlader Farbe: Weiß',
                    'Beladung: Front-loading or Top-loading Farbe: Weiß'):
            with self.subTest(raw=raw):
                result,_,log=parse_pages([raw],need_sku=False,need_capacity=False,need_loading=True)
                self.assertIsNone(result.get('loading_type'))
                self.assertIn('loading_type',result['_missing'])
                candidate=next(v for e,v in log.records if e=='pdf_loading_candidate')
                self.assertEqual(candidate['reason'],'conflicting_loading_types')
                self.assertNotIn('Farbe',candidate['raw'])
                self.assertRegex(candidate['raw'],r'(?i)front')
                self.assertRegex(candidate['raw'],r'(?i)top')

    def test_other_labels_are_separated_and_first_valid_value_stops(self):
        for raw,expected in (('Form: Frontlader Farbe: Weiß','Front loader'),
                             ('Form: Toplader, Farbe: Weiß','Top-loading'),
                             ('Bauart: Frontlader, Freistehend Farbe: Weiß','Front loader'),
                             ('Front loaded TD','Front loader')):
            result,pages,_=parse_pages([raw,'must not read'],need_sku=False,need_capacity=False,need_loading=True)
            self.assertEqual(result['loading_type'],expected)
            self.assertNotIn('Farbe',result['loading_type_raw'])
            pages[-1].extract_text.assert_not_called()
        result,pages,_=parse_pages(['Modellkennung: GOOD900\nForm: Frontlader / Toplader',
                                   'Beladung: Frontlader','skip'],need_sku=True,need_capacity=False,need_loading=True)
        self.assertEqual((result['sku'],result['loading_type']),('GOOD900','Front loader'))
        pages[-1].extract_text.assert_not_called()


class FollowupSummaryTests(base.Offline):
    make_client=SixthComparisonTests.make_client
    run_pipeline=SixthComparisonTests.run_pipeline

    def scenario(self, *, recovered=None, missing_all=False, pdf=None, fail=False, rating_failure=False):
        items=[base.target(i) for i in range(1,7)]
        if missing_all:items[-1]=base.target(6,'Wäschetrockner Unbekannt')
        c,source,log=self.make_client(items)
        c.pdf=MagicMock(return_value=pdf or {})
        if rating_failure:
            c.rating=MagicMock(return_value=dict(average_rating=None,rating_count=None,rating_state='failed',
                rating_reason='request_failed:rating:timeout'))
        def fetch(url,endpoint,**kw):
            self.assertEqual(endpoint,'comparison')
            ids=base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            if ids==['v1','v6']:
                if fail:raise client.RequestFailure('request_failed:comparison:timeout')
                values={'v1':{'Modellbezeichnung':'GOOD0001','Nennkapazität':'9 kg','Bauart':'Frontlader'},
                        'v6':recovered or {}}
            else:
                values={v:{'Modellbezeichnung':'GOOD'+v[1:].zfill(4)} for v in ids}
                if ids[0]=='v1':
                    for v in ids:values[v].update({'Nennkapazität':'9 kg','Bauart':'Frontlader'})
                elif missing_all:values['v6']={}
            return comparison_html(ids,values),{'status':200}
        return c,source,log,fetch

    def test_one_comparison_supplement_counts_once_in_actual_pipeline(self):
        c,source,log,fetch=self.scenario(recovered={'Bauart':'Frontlader'})
        with patch.object(c,'fetch',side_effect=fetch) as request:
            rows,run=self.run_pipeline(c,log,6)
        self.assertEqual(run['counts']['supplemented_items'],1)
        final=next(v for e,v in reversed(log.records) if e=='product_result')
        self.assertEqual(final['status'],'supplemented')
        self.assertEqual(final['comparison_supplemented_fields'],['loading_type'])
        self.assertEqual(request.call_count,3)
        c.pdf.assert_not_called()

    def test_multiple_fields_and_pdf_supplements_do_not_double_count(self):
        for recovered,pdf in (({'Modellbezeichnung':'REAL6000','Nennkapazität':'8 kg','Bauart':'Frontlader'},{}),
                              ({'Bauart':'Frontlader'},{'sku':'PDF900','capacity':'9 kg'})):
            with self.subTest(pdf=pdf):
                c,source,log,fetch=self.scenario(recovered=recovered,missing_all=True,pdf=pdf)
                with patch.object(c,'fetch',side_effect=fetch):rows,run=self.run_pipeline(c,log,6)
                self.assertEqual(run['counts']['supplemented_items'],1)
                self.assertEqual(rows[-1]['loading_type'],'Front loader')
                self.assertEqual(rows[-1]['sku'],'PDF900' if pdf else 'REAL6000')
                self.assertEqual(c.pdf.call_count,int(bool(pdf)))

    def test_failed_or_unconfirmed_comparison_is_not_a_success(self):
        for recovered,fail in (({},False),({'Bauart':'Freistehend'},False),({},True)):
            with self.subTest(recovered=recovered,fail=fail):
                c,source,log,fetch=self.scenario(recovered=recovered,fail=fail)
                with patch.object(c,'fetch',side_effect=fetch):rows,run=self.run_pipeline(c,log,6)
                self.assertEqual(run['counts']['supplemented_items'],0)
                self.assertIsNone(rows[-1]['loading_type'])
                self.assertEqual(run['counts']['request_error_items'],int(fail))

    def test_unselected_cached_supplement_does_not_inflate_count(self):
        c,source,log=self.make_client([base.target(6)])
        c.compare_cache={'v1':{'Bauart':'Frontlader'},
                         'v6':{'Modellbezeichnung':'GOOD6000','Bauart':'Frontlader',
                               '_present_labels':['Modellbezeichnung','Bauart']}}
        body=comparison_html(['v1','v6'],{'v1':{'Nennkapazität':'9 kg'},'v6':{'Nennkapazität':'8 kg'}})
        with patch.object(c,'fetch',return_value=(body,{'status':200})) as request:
            c.comparison(['v1','v6'],supplement={'v6':['capacity']})
            rows,run=self.run_pipeline(c,log,1)
        self.assertEqual(rows[0]['capacity'],'9 kg')
        self.assertEqual(run['counts']['supplemented_items'],0)
        self.assertEqual(request.call_count,1)

    def test_success_and_error_counts_survive_csv_state_and_summary(self):
        c,source,log,fetch=self.scenario(recovered={'Bauart':'Frontlader'},rating_failure=True)
        conn=base.FakeConnection()
        def factory(events):c.events=events;return c
        with tempfile.TemporaryDirectory() as root,patch('sys.stdout',new=io.StringIO()),patch.object(c,'fetch',side_effect=fetch):
            code,run=pipeline.execute(limit=6,output_root=root,client_factory=factory,
                database=lambda t,e,rows=None:storage.database(t,e,rows,connector=lambda:conn))
            rows,_=storage.read_output(run['csv'])
            state=json.loads(Path(run['csv']).with_name('state.json').read_text(encoding='utf-8'))
            diag=[json.loads(x) for x in Path(run['diagnostics']).read_text(encoding='utf-8').splitlines()]
            product=next(v for v in reversed(diag) if v['event']=='product_result')
        self.assertEqual(code,2)
        self.assertEqual((run['counts']['supplemented_items'],state['counts']['supplemented_items']),(1,1))
        self.assertEqual(run['counts']['request_error_items'],6)
        self.assertEqual(product['status'],'supplemented')
        self.assertTrue(product['request_error_items'])
        self.assertEqual(tuple(rows[0]),config.COLUMNS)
        self.assertEqual([r['main_rank'] for r in rows],[str(i) for i in range(1,7)])
        self.assertEqual(rows[-1]['loading_type'],'Front loader')


if __name__ == '__main__':
    unittest.main()
