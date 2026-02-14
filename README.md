# Anki Subtitle Clipper

Extract a short clip from an `.mp4` using subtitle text from a same-name `.srt`, then add it to Anki.

Front field:
- `[sound:<generated>.mp4]`

Back field:
- matched subtitle text

## Requirements

- Python 3
- `ffmpeg` in `PATH`
- Anki desktop app running
- Anki add-on: AnkiConnect (`2055492159`)

## Config

Edit `anki_config.json` in this project folder:

```json
{
  "anki_connect_url": "http://127.0.0.1:8765",
  "deck_name": "Default",
  "model_name": "Basic",
  "front_field": "Front",
  "back_field": "Back"
}
```

Notes:
- Duplicates are disallowed by default.
- `anki_config.json` is loaded automatically from the same directory as `main.py`.

## Usage

```bash
python3 main.py "/absolute/path/video.mp4" "subtitle text to find"
```

Optional:

```bash
python3 main.py "/absolute/path/video.mp4" "subtitle text to find" --padding 0.5
```

## Behavior

- Script expects subtitle file at same location with same stem:
  - `/path/video.mp4`
  - `/path/video.srt`
- If multiple subtitle lines match, it asks you to choose one.
- Extracted clip is temporary (not kept on disk after adding to Anki).
