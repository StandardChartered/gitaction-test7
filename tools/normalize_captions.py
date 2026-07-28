#!/usr/bin/env python3
"""Normalize common caption formats into timestamped JSON and plain text."""
from __future__ import annotations

import argparse
import html
import json
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

TIME_LINE = re.compile(
    r"(?P<a>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})\s+-->\s+"
    r"(?P<b>(?:\d{1,2}:)?\d{2}:\d{2}[.,]\d{3})"
)


def sec(value: str | float | int | None) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    value = value.strip().replace(',', '.')
    if value.endswith('ms'):
        return float(value[:-2]) / 1000.0
    if value.endswith('s') and ':' not in value:
        return float(value[:-1])
    parts = value.split(':')
    if len(parts) == 3:
        return int(parts[0]) * 3600 + int(parts[1]) * 60 + float(parts[2])
    if len(parts) == 2:
        return int(parts[0]) * 60 + float(parts[1])
    return float(value)


def clean(text: str) -> str:
    text = html.unescape(text)
    text = re.sub(r'<[^>]+>', ' ', text)
    text = re.sub(r'\s+', ' ', text).strip()
    return text


def parse_json3(path: Path) -> list[dict[str, Any]]:
    data = json.loads(path.read_text(encoding='utf-8-sig', errors='replace'))
    result = []
    for event in data.get('events', []):
        text = clean(''.join(seg.get('utf8', '') for seg in event.get('segs', [])))
        if not text:
            continue
        start = float(event.get('tStartMs', 0)) / 1000.0
        duration = float(event.get('dDurationMs', 0)) / 1000.0
        result.append({'start': start, 'end': start + duration, 'text': text})
    return result


def parse_vtt_srt(path: Path) -> list[dict[str, Any]]:
    lines = path.read_text(encoding='utf-8-sig', errors='replace').splitlines()
    result: list[dict[str, Any]] = []
    current: tuple[float, float] | None = None
    buffer: list[str] = []

    def flush() -> None:
        nonlocal current, buffer
        if current and buffer:
            text = clean(' '.join(buffer))
            if text:
                result.append({'start': current[0], 'end': current[1], 'text': text})
        current = None
        buffer = []

    for line in lines:
        match = TIME_LINE.search(line)
        if match:
            flush()
            current = (sec(match.group('a')), sec(match.group('b')))
        elif current and line.strip() and not line.strip().isdigit():
            buffer.append(line.strip())
        elif not line.strip():
            flush()
    flush()
    return result


def parse_xml(path: Path) -> list[dict[str, Any]]:
    text = path.read_text(encoding='utf-8-sig', errors='replace')
    root = ET.fromstring(text)
    result = []
    for node in root.iter():
        tag = node.tag.rsplit('}', 1)[-1].lower()
        if tag not in {'text', 'p'}:
            continue
        content = clean(''.join(node.itertext()))
        if not content:
            continue
        attrs = {k.rsplit('}', 1)[-1]: v for k, v in node.attrib.items()}
        start = sec(attrs.get('start') or attrs.get('begin'))
        if attrs.get('dur') is not None:
            end = start + sec(attrs['dur'])
        else:
            end = sec(attrs.get('end'))
        if end <= start:
            end = start + 3.0
        result.append({'start': start, 'end': end, 'text': content})
    return result


def detect_and_parse(path: Path) -> list[dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix in {'.json3', '.json'}:
        try:
            return parse_json3(path)
        except Exception:
            pass
    if suffix in {'.vtt', '.srt', '.txt'}:
        parsed = parse_vtt_srt(path)
        if parsed:
            return parsed
    try:
        return parse_xml(path)
    except Exception:
        return []


def score(path: Path, segments: list[dict[str, Any]]) -> tuple[int, int, int]:
    name = path.name.lower()
    korean = 1 if re.search(r'(^|[._-])ko([._-]|$)', name) else 0
    manual = 1 if 'auto' not in name and 'asr' not in name else 0
    return korean, manual, len(segments)


def dedupe(segments: list[dict[str, Any]]) -> list[dict[str, Any]]:
    segments.sort(key=lambda x: (float(x['start']), float(x['end'])))
    result: list[dict[str, Any]] = []
    for item in segments:
        item = {'start': round(float(item['start']), 3), 'end': round(float(item['end']), 3), 'text': clean(str(item['text']))}
        if not item['text']:
            continue
        if result and item['text'] == result[-1]['text']:
            result[-1]['end'] = max(result[-1]['end'], item['end'])
            continue
        if result and item['start'] <= result[-1]['end'] + 0.25:
            previous = result[-1]['text']
            if item['text'].startswith(previous) and len(item['text']) > len(previous):
                result[-1] = item
                continue
        result.append(item)
    return result


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument('source', type=Path)
    parser.add_argument('output', type=Path)
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    candidates = []
    for path in sorted(args.source.iterdir()):
        if not path.is_file():
            continue
        lower = path.name.lower()
        if not any(token in lower for token in ('caption', 'subtitle', '.vtt', '.srt', '.json3', '.ttml', '.srv', '.xml')):
            continue
        segments = detect_and_parse(path)
        if segments:
            candidates.append((score(path, segments), path, segments))
    if not candidates:
        (args.output / 'NO_CAPTIONS_FOUND.txt').write_text('No parseable caption file found.', encoding='utf-8')
        print('no parseable captions')
        return 2

    candidates.sort(key=lambda item: item[0], reverse=True)
    _, chosen, segments = candidates[0]
    segments = dedupe(segments)
    (args.output / 'source_caption_file.txt').write_text(chosen.name, encoding='utf-8')
    (args.output / 'caption_segments.json').write_text(json.dumps(segments, ensure_ascii=False, indent=2), encoding='utf-8')
    (args.output / 'caption_transcript.txt').write_text(
        '\n'.join(f"[{s['start']:09.3f} - {s['end']:09.3f}] {s['text']}" for s in segments), encoding='utf-8'
    )
    (args.output / 'caption_plain.txt').write_text('\n'.join(s['text'] for s in segments), encoding='utf-8')
    (args.output / 'caption_candidates.json').write_text(
        json.dumps([{'file': p.name, 'score': list(sc), 'segments': len(seg)} for sc, p, seg in candidates], ensure_ascii=False, indent=2),
        encoding='utf-8',
    )
    print(f'normalized {len(segments)} segments from {chosen.name}')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
