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
    from app.ai import audio, claude, speech, transcript
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
    """Модель отвечает одним моментом на весь участок; темы для названия у Minecraft есть, у Just Chatting — нет."""
    if "moments" in schema["properties"]:
        start, end = re.search(r"Участок для разметки: (\S+)–(\S+)", user).groups()
        data = {"moments": [{"start": start, "end": end, "kind": "game" if "Minecraft" in user else "talk",
                             "topic": "строят базу" if "Minecraft" in user else "болтают с чатом", "confidence": "high"}]}
    else:
        topics = ["постройка базы", "планы"] if "Категория Twitch: Minecraft" in user else []
        data = {"chapters": [{"moment": 0, "title": "Начало"}], "topics": topics}
    return data, claude.Usage(1, 2000, 200, 0.0003, 50)


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
        self.assertEqual(jc["title"], "Большой стрим | Just Chatting | ZakvielChannel")  # тем нет: название стрима
        self.assertEqual(jc["topics"], [])
        self.assertEqual(mc["title"], "Постройка базы, планы | Minecraft | ZakvielChannel")
        self.assertEqual(mc["title_now"], "Minecraft | Большой стрим | ZakvielChannel")
        # Minecraft идёт с 1:35 до 3:00, а расшифровка — до 2:00: участок расшифрован частично
        self.assertAlmostEqual(mc["moments"][0]["end"], 119 * 60 + 5)
        # у Just Chatting два окна по часу и название, у Minecraft одно окно и название
        self.assertEqual(result["usage"]["requests"], 5)
        self.assertEqual(result["effort"], "medium")
        self.assertIn("Расход: запросов 5", output)

    def test_joined_segment_is_marked_up_span_by_span(self):
        phrases = [transcript.Phrase(t * 60.0, t * 60.0 + 5, f"фраза {t}") for t in range(180)]
        (self.folder / "transcript.json").write_text(transcript.dump(phrases))
        info = vod_info(chapters=[
            {"start_time": 0, "end_time": HOUR, "title": "Just Chatting"},
            {"start_time": HOUR, "end_time": HOUR + 600, "title": "Minecraft"},  # короткий перерыв: разговоры склеиваются
            {"start_time": HOUR + 600, "end_time": 3 * HOUR, "title": "Just Chatting"}])
        code, output = self.run_cli(info=info)
        self.assertEqual(code, 0, output)
        result = json.loads((self.folder / "analysis.json").read_text())
        chat = result["segments"][0]
        self.assertEqual(chat["spans"], [[0, 3600], [4200, 10800]])
        for found in chat["moments"]:  # ни один момент не тянется через Minecraft посередине
            self.assertTrue(found["end"] <= 3600 or found["start"] >= 4200, found)
        self.assertEqual({m["start"] for m in chat["moments"]}, {0.0, 4200.0})
        self.assertEqual(chat["coverage"], 1.0)
        self.assertEqual(result["usage"]["requests"], 6)  # три окна разговора, Minecraft и два запроса глав

    def test_chapters_are_printed_and_saved(self):
        async def ask(client, *, system, user, schema, model, max_tokens, effort):
            if "chapters" in schema["properties"]:
                data = {"chapters": [{"moment": 0, "title": "Приветствие"}, {"moment": 1, "title": "Баланс игры"},
                                     {"moment": 2, "title": "Экзамен на права"}], "topics": ["баланс", "экзамен"]}
            elif "moments" in schema["properties"]:
                start, end = re.search(r"Участок для разметки: (\S+)–(\S+)", user).groups()
                rows = [(start, "0:10:00", "приветствие"), ("0:10:00", "0:30:00", "баланс"), ("0:30:00", end, "экзамен")]
                data = {"moments": [{"start": a, "end": b, "kind": "talk", "topic": t, "confidence": "high"} for a, b, t in rows]}
            return data, claude.Usage(1, 100, 10, 0.0001)

        info = vod_info(duration=HOUR, chapters=[{"start_time": 0, "end_time": HOUR, "title": "Just Chatting"}])
        code, output = self.run_cli(info=info, ask=ask)
        self.assertEqual(code, 0, output)
        self.assertIn("главы для описания:\n    00:00 Приветствие\n    10:00 Баланс игры\n    30:00 Экзамен на права", output)
        [row] = json.loads((self.folder / "analysis.json").read_text())["segments"]
        self.assertEqual(row["title"], "Баланс, экзамен | Just Chatting | ZakvielChannel")
        self.assertIn("(из них размышления 0)", output)
        self.assertEqual(row["chapters"][1], {"at": 600.0, "source": 600.0, "title": "Баланс игры"})
        self.assertEqual(json.loads((self.folder / "analysis.json").read_text())["usage"]["requests"], 3)  # два окна и главы с темами

    def test_no_join_makes_a_video_for_each_return_to_a_category(self):
        phrases = [transcript.Phrase(t * 60.0, t * 60.0 + 5, f"фраза {t}") for t in range(180)]
        (self.folder / "transcript.json").write_text(transcript.dump(phrases))
        info = vod_info(chapters=[
            {"start_time": 0, "end_time": HOUR, "title": "Minecraft"},
            {"start_time": HOUR, "end_time": 2 * HOUR, "title": "Dota 2"},
            {"start_time": 2 * HOUR, "end_time": 3 * HOUR, "title": "Minecraft"}])
        code, output = self.run_cli("--no-join", info=info)
        self.assertEqual(code, 0, output)
        rows = json.loads((self.folder / "analysis.json").read_text())["segments"]
        self.assertEqual([(r["category"], r["spans"]) for r in rows],
                         [("Minecraft", [[0, 3600]]), ("Dota 2", [[3600, 7200]]), ("Minecraft", [[7200, 10800]])])
        self.assertIn("глав нет", output)  # подставная модель отвечает одним моментом на участок

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


class TranscribeCliTest(unittest.TestCase):
    """Команда transcribe: yt-dlp, ffmpeg и сервисы распознавания подставные."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.out = Path(self.tmp.name)

    def run_cli(self, *args, keys=("OPENROUTER_API_KEY", "GROQ_API_KEY"), openrouter_error=None):
        used = []

        async def chunks(source, folder, *, chunk_sec, limit_sec, start_sec):
            folder.mkdir(parents=True, exist_ok=True)
            result = []
            for i in range(2):
                path = folder / f"chunk-{i:03d}.flac"
                path.write_bytes(b"flac")
                result.append(audio.Chunk(path, start_sec + i * 600.0, start_sec + (i + 1) * 600.0))
            return result

        def service(name):
            async def transcribe(http, key, path, **kwargs):
                used.append((name, key, path.name))
                if name == "openrouter" and openrouter_error:
                    raise openrouter_error
                data = {"segments": [{"start": 1.0, "end": 2.0, "text": f"фраза {path.stem}"}]}
                if name == "openrouter":
                    data["usage"] = {"cost": 0.0025}
                return data

            return transcribe

        clean = {k: v for k, v in os.environ.items() if k not in ("OPENROUTER_API_KEY", "GROQ_API_KEY")}
        clean.update({name: f"secret-{name}" for name in keys})
        stdout = io.StringIO()
        with mock.patch.dict(os.environ, clean, clear=True), \
                mock.patch.object(cli.audio, "audio_playlist_url", mock.AsyncMock(return_value="https://x/a.m3u8")), \
                mock.patch.object(cli.audio, "extract_chunks", chunks), \
                mock.patch("app.ai.openrouter.transcribe_file", service("openrouter")), \
                mock.patch("app.ai.groq.transcribe_file", service("groq")), \
                contextlib.redirect_stdout(stdout):
            code = cli.main(["transcribe", "555", "--out", str(self.out), *args])
        return code, stdout.getvalue(), used

    def report(self):
        return json.loads((self.out / "555" / "report.json").read_text())

    def test_openrouter_by_default_and_report(self):
        code, output, used = self.run_cli("--start", "60")
        self.assertEqual(code, 0, output)
        self.assertEqual([name for name, _, _ in used], ["openrouter", "openrouter"])
        report = self.report()
        self.assertEqual((report["services"], report["cost_usd"], report["problems"]), ({"openrouter": 2}, 0.005, {}))
        self.assertEqual(transcript.load((self.out / "555" / "transcript.json").read_text())[0].start, 3601.0)
        for path in (self.out / "555").iterdir():
            self.assertNotIn("secret-", path.read_text())
        self.assertFalse((self.out / "555" / "audio").exists())

    def test_spare_service_when_balance_is_empty(self):
        error = speech.TranscribeError("OpenRouter: на балансе нет денег", account=True)
        code, output, used = self.run_cli(openrouter_error=error)
        self.assertEqual(code, 0, output)
        self.assertEqual([name for name, _, _ in used], ["openrouter", "groq", "groq"])
        self.assertEqual(self.report()["services"], {"groq": 2})
        self.assertIn("Внимание: OpenRouter: на балансе нет денег", output)

    def test_chosen_service_and_missing_keys(self):
        code, output, used = self.run_cli("--service", "groq")
        self.assertEqual((code, {name for name, _, _ in used}), (0, {"groq"}))
        code, output, used = self.run_cli(keys=())
        self.assertEqual((code, used), (2, []))
        self.assertIn("OPENROUTER_API_KEY или GROQ_API_KEY", output)
        code, output, used = self.run_cli("--service", "groq", keys=("OPENROUTER_API_KEY",))
        self.assertEqual((code, used), (2, []))
        self.assertIn("GROQ_API_KEY", output)

    def test_failure_of_every_service_stops_the_command(self):
        code, output, _ = self.run_cli(keys=("OPENROUTER_API_KEY",),
                                       openrouter_error=speech.TranscribeError("OpenRouter не ответил"))
        self.assertEqual(code, 1)
        self.assertIn("Кусок 1: OpenRouter не ответил", output)


if __name__ == "__main__":
    unittest.main()
