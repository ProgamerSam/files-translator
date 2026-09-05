#!/usr/bin/env python3
"""
Translator - High-Speed Clean File Translation Utility
"""

import argparse
import concurrent.futures
import csv
import hashlib
import html
import json
import os
import random
import re
import sqlite3
import subprocess
import sys
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
import urllib.error
import urllib.parse
import urllib.request

# ==========================================================================
# PALETTES & THEMES
# ==========================================================================

THEMES = {
    "dark": {
        "bg": "#0B0F19",
        "card_bg": "#111827",
        "card_border": "#1F2937",
        "fg_title": "#F9FAFB",
        "fg_sub": "#9CA3AF",
        "fg_body": "#E5E7EB",
        "fg_muted": "#6B7280",

        "btn_primary_bg": "#4F46E5",
        "btn_primary_hover": "#4338CA",
        "btn_primary_fg": "#FFFFFF",

        "btn_sec_bg": "#1F2937",
        "btn_sec_hover": "#374151",
        "btn_sec_fg": "#E5E7EB",

        "btn_danger_bg": "#7F1D1D",
        "btn_danger_hover": "#991B1B",
        "btn_danger_fg": "#FEE2E2",

        "bar_bg": "#1F2937",
        "bar_border": "#374151",
        "bar_fill": "#10B981",
        "bar_glow": "#34D399",
        "bar_text": "#FFFFFF",

        "combo_bg": "#111827",
        "combo_fg": "#F9FAFB",
        "combo_border": "#374151",
        "theme_btn": "☀️ Light",
    },
    "light": {
        "bg": "#F8FAFC",
        "card_bg": "#FFFFFF",
        "card_border": "#E2E8F0",
        "fg_title": "#0F172A",
        "fg_sub": "#64748B",
        "fg_body": "#1E293B",
        "fg_muted": "#94A3B8",

        "btn_primary_bg": "#4F46E5",
        "btn_primary_hover": "#4338CA",
        "btn_primary_fg": "#FFFFFF",

        "btn_sec_bg": "#F1F5F9",
        "btn_sec_hover": "#E2E8F0",
        "btn_sec_fg": "#334155",

        "btn_danger_bg": "#FEE2E2",
        "btn_danger_hover": "#FECACA",
        "btn_danger_fg": "#991B1B",

        "bar_bg": "#E2E8F0",
        "bar_border": "#CBD5E1",
        "bar_fill": "#10B981",
        "bar_glow": "#059669",
        "bar_text": "#0F172A",

        "combo_bg": "#FFFFFF",
        "combo_fg": "#0F172A",
        "combo_border": "#CBD5E1",
        "theme_btn": "🌙 Dark",
    },
}

# ==========================================================================
# SETTINGS (remembers last-used languages between runs)
# ==========================================================================

SETTINGS_PATH = os.path.join(os.path.expanduser("~"), ".translator_settings.json")


def load_settings():
    try:
        with open(SETTINGS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_settings(data):
    try:
        with open(SETTINGS_PATH, "w", encoding="utf-8") as f:
            json.dump(data, f)
    except Exception:
        pass


# ==========================================================================
# PERSISTENT CACHE (SQLite on disk + in-memory layer)
# Re-running the same file, or translating overlapping content across
# files, becomes instant on repeat runs instead of re-hitting the network.
# ==========================================================================

_CACHE_DB_PATH = os.path.join(os.path.expanduser("~"), ".translator_cache.sqlite3")
_MEM_CACHE = {}
_MEM_CACHE_LOCK = threading.Lock()
_db_conn = None
_db_lock = threading.Lock()


def _get_db():
    global _db_conn
    if _db_conn is None:
        conn = sqlite3.connect(_CACHE_DB_PATH, check_same_thread=False)
        conn.execute("CREATE TABLE IF NOT EXISTS cache (k TEXT PRIMARY KEY, v TEXT)")
        conn.commit()
        _db_conn = conn
    return _db_conn


def _cache_key(text, source, target):
    return hashlib.sha256(f"{source}|{target}|{text}".encode("utf-8")).hexdigest()


def cache_get(text, source, target):
    mem_key = (text, source, target)
    with _MEM_CACHE_LOCK:
        if mem_key in _MEM_CACHE:
            return _MEM_CACHE[mem_key]
    try:
        with _db_lock:
            row = _get_db().execute(
                "SELECT v FROM cache WHERE k=?", (_cache_key(text, source, target),)
            ).fetchone()
        if row:
            with _MEM_CACHE_LOCK:
                _MEM_CACHE[mem_key] = row[0]
            return row[0]
    except Exception:
        pass
    return None


def clear_cache():
    """Wipe the persistent translation cache. Use this once if a previous
    run left failed segments cached as 'done' (see the note in
    fetch_translation) - old versions of this script cached failures too,
    so a prior bad run can make good segments look permanently finished."""
    global _db_conn
    with _MEM_CACHE_LOCK:
        _MEM_CACHE.clear()
    with _db_lock:
        try:
            if _db_conn is not None:
                _db_conn.close()
                _db_conn = None
            if os.path.exists(_CACHE_DB_PATH):
                os.remove(_CACHE_DB_PATH)
        except Exception as e:
            print(f"[translator] Could not clear cache: {e}", file=sys.stderr)
            return False
    return True


def cache_set(text, source, target, value):
    with _MEM_CACHE_LOCK:
        _MEM_CACHE[(text, source, target)] = value
    try:
        with _db_lock:
            db = _get_db()
            db.execute(
                "INSERT OR REPLACE INTO cache (k, v) VALUES (?, ?)",
                (_cache_key(text, source, target), value),
            )
            db.commit()
    except Exception:
        pass


# ==========================================================================
# FAILURE TRACKING (so the user finds out if something couldn't be reached,
# instead of it silently staying untranslated)
# ==========================================================================

class _FailCounter:
    def __init__(self):
        self.count = 0
        self.lock = threading.Lock()

    def inc(self, n=1):
        with self.lock:
            self.count += n

    def reset(self):
        with self.lock:
            self.count = 0

    def get(self):
        with self.lock:
            return self.count


_fail_counter = _FailCounter()

_PRINTED_DIAGNOSTIC = False
_DIAG_LOCK = threading.Lock()


def _log_once(msg):
    global _PRINTED_DIAGNOSTIC
    with _DIAG_LOCK:
        if not _PRINTED_DIAGNOSTIC:
            _PRINTED_DIAGNOSTIC = True
            print(f"[translator] Warning: {msg}", file=sys.stderr)


# ==========================================================================
# ENGINE (BATCHED + MULTI-PROVIDER + RATE-LIMIT-SAFE + PLACEHOLDER-SAFE)
# ==========================================================================

# Batching means far fewer HTTP requests than one-per-line, so a smaller
# number of *concurrent batch* workers is both faster and safer than
# blasting one request per line with many threads.
MAX_WORKERS = 4
BATCH_MAX_ITEMS = 40
BATCH_MAX_CHARS = 1800

# Sentinel used to glue multiple items together into one request. Plain
# alphanumeric junk (no punctuation/brackets) survives translation far more
# reliably than bracketed tokens, which engines sometimes localize
# (e.g. into full-width CJK brackets), breaking the split.
_SEP_TOKEN = "qz9k3xseg7fbreak"
_SEP_REGEX = re.compile(r"\s*" + re.escape(_SEP_TOKEN) + r"\s*")

# Things that should never be sent through translation: URLs, emails,
# inline code spans, {template} placeholders, and printf-style specifiers.
# Useful for JSON/i18n files and anything with technical content mixed in.
_PLACEHOLDER_PATTERNS = [
    re.compile(r"https?://\S+"),
    re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    re.compile(r"`[^`]+`"),
    re.compile(r"\{[a-zA-Z0-9_.]+\}"),
    re.compile(r"%[sd]"),
]
_PLACEHOLDER_RESTORE_RE = re.compile(r"zzph\s*(\d+)\s*zzendph")


def _protect_placeholders(text):
    tokens = []

    def _sub(m):
        tokens.append(m.group(0))
        return f"zzph{len(tokens) - 1}zzendph"

    protected = text
    for pat in _PLACEHOLDER_PATTERNS:
        protected = pat.sub(_sub, protected)
    return protected, tokens


def _restore_placeholders(text, tokens):
    if not tokens:
        return text

    def _re(m):
        idx = int(m.group(1))
        return tokens[idx] if 0 <= idx < len(tokens) else m.group(0)

    return _PLACEHOLDER_RESTORE_RE.sub(_re, text)


def sanitize_and_protect(text):
    """保護跳脫字元與格式標記 (newlines + technical placeholders)."""
    protected, tokens = _protect_placeholders(text)
    protected = protected.replace("\r\n", " [NL_BR] ").replace("\n", " [NL_BR] ")
    return protected, tokens


def restore_protected(text, tokens=None):
    """還原受保護的格式標記 (tolerant of stray spaces some engines add
    around the markers, since translators sometimes insert their own)."""
    restored = re.sub(r"\s*\[\s*nl_br\s*\]\s*", "\n", text, flags=re.IGNORECASE)
    restored = _restore_placeholders(restored, tokens)
    return restored


_HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36",
    "Accept": "*/*",
}

# ==========================================================================
# GLOBAL RATE LIMITER
#
# translate.googleapis.com/translate_a/single is an unauthenticated, free
# endpoint. It tolerates occasional requests fine, but once several worker
# threads fire batches at roughly the same time, it starts answering with
# HTTP 429 / empty responses for the whole burst.
#
# Symptom this caused: with MAX_WORKERS threads all submitting near the
# start of a run, the *first* wave of batches (i.e. the beginning of the
# document, for a normal top-to-bottom file) got throttled and fell back
# to the low-quota secondary provider or the original text. Later batches,
# submitted after earlier ones had already burned time in retry/backoff
# sleeps, happened to land after the rate-limit window cooled down and
# succeeded - so only the *end* of the file came out translated.
#
# Fix: force a minimum spacing between ANY two outgoing requests, across
# every thread, so we simply never send a burst in the first place.
# ==========================================================================

_RATE_LIMIT_LOCK = threading.Lock()
_LAST_REQUEST_AT = [0.0]
_MIN_REQUEST_INTERVAL = 0.35  # seconds between outgoing requests, globally


def _throttle():
    with _RATE_LIMIT_LOCK:
        now = time.time()
        wait = _LAST_REQUEST_AT[0] + _MIN_REQUEST_INTERVAL - now
        if wait > 0:
            time.sleep(wait)
        _LAST_REQUEST_AT[0] = time.time()


def _translate_google(query_text, source, target, retries=5, timeout=10):
    """Primary provider. Returns translated string, or None on failure."""
    url = (
        "https://translate.googleapis.com/translate_a/single"
        f"?client=gtx&sl={source}&tl={target}&dt=t&ie=UTF-8&oe=UTF-8&q="
        + urllib.parse.quote(query_text)
    )
    last_err = None
    for attempt in range(retries):
        _throttle()
        try:
            req = urllib.request.Request(url, headers=_HEADERS)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                data = json.loads(resp.read().decode("utf-8", errors="ignore"))
                if data and isinstance(data, list) and data[0]:
                    parts = [seg[0] for seg in data[0] if seg and seg[0]]
                    joined = "".join(parts)
                    if joined:
                        return html.unescape(joined)
            last_err = "empty response from Google Translate"
        except urllib.error.HTTPError as e:
            if e.code == 429:
                # Rate-limited - back off longer than a generic HTTP error,
                # and honor Retry-After if the server sent one.
                retry_after = e.headers.get("Retry-After") if e.headers else None
                try:
                    delay = float(retry_after) if retry_after else (2.0 * (attempt + 1))
                except (TypeError, ValueError):
                    delay = 2.0 * (attempt + 1)
                last_err = "HTTP 429 (rate limited) from Google Translate"
                time.sleep(delay + random.uniform(0, 0.6))
                continue
            last_err = f"HTTP {e.code} from Google Translate"
            time.sleep((1.2 * (attempt + 1)) + random.uniform(0, 0.5))
            continue
        except Exception as e:
            last_err = str(e)
        time.sleep((0.4 * (attempt + 1)) + random.uniform(0, 0.3))
    if last_err:
        _log_once(f"{last_err}. Falling back to a secondary provider / original "
                   f"text for some segments. Check internet access to "
                   f"translate.googleapis.com if this keeps happening.")
    return None


def _translate_mymemory(text, source, target, timeout=8):
    """Secondary/fallback provider. Free tier, single strings only, no
    'auto' source support - used as a safety net when Google is
    unreachable, not as the primary path."""
    if source == "auto" or not text:
        return None
    try:
        _throttle()
        langpair = f"{source}|{target}"
        url = (
            "https://api.mymemory.translated.net/get?q="
            + urllib.parse.quote(text[:490])
            + "&langpair="
            + urllib.parse.quote(langpair)
        )
        req = urllib.request.Request(url, headers=_HEADERS)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.loads(resp.read().decode("utf-8", errors="ignore"))
            translated = (data.get("responseData") or {}).get("translatedText")
            if translated and translated.strip():
                return html.unescape(translated)
    except Exception:
        pass
    return None


_LANG_SCRIPT_HINTS = [
    (re.compile(r"[\u3040-\u30ff]"), "ja"),      # Hiragana/Katakana -> Japanese
    (re.compile(r"[\uac00-\ud7a3]"), "ko"),      # Hangul -> Korean
    (re.compile(r"[\u4e00-\u9fff]"), "zh-CN"),   # CJK ideographs (no kana) -> Chinese
    (re.compile(r"[\u0e00-\u0e7f]"), "th"),      # Thai
    (re.compile(r"[\u0600-\u06ff]"), "ar"),      # Arabic
    (re.compile(r"[\u0400-\u04ff]"), "ru"),      # Cyrillic
]


def _guess_lang(text):
    """Best-effort script-based source-language guess. MyMemory (the
    fallback provider) refuses source='auto', so when the user has Auto
    Detect selected and Google is the one failing, the fallback would
    otherwise never engage at all. This only needs to be roughly right."""
    for pat, code in _LANG_SCRIPT_HINTS:
        if pat.search(text):
            return code
    return "en"


def _translate_text(text, source, target, quick=False):
    """Try providers in order. quick=True skips Google's first retry loop
    (used when we already know Google just failed for the whole batch,
    to avoid re-hammering a dead endpoint for every single item)."""
    if not quick:
        result = _translate_google(text, source, target)
        if result is not None:
            return result

    mm_source = source if source != "auto" else _guess_lang(text)
    result = _translate_mymemory(text, mm_source, target)
    if result is not None:
        return result

    # Last resort: a temporary block/rate-limit on Google's side often
    # clears within a few seconds - worth one more slow, low-effort try
    # before finally giving up and leaving the segment untranslated.
    time.sleep(3 + random.uniform(0, 2))
    return _translate_google(text, source, target, retries=2)


def fetch_translation(text, source="auto", target="zh-TW", quick=False):
    """Single-item translation with cache + placeholder protection.
    Used directly for one-off strings and as the fallback path when a
    batch response doesn't line up cleanly."""
    if not text or not text.strip() or not re.search(r"[\w]", text):
        return text

    core = text.strip()
    cached = cache_get(core, source, target)
    if cached is not None:
        return cached

    protected, tokens = sanitize_and_protect(core)
    translated = _translate_text(protected, source, target, quick=quick)
    if translated is not None:
        result = restore_protected(translated, tokens)
        # Only cache real successes. Caching a failure (result == core)
        # would permanently "poison" this segment: every future run,
        # including after fixing whatever caused the failure, would hit
        # this cache entry and skip retranslating it forever.
        cache_set(core, source, target, result)
    else:
        _fail_counter.inc()
        result = core

    return result


def fetch_translation_batch(texts, source="auto", target="zh-TW", cancel_event=None):
    """Translate a list of already-stripped, non-empty strings, ideally in
    ONE request. Falls back per-item if the batch fails or doesn't line up."""
    if not texts:
        return []

    results = [None] * len(texts)
    to_fetch, to_fetch_idx = [], []
    for i, t in enumerate(texts):
        cached = cache_get(t, source, target)
        if cached is not None:
            results[i] = cached
        else:
            to_fetch.append(t)
            to_fetch_idx.append(i)

    if not to_fetch:
        return results

    if cancel_event and cancel_event.is_set():
        for i, t in zip(to_fetch_idx, to_fetch):
            results[i] = t
        return results

    protected_list = []
    tokens_list = []
    for t in to_fetch:
        p, tok = sanitize_and_protect(t)
        protected_list.append(p)
        tokens_list.append(tok)

    # Wrapped in blank lines (not just spaces) so the separator reads as a
    # hard paragraph break to the translation engine, reducing grammar/
    # register bleeding across unrelated adjacent items in the same batch.
    combined = ("\n\n" + _SEP_TOKEN + "\n\n").join(protected_list)
    translated_full = _translate_google(combined, source, target)

    if translated_full is not None:
        parts = _SEP_REGEX.split(translated_full)
        if len(parts) == len(to_fetch):
            for local_i, part, tok, original in zip(to_fetch_idx, parts, tokens_list, to_fetch):
                cleaned = restore_protected(part.strip(), tok)
                results[local_i] = cleaned
                cache_set(original, source, target, cleaned)
            return results
        # Google is reachable but segmentation didn't line up - retry
        # these individually via the normal (non-quick) path.
        for local_i, original in zip(to_fetch_idx, to_fetch):
            results[local_i] = fetch_translation(original, source, target, quick=False)
        return results

    # Whole batch failed - Google is likely unreachable right now. Don't
    # hammer it again per item; try the lightweight fallback provider only.
    for local_i, original in zip(to_fetch_idx, to_fetch):
        results[local_i] = fetch_translation(original, source, target, quick=True)
    return results


def translate_with_padding(text, source="auto", target="zh-TW"):
    if not text:
        return text
    leading_ws = text[: len(text) - len(text.lstrip())]
    trailing_ws = text[len(text.rstrip()):]
    core = text.strip()
    if not core:
        return text
    translated_core = fetch_translation(core, source, target)
    return f"{leading_ws}{translated_core}{trailing_ws}"


def _make_batches(items):
    batches = []
    current = []
    current_chars = 0
    for idx, core in items:
        item_len = len(core)
        if current and (
            len(current) >= BATCH_MAX_ITEMS or current_chars + item_len > BATCH_MAX_CHARS
        ):
            batches.append(current)
            current = []
            current_chars = 0
        current.append((idx, core))
        current_chars += item_len
    if current:
        batches.append(current)
    return batches


def parallel_translate_list(item_list, source, target, progress_cb, cancel_event=None):
    """Translate a list of raw strings (preserving per-item leading/trailing
    whitespace), batching many items per HTTP request and running several
    batches concurrently."""
    total = len(item_list)
    if total == 0:
        return []

    results = [None] * total
    completed = 0
    lock = threading.Lock()

    translatable = []  # (index, leading_ws, core, trailing_ws)
    for i, text in enumerate(item_list):
        if not text:
            results[i] = text
            continue
        leading_ws = text[: len(text) - len(text.lstrip())]
        trailing_ws = text[len(text.rstrip()):]
        core = text.strip()
        if not core or not re.search(r"[\w]", core):
            results[i] = text
            continue
        translatable.append((i, leading_ws, core, trailing_ws))

    def bump_progress(n=1):
        nonlocal completed
        with lock:
            completed += n
            if progress_cb:
                progress_cb(completed, total)

    skipped = total - len(translatable)
    if skipped:
        bump_progress(skipped)

    if not translatable:
        return results

    batches = _make_batches([(i, core) for i, _lw, core, _tw in translatable])
    meta_by_index = {i: (lw, tw) for i, lw, _c, tw in translatable}

    def run_batch(batch):
        if cancel_event and cancel_event.is_set():
            for i, core in batch:
                lw, tw = meta_by_index[i]
                results[i] = f"{lw}{core}{tw}"
            bump_progress(len(batch))
            return
        idxs = [i for i, _core in batch]
        cores = [core for _i, core in batch]
        translated_cores = fetch_translation_batch(cores, source, target, cancel_event=cancel_event)
        for i, translated_core in zip(idxs, translated_cores):
            lw, tw = meta_by_index[i]
            results[i] = f"{lw}{translated_core}{tw}"
        bump_progress(len(batch))

    with concurrent.futures.ThreadPoolExecutor(max_workers=MAX_WORKERS) as executor:
        futures = [executor.submit(run_batch, b) for b in batches]
        concurrent.futures.wait(futures)

    return results


# ==========================================================================
# FILE HANDLERS
# ==========================================================================

def _proportional_split(text, weights):
    """Split translated text into len(weights) chunks sized proportionally
    to the original run/segment lengths, snapping each cut to a nearby
    space so words aren't sliced in half where the language uses spaces
    (best-effort; languages without spaces, e.g. Chinese, fall back to a
    plain character cut). Used so multi-run formatting in Word/PowerPoint
    keeps the same number of runs instead of collapsing into one."""
    n = len(weights)
    if n <= 1 or not text:
        return [text] + [""] * (n - 1)

    total_w = sum(weights) or n
    total_len = len(text)
    cuts = []
    cum = 0
    for w in weights[:-1]:
        cum += w
        ideal = int(total_len * (cum / total_w))
        cut = max(1, min(ideal, total_len - 1))
        for offset in range(0, 9):
            if ideal + offset < total_len and text[ideal + offset] == " ":
                cut = ideal + offset
                break
            if ideal - offset > 0 and text[ideal - offset] == " ":
                cut = ideal - offset
                break
        cuts.append(cut)

    for i in range(1, len(cuts)):
        if cuts[i] <= cuts[i - 1]:
            cuts[i] = min(cuts[i - 1] + 1, total_len)

    parts = []
    prev = 0
    for c in cuts:
        parts.append(text[prev:c])
        prev = c
    parts.append(text[prev:])
    return parts


def handle_txt(in_path, out_path, source, target, progress_cb, cancel_event=None):
    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    # Group consecutive non-blank lines into blocks so a wrapped sentence
    # translates with full context instead of being chopped mid-sentence
    # line-by-line. If a translated block doesn't split back into exactly
    # the same number of lines (rare), that block falls back to the old
    # line-by-line method - so line count and layout never change, only
    # translation quality improves when it safely can.
    blocks = []  # list of (start_idx, end_idx_exclusive)
    start = None
    for i, line in enumerate(lines):
        if line.strip() == "":
            if start is not None:
                blocks.append((start, i))
                start = None
        else:
            if start is None:
                start = i
    if start is not None:
        blocks.append((start, len(lines)))

    block_texts = ["\n".join(l.rstrip("\r\n") for l in lines[s:e]) for s, e in blocks]
    translated_blocks = parallel_translate_list(block_texts, source, target, progress_cb, cancel_event)

    out_lines = list(lines)
    for (s, e), translated in zip(blocks, translated_blocks):
        parts = translated.split("\n")
        if len(parts) == (e - s):
            for j, part in enumerate(parts):
                ending = "\n" if lines[s + j].endswith("\n") else ""
                out_lines[s + j] = part + ending
        else:
            per_line = parallel_translate_list(
                lines[s:e], source, target, lambda c, t: None, cancel_event
            )
            for j, tl in enumerate(per_line):
                out_lines[s + j] = tl

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)


def handle_csv(in_path, out_path, source, target, progress_cb, cancel_event=None):
    with open(in_path, "r", encoding="utf-8", errors="ignore", newline="") as f:
        rows = list(csv.reader(f))
    flat_cells = []
    row_lens = []
    for r in rows:
        row_lens.append(len(r))
        flat_cells.extend(r)

    translated_cells = parallel_translate_list(flat_cells, source, target, progress_cb, cancel_event)
    new_rows = []
    curr = 0
    for length in row_lens:
        new_rows.append(translated_cells[curr: curr + length])
        curr += length

    with open(out_path, "w", encoding="utf-8", newline="") as f:
        csv.writer(f).writerows(new_rows)


def handle_json(in_path, out_path, source, target, progress_cb, cancel_event=None):
    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        data = json.load(f)

    flat_strings = []

    def collect(obj):
        if isinstance(obj, dict):
            for v in obj.values():
                collect(v)
        elif isinstance(obj, list):
            for v in obj:
                collect(v)
        elif isinstance(obj, str):
            flat_strings.append(obj)

    collect(data)
    translated_strings = parallel_translate_list(flat_strings, source, target, progress_cb, cancel_event)
    trans_iter = iter(translated_strings)

    def rebuild(obj):
        if isinstance(obj, dict):
            return {k: rebuild(v) for k, v in obj.items()}
        if isinstance(obj, list):
            return [rebuild(v) for v in obj]
        if isinstance(obj, str):
            return next(trans_iter)
        return obj

    translated_data = rebuild(data)
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(translated_data, f, ensure_ascii=False, indent=2)


def handle_srt(in_path, out_path, source, target, progress_cb, cancel_event=None):
    with open(in_path, "r", encoding="utf-8", errors="ignore") as f:
        lines = f.readlines()

    time_re = re.compile(r"^\d{2}:\d{2}:\d{2},\d{3} --> \d{2}:\d{2}:\d{2},\d{3}")
    idx_re = re.compile(r"^\d+$")

    # Group each cue's text lines together (a 2-line caption is one
    # sentence, not two independent fragments) so translation keeps full
    # context. If the result doesn't come back with the same number of
    # lines, that cue falls back to line-by-line translation - so your
    # subtitle timing and line layout are never altered.
    cues = []
    current = []
    for i, line in enumerate(lines):
        stripped = line.rstrip("\r\n")
        if idx_re.match(stripped) or time_re.match(stripped):
            continue
        if stripped == "":
            if current:
                cues.append(current)
                current = []
            continue
        current.append(i)
    if current:
        cues.append(current)

    cue_texts = ["\n".join(lines[i].rstrip("\r\n") for i in idxs) for idxs in cues]
    translated_cues = parallel_translate_list(cue_texts, source, target, progress_cb, cancel_event)

    out_lines = list(lines)
    for idxs, translated in zip(cues, translated_cues):
        parts = translated.split("\n")
        if len(parts) == len(idxs):
            for line_idx, part in zip(idxs, parts):
                out_lines[line_idx] = part + "\n"
        else:
            per_line = parallel_translate_list(
                [lines[i] for i in idxs], source, target, lambda c, t: None, cancel_event
            )
            for line_idx, tl in zip(idxs, per_line):
                out_lines[line_idx] = tl if tl.endswith("\n") else tl + "\n"

    with open(out_path, "w", encoding="utf-8") as f:
        f.writelines(out_lines)


def _collect_paragraph_runs(paragraphs, out_list):
    """Group a paragraph's runs together so the whole sentence translates
    as one unit (Word/PPT often split a single sentence across multiple
    runs due to formatting, spell-check, or track changes - translating
    those fragments independently is the main cause of garbled output)."""
    for p in paragraphs:
        non_empty_runs = [r for r in p.runs if r.text]
        if not non_empty_runs:
            continue
        full_text = "".join(r.text for r in non_empty_runs)
        if full_text.strip():
            out_list.append((non_empty_runs, full_text))


def _apply_paragraph_translations(paragraphs, translated_texts):
    for (runs, _full_text), trans_text in zip(paragraphs, translated_texts):
        if len(runs) == 1:
            runs[0].text = trans_text
        else:
            # Same number of runs as before - just resized proportionally -
            # so bold/italic/etc. spans stay roughly where they were rather
            # than all formatting collapsing onto the first run. Exact
            # word-for-word mapping isn't possible after translation
            # reorders words, but the run structure itself is preserved.
            weights = [len(r.text) or 1 for r in runs]
            parts = _proportional_split(trans_text, weights)
            for r, part in zip(runs, parts):
                r.text = part


def handle_docx(in_path, out_path, source, target, progress_cb, cancel_event=None):
    from docx import Document

    doc = Document(in_path)
    paragraphs = []

    _collect_paragraph_runs(doc.paragraphs, paragraphs)
    for t in doc.tables:
        for row in t.rows:
            for cell in row.cells:
                _collect_paragraph_runs(cell.paragraphs, paragraphs)
    for s in doc.sections:
        _collect_paragraph_runs(s.header.paragraphs, paragraphs)
        _collect_paragraph_runs(s.footer.paragraphs, paragraphs)

    raw_texts = [full_text for _runs, full_text in paragraphs]
    translated = parallel_translate_list(raw_texts, source, target, progress_cb, cancel_event)
    _apply_paragraph_translations(paragraphs, translated)

    doc.save(out_path)


def handle_pptx(in_path, out_path, source, target, progress_cb, cancel_event=None):
    from pptx import Presentation

    prs = Presentation(in_path)
    paragraphs = []

    for slide in prs.slides:
        for shape in slide.shapes:
            if shape.has_text_frame:
                _collect_paragraph_runs(shape.text_frame.paragraphs, paragraphs)
            if shape.has_table:
                for row in shape.table.rows:
                    for cell in row.cells:
                        _collect_paragraph_runs(cell.text_frame.paragraphs, paragraphs)
        if slide.has_notes_slide:
            _collect_paragraph_runs(slide.notes_slide.notes_text_frame.paragraphs, paragraphs)

    raw_texts = [full_text for _runs, full_text in paragraphs]
    translated = parallel_translate_list(raw_texts, source, target, progress_cb, cancel_event)
    _apply_paragraph_translations(paragraphs, translated)

    prs.save(out_path)


HANDLERS = {
    ".txt": handle_txt,
    ".csv": handle_csv,
    ".json": handle_json,
    ".srt": handle_srt,
    ".docx": handle_docx,
    ".pptx": handle_pptx,
}
SUPPORTED_EXTS = tuple(HANDLERS.keys())

# ==========================================================================
# CUSTOM PILL BUTTON (CANVAS-BASED)
# ==========================================================================

class ModernPillButton(tk.Canvas):
    def __init__(self, master, text="Button", command=None, bg_color="#4F46E5", hover_color="#4338CA", fg_color="#FFFFFF", font=("Segoe UI", 10, "bold"), radius=8, height=36, width=120, *args, **kwargs):
        super().__init__(master, height=height, width=width, highlightthickness=0, bg=master.cget("bg"), *args, **kwargs)
        self.command = command
        self.text = text
        self.bg_color = bg_color
        self.hover_color = hover_color
        self.fg_color = fg_color
        self.font = font
        self.radius = radius
        self.h = height
        self.w = width
        self.is_hover = False
        self.is_disabled = False

        self.bind("<Enter>", self._on_enter)
        self.bind("<Leave>", self._on_leave)
        self.bind("<Button-1>", self._on_click)
        self.bind("<Configure>", self._on_resize)
        self.render()

    def _on_resize(self, event):
        self.w = event.width
        self.h = event.height
        self.render()

    def _draw_rounded_rect(self, color):
        r = self.radius
        w, h = self.w, self.h
        self.create_arc((0, 0, 2 * r, 2 * r), start=90, extent=90, fill=color, outline=color)
        self.create_arc((w - 2 * r, 0, w, 2 * r), start=0, extent=90, fill=color, outline=color)
        self.create_arc((0, h - 2 * r, 2 * r, h), start=180, extent=90, fill=color, outline=color)
        self.create_arc((w - 2 * r, h - 2 * r, w, h), start=270, extent=90, fill=color, outline=color)
        self.create_rectangle((r, 0, w - r, h), fill=color, outline=color)
        self.create_rectangle((0, r, w, h - r), fill=color, outline=color)

    def render(self):
        self.delete("all")
        current_bg = self.hover_color if self.is_hover and not self.is_disabled else self.bg_color
        if self.is_disabled:
            current_bg = "#64748B"

        self._draw_rounded_rect(current_bg)
        self.create_text(
            self.w / 2,
            self.h / 2,
            text=self.text,
            fill=self.fg_color,
            font=self.font,
        )

    def _on_enter(self, e):
        if not self.is_disabled:
            self.is_hover = True
            self.config(cursor="hand2")
            self.render()

    def _on_leave(self, e):
        self.is_hover = False
        self.config(cursor="")
        self.render()

    def _on_click(self, e):
        if not self.is_disabled and self.command:
            self.command()

    def set_config(self, text=None, bg_color=None, hover_color=None, fg_color=None, disabled=None):
        if text is not None:
            self.text = text
        if bg_color is not None:
            self.bg_color = bg_color
        if hover_color is not None:
            self.hover_color = hover_color
        if fg_color is not None:
            self.fg_color = fg_color
        if disabled is not None:
            self.is_disabled = disabled
        self.render()


# ==========================================================================
# MODERN PROGRESS BAR (HIGH CONTRAST)
# ==========================================================================

class ModernProgressBar(tk.Frame):
    def __init__(self, master, width=472, height=22, *args, **kwargs):
        super().__init__(master, *args, **kwargs)
        self.w = width
        self.h = height
        self.theme = THEMES["dark"]
        self.percent = 0.0
        self.label = "0%"

        self.canvas = tk.Canvas(self, width=self.w, height=self.h, highlightthickness=1)
        self.canvas.pack(fill="x", expand=True)

    def apply_theme(self, theme):
        self.theme = theme
        self.configure(bg=theme["bg"])
        self.canvas.configure(
            bg=theme["bar_bg"],
            highlightbackground=theme["bar_border"],
        )
        self.render()

    def update_progress(self, current, total):
        tot = max(1, total)
        self.percent = min(100.0, (current / tot) * 100.0)
        self.label = f"{current} / {tot}  ({self.percent:.1f}%)"
        self.render()

    def render(self):
        self.canvas.delete("all")
        fill_w = (self.percent / 100.0) * self.w
        if fill_w > 0:
            self.canvas.create_rectangle(0, 0, fill_w, self.h, fill=self.theme["bar_fill"], width=0)
            self.canvas.create_rectangle(0, 0, fill_w, 2, fill=self.theme["bar_glow"], width=0)

        text_color = self.theme["bar_text"]
        if fill_w > self.w * 0.45 and self.theme == THEMES["light"]:
            text_color = "#FFFFFF"

        self.canvas.create_text(
            self.w / 2,
            self.h / 2,
            text=self.label,
            fill=text_color,
            font=("Segoe UI", 8, "bold"),
        )

    def reset(self):
        self.percent = 0.0
        self.label = "0%"
        self.render()


# ==========================================================================
# GUI APPLICATION
# ==========================================================================

LANGUAGES_MAP = {
    "Auto Detect (自動偵測)": "auto",
    "Traditional Chinese (繁體中文)": "zh-TW",
    "Simplified Chinese (簡體中文)": "zh-CN",
    "English (英文)": "en",
    "Japanese (日文)": "ja",
    "Korean (韓文)": "ko",
    "Spanish (西班牙文)": "es",
    "French (法文)": "fr",
    "German (德文)": "de",
    "Russian (俄文)": "ru",
    "Portuguese (葡萄牙文)": "pt",
    "Italian (義大利文)": "it",
    "Vietnamese (越南文)": "vi",
    "Thai (泰文)": "th",
    "Indonesian (印尼文)": "id",
    "Malay (馬來文)": "ms",
    "Arabic (阿拉伯文)": "ar",
    "Hindi (印地文)": "hi",
    "Dutch (荷蘭文)": "nl",
    "Polish (波蘭文)": "pl",
    "Turkish (土耳其文)": "tr",
    "Swedish (瑞典文)": "sv",
    "Filipino/Tagalog (菲律賓文)": "tl",
}


class TranslatorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Translator")
        self.geometry("520x460")
        self.resizable(False, False)

        self.theme_mode = "dark"
        self.file_queue = []
        self.output_path = None
        self.cancel_event = threading.Event()
        self.settings = load_settings()

        self._build_ui()
        self.set_theme("dark")

    def _build_ui(self):
        # Header
        self.header = tk.Frame(self)
        self.header.pack(fill="x", padx=24, pady=(20, 10))

        self.title_label = tk.Label(self.header, text="Translator", font=("Segoe UI", 18, "bold"))
        self.title_label.pack(side="left", anchor="center")

        self.theme_btn = ModernPillButton(
            self.header,
            text="☀️ Light",
            command=self.toggle_theme,
            font=("Segoe UI", 8, "bold"),
            radius=6,
            height=28,
            width=76,
        )
        self.theme_btn.pack(side="right", anchor="center")

        self.clear_cache_btn = ModernPillButton(
            self.header,
            text="Clear Cache",
            command=self.on_clear_cache,
            font=("Segoe UI", 8, "bold"),
            radius=6,
            height=28,
            width=92,
        )
        self.clear_cache_btn.pack(side="right", anchor="center", padx=(0, 8))

        # File Section
        self.file_card = tk.Frame(self, highlightthickness=1)
        self.file_card.pack(fill="x", padx=24, pady=8)

        self.file_label = tk.Label(
            self.file_card,
            text="Select file(s) to translate...",
            font=("Segoe UI", 9),
            anchor="w",
            wraplength=320,
        )
        self.file_label.pack(side="left", padx=16, pady=12, fill="x", expand=True)

        self.browse_btn = ModernPillButton(
            self.file_card,
            text="Browse",
            command=self.attach_file,
            font=("Segoe UI", 9, "bold"),
            radius=6,
            height=30,
            width=84,
        )
        self.browse_btn.pack(side="right", padx=12, pady=10)

        # Languages Section
        self.lang_card = tk.Frame(self, highlightthickness=1)
        self.lang_card.pack(fill="x", padx=24, pady=8)

        self.from_frame = tk.Frame(self.lang_card)
        self.from_frame.pack(side="left", fill="x", expand=True, padx=14, pady=12)
        self.from_label = tk.Label(self.from_frame, text="FROM", font=("Segoe UI", 8, "bold"))
        self.from_label.pack(anchor="w")

        default_source = self.settings.get("source_lang", "Auto Detect (自動偵測)")
        if default_source not in LANGUAGES_MAP:
            default_source = "Auto Detect (自動偵測)"
        self.source_var = tk.StringVar(value=default_source)
        self.source_menu = ttk.Combobox(
            self.from_frame,
            textvariable=self.source_var,
            values=list(LANGUAGES_MAP.keys()),
            state="readonly",
            style="Clean.TCombobox",
        )
        self.source_menu.pack(fill="x", pady=(4, 0))

        self.to_frame = tk.Frame(self.lang_card)
        self.to_frame.pack(side="right", fill="x", expand=True, padx=14, pady=12)
        self.to_label = tk.Label(self.to_frame, text="TO", font=("Segoe UI", 8, "bold"))
        self.to_label.pack(anchor="w")

        default_target = self.settings.get("target_lang", "Traditional Chinese (繁體中文)")
        if default_target not in list(LANGUAGES_MAP.keys())[1:]:
            default_target = "Traditional Chinese (繁體中文)"
        self.target_var = tk.StringVar(value=default_target)
        self.target_menu = ttk.Combobox(
            self.to_frame,
            textvariable=self.target_var,
            values=list(LANGUAGES_MAP.keys())[1:],
            state="readonly",
            style="Clean.TCombobox",
        )
        self.target_menu.pack(fill="x", pady=(4, 0))

        # Start / Cancel Buttons (share the same row)
        self.action_row = tk.Frame(self)
        self.action_row.pack(fill="x", padx=24, pady=(12, 10))

        self.start_btn = ModernPillButton(
            self.action_row,
            text="Start",
            command=self.start_translation,
            font=("Segoe UI", 11, "bold"),
            radius=8,
            height=40,
            width=472,
        )
        self.start_btn.pack(fill="x")

        self.cancel_btn = ModernPillButton(
            self.action_row,
            text="Cancel",
            command=self.cancel_translation,
            font=("Segoe UI", 10, "bold"),
            radius=8,
            height=32,
            width=472,
        )
        # not packed until a job starts

        # Progress Bar
        self.progress_bar = ModernProgressBar(self, width=472, height=22)
        self.progress_bar.pack(padx=24, pady=2)

        # Status & Location
        self.status_label = tk.Label(
            self,
            text="",
            font=("Segoe UI", 8),
            anchor="w",
            justify="left",
            wraplength=470,
        )
        self.status_label.pack(fill="x", padx=26, pady=(8, 0))

        self.open_folder_btn = ModernPillButton(
            self,
            text="Open File Location",
            command=self.open_output_folder,
            font=("Segoe UI", 8, "bold"),
            radius=6,
            height=28,
            width=130,
        )

    def on_clear_cache(self):
        if messagebox.askyesno(
            "Clear Cache",
            "This deletes all cached translations from previous runs.\n\n"
            "Do this if past runs left segments untranslated - a bug in "
            "older versions could permanently mark a failed segment as "
            "'done'. After clearing, everything will be re-translated "
            "from scratch on the next run.",
        ):
            if clear_cache():
                messagebox.showinfo("Clear Cache", "Cache cleared.")
            else:
                messagebox.showerror("Clear Cache", "Could not clear the cache file. See console for details.")

    def toggle_theme(self):
        self.set_theme("light" if self.theme_mode == "dark" else "dark")

    def set_theme(self, mode):
        self.theme_mode = mode
        t = THEMES[mode]

        self.configure(bg=t["bg"])
        self.header.configure(bg=t["bg"])
        self.title_label.configure(bg=t["bg"], fg=t["fg_title"])

        self.theme_btn.configure(bg=t["bg"])
        self.theme_btn.set_config(
            text=t["theme_btn"],
            bg_color=t["btn_sec_bg"],
            hover_color=t["btn_sec_hover"],
            fg_color=t["btn_sec_fg"],
        )

        self.file_card.configure(bg=t["card_bg"], highlightbackground=t["card_border"])
        self.file_label.configure(bg=t["card_bg"], fg=t["fg_title"] if self.file_queue else t["fg_muted"])

        self.browse_btn.configure(bg=t["card_bg"])
        self.browse_btn.set_config(
            bg_color=t["btn_sec_bg"],
            hover_color=t["btn_sec_hover"],
            fg_color=t["btn_sec_fg"],
        )

        self.lang_card.configure(bg=t["card_bg"], highlightbackground=t["card_border"])
        self.from_frame.configure(bg=t["card_bg"])
        self.to_frame.configure(bg=t["card_bg"])
        self.from_label.configure(bg=t["card_bg"], fg=t["fg_muted"])
        self.to_label.configure(bg=t["card_bg"], fg=t["fg_muted"])

        style = ttk.Style(self)
        style.theme_use("clam")
        style.configure(
            "Clean.TCombobox",
            fieldbackground=t["combo_bg"],
            background=t["combo_border"],
            foreground=t["combo_fg"],
            darkcolor=t["combo_bg"],
            lightcolor=t["combo_border"],
            bordercolor=t["combo_border"],
            arrowcolor=t["fg_muted"],
            padding=5,
        )
        self.option_add("*TCombobox*Listbox.background", t["combo_bg"])
        self.option_add("*TCombobox*Listbox.foreground", t["combo_fg"])
        self.option_add("*TCombobox*Listbox.selectBackground", t["btn_primary_bg"])
        self.option_add("*TCombobox*Listbox.selectForeground", "#FFFFFF")

        self.action_row.configure(bg=t["bg"])
        self.start_btn.configure(bg=t["bg"])
        self.start_btn.set_config(
            bg_color=t["btn_primary_bg"],
            hover_color=t["btn_primary_hover"],
            fg_color=t["btn_primary_fg"],
        )
        self.cancel_btn.configure(bg=t["bg"])
        self.cancel_btn.set_config(
            bg_color=t["btn_danger_bg"],
            hover_color=t["btn_danger_hover"],
            fg_color=t["btn_danger_fg"],
        )

        self.progress_bar.apply_theme(t)
        self.status_label.configure(bg=t["bg"], fg=t["fg_muted"])

        self.open_folder_btn.configure(bg=t["bg"])
        self.open_folder_btn.set_config(
            bg_color=t["btn_sec_bg"],
            hover_color=t["btn_sec_hover"],
            fg_color="#10B981",
        )

    def attach_file(self):
        filetypes = [("Supported Files", " ".join(f"*{ext}" for ext in SUPPORTED_EXTS)), ("All Files", "*.*")]
        paths = filedialog.askopenfilenames(title="Select File(s)", filetypes=filetypes)
        if not paths:
            return

        valid, skipped_exts = [], set()
        for p in paths:
            ext = os.path.splitext(p)[1].lower()
            if ext in SUPPORTED_EXTS:
                valid.append(p)
            else:
                skipped_exts.add(ext or "(no extension)")

        if not valid:
            messagebox.showerror("Unsupported", "None of the selected files are supported.")
            return
        if skipped_exts:
            messagebox.showwarning("Some files skipped", f"Skipped unsupported types: {', '.join(sorted(skipped_exts))}")

        self.file_queue = valid
        if len(valid) == 1:
            self.file_label.config(text=os.path.basename(valid[0]), fg=THEMES[self.theme_mode]["fg_title"])
        else:
            names = ", ".join(os.path.basename(p) for p in valid[:2])
            more = f" +{len(valid) - 2} more" if len(valid) > 2 else ""
            self.file_label.config(text=f"{len(valid)} files: {names}{more}", fg=THEMES[self.theme_mode]["fg_title"])
        self.status_label.config(text="")
        self.progress_bar.reset()
        self.open_folder_btn.pack_forget()

    def update_progress_ui(self, current, total):
        self.after(0, self.progress_bar.update_progress, current, total)

    def start_translation(self):
        if not self.file_queue:
            messagebox.showwarning("Warning", "Please attach at least one file first.")
            return

        source = LANGUAGES_MAP[self.source_var.get()]
        target = LANGUAGES_MAP[self.target_var.get()]

        if source != "auto" and source == target:
            messagebox.showwarning("Warning", "Source and Target cannot be identical.")
            return

        save_settings({"source_lang": self.source_var.get(), "target_lang": self.target_var.get()})

        self.cancel_event = threading.Event()
        self.start_btn.set_config(text="Translating...", disabled=True)
        self.cancel_btn.pack(fill="x", pady=(8, 0))
        self.status_label.config(text="Processing parallel requests...", fg=THEMES[self.theme_mode]["fg_muted"])
        self.progress_bar.reset()

        threading.Thread(target=self._run_worker, args=(source, target), daemon=True).start()

    def cancel_translation(self):
        self.cancel_event.set()
        self.status_label.config(text="Cancelling after the current batch...", fg="#F59E0B")
        self.cancel_btn.set_config(disabled=True)

    def _run_worker(self, source, target):
        total_files = len(self.file_queue)
        overall_start = time.time()
        total_fails = 0
        last_out = None
        try:
            for i, path in enumerate(self.file_queue, start=1):
                if self.cancel_event.is_set():
                    break
                self.after(0, self._set_file_status, i, total_files, os.path.basename(path))
                self.after(0, self.progress_bar.reset)

                _fail_counter.reset()
                ext = os.path.splitext(path)[1].lower()
                base, ext_ = os.path.splitext(path)
                out_path = f"{base}_{target}{ext_}"
                HANDLERS[ext](path, out_path, source, target, self.update_progress_ui, self.cancel_event)
                total_fails += _fail_counter.get()
                last_out = out_path

            elapsed = time.time() - overall_start
            cancelled = self.cancel_event.is_set()
            self.after(0, self._on_success, last_out, elapsed, total_fails, cancelled, total_files)
        except Exception as e:
            self.after(0, self._on_error, str(e))

    def _set_file_status(self, i, total_files, name):
        label = f"File {i}/{total_files}: {name}" if total_files > 1 else f"Translating {name}"
        self.status_label.config(text=label, fg=THEMES[self.theme_mode]["fg_muted"])

    def _on_success(self, out_path, elapsed, total_fails, cancelled, total_files):
        self.start_btn.set_config(text="Start", disabled=False)
        self.cancel_btn.pack_forget()
        self.cancel_btn.set_config(disabled=False)
        self.output_path = out_path

        if cancelled:
            msg = f"Cancelled. Files completed before stopping are saved. ({elapsed:.1f}s)"
            color = "#F59E0B"
        else:
            file_word = "file" if total_files == 1 else f"{total_files} files"
            msg = f"Done: {file_word} translated ({elapsed:.1f}s)."
            if total_fails > 0:
                msg += f" {total_fails} segment(s) couldn't be reached online and were kept in the original language."
                color = "#F59E0B"
            else:
                color = "#10B981"

        self.status_label.config(text=msg, fg=color)
        if out_path:
            self.open_folder_btn.pack(padx=24, pady=(4, 0), anchor="w")

    def _on_error(self, err_msg):
        self.start_btn.set_config(text="Start", disabled=False)
        self.cancel_btn.pack_forget()
        self.cancel_btn.set_config(disabled=False)
        self.status_label.config(text=f"Error: {err_msg}", fg="#EF4444")

    def open_output_folder(self):
        if not self.output_path:
            return
        folder = os.path.dirname(os.path.abspath(self.output_path))
        if sys.platform == "win32":
            os.startfile(folder)
        elif sys.platform == "darwin":
            subprocess.Popen(["open", folder])
        else:
            subprocess.Popen(["xdg-open", folder])


# ==========================================================================
# CLI ENTRY POINT
# ==========================================================================

def run_cli():
    parser = argparse.ArgumentParser(description="Command line file translator.")
    parser.add_argument("--cli", dest="input", required=True, help="Input file")
    parser.add_argument("-o", "--output", help="Output file")
    parser.add_argument("--source", default="auto", help="Source language")
    parser.add_argument("--target", default="zh-TW", help="Target language")
    parser.add_argument("--clear-cache", action="store_true",
                         help="Wipe the persistent translation cache before running "
                              "(use this if a previous run left segments stuck untranslated)")
    args = parser.parse_args()

    if args.clear_cache:
        clear_cache()
        print("[translator] Cache cleared.")

    ext = os.path.splitext(args.input)[1].lower()
    if ext not in HANDLERS:
        print(f"Unsupported extension: {ext}", file=sys.stderr)
        sys.exit(1)

    out_path = args.output or f"{os.path.splitext(args.input)[0]}_{args.target}{ext}"

    def cli_progress(cur, tot):
        pct = (cur / max(1, tot)) * 100.0
        print(f"\rProgress: [{cur}/{tot}] ({pct:.1f}%)", end="", flush=True)

    print(f"Translating {args.input} -> {out_path} ...")
    start = time.time()
    _fail_counter.reset()
    HANDLERS[ext](args.input, out_path, args.source, args.target, cli_progress, None)
    fails = _fail_counter.get()
    print(f"\nCompleted in {time.time() - start:.2f}s.")
    if fails:
        print(f"Note: {fails} segment(s) could not be reached online and were kept in the original language.")


if __name__ == "__main__":
    if "--cli" in sys.argv:
        run_cli()
    else:
        app = TranslatorApp()
        app.mainloop()
