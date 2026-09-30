import unittest

from app.checks import verify_probe


def probe(duration, video=None, audio=None):
    streams = []
    if video is not None:
        streams.append({"codec_type": "video", "duration": str(video)})
    if audio is not None:
        streams.append({"codec_type": "audio", "duration": str(audio)})
    return {"format": {"duration": str(duration)}, "streams": streams}


class VerifyProbeTest(unittest.TestCase):
    def test_whole_file_passes(self):
        self.assertIsNone(verify_probe(probe(3601.5, 3601.5, 3601.4), expected=3600))

    def test_keyframe_slack_is_allowed(self):
        self.assertIsNone(verify_probe(probe(3604, 3604, 3604), expected=3600))

    def test_truncated_file(self):
        self.assertIn("вместо", verify_probe(probe(1800, 1800, 1800), expected=3600))

    def test_video_shorter_than_audio(self):
        # так выглядит баг yt-dlp #15825: конец видео «сжат», звук идёт до конца
        self.assertIn("разной длины", verify_probe(probe(10800, 9000, 10800), expected=10800))

    def test_no_video(self):
        self.assertEqual(verify_probe(probe(3600, audio=3600), expected=3600), "в файле нет видео")

    def test_unknown_duration(self):
        self.assertIsNotNone(verify_probe({"format": {}, "streams": []}, expected=3600))
        self.assertIsNotNone(verify_probe({"format": {"duration": "N/A"}}, expected=3600))

    def test_missing_stream_durations_are_tolerated(self):
        data = {"format": {"duration": "3600"}, "streams": [{"codec_type": "video"}, {"codec_type": "audio"}]}
        self.assertIsNone(verify_probe(data, expected=3600))


if __name__ == "__main__":
    unittest.main()
