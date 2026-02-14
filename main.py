#!/usr/bin/env python3
import argparse
import base64
import json
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from pathlib import Path


SRT_TIMESTAMP_RE = re.compile(
    r"^\s*(\d{2}:\d{2}:\d{2},\d{3})\s*-->\s*(\d{2}:\d{2}:\d{2},\d{3})"
)
ANKI_CONNECT_URL = "http://127.0.0.1:8765"
ANKI_DECK_NAME = "Default"
ANKI_MODEL_NAME = "Basic"
ANKI_FRONT_FIELD = "Front"
ANKI_BACK_FIELD = "Back"


def normalize_text(text: str) -> str:
    text = text.lower().strip()
    text = re.sub(r"\s+", " ", text)
    return text


def safe_filename_from_sub_text(text: str, max_len: int = 80) -> str:
    cleaned = text.strip().lower()
    cleaned = re.sub(r"\s+", "_", cleaned)
    cleaned = re.sub(r"[^a-z0-9._-]", "", cleaned)
    cleaned = cleaned.strip("._-")
    if not cleaned:
        cleaned = "clip"
    return cleaned[:max_len]


def srt_to_seconds(ts: str) -> float:
    hh, mm, rest = ts.split(":")
    ss, ms = rest.split(",")
    return int(hh) * 3600 + int(mm) * 60 + int(ss) + int(ms) / 1000.0


def seconds_to_ffmpeg_time(seconds: float) -> str:
    if seconds < 0:
        seconds = 0
    hh = int(seconds // 3600)
    mm = int((seconds % 3600) // 60)
    ss = seconds % 60
    return f"{hh:02d}:{mm:02d}:{ss:06.3f}"


def parse_srt(path: Path):
    content = path.read_text(encoding="utf-8", errors="replace")
    blocks = re.split(r"\n\s*\n", content.strip(), flags=re.MULTILINE)
    entries = []
    for block in blocks:
        lines = [line.rstrip("\r") for line in block.splitlines() if line.strip()]
        if len(lines) < 2:
            continue
        time_line_idx = 1 if lines[0].isdigit() else 0
        if time_line_idx >= len(lines):
            continue
        m = SRT_TIMESTAMP_RE.match(lines[time_line_idx])
        if not m:
            continue
        start_ts, end_ts = m.group(1), m.group(2)
        text_lines = lines[time_line_idx + 1 :]
        text = " ".join(text_lines).strip()
        if not text:
            continue
        entries.append(
            {
                "start": srt_to_seconds(start_ts),
                "end": srt_to_seconds(end_ts),
                "text": text,
                "normalized": normalize_text(text),
            }
        )
    return entries


def find_matching_entries(entries, sub_input: str):
    target = normalize_text(sub_input)
    return [entry for entry in entries if target in entry["normalized"]]


def collect_text_in_range(entries, start_sec: float, end_sec: float) -> str:
    parts = []
    for entry in entries:
        if entry["end"] >= start_sec and entry["start"] <= end_sec:
            parts.append(entry["text"])
    if not parts:
        return "clip"
    return " ".join(parts)


def run_ffmpeg(input_video: Path, output_video: Path, start_sec: float, end_sec: float):
    start_str = seconds_to_ffmpeg_time(start_sec)
    end_str = seconds_to_ffmpeg_time(end_sec)
    cmd = [
        "ffmpeg",
        "-y",
        "-ss",
        start_str,
        "-to",
        end_str,
        "-i",
        str(input_video),
        "-vf",
        "scale=640:480",
        "-c:v",
        "libx264",
        "-preset",
        "veryfast",
        "-crf",
        "32",
        "-c:a",
        "aac",
        "-b:a",
        "64k",
        "-movflags",
        "+faststart",
        str(output_video),
    ]
    try:
        subprocess.run(cmd, check=True)
    except FileNotFoundError:
        raise RuntimeError("ffmpeg is not installed or not in PATH.")
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"ffmpeg failed with exit code {exc.returncode}.")


def escape_for_anki_query(text: str) -> str:
    return text.replace("\\", "\\\\").replace('"', '\\"')


def anki_connect_request(url: str, action: str, params: dict):
    payload = json.dumps({"action": action, "version": 6, "params": params}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Cannot connect to AnkiConnect at {url}: {exc}")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError:
        raise RuntimeError(f"Invalid AnkiConnect response: {raw}")

    if data.get("error"):
        raise RuntimeError(f"AnkiConnect error for '{action}': {data['error']}")
    return data.get("result")


def add_note_to_anki(output_video: Path, back_text: str, anki_url: str, deck_name: str):
    video_bytes = output_video.read_bytes()
    media_name = output_video.name
    media_b64 = base64.b64encode(video_bytes).decode("ascii")

    anki_connect_request(
        anki_url,
        "storeMediaFile",
        {"filename": media_name, "data": media_b64},
    )

    fields = {
        ANKI_FRONT_FIELD: f"[sound:{media_name}]",
        ANKI_BACK_FIELD: back_text,
    }
    note = {
        "deckName": deck_name,
        "modelName": ANKI_MODEL_NAME,
        "fields": fields,
        "options": {"allowDuplicate": True},
    }
    note_id = anki_connect_request(anki_url, "addNote", {"note": note})
    return note_id


def find_existing_notes(url: str, deck_name: str, query_text: str):
    deck_q = escape_for_anki_query(deck_name)
    text_q = escape_for_anki_query(query_text)
    anki_query = f'deck:"{deck_q}" "{text_q}"'
    return anki_connect_request(url, "findNotes", {"query": anki_query})


def fetch_note_previews(url: str, note_ids):
    if not note_ids:
        return []
    infos = anki_connect_request(url, "notesInfo", {"notes": note_ids})
    previews = []
    for info in infos:
        fields = info.get("fields", {})
        back_val = fields.get(ANKI_BACK_FIELD, {}).get("value", "").strip()
        front_val = fields.get(ANKI_FRONT_FIELD, {}).get("value", "").strip()
        text = back_val or front_val
        text = " ".join(text.split())
        if len(text) > 120:
            text = text[:117] + "..."
        previews.append(text if text else "(empty)")
    return previews


def should_continue_with_existing(existing_count: int, previews) -> bool:
    if existing_count <= 0:
        return True
    print(f"Found {existing_count} existing note(s) matching this text in the deck.")
    print("Matched existing text:")
    for idx, text in enumerate(previews, start=1):
        print(f"{idx}. {text}")
    raw = input("Continue and add new card? [Y/n]: ").strip().lower()
    return raw in ("", "y", "yes")


def main():
    parser = argparse.ArgumentParser(
        description=(
            "Extract video clip by matching subtitle text from a same-named .srt file "
            "in the same directory as the input .mp4 file."
        )
    )
    parser.add_argument(
        "video",
        help="Absolute or relative path to input .mp4 file.",
    )
    parser.add_argument(
        "sub",
        help='Subtitle text to find, e.g. "hi, I met you..."',
    )
    parser.add_argument(
        "--padding",
        type=float,
        default=0.0,
        help="Seconds to extend before start and after end (default: 0).",
    )
    args = parser.parse_args()

    video_path = Path(args.video).expanduser().resolve()
    if video_path.suffix.lower() != ".mp4":
        print("Error: input video must be a .mp4 file.", file=sys.stderr)
        sys.exit(1)
    work_dir = video_path.parent
    srt_path = work_dir / f"{video_path.stem}.srt"

    if not video_path.exists():
        print(f"Error: video file not found: {video_path}", file=sys.stderr)
        sys.exit(1)
    if not srt_path.exists():
        print(f"Error: subtitle file not found: {srt_path}", file=sys.stderr)
        sys.exit(1)

    try:
        existing_ids = find_existing_notes(ANKI_CONNECT_URL, ANKI_DECK_NAME, args.sub)
        existing_previews = fetch_note_previews(ANKI_CONNECT_URL, existing_ids)
        if not should_continue_with_existing(len(existing_ids), existing_previews):
            print("Skipped adding note.")
            return
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    entries = parse_srt(srt_path)
    if not entries:
        print(f"Error: no subtitle entries found in {srt_path}", file=sys.stderr)
        sys.exit(1)

    matches = find_matching_entries(entries, args.sub)
    if not matches:
        print("Error: subtitle text not found in .srt file.", file=sys.stderr)
        sys.exit(1)
    if len(matches) == 1:
        match = matches[0]
    else:
        print("Multiple subtitle matches found. Choose one:")
        for idx, entry in enumerate(matches, start=1):
            print(
                f"{idx}. {seconds_to_ffmpeg_time(entry['start'])} -> "
                f"{seconds_to_ffmpeg_time(entry['end'])} | {entry['text']}"
            )
        while True:
            raw = input(f"Enter choice [1-{len(matches)}]: ").strip()
            try:
                choice = int(raw)
                if 1 <= choice <= len(matches):
                    match = matches[choice - 1]
                    break
            except ValueError:
                pass
            print("Invalid choice, try again.")

    pad = max(args.padding, 0.0)
    start_sec = max(match["start"] - pad, 0.0)
    end_sec = max(match["end"] + pad, start_sec + 0.1)
    range_text = collect_text_in_range(entries, start_sec, end_sec)
    media_name = safe_filename_from_sub_text(range_text) + ".mp4"

    try:
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / media_name
            run_ffmpeg(video_path, output_path, start_sec, end_sec)
            note_id = add_note_to_anki(
                output_path,
                match["text"],
                ANKI_CONNECT_URL,
                ANKI_DECK_NAME,
            )
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Time range: {seconds_to_ffmpeg_time(start_sec)} -> {seconds_to_ffmpeg_time(end_sec)}")
    print(f"Anki media: {media_name}")
    print(f"Matched subtitle: {match['text']}")
    print(f"Anki note created: {note_id}")


if __name__ == "__main__":
    main()
