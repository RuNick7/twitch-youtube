import contextlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path
from unittest import mock

try:
    from app.ai import __main__ as cli
    from app.ai import claude, transcript
    from app.tools import ToolError, VodInfo
except ImportError:  # без зависимостей из requirements.txt: эти тесты идут в тестовом образе
    raise unittest.SkipTest("нужен httpx")

HOUR = 3600


def vod_info(**kwargs):
    chapters = [
        {"start_time": 0, "end_time": 1.5 * HOUR, "title": "Just Chatting"},
        {"start_time": 1.5 * HOUR, "end_time": 1.5 * HOUR + 300, "title": "Dota 2"},  # 5 минут: бот такой ролик не грузит
        {"start_time": 1.5 * HOUR + 300, "end_time": 3 * HOUR, "title": "Minecraft"},
    ]
    values = dict(id="123", title="Большой стрим", duration=3 * HOUR, uploader="ZakvielChannel", uploader_login="zakvielchannel",
                  started_at=None, is_live=False, chapters=chapters, playlist_url=None)
    values.update(kwargs)
    return VodInfo(**values)


async def fake_ask(client, *, system, user, schema, model, max_tokens, effort):
    """Модель отвечает одним моментом на весь участок; название у Minecraft есть, у Just Chatting — пустое."""
    if "moments" in schema["properties"]:
        start, end = re.search(r"Участок для разметки: (\S+)–(\S+)", user).groups()
        data = {"moments": [{"start": start, "end": end, "kind": "game" if "Minecraft" in user else "talk",
                             "topic": "строят базу" if "Minecraft" in user else "болтают с чатом", "confidence": "high"}]}
    else:
        data = {"title": "Строим базу и обсуждаем планы" if "Категория Twitch: Minecraft" in user else ""}
    return data, claude.Usage(1, 2000, 200, 0.0003)


class AnalyzeCliTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)
        self.folder = self.out / "123"
        self.folder.mkdir()
        # расшифровка покрывает первые два часа: фраза в минуту
        phrases = [transcript.Phrase(t * 60.0, t * 60.0 + 5, f"фраза {t}") for t in range(120)]
        (self.folder / "transcript.json").write_text(transcript.dump(phrases))

    def run_cli(self, *args, info=None, key="sk-test", ask=fake_ask):
        """info — запись Twitch или исключение, которое должен выбросить yt-dlp."""
        env = {"ANTHROPIC_API_KEY": key} if key else {}
        stdout = io.StringIO()
        fetch = mock.AsyncMock(side_effect=info) if isinstance(info, Exception) else mock.AsyncMock(return_value=info or vod_info())
        patches = [
            mock.patch.dict(os.environ, env, clear=False),
            mock.patch.object(cli.tools, "fetch_vod_info", fetch),
            mock.patch.object(cli.claude, "make_client", lambda api_key: object()),
            mock.patch.object(cli.claude, "ask_json", ask),
        ]
        if not key:
            patches[0] = mock.patch.dict(os.environ, {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}, clear=True)
        with contextlib.ExitStack() as stack, contextlib.redirect_stdout(stdout):
            for patch in patches:
                stack.enter_context(patch)
            code = cli.main(["analyze", "123", "--out", str(self.out), *args])
        return code, stdout.getvalue()

    def test_segments_moments_and_titles(self):
        code, output = self.run_cli("--effort", "medium")
        self.assertEqual(code, 0, output)
        result = json.loads((self.folder / "analysis.json").read_text())
        jc, dota, mc = result["segments"]
        self.assertEqual((jc["category"], dota["category"], mc["category"]), ("Just Chatting", "Dota 2", "Minecraft"))
        self.assertIn("короче 7 минут", dota["skipped"])
        self.assertEqual(jc["moments"][0]["topic"], "болтают с чатом")
        self.assertEqual(jc["title"], jc["title_now"])  # модель ничего не предложила: остаётся название стрима
        self.assertEqual(jc["middle_rejected"], "модель не нашла конкретики")
        self.assertEqual(mc["title"], "Minecraft | Строим базу и обсуждаем планы | ZakvielChannel")
        self.assertEqual(mc["title_now"], "Minecraft | Большой стрим | ZakvielChannel")
        # Minecraft идёт с 1:35 до 3:00, а расшифровка — до 2:00: участок расшифрован частично
        self.assertAlmostEqual(mc["moments"][0]["end"], 119 * 60 + 5)
        # у Just Chatting два окна по часу и название, у Minecraft одно окно и название
        self.assertEqual(result["usage"]["requests"], 5)
        self.assertEqual(result["effort"], "medium")
        self.assertIn("Расход: запросов 5", output)

    def test_name_option_and_uncovered_segment(self):
        info = vod_info(duration=4 * HOUR, chapters=[
            {"start_time": 0, "end_time": 1 * HOUR, "title": "Just Chatting"},
            {"start_time": 3 * HOUR, "end_time": 4 * HOUR, "title": "Minecraft"}])
        code, _ = self.run_cli("--name", "Заквиель", info=info)
        self.assertEqual(code, 0)
        segments = json.loads((self.folder / "analysis.json").read_text())["segments"]
        self.assertTrue(any("нет расшифровки" in row.get("skipped", "") for row in segments))
        analyzed = next(row for row in segments if "moments" in row)
        self.assertTrue(analyzed["title"].endswith("| Заквиель"))

    def test_missing_key_or_transcript(self):
        code, output = self.run_cli(key="")
        self.assertEqual(code, 2)
        self.assertIn("ANTHROPIC_API_KEY", output)
        (self.folder / "transcript.json").unlink()
        code, output = self.run_cli()
        self.assertEqual(code, 1)
        self.assertIn("python -m app.ai transcribe 123", output)

    def test_vod_info_error(self):
        code, output = self.run_cli(info=ToolError("yt-dlp упал"))
        self.assertEqual(code, 1)
        self.assertIn("yt-dlp упал", output)
        self.assertFalse((self.folder / "analysis.json").exists())

    def test_claude_error_stops_but_keeps_what_is_done(self):
        calls = []

        async def failing_ask(client, **kwargs):
            calls.append(1)
            if len(calls) > 3:  # первый сегмент (2 окна и название) проходит, на втором запросе второго — сбой
                raise claude.ClaudeError("Anthropic не принял ключ")
            return await fake_ask(client, **kwargs)

        code, output = self.run_cli(ask=failing_ask)
        self.assertEqual(code, 1)
        self.assertIn("Разбор остановлен", output)
        result = json.loads((self.folder / "analysis.json").read_text())
        self.assertIn("moments", result["segments"][0])
        self.assertIn("ошибка Claude", result["segments"][-1]["skipped"])

    def test_key_never_reaches_output_files(self):
        self.run_cli(key="sk-ant-very-secret")
        for path in self.folder.iterdir():
            self.assertNotIn("very-secret", path.read_text())


if __name__ == "__main__":
    unittest.main()
