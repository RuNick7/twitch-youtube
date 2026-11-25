"""Проверка расшифровки на настоящей записи, без запуска бота:

    GROQ_API_KEY=... python -m app.ai transcribe <ID записи Twitch> [--minutes 20]

Пишет в <папка>/<ID записи>: transcript.json, transcript.txt (то, что увидит языковая модель) и report.json
(время, размеры, цена, сколько фраз отброшено и почему). Видео не скачивается, ключ нигде не сохраняется.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import shutil
import sys
import time
from collections import Counter
from pathlib import Path

import httpx

from ..tools import ToolError
from . import audio, groq, transcript


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
            url, out / "audio", chunk_sec=args.chunk * 60, limit_sec=args.minutes * 60 if args.minutes else None
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
        "vod": args.vod_id, "model": args.model, "language": args.language,
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


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.ai", description=__doc__.split("\n\n")[0])
    commands = parser.add_subparsers(dest="command", required=True)
    cmd = commands.add_parser("transcribe", help="расшифровать звук записи Twitch через Groq")
    cmd.add_argument("vod_id", help="ID записи, например 2927594240")
    cmd.add_argument("--minutes", type=int, default=0, help="взять только первые N минут (для пробы); 0 — всю запись")
    cmd.add_argument("--chunk", type=int, default=audio.CHUNK_SEC // 60, help="длина куска в минутах")
    cmd.add_argument("--model", default=groq.MODEL)
    cmd.add_argument("--language", default="ru")
    cmd.add_argument("--out", default=os.environ.get("AI_LAB_DIR", "/data/ai-lab"))
    cmd.add_argument("--keep-audio", action="store_true", help="не удалять куски звука после расшифровки")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    return asyncio.run(run_transcribe(args))


if __name__ == "__main__":
    sys.exit(main())
