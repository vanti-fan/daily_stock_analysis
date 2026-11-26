import csv
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from src.raw_data_archive import archive_inputs, archive_research, archive_market, verify, write_index, record


class RawArchiveTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.rows = [{"code":"002938","date":"2026-11-26","open":47,"high":47.41,
                      "low":45.2,"close":45.8,"volume":28849300,"amount":1326000000,
                      "data_source":"TushareFetcher"}]
        self.kw = dict(code="002938",name="鹏鼎控股",market="cn",query_id="test",rows=self.rows)

    def test_inputs_and_manifest(self):
        p=archive_inputs(self.root,**self.kw)
        with (p/"ohlcv_daily.csv").open(encoding="utf-8") as f:
            row=next(csv.DictReader(f))
        self.assertEqual(row["code"],"002938")
        self.assertEqual(row["volume"],"28849300")
        self.assertEqual(row["data_source"],"TushareFetcher")
        manifest=json.loads((p/"manifest.json").read_text())
        self.assertIsNone(manifest["metadata"]["history_adjustment"])
        self.assertEqual(manifest["metadata"]["history_count"],1)
        self.assertIsNone(json.loads((p/"chip.json").read_text()))
        self.assertEqual(len(verify(self.root)),1)

    def test_missing_and_invalid_model(self):
        p=archive_inputs(self.root,**dict(self.kw,rows=self.rows*2))
        self.assertTrue((p/"ohlcv_daily.csv").exists())
        self.assertFalse((p/"financial_data.json").exists())
        self.assertEqual(json.loads((p/"manifest.json").read_text())["metadata"]["normalization_status"],"INVALID")

    def test_immutable_and_integrity(self):
        p=archive_inputs(self.root,**self.kw)
        q=archive_inputs(self.root,**self.kw)
        self.assertNotEqual(p,q)
        self.assertEqual(write_index(self.root),2)
        (p/"quote.json").write_text("tampered")
        with self.assertRaises(ValueError):
            verify(self.root)

    def test_research_separation_and_filter(self):
        p=archive_research(self.root,code="002938",query_id="test",result={
            "success":True,"model_used":"fixture","dashboard":{"price":999,"api_key":"secret"},
            "portfolio_context":{"cash":123}})
        data=json.loads((p/"ai_research.json").read_text())
        self.assertEqual(data["dashboard"]["price"],999)
        self.assertNotIn("api_key",data["dashboard"])
        self.assertNotIn("portfolio_context",data)
        self.assertEqual(data["model_used"],"fixture")

    def test_market_missing_breadth_and_reused(self):
        p=archive_market(self.root,region="cn",query_id="test",
                         payload={"region":"cn","indices":[{"name":"fixture","close":1}],"markdown_report":"test"})
        self.assertFalse(json.loads((p/"manifest.json").read_text())["metadata"]["breadth_available"])
        p=archive_market(self.root,region="cn",query_id="test",payload={"markdown_report":"reused"},reused=True)
        m=json.loads((p/"manifest.json").read_text())["metadata"]
        self.assertTrue(m["reused"])
        self.assertFalse(m["structured_available"])

    def test_opt_in_and_fail_open(self):
        with patch.dict("os.environ",{"DSA_RAW_DATA_ARCHIVE":"false"}):
            self.assertIsNone(record("inputs",**self.kw))
        with patch.dict("os.environ",{"DSA_RAW_DATA_ARCHIVE":"true"}), patch("src.raw_data_archive.archive_root",return_value=self.root), patch("src.raw_data_archive.archive_inputs",side_effect=OSError):
            self.assertIsNone(record("inputs",**self.kw))

if __name__=="__main__":
    unittest.main()
