"""Production integration with synthetic history, SMTP and crawler responses only."""
import io
import os
from pathlib import Path
import tempfile
from unittest.mock import Mock, patch

import test_dry as base
from dry import config, history, notify, pipeline, run, storage


def current(**changes):
    row = base.DatabaseTests().row()
    row.update(item="1", retailer_sku_name="Brand Trockner DR1000",
               product_url="https://www.otto.de/p/dryer-1/?variationId=V1",
               batch_id="current", crawl_datetime="2026-10-28 12:00:00")
    row.update(changes)
    return row


def past(**changes):
    row = current(id=1, batch_id="previous", crawl_datetime="2026-10-27 12:00:00",
                  sku="MODEL900", capacity="9 kg", loading_type="Front loader")
    row.update(changes)
    return row


class HistoryConnection(base.FakeConnection):
    def __init__(self, history_rows=(), fail=False):
        super().__init__()
        self.history_rows, self.fail = list(history_rows), fail
        self.sessions = []
        self.closed = False

    def set_session(self, **kwargs):
        self.sessions.append(kwargs)

    def execute(self, sql, params=None):
        super().execute(sql, params)
        if sql.startswith('SELECT "id",'):
            if self.fail:
                raise RuntimeError("DO_NOT_EXPOSE_HISTORY_DETAILS")
            columns = [c.strip().strip('"') for c in sql.split(" FROM ")[0][len("SELECT "):].split(",")]
            self.result = [tuple(row.get(column) for column in columns) for row in self.history_rows]

    def close(self):
        self.closed = True


class HistoryTests(base.Offline):
    def fill(self, rows, records):
        connection, log = HistoryConnection(records), base.Log()
        result = history.backfill(config.DEFAULT_TABLE, log, rows, connector=lambda: connection)
        return result, connection, log

    def test_production_default_and_table_override_precedence(self):
        self.assertEqual(config.DEFAULT_TABLE, "dx_seg.dx_seg_ldy_dryer_retail")
        for env, args, expected in (({}, [], config.DEFAULT_TABLE),
                ({"OTTO_DRY_TABLE": "dx_seg.fixture"}, [], "dx_seg.fixture"),
                ({"OTTO_DRY_TABLE": "dx_seg.fixture"}, ["--table", "dx_seg.explicit"], "dx_seg.explicit")):
            with self.subTest(expected=expected), patch.dict(os.environ, env, clear=True), \
                    patch.object(run, "execute", return_value=(0, {})) as execute:
                self.assertEqual(run.main(args), 0)
                self.assertEqual(execute.call_args.kwargs["table"], expected)

    def test_current_values_win_and_only_three_fields_can_be_filled(self):
        row = current(sku="CURRENT900", final_sku_price=None, star_rating=None)
        result, connection, _ = self.fill([row], [past(final_sku_price="999", star_rating="5")])
        self.assertEqual(row["sku"], "CURRENT900")
        self.assertEqual((row["capacity"], row["loading_type"]), ("9 kg", "Front loader"))
        self.assertIsNone(row["final_sku_price"])
        self.assertIsNone(row["star_rating"])
        self.assertEqual(result["recovered_rows"], 1)
        self.assertEqual(result["recovered_fields"], {"capacity": 1, "loading_type": 1})
        query = next(sql for sql, _ in connection.statements if sql.startswith('SELECT "id",'))
        self.assertNotIn('"sku"', query)
        self.assertNotIn('"star_rating"', query)
        self.assertNotIn("_test", query)
        self.assertEqual(connection.sessions, [{"readonly": True}])

    def test_per_field_latest_valid_manual_history_and_rejected_short_sku(self):
        row = current()
        newer = past(id=2, batch_id="newer", sku="A1", capacity=None, loading_type="Top-loading")
        older = past(id=1, batch_id="manual", crawl_datetime="2026-10-26 00:00:00", capacity="2,5 kg")
        result, _, log = self.fill([row], [older, newer])
        self.assertEqual((row["sku"], row["capacity"], row["loading_type"]), ("MODEL900", "2.5 kg", "Top-loading"))
        self.assertEqual(result["recovered_rows"], 1)
        self.assertEqual(result["rejected"]["invalid_sku"], 1)
        selected = {value["field"]: value for event, value in log.records if event == "history_field"}
        self.assertEqual(selected["sku"]["source_batch"], "manual")
        self.assertEqual(selected["loading_type"]["source_batch"], "newer")

    def test_wrong_retailer_country_item_name_option_and_current_or_future_batch_rejected(self):
        bad = [past(account_name="MMKT"), past(country="OTHER"), past(item="2"),
               past(retailer_sku_name="Other dryer"),
               past(product_url="https://www.otto.de/p/dryer-1/?variationId=V2"),
               past(product_url="https://www.otto.de/p/dryer-1/"),
               past(batch_id="current"), past(batch_id=" current "), past(crawl_datetime="2026-10-29 00:00:00"),
               past(crawl_datetime="unknown"), past(batch_id="")]
        for record in bad:
            with self.subTest(record=record):
                row = current()
                result, _, _ = self.fill([row], [record])
                self.assertEqual(result["recovered_rows"], 0)
                self.assertTrue(all(row[field] is None for field in history.FIELDS))

    def test_ldy_normalization_but_query_identity_remains_option_specific(self):
        row = current()
        record = past(item=" 1 ", retailer_sku_name="Ｂrand   Trockner DR1000", account_name="otto", country="seg")
        result, _, _ = self.fill([row], [record])
        self.assertEqual(result["recovered_rows"], 1)

    def test_invalid_historical_fields_remain_null_and_keep_rank(self):
        row = current()
        result, _, _ = self.fill([row], [past(sku="ABC", capacity="2 5 kg", loading_type="Front loader / Top-loading")])
        self.assertTrue(all(row[field] is None for field in history.FIELDS))
        self.assertEqual((row["main_rank"], row["bsr_rank"]), (1, 1))
        self.assertEqual(result["recovered_rows"], 0)
        self.assertEqual(result["rejected"], {"invalid_sku": 1, "invalid_capacity": 1, "invalid_loading_type": 1})

    def test_no_lookup_when_values_complete_or_option_unconfirmed(self):
        for row in (current(sku="MODEL900", capacity="9 kg", loading_type="Front loader"),
                    current(product_url="https://www.otto.de/p/dryer-1/")):
            connector = Mock(side_effect=AssertionError("NO_CONNECTION"))
            result = history.backfill(config.DEFAULT_TABLE, base.Log(), [row], connector=connector)
            connector.assert_not_called()
            self.assertEqual(result["recovered_rows"], 0)

    def test_one_query_for_multiple_products_and_explicit_target_only(self):
        rows = [current(), current(item="2", retailer_sku_name="Other DR2000",
                                 product_url="https://www.otto.de/p/dryer-2/?variationId=V2")]
        connection = HistoryConnection()
        connector = Mock(return_value=connection)
        history.backfill("dx_seg.fixture", base.Log(), rows, connector=connector)
        connector.assert_called_once()
        queries = [(sql, params) for sql, params in connection.statements if sql.startswith('SELECT "id",')]
        self.assertEqual(len(queries), 1)
        self.assertIn('FROM "dx_seg"."fixture"', queries[0][0])
        self.assertEqual(queries[0][1][2], ["1", "2"])
        self.assertEqual(queries[0][1][3], ["current"])
        self.assertFalse(any(word in sql for sql, _ in connection.statements for word in ("INSERT", "UPDATE", "DELETE", "ALTER", "CREATE")))

    def test_query_failure_is_safe_and_preserves_current_values(self):
        row, log = current(sku="CURRENT900"), base.Log()
        before = dict(row)
        connection = HistoryConnection(fail=True)
        result = history.backfill(config.DEFAULT_TABLE, log, [row], connector=lambda: connection)
        self.assertEqual(row, before)
        self.assertEqual(result["status"], "failed")
        self.assertTrue(connection.closed)
        self.assertNotIn("DO_NOT_EXPOSE", str(result) + str(log.records))


class ProductionFlowTests(base.Offline):
    def source(self, request_error=False):
        source = base.Source([[base.target(1, "Brand Trockner")]])
        source.overrides["v1"] = {"_name": "Brand Trockner", **({"_error": "comparison_timeout", "_error_kind": "request"} if request_error else {})}
        source.pdf = Mock(return_value={})
        return source

    def history_for(self, source):
        return HistoryConnection([past(retailer_sku_name=source.pages[0][0]["retailer_sku_name"],
                                      crawl_datetime="2020-01-01 00:00:00")])

    def execute(self, root, source, db, historical, **kwargs):
        return pipeline.execute(limit=1, output_root=root, client_factory=lambda events: source,
            database=lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db),
            backfill=lambda table, events, rows: history.backfill(table, events, rows, connector=lambda: historical), **kwargs)

    def test_filled_csv_hash_db_diagnostics_and_reload_remain_identical(self):
        source, db = self.source(), base.FakeConnection()
        hist = self.history_for(source)
        notice = Mock(return_value={"status": "sent", "sent": True})
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = self.execute(root, source, db, hist, notifier=notice)
            self.assertEqual(code, 0)
            rows, saved = storage.read_output(state["csv"])
            self.assertEqual(tuple(rows[0]), config.COLUMNS)
            self.assertEqual(rows[0]["loading_type"], "Front loader")
            self.assertEqual((rows[0]["sku"], rows[0]["capacity"]), ("MODEL900", "9 kg"))
            self.assertEqual(saved["null_counts"]["loading_type"], 0)
            self.assertEqual(state["history_backfill"]["recovered_rows"], 1)
            self.assertEqual(state["history_backfill"]["recovered_fields"],
                             {"sku": 1, "capacity": 1, "loading_type": 1})
            for field in history.FIELDS:
                self.assertEqual(db.records[0][config.COLUMNS[1:].index(field)], rows[0][field])
            self.assertEqual(db.records[0][config.COLUMNS[1:].index("loading_type")], "Front loader")
            self.assertEqual((rows[0]["main_rank"], rows[0]["bsr_rank"]), ("1", "1"))
            self.assertEqual(set(Path(state["csv"]).parent.iterdir()),
                             {Path(state["csv"]).parent / f for f in ("otto_dry.csv", "state.json", "run.log", "diagnostics.jsonl")})
            notice.assert_called_once()
            before = Path(state["csv"]).read_bytes()
            no_history, no_client = Mock(side_effect=AssertionError("NO_HISTORY")), Mock(side_effect=AssertionError("NO_SITE"))
            repeat, repeat_state = pipeline.execute(load_only=state["csv"], client_factory=no_client,
                database=lambda table, events, rows=None: storage.database(table, events, rows, connector=lambda: db),
                backfill=no_history, notifier=notice)
            self.assertEqual(repeat, 0)
            self.assertEqual(repeat_state["database"], dict(status="already_loaded", inserted=0, duplicates=1))
            self.assertEqual(Path(state["csv"]).read_bytes(), before)
            self.assertEqual(notice.call_count, 2)  # Exactly one per invocation, not per database call.
            no_history.assert_not_called()
            no_client.assert_not_called()

    def test_insert_failure_preserves_filled_reloadable_csv_and_sends_failure_report(self):
        source, db = self.source(), base.FakeConnection(fail_insert=True)
        report = []
        def notifier(state, rows):
            report.append(notify.build_report(state, rows))
            return dict(status="sent", sent=True)
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = self.execute(root, source, db, self.history_for(source), notifier=notifier)
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(code, 4)
            self.assertEqual(db.records, [])
            self.assertEqual(rows[0]["loading_type"], "Front loader")
            self.assertIn("실패", report[0][0])
            self.assertNotIn("DO_NOT_EXPOSE", str(state) + str(report))

    def test_history_failure_does_not_stop_insert_but_reports_warning(self):
        source, db = self.source(), base.FakeConnection()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = self.execute(root, source, db, HistoryConnection(fail=True))
            self.assertEqual(code, 2)
            self.assertEqual(state["db_status"], "loaded")
            self.assertEqual(len(db.records), 1)
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(rows[0]["loading_type"], "")
            self.assertIsNone(db.records[0][config.COLUMNS[1:].index("loading_type")])

    def test_site_errors_survive_successful_history_backfill(self):
        source, db = self.source(request_error=True), base.FakeConnection()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = self.execute(root, source, db, self.history_for(source))
            self.assertEqual(code, 2)
            self.assertEqual(state["counts"]["request_error_items"], 1)
            self.assertEqual(state["history_backfill"]["recovered_rows"], 1)
            self.assertEqual(state["database"]["inserted"], 1)

    def test_history_and_mail_add_no_site_requests_or_candidate_recalculations(self):
        sources = [self.source(), self.source()]
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            counts = []
            for index, source in enumerate(sources):
                from dry.fields import candidates
                with patch.object(pipeline, "candidates", wraps=candidates) as calculations:
                    self.execute(Path(root) / str(index), source, base.FakeConnection(),
                                 self.history_for(source) if index else HistoryConnection())
                    counts.append(calculations.call_count)
            self.assertEqual(sources[0].calls, sources[1].calls)
            self.assertEqual(sources[0].pdf.call_args_list, sources[1].pdf.call_args_list)
            self.assertEqual(counts[0], counts[1])

    def test_collection_and_preflight_failures_still_notify_once(self):
        for phase in ("preflight", "collection"):
            with self.subTest(phase=phase), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                report = []
                notifier = lambda state, rows: report.append(notify.build_report(state, rows)) or dict(status="sent")
                database = Mock(side_effect=config.DryError("preflight_failed")) if phase == "preflight" else Mock(return_value={})
                source = base.Source([[base.target(1)]], failure=1)
                code, state = pipeline.execute(output_root=root, client_factory=lambda e: source,
                                               database=database, notifier=notifier)
                self.assertEqual(code, 4 if phase == "preflight" else 3)
                self.assertEqual(len(report), 1)
                self.assertIn("실패", report[0][0])
                database.assert_called_once()

    def test_mail_failure_preserves_db_result_and_original_error_code(self):
        for db_fails in (False, True):
            with self.subTest(db_fails=db_fails), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                source, db = self.source(), base.FakeConnection(fail_insert=db_fails)
                notifier = Mock(side_effect=RuntimeError("DO_NOT_EXPOSE_EMAIL_DETAILS"))
                code, state = self.execute(root, source, db, self.history_for(source), notifier=notifier)
                self.assertEqual(code, 4 if db_fails else 6)
                self.assertEqual(state["collection_exit_code"], 4 if db_fails else 0)
                self.assertEqual(len([sql for sql, _ in db.statements if sql.startswith("INSERT")]), 1)
                self.assertEqual(len(db.records), 0 if db_fails else 1)
                self.assertNotIn("DO_NOT_EXPOSE", str(state))
                notifier.assert_called_once()


class NotificationTests(base.Offline):
    def state(self, **changes):
        state = dict(batch_id="fixture", started_at="2026-10-28 12:00:00", table=config.DEFAULT_TABLE,
                     collection_exit_code=0, mode="collect", target=300, end_reason="actual_search_end",
                     db_status="loaded", database=dict(inserted=290, duplicates=0), total_seconds=123.4,
                     durations={"사양·평점 수집": 120}, counts=dict(excluded=20, duplicates=10))
        state.update(changes)
        return state

    def test_actual_end_and_nulls_are_information_not_failures(self):
        rows = [current(main_rank=i, bsr_rank=i if i <= 100 else None) for i in range(1, 291)]
        subject, body = notify.build_report(self.state(), rows)
        self.assertIn("수집 완료", subject)
        self.assertNotIn("확인 필요", subject)
        self.assertIn("290행 / 목표 최대 300행", body)
        self.assertIn("main 290개 / BSR 100개", body)
        self.assertIn("실제 검색 끝까지 확인", body)
        self.assertIn("NULL 290", body)

    def test_disabled_and_dry_run_do_not_send(self):
        sender = Mock(side_effect=AssertionError("NO_SMTP"))
        for values, status in (({}, "disabled"), ({"SEG_EMAIL_NOTIFY": "1", "SEG_EMAIL_DRY_RUN": "1"}, "dry_run")):
            with patch.object(notify, "env_value", side_effect=lambda name, default=None: values.get(name, default)):
                self.assertEqual(notify.send(self.state(), [], sender=sender)["status"], status)
        sender.assert_not_called()

    def test_reuses_existing_transport_once_and_never_exposes_error_text(self):
        settings = lambda name, default=None: "1" if name == "SEG_EMAIL_NOTIFY" else default
        with patch.object(notify, "env_value", side_effect=settings), \
                patch("common.notify._send", return_value=(True, 2, None)) as sender:
            result = notify.send(self.state(), [current()])
            self.assertEqual((result["status"], result["recipients_count"]), ("sent", 2))
            sender.assert_called_once()
        with patch.object(notify, "env_value", side_effect=settings):
            sender = Mock(return_value=(False, 1, "DO_NOT_EXPOSE_SMTP_RESPONSE"))
            result = notify.send(self.state(), [], sender=sender)
            self.assertEqual(result["error"], "smtp_send_failed")
            self.assertNotIn("DO_NOT_EXPOSE", str(result))
            sender.assert_called_once()

    def test_reload_report_and_history_warning(self):
        state = self.state(mode="load_only", collection_exit_code=2,
                           history_backfill=dict(status="failed", error="history_query_failed:TimeoutError"))
        subject, body = notify.build_report(state, [current()])
        self.assertIn("CSV 재적재 확인 필요", subject)
        self.assertIn("재조회하지 않음", body)
        self.assertIn("DB 보충 오류", body)
