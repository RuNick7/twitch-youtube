"""Проверка нейросетей на настоящей записи, без запуска бота.

Шаг 1, расшифровка (ключ Groq):

    GROQ_API_KEY=... python -m app.ai transcribe <ID записи Twitch> [--start 60] [--minutes 20]

пишет в <папка>/<ID записи> transcript.json, transcript.txt (то, что увидит языковая модель) и report.json
(время, размеры, цена, сколько фраз отброшено и почему).

Шаги 2 и 3, таймкоды и названия по готовой расшифровке (ключ Anthropic):

    ANTHROPIC_API_KEY=... python -m app.ai analyze <ID записи Twitch>

режет запись на сегменты так же, как бот (по главам Twitch), и для каждого сегмента показывает моменты (игра,
разговор, чужое видео, перерыв) и предложенное название; пишет analysis.json и analysis.md.

Видео не скачивается, ключи нигде не сохраняются.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from dataclasses import asdict
from pathlib import Path

import httpx

from .. import segments, tools
from ..tools import ToolError
from . import audio, claude, groq, timeline, titles, transcript

MIN_SEGMENT_SEC = 120  # как min_segment_sec у бота
SKIP_SHORTER_SEC = 7 * 60  # как skip_shorter_min у бота: такие ролики не загружаются


async def run_transcribe(args: argparse.Namespace) -> int:
    key = os.environ.get("GROQ_API_KEY", "").strip()
    if not key:
        print("Нужна переменная окружения GROQ_API_KEY.")
        return 2
    out = Path(args.out) / args.vod_id.lstrip("v")
    out.mkdir(parents=True, exist_ok=True)

    started = time.monotonic()
    try:
        url = await audio.audio_playlist_url(args.vod_id.lstrip("v"))
        chunks = await audio.extract_chunks(
            url,
            out / "audio",
            chunk_sec=args.chunk * 60,
            limit_sec=args.minutes * 60 if args.minutes else None,
            start_sec=args.start * 60,
        )
    except ToolError as exc:
        print(f"Звук не получен: {exc}")
        return 1
    extracted = time.monotonic() - started
    size_mb = sum(chunk.path.stat().st_size for chunk in chunks) / 1e6
    print(f"Звук: {len(chunks)} кусков, {sum(c.duration for c in chunks) / 60:.0f} мин, {size_mb:.0f} МБ, "
          f"получен за {extracted:.0f} с; самый большой кусок {max(c.path.stat().st_size for c in chunks) / 1e6:.1f} МБ")

    phrases: list[transcript.Phrase] = []
    dropped: Counter = Counter()
    chunk_reports = []
    async with httpx.AsyncClient() as http:
        for number, chunk in enumerate(chunks, 1):
            began = time.monotonic()
            try:
                data = await groq.transcribe_file(http, key, chunk.path, model=args.model, language=args.language)
            except groq.TranscribeError as exc:
                print(f"Кусок {number}: {exc}")
                return 1
            part, part_dropped = transcript.parse_verbose(data, chunk.start)
            phrases += part
            dropped += part_dropped
            took = time.monotonic() - began
            chunk_reports.append({"chunk": chunk.path.name, "start": chunk.start, "seconds": round(chunk.duration, 1),
                                  "took": round(took, 1), "phrases": len(part), "dropped": dict(part_dropped)})
            print(f"Кусок {number}/{len(chunks)}: {chunk.duration / 60:.0f} мин за {took:.1f} с, фраз {len(part)}, "
                  f"отброшено {sum(part_dropped.values())}")

    phrases.sort(key=lambda phrase: phrase.start)
    (out / "transcript.json").write_text(transcript.dump(phrases))
    (out / "transcript.txt").write_text(transcript.render(phrases))
    report = {
        "vod": args.vod_id, "model": args.model, "language": args.language, "start_minute": args.start,
        "audio_minutes": round(sum(c.duration for c in chunks) / 60, 1), "audio_mb": round(size_mb, 1),
        "extract_seconds": round(extracted), "total_seconds": round(time.monotonic() - started),
        "cost_usd": round(groq.cost([c.duration for c in chunks], args.model), 4),
        "phrases": len(phrases), "words": sum(len(p.text.split()) for p in phrases), "dropped": dict(dropped),
        "chunks": chunk_reports,
    }
    (out / "report.json").write_text(json.dumps(report, ensure_ascii=False, indent=2))
    if not args.keep_audio:
        shutil.rmtree(out / "audio", ignore_errors=True)

    print(f"\nГотово за {report['total_seconds']} с: {report['phrases']} фраз, {report['words']} слов, "
          f"цена ≈ ${report['cost_usd']:.3f}. Отброшено: {dict(dropped) or 'ничего'}")
    print(f"Файлы: {out}")
    return 0


async def run_analyze(args: argparse.Namespace) -> int:
    key = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if not key:
        print("Нужна переменная окружения ANTHROPIC_API_KEY.")
        return 2
    vod_id = args.vod_id.lstrip("v")
    folder = Path(args.out) / vod_id
    source = folder / "transcript.json"
    if not source.exists():
        print(f"Нет расшифровки {source}. Сначала: python -m app.ai transcribe {vod_id}")
        return 1
    phrases = transcript.load(source.read_text())
    if not phrases:
        print("Расшифровка пустая: в записи не нашлось речи.")
        return 1
    try:
        info = await tools.fetch_vod_info(vod_id)
    except ToolError as exc:
        print(f"Данные записи не получены: {exc}")
        return 1

    name = args.name or info.uploader
    chapters = segments.normalize_chapters(info.chapters, info.duration)
    planned = segments.plan_segments(chapters, min_sec=MIN_SEGMENT_SEC, join=True)
    context = timeline.Context(name, info.title, [(c.start, c.end, c.title) for c in chapters])
    ask = functools.partial(claude.ask_json, claude.make_client(key), model=args.model)
    print(f"Запись {vod_id}: «{info.title}», {info.duration / 3600:.1f} ч, сегментов по главам Twitch: {len(planned)}; "
          f"расшифровка покрывает {phrases[-1].end / 60:.0f} мин\n")

    usage = claude.Usage()
    rows: list[dict] = []
    failure = None
    for number, seg in enumerate(planned, 1):
        stream_title = seg.stream_title or info.title
        row = {"number": number, "category": seg.category, "stream_title": stream_title,
               "spans": seg.spans, "minutes": round(seg.duration / 60, 1)}
        rows.append(row)
        label = f"{number}. {seg.category} · {', '.join(transcript.fmt_time(a) + '–' + transcript.fmt_time(b) for a, b in seg.spans)}"
        if seg.duration < SKIP_SHORTER_SEC:
            row["skipped"] = "короче 7 минут: бот такой ролик не загружает"
        else:
            inside = [p for p in phrases if any(a <= p.start < b for a, b in seg.spans)]
            if not inside:
                row["skipped"] = f"нет расшифровки на этом участке (она есть только до {phrases[-1].end / 60:.0f}-й минуты)"
            else:
                try:
                    moments, used = await timeline.analyze(inside, context, ask, minutes=args.window, effort=args.effort)
                    usage.add(used)
                    middle, reason, used = await titles.suggest(
                        ask, streamer=name, stream_title=stream_title, category=seg.category, moments=moments,
                        effort=args.effort,
                    )
                    usage.add(used)
                except claude.ClaudeError as exc:
                    failure = f"{label}: {exc}"
                    row["skipped"] = f"ошибка Claude: {exc}"
                    print(label, "\n  ОШИБКА:", exc)
                    break
                row.update(
                    moments=[asdict(m) for m in moments],
                    coverage=round(timeline.coverage(moments, inside[0].start, inside[-1].end), 2),
                    middle=middle, middle_rejected=reason,
                    title=titles.compose(seg.category, middle, stream_title, name),
                    title_now=segments.build_title(seg.category, stream_title, name),
                )
        print(label, f"({row['minutes']} мин)")
        if "skipped" in row:
            print(f"  пропущен: {row['skipped']}\n")
            continue
        print(timeline.render([timeline.Moment(**m) for m in row["moments"]]))
        print(f"  покрытие моментами: {row['coverage']:.0%}")
        print(f"  название сейчас: {row['title_now']}")
        print(f"  название от Haiku: {row['title']}" + (f"  (не взято: {row['middle_rejected']})" if row["middle_rejected"] else "") + "\n")

    (folder / "analysis.json").write_text(json.dumps({"vod": vod_id, "model": args.model, "effort": args.effort,
                                                      "usage": asdict(usage), "segments": rows}, ensure_ascii=False, indent=2))
    print(f"Расход: запросов {usage.requests}, токенов {usage.input_tokens} на входе и {usage.output_tokens} на выходе, "
          f"цена ≈ ${usage.cost_usd:.4f}. Файл: {folder / 'analysis.json'}")
    if failure:
        print("Разбор остановлен:", failure)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ai", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("transcribe", help="расшифровать звук записи Twitch через Groq")
    cmd.add_argument("vod_id", help="ID записи, например 2927594240")
    cmd.add_argument("--minutes", type=int, default=0, help="взять только N минут (для пробы); 0 — до конца записи")
    cmd.add_argument("--start", type=int, default=0, help="с какой минуты записи начать")
    cmd.add_argument("--chunk", type=int, default=audio.CHUNK_SEC // 60, help="длина куска в минутах")
    cmd.add_argument("--model", default=groq.MODEL)
    cmd.add_argument("--language", default="ru")
    cmd.add_argument("--out", default=os.environ.get("AI_LAB_DIR", "/data/ai-lab"))
    cmd.add_argument("--keep-audio", action="store_true", help="не удалять куски звука после расшифровки")
    ana = commands.add_parser("analyze", help="таймкоды и названия по готовой расшифровке через Claude Haiku")
    ana.add_argument("vod_id", help="ID записи: расшифровка должна лежать в папке --out")
    ana.add_argument("--name", default="", help="имя стримера в названиях (по умолчанию — название канала на Twitch)")
    ana.add_argument("--window", type=int, default=60, help="длина окна для одного запроса, минут")
    ana.add_argument("--effort", default="low", choices=("low", "medium", "high"), help="глубина размышлений модели")
    ana.add_argument("--model", default=claude.MODEL)
    ana.add_argument("--out", default=os.environ.get("AI_LAB_DIR", "/data/ai-lab"))
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run_transcribe(args) if args.command == "transcribe" else run_analyze(args))


if __name__ == "__main__":
    sys.exit(main())
