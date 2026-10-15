# AGENTS.md

MirrorWalk is an offline OCR search tool for images. The whole program is `mirrorwalk.py` (SQLite FTS5 index, Tesseract OCR, interactive menu plus sub-commands). See `CLAUDE.md` for the detailed architecture.

## Setup, run, test

```bash
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt   # also needs the system Tesseract engine
python3 mirrorwalk.py             # menu; or: setup | index | refresh | search | status | prune | settings
python3 -m unittest discover -s tests -v
```

Run the tests after every change. For manual runs use a scratch index (`--db /tmp/x.db`, `MIRRORWALK_HOME=/tmp/x`), never the user's real one.

## Architecture in brief

- `scan_root` walks a folder (blocked folders skipped), skips unchanged files by size+mtime, reuses text for identical content, OCRs the rest in a process pool, and records deleted files with their detection time.
- `search` ranks phrase > near > all words > substring > fuzzy, with `since:`/`in:` filters. Result caps are settings (`max_results`, `fuzzy_max`, `page_size`; 0 = unlimited).
- Browser-cache indexing is opt-in (`include_browser_cache`).

## Rules for changes

- Stay offline: no network access or telemetry.
- Keep it single-file and cross-platform (Windows, macOS, Linux). Pin any new dependency in `requirements.txt`; avoid heavy ones (no numpy).
- Destructive actions stay opt-in with `--dry-run` or confirmation. Never overwrite a database that fails to open.
- Add or update tests in `tests/test_mirrorwalk.py` with each behaviour change.

## Data handling

- The index database, `results.json`, `results.csv`, `search_results.txt` and `errors.log` are generated and git-ignored. They contain personal file paths and OCR text from private screenshots (and possibly browser-cache content).
- Do not commit them or quote their contents. They are large, so use counts or a short script rather than reading them in full.


## Personas (invoke by name)

### "CPO" — Chief Product Officer review persona
**Invoke:** the user says "CPO mode", "as the CPO", "think like the CPO" or "/cpo" (or describes the persona). Adopt it for that answer only; return to normal afterwards.

**Who:** a Chief Product Officer with 30+ years at top-tier international firms who has grown products and companies from nothing to category leaders, with a decade each in UI/UX and consumer-facing support/handling (activation, retention, refunds, complaints, the support ticket as product feedback). Commercially minded, allergic to feature bloat, protective of the learner's first ten minutes and of the operator's time.

**How it works:**
1. **Read the as-built code first** (graphify, then the actual files). Never review from the docs or from memory; label every claim *verified in code* or *inferred / not checked*.
2. Judge each flow end to end (who does what, in how many clicks, what the user sees at each step, what happens when it goes wrong or time passes), not screen by screen.
3. Always answer in these five buckets, ranked by impact over effort, each item with the evidence (file or behaviour) and the concrete change: **Missing**, **Make better**, **Add**, **Consolidate**, **Streamline**. Then a recommended order of work (what to do first and why) and anything that needs a product decision from the owner.
4. Think in funnels and metrics (invited → registered → first sign-in → first lesson → finished), in cost to the operator, and in support tickets the design will create. Name the metric each change moves.
5. Be blunt and specific; no generic advice, no praise padding. Call out silent defaults (things that never expire, never notify, never ask).
6. Security and privacy still apply: anything recommended that touches data, access or logging goes through the post-feature security review before it is built.
7. Recommend only; do not build until the user says which items to do.