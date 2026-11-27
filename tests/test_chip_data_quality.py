import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
import pandas as pd
from src import chip_data_quality as cq

DAY="2026-11-26"
CODE="002938"

def frame():
    return pd.DataFrame([{"日期":DAY,"获利比例":0.0,"平均成本":45.2,
        "90成本-低":40,"90成本-高":50,"70成本-低":43,"70成本-高":47,
        "90集中度":10/90,"70集中度":4/90}])

def chip():
    return cq.akshare_snapshot(frame(),CODE,DAY)

def assess(root, value):
    from src.raw_data_archive import archive_inputs,verify
    from src.data_quality_manager import assess_stock
    archive_inputs(root,code=CODE,name="鹏鼎控股",market="cn",query_id="fixture",
                   chip=value,expected_trade_date=DAY)
    return assess_stock(root,verify(root),dict(code=CODE,query_id="fixture",status="TIMEOUT"))

class ChipQualityTest(unittest.TestCase):
    def setUp(self):
        p=patch.dict(os.environ,{"DSA_CHIP_DATA_QUALITY":"true","DSA_CHIP_TUSHARE_FALLBACK":"false","DSA_PROVIDER_HEALTH_JOURNAL":""})
        p.start();self.addCleanup(p.stop)
    def test_zero_and_fraction(self):
        c=chip()
        self.assertEqual(c.profit_ratio,0)
        self.assertIsNone(cq.snapshot_error(c,CODE,DAY))
        self.assertEqual(cq.fraction("12%"),.12)
    def test_missing_and_units(self):
        self.assertIsNone(cq.akshare_snapshot(frame().drop(columns=["获利比例"]),CODE,DAY))
        self.assertIsNone(cq.akshare_snapshot(frame().assign(获利比例=80),CODE,DAY))
    def test_dates_and_duplicates(self):
        self.assertIsNone(cq.akshare_snapshot(frame(),CODE,"2026-11-27"))
        self.assertIsNone(cq.akshare_snapshot(pd.concat([frame(),frame()]),CODE,DAY))
        self.assertEqual(cq.snapshot_error(chip(),CODE,"2026-11-27"),"stale")
    def test_identity_and_presence(self):
        c=chip()
        self.assertEqual(cq.snapshot_error(c,"300394",DAY),"identity_mismatch")
        c.present_fields=[]
        self.assertEqual(cq.snapshot_error(c,CODE,DAY),"missing_explicit_fields")
    def test_quantile_conflict(self):
        c=chip();c.cost_70_low=39
        self.assertEqual(cq.metric_error(c),"cost_quantiles_not_nested")
    def test_concentration_conflict(self):
        c=chip();c.concentration_90=.5
        self.assertEqual(cq.metric_error(c),"concentration_formula_conflict")
    def test_histogram(self):
        bins=pd.DataFrame({"ts_code":["002938.SZ"]*2,"trade_date":["20261126"]*2,"price":[40,50],"percent":[40,60]})
        daily=pd.DataFrame({"ts_code":["002938.SZ"],"trade_date":["20261126"],"close":[45]})
        self.assertEqual(cq.validate_tushare_inputs(bins,daily,"002938.SZ",DAY)["reference_close"],45)
        with self.assertRaises(ValueError): cq.validate_tushare_inputs(bins.assign(percent=[4,6]),daily,"002938.SZ",DAY)
        with self.assertRaises(ValueError): cq.validate_tushare_inputs(bins,daily.assign(ts_code="300394.SZ"),"002938.SZ",DAY)
    def test_actual_manager_fallback(self):
        from data_provider.base import DataFetcherManager
        from data_provider.realtime_types import get_chip_circuit_breaker
        class Fetcher:
            def __init__(self,name,priority,value):
                self.name=name;self.priority=priority;self.value=value;self.calls=0
            def get_chip_distribution(self,code):
                self.calls+=1;return self.value
        c=chip();c.source="tushare_cyq_chips";c.data_kind="PROVIDER_DISTRIBUTION_DERIVED"
        a=Fetcher("AkshareFetcher",0,None);b=Fetcher("TushareFetcher",1,c)
        get_chip_circuit_breaker().reset()
        with patch("src.config.get_config",return_value=SimpleNamespace(enable_chip_distribution=True)),patch.object(cq,"expected_day",return_value=DAY):
            m=DataFetcherManager([a,b])
            self.assertIsNone(m.get_chip_distribution(CODE))
            self.assertEqual(b.calls,0)
            with patch.dict(os.environ,{"DSA_CHIP_TUSHARE_FALLBACK":"true"}):
                self.assertIs(m.get_chip_distribution(CODE),c)
                self.assertEqual(b.calls,1)
        get_chip_circuit_breaker().reset()
    def test_archive_model_and_zero(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=assess(Path(tmp),chip())
            self.assertEqual(q["chip_quality"]["status"],"AVAILABLE")
            self.assertEqual(q["groups"]["chips"]["profit_ratio"]["value"],0)
            self.assertTrue(q["groups"]["chips"]["profit_ratio"]["usable"])
            self.assertEqual(q["coverage"]["chips"]["usable"],8)
    def test_missing_archive(self):
        with tempfile.TemporaryDirectory() as tmp:
            q=assess(Path(tmp),None)
            self.assertEqual(q["chip_quality"]["status"],"UNAVAILABLE")
            self.assertEqual(q["coverage"]["chips"]["usable"],0)
    def test_report_guard(self):
        from src.analyzer import normalize_chip_structure_availability
        r=SimpleNamespace(dashboard={"data_perspective":{"chip_structure":{"avg_cost":999}}})
        normalize_chip_structure_availability(r,chip())
        self.assertEqual(r.dashboard["data_perspective"]["chip_structure"]["avg_cost"],45.2)
        normalize_chip_structure_availability(r,None)
        self.assertEqual(r.dashboard["data_perspective"]["chip_structure"],{})
    def test_dashboard(self):
        from src.financial_dashboard import chip_page
        with tempfile.TemporaryDirectory() as tmp:
            q=assess(Path(tmp),chip())
            _,html=chip_page(CODE,q,DAY)
            self.assertIn("0.00%",html)
            self.assertIn("LIBRARY_ESTIMATE",html)
            self.assertIn("45.20",html)
    def test_audit(self):
        self.assertEqual(cq.audit()["baseline"],"ecbad12")
        self.assertFalse(cq.tushare_enabled())

def render_fixture(output):
    from src.financial_dashboard import chip_page,render_pages
    out=Path(output);out.mkdir(parents=True,exist_ok=True)
    with patch.dict(os.environ,{"DSA_CHIP_DATA_QUALITY":"true"}):
        pages=[]
        for label,value in (("valid",chip()),("missing",None),("stale",chip())):
            if label=="stale": value.date="2026-11-25"
            root=out/label/"raw"
            q=assess(root,value)
            (out/(label+"-quality.json")).write_text(json.dumps(q,ensure_ascii=False,indent=2),encoding="utf8")
            name,html=chip_page(CODE,q,DAY)
            pages.append((label+"-"+name,html))
        render_pages(pages,out)
    (out/"source-audit.json").write_text(json.dumps(cq.audit(),ensure_ascii=False,indent=2),encoding="utf8")
