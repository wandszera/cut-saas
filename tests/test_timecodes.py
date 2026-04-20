import unittest

from app.utils.timecodes import parse_timecode_to_seconds


class TimecodeParsingTestCase(unittest.TestCase):
    def test_parse_seconds_string(self):
        self.assertEqual(parse_timecode_to_seconds("72.5"), 72.5)

    def test_parse_mmss(self):
        self.assertEqual(parse_timecode_to_seconds("01:12"), 72.0)

    def test_parse_hhmmss(self):
        self.assertEqual(parse_timecode_to_seconds("01:02:03"), 3723.0)

    def test_reject_invalid_minute_second_ranges(self):
        with self.assertRaises(ValueError):
            parse_timecode_to_seconds("00:61:00")


if __name__ == "__main__":
    unittest.main()
