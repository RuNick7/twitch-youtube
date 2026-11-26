import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx

    from app.ai import groq
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужен httpx")

OK = {"text": "привет", "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": " привет", "avg_logprob": -0.1,
                                      "no_speech_prob": 0.0, "compression_ratio": 1.2}]}


def client(handler):
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


class TranscribeFileTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chunk-000.flac"
        self.path.write_bytes(b"fLaC-audio")
        sleep = mock.patch("app.ai.groq.asyncio.sleep", new=mock.AsyncMock())
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_call(self, handler, **kwargs):
        async def go():
            async with client(handler) as http:
                return await groq.transcribe_file(http, "gsk_secret", self.path, **kwargs)

        return asyncio.run(go())

    def test_request_shape(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=OK)

        self.assertEqual(self.run_call(handler, prompt="Заквиель, Майнкрафт"), OK)
        request = seen[0]
        body = request.read().decode("utf-8", "replace")
        self.assertEqual(str(request.url), groq.URL)
        self.assertEqual(request.headers["authorization"], "Bearer gsk_secret")
        for needle in ('name="model"', groq.MODEL, 'name="language"', "ru", 'name="response_format"', "verbose_json",
                       'name="timestamp_granularities[]"', "segment", 'name="temperature"', "Заквиель, Майнкрафт",
                       'filename="chunk-000.flac"', "fLaC-audio"):
            self.assertIn(needle, body, needle)

    def test_rate_limit_waits_for_retry_after(self):
        answers = [httpx.Response(429, headers={"retry-after": "7"}, json={"error": {"message": "slow down"}}),
                   httpx.Response(200, json=OK)]
        self.assertEqual(self.run_call(lambda request: answers.pop(0)), OK)
        self.sleep.assert_awaited_once_with(7.0)

    def test_long_rate_limit_is_waited_out_beyond_retry_count(self):
        answers = [httpx.Response(429, headers={"retry-after": "900"}, json={"error": {"message": "audio seconds per hour"}})
                   for _ in range(groq.RETRIES + 3)] + [httpx.Response(200, json=OK)]
        self.assertEqual(self.run_call(lambda request: answers.pop(0)), OK)
        self.assertEqual([call.args[0] for call in self.sleep.await_args_list], [900.0] * (groq.RETRIES + 3))

    def test_rate_limit_waiting_has_a_ceiling(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(429, headers={"retry-after": "999999"}, json={"error": {"message": "day limit"}})

        with self.assertRaisesRegex(groq.TranscribeError, "day limit"):
            self.run_call(handler)
        self.assertEqual(len(calls), int(groq.LIMIT_WAIT_MAX / groq.RETRY_AFTER_MAX) + 1)

    def test_server_errors_and_network_errors_are_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            if len(calls) == 1:
                raise httpx.ConnectError("нет связи")
            if len(calls) == 2:
                return httpx.Response(503, text="busy")
            return httpx.Response(200, json=OK)

        self.assertEqual(self.run_call(handler), OK)
        self.assertEqual(len(calls), 3)

    def test_wrong_key_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(401, json={"error": {"message": "Invalid API Key"}})

        with self.assertRaises(groq.TranscribeError) as caught:
            self.run_call(handler)
        self.assertEqual(len(calls), 1)
        self.assertIn("ключ", str(caught.exception))
        self.assertNotIn("gsk_secret", str(caught.exception))

    def test_file_rejected_is_not_retried(self):
        with self.assertRaises(groq.TranscribeError) as caught:
            self.run_call(lambda request: httpx.Response(413, json={"error": {"message": "file too large"}}))
        self.assertIn("file too large", str(caught.exception))

    def test_gives_up_after_retries(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(500, text="oops")

        with self.assertRaises(groq.TranscribeError):
            self.run_call(handler)
        self.assertEqual(len(calls), groq.RETRIES)


class CostTest(unittest.TestCase):
    def test_hour_of_audio(self):
        self.assertAlmostEqual(groq.cost([600.0] * 6), 0.04)
        self.assertAlmostEqual(groq.cost([600.0] * 6, "whisper-large-v3"), 0.111)

    def test_short_chunk_is_billed_as_ten_seconds(self):
        self.assertAlmostEqual(groq.cost([2.0]), 10 / 3600 * 0.04)

    def test_unknown_model_uses_turbo_price(self):
        self.assertAlmostEqual(groq.cost([3600.0], "whisper-new"), 0.04)


if __name__ == "__main__":
    unittest.main()
