import csv
import io
import unittest
from unittest.mock import AsyncMock, patch

import httpx
from fastapi.testclient import TestClient

import main


HEADERS = ["Market", "Side", "Date", "Position Size", "Payment", "Rate"]
PAYMENT = ["TTWO", "long", "2026-10-01 16:00:00", "0.0491", "-0.000039", "0.000004%"]


def funding_csv(rows):
    output = io.StringIO()
    writer = csv.writer(output)
    writer.writerow(HEADERS)
    writer.writerows(rows)
    return output.getvalue()


class FundingExportTests(unittest.TestCase):
    def test_sample_format_preserves_payment_and_percentage_precision(self):
        result = main.process_funding_export_csv("\ufeff" + funding_csv([PAYMENT]))
        self.assertEqual(result["fundings"], [{
            "market": "TTWO", "side": "long", "datetime_utc": "2026-10-01 16:00:00 UTC",
            "size": 0.0491, "change_usd": -0.000039, "rate_percent": "0.000004",
        }])
        self.assertEqual(result["raw_fundings"], [dict(zip(HEADERS, PAYMENT))])

    def test_empty_and_large_exports(self):
        for count in (0, 3501):
            result = main.process_funding_export_csv(funding_csv([PAYMENT] * count))
            self.assertEqual(result["total_fundings"], count)
            self.assertFalse(result["limit_reached"])

    def test_row_cap_requires_split(self):
        with patch.object(main, "EXPORT_ROW_LIMIT", 2):
            result = main.process_funding_export_csv(funding_csv([PAYMENT] * 2))
        self.assertEqual(result, {"success": True, "limit_reached": True})

    def test_invalid_rows_fail_instead_of_returning_partial_data(self):
        for content in ("Error,Message\n500,failed\n", funding_csv([PAYMENT[:-1]]),
                        funding_csv([PAYMENT[:-1] + ["0.000004"]]),
                        funding_csv([PAYMENT[:4] + ["NaN", PAYMENT[-1]]]),
                        funding_csv([PAYMENT[:2] + ["bad-date"] + PAYMENT[3:]]),
                        funding_csv([PAYMENT]) + '"unterminated'):
            with self.subTest(content=content), self.assertRaises((ValueError, csv.Error)):
                main.process_funding_export_csv(content)

    def test_download_security_and_sanitized_failures(self):
        with TestClient(main.app) as client, patch("main.httpx.AsyncClient") as download:
            for url in ("https://example.com/file.csv", "http://localhost/private",
                        f"https://user@{main.EXPORT_DOWNLOAD_HOST}/file.csv",
                        f"https://{main.EXPORT_DOWNLOAD_HOST}.evil.test/file.csv"):
                self.assertEqual(client.post("/api/process-fundings-export", json={"data_url": url}).status_code, 400)
            download.assert_not_called()

        for host in (main.EXPORT_DOWNLOAD_HOST, "zklighter-rh-mainnet-data-export.s3.amazonaws.com"):
            url = f"https://{host}/file.csv?signature=secret"
            for status, content in ((200, funding_csv([PAYMENT])), (403, "Expired"),
                                    (302, ""), (200, "Not a CSV")):
                mock_client = AsyncMock()
                mock_client.__aenter__.return_value = mock_client
                mock_client.get.return_value = httpx.Response(status, text=content, request=httpx.Request("GET", url))
                with TestClient(main.app) as client, patch("main.httpx.AsyncClient", return_value=mock_client) as factory:
                    response = client.post("/api/process-fundings-export", json={"data_url": url})
                factory.assert_called_once_with(timeout=120, follow_redirects=False)
                self.assertNotIn("secret", response.text)
                self.assertEqual(response.status_code, 200 if status == 200 and content.startswith("Market") else 502)


if __name__ == "__main__":
    unittest.main()
