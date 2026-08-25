# Translator

A fast, offline-friendly desktop tool (with a CLI mode) for translating whole files — `.txt`, `.csv`, `.json`, `.srt`, `.docx`, and `.pptx` — while keeping the original file structure and formatting intact.

![Python](https://img.shields.io/badge/python-3.8%2B-blue)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

## Features

- **Batch, high-speed translation** — groups many lines/cells into single requests instead of one request per line, so large files translate fast without getting rate-limited.
- **Sentence-aware, not fragment-aware** — wrapped `.txt` lines, multi-line `.srt` captions, and Word/PowerPoint runs split by formatting are translated together for context, then mapped back so line counts, run counts, and formatting positions never change.
- **Two-provider fallback** — uses Google Translate as the primary engine and automatically falls back to MyMemory if it's unreachable, instead of silently leaving text untranslated.
- **Placeholder protection** — URLs, emails, `` `code spans` ``, `{template_vars}`, and `%s`/`%d` format specifiers are shielded from translation, so JSON/config/dev files come back intact.
- **Persistent cache** — previously translated text is cached to disk (SQLite), so re-running the same file (or files with overlapping content) is instant.
- **Multi-file queue, cancel button, dark/light theme** — a small but complete desktop GUI (Tkinter), plus a `--cli` mode for scripting.
- **Remembers your last-used languages** between runs.

## Supported file types

| Extension | Notes |
|---|---|
| `.txt` | Plain text, line-wrap aware |
| `.csv` | Cell-by-cell |
| `.json` | All string values, structure preserved |
| `.srt` | Subtitles, cue-aware |
| `.docx` | Paragraphs, tables, headers/footers |
| `.pptx` | Slides, tables, speaker notes |

## Supported languages

Auto Detect, Traditional Chinese, Simplified Chinese, English, Japanese, Korean, Spanish, French, German, Russian, Portuguese, Italian, Vietnamese, Thai, Indonesian, Malay, Arabic, Hindi, Dutch, Polish, Turkish, Swedish, Filipino/Tagalog.

(Add more by editing the `LANGUAGES_MAP` dictionary near the top of `translator.py` — any [Google Translate language code](https://cloud.google.com/translate/docs/languages) will work.)

## Installation

```bash
git clone https://github.com/<your-username>/<your-repo>.git
cd <your-repo>
pip install -r requirements.txt
```

> **Linux users:** Tkinter isn't installed via pip. If you get `ModuleNotFoundError: No module named 'tkinter'`, install it with:
> ```bash
> sudo apt-get install python3-tk      # Debian/Ubuntu
> sudo dnf install python3-tkinter     # Fedora
> ```
> Windows and macOS's standard Python installers already include Tkinter.

## Usage

### GUI

```bash
python translator.py
```

1. Click **Browse** and select one or more files.
2. Choose the **FROM** and **TO** languages.
3. Click **Start**. Progress, elapsed time, and a completion summary are shown live; **Cancel** stops after the current batch.
4. Output is saved next to the original file as `<name>_<target-language-code><ext>` (e.g. `report_zh-TW.docx`).

### CLI

```bash
python translator.py --cli input.txt --source auto --target zh-TW -o output.txt
```

| Flag | Description | Default |
|---|---|---|
| `--cli <path>` | Input file (required) | — |
| `-o, --output <path>` | Output file path | `<input>_<target><ext>` |
| `--source <code>` | Source language code, or `auto` | `auto` |
| `--target <code>` | Target language code | `zh-TW` |

## How it works (briefly)

- Text is grouped into batches (by paragraph/cue/run context, not arbitrarily) and sent as single HTTP requests using a plain-text separator token that survives translation intact, then split back apart.
- If a batch's structure doesn't come back exactly as expected, the affected block automatically falls back to translating line-by-line — so output structure is never corrupted, only translation context is occasionally reduced.
- A local SQLite cache (`~/.translator_cache.sqlite3`) and a small settings file (`~/.translator_settings.json`, remembers your last language pair) are created in your home directory. Delete either anytime to reset.

## Limitations

- Relies on Google Translate's public (unofficial) endpoint as the primary provider — there's no official SLA or quota guarantee. MyMemory is used as an automatic fallback but has its own free-tier limits.
- Formatting redistribution in `.docx`/`.pptx` (when a sentence spans multiple differently-formatted runs) is proportional, not word-perfect — bold/italic spans stay roughly where they were, not necessarily on the exact original words, since translation reorders words.
- Not intended for confidential/sensitive documents, since text is sent to third-party translation APIs.

## Contributing

Issues and pull requests are welcome.

## License

[MIT](LICENSE)
