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


def load_anki_config(path: Path):
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise RuntimeError(f"Anki config file not found: {path}")
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON in Anki config: {exc}")

    required = ["deck_name", "model_name", "front_field", "back_field"]
    missing = [key for key in required if key not in data]
    if missing:
        raise RuntimeError(f"Missing keys in Anki config: {', '.join(missing)}")

    data.setdefault("anki_connect_url", "http://127.0.0.1:8765")
    data.setdefault("allow_duplicate", False)
    return data


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


def add_note_to_anki(output_video: Path, back_text: str, config: dict):
    video_bytes = output_video.read_bytes()
    media_name = output_video.name
    media_b64 = base64.b64encode(video_bytes).decode("ascii")

    url = config["anki_connect_url"]
    anki_connect_request(
        url,
        "storeMediaFile",
        {"filename": media_name, "data": media_b64},
    )

    fields = {
        config["front_field"]: f"[sound:{media_name}]",
        config["back_field"]: back_text,
    }
    note = {
        "deckName": config["deck_name"],
        "modelName": config["model_name"],
        "fields": fields,
        "options": {"allowDuplicate": bool(config["allow_duplicate"])},
    }
    note_id = anki_connect_request(url, "addNote", {"note": note})
    return note_id


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
    config_path = Path(__file__).resolve().parent / "anki_config.json"

    try:
        anki_config = load_anki_config(config_path)
        with tempfile.TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / media_name
            run_ffmpeg(video_path, output_path, start_sec, end_sec)
            note_id = add_note_to_anki(output_path, match["text"], anki_config)
    except RuntimeError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        sys.exit(1)

    print(f"Time range: {seconds_to_ffmpeg_time(start_sec)} -> {seconds_to_ffmpeg_time(end_sec)}")
    print(f"Anki media: {media_name}")
    print(f"Matched subtitle: {match['text']}")
    print(f"Anki note created: {note_id}")


if __name__ == "__main__":
    main()
