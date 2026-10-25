"""Offline 300-main / 100-BSR contracts, including real CSV and fake transactional DB."""
import copy
import csv
import io
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import test_dry as base
from dry import client, config, pipeline, run, storage


def source_with_count(count):
    products = [base.target(i) for i in range(1, count + 1)]
    return base.Source([products[i:i + 60] for i in range(0, count, 60)])


def database_for(connection):
    return lambda table, events, rows=None: storage.database(
        table, events, rows, connector=lambda: connection)


def saved_fixture(directory, count, status="complete"):
    rows = []
    for rank in range(1, count + 1):
        row = dict.fromkeys(config.COLUMNS)
        row.update(country=config.COUNTRY, account_name=config.ACCOUNT_NAME,
                   item=str(rank), retailer_sku_name=f"Trockner DR{rank:04}",
                   product_url=f"https://www.otto.de/p/dryer-{rank}/", batch_id="fixture",
                   main_rank=rank, bsr_rank=rank if rank <= 100 else None,
                   calendar_week="w43", crawl_datetime="2026-10-24 12:00:00")
        rows.append(row)
    state = dict(batch_id="fixture", target=count, collection_status=status)
    storage.checkpoint(Path(directory), rows, state)
    return rows, state


class LimitTests(base.Offline):
    def test_default_300_csv_db_reload_and_only_existing_requests(self):
        source = source_with_count(360)
        connection = base.FakeConnection()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(output_root=root, client_factory=lambda e: source,
                                           database=database_for(connection))
            self.assertEqual(code, 0)
            self.assertEqual((state["target"], state["output_rows"]), (300, 300))
            self.assertEqual(state["bsr_limit"], 100)
            self.assertEqual(state["end_reason"], "target_reached")
            self.assertIsNone(state["shortfall_reason"])
            rows, saved = storage.read_output(state["csv"])
            self.assertEqual(tuple(rows[0]), config.COLUMNS)
            self.assertEqual([r["main_rank"] for r in rows], [str(i) for i in range(1, 301)])
            self.assertEqual([r["bsr_rank"] for r in rows], [str(i) for i in range(1, 101)] + [""] * 200)
            self.assertTrue(all(r["id"] == "" for r in rows))
            self.assertEqual(saved["null_counts"]["bsr_rank"], 200)
            bsr_index = config.COLUMNS[1:].index("bsr_rank")
            self.assertEqual(connection.records[99][bsr_index], "100")
            self.assertTrue(all(r[bsr_index] is None for r in connection.records[100:]))
            self.assertEqual(len(connection.records), 300)
            # All listing pages first; only selected targets get detail requests.
            self.assertEqual([c[1] for c in source.calls if c[0] == "page"], [1, 2, 3, 4, 5, 6])
            for endpoint in ("tiles", "comparison"):
                ids = [vid for kind, *args in source.calls if kind == endpoint for vid in args[0]]
                self.assertEqual(ids, [f"v{i}" for i in range(1, 301)])
            self.assertEqual([c[1] for c in source.calls if c[0] == "rating"], [str(i) for i in range(1, 301)])
            self.assertEqual({c[0] for c in source.calls}, {"page", "tiles", "comparison", "rating"})
            log = [json.loads(line) for line in Path(state["log"]).read_text(encoding="utf-8").splitlines()]
            start = next(v for v in log if v["event"] == "start")
            self.assertEqual((start["target"], start["bsr_limit"]), (300, 100))
            no_client = MagicMock(side_effect=AssertionError("NO_RECRAWL"))
            code, reloaded = pipeline.execute(load_only=state["csv"], client_factory=no_client,
                                               database=database_for(connection))
            self.assertEqual(code, 0)
            self.assertEqual(reloaded["database"], dict(status="already_loaded", inserted=0, duplicates=300))
            self.assertEqual(reloaded["batch_id"], state["batch_id"])
            self.assertEqual(len(connection.records), 300)
            no_client.assert_not_called()

    def test_290_actual_end_is_normal_and_progress_is_not_faked(self):
        source = source_with_count(290)
        connection = base.FakeConnection()
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(limit=300, output_root=root, client_factory=lambda e: source,
                                           database=database_for(connection))
            self.assertEqual(code, 0)
            self.assertEqual(state["collection_status"], "complete")
            self.assertEqual(state["end_reason"], "actual_search_end")
            self.assertEqual(state["shortfall_reason"]["missing"], 10)
            self.assertEqual(state["counts"]["eligible"], 290)
            self.assertEqual(state["database"]["inserted"], 290)
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(len(rows), 290)
            self.assertEqual(sum(bool(r["bsr_rank"]) for r in rows), 100)
            log = [json.loads(line) for line in Path(state["log"]).read_text(encoding="utf-8").splitlines()]
            progress = [v for v in log if v["event"] == "product_end"][-1]
            self.assertEqual((progress["number"], progress["remaining"], progress["percent"]), (290, 0, 100.0))
            self.assertEqual(progress["goal"], 300)
            self.assertEqual(log[-1]["output_rows"], 290)

    def test_filters_duplicates_and_final_page_still_stop_at_300(self):
        products = [base.target(900, "Filter für Trockner"), base.target(1),
                    base.target(901, base.target(1)["retailer_sku_name"])]
        products += [base.target(i) for i in range(2, 310)]
        source = base.Source([products])
        state = dict(target=300, batch_id="fixture", calendar_week="w43", crawl_datetime="2026-10-24 12:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda r: None)
        self.assertEqual([r["item"] for r in rows], [str(i) for i in range(1, 301)])
        self.assertEqual(state["end_reason"], "target_reached")  # Last page was also marked as end.
        self.assertEqual((state["counts"]["excluded"], state["counts"]["duplicates"]), (1, 1))
        self.assertEqual([r["main_rank"] for r in rows], list(range(1, 301)))
        self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101)) + [None] * 200)

    def test_cli_default_environment_and_explicit_100_precedence(self):
        for environment, args, expected in (({}, [], 300), ({"OTTO_DRY_LIMIT": "250"}, [], 250),
                                             ({"OTTO_DRY_LIMIT": "300"}, ["--limit", "100"], 100)):
            with self.subTest(expected=expected), patch.dict(os.environ, environment, clear=True), \
                    patch.object(run, "execute", return_value=(0, {})) as execute:
                self.assertEqual(run.main(args), 0)
                self.assertEqual(execute.call_args.kwargs["limit"], expected)
                self.assertEqual(execute.call_args.kwargs["table"], config.DEFAULT_TABLE)

    def test_explicit_100_only_details_100_after_listing_snapshot(self):
        source = source_with_count(360)
        state = dict(target=100, batch_id="fixture", calendar_week="w43", crawl_datetime="2026-10-24 12:00:00")
        rows = pipeline.collect(source, base.Log(), state, lambda r: None)
        self.assertEqual(len(rows), 100)
        self.assertEqual([r["bsr_rank"] for r in rows], list(range(1, 101)))
        self.assertEqual([c[1] for c in source.calls if c[0] == "page"], [1, 2, 3, 4, 5, 6])
        self.assertEqual(sum(c[0] == "rating" for c in source.calls), 100)

    def test_invalid_limits_fail_before_site_database_or_artifacts(self):
        for limit in (0, -1, 301):
            with self.subTest(limit=limit), tempfile.TemporaryDirectory() as root:
                site, database = MagicMock(), MagicMock()
                with self.assertRaisesRegex(config.DryError, "limit_"):
                    pipeline.execute(limit=limit, output_root=root, client_factory=site, database=database)
                site.assert_not_called()
                database.assert_not_called()
                self.assertEqual(list(Path(root).iterdir()), [])

    def test_actual_end_does_not_hide_real_item_errors_or_reload_warnings(self):
        for issue in ("item_failures", "request_error_items", "extraction_error_items", "price_warning_items"):
            with self.subTest(issue=issue), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                source = source_with_count(3)
                if issue == "item_failures":
                    original = source.tiles
                    source.tiles = lambda ids: {k: v for k, v in original(ids).items() if k != "v1"}
                elif issue == "request_error_items":
                    source.overrides["v1"] = dict(base.chars(1), _error="comparison_timeout", _error_kind="request")
                elif issue == "extraction_error_items":
                    source.rating = lambda pid: dict(average_rating="4.3", rating_count=None,
                                                     rating_state="failed", rating_reason="rating_unconfirmed")
                else:
                    original = source.tiles
                    source.tiles = lambda ids: {k: dict(v, original_sku_price="599,00 €") for k, v in original(ids).items()}
                db = MagicMock(return_value={"status": "loaded"})
                code, state = pipeline.execute(output_root=root, client_factory=lambda e: source, database=db)
                self.assertEqual(code, 2)
                self.assertEqual(state["end_reason"], "actual_search_end")
                self.assertGreater(state["counts"][issue], 0)
                no_client = MagicMock(side_effect=AssertionError("NO_RECRAWL"))
                code, reloaded = pipeline.execute(load_only=state["csv"], client_factory=no_client, database=db)
                self.assertEqual(code, 2)
                self.assertEqual(reloaded["counts"], state["counts"])
                no_client.assert_not_called()

    def test_listing_failure_after_101_preserves_csv_without_inserting(self):
        source = base.Source([[base.target(i) for i in range(1, 102)], [base.target(102)]], failure=2)
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            db = MagicMock(return_value={})
            code, state = pipeline.execute(output_root=root, client_factory=lambda e: source, database=db)
            self.assertEqual(code, 3)
            self.assertEqual(state["collection_status"], "failed")
            self.assertNotIn("end_reason", state)
            self.assertEqual(state["output_rows"], 0)
            self.assertEqual(db.call_count, 1)
            with Path(state["csv"]).open(encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.DictReader(handle))
            self.assertEqual(rows, [])
            self.assertFalse(state["listing_complete"])
            with self.assertRaisesRegex(config.DryError, "output_not_completed"):
                storage.read_output(state["csv"])


class RankStorageTests(base.Offline):
    def test_old_100_complete_and_shortfall_states_remain_loadable(self):
        for status in ("complete", "shortfall"):
            with self.subTest(status=status), tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
                rows, state = saved_fixture(root, 100, status)
                if status == "shortfall":
                    state.update(target=120, end_reason="actual_search_end",
                                 shortfall_reason={"reason": "actual_search_end_after_filtering", "missing": 20})
                    storage.checkpoint(Path(root), rows, state)
                csv_path = Path(root) / "otto_dry.csv"
                self.assertEqual(len(storage.read_output(csv_path)[0]), 100)
                no_client = MagicMock(side_effect=AssertionError("NO_RECRAWL"))
                code, reloaded = pipeline.execute(load_only=csv_path, client_factory=no_client,
                                                   database=database_for(base.FakeConnection()))
                self.assertEqual(code, 0)
                self.assertEqual(reloaded["database"]["inserted"], 100)
                no_client.assert_not_called()

    def test_invalid_bsr_and_main_ranks_are_rejected_with_valid_checksum(self):
        with tempfile.TemporaryDirectory() as root:
            rows, state = saved_fixture(root, 300)
            for index, column, value in ((99, "bsr_rank", None), (100, "bsr_rank", 101),
                                         (100, "bsr_rank", 0), (100, "bsr_rank", 100),
                                         (299, "main_rank", 301)):
                with self.subTest(index=index, column=column, value=value):
                    invalid = copy.deepcopy(rows)
                    invalid[index][column] = value
                    storage.checkpoint(Path(root), invalid, state)
                    with self.assertRaisesRegex(config.DryError, "output_row_identity_or_rank_invalid"):
                        storage.read_output(Path(root) / "otto_dry.csv")
            saved_fixture(root, 301)
            with self.assertRaisesRegex(config.DryError, "output_row_count_invalid"):
                storage.read_output(Path(root) / "otto_dry.csv")

    def test_non_nullable_bsr_preflight_blocks_site_and_does_not_alter_schema(self):
        schema = [(c, t, "NO" if c == "bsr_rank" else null, default, identity, length, generated)
                  for c, t, null, default, identity, length, generated in base.schema_rows()]
        connection = base.FakeConnection(schema=schema)
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            no_client = MagicMock(side_effect=AssertionError("NO_SITE"))
            code, state = pipeline.execute(output_root=root, client_factory=no_client, database=database_for(connection))
            self.assertEqual(code, 4)
            self.assertEqual(state["failure"], "table_requires_nullable:bsr_rank")
            self.assertEqual(state["collection_status"], "not_started")
            self.assertFalse(any(any(word in sql for word in ("ALTER", "CREATE", "INSERT")) for sql, _ in connection.statements))
            no_client.assert_not_called()

    def test_numeric_bsr_null_and_failed_insert_keep_reloadable_csv(self):
        schema = [(c, "integer" if c == "bsr_rank" else t, null, default, identity, length, generated)
                  for c, t, null, default, identity, length, generated in base.schema_rows()]
        connection = base.FakeConnection(schema=schema, fail_insert=True)
        with tempfile.TemporaryDirectory() as root, patch("sys.stdout", new=io.StringIO()):
            code, state = pipeline.execute(limit=101, output_root=root,
                                           client_factory=lambda e: source_with_count(101), database=database_for(connection))
            self.assertEqual(code, 4)
            self.assertEqual(connection.records, [])
            rows, _ = storage.read_output(state["csv"])
            self.assertEqual(len(rows), 101)
            self.assertEqual(rows[100]["bsr_rank"], "")
            self.assertTrue(state["listing_complete"])
            inserted_values = next(values for sql, values in connection.statements if sql.startswith("INSERT"))
            index = config.COLUMNS[1:].index("bsr_rank")
            self.assertEqual(inserted_values[99][index], 100)
            self.assertIsNone(inserted_values[100][index])


if __name__ == "__main__":
    unittest.main()
