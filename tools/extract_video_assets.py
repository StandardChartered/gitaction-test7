#!/usr/bin/env python3
"""Extract visually distinct frames from a long lecture video.

The script samples the source at a fixed rate, compares perceptual hashes and
blurred pixel differences, preserves periodic coverage frames, and writes a
manifest plus contact sheets. It deliberately favors recall over compactness so
short-lived diagrams and inserted pictures are less likely to be omitted.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import shutil
import subprocess
import sys
from dataclasses import dataclass, asdict
from pathlib import Path
from typing import Iterable

import imagehash
import numpy as np
from PIL import Image, ImageDraw, ImageFilter, ImageFont, ImageOps


@dataclass
class SelectedFrame:
    sample_index: int
    timestamp_seconds: float
    timestamp: str
    filename: str
    phash_distance: int
    mean_absolute_difference: float
    periodic_capture: bool
    reason: str


def run(cmd: list[str]) -> None:
    print("+", " ".join(cmd), flush=True)
    subprocess.run(cmd, check=True)


def ffprobe_duration(video: Path) -> float:
    result = subprocess.run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "default=noprint_wrappers=1:nokey=1",
            str(video),
        ],
        check=True,
        text=True,
        capture_output=True,
    )
    return float(result.stdout.strip())


def format_timestamp(seconds: float) -> str:
    milliseconds = int(round((seconds - math.floor(seconds)) * 1000))
    whole = int(math.floor(seconds))
    if milliseconds == 1000:
        whole += 1
        milliseconds = 0
    hours, rem = divmod(whole, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}.{milliseconds:03d}"


def filename_timestamp(seconds: float) -> str:
    text = format_timestamp(seconds)
    return text.replace(":", "h", 1).replace(":", "m", 1).replace(".", "s")


def analysis_representation(image: Image.Image) -> tuple[np.ndarray, imagehash.ImageHash]:
    gray = ImageOps.grayscale(image)
    gray = gray.resize((192, 108), Image.Resampling.LANCZOS)
    gray = gray.filter(ImageFilter.GaussianBlur(radius=1.6))
    arr = np.asarray(gray, dtype=np.float32) / 255.0
    phash = imagehash.phash(gray, hash_size=16, highfreq_factor=4)
    return arr, phash


def iter_jpegs(directory: Path) -> Iterable[Path]:
    yield from sorted(directory.glob("sample_*.jpg"))


def make_contact_sheets(
    records: list[SelectedFrame],
    selected_dir: Path,
    sheets_dir: Path,
    columns: int = 4,
    rows: int = 4,
) -> None:
    sheets_dir.mkdir(parents=True, exist_ok=True)
    cell_w, cell_h = 360, 236
    thumb_w, thumb_h = 340, 191
    margin = 10
    per_sheet = columns * rows
    try:
        font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 18)
        small_font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", 13)
    except OSError:
        font = ImageFont.load_default()
        small_font = font

    for sheet_index in range(0, len(records), per_sheet):
        group = records[sheet_index : sheet_index + per_sheet]
        sheet = Image.new("RGB", (columns * cell_w, rows * cell_h), "white")
        draw = ImageDraw.Draw(sheet)
        for position, record in enumerate(group):
            row, col = divmod(position, columns)
            x = col * cell_w + margin
            y = row * cell_h + margin
            with Image.open(selected_dir / record.filename) as frame:
                frame = ImageOps.exif_transpose(frame).convert("RGB")
                frame.thumbnail((thumb_w, thumb_h), Image.Resampling.LANCZOS)
                paste_x = x + (thumb_w - frame.width) // 2
                paste_y = y + (thumb_h - frame.height) // 2
                sheet.paste(frame, (paste_x, paste_y))
            text_y = y + thumb_h + 4
            draw.text((x, text_y), record.timestamp, fill="black", font=font)
            draw.text(
                (x, text_y + 23),
                f"#{record.sample_index}  {record.reason}",
                fill="#444444",
                font=small_font,
            )
        sheet_no = sheet_index // per_sheet + 1
        sheet.save(sheets_dir / f"contact_{sheet_no:04d}.jpg", quality=88, optimize=True)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("video", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--sample-fps", type=float, default=2.0)
    parser.add_argument("--phash-threshold", type=int, default=9)
    parser.add_argument("--mad-threshold", type=float, default=0.026)
    parser.add_argument("--periodic-seconds", type=float, default=40.0)
    args = parser.parse_args()

    video = args.video.resolve()
    output = args.output.resolve()
    samples_dir = output / "_samples"
    selected_dir = output / "selected"
    sheets_dir = output / "contact_sheets"
    output.mkdir(parents=True, exist_ok=True)
    samples_dir.mkdir(parents=True, exist_ok=True)
    selected_dir.mkdir(parents=True, exist_ok=True)

    duration = ffprobe_duration(video)
    metadata = {
        "video": str(video),
        "duration_seconds": duration,
        "sample_fps": args.sample_fps,
        "phash_threshold": args.phash_threshold,
        "mad_threshold": args.mad_threshold,
        "periodic_seconds": args.periodic_seconds,
    }
    (output / "extraction_config.json").write_text(
        json.dumps(metadata, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    vf = f"fps={args.sample_fps},scale=w='min(1280,iw)':h=-2"
    run(
        [
            "ffmpeg",
            "-hide_banner",
            "-loglevel",
            "warning",
            "-y",
            "-i",
            str(video),
            "-vf",
            vf,
            "-q:v",
            "3",
            str(samples_dir / "sample_%07d.jpg"),
        ]
    )

    sample_files = list(iter_jpegs(samples_dir))
    if not sample_files:
        raise RuntimeError("ffmpeg produced no sample frames")

    records: list[SelectedFrame] = []
    last_arr: np.ndarray | None = None
    last_hash: imagehash.ImageHash | None = None
    last_selected_time = -1e9

    for zero_index, sample_path in enumerate(sample_files):
        sample_index = zero_index + 1
        timestamp_seconds = zero_index / args.sample_fps
        with Image.open(sample_path) as source:
            image = ImageOps.exif_transpose(source).convert("RGB")
            arr, phash = analysis_representation(image)

            if last_arr is None or last_hash is None:
                distance = 256
                mad = 1.0
                periodic = False
                selected = True
                reason = "first"
            else:
                distance = int(phash - last_hash)
                mad = float(np.mean(np.abs(arr - last_arr)))
                periodic = timestamp_seconds - last_selected_time >= args.periodic_seconds
                major_change = distance >= args.phash_threshold or mad >= args.mad_threshold
                selected = major_change or periodic
                if major_change and periodic:
                    reason = "change+coverage"
                elif major_change:
                    reason = "visual-change"
                else:
                    reason = "coverage"

            is_last = sample_index == len(sample_files)
            if is_last and not selected:
                selected = True
                periodic = False
                reason = "last"

            if selected:
                ts_text = format_timestamp(timestamp_seconds)
                ts_file = filename_timestamp(timestamp_seconds)
                filename = f"frame_{len(records)+1:05d}_{ts_file}.jpg"
                destination = selected_dir / filename
                image.save(destination, format="JPEG", quality=90, optimize=True, progressive=True)
                records.append(
                    SelectedFrame(
                        sample_index=sample_index,
                        timestamp_seconds=round(timestamp_seconds, 3),
                        timestamp=ts_text,
                        filename=filename,
                        phash_distance=distance,
                        mean_absolute_difference=round(mad, 6),
                        periodic_capture=periodic,
                        reason=reason,
                    )
                )
                last_arr = arr
                last_hash = phash
                last_selected_time = timestamp_seconds

        if sample_index % 2000 == 0:
            print(
                f"processed {sample_index}/{len(sample_files)} samples; selected {len(records)}",
                flush=True,
            )

    with (output / "manifest.csv").open("w", newline="", encoding="utf-8-sig") as fp:
        writer = csv.DictWriter(fp, fieldnames=list(asdict(records[0]).keys()))
        writer.writeheader()
        writer.writerows(asdict(record) for record in records)

    (output / "manifest.json").write_text(
        json.dumps([asdict(record) for record in records], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )

    summary = {
        **metadata,
        "sample_count": len(sample_files),
        "selected_count": len(records),
        "selection_ratio": round(len(records) / len(sample_files), 6),
        "first_timestamp": records[0].timestamp,
        "last_timestamp": records[-1].timestamp,
    }
    (output / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    make_contact_sheets(records, selected_dir, sheets_dir)
    shutil.rmtree(samples_dir, ignore_errors=True)
    print(json.dumps(summary, ensure_ascii=False, indent=2), flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except Exception as exc:  # noqa: BLE001
        print(f"fatal: {exc}", file=sys.stderr, flush=True)
        raise
