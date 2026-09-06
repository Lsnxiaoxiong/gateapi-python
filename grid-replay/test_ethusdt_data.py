import csv
import io
import tempfile
import unittest
import zipfile
from datetime import date
from pathlib import Path

from csv_index import CsvIndex
from download_ethusdt_1m import HEADER, merge, ms


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.start, self.end = date(2021, 9, 4), date(2021, 9, 5)
        self.t = ms(self.start)

    def candle(self, ts):
        return [ts, 3000, 3100, 2900, 3050, 100, ts + 59999, 300000, 10, 50, 150000, 0]

    def archive(self, rows):
        text = io.StringIO()
        csv.writer(text).writerows(rows)
        path = self.root / "ETHUSDT-1m-2021-09-04.zip"
        with zipfile.ZipFile(path, "w") as z:
            z.writestr("data.csv", text.getvalue())
        return [{"path": str(path)}]

    def test_full_day_header_trim_and_index(self):
        rows = [HEADER] + [self.candle(self.t + n * 60000) for n in range(-1, 1441)]
        out = self.root / "full.csv"
        report = merge(self.archive(rows), out, self.start, self.end)
        self.assertEqual(report["rows"], 1440)
        self.assertEqual(report["gaps"], [])
        index = CsvIndex(out)
        self.assertEqual(index.row_for_timestamp(self.t + 60000), 1)
        self.assertEqual(CsvIndex(out).total_rows, 1440)

    def test_gap_is_reported_not_filled(self):
        rows = [self.candle(self.t + n * 60000) for n in range(1440) if n != 7]
        report = merge(self.archive(rows), self.root / "gap.csv", self.start, self.end)
        self.assertEqual(report["rows"], 1439)
        self.assertEqual(report["missing_minutes"], 1)
        self.assertEqual(report["gaps"][0]["start_ms"], self.t + 7 * 60000)

    def test_duplicate_rejected(self):
        with self.assertRaisesRegex(ValueError, "Duplicate"):
            merge(self.archive([self.candle(self.t)] * 2), self.root / "duplicate.csv", self.start, self.end)

    def test_invalid_ohlc_rejected(self):
        row = self.candle(self.t)
        row[2] = 2000
        with self.assertRaisesRegex(ValueError, "OHLCV"):
            merge(self.archive([row]), self.root / "invalid.csv", self.start, self.end)

    def test_cli_import_does_not_load_btc(self):
        import sys
        import backtest_cli
        self.assertNotIn("server", sys.modules)
        self.assertTrue(callable(backtest_cli.run))


if __name__ == "__main__":
    unittest.main()
