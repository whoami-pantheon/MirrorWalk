# MirrorWalk

                     "The reflection holds the memory..."

              >_ Engineering Systemic Memory & Data Hermeticism|

                         ┌───────────────────────────┐
                         │         MIRRORWALK        │
                         │     "Text from Image"     │
                         └───────────────────────────┘

                                     .---.
                                    /  .  \
                                   |   .   |
                                   | (   ) |
                                   |  `.'  |
                                    \  .  /
                                     `---'
                                       |
                                .------|------.
                               /       |       \
                              /        |        \
                             |         |         |
                             |         |         |
                              \        |        /
                               \       |       /
                                '------|------'
                                       |
                                       |
    
    
                      ...an observer in the gallery of pixels,    
    
                             [ M I R R O R W A L K ]
                           "Your Artifacts, Indexed."

## Author

*   **Name:** Clive Akporube
*   **GitHub:** [whoami-pantheon](https://github.com/whoami-pantheon)
*   **LinkedIn:** [Clive Kaiser](https://linkedin.com/in/clive-kaiser)

## Quick start

```bash
sudo apt install tesseract-ocr            # Mac: brew install tesseract   Windows: UB-Mannheim installer
python3 -m venv venv && source venv/bin/activate
pip install -r requirements.txt

python3 mirrorwalk.py setup               # finds your Pictures/Desktop/Downloads, offers login refresh, indexes
python3 mirrorwalk.py search "connection refused"
```

Run `python3 mirrorwalk.py` with no arguments for the interactive menu.

## Solving Information Entropy (A Glimpse into the Glass)

Organizational memory is often trapped in unindexed visual artifacts: architectural diagrams, legacy logs, and critical technical screenshots. MirrorWalk builds a fast, fully offline text index of them so that "that screenshot that said X" is one search away. Nothing leaves your machine: no telemetry, no network calls.

## What it does

*   **Full-text search that understands phrases.** Results are ranked: exact consecutive phrase, then words close together, then all words anywhere, then substring, then fuzzy (typo-tolerant). Put `"quotes"` around a sentence to require it exactly. Punctuation and case are ignored, so `"connection refused by peer"` finds `Error: Connection refused (by peer)!`.
*   **Deleted files are remembered, not blackholed.** If an image disappears, its text stays searchable and the result is flagged `DELETED - detected 3d ago`. If the file was moved, the result says where. Files that reappear are revived automatically.
*   **Fast refreshes.** Unchanged files are skipped by size and modification time (no re-hashing), identical copies are read once, touched files are verified by hash rather than re-read, and OCR runs on every core.
*   **Ignores noise by default.** Hidden folders, `venv`, `site-packages`, `node_modules`, thumbnails, `.git` and similar are never walked. Icons and thumbnails too small to hold text are skipped.
*   **Optional browser-cache indexing.** Turn on `include_browser_cache` to also read images from Chrome, Brave, Edge, Chromium and Firefox caches.
*   **Quick folders and auto refresh.** `setup` registers your default picture folders. The menu refreshes them on startup, and `setup --autostart` can refresh at every login.
*   **Safe by design.** The index is a SQLite database (crash-safe). A corrupt database is reported and never overwritten. Failed images are retried (3 times) and logged to `errors.log`.

## Commands

| Command | What it does |
|---|---|
| `setup [--yes] [--autostart] [--cache] [--no-index]` | First-run setup on a new computer. `--remove-autostart` undoes the login refresh. |
| `index PATH... [--flat]` | Index folders (they are remembered for refresh). |
| `refresh [--quiet] [--retry-errors]` | Re-scan every remembered folder: new, changed, deleted. |
| `search WORDS... [--limit N] [--all] [--since 7d] [--in PATH] [--hide-deleted] [--sort MODE] [--json] [--open N]` | Search. Inline filters work too: `search error since:2w in:~/Pictures`. Exit code 1 when nothing matches. |
| `roots [list\|add\|remove] [PATH]` | Manage remembered folders. |
| `defaults [--add]` | Show or register the standard picture folders. |
| `settings [KEY [VALUE]]` | View or change settings. |
| `status [--deleted]` | What is indexed, deleted, failed, and what could be pruned. |
| `prune --blocked / --deleted [--older-than 30d] / --errors [--dry-run] [--yes]` | Remove records you no longer want. |
| `errors` | List images that failed. |
| `export PATH.csv\|.json` | Export the index. |
| `import-legacy [results.json]` | Import an index from MirrorWalk 1.x (keeps the file; drops entries from ignored folders). |

In the interactive search: a number opens the image, `v N` opens the full OCR text in your default text viewer, `t N` prints it in the terminal, `f N` opens the image's folder, `c N` copies its path, `n`/`p` page, `s` changes the sort order, `e` exports. The text commands also work for deleted files. From the command line: `search ... --open N` (image) or `--open-text N` (full text).

## Settings

| Setting | Default | Meaning |
|---|---|---|
| `include_browser_cache` | off | Also index browser-cache images. |
| `auto_refresh` | on | Refresh registered folders when the menu starts. |
| `fuzzy` / `fuzzy_threshold` | on / 80 | Typo-tolerant matches below exact ones; 0-100, higher is stricter. |
| `max_results` | 2000 | Most results a search collects. **0 = unlimited.** |
| `fuzzy_max` | 200 | Most fuzzy matches added. **0 = unlimited.** |
| `default_sort` | relevance | Result order: `relevance`, `newest`, `oldest`, `name`, `path`, `largest`, `smallest`. |
| `page_size` | 20 | Results per page / shown by `search`. **0 = show everything.** |
| `include_hidden` | off | Walk hidden (dot) folders. |
| `extra_blocked` | none | Extra folder names to never walk, e.g. `memes,backup`. |
| `lang` | eng | Tesseract language(s), e.g. `eng+fra` (needs the language pack installed). |
| `workers` | 0 | OCR processes; 0 = one per CPU. |

## Where things live

The index is `index.db` in your per-user data folder (`~/.local/share/mirrorwalk`, `~/Library/Application Support/MirrorWalk`, or `%APPDATA%\MirrorWalk`). Override with `--db PATH`, `MIRRORWALK_DB` or `MIRRORWALK_HOME`. It contains paths and the text of your images, so treat it as sensitive.

## Upgrading from 1.x

Run `python3 mirrorwalk.py` in the folder that holds your old `results.json`, or `python3 mirrorwalk.py import-legacy`. Your OCR work is kept (no re-reading), files in ignored folders are dropped, and files that no longer exist are kept as deleted history. The old JSON/CSV files are never modified.

## Tests

```bash
python3 -m unittest discover -s tests -v
```

## A Word of Caution (The Sovereignty Mandate)

MirrorWalk is built for Privacy Sovereignty. It eliminates the privacy-utility trade-off. However, the architect is not responsible for the nature of the data you choose to index. Browser caches in particular can contain private pages; index them knowingly.

## License

This project is licensed under the Apache 2.0 License.

## Attribution & Foundational Dependencies

* **Tesseract OCR**: MirrorWalk utilizes the Tesseract engine as its foundational neural layer for high-fidelity character recognition.
* **The Digital Commons**: This instrument is built upon the robust architecture of the open-source community, leveraging specialized libraries for parallel execution and fuzzy logic.
* **Operational Context**: Designed for those who recognize that unindexed data is a liability, and seek to transform it into a sovereign asset.

---
*The reflection is vast, but the words are now within reach.*
---  
