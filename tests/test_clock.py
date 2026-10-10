import unittest
from datetime import datetime, timedelta, timezone

from racer.clock import FixedClock, SystemClock, format_recorded_at


class ClockTests(unittest.TestCase):
    def test_system_clock_returns_timezone_aware_utc(self) -> None:
        instant = SystemClock().now_utc()

        self.assertIs(instant.tzinfo, timezone.utc)
        self.assertEqual(instant.utcoffset(), timedelta(0))

    def test_fixed_clock_rejects_naive_datetime(self) -> None:
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            FixedClock(datetime(2026, 9, 1, 12, 34, 56))

    def test_fixed_clock_formats_canonical_utc(self) -> None:
        clock = FixedClock(
            datetime(
                2026,
                9,
                1,
                12,
                34,
                56,
                123456,
                tzinfo=timezone(timedelta(hours=8)),
            )
        )

        self.assertEqual(
            format_recorded_at(clock.now_utc()),
            "2026-09-01T04:34:56.123456Z",
        )

    def test_format_recorded_at_rejects_naive_and_unknown_offset_datetimes(self) -> None:
        class UnknownOffset(datetime):
            def utcoffset(self):
                return None

        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            format_recorded_at(datetime(2026, 9, 1, 12, 0, 0))
        with self.assertRaisesRegex(ValueError, "timezone-aware"):
            format_recorded_at(UnknownOffset(2026, 9, 1, 12, 0, 0))

    def test_equal_aware_instants_with_different_offsets_format_identically(self) -> None:
        utc = datetime(2026, 9, 1, 4, 34, 56, 123456, tzinfo=timezone.utc)
        plus_eight = datetime(
            2026,
            9,
            1,
            12,
            34,
            56,
            123456,
            tzinfo=timezone(timedelta(hours=8)),
        )

        self.assertEqual(format_recorded_at(utc), "2026-09-01T04:34:56.123456Z")
        self.assertEqual(format_recorded_at(plus_eight), format_recorded_at(utc))


if __name__ == "__main__":
    unittest.main()
