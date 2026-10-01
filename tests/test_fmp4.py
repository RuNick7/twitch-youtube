import struct
import unittest
from fractions import Fraction

from app.fmp4 import (
    Part,
    base_time,
    boxes,
    first_times,
    parse_playlist,
    rebase,
    select_parts,
    shifts,
    timescales_from_init,
)

AUDIO, VIDEO = 1, 2
TIMESCALES = {AUDIO: 48000, VIDEO: 1_000_000}


def box(kind: bytes, payload: bytes) -> bytes:
    return struct.pack(">I4s", 8 + len(payload), kind) + payload


def full(kind: bytes, version: int, payload: bytes) -> bytes:
    return box(kind, bytes([version, 0, 0, 0]) + payload)


def init_segment() -> bytes:
    traks = b""
    for track, timescale in TIMESCALES.items():
        tkhd = full(b"tkhd", 0, struct.pack(">III", 0, 0, track) + bytes(68))
        mdhd = full(b"mdhd", 0, struct.pack(">IIII", 0, 0, timescale, 0) + bytes(4))
        traks += box(b"trak", tkhd + box(b"mdia", mdhd))
    return box(b"ftyp", b"isom" + bytes(4)) + box(b"moov", full(b"mvhd", 0, bytes(96)) + traks)


def fragment(times: dict, *, emsg: bool = True) -> bytes:
    """Как у Twitch: emsg с ID3, затем moof+mdat; звук с 32-битным tfdt, видео с 64-битным."""
    trafs = b""
    for track, value in times.items():
        version = 1 if track == VIDEO else 0
        tfdt = full(b"tfdt", version, struct.pack(">Q" if version else ">I", value))
        tfhd = full(b"tfhd", 0, struct.pack(">I", track))
        trafs += box(b"traf", tfhd + tfdt + full(b"trun", 0, struct.pack(">I", 0)))
    moof = box(b"moof", full(b"mfhd", 0, struct.pack(">I", 1)) + trafs)
    return (full(b"emsg", 0, b"urn:twitch:id3\0") if emsg else b"") + moof + box(b"mdat", b"x" * 64)


def tfdts(data: bytes) -> dict:
    return first_times(data)


class PlaylistTest(unittest.TestCase):
    TEXT = (
        "#EXTM3U\n#EXT-X-VERSION:6\n#EXT-X-TARGETDURATION:10\n#EXT-X-MAP:URI=\"init-0.mp4\"\n"
        "#EXTINF:10.000,\n0.mp4\n#EXTINF:10.000,\n1-muted.mp4\n#EXTINF:4.5,\n2.mp4\n#EXT-X-ENDLIST\n"
    )

    def test_fmp4(self):
        playlist = parse_playlist(self.TEXT, "https://cdn.example/abc/1080p60/index-dvr.m3u8")
        self.assertEqual(playlist.init_url, "https://cdn.example/abc/1080p60/init-0.mp4")
        self.assertTrue(playlist.ended)
        self.assertEqual(
            [(p.url.rsplit("/", 1)[1], p.start, p.duration) for p in playlist.parts],
            [("0.mp4", 0.0, 10.0), ("1-muted.mp4", 10.0, 10.0), ("2.mp4", 20.0, 4.5)],
        )

    def test_ts_and_unfinished(self):
        playlist = parse_playlist("#EXTM3U\n#EXTINF:10,\n0.ts\n", "https://cdn.example/x/index.m3u8")
        self.assertIsNone(playlist.init_url)
        self.assertFalse(playlist.ended)
        self.assertEqual(playlist.parts[0].url, "https://cdn.example/x/0.ts")


class SelectPartsTest(unittest.TestCase):
    PARTS = [Part(f"{i}.mp4", i * 10.0, 10.0) for i in range(6)]

    def names(self, start, end):
        return [p.url for p in select_parts(self.PARTS, start, end)]

    def test_by_midpoint(self):
        self.assertEqual(self.names(0, 15), ["0.mp4"])
        self.assertEqual(self.names(15, 60), ["1.mp4", "2.mp4", "3.mp4", "4.mp4", "5.mp4"])

    def test_neighbours_cover_everything_once(self):
        cuts = [0, 12, 27, 33, 60]
        chosen = [name for a, b in zip(cuts, cuts[1:]) for name in self.names(a, b)]
        self.assertEqual(chosen, [p.url for p in self.PARTS])


class BoxesTest(unittest.TestCase):
    def test_walks_top_level(self):
        self.assertEqual([kind for kind, *_ in boxes(fragment({AUDIO: 1, VIDEO: 2}))], [b"emsg", b"moof", b"mdat"])

    def test_truncated_data_is_rejected(self):
        data = fragment({AUDIO: 1, VIDEO: 2})
        with self.assertRaises(ValueError):
            list(boxes(data[:-10]))
        with self.assertRaises(ValueError):
            list(boxes(data + b"\0\0\0"))

    def test_timescales(self):
        self.assertEqual(timescales_from_init(init_segment()), TIMESCALES)


class RebaseTest(unittest.TestCase):
    # Реальные значения из VOD zakvielchannel: звук и видео начинаются почти одновременно
    AUDIO_T, VIDEO_T = 723074080, 15064032000

    def test_first_times(self):
        self.assertEqual(tfdts(fragment({AUDIO: self.AUDIO_T, VIDEO: self.VIDEO_T})), {AUDIO: self.AUDIO_T, VIDEO: self.VIDEO_T})

    def test_new_zero_keeps_sync(self):
        base = base_time({AUDIO: self.AUDIO_T, VIDEO: self.VIDEO_T}, TIMESCALES)
        self.assertEqual(base, Fraction(self.VIDEO_T, 1_000_000))
        shift = shifts(base, TIMESCALES)
        data = bytearray(fragment({AUDIO: self.AUDIO_T, VIDEO: self.VIDEO_T}))
        size = len(data)
        rebase(data, shift)
        self.assertEqual(len(data), size)
        after = tfdts(bytes(data))
        self.assertEqual(after[VIDEO], 0)
        # звук по-прежнему отстаёт от видео на те же ~11 мс
        self.assertAlmostEqual(after[AUDIO] / 48000, self.AUDIO_T / 48000 - self.VIDEO_T / 1e6, places=4)

    def test_later_fragment_moves_by_same_shift(self):
        shift = shifts(Fraction(15064032, 1000), TIMESCALES)
        data = bytearray(fragment({AUDIO: self.AUDIO_T + 480000, VIDEO: self.VIDEO_T + 10_000_000}))
        rebase(data, shift)
        self.assertEqual(tfdts(bytes(data))[VIDEO], 10_000_000)

    def test_emsg_becomes_free(self):
        data = bytearray(fragment({AUDIO: self.AUDIO_T, VIDEO: self.VIDEO_T}))
        rebase(data, shifts(Fraction(15064, 1), TIMESCALES))
        self.assertEqual([kind for kind, *_ in boxes(data)], [b"free", b"moof", b"mdat"])

    def test_time_before_new_zero_is_an_error(self):
        data = bytearray(fragment({AUDIO: 100, VIDEO: 100}))
        with self.assertRaises(ValueError):
            rebase(data, {AUDIO: 1000, VIDEO: 0})

    def test_unknown_track_is_an_error(self):
        data = bytearray(fragment({3: 100}))
        with self.assertRaises(ValueError):
            rebase(data, {AUDIO: 0, VIDEO: 0})


if __name__ == "__main__":
    unittest.main()
