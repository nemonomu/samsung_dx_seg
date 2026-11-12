"""Offline regression cases for explicit types, short models and bounded PDF work."""
import importlib.util
import io
import multiprocessing
import socket
import sys
import tempfile
import time
import types
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_dry as base
from dry import client, config, fields, pdf_process, pipeline, storage


def _blocked_http_worker(connection, uri, body, needed, timeout):
    with patch.object(socket.socket, "connect", side_effect=AssertionError("NO_NETWORK")), \
            patch.object(client, "urlopen", side_effect=lambda *a, **k: time.sleep(30)):
        pdf_process._pdf_worker(connection, uri, body, needed, timeout)


def _blocked_parse_worker(connection, uri, body, needed, timeout):
    @contextmanager
    def document(*args):
        first = types.SimpleNamespace(extract_text=lambda: "Modellkennung: PDF900")
        second = types.SimpleNamespace(extract_text=lambda: time.sleep(30))
        yield types.SimpleNamespace(pages=[first, second])
    with patch.object(socket.socket, "connect", side_effect=AssertionError("NO_NETWORK")), \
            patch.dict(sys.modules, {"pdfplumber": types.SimpleNamespace(open=document)}):
        pdf_process._pdf_worker(connection, uri, body, needed, timeout)


def _blocked_open_worker(connection, uri, body, needed, timeout):
    with patch.object(socket.socket, "connect", side_effect=AssertionError("NO_NETWORK")), \
            patch.dict(sys.modules, {"pdfplumber": types.SimpleNamespace(open=lambda *a: time.sleep(30))}):
        pdf_process._pdf_worker(connection, uri, body, needed, timeout)


def _synthetic_worker(connection, uri, body, needed, timeout):
    @contextmanager
    def document(*args):
        if uri.endswith('unreadable.pdf'):
            raise ValueError('RAW_DOCUMENT_MUST_NOT_APPEAR')
        texts = ['Modellkennung: PDF900', 'info', 'info', 'Rated capacity 9\nBauart: Frontlader']
        yield types.SimpleNamespace(pages=[types.SimpleNamespace(extract_text=lambda t=t: t) for t in texts])
    response = MagicMock()
    response.status = 200
    response.geturl.return_value = uri
    response.__enter__.return_value = response
    response.read1.side_effect = [b'%PDF synthetic', b'']
    with patch.object(socket.socket, 'connect', side_effect=AssertionError('NO_NETWORK')), \
            patch.object(client, 'urlopen', return_value=response), \
            patch.dict(sys.modules, {'pdfplumber': types.SimpleNamespace(open=document)}):
        pdf_process._pdf_worker(connection, uri, body, needed, timeout)


def pdf_bytes(texts):
    """Small valid, text-only fixture, generated without a PDF dependency."""
    objects = [b'<< /Type /Catalog /Pages 2 0 R >>', b'', b'<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>']
    pages = []
    for text in texts:
        page_id = len(objects) + 1
        pages.append(page_id)
        objects.append(f'<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] /Resources << /Font << /F1 3 0 R >> >> /Contents {page_id+1} 0 R >>'.encode())
        stream = ('BT /F1 12 Tf 30 750 Td (' + text + ') Tj ET').encode('ascii')
        objects.append(f'<< /Length {len(stream)} >>\nstream\n'.encode() + stream + b'\nendstream')
    objects[1] = ('<< /Type /Pages /Count %d /Kids [%s] >>' % (len(pages), ' '.join(f'{n} 0 R' for n in pages))).encode()
    result = b'%PDF-1.4\n'; offsets = [0]
    for number, obj in enumerate(objects, 1):
        offsets.append(len(result)); result += f'{number} 0 obj\n'.encode() + obj + b'\nendobj\n'
    start = len(result)
    result += f'xref\n0 {len(offsets)}\n0000000000 65535 f \n'.encode()
    result += b''.join(f'{n:010d} 00000 n \n'.encode() for n in offsets[1:])
    return result + f'trailer\n<< /Size {len(offsets)} /Root 1 0 R >>\nstartxref\n{start}\n%%EOF\n'.encode()


class SelectionRevisionTests(base.Offline):
    def test_explicit_products_override_generic_names_and_specs(self):
        for name in ("ibettertec Elektrischer Wäschetrockner Portabler Wäschetrockner 850W 33L",
                     "Brand Portabler Wäschetrockner", "Brand Portablen Wäschetrockner",
                     "HEYDRY Elektrischer Wäschetrockner HEYDRY Lüfter für Wäscheständer",
                     "Brand Ventilator für den Wäscheständer", "Brand Luefter fuer Waeschestaender"):
            with self.subTest(name=name):
                self.assertFalse(fields.classify(name)[0])
                self.assertFalse(fields.classify("Wäschetrockner DR900", selection_name=name)[0])

    def test_fan_features_and_compatibility_are_not_sold_type(self):
        for suffix in ("mit Lüfter", "mit Lüfter für Wäscheständer", "kompatibel mit Lüfter für Wäscheständer",
                       "für Wäscheständer geeignet", "mit mobiler App", "portable App-Steuerung",
                       "mobile Steuerung", "mit tragbarer Fernbedienung", "mit Ersatzfilter und Schlauch"):
            with self.subTest(suffix=suffix):
                self.assertTrue(fields.classify("Wärmepumpentrockner DR900 " + suffix)[0])

    def test_existing_normal_models_remain_included(self):
        for model in ("HWT8EGT", "HWT8DGT", "KD-NHH9S8GW3-DE", "Vivace RT90V9W"):
            self.assertTrue(fields.classify("Wärmepumpentrockner " + model + " mit Pumpe, Filter und Schlauch")[0])
        self.assertEqual(fields.classify("Brand unbekannt"), (True, "ambiguous_included_product_type"))

    def test_portable_describes_control_or_accessory_not_appliance(self):
        for feature in ("mobile Fernsteuerung", "tragbare Fernbedienung", "portable App-Steuerung",
                        "mobiler Fernzugriff", "tragbares Bedienpanel", "mobile Smartphone-Steuerung",
                        "mit mobiler App", "kompatibel mit tragbaren Geräten"):
            title = "Wärmepumpentrockner DR900, " + feature
            with self.subTest(title=title):
                self.assertTrue(fields.classify(title)[0])
                self.assertTrue(fields.classify("Wäschetrockner DR900", selection_name=title)[0])
        self.assertTrue(fields.classify("Mobile Fernsteuerung, Wärmepumpentrockner DR900")[0])

    def test_actual_portable_appliance_stays_excluded_with_controls(self):
        for title in ("Portabler Wäschetrockner DR900 mit mobiler App",
                      "Mobiler elektrischer Wäschetrockner DR900, tragbare Fernbedienung",
                      "Elektrischer Wäschetrockner Portabler Wäschetrockner 850W 33L, mobile Fernsteuerung",
                      "Portable App-Steuerung, Portabler Wäschetrockner DR900",
                      "Tragbarer Wäschetrockner DR900, mobile Fernsteuerung",
                      "Trockner DR900 portable", "Wäschetrockner DR900, portabel, App-Steuerung"):
            with self.subTest(title=title):
                self.assertFalse(fields.classify(title)[0])
                self.assertFalse(fields.classify("Wäschetrockner DR900", selection_name=title)[0])
        self.assertTrue(fields.classify("Nicht portabler Wäschetrockner DR900")[0])

    def test_new_exclusions_replenish_100_ranks_without_extra_requests(self):
        excluded = [base.target(900, "Portabler Wäschetrockner"), base.target(901, "Lüfter für Wäscheständer")]
        source = base.Source([excluded + [base.target(1), base.target(2, base.target(1)["retailer_sku_name"])]
                              + [base.target(i) for i in range(3, 110)]])
        state = dict(target=100, batch_id="test", calendar_week="w43", crawl_datetime="2026-10-24 00:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda rows: None)
        self.assertEqual(len(rows), 100)
        self.assertEqual([(r['main_rank'], r['bsr_rank']) for r in rows], [(n,n) for n in range(1,101)])
        self.assertEqual((state['counts']['excluded'], state['counts']['duplicates']), (2,1))
        self.assertEqual(len([c for c in source.calls if c[0]=='rating']),100)
        self.assertFalse(any(c[0]=='pdf' for c in source.calls))


class SkuRevisionTests(base.Offline):
    def test_boundaries_and_normal_model_preservation(self):
        for raw in ("A", "AB", "ABC", "  ABC  ", "A  B", "-", ""):
            self.assertIsNone(fields.valid_sku(raw))
        for raw in ("AB12", "TSA523WP 8kg Active", "TQ 1000 WP Nova Edition"):
            self.assertEqual(fields.valid_sku(raw),raw)

    def test_priority_skips_short_sources_and_records_reason(self):
        t=base.target(1, "Wärmepumpentrockner TITLE900")
        log=base.Log()
        self.assertEqual(fields.choose(t,{"Modellbezeichnung":"A"},{"sku":"PDF900"},log)['sku'],'PDF900')
        diag=next(v for e,v in log.records if v['field']=='sku')
        self.assertEqual(diag['candidates'][0]['reason'],'sku_too_short')
        self.assertEqual(diag['candidates'][0]['raw'],'A')
        self.assertEqual(fields.choose(t,{"Modellbezeichnung":"AB"},{"sku":"ABC"})['sku'],'TITLE900')
        self.assertEqual(fields.choose(t,{"Modellbezeichnung":"GOOD900"},{"sku":"PDF900"})['sku'],'GOOD900')

    def test_null_sku_row_csv_and_insert_values_are_preserved(self):
        source=base.Source([[base.target(1,"Trockner Alpha"),base.target(2)]], {'v1':{'Modellbezeichnung':'A','_name':'Trockner 9 kg Frontlader'}})
        source.pdf=MagicMock(return_value={})
        conn=base.FakeConnection()
        with tempfile.TemporaryDirectory() as folder,patch('sys.stdout',new=io.StringIO()):
            code,state=pipeline.execute(limit=2,output_root=folder,client_factory=lambda e:source,
                database=lambda table,events,rows=None:storage.database(table,events,rows,connector=lambda:conn))
            rows,_=storage.read_output(state['csv'])
        self.assertEqual(code,0)
        self.assertEqual(len(rows),2)
        self.assertEqual(rows[0]['sku'],'')
        self.assertIsNone(conn.records[0][config.COLUMNS[1:].index('sku')])
        self.assertEqual(rows[1]['main_rank'],'2')
        source.pdf.assert_called_once_with(None,need_sku=True,need_capacity=False,need_loading=False)


class PdfRevisionTests(base.Offline):
    def pages(self,texts,**needed):
        pages=[MagicMock() for _ in texts]
        for page,text in zip(pages,texts):
            if isinstance(text,Exception):page.extract_text.side_effect=text
            else:page.extract_text.return_value=text
        document=MagicMock();document.__enter__.return_value.pages=pages
        with patch.dict(sys.modules,{'pdfplumber':types.SimpleNamespace(open=MagicMock(return_value=document))}):
            result=client.parse_pdf(b'%PDF',**needed)
        return result,pages

    def test_sku_on_fourth_page_then_stop(self):
        result,pages=self.pages(['info']*3+['Modellkennung: PDF900','do not read'],need_sku=True,need_capacity=False)
        self.assertEqual(result['sku'],'PDF900')
        pages[4].extract_text.assert_not_called()

    def test_capacity_and_loading_after_model(self):
        result,pages=self.pages(['Modellkennung: PDF900','info','info','Rated capacity 9','Bauart: Frontlader','skip'],
                               need_sku=True,need_capacity=True,need_loading=True)
        self.assertEqual((result['sku'],result['capacity'],result['loading_type']),('PDF900','9 kg','Front loader'))
        pages[-1].extract_text.assert_not_called()

    def test_short_pdf_candidate_does_not_stop_search(self):
        log=base.Log()
        result,pages=self.pages(['Modellkennung: AB1','Modellkennung: PDF900','skip'],
                               need_sku=True,need_capacity=False,progress=log.emit)
        self.assertEqual(result['sku'],'PDF900')
        self.assertTrue(any(v.get('reason')=='sku_too_short' for _,v in log.records))
        pages[-1].extract_text.assert_not_called()

    def test_same_page_rejects_short_candidates_before_valid_model(self):
        log = base.Log()
        result, pages = self.pages([
            'Modellkennung: A\nModellbezeichnung: A1\nModel identifier: AB1\nModellkennung: PDF900',
            'Modellkennung: LATER900'], need_sku=True, need_capacity=False, progress=log.emit)
        self.assertEqual(result['sku'], 'PDF900')
        candidates = [v for e, v in log.records if e == 'pdf_sku_candidate']
        self.assertEqual([v['raw'] for v in candidates], ['A', 'A1', 'AB1', 'PDF900'])
        self.assertEqual([v['reason'] for v in candidates[:3]], ['sku_too_short'] * 3)
        self.assertEqual(candidates[-1]['selected'], 'PDF900')
        self.assertTrue(all(v['page'] == 1 for v in candidates))
        pages[-1].extract_text.assert_not_called()

    def test_same_line_model_labels_are_not_joined(self):
        for text in ('Modellkennung: A1 Model identifier: PDF900',
                     'Modellkennung:\nModel identifier: PDF900',
                     'Modellkennung: NONE\nModel identifier: PDF900'):
            with self.subTest(text=text):
                result, _ = self.pages([text], need_sku=True, need_capacity=False)
                self.assertEqual(result['sku'], 'PDF900')

    def test_first_valid_and_long_models_are_preserved(self):
        for model in ('AB12', 'TSA523WP 8kg Active', 'TQ 1000 WP Nova Edition', 'WAM 914 A'):
            with self.subTest(model=model):
                result, _ = self.pages(['Modellkennung: ' + model + '\nModel identifier: LATER900'],
                                       need_sku=True, need_capacity=False)
                self.assertEqual(result['sku'], model)

    def test_unlabelled_codes_and_specifications_are_not_models(self):
        for text in ('Wifi code PDF900\nRated capacity 9',
                     'Modellkennung:\nRated capacity 9\nWifi code PDF900',
                     'Modellkennung: A1 Gewicht: 50 kg\nModellbezeichnung: AB1',
                     'Modellkennung: A1\nModel identifier: AB1\nPower demand: 850 W'):
            with self.subTest(text=text):
                result, _ = self.pages([text], need_sku=True, need_capacity=False)
                self.assertFalse(result.get('sku'))
                self.assertIn('sku', result['_missing'])

    def test_same_page_model_then_missing_fields_continue_without_replacing_model(self):
        result, pages = self.pages(['Modellkennung: A1\nModel identifier: PDF900',
                                   'Modellkennung: OTHER900\nRated capacity 9',
                                   'Bauart: Frontlader', 'skip'],
                                   need_sku=True, need_capacity=True, need_loading=True)
        self.assertEqual((result['sku'], result['capacity'], result['loading_type']),
                         ('PDF900', '9 kg', 'Front loader'))
        pages[-1].extract_text.assert_not_called()

    def test_same_page_model_survives_later_page_failure(self):
        result, _ = self.pages(['Modellkennung: A1\nModel identifier: PDF900', ValueError('private')],
                               need_sku=True, need_capacity=True)
        self.assertEqual(result['sku'], 'PDF900')
        self.assertEqual(result['_error'], 'pdf_extract:ValueError')

    def test_first_page_model_only_does_not_read_remaining(self):
        result,pages=self.pages(['Modellkennung: PDF900','skip'],need_sku=True,need_capacity=False)
        self.assertEqual(result['sku'],'PDF900');pages[-1].extract_text.assert_not_called()

    def test_partial_fields_survive_extraction_exception_without_raw_error(self):
        result,_=self.pages(['Modellkennung: PDF900',ValueError('PRIVATE_RAW_BODY')],need_sku=True,need_capacity=True)
        self.assertEqual(result['sku'],'PDF900')
        self.assertEqual(result['_error'],'pdf_extract:ValueError')
        self.assertNotIn('PRIVATE_RAW_BODY',str(result))

    def test_retry_budget_and_success(self):
        failure={'_error':'pdf_request_timeout','_error_kind':'request','_retryable':True}
        for responses,expected in (([(failure,None),({'sku':'PDF900'},b'%PDF')],2), ([(failure,None)]*3,3)):
            c=client.Client(base.Log(),attempts=9)
            with patch.object(client,'pdf_attempt',side_effect=responses) as attempt,patch.object(client.time,'sleep'):
                result=c.pdf('https://example.invalid/document.pdf',need_sku=True,need_capacity=False)
            self.assertEqual(attempt.call_count,expected)
            if expected==2:self.assertEqual(result['sku'],'PDF900')
            else:self.assertIn('sku',result['_missing'])

    def test_parse_retry_reuses_bytes_and_only_missing_fields(self):
        partial={'sku':'PDF900','sku_raw':'PDF900','_error':'pdf_extract:ValueError','_error_kind':'extraction','_retryable':True}
        c=client.Client(base.Log())
        with patch.object(client,'pdf_attempt',side_effect=[(partial,b'%PDF cached'),({'capacity':'9 kg'},b'%PDF cached')]) as attempt,patch.object(client.time,'sleep'):
            result=c.pdf('https://example.invalid/document.pdf',need_sku=True,need_capacity=True)
        self.assertEqual(result['sku'],'PDF900');self.assertEqual(result['capacity'],'9 kg')
        self.assertEqual(attempt.call_args.args[1],b'%PDF cached')
        self.assertEqual(attempt.call_args.args[2],dict(sku=False,capacity=True,loading_type=False))

    def test_missing_labels_do_not_retry_and_complete_values_do_not_request(self):
        c=client.Client(base.Log())
        with patch.object(client,'pdf_attempt',return_value=({'_unavailable':'pdf_label_unconfirmed'},b'%PDF')) as attempt:
            c.pdf('https://example.invalid/document.pdf',need_sku=True,need_capacity=False)
            self.assertEqual(c.pdf('https://example.invalid/document.pdf',need_sku=False,need_capacity=False),{})
        attempt.assert_called_once()

    def test_worker_has_exactly_one_http_attempt(self):
        receiver=MagicMock()
        parsed={'sku':'PDF900'}
        with patch.object(client.Client,'fetch',return_value=(b'%PDF mock',{})) as fetch,patch.object(client,'parse_pdf',return_value=parsed):
            pdf_process._pdf_worker(receiver,'https://example.invalid/document.pdf',None,dict(sku=True,capacity=False,loading_type=False),30)
        self.assertEqual(fetch.call_count,1)
        self.assertEqual(fetch.call_args.kwargs['attempts'],1)
        self.assertEqual(receiver.send.call_args.args[0],('done',parsed))

    def test_partial_model_survives_all_later_retries_failing(self):
        first={'sku':'PDF900','_error':'pdf_extract:ValueError','_error_kind':'extraction','_retryable':True}
        later={'_error':'pdf_parse_timeout','_error_kind':'extraction','_retryable':True}
        with patch.object(client,'pdf_attempt',side_effect=[(first,b'%PDF'),(later,b'%PDF'),(later,b'%PDF')]) as attempt,patch.object(client.time,'sleep'):
            result=client.Client(base.Log()).pdf('https://example.invalid/doc.pdf',need_sku=True,need_capacity=True)
        self.assertEqual(result['sku'],'PDF900')
        self.assertEqual(result['_missing'],['capacity'])
        self.assertEqual(attempt.call_count,3)

    def test_open_failure_retries_then_succeeds_and_invalid_uri_never_starts(self):
        failure={'_error':'pdf_open:ValueError','_error_kind':'extraction','_retryable':True}
        with patch.object(client,'pdf_attempt',side_effect=[(failure,None),({'sku':'PDF900'},b'%PDF')]) as attempt,patch.object(client.time,'sleep'):
            result=client.Client(base.Log()).pdf('https://example.invalid/doc.pdf',need_sku=True,need_capacity=False)
        self.assertEqual(result['sku'],'PDF900');self.assertIsNone(attempt.call_args.args[1])
        with patch.object(client,'pdf_attempt') as attempt:
            result=client.Client(base.Log()).pdf('https://[invalid',need_sku=True,need_capacity=False)
        attempt.assert_not_called();self.assertEqual(result['_error'],'own_pdf_link_invalid')

    def test_failed_pdf_preserves_other_sources_and_next_product(self):
        source=base.Source([[base.target(1),base.target(2)]],{'v1':{'Modellbezeichnung':'GOOD900','_name':'Trockner 9 kg'}})
        source.pdf=MagicMock(return_value={'_error':'pdf_parse_timeout','_error_kind':'extraction'})
        state=dict(target=2,batch_id='test',calendar_week='w43',crawl_datetime='2026-10-24 00:00:00')
        rows=pipeline.collect(source,base.Log(),state,lambda r:None)
        self.assertEqual(len(rows),2)
        self.assertEqual((rows[0]['sku'],rows[0]['capacity']),('GOOD900','9 kg'))
        self.assertEqual(state['counts']['extraction_error_items'],1)


class PdfProcessTests(base.Offline):
    def test_spawned_success_and_unreadable_document_cache(self):
        for filename in ('good.pdf','unreadable.pdf'):
            log=base.Log()
            with patch.object(pdf_process,'_pdf_worker',_synthetic_worker):
                result,body=pdf_process.pdf_attempt('https://example.invalid/'+filename,None,
                    dict(sku=True,capacity=True,loading_type=True),5,5,log.emit)
            self.assertEqual(sum(e=='request_start' for e,_ in log.records),1)
            if filename=='good.pdf':
                self.assertEqual((result['sku'],result['capacity'],result['loading_type']),('PDF900','9 kg','Front loader'))
                self.assertEqual(body,b'%PDF synthetic')
                self.assertTrue(any(e=='pdf_page' and v['page']==4 for e,v in log.records))
            else:
                self.assertEqual(result['_error'],'pdf_open:ValueError');self.assertIsNone(body)
            self.assertNotIn('RAW_DOCUMENT_MUST_NOT_APPEAR',str(result)+str(log.records))

    def bounded(self,target,body,needed):
        before={p.pid for p in multiprocessing.active_children()}
        log=base.Log();started=time.monotonic()
        with patch.object(pdf_process,'_pdf_worker',target):
            result,cached=pdf_process.pdf_attempt('https://example.invalid/document.pdf',body,needed,1.5,1.5,log.emit)
        self.assertLess(time.monotonic()-started,6)
        self.assertEqual({p.pid for p in multiprocessing.active_children()},before)
        return result,cached,log

    def test_stalled_http_is_actually_terminated(self):
        result,_,_=self.bounded(_blocked_http_worker,None,dict(sku=True,capacity=False,loading_type=False))
        self.assertEqual(result['_error'],'pdf_request_timeout')

    def test_stalled_pdf_open_is_actually_terminated(self):
        result,cached,_=self.bounded(_blocked_open_worker,b'%PDF mock',dict(sku=True,capacity=False,loading_type=False))
        self.assertEqual(result['_error'],'pdf_parse_timeout');self.assertIsNone(cached)

    def test_stalled_page_preserves_earlier_fields_and_cached_bytes(self):
        result,cached,log=self.bounded(_blocked_parse_worker,b'%PDF mock',dict(sku=True,capacity=True,loading_type=False))
        self.assertEqual(result['_error'],'pdf_parse_timeout')
        self.assertEqual(result['sku'],'PDF900');self.assertEqual(cached,b'%PDF mock')
        self.assertTrue(any(e=='pdf_page' and v['page']==2 for e,v in log.records))

    @unittest.skipUnless(importlib.util.find_spec('pdfplumber'), 'pdfplumber not installed; no package installation authorized')
    def test_real_pdf_decoding_in_spawned_worker(self):
        body=pdf_bytes(['info','info','info','Modellkennung: A1 Model identifier: PDF900'])
        result,_=pdf_process.pdf_attempt(None,body,dict(sku=True,capacity=False,loading_type=False),5,10,base.Log().emit)
        self.assertEqual(result['sku'],'PDF900')
        self.assertNotIn('_error',result)


if __name__=='__main__':
    unittest.main()
