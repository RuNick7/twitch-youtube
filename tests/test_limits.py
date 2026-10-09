import unittest
from datetime import datetime, timedelta, timezone

from app.limits import affects_everyone, describe, next_quota_reset, retry_at


def utc(*args):
    return datetime(*args, tzinfo=timezone.utc)


class QuotaResetTest(unittest.TestCase):
    def test_midnight_pacific_summer_and_winter(self):
        # Летом Калифорния в UTC−7, зимой в UTC−8; бот берёт 5 минут запаса
        self.assertEqual(next_quota_reset(utc(2026, 10, 2, 14, 30)), utc(2026, 10, 3, 7, 5))
        self.assertEqual(next_quota_reset(utc(2026, 12, 1, 10, 0)), utc(2026, 12, 2, 8, 5))

    def test_right_after_reset_waits_for_the_next_one(self):
        self.assertEqual(next_quota_reset(utc(2026, 10, 3, 7, 1)), utc(2026, 10, 4, 7, 5))

    def test_day_when_clocks_go_back(self):
        # 1 ноября 2026 в 2:00 по Калифорнии часы переводят назад, полночь перед этим ещё летняя
        self.assertEqual(next_quota_reset(utc(2026, 10, 31, 20, 0)), utc(2026, 11, 1, 7, 5))
        self.assertEqual(next_quota_reset(utc(2026, 11, 1, 20, 0)), utc(2026, 11, 2, 8, 5))


class RetryTest(unittest.TestCase):
    now = utc(2026, 10, 2, 14, 30)

    def test_channel_limit_retries_in_a_few_hours_for_this_channel_only(self):
        self.assertEqual(retry_at("uploadLimitExceeded", self.now), self.now + timedelta(hours=3))
        self.assertFalse(affects_everyone("uploadLimitExceeded"))

    def test_project_quota_waits_for_reset_for_everyone(self):
        for reason in ("quotaExceeded", "dailyLimitExceeded"):
            self.assertEqual(retry_at(reason, self.now), utc(2026, 10, 3, 7, 5))
            self.assertTrue(affects_everyone(reason))

    def test_rate_limit_is_short(self):
        self.assertEqual(retry_at("rateLimitExceeded", self.now), self.now + timedelta(minutes=15))

    def test_reasons_in_words(self):
        self.assertEqual(describe("uploadLimitExceeded"), "исчерпан дневной лимит канала")
        self.assertEqual(describe("quotaExceeded"), "кончилась суточная квота YouTube API")
        self.assertEqual(describe("somethingNew"), "лимит YouTube (somethingNew)")


if __name__ == "__main__":
    unittest.main()
