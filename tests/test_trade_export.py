import csv
import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import main


HEADERS = ["Market", "Side", "Date", "Trade Value", "Size", "Price",
           "Closed PnL", "Fee", "Role", "Type", "Trade ID"]


def export_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return output.getvalue()


PERP = ["ETH", "Close Long", "2026-09-02 11:38:54", "3.807696", "0.0016",
        "2379.81", "0.000288", "0.0002", "Taker", "trade", "29215432655"]
SPOT = ["LIT/USDC", "Buy", "2026-09-19 15:30:50", "6.792350", "1.31",
        "5.185", "-", "0.0000", "Maker", "trade", "31112596613"]


class TradeExportTests(unittest.TestCase):
    def test_existing_columns_and_raw_export_rows(self):
        result = main.process_export_csv("\ufeff" + export_csv([PERP, SPOT]))
        self.assertEqual(result["total_trades"], 2)
        spot, perp = result["trades"]
        self.assertEqual(set(spot), set(main.TradeData.model_fields))
        self.assertEqual((spot["market"], spot["market_type"], spot["side"]), ("LIT", "Spot", "Buy"))
        self.assertEqual(spot["datetime_utc"], "2026-09-19 15:30:50 UTC")
        self.assertEqual(spot["tx_hash"], "")
        self.assertIsNone(spot["pnl_usd"])
        self.assertEqual(perp["trade_value_usd"], 3.81)
        self.assertEqual(perp["pnl_usd"], 0.0003)
        self.assertEqual(perp["fee_usd"], 0.0002)
        self.assertEqual(result["raw_trades"][0], dict(zip(HEADERS, PERP)))

    def test_more_than_three_thousand_trades(self):
        rows = [PERP[:-1] + [str(index)] for index in range(3501)]
        result = main.process_export_csv(export_csv(rows))
        self.assertEqual(result["total_trades"], 3501)
        self.assertEqual(len(result["raw_trades"]), 3501)
        self.assertFalse(result["limit_reached"])

    def test_capped_export_requests_range_split(self):
        with patch.object(main, "EXPORT_ROW_LIMIT", 2):
            result = main.process_export_csv(export_csv([PERP, SPOT]))
        self.assertTrue(result["limit_reached"])
        self.assertNotIn("trades", result)

    def test_empty_export(self):
        result = main.process_export_csv(export_csv([]))
        self.assertEqual(result["total_trades"], 0)

    def test_invalid_rows_are_not_silently_skipped(self):
        for content in ["Error,Message\n500,failed\n", export_csv([PERP[:-1]]),
                        export_csv([PERP[:4] + ["bad-size"] + PERP[5:]]),
                        export_csv([PERP]) + '"unterminated']:
            with self.subTest(content=content):
                with self.assertRaises((ValueError, csv.Error)):
                    main.process_export_csv(content)

    def test_only_lighter_download_host_is_allowed(self):
        urls = ["http://localhost/private", "file:///etc/passwd",
                "https://example.com/file.csv",
                f"http://{main.EXPORT_DOWNLOAD_HOST}/file.csv",
                f"https://{main.EXPORT_DOWNLOAD_HOST}.evil.test/file.csv",
                f"https://{main.EXPORT_DOWNLOAD_HOST}@evil.test/file.csv",
                f"https://user@{main.EXPORT_DOWNLOAD_HOST}/file.csv",
                f"https://{main.EXPORT_DOWNLOAD_HOST}:444/file.csv"]
        with TestClient(main.app) as client, patch("main.httpx.AsyncClient") as download:
            for url in urls:
                with self.subTest(url=url):
                    response = client.post("/api/process-trades-export", json={"data_url": url})
                    self.assertEqual(response.status_code, 400)
            download.assert_not_called()

    def test_download_processing_and_sanitized_failures(self):
        url = f"https://{main.EXPORT_DOWNLOAD_HOST}/file.csv?signature=secret"
        with TestClient(main.app) as client:
            for status, content in [(200, export_csv([PERP])), (403, "Expired signature"),
                                    (302, ""), (200, "Not a CSV")]:
                with self.subTest(status=status, content=content):
                    downloaded = httpx.Response(status, text=content, request=httpx.Request("GET", url))
                    mock_client = AsyncMock()
                    mock_client.__aenter__.return_value = mock_client
                    mock_client.get.return_value = downloaded
                    with patch("main.httpx.AsyncClient", return_value=mock_client) as factory:
                        response = client.post("/api/process-trades-export", json={"data_url": url})
                    factory.assert_called_once_with(timeout=120, follow_redirects=False)
                    self.assertNotIn("secret", response.text)
                    self.assertEqual(response.status_code, 200 if status == 200 and content.startswith("Market") else 502)


if __name__ == "__main__":
    unittest.main()
