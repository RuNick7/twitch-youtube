import unittest

from app.checks import (
    FAILED,
    PROCESSING,
    READY,
    REJECTED,
    failure_reason,
    parse_duration,
    processing_state,
    youtube_warnings,
)


def item(upload="processed", processing="succeeded", duration="PT1H", **extra):
    status = {"uploadStatus": upload, "privacyStatus": "private"}
    status.update(extra.pop("status", {}))
    details = {"duration": duration}
    details.update(extra.pop("details", {}))
    return {"status": status, "processingDetails": {"processingStatus": processing}, "contentDetails": details}


class DurationTest(unittest.TestCase):
    def test_parse(self):
        self.assertEqual(parse_duration("PT3H29M2S"), 12542)
        self.assertEqual(parse_duration("PT45.5S"), 45.5)
        self.assertEqual(parse_duration("P1DT2H"), 93600)
        self.assertEqual(parse_duration("P0D"), 0)

    def test_invalid(self):
        for value in (None, "", "P", "PT", "3H", "PT3X"):
            self.assertIsNone(parse_duration(value), value)


class StateTest(unittest.TestCase):
    def test_processing(self):
        self.assertEqual(processing_state(item(upload="uploaded", processing="processing")), PROCESSING)
        self.assertEqual(processing_state({"status": {"uploadStatus": "uploaded"}}), PROCESSING)

    def test_ready(self):
        self.assertEqual(processing_state(item()), READY)
        self.assertEqual(processing_state(item(upload="uploaded", processing="succeeded")), READY)

    def test_rejected(self):
        rejected = item(upload="rejected", status={"rejectionReason": "claim"})
        self.assertEqual(processing_state(rejected), REJECTED)
        self.assertEqual(failure_reason(rejected), "претензия правообладателя (Content ID)")

    def test_failed(self):
        failed = item(upload="failed", processing="failed", status={"failureReason": "codec"})
        self.assertEqual(processing_state(failed), FAILED)
        self.assertEqual(failure_reason(failed), "неподдерживаемый кодек")
        self.assertEqual(processing_state(item(upload="uploaded", processing="terminated")), FAILED)

    def test_unknown_reason_is_shown_as_is(self):
        self.assertEqual(failure_reason(item(upload="rejected", status={"rejectionReason": "somethingNew"})), "somethingNew")


class WarningsTest(unittest.TestCase):
    def test_clean_video(self):
        self.assertEqual(youtube_warnings(item(duration="PT1H0M5S"), 3600), [])

    def test_blocked_countries(self):
        warnings = youtube_warnings(item(details={"regionRestriction": {"blocked": ["DE", "US"]}}), 3600)
        self.assertEqual(warnings, ["заблокирован в 2 странах: DE, US"])

    def test_many_blocked_countries_are_shortened(self):
        blocked = [f"C{i}" for i in range(30)]
        warning = youtube_warnings(item(details={"regionRestriction": {"blocked": blocked}}), None)[0]
        self.assertIn("в 30 странах", warning)
        self.assertTrue(warning.endswith("и другие"))

    def test_allowed_only(self):
        warnings = youtube_warnings(item(details={"regionRestriction": {"allowed": ["RU"]}}), None)
        self.assertEqual(warnings, ["доступен только в 1 странах"])

    def test_age_restriction(self):
        warnings = youtube_warnings(item(details={"contentRating": {"ytRating": "ytAgeRestricted"}}), None)
        self.assertEqual(warnings, ["возрастное ограничение 18+"])

    def test_duration_mismatch(self):
        self.assertEqual(youtube_warnings(item(duration="PT30M"), 3600), ["длительность 30 мин вместо 1 ч"])

    def test_small_duration_difference_is_fine(self):
        self.assertEqual(youtube_warnings(item(duration="PT3H0M20S"), 10800), [])
        self.assertEqual(youtube_warnings(item(duration="PT2M"), 100), [])

    def test_unknown_expected_duration_is_not_checked(self):
        self.assertEqual(youtube_warnings(item(duration="PT5M"), None), [])


if __name__ == "__main__":
    unittest.main()
