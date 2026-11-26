"""Offline contract checks for the deployment-owned financial dashboard."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from src import financial_dashboard as fd

MARKET = """# A股大盘复盘
2026-11-26

## 盘面总览
盘面信号: 49/100（震荡，需观察）
市场统计数据缺失。

## 指数结构
| 指数 | 最新 | 涨跌幅 |
| --- | --- | --- |
| 上证指数 | 3476.88 | +0.01% |
| 深证成指 | 10084.04 | -0.02% |
| 创业板指 | 2189.82 | +0.29% |
| 科创50 | 1169.99 | -1.36% |
| 上证50 | 2753.71 | +0.07% |
| 沪深300 | 3900.55 | +0.06% |

## 板块主线
| 排名 | 行业板块 | 涨跌幅 |
| --- | --- | --- |
| 1 | 渔业 | +5.88% |
| 2 | 房屋建筑业 | +5.23% |
| 3 | 油气开采 | +2.82% |
| 4 | 煤炭开采 | +2.81% |
| 5 | 有色金属矿采选 | +2.13% |

| 排名 | 行业板块 | 涨跌幅 |
| --- | --- | --- |
| 1 | 机动车等修理 | -3.38% |
| 2 | 航空运输 | -2.80% |
| 3 | 通用设备 | -2.39% |
| 4 | 文教工美用品 | -2.33% |
| 5 | 餐饮业 | -2.29% |

## 明日交易计划
- 控制风险，等待量价确认。

## 风险提示
- 数据不完整。
"""

CODES = ["002938","300394","300006","601958"]
FAILURES = {code:"TIMEOUT" for code in CODES[:3]}
STOCK = """## 金钼股份 (601958)
> 2026-11-26 | 评分: 35 | 看空
"""
PAYLOAD = {
    "code":"601958", "name":"金钼股份", "sentiment_score":35,
    "operation_advice":"减仓", "trend_prediction":"看空",
    "current_price":14.73, "change_pct":0.61,
    "dashboard":{
        "core_conclusion":{"one_sentence":"等待技术面确认"},
        "battle_plan":{"sniper_points":{"ideal_buy":"等待企稳","secondary_buy":14.2,
                                        "stop_loss":14.4,"take_profit":15.01}},
        "data_perspective":{"volume_analysis":{"volume_ratio":0.94,"turnover_rate":0.65}},
    },
}

def sample():
    result = SimpleNamespace(code="601958",name="金钼股份",success=True,to_dict=lambda:PAYLOAD)
    notifier = Mock()
    notifier.generate_single_stock_report.return_value = STOCK
    rows = fd.batch_status(CODES,[result],FAILURES)
    return notifier, rows, [result]


class DashboardContract(unittest.TestCase):
    def test_partial_and_all_failed(self):
        notifier, rows, results = sample()
        self.assertEqual([r["status"] for r in rows],["TIMEOUT"]*3+["SUCCESS"])
        pages = fd.stock_pages(notifier,rows,results,STOCK)
        self.assertEqual(len(pages),3)
        for code in CODES:
            self.assertIn(code,pages[0][1])
        self.assertIn("交易计划待核实",pages[-1][1])
        self.assertNotIn(">14.40<",pages[-1][1])
        all_failed = fd.batch_status(CODES,[],dict.fromkeys(CODES,"TIMEOUT"))
        self.assertIn("0/4",fd.status_text(all_failed))
        self.assertEqual(len(fd.stock_pages(notifier,all_failed,[],"")),1)
        with tempfile.TemporaryDirectory() as d:
            fd.save_status(rows,d)
            data=json.loads((Path(d)/"batch-status.json").read_text())
            self.assertEqual(data["planned"],4)
            self.assertEqual(data["succeeded"],1)

    def test_market_pages_and_colors(self):
        self.assertTrue(fd.is_market_report(MARKET,{"region":"cn"}))
        self.assertFalse(fd.is_market_report(MARKET,{"region":"us"}))
        self.assertFalse(fd.is_market_report(MARKET,{"region":"cn,us"}))
        pages=fd.market_pages(MARKET)
        self.assertEqual(len(pages),3)
        self.assertIn("沪深300",pages[0][1])
        self.assertIn("未取得有效市场宽度",pages[0][1])
        self.assertIn("餐饮业",pages[1][1])
        self.assertEqual(fd.change_class("+0.61%"),"up")
        self.assertEqual(fd.change_class("-0.61%"),"down")

    def test_render_failure_text_fallback(self):
        notifier,rows,results=sample()
        notifier.send_to_telegram.return_value=True
        with tempfile.TemporaryDirectory() as d, patch.object(fd,"OUTPUT",Path(d)), patch.object(fd,"render_pages",side_effect=RuntimeError("test")):
            self.assertTrue(fd.send_batch(notifier,rows,results,STOCK))
        message=notifier.send_to_telegram.call_args.args[0]
        self.assertIn("1/4",message)
        self.assertIn("002938",message)

    def test_document_failure_text_fallback(self):
        notifier,rows,results=sample()
        notifier.send_to_telegram.return_value=True
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/"sample.png"
            p.write_bytes(b"fixture")
            with patch.object(fd,"OUTPUT",Path(d)), patch.object(fd,"render_pages",return_value=[p]), patch.object(fd,"send_file",return_value=False):
                self.assertTrue(fd.send_batch(notifier,rows,results,STOCK))
        notifier.send_to_telegram.assert_called_once()


if __name__=="__main__":
    unittest.main()
