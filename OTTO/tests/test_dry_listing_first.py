"""Offline listing-first, title-only selection and bounded reuse contracts."""
import copy
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_dry as base
from dry import client, fields, pipeline, storage
from dry.events import Events


def state(limit=300):
    return dict(target=limit, batch_id="test", calendar_week="w43", crawl_datetime="2026-10-25 12:00:00")


class ListingFirstTests(base.Offline):
    def test_existing_filament_dryer_type_handles_english_long_title_without_feature_false_exclusion(self):
        for title in ("Brand Filament Dryer FD900", "3D Druckerband Space Pi X4 Filament Dryer"):
            self.assertFalse(fields.classify("Generic card",selection_name=title)[0])
        for title in ("Wärmepumpentrockner DR900 mit Filament Dryer Funktion",
                      "Wäschetrockner DR900, Filament Dryer inklusive"):
            self.assertTrue(fields.classify(title)[0])

    def test_all_listing_pages_precede_every_other_request_and_keep_order(self):
        items = [base.target(i) for i in range(1, 9)]
        source = base.Source([items[:3], items[3:6], items[6:]])
        log, run = base.Log(), state(4)
        rows = pipeline.collect(source, log, run, lambda r: None)
        self.assertEqual(source.calls[:3], [("page",1,0),("page",2,3),("page",3,6)])
        self.assertFalse(any(c[0]=="page" for c in source.calls[3:]))
        self.assertEqual([r["item"] for r in rows], ["1","2","3","4"])
        self.assertEqual(run["listing"]["occurrences"],8)
        self.assertEqual(run["counts"]["reviewed"],4)
        self.assertTrue(run["listing_complete"])

    def test_long_title_excludes_before_cards_and_cannot_be_rescued_by_details(self):
        excluded = base.target(1,"Portabler Wäschetrockner DR900")
        included = base.target(2,"Wärmepumpentrockner DR901, mobile Fernsteuerung")
        source = base.Source([[excluded,included]],{"v2":{"_name":"Waschtrockner", "Produkttyp":"Waschtrockner"}})
        original = source.tiles
        def tiles(ids):
            result=original(ids)
            for v in result.values(): v["retailer_sku_name"]="Short card name"
            return result
        source.tiles=tiles
        rows=pipeline.collect(source,base.Log(),state(1),lambda r:None)
        self.assertEqual([r["item"] for r in rows],["2"])
        self.assertEqual(rows[0]["retailer_sku_name"],"Short card name")
        self.assertEqual([v for c in source.calls if c[0]=="tiles" for v in c[1]],["v2"])
        self.assertEqual([v for c in source.calls if c[0]=="comparison" for v in c[1]],["v2"])

    def test_missing_long_title_uses_card_and_card_name_dedup_is_unchanged(self):
        items=[base.target(i) for i in range(1,5)]
        items[0]["retailer_sku_name"]=""
        source=base.Source([items])
        original=source.tiles
        def tiles(ids):
            result=original(ids)
            names={"v1":"Filter für Trockner","v2":"Wärmepumpentrockner SAME900", "v3":"Wärmepumpentrockner SAME900", "v4":"Wärmepumpentrockner LAST900"}
            return {v:dict(data,retailer_sku_name=names[v]) for v,data in result.items()}
        source.tiles=tiles
        run=state(2)
        rows=pipeline.collect(source,base.Log(),run,lambda r:None)
        self.assertEqual([r["item"] for r in rows],["2","4"])
        self.assertEqual((run["counts"]["excluded"],run["counts"]["duplicates"]),(1,1))

    def test_300_after_filters_and_duplicates_does_not_truncate_raw_candidates(self):
        items=[base.target(900+i,"Filter für Trockner") for i in range(160)]
        items += [base.target(1),base.target(700,base.target(1)["retailer_sku_name"])]
        items += [base.target(i) for i in range(2,311)]
        source=base.Source([items[:120],items[120:240],items[240:360],items[360:]])
        run=state()
        rows=pipeline.collect(source,base.Log(),run,lambda r:None)
        self.assertEqual([r["item"] for r in rows],[str(i) for i in range(1,301)])
        self.assertEqual([r["bsr_rank"] for r in rows],list(range(1,101))+[None]*200)
        self.assertEqual(run["counts"]["excluded"],160)
        compared=[v for c in source.calls if c[0]=="comparison" for v in c[1]]
        self.assertEqual(compared,["v"+str(i) for i in range(1,301)])

    def test_250_end_is_normal_actual_detail_progress_ends_at_250(self):
        source=base.Source([[base.target(i) for i in range(1,251)]])
        log,run=base.Log(),state()
        rows=pipeline.collect(source,log,run,lambda r:None)
        last=next(v for e,v in reversed(log.records) if e=="product_end")
        self.assertEqual((len(rows),run["end_reason"]),(250,"actual_search_end"))
        self.assertEqual((last["target"],last["goal"],last["remaining"],last["percent"]),(250,300,0,100.0))

    def test_missing_card_refills_and_records_failure_without_claiming_normality(self):
        source=base.Source([[base.target(i) for i in range(1,5)]])
        original=source.tiles
        source.tiles=lambda ids:{k:v for k,v in original(ids).items() if k!="v1"}
        run=state(2)
        rows=pipeline.collect(source,base.Log(),run,lambda r:None)
        self.assertEqual([r["item"] for r in rows],["2","3"])
        self.assertEqual(run["counts"]["item_failures"],1)

    def test_failure_during_listing_keeps_empty_csv_and_listing_evidence(self):
        source=base.Source([[base.target(1)],[base.target(2)]],failure=2)
        with tempfile.TemporaryDirectory() as root,patch("sys.stdout",new=io.StringIO()):
            db=MagicMock(return_value={})
            code,run=pipeline.execute(limit=2,output_root=root,client_factory=lambda e:source,database=db)
            self.assertEqual((code,run["output_rows"]),(3,0))
            self.assertFalse(run["listing_complete"])
            diag=[json.loads(x) for x in Path(run["diagnostics"]).read_text(encoding="utf-8").splitlines()]
            page=next(x for x in diag if x["event"]=="listing_candidates")
            self.assertEqual(page["candidates"][0]["product_id"],"1")
            self.assertEqual(db.call_count,1)
            self.assertFalse(any(c[0] in {"tiles","comparison","pdf","rating"} for c in source.calls))

    def test_detail_failure_preserves_each_completed_row_and_blocks_partial_load(self):
        source=base.Source([[base.target(i) for i in range(1,11)]])
        original=source.comparison
        def comparison(ids):
            if ids[0]=="v6":raise client.RequestFailure("comparison_test_abort")
            return original(ids)
        source.comparison=comparison
        with tempfile.TemporaryDirectory() as root,patch("sys.stdout",new=io.StringIO()):
            db=MagicMock(return_value={})
            code,run=pipeline.execute(limit=10,output_root=root,client_factory=lambda e:source,database=db)
            self.assertEqual((code,run["output_rows"]),(3,5))
            self.assertEqual(db.call_count,1)
            with self.assertRaisesRegex(storage.DryError,"output_not_completed"):
                storage.read_output(run["csv"])

    def test_candidates_are_reused_but_pdf_additions_are_recomputed(self):
        original=fields.candidates
        for needs_pdf,expected in ((False,1),(True,2)):
            source=base.Source([[base.target(1)]],{"v1":{}} if needs_pdf else None)
            with self.subTest(needs_pdf=needs_pdf),patch.object(pipeline,"candidates",wraps=original) as parsed, \
                    patch.object(fields,"candidates",side_effect=AssertionError("duplicate_candidate_parse")):
                rows=pipeline.collect(source,base.Log(),state(1),lambda r:None)
                self.assertEqual(parsed.call_count,expected)
                self.assertEqual(rows[0]["sku"],"PDF900" if needs_pdf else "DR0001")


class ReuseTests(base.Offline):
    def test_tile_cache_is_option_scoped_defensive_and_fresh_for_next_client(self):
        log=base.Log()
        c=client.Client(log)
        def fetch(url,endpoint,**kw):
            ids=base.parse_qs(base.urlsplit(url).query)["variationIds"][0].split(",")
            return [dict(variationId=v,name="Trockner DR900",price={"retailPrice":"100"},
                         topInfos=[dict(label="Nennkapazität",value="9 kg")]) for v in ids],{}
        with patch.object(c,"fetch",side_effect=fetch) as request:
            first=c.tiles(["v1"])
            first["v1"]["top_infos"]["Nennkapazität"]="WRONG"
            result=c.tiles(["v1","v2"])
            self.assertEqual(result["v1"]["top_infos"]["Nennkapazität"],"9 kg")
            self.assertEqual(request.call_count,2)
            self.assertEqual(request.call_args.args[0].split("variationIds=")[1],"v2")
            c.tiles(["v2","v1"])
            self.assertEqual(request.call_count,2)
        self.assertTrue(any(e=="cache_hit" and v["endpoint"]=="tiles" for e,v in log.records))
        fresh=client.Client(base.Log())
        with patch.object(fresh,"fetch",side_effect=fetch) as request:
            fresh.tiles(["v1"])
            self.assertEqual(request.call_count,1)

    def test_missing_tile_is_not_cached_as_success(self):
        c=client.Client(base.Log())
        with patch.object(c,"fetch",side_effect=[([],{}),([dict(variationId="v1",name="Trockner DR900")],{})]) as request:
            self.assertEqual(c.tiles(["v1"]),{})
            self.assertIn("v1",c.tiles(["v1"]))
            self.assertEqual(request.call_count,2)

    def test_comparison_cache_defensive_and_failures_remain_failed(self):
        c=client.Client(base.Log())
        c.compare_cache["v1"]={"Modellbezeichnung":"GOOD900"}
        result=c.comparison(["v1"])
        result["v1"]["Modellbezeichnung"]="WRONG"
        self.assertEqual(c.comparison(["v1"])["v1"]["Modellbezeichnung"],"GOOD900")
        with patch.object(c,"fetch",side_effect=client.RequestFailure("request_failed:comparison:timeout")) as request:
            first=c.comparison(["v2"])
            second=c.comparison(["v2"])
            self.assertEqual(request.call_count,1)
            self.assertEqual(first,second)
            self.assertIn("_error",second["v2"])

    def test_ratings_reuse_normal_and_zero_but_not_across_products(self):
        for mean,count in (("4,3","123"),("0","0")):
            with self.subTest(mean=mean):
                c=client.Client(base.Log())
                with patch.object(c,"fetch",return_value=(base.rating_document(mean,count),{"status":200})) as request:
                    first=c.rating("p1")
                    first["rating_count"]=999
                    second=c.rating("p1")
                    self.assertEqual(second["rating_count"],int(count))
                    c.rating("p2")
                    self.assertEqual(request.call_count,2)

    def test_failed_rating_retry_budget_is_not_reset_by_reuse(self):
        c=client.Client(base.Log(),attempts=2)
        with patch.object(c,"fetch",side_effect=[(base.rating_document(count="bad"),{"status":200}),client.RequestFailure("timeout")]) as request, \
                patch.object(client.time,"sleep"):
            first=c.rating("p1")
            second=c.rating("p1")
            self.assertEqual(request.call_count,2)
        self.assertEqual(first,second)
        self.assertEqual(second["rating_state"],"failed")
        self.assertEqual(second["average_rating"],"4.3")
        self.assertIsNone(second["rating_count"])
        self.assertNotIn("p1",c.rating_cache)

    def test_pdf_reuses_one_opened_document_without_sharing_models_or_needed_fields(self):
        c=client.Client(base.Log())
        calls=[]
        def attempt(uri,body,needed,timeout,parse_timeout,progress):
            calls.append((uri,body,dict(needed)))
            progress("pdf_opened")
            if needed["sku"]:return {"sku":"MODEL900"},b"%PDF-known"
            return {"capacity":"9 kg","capacity_raw":"9"},b"%PDF-known"
        with patch.object(client,"pdf_attempt",side_effect=attempt):
            first=c.pdf("https://d.otto.de/a.pdf",need_sku=True,need_capacity=False)
            second=c.pdf("https://d.otto.de/a.pdf",need_sku=False,need_capacity=True)
            c.pdf("https://d.otto.de/b.pdf",need_sku=False,need_capacity=True)
        self.assertEqual(first["sku"],"MODEL900")
        self.assertEqual(second["capacity"],"9 kg")
        self.assertNotIn("sku",second)
        self.assertEqual([v[1] for v in calls],[None,b"%PDF-known",None])
        self.assertTrue(calls[1][2]["capacity"])
        self.assertLessEqual(len(c._pdf_body),client.MAX_PDF_BYTES)

    def test_unreadable_pdf_is_not_a_successful_document_cache(self):
        c=client.Client(base.Log(),attempts=1)
        def attempt(uri,body,*args):
            self.assertIsNone(body)
            return {"_error":"pdf_open:ValueError","_error_kind":"extraction"},None
        with patch.object(client,"pdf_attempt",side_effect=attempt) as parse:
            c.pdf("https://d.otto.de/a.pdf",need_sku=True,need_capacity=False)
            c.pdf("https://d.otto.de/a.pdf",need_sku=True,need_capacity=False)
        self.assertEqual(parse.call_count,2)
        self.assertIsNone(c._pdf_body)


class EvidenceTests(base.Offline):
    def test_counts_use_original_response_without_extra_request(self):
        products=[dict(id=str(i),bestVariationId="v"+str(i),variationPath=f"/p/dryer-{i}/",name="Trockner DR900") for i in range(120)]
        data={"intents":[dict(intent="ranked",count=637,displayCount=597,products=products),
                         dict(intent="sponsored",products=products[:11])]}
        c=client.Client(base.Log())
        with patch.object(c,"fetch",return_value=(data,{})) as request:
            rows,consumed,end,_=c.page(1,0)
        self.assertEqual(request.call_count,1)
        self.assertEqual(c.last_page_meta,dict(count=637,display_count=597,organic_occurrences=120,sponsored_occurrences=11))
        self.assertEqual((len(rows),consumed,end),(131,109,False))

    def test_detailed_evidence_has_one_canonical_copy_and_request_statistics(self):
        with tempfile.TemporaryDirectory() as root,patch("sys.stdout",new=io.StringIO()):
            e=Events(Path(root),"batch")
            e.emit("request_start",endpoint="tiles",attempt=1)
            e.emit("parsed",endpoint="tiles",count=1)
            e.emit("cache_hit",endpoint="tiles",count=2)
            e.emit("field",item="1",field="sku",selected="GOOD900",source="comparison_model",
                   candidates=[dict(source="comparison_model",raw="GOOD900",normalized="GOOD900"),
                               dict(source="listing_title",raw="other evidence "*40,normalized=None)])
            e.emit("listing_candidates",page=1,candidates=[base.target(1)])
            e.emit("summary",batch_id="batch",output_rows=1)
            metrics=e.statistics()
            e.close()
            log=[json.loads(v) for v in (Path(root)/"run.log").read_text(encoding="utf-8").splitlines()]
            diag=[json.loads(v) for v in (Path(root)/"diagnostics.jsonl").read_text(encoding="utf-8").splitlines()]
        summary=next(v for v in log if v["event"]=="field")
        detail=next(v for v in diag if v["event"]=="field")
        self.assertNotIn("candidates",summary)
        self.assertEqual(len(detail["candidates"]),2)
        self.assertEqual(summary["diagnostic_record"],detail["record_id"])
        self.assertEqual(summary["raw"],"GOOD900")
        self.assertEqual(metrics["requests"],{"tiles":1})
        self.assertEqual(metrics["cache_hits"],{"tiles":2})
        self.assertEqual(metrics["parses"],{"tiles":1})


if __name__=="__main__":unittest.main()
