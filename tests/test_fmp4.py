import struct
import unittest
from fractions import Fraction

from app.fmp4 import (
    Part,
    base_time,
    boxes,
    first_times,
    fragment_end_times,
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


def fragment(times: dict, *, emsg: bool = True, durations: dict = None) -> bytes:
    """Как у Twitch: emsg с ID3, затем moof+mdat; звук с 32-битным tfdt, видео с 64-битным.

    durations — длительности сэмплов по дорожкам (trun с флагом 0x100).
    """
    trafs = b""
    for track, value in times.items():
        version = 1 if track == VIDEO else 0
        tfdt = full(b"tfdt", version, struct.pack(">Q" if version else ">I", value))
        tfhd = full(b"tfhd", 0, struct.pack(">I", track))
        samples = (durations or {}).get(track, [])
        trun = box(b"trun", bytes([0, 0, 0x03, 0x01]) + struct.pack(">Ii", len(samples), 0)
                   + b"".join(struct.pack(">II", d, 100) for d in samples))
        trafs += box(b"traf", tfhd + tfdt + trun)
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



class JoinTest(unittest.TestCase):
    def test_fragment_end_times(self):
        data = fragment({AUDIO: 1000, VIDEO: 5000}, durations={AUDIO: [1024, 1024], VIDEO: [16666, 16667, 16667]})
        self.assertEqual(fragment_end_times(data), {AUDIO: 3048, VIDEO: 55000})

    def test_end_without_sample_durations_is_an_error(self):
        data = bytearray(fragment({AUDIO: 1000}))
        data[data.index(b"trun") + 6] = 0x00  # флаги 0x000001: длительностей нет, а в tfhd нет значения по умолчанию
        with self.assertRaises(ValueError):
            fragment_end_times(bytes(data))

    def test_second_fragment_continues_the_first(self):
        # Первый отрезок: 4:00:00–4:00:10, второй — с 6:00:00; во втором ролике он должен начаться с 0:00:10
        first = shifts(Fraction(14400), TIMESCALES)
        second = shifts(Fraction(21600), TIMESCALES, offset=Fraction(10))
        a = bytearray(fragment({AUDIO: 14400 * 48000, VIDEO: 14400 * 10**6}))
        b = bytearray(fragment({AUDIO: 21600 * 48000, VIDEO: 21600 * 10**6}))
        rebase(a, first)
        rebase(b, second)
        self.assertEqual(tfdts(bytes(a)), {AUDIO: 0, VIDEO: 0})
        self.assertEqual(tfdts(bytes(b)), {AUDIO: 10 * 48000, VIDEO: 10 * 10**6})

    def test_offset_is_rounded_up_so_fragments_never_overlap(self):
        shift = shifts(Fraction(100), {AUDIO: 48000}, offset=Fraction(1, 3))
        self.assertEqual(shift[AUDIO], 100 * 48000 - 16000)
        shift = shifts(Fraction(100), {AUDIO: 48000}, offset=Fraction(1, 7))
        self.assertEqual(shift[AUDIO], 100 * 48000 - 6858)  # 6857,14 тика округлены вверх

    def test_audio_time_must_fit_32_bits(self):
        data = bytearray(fragment({AUDIO: 10}))
        with self.assertRaises(ValueError):
            rebase(data, {AUDIO: -(2**32)})

if __name__ == "__main__":
    unittest.main()
