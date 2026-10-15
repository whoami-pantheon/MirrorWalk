# MirrorWalk

Offline OCR search for images. Single file, `mirrorwalk.py`: SQLite (FTS5) index, Tesseract OCR, interactive menu plus sub-commands.

## Run and test

```bash
source venv/bin/activate
pip install -r requirements.txt   # also needs the system Tesseract engine
python3 mirrorwalk.py             # menu; or: setup | index | refresh | search | status | prune | settings ...
python3 -m unittest discover -s tests -v
```

Tests fake OCR except one end-to-end test (skipped without Tesseract). Always point manual runs at a scratch index with `--db` and `MIRRORWALK_HOME`, never the user's real one.

## Architecture

- **DB:** `images` (one row per path), `roots`, `meta` (settings JSON), `images_fts` (external-content FTS5, kept in sync by triggers on `text, filename`). `norm` is a precomputed lowercase, punctuation-free copy of `text` for fuzzy search.
- **`scan_root`** (index/refresh): walk (blocked folders pruned) -> stat-cache classify -> threaded prepare (dimensions, md5) -> dedupe by hash / reuse text -> OCR pool (one job per unique hash) -> deletion detection. Same size+mtime = skipped without hashing; same hash = text reused.
- **Deleted files:** a vanished file with text keeps its row with `deleted_at` (detection time) so it stays searchable; one without text is dropped. Detection only counts a file missing if it is also gone from disk, and is skipped when an emptied folder had `DELETE_GUARD_MIN`+ records (likely an unmounted drive). `check_alive` also marks deletions at search time.
- **Search:** tiers phrase > near > all/match > substring > fuzzy (rapidfuzz `partial_ratio` over `norm`). Query tokens are always quoted before reaching FTS5, so user input cannot break the query. `since:` and `in:` are inline filters.
- **Blocked folders** (`BLOCKED_DIR_NAMES`, hidden dirs, `extra_blocked`) are never walked; `prune --blocked` removes stored records that match. Browser caches are scanned only through `default_cache_dirs()` when `include_browser_cache` is on (`kind='cache'`; images are located by sniffing magic bytes inside cache entries).
- **Settings:** `DEFAULT_SETTINGS`; 0 means unlimited for `max_results`, `fuzzy_max` and `page_size`.

## Conventions

- Keep it a single-file script with minimal pinned dependencies (no numpy: `rapidfuzz.process.cdist` needs it).
- Offline only: no network calls or telemetry.
- Cross-platform (Linux, macOS, Windows). macOS and Windows autostart code is untested on real machines.
- Anything destructive (`prune`, `roots remove --purge`) must stay opt-in with `--dry-run`/confirmation. Never overwrite or "repair" a database that fails to open.
- `input()` must not swallow `EOFError` (a closed stdin has to end the menu).

## Data

- The index, `results.json`, `results.csv`, `search_results.txt` and `errors.log` hold personal paths and OCR text from private screenshots (and possibly browser-cache content). They are git-ignored. Never commit, quote or paste their contents; print counts instead.
- Legacy `results.json` (v1) is only ever read by `import-legacy`.
