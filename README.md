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

## Config In Code

Edit constants in `main.py`:

- `ANKI_CONNECT_URL`
- `ANKI_DECK_NAME`
- `ANKI_MODEL_NAME`
- `ANKI_FRONT_FIELD`
- `ANKI_BACK_FIELD`

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
- It checks existing notes in the target deck using your search text. If matches exist, it asks `Y/n` (default `Y`) before adding.
- Extracted clip is temporary (not kept on disk after adding to Anki).
