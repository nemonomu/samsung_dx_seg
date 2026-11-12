"""Offline regressions for sold titles, comparison reuse and labelled PDF models."""
import importlib.util
import re
import unittest
from unittest.mock import MagicMock, patch

import test_dry as base
from test_dry_fourth import parse_pages
from test_dry_revision import pdf_bytes
import test_dry_sixth as sixth
from test_dry_sixth import comparison_html
from dry import client, config, fields, history, pipeline


SOLD_TITLES = (
    'Lenz Space Warmer 2.0 Trockner Motorradstiefel',
    'Lenz Skisocken',
    'BOSCH Griffkappe Schalengriff 00652549 Griffplatte',
    'Keyura Wäschesack Schuhwaschbeutel für Waschmaschine 2 Stück',
    'Kabelschloss Steckerschloss Box Waschmaschine Steckersafe',
    'Standventilator Bodenlüfter Turbolüfter Ventilator Trockner Bodenventilator Fußbläser',
    'Lalano Cosmetics Kosmetikpinsel-Set LUMIBRUSH 3-in-1 Pinselreiniger & Trockner',
    'BOSCH Dichtungsring Trommeldichtung 00649045 Türdichtung',
    'Halloween Clown-Kostüm Scary Clown Kostüm, nicht im Trockner trocknen',
    'AEG Dichtungsring Trommeldichtung 136606311/1',
    'Dyckhoff Badematte 3 Badvorleger NATURE 50x80cm - stein',
    'Wisam Lernspielzeug Beauty-Set Kosmetiktasche für Kinder Nagelzubehör Trockner',
    'THERM-IC Einlegesohlen THERM-IC -ic dryer Trockner Unisex Ski, leicht',
    'COSTWAY Kühlbox Isolierter Lebensmittel-Trockner 77 Liter',
    'Lüftungsgitter 150mm Abluftgitter Fliegengitter für Bad Küche Trockner',
    'Kettenschloss Steckerschloss Waschmaschine Steckersafe Schloss Stecker',
    'Avene Deo-Roller Cicalfate + Reparatur-Trockner-Spray',
    'Sesiom World Haarpflege-Set WINDPRO 3800 Profi-Trockner 2000W',
    'Sesiom World Haarpflege-Set WINDPRO 3500 Profi-Trockner 2000W',
    'Sesiom World Haarpflege-Set WINDPRO 3900 TURBO Profi-Trockner 2200W',
    'Nedis Dämpfer WASK130WT, Sicher stapelbar, Universal-Stapel-Kit, geeignet für breite Trockner',
)


class SoldTitleTests(base.Offline):
    def test_saved_clear_sold_titles(self):
        for title in SOLD_TITLES:
            with self.subTest(title=title):
                self.assertFalse(fields.classify(title)[0])
                proof = fields.classification_evidence(title)[0]
                self.assertEqual(proof['raw'], title)
                self.assertEqual(proof['source'], 'listing_title')
                self.assertEqual(proof['decision'], 'exclude')

    def test_feature_delivery_and_compatibility_descriptions_stay_included(self):
        for noun in ('Haarpflege-Set', 'Kosmetikpinsel-Set', 'Pinselreiniger',
                     'Lüftungsgitter', 'Dämpfer', 'Dichtungsring', 'Badematte',
                     'Standventilator', 'Deo-Roller', 'Skisocken'):
            for title in (f'Wäschetrockner mit {noun}',
                          f'Wäschetrockner DR900, {noun} inklusive',
                          f'Wärmepumpentrockner DR900, {noun}',
                          f'Wäschetrockner DR900, kompatibel mit {noun}'):
                with self.subTest(title=title):
                    self.assertTrue(fields.classify(title)[0])
        for title in ('Mini Wäschetrockner 4 kg',
                      'Wärmepumpentrockner DR900, mobile Fernsteuerung',
                      'Wäschetrockner DR900, tragbare Fernbedienung',
                      'Wäschetrockner DR900, Filter leicht zu reinigen'):
            self.assertTrue(fields.classify(title)[0])

    def test_pipeline_excludes_before_details_and_refills_duplicates(self):
        items = [base.target(1, SOLD_TITLES[-4]), base.target(2), base.target(2),
                 base.target(3, next(t for t in SOLD_TITLES if 'Pinselreiniger' in t)), base.target(4), base.target(5)]
        source, log = base.Source([items]), base.Log()
        state = dict(target=3, batch_id='test', calendar_week='w44', crawl_datetime='2026-10-31 00:00:00')
        rows = pipeline.collect(source, log, state, lambda rows: None)
        self.assertEqual([r['item'] for r in rows], ['2', '4', '5'])
        self.assertEqual([r['main_rank'] for r in rows], [1, 2, 3])
        self.assertEqual([r['bsr_rank'] for r in rows], [1, 2, 3])
        self.assertEqual(tuple(rows[0]), config.COLUMNS)
        self.assertEqual((state['counts']['excluded'], state['counts']['duplicates']), (2, 1))
        details = [v for call in source.calls if call[0] == 'comparison' for v in call[1]]
        self.assertNotIn('v1', details)
        self.assertNotIn('v3', details)
        self.assertEqual([call[1] for call in source.calls if call[0] == 'rating'], ['2', '4', '5'])
        self.assertFalse(any(call[0] == 'pdf' for call in source.calls))


class ComparisonReuseTests(base.Offline):
    make_client = sixth.SixthComparisonTests.make_client
    run_pipeline = sixth.SixthComparisonTests.run_pipeline

    def scenario(self, *, sku_only=False, present_empty=False, title_only=False):
        c, source, log = self.make_client([base.target(i) for i in range(1, 7)])
        c.pdf = MagicMock(return_value={'sku': 'PDF900'})
        requests = []

        def fetch(url, endpoint, **kwargs):
            self.assertEqual(endpoint, 'comparison')
            ids = base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            requests.append(ids)
            if ids == ['v1', 'v6']:
                values = {'v1': {'Modellbezeichnung': 'REF900', 'Bauart': 'Frontlader'},
                          'v6': {} if title_only else {
                              'Modellbezeichnung': 'TSD643WP EcoSpeed&9kg', 'Bauart': 'Frontlader',
                              'Nennkapazität': '7 kg'}}
                body = comparison_html(ids, values)
                if title_only:
                    body = body.replace(b'DR6000</h3>', b'DR6000, 8 kg, Frontlader</h3>')
                return body, {'status': 200}
            values = {v: {'Modellbezeichnung': 'GOOD' + v[1:].zfill(4), 'Bauart': 'Frontlader'} for v in ids}
            if 'v6' in ids:
                values['v6'] = {'Bauart': 'Frontlader'} if sku_only or present_empty else {}
                if present_empty:
                    values['v6']['Modellbezeichnung'] = '-'
            body = comparison_html(ids, values)
            if title_only and 'v6' in ids:
                body = re.sub(rb'<a data-variation-id="v6".*?</a>', b'', body)
            return body, {'status': 200}

        return c, source, log, fetch, requests

    def test_listing_sku_does_not_block_model_recovery_in_existing_request(self):
        c, source, log, fetch, requests = self.scenario()
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, run = self.run_pipeline(c, log, 6)
        self.assertEqual(rows[-1]['sku'], 'TSD643WP EcoSpeed&9kg')
        self.assertEqual(rows[-1]['loading_type'], 'Front loader')
        self.assertEqual(rows[-1]['capacity'], '9 kg')  # title outranks comparison capacity
        self.assertEqual(len(requests), 3)  # two base groups, one supplement
        self.assertEqual(requests[-1], ['v1', 'v6'])
        c.pdf.assert_not_called()
        self.assertEqual(run['counts']['supplemented_items'], 1)
        result = next(v for e, v in reversed(log.records) if e == 'product_result')
        self.assertEqual(set(result['comparison_supplemented_fields']), {'sku', 'loading_type'})
        sku = next(v for e, v in reversed(log.records) if e == 'field' and v['field'] == 'sku')
        self.assertEqual(sku['source'], 'comparison_model')

    def test_model_only_omission_needs_one_bounded_group_not_pdf(self):
        c, source, log, fetch, requests = self.scenario(sku_only=True)
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, _ = self.run_pipeline(c, log, 6)
        self.assertEqual(rows[-1]['sku'], 'TSD643WP EcoSpeed&9kg')
        self.assertEqual(len(requests), 3)
        c.pdf.assert_not_called()

    def test_present_empty_model_uses_existing_pdf_instead_of_missing_row_retry(self):
        c, source, log, fetch, requests = self.scenario(present_empty=True)
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, _ = self.run_pipeline(c, log, 6)
        self.assertEqual(rows[-1]['sku'], 'PDF900')
        self.assertEqual(len(requests), 2)
        c.pdf.assert_called_once()
        self.assertTrue(c.pdf.call_args.kwargs['need_sku'])

    def test_recovered_title_alone_updates_candidates_pdf_and_statistics(self):
        c, source, log, fetch, requests = self.scenario(title_only=True)
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, run = self.run_pipeline(c, log, 6)
        self.assertEqual((rows[-1]['capacity'], rows[-1]['loading_type']), ('8 kg', 'Front loader'))
        c.pdf.assert_called_once()  # still needed for SKU
        self.assertEqual(c.pdf.call_args.kwargs, dict(need_sku=True, need_capacity=False, need_loading=False))
        result = next(v for e, v in reversed(log.records) if e == 'product_result')
        self.assertEqual(set(result['comparison_supplemented_fields']), {'capacity', 'loading_type'})
        self.assertEqual(run['counts']['supplemented_items'], 1)
        self.assertEqual(len(requests), 3)

    def test_reuses_unrequested_model_and_preserves_existing_values_and_cache_copy(self):
        c = client.Client(base.Log())
        c.compare_cache = {'r': {'Bauart': 'Frontlader'},
                           'v6': {'Nennkapazität': '9 kg', '_name': 'Wäschetrockner, 9 kg',
                                  '_present_labels': ['Nennkapazität']}}
        body = comparison_html(['r', 'v6'], {'r': {'Bauart': 'Frontlader'}, 'v6': {
            'Modellbezeichnung': 'REAL900 Active', 'Bauart': 'Toplader', 'Nennkapazität': '1 kg'}})
        with patch.object(c, 'fetch', return_value=(body, {'status': 200})) as request:
            recovered = c.comparison(['r', 'v6'], supplement={'v6': ['loading_type']})
            c.comparison(['r', 'v6'], supplement={'v6': ['sku']})
        self.assertEqual(recovered['v6']['Modellbezeichnung'], 'REAL900 Active')
        self.assertEqual(recovered['v6']['Nennkapazität'], '9 kg')
        self.assertEqual(recovered['v6']['_name'], 'Wäschetrockner, 9 kg')
        self.assertEqual(request.call_count, 1)
        recovered['v6']['Modellbezeichnung'] = 'WRONG900'
        self.assertEqual(c.compare_cache['v6']['Modellbezeichnung'], 'REAL900 Active')

    def test_comparison_model_does_not_suppress_pdf_needed_for_loading(self):
        c, source, log, fetch, requests = self.scenario()
        def missing_loading(url, endpoint, **kwargs):
            body, meta = fetch(url, endpoint, **kwargs)
            if len(requests) == 3:
                body = body.replace(b'>Frontlader</div>', b'>Freistehend</div>')
            return body, meta
        with patch.object(c, 'fetch', side_effect=missing_loading):
            rows, _ = self.run_pipeline(c, log, 6)
        self.assertEqual(rows[-1]['sku'], 'TSD643WP EcoSpeed&9kg')
        c.pdf.assert_called_once()
        self.assertEqual(c.pdf.call_args.kwargs, dict(need_sku=False, need_capacity=False, need_loading=True))

    def test_different_field_references_reuse_one_response_not_two_requests(self):
        c, source, log = self.make_client([base.target(i) for i in range(1, 7)])
        requests = []
        def fetch(url, endpoint, **kwargs):
            ids = base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            requests.append(ids)
            values = {v: {'Modellbezeichnung': 'GOOD900', 'Bauart': 'Frontlader'} for v in ids}
            if len(requests) == 1:
                values['v1']['Bauart'] = '-'
                values['v2']['Modellbezeichnung'] = '-'
            elif ids == ['v6']:
                values['v6'] = {}
            return comparison_html(ids, values), {'status': 200}
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, run = self.run_pipeline(c, log, 6)
        self.assertEqual(requests, [['v1', 'v2', 'v3', 'v4', 'v5'], ['v6'], ['v1', 'v6']])
        self.assertEqual((rows[-1]['sku'], rows[-1]['loading_type']), ('GOOD900', 'Front loader'))
        self.assertEqual(run['counts']['supplemented_items'], 2)  # v2's PDF SKU and v6's comparison

    def test_recovered_model_becomes_reference_for_later_chunk(self):
        c, source, log = self.make_client([base.target(i) for i in range(1, 12)])
        c.pdf = MagicMock(return_value={'sku': 'PDF900'})
        requests = []
        def fetch(url, endpoint, **kwargs):
            ids = base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            requests.append(ids)
            supplement = ids[0] == 'v1' and 'v6' in ids or ids == ['v1', 'v10'] or ids == ['v6', 'v11']
            values = {v: {'Modellbezeichnung': 'GOOD' + v[1:].zfill(4), 'Bauart': 'Frontlader'} for v in ids}
            if not supplement:
                if ids[0] == 'v1':
                    for v in ids: values[v]['Modellbezeichnung'] = '-'
                elif ids[0] == 'v6':
                    values = {v: {} for v in ids}
                elif ids == ['v11']:
                    values['v11'] = {'Bauart': 'Frontlader'}
            return comparison_html(ids, values), {'status': 200}
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, _ = self.run_pipeline(c, log, 11)
        self.assertEqual(requests[-1], ['v6', 'v11'])
        self.assertEqual(len(requests), 6)
        self.assertEqual(rows[-1]['sku'], 'GOOD0011')
        self.assertEqual(c.pdf.call_count, 5)  # only first group's explicitly empty model fields

    def test_unconfirmed_opportunistic_read_does_not_block_needed_reference_request(self):
        c, source, log = self.make_client([base.target(i) for i in range(1, 7)])
        requests = []
        def fetch(url, endpoint, **kwargs):
            ids = base.parse_qs(base.urlsplit(url).query)['variationIds'][0].split(',')
            requests.append(ids)
            values = {v: {'Modellbezeichnung': 'GOOD900', 'Bauart': 'Frontlader'} for v in ids}
            if len(requests) == 1:
                values['v1']['Bauart'] = '-'
                values['v2']['Modellbezeichnung'] = '-'
            elif ids == ['v6']:
                values['v6'] = {}
            elif ids == ['v1', 'v6']:
                values['v6'] = {'Modellbezeichnung': 'REAL6000'}  # no loading row in this response
            elif ids == ['v2', 'v6']:
                values['v6'] = {'Modellbezeichnung': 'WRONG6000', 'Bauart': 'Frontlader'}
            return comparison_html(ids, values), {'status': 200}
        with patch.object(c, 'fetch', side_effect=fetch):
            rows, _ = self.run_pipeline(c, log, 6)
        self.assertEqual(requests[-2:], [['v1', 'v6'], ['v2', 'v6']])
        self.assertEqual(len(requests), 4)
        self.assertEqual((rows[-1]['sku'], rows[-1]['loading_type']), ('REAL6000', 'Front loader'))

    def test_recovered_explicit_dryer_category_is_reused_but_washer_category_is_not(self):
        for category, expected in (('Frontlader Wäschetrockner', 'Front loader'),
                                   ('Frontlader Waschmaschine', None)):
            with self.subTest(category=category):
                c, source, log, fetch, requests = self.scenario()
                def category_response(url, endpoint, **kwargs):
                    body, meta = fetch(url, endpoint, **kwargs)
                    if len(requests) == 3:
                        body = comparison_html(['v1', 'v6'], {'v1': {'Bauart': 'Frontlader'}, 'v6': {
                            'Modellbezeichnung': 'REAL6000', 'Kategorie': category}})
                    return body, meta
                with patch.object(c, 'fetch', side_effect=category_response):
                    rows, _ = self.run_pipeline(c, log, 6)
                self.assertEqual(rows[-1]['loading_type'], expected)
                self.assertEqual(c.pdf.call_count, int(expected is None))
                self.assertEqual(len(requests), 3)
                if expected:
                    result = next(v for e, v in reversed(log.records) if e == 'product_result')
                    self.assertIn('loading_type', result['comparison_supplemented_fields'])


class LabelledPdfModelTests(base.Offline):
    def test_full_model_field_keeps_mixed_case_and_ampersand(self):
        for model in ('TSD643WP EcoSpeed&9kg', 'TSA523WP 8kg Active', 'DR900 EcoSpeed', 'WAM 914 A'):
            with self.subTest(model=model):
                result, pages, log = parse_pages(['Modellkennung: ' + model, 'must not read'],
                                                need_sku=True, need_capacity=False)
                self.assertEqual(result['sku'], model)
                self.assertEqual(result['sku_raw'], model)
                pages[-1].extract_text.assert_not_called()
                self.assertEqual(next(v['normalized'] for e, v in log.records if e == 'pdf_sku_candidate'), model)

    def test_adjacent_fields_and_footnote_do_not_become_model_suffixes(self):
        for suffix in ('Gewicht: 50 kg', 'Weight: 50 kg', 'Farbe: Weiss', 'Color: White',
                       'Rated capacity 9 kg', 'Energy consumption: 1.5 kWh', 'Width: 60 cm',
                       'Beladung: Frontlader', 'Nennkapazitat 9 kg', 'Drying capacity: 9 kg'):
            with self.subTest(suffix=suffix):
                result, _, _ = parse_pages(['Model identifier: (a) PDF900 ' + suffix],
                                           need_sku=True, need_capacity=False)
                self.assertEqual(result['sku'], 'PDF900')

    def test_short_and_invalid_candidates_continue_without_losing_other_fields(self):
        result, pages, _ = parse_pages([
            'Modellkennung: A1\nModellbezeichnung: 150mm\nModel identifier: TSD643WP EcoSpeed&9kg\nRated capacity 9 kg',
            'Beladung: Frontlader', 'must not read'], need_sku=True, need_capacity=True, need_loading=True)
        self.assertEqual((result['sku'], result['capacity'], result['loading_type']),
                         ('TSD643WP EcoSpeed&9kg', '9 kg', 'Front loader'))
        pages[-1].extract_text.assert_not_called()

    def test_sku_and_history_validation_are_not_expanded(self):
        for value in ('3-in-1', '19025', 'TSA523WP 8kg Active', 'CM1234MM'):
            self.assertEqual(fields.valid_sku(value), value)
            self.assertEqual(history.normalized('sku', value), value)
        self.assertIsNone(fields.valid_sku('150mm'))
        self.assertIsNone(fields.valid_sku('A1'))

    @unittest.skipUnless(importlib.util.find_spec('pdfplumber'), 'pdfplumber not installed; no package installation')
    def test_real_pdf_decoding_preserves_full_field_and_stops_at_next_label(self):
        result = client.parse_pdf(pdf_bytes(['Modellkennung: TSD643WP EcoSpeed&9kg Farbe: Weiss']),
                                  need_sku=True, need_capacity=False)
        self.assertEqual(result['sku'], 'TSD643WP EcoSpeed&9kg')
        self.assertNotIn('_error', result)


if __name__ == '__main__':
    unittest.main()
