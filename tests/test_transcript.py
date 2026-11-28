import tempfile
import unittest
from pathlib import Path

from app.ai import audio, transcript
from app.ai.transcript import Phrase


def seg(text, start=0.0, end=1.0, no_speech=0.01, logprob=-0.2, ratio=1.5):
    return {"text": text, "start": start, "end": end, "no_speech_prob": no_speech, "avg_logprob": logprob,
            "compression_ratio": ratio}


class ParseVerboseTest(unittest.TestCase):
    def parse(self, *segments, offset=0.0):
        return transcript.parse_verbose({"segments": list(segments)}, offset)

    def test_normal_speech_is_kept_with_offset(self):
        phrases, dropped = self.parse(seg(" Привет,  чат! ", 1.5, 3.0), seg("Погнали", 4.0, 5.0), offset=600)
        self.assertEqual(phrases, [Phrase(601.5, 603.0, "Привет, чат!"), Phrase(604.0, 605.0, "Погнали")])
        self.assertFalse(dropped)

    def test_silence_needs_both_signs(self):
        phrases, dropped = self.parse(
            seg("из ничего", no_speech=0.9, logprob=-1.5),
            seg("тихо, но уверенно", no_speech=0.9, logprob=-0.3),
            seg("шумно, но речь", no_speech=0.1, logprob=-1.5),
        )
        self.assertEqual([p.text for p in phrases], ["тихо, но уверенно", "шумно, но речь"])
        self.assertEqual(dropped["тишина или музыка"], 1)

    def test_loop_and_empty_and_credits(self):
        phrases, dropped = self.parse(
            seg("ааа ааа ааа", ratio=3.1),
            seg("   "),
            seg("Субтитры сделал DimaTorzok"),
            seg("Редактор субтитров А.Семкин Корректор А.Егорова"),
            seg("нормальная фраза"),
        )
        self.assertEqual([p.text for p in phrases], ["нормальная фраза"])
        self.assertEqual(dict(dropped), {"зацикливание": 1, "пусто": 1, "чужие субтитры": 2})

    def test_filler_phrases_are_dropped_only_when_whisper_doubted_speech(self):
        phrases, dropped = self.parse(
            seg("Подписывайтесь на канал!", no_speech=0.5),
            seg("Спасибо за просмотр!", no_speech=0.02),
        )
        self.assertEqual([p.text for p in phrases], ["Спасибо за просмотр!"])
        self.assertEqual(dropped["фраза из тишины"], 1)

    def test_to_be_continued_alone_is_always_invented(self):
        phrases, dropped = self.parse(
            seg("Продолжение следует...", no_speech=0.01, logprob=-0.1),
            seg("Там в конце написано, что продолжение следует, и всё", no_speech=0.01),
        )
        self.assertEqual([p.text for p in phrases], ["Там в конце написано, что продолжение следует, и всё"])
        self.assertEqual(dropped["чужие субтитры"], 1)

    def test_unsure_thank_you_is_dropped_but_a_clear_one_stays(self):
        phrases, dropped = self.parse(
            seg("Спасибо.", logprob=-0.7),  # так Whisper «слышит» музыку
            seg(" спасибо! ", logprob=-0.3),
            seg("Спасибо за донат, дружище", logprob=-0.8),
        )
        self.assertEqual([p.text for p in phrases], ["спасибо!", "Спасибо за донат, дружище"])
        self.assertEqual(dropped["фраза из тишины"], 1)

    def test_punctuation_only_is_empty(self):
        phrases, dropped = self.parse(seg("..."), seg(" — "), seg("да"))
        self.assertEqual(([p.text for p in phrases], dropped["пусто"]), (["да"], 2))

    def test_end_never_before_start(self):
        phrases, _ = self.parse(seg("странно", 10.0, 9.0))
        self.assertEqual((phrases[0].start, phrases[0].end), (10.0, 10.0))

    def test_missing_segments(self):
        self.assertEqual(transcript.parse_verbose({}), ([], {}))


class CollapseLoopsTest(unittest.TestCase):
    def phrases(self, *texts):
        return [Phrase(i * 2.0, i * 2.0 + 1, text) for i, text in enumerate(texts)]

    def test_long_run_becomes_one_phrase(self):
        result, removed = transcript.collapse_loops(self.phrases("Я не знаю.", "я не знаю", "Я не знаю!", "Я не знаю", "дальше"))
        self.assertEqual([p.text for p in result], ["Я не знаю.", "дальше"])
        self.assertEqual((result[0].start, result[0].end, removed), (0.0, 7.0, 3))

    def test_short_run_is_speech(self):
        texts = ("да", "да", "да", "нет")
        result, removed = transcript.collapse_loops(self.phrases(*texts))
        self.assertEqual(([p.text for p in result], removed), (list(texts), 0))


class RenderAndWindowsTest(unittest.TestCase):
    def test_time_format(self):
        self.assertEqual([transcript.fmt_time(s) for s in (0, 59.9, 61, 3725)], ["0:00:00", "0:00:59", "0:01:01", "1:02:05"])

    def test_lines_group_about_thirty_seconds(self):
        phrases = [Phrase(0, 5, "раз"), Phrase(10, 12, "два"), Phrase(31, 33, "три"), Phrase(70, 72, "четыре")]
        self.assertEqual(transcript.render(phrases), "[0:00:00] раз два\n[0:00:31] три\n[0:01:10] четыре")
        self.assertEqual(transcript.render([]), "")

    def test_windows_overlap_and_cover_everything(self):
        phrases = [Phrase(minute * 60.0, minute * 60.0 + 5, f"м{minute}") for minute in range(0, 150, 10)]
        parts = transcript.windows(phrases, minutes=60, overlap_minutes=10)
        self.assertEqual([(p[0].text, p[-1].text) for p in parts], [("м0", "м50"), ("м50", "м100"), ("м100", "м140")])
        self.assertEqual({p.text for part in parts for p in part}, {p.text for p in phrases})

    def test_short_stream_is_one_window(self):
        phrases = [Phrase(0, 1, "а"), Phrase(600, 601, "б")]
        self.assertEqual(len(transcript.windows(phrases)), 1)
        self.assertEqual(transcript.windows([]), [])

    def test_windows_start_at_the_first_phrase_not_at_zero(self):
        phrases = [Phrase(10_800.0 + minute * 60, 10_805.0 + minute * 60, f"м{minute}") for minute in range(0, 90)]
        parts = transcript.windows(phrases, minutes=60, overlap_minutes=2)
        self.assertEqual([(p[0].text, p[-1].text) for p in parts], [("м0", "м59"), ("м58", "м89")])

    def test_overlap_must_be_smaller_than_window(self):
        with self.assertRaises(ValueError):
            transcript.windows([Phrase(0, 1, "а")], minutes=10, overlap_minutes=10)

    def test_json_round_trip(self):
        phrases = [Phrase(1.234, 2.5, "Привет"), Phrase(3.0, 4.0, 'с "кавычками"')]
        self.assertEqual(transcript.load(transcript.dump(phrases)), [Phrase(1.23, 2.5, "Привет"), Phrase(3.0, 4.0, 'с "кавычками"')])


class AudioCommandTest(unittest.TestCase):
    def test_command_cuts_audio_into_chunks_and_writes_listing(self):
        command = audio.ffmpeg_command("ffmpeg", "https://x/audio.m3u8", Path("/d/chunk-%03d.flac"), Path("/d/chunks.csv"), chunk_sec=600)
        self.assertEqual(command[0], "ffmpeg")
        self.assertIn("-vn", command)
        self.assertEqual(command[command.index("-i") + 1], "https://x/audio.m3u8")
        self.assertEqual(command[command.index("-ar") + 1], "16000")
        self.assertEqual(command[command.index("-ac") + 1], "1")
        self.assertEqual(command[command.index("-segment_time") + 1], "600")
        self.assertEqual(command[command.index("-segment_list") + 1], "/d/chunks.csv")
        self.assertEqual(command[-1], "/d/chunk-%03d.flac")
        self.assertNotIn("-t", command)

    def test_limit_goes_before_input(self):
        command = audio.ffmpeg_command("ffmpeg", "src", Path("o-%03d.flac"), Path("l.csv"), limit_sec=1200)
        self.assertLess(command.index("-t"), command.index("-i"))
        self.assertEqual(command[command.index("-t") + 1], "1200")

    def test_start_goes_before_input_and_shifts_chunk_times(self):
        command = audio.ffmpeg_command("ffmpeg", "src", Path("o-%03d.flac"), Path("l.csv"), start_sec=3600, limit_sec=600)
        self.assertLess(command.index("-ss"), command.index("-i"))
        self.assertEqual(command[command.index("-ss") + 1], "3600")
        self.assertNotIn("-ss", audio.ffmpeg_command("ffmpeg", "src", Path("o"), Path("l")))
        chunks = audio.parse_listing("chunk-000.flac,0.0,600.0\nchunk-001.flac,600.0,900.0\n", Path("/d"), 3600)
        self.assertEqual([(c.start, c.end) for c in chunks], [(3600.0, 4200.0), (4200.0, 4500.0)])

    def test_listing_gives_offsets_in_order(self):
        text = "chunk-001.flac,600.000000,1200.010000\nchunk-000.flac,0.000000,600.000000\n\nbroken\n"
        chunks = audio.parse_listing(text, Path("/d"))
        self.assertEqual([(c.path.name, c.start, c.end) for c in chunks],
                         [("chunk-000.flac", 0.0, 600.0), ("chunk-001.flac", 600.0, 1200.01)])
        self.assertAlmostEqual(chunks[1].duration, 600.01)


class AudioExtractionTest(unittest.IsolatedAsyncioTestCase):
    async def test_last_flac_header_describes_only_its_chunk(self):
        try:
            import imageio_ffmpeg
        except ImportError:
            self.skipTest("нужен imageio-ffmpeg")

        with tempfile.TemporaryDirectory() as folder:
            playlist = Path(folder) / "source.m3u8"
            code, _, err = await audio.run(
                imageio_ffmpeg.get_ffmpeg_exe(), "-hide_banner", "-loglevel", "error",
                "-f", "lavfi", "-i", "sine=frequency=500:sample_rate=48000",
                "-t", "5", "-c:a", "aac", "-f", "hls", "-hls_time", "2",
                "-hls_playlist_type", "vod", str(playlist), timeout=20,
            )
            self.assertEqual(code, 0, err)
            chunks = await audio.extract_chunks(str(playlist), Path(folder) / "audio", chunk_sec=2, limit_sec=3)
            self.assertEqual(len(chunks), 2)
            first, last = chunks
            self.assertEqual(last.start, first.end)
            with last.path.open("rb") as flac:
                header = flac.read(42)
            self.assertEqual(header[:4], b"fLaC")
            stream_info = int.from_bytes(header[18:26], "big")
            sample_rate = stream_info >> 44
            samples = stream_info & ((1 << 36) - 1)
            self.assertEqual(sample_rate, 16000)
            self.assertAlmostEqual(samples / sample_rate, last.duration, delta=0.02)


if __name__ == "__main__":
    unittest.main()
