import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
from src import provider_health_monitor as h
from src.services.run_diagnostics import record_provider_run, record_provider_run_started

def fixture(path):
    with patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":str(path)}):
        for category in h.CATEGORIES:
            for status in ("success","empty","timeout","permission_denied","rate_limited"):
                record_provider_run_started(data_type=category,provider="fixture",operation="fetch")
                record_provider_run(data_type=category,provider="fixture",operation="fetch",
                    success=status=="success",error_type=status)
        record_provider_run_started(data_type="market_indices",provider="fixture",operation="fetch")
        record_provider_run(data_type="market_indices",provider="fixture",operation="fetch",success=True,cache_hit=True)
        record_provider_run(data_type="market_indices",provider="fixture",operation="fetch",success=True)
        record_provider_run_started(data_type="market_indices",provider="fixture",operation="unfinished")
        record_provider_run_started(data_type="market_stats",provider="primary",operation="fetch")
        record_provider_run(data_type="market_stats",provider="primary",operation="fetch",success=False,error_type="timeout",fallback_to="backup")
        record_provider_run_started(data_type="market_stats",provider="backup",operation="fetch")
        record_provider_run(data_type="market_stats",provider="backup",operation="fetch",success=True)
    return h.build_report(path,profile="FIXTURE")

class HealthTests(unittest.TestCase):
    def test_all_categories(self):
        with tempfile.TemporaryDirectory() as d:
            report=fixture(Path(d)/"journal.jsonl")
            self.assertTrue(all(r["status"]=="OBSERVED" for r in report["categories"]))
            for r in report["providers"]:
                if r["provider"]=="fixture" and r["operation"]=="fetch":
                    self.assertEqual(r["completed"],5)
                    self.assertEqual(r["success_rate"],.2)
                    self.assertEqual(r["status_counts"]["ACCESS_DENIED"],1)
                    self.assertEqual(r["status_counts"]["RATE_LIMITED"],1)
            self.assertEqual(len(report["fallback_hints"]),1)
    def test_empty_and_malformed(self):
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"journal"
            self.assertFalse(h.build_report(p)["journal_present"])
            p.write_text("not json\n")
            self.assertEqual(h.build_report(p)["malformed_events"],1)
    def test_percentiles(self):
        self.assertEqual(h.percentile([10,20,30,40,50],.5),30)
        self.assertEqual(h.percentile([10,20,30,40,50],.95),50)
        self.assertIsNone(h.percentile([], .95))
    def test_observe_preserves_calls(self):
        with tempfile.TemporaryDirectory() as d, patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":str(Path(d)/"journal")}):
            from unittest.mock import Mock
            call=Mock(return_value=([],[]))
            self.assertEqual(h.observe_call("sector_rankings","fixture","fetch",call,ranking=True),([],[]))
            call.assert_called_once()
            call=Mock(side_effect=TimeoutError("secret=never-store"))
            with self.assertRaises(TimeoutError):
                h.observe_call("market_indices","fixture","fetch",call)
            self.assertNotIn("never-store",(Path(d)/"journal").read_text())
            self.assertEqual(h.classify(http_status=429),"RATE_LIMITED")
            self.assertEqual(h.classify(http_status=503),"UPSTREAM_ERROR")
    def test_disabled(self):
        with patch.dict(os.environ,{"DSA_PROVIDER_HEALTH_JOURNAL":""}):
            self.assertEqual(h.observe_call("market_stats","fixture","fetch",lambda:42),42)

def render_fixture(output):
    from src import financial_dashboard as fd
    p=Path(output); p.mkdir(parents=True,exist_ok=True)
    journal=p/"provider-health.jsonl"
    report=fixture(journal)
    h.write_report(journal,p/"provider-health.json",profile="FIXTURE")
    pages=fd.provider_health_pages(report,"2026-11-26")
    paths=fd.render_pages(pages,p)
    assert len(paths)==len(pages)

if __name__=="__main__":
    unittest.main()
