import unittest
from unittest.mock import patch

from app.services import second_part_client


class SecondPartClientTests(unittest.TestCase):
    def setUp(self):
        self.url = second_part_client.settings.SECOND_PART_API_URL
        self.token = second_part_client.settings.SECOND_PART_API_TOKEN
        second_part_client.settings.SECOND_PART_API_URL = "https://example.test/records"
        second_part_client.settings.SECOND_PART_API_TOKEN = "test-token"

    def tearDown(self):
        second_part_client.settings.SECOND_PART_API_URL = self.url
        second_part_client.settings.SECOND_PART_API_TOKEN = self.token

    def test_date_window_is_split_when_source_hits_observed_response_cap(self):
        def fake_fetch(from_date, to_date):
            if (from_date, to_date) == ("2026-10-01", "2026-10-04"):
                return [{}] * 5000
            if (from_date, to_date) == ("2026-10-01", "2026-10-02"):
                return [{"id": "left"}]
            if (from_date, to_date) == ("2026-10-03", "2026-10-04"):
                return [{"id": "right"}]
            raise AssertionError((from_date, to_date))

        with patch.object(second_part_client, "_fetch_once", side_effect=fake_fetch):
            result = second_part_client.fetch_records("2026-10-01", "2026-10-04")

        self.assertEqual([item["id"] for item in result.records], ["left", "right"])
        self.assertFalse(result.possibly_truncated)
        self.assertEqual(result.request_count, 3)

    def test_single_day_at_source_cap_remains_flagged(self):
        with patch.object(
            second_part_client,
            "_fetch_once",
            return_value=[{}] * 5000,
        ):
            result = second_part_client.fetch_records("2026-10-01", "2026-10-01")

        self.assertTrue(result.possibly_truncated)
        self.assertEqual(result.request_count, 1)


if __name__ == "__main__":
    unittest.main()
