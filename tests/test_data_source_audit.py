import unittest
from pathlib import Path
from src.data_source_audit import (build_audit, quote_routes, tushare_permissions, render_markdown, BASELINE)

class SourceAuditTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.report = build_audit(Path(__file__).resolve().parents[1], points=120)
    def test_inventory(self):
        rows = {r["provider"]: r for r in self.report["provider_inventory"]}
        self.assertEqual(rows["baostock"]["capabilities"]["daily"]["implementation"], "DECLARED")
        self.assertEqual(rows["baostock"]["capabilities"]["chip"]["implementation"], "NOT_DECLARED")
        self.assertTrue(all(r["runtime_health"]=="NOT_OBSERVED" for r in rows.values()))
    def test_shared_dependency(self):
        self.assertTrue(any(r["dependency"]=="eastmoney" and {"akshare","efinance"} <= set(r["providers"]) for r in self.report["shared_dependencies"]))
    def test_quote_alias(self):
        r=quote_routes("tencent,akshare_qq,akshare_sina,unknown-secret-value")
        self.assertEqual(r[0]["provider"],"akshare")
        self.assertEqual(r[1]["status"],"DUPLICATE_ALIAS")
        self.assertNotIn("unknown-secret-value",str(r))
    def test_entitlement(self):
        rows={r["endpoint"]:r for r in tushare_permissions(120)}
        self.assertEqual(rows["daily"]["assessment"],"DOCUMENTED_THRESHOLD_MET")
        self.assertEqual(rows["daily_basic"]["assessment"],"BELOW_DOCUMENTED_THRESHOLD")
        self.assertEqual(rows["cyq_chips"]["assessment"],"UNKNOWN")
        self.assertTrue(all(r["account_entitlement"]=="UNVERIFIED" for r in rows.values()))
        with self.assertRaises(ValueError):
            tushare_permissions(-1)
    def test_adjustment(self):
        r=self.report["reviewed_paths"]
        self.assertTrue(any(x["provider"]=="tushare" and x["adjustment"]=="UNADJUSTED" for x in r))
        self.assertTrue(any(x["provider"]=="baostock" and x["adjustment"]=="QFQ_REQUESTED" for x in r))
    def test_report(self):
        self.assertEqual(self.report["accepted_baseline"],BASELINE)
        self.assertEqual(self.report["network_requests"],0)
        self.assertTrue(all(len(v)==64 for v in self.report["source_sha256"].values()))
        self.assertIn("Runtime health and entitlements are unverified",render_markdown(self.report))

if __name__=="__main__":
    unittest.main()
