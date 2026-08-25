import unittest
from datetime import date

from scout.datetime_utils import publication_date_bound


class PublicationDateBoundTests(unittest.TestCase):
    def test_full_timestamp_resolves_to_beijing_day(self) -> None:
        self.assertEqual(
            publication_date_bound("2026-08-10T12:00:00+08:00"), date(2026, 8, 10)
        )
        self.assertEqual(
            publication_date_bound("2026-08-10T18:30:00+00:00"), date(2026, 8, 11)
        )

    def test_day_precision_keeps_that_day(self) -> None:
        self.assertEqual(publication_date_bound("2026-04-20"), date(2026, 4, 20))

    def test_month_precision_uses_the_last_day(self) -> None:
        self.assertEqual(publication_date_bound("2026-02"), date(2026, 2, 28))
        self.assertEqual(publication_date_bound("2024-02"), date(2024, 2, 29))

    def test_unparseable_values_stay_conservatively_fresh(self) -> None:
        for value in ("", "   ", "Jul 20", "2026年8月", "2026-13", "2026-02-30"):
            with self.subTest(value=value):
                self.assertIsNone(publication_date_bound(value))


if __name__ == "__main__":
    unittest.main()
