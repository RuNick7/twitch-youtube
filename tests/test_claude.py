import asyncio
import json
import unittest
from types import SimpleNamespace

from app.ai import claude

SCHEMA = {"type": "object", "properties": {"x": {"type": "string"}}, "required": ["x"], "additionalProperties": False}


def reply(text='{"x": "да"}', stop="end_turn", input_tokens=1000, output_tokens=100, blocks=None, **extra):
    content = blocks if blocks is not None else [SimpleNamespace(type="thinking", thinking=""), SimpleNamespace(type="text", text=text)]
    usage = SimpleNamespace(input_tokens=input_tokens, output_tokens=output_tokens, **extra)
    return SimpleNamespace(stop_reason=stop, content=content, usage=usage, stop_details=None)


class FakeClient:
    def __init__(self, response=None, error=None):
        self.calls = []
        self.response, self.error = response, error
        self.messages = SimpleNamespace(create=self.create)

    async def create(self, **kwargs):
        self.calls.append(kwargs)
        if self.error:
            raise self.error
        return self.response


def ask(client, **kwargs):
    return asyncio.run(claude.ask_json(client, system="система", user="вопрос", schema=SCHEMA, **kwargs))


class AskJsonTest(unittest.TestCase):
    def test_request_shape(self):
        client = FakeClient(reply())
        data, _ = ask(client, effort="medium", max_tokens=1234)
        self.assertEqual(data, {"x": "да"})
        call = client.calls[0]
        self.assertEqual(call["model"], "claude-haiku-5-5")
        self.assertEqual((call["max_tokens"], call["system"]), (1234, "система"))
        self.assertEqual(call["messages"], [{"role": "user", "content": "вопрос"}])
        self.assertEqual(call["output_config"], {"effort": "medium", "format": {"type": "json_schema", "schema": SCHEMA}})
        for forbidden in ("temperature", "top_p", "top_k", "thinking"):  # у Haiku 5.5 их задавать нельзя
            self.assertNotIn(forbidden, call)

    def test_text_block_is_found_after_thinking_block(self):
        data, _ = ask(FakeClient(reply()))
        self.assertEqual(data["x"], "да")

    def test_cost_short_and_long_prompt(self):
        _, usage = ask(FakeClient(reply(input_tokens=10_000, output_tokens=1_000)))
        self.assertEqual((usage.requests, usage.input_tokens, usage.output_tokens), (1, 10_000, 1_000))
        self.assertAlmostEqual(usage.cost_usd, (10_000 * 0.10 + 1_000 * 0.50) / 1e6)
        self.assertEqual(usage.thinking_tokens, 0)
        details = SimpleNamespace(thinking_tokens=700)
        _, usage = ask(FakeClient(reply(input_tokens=10, output_tokens=1_000, output_tokens_details=details)))
        total = claude.Usage()
        total.add(usage)
        total.add(usage)
        self.assertEqual(total.thinking_tokens, 1400)
        _, usage = ask(FakeClient(reply(input_tokens=200_000, output_tokens=1_000)))
        self.assertAlmostEqual(usage.cost_usd, (200_000 * 0.50 + 1_000 * 2.50) / 1e6)

    def test_cache_tokens_count_as_prompt(self):
        _, usage = ask(FakeClient(reply(input_tokens=60_000, cache_read_input_tokens=50_000, cache_creation_input_tokens=0)))
        self.assertEqual(usage.input_tokens, 110_000)
        self.assertAlmostEqual(usage.cost_usd, (110_000 * 0.50 + 100 * 2.50) / 1e6)

    def test_refusal_and_truncation(self):
        refusal = reply(stop="refusal")
        refusal.stop_details = SimpleNamespace(category="cyber")
        with self.assertRaisesRegex(claude.ClaudeError, "отказался.*cyber"):
            ask(FakeClient(refusal))
        with self.assertRaisesRegex(claude.ClaudeError, "обрезан на 500"):
            ask(FakeClient(reply(stop="max_tokens")), max_tokens=500)

    def test_broken_answers(self):
        for response, needle in (
            (reply(blocks=[SimpleNamespace(type="thinking", thinking="")]), "нет текста"),
            (reply(text="не json"), "не JSON"),
            (reply(text="[1, 2]"), "не объект"),
        ):
            with self.assertRaisesRegex(claude.ClaudeError, needle):
                ask(FakeClient(response))

    def test_sdk_error_becomes_claude_error_with_cause(self):
        with self.assertRaises(claude.ClaudeError) as caught:
            ask(FakeClient(error=RuntimeError("boom")))
        self.assertIn("boom", str(caught.exception))
        self.assertIsInstance(caught.exception.__cause__, RuntimeError)

    def test_usage_adds_up(self):
        total = claude.Usage()
        total.add(claude.Usage(1, 100, 10, 0.5))
        total.add(claude.Usage(2, 50, 5, 0.25))
        self.assertEqual((total.requests, total.input_tokens, total.output_tokens, total.cost_usd), (3, 150, 15, 0.75))

    @unittest.skipIf(claude.anthropic is not None, "пакет anthropic установлен")
    def test_client_needs_sdk(self):
        with self.assertRaisesRegex(claude.ClaudeError, "anthropic"):
            claude.make_client("key")

    def test_schema_is_plain_json(self):
        json.dumps(SCHEMA)


if __name__ == "__main__":
    unittest.main()
