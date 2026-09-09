r"""Human-readable MediaMarkt Sponsored listing smoke test.

This diagnostic does not import product config, write production CSVs, save to
a database, send email, or visit product-detail pages. It uses the same browser
session and ``parse_listing_html`` function as the production Main collector,
then stores isolated evidence under ``MMKT/test_outputs``.

Examples:
    python MMKT\tools\smoke_test_sponsored_listing.py --product all --pages 3
    python MMKT\tools\smoke_test_sponsored_listing.py --product tv --pages 3
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import socket
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path
from typing import Any, TextIO
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit


MMKT_ROOT = Path(__file__).resolve().parents[1]
if str(MMKT_ROOT) not in sys.path:
    sys.path.insert(0, str(MMKT_ROOT))

from common.parsers import extract_sponsored_diagnostics, parse_listing_html  # noqa: E402


MAIN_URLS = {
    "tv": "https://www.mediamarkt.de/de/category/fernseher-nach-gr%C3%B6%C3%9Fen-4708.html",
    "ref": "https://www.mediamarkt.de/de/category/k%C3%BChlschr%C3%A4nke-33.html",
    "ldy": "https://www.mediamarkt.de/de/category/waschmaschinen-3.html",
}

CSV_COLUMNS = [
    "product", "page", "listing_position", "section", "card_type", "sku_id",
    "title", "final_price", "original_price", "sponsored", "product_url",
]

CHROME_EXE = Path(r"C:\Program Files\Google\Chrome\Application\chrome.exe")


class StandardChromeSession:
    """Chrome launched normally, then attached through a test-only debug port."""

    def __init__(self, profile_dir: Path, *, nav_timeout_s: int = 70) -> None:
        self.profile_dir = profile_dir
        self.nav_timeout_s = nav_timeout_s
        self.driver = None
        self.process: subprocess.Popen | None = None
        self.warmup_status: dict[str, Any] = {}

    @staticmethod
    def _free_port() -> int:
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
            sock.bind(("127.0.0.1", 0))
            return int(sock.getsockname()[1])

    @staticmethod
    def _blocked(src: str) -> bool:
        head = (src or "")[:12000].lower()
        return any(marker in head for marker in (
            "nur einen moment", "just a moment", "ein mensch sind",
            "nachfolgendes captcha", "verifiziere",
        ))

    def open(self) -> None:
        if not CHROME_EXE.is_file():
            raise FileNotFoundError(f"Chrome executable not found: {CHROME_EXE}")
        from selenium import webdriver

        self.profile_dir.mkdir(parents=True, exist_ok=True)
        port = self._free_port()
        started = time.perf_counter()
        self.process = subprocess.Popen(
            [
                str(CHROME_EXE),
                f"--remote-debugging-port={port}",
                f"--user-data-dir={self.profile_dir}",
                "--no-first-run",
                "--no-default-browser-check",
                "about:blank",
            ],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        options = webdriver.ChromeOptions()
        options.debugger_address = f"127.0.0.1:{port}"
        last_error: Exception | None = None
        for _ in range(30):
            try:
                self.driver = webdriver.Chrome(options=options)
                break
            except Exception as exc:
                last_error = exc
                time.sleep(1.0)
        if self.driver is None:
            raise RuntimeError(f"could not attach Selenium to standard Chrome: {last_error}")
        self.driver.set_page_load_timeout(self.nav_timeout_s)
        self.warmup_status = {
            "mode": "standard-chrome-remote-attach",
            "debug_port": port,
            "profile_dir": str(self.profile_dir),
            "open_seconds": round(time.perf_counter() - started, 2),
        }

    def navigate(self, url: str, *, settle_s: float = 15.0) -> dict[str, Any]:
        html = ""
        error = None
        try:
            self.driver.get(url)
            time.sleep(settle_s)
            html = self.driver.page_source or ""
        except Exception as exc:
            error = type(exc).__name__ + ": " + str(exc)
            try:
                html = self.driver.page_source or ""
            except Exception:
                pass
        return {"html": html, "blocked": self._blocked(html), "error": error, "url": url}

    def close(self) -> None:
        if self.driver is not None:
            try:
                self.driver.quit()
            except Exception:
                pass
            self.driver = None
        if self.process is not None and self.process.poll() is None:
            try:
                self.process.terminate()
                self.process.wait(timeout=5)
            except Exception:
                pass
        self.process = None


class Reporter:
    def __init__(self, path: Path) -> None:
        self._file: TextIO = path.open("w", encoding="utf-8")

    def write(self, message: str = "") -> None:
        print(message, flush=True)
        print(message, file=self._file, flush=True)

    def close(self) -> None:
        self._file.close()


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Test the current MediaMarkt Main Sponsored parser without DB/PDP/email."
    )
    parser.add_argument("--product", choices=["all", *MAIN_URLS], default="all")
    parser.add_argument("--pages", type=int, default=3)
    parser.add_argument(
        "--render-settle", type=float, default=15.0,
        help="seconds to wait for lazy Sponsored cards (production default: 15)",
    )
    parser.add_argument(
        "--sponsored-extra-wait", type=float, default=20.0,
        help="extra seconds when Gesponsert placeholders still have no product link",
    )
    parser.add_argument("--headless", action="store_true", help="hide Chrome window")
    parser.add_argument(
        "--browser", choices=["standard", "uc"], default="standard",
        help="standard attaches to a normally launched Chrome; uc mirrors the legacy transport",
    )
    parser.add_argument(
        "--captcha-timeout", type=float, default=600.0,
        help="seconds to keep the visible browser open for a human CAPTCHA check",
    )
    parser.add_argument("--output-dir", default="")
    args = parser.parse_args()
    if args.pages < 1:
        parser.error("--pages must be at least 1")
    if args.render_settle < 0:
        parser.error("--render-settle cannot be negative")
    if args.sponsored_extra_wait < 0:
        parser.error("--sponsored-extra-wait cannot be negative")
    if args.captcha_timeout < 0:
        parser.error("--captcha-timeout cannot be negative")
    return args


def listing_page_url(base_url: str, page: int) -> str:
    parts = urlsplit(base_url)
    if parts.scheme != "https" or parts.netloc != "www.mediamarkt.de" or "/category/" not in parts.path:
        raise ValueError(f"detail/non-MediaMarkt URL is forbidden: {base_url}")
    query = [(key, value) for key, value in parse_qsl(parts.query) if key.lower() != "page"]
    if page > 1:
        query.append(("page", str(page)))
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), ""))


def row_for_report(product: str, page: int, position: int, row: dict[str, Any]) -> dict[str, Any]:
    return {
        "product": product.upper(),
        "page": page,
        "listing_position": position,
        "section": "상품 목록",
        "card_type": row.get("listing_card_type") or "state-only",
        "sku_id": row.get("sku_id") or "",
        "title": row.get("retailer_sku_name") or "",
        "final_price": row.get("final_sku_price") or "",
        "original_price": row.get("original_sku_price") or "",
        "sponsored": "Sponsored" if row.get("sku_status") == "Sponsored" else "-",
        "product_url": row.get("product_url") or "",
    }


def verdict_for_page(
    *, blocked: bool, error: str | None, html: str,
    rows: list[dict[str, Any]], diagnostics: dict[str, Any],
) -> tuple[str, list[str]]:
    failures: list[str] = []
    warnings: list[str] = []
    if blocked:
        failures.append("Cloudflare/CAPTCHA 차단")
    if error:
        failures.append(f"브라우저 오류: {error}")
    if "__PRELOADED_STATE__" not in html:
        failures.append("__PRELOADED_STATE__ 없음")
    if not rows:
        failures.append("파싱 상품 0건")
    listing_labels = int(diagnostics.get("visible_label_occurrences") or 0)
    parsed_sponsored = int(diagnostics.get("parsed_sponsored_rows") or 0)
    if listing_labels and parsed_sponsored == 0:
        failures.append("상품 목록에 Gesponsert가 있지만 Sponsored 연결 0건")
    if diagnostics.get("unmatched_sponsored_ids"):
        failures.append("Sponsored ID가 최종 상품 행에 연결되지 않음")
    unmapped = int(diagnostics.get("unmapped_label_occurrences") or 0)
    if unmapped:
        warnings.append(f"상품 ID를 연결하지 못한 목록 라벨 {unmapped}건")
    sponsored_rows = [row for row in rows if row.get("sku_status") == "Sponsored"]
    incomplete = [
        str(row.get("sku_id") or "(ID 없음)") for row in sponsored_rows
        if not row.get("retailer_sku_name") or not row.get("final_sku_price") or not row.get("product_url")
    ]
    if incomplete:
        warnings.append("Sponsored 제목/금액/URL 불완전: " + ", ".join(incomplete))
    if failures:
        return "FAIL", failures + warnings
    if warnings:
        return "WARN", warnings
    return "PASS", []


def navigate_with_human_clearance(
    session: Any,
    url: str,
    *,
    render_settle: float,
    captcha_timeout: float,
    reporter: Reporter,
) -> dict[str, Any]:
    """Navigate once, pausing on the same page while a human clears CAPTCHA."""
    response = session.navigate(url, settle_s=render_settle)
    response["manual_clearance_used"] = False
    if not response.get("blocked"):
        return response

    response["manual_clearance_used"] = True
    reporter.write(
        f"[사람 확인 대기] Chrome에서 봇 체크를 완료해 주세요. "
        f"최대 {captcha_timeout:.0f}초 동안 같은 페이지를 유지합니다."
    )
    deadline = time.monotonic() + captcha_timeout
    next_notice = time.monotonic() + 10.0
    while time.monotonic() < deadline:
        html = session.driver.page_source or ""
        if not session._blocked(html) and "__PRELOADED_STATE__" in html:
            reporter.write(
                f"[사람 확인 완료] 정상 listing을 확인했습니다. "
                f"동적 Sponsored 로딩을 {render_settle:.1f}초 더 기다립니다."
            )
            time.sleep(render_settle)
            html = session.driver.page_source or ""
            return {
                "html": html,
                "blocked": session._blocked(html),
                "error": None,
                "url": url,
                "manual_clearance_used": True,
            }
        now = time.monotonic()
        if now >= next_notice:
            remaining = max(0, int(deadline - now))
            reporter.write(f"[사람 확인 대기 중] 남은 시간 약 {remaining}초")
            next_notice = now + 10.0
        time.sleep(2.0)

    html = session.driver.page_source or ""
    reporter.write("[사람 확인 시간 초과] CAPTCHA가 해제되지 않았습니다.")
    return {
        "html": html,
        "blocked": session._blocked(html),
        "error": "manual CAPTCHA timeout",
        "url": url,
        "manual_clearance_used": True,
    }


def wait_for_sponsored_placeholders(
    session: Any,
    response: dict[str, Any],
    *,
    extra_wait: float,
    reporter: Reporter,
) -> dict[str, Any]:
    """Match production: allow unresolved listing ad slots to finish once."""
    response["sponsored_extra_wait"] = 0.0
    html = response.get("html") or ""
    if response.get("blocked") or "__PRELOADED_STATE__" not in html:
        return response
    early = extract_sponsored_diagnostics(html)
    unresolved = int(early.get("unmapped_label_occurrences") or 0)
    if not unresolved or extra_wait <= 0:
        return response
    reporter.write(
        f"[Sponsored 슬롯 대기] 상품 링크 없는 Gesponsert {unresolved}건: "
        f"{extra_wait:.1f}초 추가 대기"
    )
    time.sleep(extra_wait)
    html = session.driver.page_source or ""
    response.update({
        "html": html,
        "blocked": session._blocked(html),
        "sponsored_extra_wait": extra_wait,
    })
    return response


def main() -> int:
    if hasattr(sys.stdout, "buffer"):
        sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    args = parse_args()
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    output_root = (
        Path(args.output_dir).resolve() if args.output_dir
        else MMKT_ROOT / "test_outputs" / f"sponsored_listing_{stamp}"
    )
    output_root.mkdir(parents=True, exist_ok=False)
    reporter = Reporter(output_root / "summary.txt")
    products = list(MAIN_URLS) if args.product == "all" else [args.product]
    page_results: list[dict[str, Any]] = []
    csv_rows: list[dict[str, Any]] = []
    failure_count = 0

    reporter.write("MediaMarkt Main Sponsored 수집 스모크 테스트")
    reporter.write(f"시작 시각: {datetime.now().isoformat(timespec='seconds')}")
    reporter.write(f"대상: {', '.join(name.upper() for name in products)} / 각 {args.pages}페이지")
    reporter.write(f"렌더링 대기: {args.render_settle:.1f}초 (운영 기본값과 동일)")
    reporter.write(f"빈 Sponsored 슬롯 추가 대기: 최대 {args.sponsored_extra_wait:.1f}초")
    reporter.write(f"브라우저 방식: {args.browser}")
    reporter.write("금지 작업: 상세페이지 방문 / DB 적재 / 운영 CSV 변경 / 메일 발송 / config·.env 로드")
    reporter.write(f"결과 폴더: {output_root}")

    if args.browser == "standard":
        if args.headless:
            raise ValueError("standard Chrome 사람 확인 모드에서는 --headless를 사용할 수 없습니다")
        session: Any = StandardChromeSession(output_root / "chrome_profile")
    else:
        from common.uc import UcSession
        session = UcSession(headless=args.headless)
    try:
        session.open()
        reporter.write(f"브라우저 준비: {session.warmup_status}")
        for product in products:
            product_dir = output_root / product
            product_dir.mkdir(parents=True, exist_ok=True)
            for page in range(1, args.pages + 1):
                url = listing_page_url(MAIN_URLS[product], page)
                reporter.write()
                reporter.write("=" * 110)
                reporter.write(f"[{product.upper()} / Main / Page {page}] {url}")
                started = time.perf_counter()
                response = navigate_with_human_clearance(
                    session, url, render_settle=args.render_settle,
                    captcha_timeout=args.captcha_timeout, reporter=reporter,
                )
                response = wait_for_sponsored_placeholders(
                    session, response, extra_wait=args.sponsored_extra_wait, reporter=reporter,
                )
                elapsed = round(time.perf_counter() - started, 2)
                html = response.get("html") or ""
                html_path = product_dir / f"page_{page:02d}.html"
                html_path.write_text(html, encoding="utf-8")

                diagnostics: dict[str, Any] = {}
                rows = parse_listing_html(html, page=page, diagnostics=diagnostics)
                verdict, reasons = verdict_for_page(
                    blocked=bool(response.get("blocked")), error=response.get("error"),
                    html=html, rows=rows, diagnostics=diagnostics,
                )
                if verdict == "FAIL":
                    failure_count += 1

                report_rows = [
                    row_for_report(product, page, position, row)
                    for position, row in enumerate(rows, start=1)
                ]
                csv_rows.extend(report_rows)
                sponsored_rows = [row for row in report_rows if row["sponsored"] == "Sponsored"]
                result = {
                    "product": product.upper(), "page": page, "url": url,
                    "elapsed_seconds": elapsed, "blocked": bool(response.get("blocked")),
                    "manual_clearance_used": bool(response.get("manual_clearance_used")),
                    "sponsored_extra_wait": float(response.get("sponsored_extra_wait") or 0),
                    "browser_error": response.get("error"), "html_file": str(html_path),
                    "verdict": verdict, "reasons": reasons, "parsed_rows": len(rows),
                    "sponsored_rows": len(sponsored_rows), "diagnostics": diagnostics,
                    "rows": report_rows,
                }
                page_results.append(result)
                (product_dir / f"page_{page:02d}_report.json").write_text(
                    json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8"
                )

                reporter.write(
                    "상태={verdict} | 소요={elapsed:.2f}초 | SSR={ssr} | 렌더링 상품={rendered} | "
                    "최종 상품={total} | 목록 라벨={labels} | Sponsored={sponsored} | "
                    "미연결 라벨={unmapped} | 목록 밖 라벨={outside}".format(
                        verdict=verdict, elapsed=elapsed,
                        ssr=diagnostics.get("state_listing_rows", 0),
                        rendered=diagnostics.get("rendered_listing_rows", 0), total=len(rows),
                        labels=diagnostics.get("visible_label_occurrences", 0),
                        sponsored=len(sponsored_rows),
                        unmapped=diagnostics.get("unmapped_label_occurrences", 0),
                        outside=diagnostics.get("outside_product_list_label_occurrences", 0),
                    )
                )
                for reason in reasons:
                    reporter.write(f"판정 사유: {reason}")
                reporter.write("상품 목록 (브라우저 표시 순서)")
                for row in report_rows:
                    reporter.write(
                        "{pos:03d} | 구역={section} | 카드={card} | 제목={title} | 금액={price} | "
                        "Sponsored={sponsored} | ID={sku_id} | URL={url}".format(
                            pos=row["listing_position"], section=row["section"],
                            card=row["card_type"], title=row["title"] or "(제목 없음)",
                            price=row["final_price"] or "(금액 없음)",
                            sponsored=row["sponsored"], sku_id=row["sku_id"] or "(ID 없음)",
                            url=row["product_url"] or "(URL 없음)",
                        )
                    )
    finally:
        session.close()

    with (output_root / "all_listing_rows.csv").open("w", encoding="utf-8-sig", newline="") as file:
        writer = csv.DictWriter(file, fieldnames=CSV_COLUMNS)
        writer.writeheader()
        writer.writerows(csv_rows)
    summary = {
        "created_at": datetime.now().isoformat(timespec="seconds"),
        "purpose": "verify current Main parser collects and maps rendered Sponsored products",
        "products": [name.upper() for name in products],
        "pages_per_product": args.pages,
        "render_settle_seconds": args.render_settle,
        "sponsored_extra_wait_seconds": args.sponsored_extra_wait,
        "browser_mode": args.browser,
        "captcha_timeout_seconds": args.captcha_timeout,
        "page_count": len(page_results),
        "pass_count": sum(result["verdict"] == "PASS" for result in page_results),
        "warn_count": sum(result["verdict"] == "WARN" for result in page_results),
        "fail_count": sum(result["verdict"] == "FAIL" for result in page_results),
        "total_rows": len(csv_rows),
        "total_sponsored_rows": sum(row["sponsored"] == "Sponsored" for row in csv_rows),
        "pages": page_results,
    }
    (output_root / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    reporter.write()
    reporter.write("=" * 110)
    reporter.write(
        f"최종 결과: PASS={summary['pass_count']} WARN={summary['warn_count']} "
        f"FAIL={summary['fail_count']} / 상품={summary['total_rows']}건 / "
        f"Sponsored={summary['total_sponsored_rows']}건"
    )
    reporter.write(f"상세 텍스트: {output_root / 'summary.txt'}")
    reporter.write(f"전체 CSV: {output_root / 'all_listing_rows.csv'}")
    reporter.write(f"원본 HTML 및 페이지별 JSON: {output_root}")
    reporter.close()
    return 1 if failure_count else 0


if __name__ == "__main__":
    raise SystemExit(main())
