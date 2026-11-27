import asyncio
import base64
import json
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    import httpx

    from app.ai import groq, openrouter, speech
    from app.ai.recognizer import SPARE_LIMIT_WAIT, Recognizer
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужен httpx")

OK = {"task": "transcribe", "language": "russian", "duration": 600.0, "text": "привет",
      "segments": [{"id": 0, "start": 0.0, "end": 1.0, "text": " привет", "avg_logprob": -0.1, "no_speech_prob": 0.0,
                    "compression_ratio": 1.2}],
      "usage": {"seconds": 600.0, "cost": 0.0027}}


class Case(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = Path(self.tmp.name) / "chunk-000.flac"
        self.path.write_bytes(b"fLaC-audio")
        sleep = mock.patch("app.ai.speech.asyncio.sleep", new=mock.AsyncMock())
        self.sleep = sleep.start()
        self.addCleanup(sleep.stop)

    def run_with(self, handler, call):
        async def go():
            async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as http:
                return await call(http)

        return asyncio.run(go())


class OpenRouterTest(Case):
    def transcribe(self, handler, **kwargs):
        return self.run_with(handler, lambda http: openrouter.transcribe_file(http, "sk-or-secret", self.path, **kwargs))

    def test_request_shape(self):
        seen = []

        def handler(request):
            seen.append(request)
            return httpx.Response(200, json=OK)

        self.assertEqual(self.transcribe(handler, prompt="Заквиель"), OK)
        request = seen[0]
        self.assertEqual(str(request.url), openrouter.URL)
        self.assertEqual(request.headers["authorization"], "Bearer sk-or-secret")
        body = json.loads(request.read())
        self.assertEqual(body["model"], "openai/whisper-large-v3-turbo")
        self.assertEqual(body["input_audio"], {"data": base64.b64encode(b"fLaC-audio").decode(), "format": "flac"})
        self.assertEqual((body["language"], body["response_format"], body["timestamp_granularities"], body["temperature"]),
                         ("ru", "verbose_json", ["segment"], 0))
        self.assertEqual(body["provider"], {"only": ["deepinfra", "groq"], "order": ["deepinfra", "groq"], "allow_fallbacks": True})
        self.assertEqual(body["prompt"], "Заквиель")

    def test_answer_without_timed_phrases_is_an_error(self):
        with self.assertRaisesRegex(speech.TranscribeError, "без времени фраз"):
            self.transcribe(lambda request: httpx.Response(200, json={"text": "привет", "usage": {}}))

    def test_empty_balance_is_an_account_problem_and_is_not_retried(self):
        calls = []

        def handler(request):
            calls.append(1)
            return httpx.Response(402, json={"error": {"message": "This request requires at least $0.50 in balance"}})

        with self.assertRaises(speech.TranscribeError) as caught:
            self.transcribe(handler)
        self.assertEqual((len(calls), caught.exception.account), (1, True))
        self.assertIn("$0.50", str(caught.exception))
        self.assertNotIn("sk-or-secret", str(caught.exception))

    def test_server_error_is_retried_and_rejected_file_is_not(self):
        answers = [httpx.Response(502, text="bad gateway"), httpx.Response(200, json=OK)]
        self.assertEqual(self.transcribe(lambda request: answers.pop(0)), OK)
        with self.assertRaises(speech.TranscribeError) as caught:
            self.transcribe(lambda request: httpx.Response(413, json={"error": "too large"}))
        self.assertFalse(caught.exception.account)

    def test_cost_from_answer_or_by_price(self):
        self.assertEqual(openrouter.cost(OK, 600), 0.0027)  # не по тарифу, а из ответа
        self.assertAlmostEqual(openrouter.cost({}, 3600), 0.012)


class RecognizerTest(Case):
    KEYS = {"openrouter": "sk-or-secret", "groq": "gsk_secret"}

    def run_chunks(self, recognizer, handler, count=1):
        async def call(http):
            return [await recognizer.transcribe(http, self.path, 600.0) for _ in range(count)]

        return self.run_with(handler, call)

    def router(self, openrouter_answer, groq_answer=None):
        """Ответы по адресу сервиса; считает, сколько раз к кому обращались."""
        calls = {"openrouter": 0, "groq": 0}

        def handler(request):
            name = "openrouter" if request.url.host == "openrouter.ai" else "groq"
            calls[name] += 1
            answer = openrouter_answer if name == "openrouter" else groq_answer
            return answer() if callable(answer) else httpx.Response(200, json=OK)

        return handler, calls

    def test_openrouter_goes_first(self):
        handler, calls = self.router(None)
        [result] = self.run_chunks(Recognizer(self.KEYS), handler)
        self.assertEqual((result.service, result.cost_usd, calls), ("openrouter", 0.0027, {"openrouter": 1, "groq": 0}))

    def test_empty_balance_switches_to_groq_for_all_following_chunks(self):
        handler, calls = self.router(lambda: httpx.Response(402, json={"error": {"message": "no credits"}}))
        recognizer = Recognizer(self.KEYS)
        results = self.run_chunks(recognizer, handler, count=3)
        self.assertEqual([r.service for r in results], ["groq"] * 3)
        self.assertEqual(calls, {"openrouter": 1, "groq": 3})
        self.assertIn("no credits", recognizer.broken["openrouter"])
        self.assertAlmostEqual(results[0].cost_usd, 600 / 3600 * 0.04)

    def test_temporary_failure_uses_groq_once_and_returns_to_openrouter(self):
        state = {"fails": speech.RETRIES}

        def flaky():
            if state["fails"]:
                state["fails"] -= 1
                return httpx.Response(500, text="oops")
            return httpx.Response(200, json=OK)

        handler, calls = self.router(flaky)
        results = self.run_chunks(Recognizer(self.KEYS), handler, count=2)
        self.assertEqual([r.service for r in results], ["groq", "openrouter"])

    def test_primary_does_not_wait_long_for_a_rate_limit_but_the_last_service_does(self):
        limited = lambda: httpx.Response(429, headers={"retry-after": str(int(SPARE_LIMIT_WAIT) + 1)}, text="slow down")
        handler, calls = self.router(limited)
        [result] = self.run_chunks(Recognizer(self.KEYS), handler)
        self.assertEqual((result.service, calls["openrouter"]), ("groq", 1))
        self.sleep.assert_not_awaited()
        answers = [limited(), httpx.Response(200, json=OK)]
        [result] = self.run_chunks(Recognizer({"openrouter": "k"}), lambda request: answers.pop(0))
        self.assertEqual(result.service, "openrouter")
        self.sleep.assert_awaited_once_with(SPARE_LIMIT_WAIT + 1)

    def test_all_services_failing_names_each_reason(self):
        handler, _ = self.router(lambda: httpx.Response(402, text="empty"), lambda: httpx.Response(401, text="bad key"))
        recognizer = Recognizer(self.KEYS)
        with self.assertRaises(speech.TranscribeError) as caught:
            self.run_chunks(recognizer, handler)
        self.assertIn("OpenRouter", str(caught.exception))
        self.assertIn("Groq", str(caught.exception))
        with self.assertRaisesRegex(speech.TranscribeError, "OpenRouter.*Groq"):
            self.run_chunks(recognizer, handler)  # больше не обращаемся, но причины называем

    def test_keys_come_from_environment(self):
        environ = {"OPENROUTER_API_KEY": " sk ", "GROQ_API_KEY": ""}
        self.assertEqual(Recognizer.from_env(environ).keys, {"openrouter": "sk"})
        self.assertEqual(Recognizer.from_env({"GROQ_API_KEY": "g", "OPENROUTER_API_KEY": "o"}, "groq").keys, {"groq": "g"})
        self.assertEqual(Recognizer.from_env({}).keys, {})
        with self.assertRaises(speech.TranscribeError):
            self.run_chunks(Recognizer({}), lambda request: httpx.Response(200, json=OK))


if __name__ == "__main__":
    unittest.main()
