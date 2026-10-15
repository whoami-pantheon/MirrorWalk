#!/usr/bin/env python3
"""MirrorWalk: offline OCR search for your images.

Indexes images with Tesseract into a local SQLite (FTS5) database and searches the
extracted text. Run with no arguments for the interactive menu, or use the
sub-commands (index, refresh, search, setup, ...). See `mirrorwalk.py --help`.
"""
import argparse
import csv
import datetime
import glob
import hashlib
import io
import json
import os
import platform
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, wait, FIRST_COMPLETED

import pytesseract
from PIL import Image, ImageOps, ImageStat

os.environ.setdefault('OMP_THREAD_LIMIT', '1')
os.environ.setdefault('MKL_NUM_THREADS', '1')

__version__ = '2.0.0'

VALID_EXT = ('.png', '.jpg', '.jpeg', '.tiff', '.tif', '.bmp', '.webp', '.gif')
BATCH_SIZE = 25            # OCR results committed per batch
MAX_ATTEMPTS = 3           # failed images are retried this many times, then left alone
OCR_TIMEOUT = 90           # seconds per image before Tesseract is killed
CACHE_MIN_BYTES = 4096     # browser-cache entries smaller than this are never images worth reading
CACHE_SNIFF_BYTES = 16384  # how far into a cache entry to look for the image header
CACHE_MAX_BYTES = 40 * 1024 * 1024
LOCK_STALE_SECONDS = 6 * 3600
DELETE_GUARD_MIN = 5       # an emptied folder holding this many indexed images is treated as an unmounted drive

DEFAULT_SETTINGS = {
    'include_browser_cache': False,  # also index images found in browser caches
    'auto_refresh': True,            # refresh indexed folders when the menu starts
    'fuzzy': True,                   # add fuzzy matches below exact ones
    'fuzzy_threshold': 80,           # 0-100, higher = stricter
    'include_hidden': False,         # walk hidden (dot) folders
    'extra_blocked': [],             # extra folder names to never walk
    'lang': 'eng',                   # Tesseract language(s), e.g. eng+fra
    'workers': 0,                    # OCR processes, 0 = one per CPU
    'page_size': 20,                 # results shown per page / by `search` (0 = show everything)
    'max_results': 2000,             # most results a search collects (0 = unlimited)
    'fuzzy_max': 200,                # most fuzzy matches added to a search (0 = unlimited)
    'default_sort': 'relevance',     # relevance | newest | oldest | name | path | largest | smallest
}

SORT_MODES = {
    'relevance': 'best match first (phrase, near, all words, substring, fuzzy)',
    'newest': 'file date, newest first',
    'oldest': 'file date, oldest first',
    'name': 'file name, A to Z',
    'path': 'folder then name, A to Z',
    'largest': 'file size, largest first',
    'smallest': 'file size, smallest first',
}

# Folders that only ever hold program assets, never user images.
BLOCKED_DIR_NAMES = {
    'site-packages', 'dist-packages', 'node_modules', 'venv', '.venv', '__pycache__',
    '.git', '.svn', '.hg', 'thumbnails', '.thumbnails', 'appdata', '$recycle.bin',
}
BLOCKED_PAIRS = {('library', 'caches'), ('library', 'application support')}

TIER_ORDER = {'phrase': 0, 'near': 1, 'all': 2, 'match': 2, 'substring': 3, 'fuzzy': 4}
TIER_LABEL = {'phrase': 'Phrase', 'near': 'Near', 'all': 'All words', 'match': 'Match',
              'substring': 'Substring', 'fuzzy': 'Fuzzy'}


class MirrorWalkError(Exception):
    pass


# ----------------------------------------------------------------------------
# Paths, formatting, small helpers
# ----------------------------------------------------------------------------

def data_dir():
    override = os.environ.get('MIRRORWALK_HOME')
    if override:
        base = override
    elif platform.system() == 'Windows':
        base = os.path.join(os.environ.get('APPDATA', os.path.expanduser('~')), 'MirrorWalk')
    elif platform.system() == 'Darwin':
        base = os.path.expanduser('~/Library/Application Support/MirrorWalk')
    else:
        base = os.path.join(os.environ.get('XDG_DATA_HOME') or os.path.expanduser('~/.local/share'), 'mirrorwalk')
    os.makedirs(base, exist_ok=True)
    return base


def default_db_path():
    return os.environ.get('MIRRORWALK_DB') or os.path.join(data_dir(), 'index.db')


def norm_path(p):
    return os.path.abspath(os.path.expanduser(p))


def short_path(p):
    home = os.path.expanduser('~')
    if p == home:
        return '~'
    if p.startswith(home + os.sep):
        return '~' + p[len(home):]
    return p


def fmt_ago(seconds):
    s = int(max(0, seconds))
    if s < 60:
        return 'just now'
    for unit, size in (('y', 31536000), ('mo', 2592000), ('w', 604800), ('d', 86400), ('h', 3600), ('m', 60)):
        if s >= size:
            return f'{s // size}{unit} ago'
    return 'just now'


def parse_since(value, now=None):
    """'7d', '2w', '3mo', '1y', '12h' or an ISO date -> epoch seconds cutoff."""
    now = now or time.time()
    m = re.fullmatch(r'(\d+)\s*(h|d|w|mo|y)', value.strip().lower())
    if m:
        n, unit = int(m.group(1)), m.group(2)
        return now - n * {'h': 3600, 'd': 86400, 'w': 604800, 'mo': 2592000, 'y': 31536000}[unit]
    try:
        return datetime.datetime.fromisoformat(value.strip()).timestamp()
    except ValueError:
        raise MirrorWalkError(f"Can't read date '{value}'. Use e.g. 7d, 2w, 3mo, 1y or 2025-01-31.")


def human_bytes(n):
    for unit in ('B', 'KB', 'MB', 'GB'):
        if n < 1024 or unit == 'GB':
            return f'{n:.0f} {unit}' if unit == 'B' else f'{n:.1f} {unit}'
        n /= 1024


def use_color():
    if os.environ.get('NO_COLOR') or not sys.stdout.isatty():
        return False
    return platform.system() != 'Windows' or bool(os.environ.get('WT_SESSION'))


def say(msg='', quiet=False, **kw):
    if not quiet:
        print(msg, flush=True, **kw)


def ask(prompt, default=''):
    ans = input(prompt).strip()      # EOFError propagates so a closed stdin ends the program
    return ans or default


def yes_no(prompt, default=True):
    suffix = ' [Y/n] ' if default else ' [y/N] '
    ans = ask(prompt + suffix).lower()
    if not ans:
        return default
    return ans.startswith('y')


class Progress:
    """Single-line progress with rate and ETA; prints sparse lines when not a terminal."""

    def __init__(self, label, total, quiet=False):
        self.label, self.total, self.quiet = label, total, quiet
        self.n = 0
        self.t0 = time.time()
        self.last = 0.0
        self.tty = sys.stdout.isatty()

    def update(self, n=1):
        self.n += n
        if self.quiet:
            return
        now = time.time()
        if now - self.last < (0.2 if self.tty else 15) and self.n < self.total:
            return
        self.last = now
        elapsed = max(now - self.t0, 1e-6)
        rate = self.n / elapsed
        eta = (self.total - self.n) / rate if rate else 0
        line = f'{self.label}: {self.n}/{self.total}  {rate:.1f}/s  ETA {int(eta // 60)}m{int(eta % 60):02d}s'
        if self.tty:
            print('\r' + line.ljust(70), end='', flush=True)
        else:
            print(line, flush=True)

    def done(self):
        if self.tty and not self.quiet and self.n:
            print('\r' + ' ' * 70 + '\r', end='', flush=True)


# ----------------------------------------------------------------------------
# Database
# ----------------------------------------------------------------------------

SCHEMA = """
CREATE TABLE IF NOT EXISTS images(
    id INTEGER PRIMARY KEY,
    path TEXT NOT NULL UNIQUE,
    filename TEXT NOT NULL,
    hash TEXT,
    size INTEGER,
    mtime_ns INTEGER,
    width INTEGER,
    height INTEGER,
    text TEXT NOT NULL DEFAULT '',
    norm TEXT,
    status TEXT NOT NULL DEFAULT 'ok',
    note TEXT,
    attempts INTEGER NOT NULL DEFAULT 0,
    ocr_ms INTEGER,
    source TEXT NOT NULL DEFAULT 'file',
    root TEXT,
    data_offset INTEGER NOT NULL DEFAULT 0,
    indexed_at REAL,
    last_seen REAL,
    deleted_at REAL
);
CREATE INDEX IF NOT EXISTS images_hash ON images(hash);
CREATE INDEX IF NOT EXISTS images_deleted ON images(deleted_at);
CREATE TABLE IF NOT EXISTS roots(
    path TEXT PRIMARY KEY,
    kind TEXT NOT NULL DEFAULT 'file',
    recursive INTEGER NOT NULL DEFAULT 1,
    added_at REAL,
    last_indexed REAL
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
CREATE VIRTUAL TABLE IF NOT EXISTS images_fts USING fts5(
    text, filename, content='images', content_rowid='id',
    tokenize='unicode61 remove_diacritics 2');
CREATE TRIGGER IF NOT EXISTS images_ai AFTER INSERT ON images BEGIN
    INSERT INTO images_fts(rowid, text, filename) VALUES (new.id, new.text, new.filename);
END;
CREATE TRIGGER IF NOT EXISTS images_ad AFTER DELETE ON images BEGIN
    INSERT INTO images_fts(images_fts, rowid, text, filename) VALUES ('delete', old.id, old.text, old.filename);
END;
CREATE TRIGGER IF NOT EXISTS images_au AFTER UPDATE OF text, filename ON images BEGIN
    INSERT INTO images_fts(images_fts, rowid, text, filename) VALUES ('delete', old.id, old.text, old.filename);
    INSERT INTO images_fts(rowid, text, filename) VALUES (new.id, new.text, new.filename);
END;
"""


def connect(path=None):
    path = path or default_db_path()
    os.makedirs(os.path.dirname(os.path.abspath(path)), exist_ok=True)
    try:
        conn = sqlite3.connect(path, timeout=30)
        conn.row_factory = sqlite3.Row
        conn.execute('PRAGMA journal_mode=WAL')
        conn.execute('PRAGMA synchronous=NORMAL')
        conn.executescript(SCHEMA)
        cols = {r['name'] for r in conn.execute('PRAGMA table_info(images)')}
        if 'norm' not in cols:
            conn.execute('ALTER TABLE images ADD COLUMN norm TEXT')
        todo = conn.execute("SELECT id, text FROM images WHERE norm IS NULL").fetchall()
        conn.executemany('UPDATE images SET norm=? WHERE id=?', [(norm_text(r['text']), r['id']) for r in todo])
        conn.commit()
    except sqlite3.DatabaseError as e:
        try:
            conn.close()
        except (NameError, UnboundLocalError, sqlite3.Error):
            pass
        raise MirrorWalkError(
            f'Cannot open the index at {path}: {e}\n'
            'The file was NOT modified. Move it aside (or restore a backup) and run again to start a new index.')
    return conn


def db_file(conn):
    return conn.execute('PRAGMA database_list').fetchone()['file']


def load_settings(conn):
    row = conn.execute("SELECT value FROM meta WHERE key='settings'").fetchone()
    settings = {k: (list(v) if isinstance(v, list) else v) for k, v in DEFAULT_SETTINGS.items()}
    if row:
        try:
            settings.update({k: v for k, v in json.loads(row['value']).items() if k in DEFAULT_SETTINGS})
        except ValueError:
            pass
    return settings


def coerce_setting(key, value):
    if key not in DEFAULT_SETTINGS:
        raise MirrorWalkError(f"Unknown setting '{key}'. Known: {', '.join(DEFAULT_SETTINGS)}")
    default = DEFAULT_SETTINGS[key]
    if isinstance(default, bool):
        v = str(value).strip().lower()
        if v in ('1', 'true', 'yes', 'y', 'on'):
            return True
        if v in ('0', 'false', 'no', 'n', 'off'):
            return False
        raise MirrorWalkError(f"'{key}' must be on or off.")
    if isinstance(default, int):
        try:
            return int(value)
        except ValueError:
            raise MirrorWalkError(f"'{key}' must be a whole number.")
    if isinstance(default, list):
        return [x.strip() for x in str(value).split(',') if x.strip()]
    value = str(value).strip()
    if key == 'default_sort':
        value = value.lower()
        if value not in SORT_MODES:
            raise MirrorWalkError(f"'default_sort' must be one of: {', '.join(SORT_MODES)}")
    return value


def save_setting(conn, key, value):
    settings = load_settings(conn)
    settings[key] = value
    conn.execute("INSERT INTO meta(key,value) VALUES('settings',?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                 (json.dumps(settings),))
    conn.commit()
    return settings


def upsert_image(conn, **f):
    if 'text' in f:
        f['norm'] = norm_text(f['text'])   # lowercase, punctuation-free copy used by fuzzy search
    cols = list(f)
    sql = (f"INSERT INTO images({','.join(cols)}) VALUES({','.join('?' * len(cols))}) "
           f"ON CONFLICT(path) DO UPDATE SET " + ','.join(f'{c}=excluded.{c}' for c in cols if c != 'path'))
    conn.execute(sql, [f[c] for c in cols])


def scope_range(root):
    lo = root.rstrip(os.sep) + os.sep
    return lo, lo + '\U0010ffff'


# ----------------------------------------------------------------------------
# Blocked folders and browser caches
# ----------------------------------------------------------------------------

def dir_blocked(name, parent, settings):
    low = name.lower()
    if low in BLOCKED_DIR_NAMES or low in {x.lower() for x in settings['extra_blocked']}:
        return low
    if name.startswith('.') and not settings['include_hidden']:
        return 'hidden folder'
    if (parent.lower(), low) in BLOCKED_PAIRS:
        return f'{parent}/{name}'
    return None


def blocked_reason(parts, settings):
    """First blocked folder name in a list of directory names, or None."""
    for i, name in enumerate(parts):
        why = dir_blocked(name, parts[i - 1] if i else '', settings)
        if why:
            return why
    return None


CACHE_GLOBS = {
    'Linux': [
        '~/.cache/google-chrome/*/Cache/Cache_Data', '~/.cache/chromium/*/Cache/Cache_Data',
        '~/.cache/BraveSoftware/Brave-Browser/*/Cache/Cache_Data', '~/.cache/microsoft-edge/*/Cache/Cache_Data',
        '~/.cache/vivaldi/*/Cache/Cache_Data', '~/.cache/opera/*/Cache/Cache_Data',
        '~/.cache/mozilla/firefox/*/cache2/entries',
        '~/snap/chromium/common/.cache/chromium/*/Cache/Cache_Data',
        '~/.var/app/org.mozilla.firefox/cache/mozilla/firefox/*/cache2/entries',
        '~/.var/app/com.google.Chrome/cache/google-chrome/*/Cache/Cache_Data',
    ],
    'Darwin': [
        '~/Library/Caches/Google/Chrome/*/Cache/Cache_Data', '~/Library/Caches/BraveSoftware/Brave-Browser/*/Cache/Cache_Data',
        '~/Library/Caches/Microsoft Edge/*/Cache/Cache_Data', '~/Library/Caches/Firefox/Profiles/*/cache2/entries',
        '~/Library/Caches/com.operasoftware.Opera/Cache/Cache_Data',
    ],
    'Windows': [
        '%LOCALAPPDATA%/Google/Chrome/User Data/*/Cache/Cache_Data',
        '%LOCALAPPDATA%/BraveSoftware/Brave-Browser/User Data/*/Cache/Cache_Data',
        '%LOCALAPPDATA%/Microsoft/Edge/User Data/*/Cache/Cache_Data',
        '%LOCALAPPDATA%/Mozilla/Firefox/Profiles/*/cache2/entries',
    ],
}


def default_cache_dirs():
    out = []
    for pattern in CACHE_GLOBS.get(platform.system(), []):
        pattern = os.path.expandvars(os.path.expanduser(pattern))
        out.extend(p for p in sorted(glob.glob(pattern)) if os.path.isdir(p))
    return out


def xdg_dir(name, fallback):
    cfg = os.path.expanduser('~/.config/user-dirs.dirs')
    try:
        with open(cfg, encoding='utf-8') as f:
            for line in f:
                m = re.match(rf'\s*XDG_{name}_DIR="?([^"\n]+)"?', line)
                if m:
                    return os.path.expandvars(m.group(1).replace('$HOME', os.path.expanduser('~')))
    except OSError:
        pass
    return os.path.expanduser(fallback)


def default_dirs():
    """Existing folders where screenshots and pictures usually live: [(label, path)]."""
    cands = [('Pictures', xdg_dir('PICTURES', '~/Pictures')),
             ('Desktop', xdg_dir('DESKTOP', '~/Desktop')),
             ('Downloads', xdg_dir('DOWNLOAD', '~/Downloads')),
             ('Documents', xdg_dir('DOCUMENTS', '~/Documents'))]
    out, seen = [], set()
    for label, path in cands:
        path = norm_path(path)
        if os.path.isdir(path) and path != os.path.expanduser('~') and path not in seen:
            seen.add(path)
            out.append((label, path))
    return out


# ----------------------------------------------------------------------------
# Image reading, preprocessing, OCR
# ----------------------------------------------------------------------------

_MAGICS = ((b'\x89PNG\r\n\x1a\n', 'png'), (b'\xff\xd8\xff', 'jpg'), (b'GIF87a', 'gif'), (b'GIF89a', 'gif'))


def sniff_image(path):
    """Offset of an embedded image inside a (browser cache) file, or None."""
    try:
        with open(path, 'rb') as f:
            head = f.read(CACHE_SNIFF_BYTES)
    except OSError:
        return None
    best = None
    for magic, _ in _MAGICS:
        i = head.find(magic)
        if i >= 0 and (best is None or i < best):
            best = i
    j = head.find(b'RIFF')
    while j >= 0:
        if head[j + 8:j + 12] == b'WEBP':
            if best is None or j < best:
                best = j
            break
        j = head.find(b'RIFF', j + 1)
    return best


def load_image(path, offset=0):
    if offset:
        with open(path, 'rb') as f:
            f.seek(offset)
            data = f.read(CACHE_MAX_BYTES)
        return Image.open(io.BytesIO(data))
    return Image.open(path)


def too_small(w, h):
    """Icons and thumbnails: no room for readable text."""
    return max(w, h) < 100 or min(w, h) < 24


def file_md5(path):
    h = hashlib.md5(usedforsecurity=False)
    with open(path, 'rb') as f:
        for chunk in iter(lambda: f.read(1 << 20), b''):
            h.update(chunk)
    return h.hexdigest()


def prepare_for_ocr(img):
    """Grayscale, flatten transparency, invert dark-mode images, upscale small ones.

    Returns (image, inverted)."""
    if img.mode in ('RGBA', 'LA') or (img.mode == 'P' and 'transparency' in img.info):
        rgba = img.convert('RGBA')
        bg = Image.new('RGBA', rgba.size, (255, 255, 255, 255))
        img = Image.alpha_composite(bg, rgba)
    gray = img.convert('L')
    inverted = False
    if ImageStat.Stat(gray.resize((64, 64))).mean[0] < 100:
        gray = ImageOps.invert(gray)
        inverted = True
    longest = max(gray.size)
    if longest < 1000:
        scale = min(2.0, 2500 / longest)
        gray = gray.resize((int(gray.width * scale), int(gray.height * scale)), Image.LANCZOS)
    return gray, inverted


def ocr_image(path, offset=0, lang='eng', timeout=OCR_TIMEOUT):
    t0 = time.time()
    try:
        with load_image(path, offset) as img:
            img.load()
            gray, inverted = prepare_for_ocr(img)
        config = '--oem 3 --psm 3' + (' -c tessedit_do_invert=0' if inverted else '')
        text = pytesseract.image_to_string(gray, lang=lang, config=config, timeout=timeout)
        return {'ok': True, 'text': ' '.join(text.split()), 'ms': int((time.time() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001 - any failure is recorded against the image
        return {'ok': False, 'error': f'{type(e).__name__}: {e}'[:300], 'ms': int((time.time() - t0) * 1000)}


def ocr_job(job):
    key, path, offset, lang = job
    return key, ocr_image(path, offset, lang)


def run_ocr(jobs, workers, lang, inline, on_result):
    """Run OCR jobs [(key, path, offset)], calling on_result(key, result) as they finish."""
    jobs = [(k, p, o, lang) for k, p, o in jobs]
    if inline or len(jobs) <= 2:
        for job in jobs:
            on_result(*ocr_job(job))
        return
    n = max(1, workers or os.cpu_count() or 1)
    it, pending = iter(jobs), set()
    ex = ProcessPoolExecutor(max_workers=n)
    try:
        while True:
            while len(pending) < n * 2:
                job = next(it, None)
                if job is None:
                    break
                pending.add(ex.submit(ocr_job, job))
            if not pending:
                break
            done, pending = wait(pending, return_when=FIRST_COMPLETED)
            for fut in done:
                on_result(*fut.result())
    except BaseException:
        ex.shutdown(wait=False, cancel_futures=True)
        raise
    ex.shutdown(wait=True)


def check_tesseract(lang='eng'):
    """(ok, info, hint). info is the version string; hint says how to fix a problem."""
    try:
        version = str(pytesseract.get_tesseract_version())
    except Exception:  # noqa: BLE001
        system = platform.system()
        hint = {'Windows': 'Install it from https://github.com/UB-Mannheim/tesseract/wiki and add it to PATH.',
                'Darwin': 'Run: brew install tesseract'}.get(system, 'Run: sudo apt install tesseract-ocr')
        return False, None, f'Tesseract engine not found. {hint}'
    try:
        langs = set(pytesseract.get_languages(config=''))
    except Exception:  # noqa: BLE001
        langs = set()
    wanted = [x for x in lang.split('+') if x]
    missing = [x for x in wanted if langs and x not in langs]
    if missing:
        return False, version, (f"Tesseract language pack(s) missing: {', '.join(missing)} "
                                f"(installed: {', '.join(sorted(langs))}). "
                                f"Try: sudo apt install tesseract-ocr-{missing[0]}  (or brew install tesseract-lang)")
    return True, version, ''


# ----------------------------------------------------------------------------
# Scanning / indexing
# ----------------------------------------------------------------------------

def iter_files(root, recursive, settings, stats, kind='file'):
    stack = [(root, '')]
    while stack:
        d, parent = stack.pop()
        try:
            it = os.scandir(d)
        except OSError:
            stats['unreadable'] += 1
            continue
        with it:
            for e in it:
                try:
                    if e.is_dir(follow_symlinks=False):
                        if not recursive:
                            continue
                        if kind == 'file' and dir_blocked(e.name, os.path.basename(d), settings):
                            stats['blocked_dirs'] += 1
                            continue
                        stack.append((e.path, e.name))
                    elif e.is_file():
                        if kind == 'file':
                            if not e.name.lower().endswith(VALID_EXT):
                                continue
                        st = e.stat()
                        if kind == 'cache' and st.st_size < CACHE_MIN_BYTES:
                            stats['cache_tiny'] += 1
                            continue
                        yield e.path, st.st_size, st.st_mtime_ns
                except OSError:
                    stats['unreadable'] += 1


def load_scope(conn, root, kind, recursive):
    lo, hi = scope_range(root)
    out = {}
    for r in conn.execute('SELECT * FROM images WHERE source=? AND path>=? AND path<?', (kind, lo, hi)):
        if not recursive and os.path.dirname(r['path']) != root:
            continue
        out[r['path']] = r
    return out


def prepare_item(task):
    """Cheap per-file checks, run in a thread pool: kind, dimensions, hash."""
    path, kind = task
    try:
        offset = 0
        if kind == 'cache':
            offset = sniff_image(path)
            if offset is None:
                return {'path': path, 'skip': 'not-image'}
        try:
            with load_image(path, offset) as im:
                w, h = im.size
        except Exception:
            if kind == 'cache':
                return {'path': path, 'skip': 'not-image'}
            raise
        if too_small(w, h):
            return {'path': path, 'skip': f'too-small:{w}x{h}', 'width': w, 'height': h}
        return {'path': path, 'hash': file_md5(path), 'width': w, 'height': h, 'offset': offset}
    except Exception as e:  # noqa: BLE001
        return {'path': path, 'error': f'{type(e).__name__}: {e}'[:300]}


def log_error(path, message):
    try:
        with open(os.path.join(data_dir(), 'errors.log'), 'a', encoding='utf-8') as f:
            f.write(f'{datetime.datetime.now().isoformat(timespec="seconds")}\t{path}\t{message}\n')
    except OSError:
        pass


def scan_root(conn, root, settings, *, kind='file', recursive=True, quiet=False, inline=None,
              retry_errors=False, now=None):
    """Scan one folder: detect new/changed/deleted images and OCR what is new. Returns a Counter."""
    root = norm_path(root)
    stats = Counter()
    now = now or time.time()
    if not os.path.isdir(root):
        stats['root_missing'] = 1
        return stats

    seen = {}
    for path, size, mtime in iter_files(root, recursive, settings, stats, kind):
        seen[path] = (size, mtime)
    stats['found'] = len(seen)
    existing = load_scope(conn, root, kind, recursive)

    touch, todo = [], []
    for path, (size, mtime) in seen.items():
        row = existing.get(path)
        if row is None:
            todo.append((path, size, mtime, None))
            continue
        if row['size'] != size or row['mtime_ns'] != mtime:
            todo.append((path, size, mtime, row))
            continue
        if row['status'] == 'skipped':
            note = row['note'] or ''
            if note.startswith('too-small') and row['width'] and not too_small(row['width'], row['height']):
                todo.append((path, size, mtime, row))
                continue
        elif row['status'] == 'error' and (row['attempts'] < MAX_ATTEMPTS or retry_errors):
            todo.append((path, size, mtime, row))
            continue
        if row['deleted_at'] is not None:
            stats['revived'] += 1
            conn.execute('UPDATE images SET deleted_at=NULL WHERE id=?', (row['id'],))
        touch.append((now, root, row['id']))
        stats['unchanged'] += 1
    conn.executemany('UPDATE images SET last_seen=?, root=? WHERE id=?', touch)
    conn.commit()

    # Phase 1: cheap checks (dimensions, hash) in threads.
    prepared = []
    if todo:
        prog = Progress('Checking files', len(todo), quiet)
        meta = {p: (s, m, r) for p, s, m, r in todo}
        with ThreadPoolExecutor(max_workers=min(16, (os.cpu_count() or 2) * 2)) as ex:
            for item in ex.map(prepare_item, [(p, kind) for p, *_ in todo], chunksize=16):
                item['size'], item['mtime_ns'], item['row'] = meta[item['path']]
                prepared.append(item)
                prog.update()
        prog.done()

    # Phase 2: decide what needs OCR.
    to_ocr = {}      # hash -> (path, offset)
    groups = {}      # hash -> [items waiting for the text]
    base = dict(root=root, source=kind, last_seen=now, deleted_at=None)

    for item in prepared:
        path, row = item['path'], item['row']
        fields = dict(base, path=path, filename=os.path.basename(path), size=item['size'], mtime_ns=item['mtime_ns'],
                      width=item.get('width'), height=item.get('height'))
        if kind == 'cache':
            fields['filename'] = os.path.basename(path) + ' (cache)'
        if 'skip' in item:
            stats['skipped_small' if item['skip'].startswith('too-small') else 'skipped_not_image'] += 1
            upsert_image(conn, **fields, hash=None, text='', status='skipped', note=item['skip'], attempts=0,
                         ocr_ms=None, data_offset=0, indexed_at=now)
            continue
        if 'error' in item:
            attempts = (row['attempts'] if row else 0) + 1
            stats['errors'] += 1
            log_error(path, item['error'])
            upsert_image(conn, **fields, hash=None, text='', status='error', note=item['error'], attempts=attempts,
                         ocr_ms=None, data_offset=0, indexed_at=now)
            continue
        h = item['hash']
        fields.update(hash=h, data_offset=item['offset'])
        if row is not None and row['hash'] == h and row['status'] == 'ok':
            stats['verified'] += 1          # same content, only the file stamp changed
            upsert_image(conn, **fields, text=row['text'], status='ok', note=None, attempts=0,
                         ocr_ms=row['ocr_ms'], indexed_at=row['indexed_at'] or now)
            continue
        known = conn.execute("SELECT text, ocr_ms FROM images WHERE hash=? AND status='ok' AND path!=? LIMIT 1",
                             (h, path)).fetchone()
        if known is not None:
            stats['reused'] += 1            # identical content already OCRed under another path
            upsert_image(conn, **fields, text=known['text'], status='ok', note=None, attempts=0,
                         ocr_ms=known['ocr_ms'], indexed_at=now)
            continue
        item['fields'] = fields
        groups.setdefault(h, []).append(item)
        to_ocr.setdefault(h, (path, item['offset']))
    conn.commit()

    # Phase 3: OCR, one job per unique content hash.
    if to_ocr:
        prog = Progress('Reading text', len(to_ocr), quiet)
        done_count = [0]

        def on_result(h, res):
            for i, item in enumerate(groups[h]):
                f = item['fields']
                row = item['row']
                if res['ok']:
                    stats['ocr' if i == 0 else 'reused'] += 1
                    upsert_image(conn, **f, text=res['text'], status='ok', note=None, attempts=0,
                                 ocr_ms=res['ms'], indexed_at=now)
                else:
                    stats['errors'] += 1
                    log_error(f['path'], res['error'])
                    upsert_image(conn, **f, text='', status='error', note=res['error'],
                                 attempts=(row['attempts'] if row else 0) + 1, ocr_ms=res['ms'], indexed_at=now)
            done_count[0] += 1
            prog.update()
            if done_count[0] % BATCH_SIZE == 0:
                conn.commit()

        jobs = [(h, p, o) for h, (p, o) in to_ocr.items()]
        try:
            run_ocr(jobs, settings['workers'], settings['lang'], inline, on_result)
        finally:
            conn.commit()
            prog.done()

    # Phase 4: deleted-file detection.
    missing = [r for p, r in existing.items() if p not in seen and r['deleted_at'] is None]
    if len(missing) >= DELETE_GUARD_MIN and not seen:
        stats['delete_check_skipped'] = len(missing)   # empty folder with a populated index: likely unmounted
    else:
        for r in missing:
            if os.path.exists(r['path']):
                continue
            if r['status'] == 'ok' and (r['text'] or '').strip():
                conn.execute('UPDATE images SET deleted_at=? WHERE id=?', (now, r['id']))
                stats['deleted'] += 1
            else:
                conn.execute('DELETE FROM images WHERE id=?', (r['id'],))
                stats['forgotten'] += 1
    conn.execute('INSERT INTO roots(path,kind,recursive,added_at,last_indexed) VALUES(?,?,?,?,?) '
                 'ON CONFLICT(path) DO UPDATE SET last_indexed=excluded.last_indexed, recursive=excluded.recursive',
                 (root, kind, int(recursive), now, now))
    conn.commit()
    return stats


def summarize(stats, root=None):
    parts = []
    labels = [('ocr', 'new text read'), ('reused', 'duplicates reused'), ('verified', 'verified unchanged'),
              ('unchanged', 'already indexed'), ('revived', 'files reappeared'), ('deleted', 'deleted files detected'),
              ('forgotten', 'empty records dropped'), ('skipped_small', 'icons skipped'),
              ('skipped_not_image', 'non-images skipped'), ('blocked_dirs', 'folders ignored'),
              ('errors', 'failed')]
    for key, label in labels:
        if stats.get(key):
            parts.append(f'{stats[key]} {label}')
    head = f'{short_path(root)}: ' if root else ''
    if stats.get('root_missing'):
        return head + 'folder not found (drive unmounted?) - nothing changed.'
    msg = head + (', '.join(parts) if parts else 'nothing to do')
    if stats.get('delete_check_skipped'):
        msg += (f"\n  ! Found no images, but the index holds {stats['delete_check_skipped']} for this folder. "
                'Deletion check skipped in case the drive is not mounted.')
    return msg


class RefreshLock:
    """Keeps two refreshes (menu + autostart) from OCRing the same files at once."""

    def __init__(self):
        self.path = os.path.join(data_dir(), 'refresh.lock')
        self.held = False

    def _stale(self):
        try:
            if time.time() - os.path.getmtime(self.path) > LOCK_STALE_SECONDS:
                return True
            with open(self.path) as f:
                pid = int(f.read().strip() or 0)
            if pid and platform.system() != 'Windows':
                os.kill(pid, 0)
        except ProcessLookupError:
            return True
        except (OSError, ValueError):
            return False
        return False

    def __enter__(self):
        for _ in range(2):
            try:
                fd = os.open(self.path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
                os.write(fd, str(os.getpid()).encode())
                os.close(fd)
                self.held = True
                return self
            except FileExistsError:
                if self._stale():
                    try:
                        os.remove(self.path)
                    except OSError:
                        pass
                else:
                    break
        return self

    def __exit__(self, *exc):
        if self.held:
            try:
                os.remove(self.path)
            except OSError:
                pass


def index_paths(conn, paths, settings, *, recursive=True, quiet=False, inline=None):
    total = Counter()
    for p in paths:
        p = norm_path(p)
        if not os.path.isdir(p):
            say(f"  ! Not a folder: {p}", quiet)
            continue
        say(f'Indexing {short_path(p)} ...', quiet)
        st = scan_root(conn, p, settings, recursive=recursive, quiet=quiet, inline=inline)
        say('  ' + summarize(st), quiet)
        total.update(st)
    return total


def refresh_all(conn, settings, *, quiet=False, inline=None, retry_errors=False):
    """Refresh every registered folder (and browser caches if enabled). Returns a Counter."""
    total = Counter()
    with RefreshLock() as lock:
        if not lock.held:
            say('Another MirrorWalk refresh is already running; skipping.', quiet)
            return total
        roots = [(r['path'], bool(r['recursive'])) for r in
                 conn.execute("SELECT path, recursive FROM roots WHERE kind='file' ORDER BY path")]
        caches = default_cache_dirs() if settings['include_browser_cache'] else []
        if not roots and not caches:
            say('No folders registered yet. Index a folder first, or run: mirrorwalk.py setup', quiet)
            return total
        try:
            for path, rec in roots:
                st = scan_root(conn, path, settings, recursive=rec, quiet=quiet, inline=inline, retry_errors=retry_errors)
                say('  ' + summarize(st, path), quiet)
                total.update(st)
            for path in caches:
                st = scan_root(conn, path, settings, kind='cache', recursive=True, quiet=quiet, inline=inline,
                               retry_errors=retry_errors)
                say('  ' + summarize(st, path), quiet)
                total.update(st)
        except KeyboardInterrupt:
            say('\nInterrupted. Progress is saved; the rest will be picked up next time.', quiet)
    return total


# ----------------------------------------------------------------------------
# Pruning and legacy import
# ----------------------------------------------------------------------------

def find_prune_candidates(conn, settings, *, deleted=False, older_than=None, blocked=False, errors=False):
    """{category: [ids]} of rows that would be removed. older_than is a '30d' style age for deleted rows."""
    out = {'deleted': [], 'blocked': [], 'errors': []}
    now = time.time()
    min_age = 0
    if older_than:
        min_age = now - parse_since(older_than, now)
    if deleted:
        for r in conn.execute('SELECT id, deleted_at FROM images WHERE deleted_at IS NOT NULL'):
            if now - r['deleted_at'] >= min_age:
                out['deleted'].append(r['id'])
    if blocked:
        for r in conn.execute('SELECT id, path, root, source FROM images'):
            if r['source'] == 'cache':
                if not settings['include_browser_cache']:
                    out['blocked'].append(r['id'])
                continue
            d = os.path.dirname(r['path'])
            if r['root'] and (d == r['root'] or d.startswith(r['root'].rstrip(os.sep) + os.sep)):
                rel = d[len(r['root']):].strip(os.sep)
                parts = rel.split(os.sep) if rel else []
            else:
                parts = [p for p in d.split(os.sep) if p]
            if blocked_reason(parts, settings):
                out['blocked'].append(r['id'])
    if errors:
        out['errors'] = [r['id'] for r in conn.execute("SELECT id FROM images WHERE status='error'")]
    return out


def apply_prune(conn, candidates):
    ids = sorted({i for v in candidates.values() for i in v})
    for k in range(0, len(ids), 500):
        chunk = ids[k:k + 500]
        conn.execute(f"DELETE FROM images WHERE id IN ({','.join('?' * len(chunk))})", chunk)
    conn.commit()
    if len(ids) > 1000:
        conn.execute("INSERT INTO images_fts(images_fts) VALUES('optimize')")
        conn.commit()
        conn.execute('VACUUM')
    return len(ids)


def import_legacy(conn, json_path, settings, now=None):
    """Import a v1 results.json (keyed by md5). Blocked-folder entries are dropped; missing files are kept
    as deleted (detected now) when they have text. The JSON file itself is never modified."""
    now = now or time.time()
    try:
        with open(json_path, encoding='utf-8') as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        raise MirrorWalkError(f'Cannot read {json_path}: {e}. Nothing was imported.')
    json_mtime = os.path.getmtime(json_path)
    stats = Counter()
    for h, info in data.items():
        path = info.get('full_path') or info.get('filename')
        if not path:
            continue
        d = os.path.dirname(path)
        if blocked_reason([p for p in d.split(os.sep) if p], settings):
            stats['blocked'] += 1
            continue
        text = info.get('text') or ''
        failed = text.startswith('Error:')
        if failed:
            text = ''
        exists = os.path.exists(path)
        if not exists and not text.strip():
            stats['dropped_empty_missing'] += 1
            continue
        size = mtime_ns = None
        if exists:
            try:
                st = os.stat(path)
                if st.st_mtime <= json_mtime:       # unchanged since it was indexed: trust the stored hash
                    size, mtime_ns = st.st_size, st.st_mtime_ns
            except OSError:
                pass
        upsert_image(conn, path=path, filename=info.get('filename') or os.path.basename(path), hash=h, size=size,
                     mtime_ns=mtime_ns, width=None, height=None, text=text,
                     status='error' if failed else 'ok', note='legacy import: ' + info['text'][:200] if failed else None,
                     attempts=0, ocr_ms=int(float(info.get('time', 0)) * 1000), source='file', root=None,
                     data_offset=0, indexed_at=now, last_seen=now if exists else None,
                     deleted_at=None if exists else now)
        stats['missing' if not exists else 'imported'] += 1
    conn.commit()
    return stats


# ----------------------------------------------------------------------------
# Search
# ----------------------------------------------------------------------------

_FILTER_RE = re.compile(r'\b(since|in):(?:"([^"]*)"|(\S+))', re.I)
_TOKEN_RE = re.compile(r'\w+', re.UNICODE)


def norm_text(s):
    return ' '.join(_TOKEN_RE.findall(s.lower()))


def parse_query(raw):
    """'error "connection refused" since:7d in:~/Pictures' -> parsed dict."""
    filters = {}
    for m in _FILTER_RE.finditer(raw):
        filters[m.group(1).lower()] = m.group(2) if m.group(2) is not None else m.group(3)
    rest = _FILTER_RE.sub(' ', raw)
    phrases = [t for t in (_TOKEN_RE.findall(p) for p in re.findall(r'"([^"]*)"', rest)) if t]
    rest = re.sub(r'"[^"]*"', ' ', rest)
    terms = [t.lower() for t in _TOKEN_RE.findall(rest)]
    phrases = [[t.lower() for t in p] for p in phrases]
    plain = ' '.join(re.sub(r'["]', ' ', rest).split()).lower()
    if phrases and not terms:
        plain = ' '.join(' '.join(p) for p in phrases)
    return {'terms': terms, 'phrases': phrases, 'plain': plain, 'since': filters.get('since'), 'in': filters.get('in')}


def _term_expr(t):
    return f'"{t}"*' if len(t) >= 3 else f'"{t}"'


def _phrase_expr(toks):
    return '"' + ' '.join(toks) + '"'


def build_tiers(parsed):
    terms, phrases = parsed['terms'], parsed['phrases']
    if phrases or len(terms) < 2:
        comps = [_phrase_expr(p) for p in phrases] + [_term_expr(t) for t in terms]
        return [('phrase' if phrases and not terms else 'match', ' AND '.join(comps))]
    return [('phrase', _phrase_expr(terms)),
            ('near', 'NEAR(' + ' '.join(_term_expr(t) for t in terms) + ', 8)'),
            ('all', ' AND '.join(_term_expr(t) for t in terms))]


def make_snippet(text, parsed, width=60):
    """Context around the first match with \\x01..\\x02 markers around the matched text."""
    needles = [r'\W+'.join(re.escape(t) for t in p) for p in parsed['phrases']]
    if len(parsed['terms']) > 1:
        needles.append(r'\W+'.join(re.escape(t) for t in parsed['terms']))
    if parsed['plain']:
        needles.append(re.escape(parsed['plain']))
    needles += [re.escape(t) for t in parsed['terms']]
    for n in needles:
        m = re.search(n, text, re.I)
        if m:
            a, b = max(0, m.start() - width), min(len(text), m.end() + width)
            return (('…' if a else '') + text[a:m.start()] + '\x01' + text[m.start():m.end()] + '\x02'
                    + text[m.end():b] + ('…' if b < len(text) else ''))
    return text[:width * 2] + ('…' if len(text) > width * 2 else '')


def _filters_sql(parsed, hide_deleted, since, in_path):
    clauses, args = [], []
    if hide_deleted:
        clauses.append('i.deleted_at IS NULL')
    cutoff = parse_since(since or parsed['since']) if (since or parsed['since']) else None
    if cutoff is not None:
        clauses.append('i.mtime_ns >= ?')
        args.append(int(cutoff * 1e9))
    folder = in_path or parsed['in']
    if folder:
        lo, hi = scope_range(norm_path(folder))
        clauses.append('i.path >= ? AND i.path < ?')
        args += [lo, hi]
    return (' AND ' + ' AND '.join(clauses)) if clauses else '', args


def search(conn, raw, *, fuzzy=None, threshold=None, hide_deleted=False, since=None, in_path=None, cap=None,
           settings=None, sort=None):
    """Ranked results: phrase > near > all words > substring > fuzzy. Returns a list of dicts.

    cap (default: the max_results setting) limits the total; 0 or negative means unlimited.
    sort (default: the default_sort setting) reorders the collected results; see SORT_MODES."""
    settings = settings or load_settings(conn)
    sort = sort or settings['default_sort']
    cap = settings["max_results"] if cap is None else cap
    sql_cap = cap if cap and cap > 0 else -1     # SQLite: LIMIT -1 = no limit
    fuzzy = settings['fuzzy'] if fuzzy is None else fuzzy
    threshold = settings['fuzzy_threshold'] if threshold is None else threshold
    parsed = parse_query(raw)
    where, fargs = _filters_sql(parsed, hide_deleted, since, in_path)
    results, found = [], set()

    def add(rows, tier, score=None):
        for r in rows:
            if r['id'] in found:
                continue
            found.add(r['id'])
            d = dict(r)
            d.pop('rank', None)
            d.pop('norm', None)
            d['tier'], d['score'] = tier, score
            d['snippet'] = make_snippet(d['text'], parsed) if parsed['plain'] else d['text'][:120]
            results.append(d)

    if not parsed['plain'] and not parsed['terms'] and not parsed['phrases']:
        rows = conn.execute(f"SELECT i.* FROM images i WHERE i.text != ''{where} ORDER BY i.mtime_ns DESC LIMIT ?",
                            fargs + [sql_cap]).fetchall()
        add(rows, 'match')
        return _finish(results, sort, cap, sql_cap)

    for tier, expr in (build_tiers(parsed) if (parsed['terms'] or parsed['phrases']) else []):
        rows = conn.execute(
            'SELECT i.*, bm25(images_fts, 1.0, 0.4) AS rank FROM images_fts JOIN images i ON i.id = images_fts.rowid '
            f'WHERE images_fts MATCH ?{where} ORDER BY rank, i.mtime_ns DESC LIMIT ?',
            [expr] + fargs + [sql_cap]).fetchall()
        add(rows, tier)

    if parsed['plain'] and (sql_cap < 0 or len(results) < sql_cap):
        rows = conn.execute(
            f"SELECT i.* FROM images i WHERE i.text != '' AND instr(lower(i.text), ?) > 0{where} "
            'ORDER BY i.mtime_ns DESC LIMIT ?', [parsed['plain']] + fargs + [sql_cap]).fetchall()
        add(rows, 'substring')

    qn = norm_text(parsed['plain'])
    if fuzzy and len(qn) >= 4:
        from rapidfuzz import fuzz, process
        rows = conn.execute(f"SELECT i.* FROM images i WHERE i.text != ''{where}", fargs).fetchall()
        by_id = {r['id']: r for r in rows if r['id'] not in found}
        choices = {}
        for i, r in by_id.items():
            t = r['norm'] or ''
            if len(t) >= len(qn):     # a haystack shorter than the query would match by being a piece of it
                choices[i] = t
        for _, score, rid in process.extract(qn, choices, scorer=fuzz.partial_ratio, score_cutoff=threshold,
                                             limit=settings['fuzzy_max'] or None):
            add([by_id[rid]], 'fuzzy', int(score))

    results.sort(key=lambda d: (TIER_ORDER[d['tier']], -(d['score'] or 0)))  # stable: bm25 order kept within a tier
    return _finish(results, sort, cap, sql_cap)


def _finish(results, sort, cap, sql_cap):
    if sql_cap > 0:
        results = results[:cap]      # the cap always keeps the best matches, whatever the display order
    for n, d in enumerate(results):
        d['rank_pos'] = n
    return sort_results(results, sort)


def resolve_sort(value):
    """Accept a mode name, a unique prefix, or its 1-based number; returns the mode or None."""
    v = str(value).strip().lower()
    modes = list(SORT_MODES)
    if v.isdigit():
        return modes[int(v) - 1] if 1 <= int(v) <= len(modes) else None
    hits = [m for m in modes if m.startswith(v)] if v else []
    return hits[0] if len(hits) == 1 else (v if v in SORT_MODES else None)


def sort_results(results, mode):
    """Sort in place. Every mode is stable and falls back to relevance order on ties."""
    key = {
        'newest': lambda d: -(d.get('mtime_ns') or 0),
        'oldest': lambda d: d.get('mtime_ns') or 0,
        'name': lambda d: d['filename'].lower(),
        'path': lambda d: (os.path.dirname(d['path']).lower(), d['filename'].lower()),
        'largest': lambda d: -(d.get('size') or 0),
        'smallest': lambda d: d.get('size') or 0,
    }.get(mode, lambda d: d['rank_pos'])
    results.sort(key=lambda d: (key(d), d['rank_pos']))
    return results


def check_alive(conn, results, now=None):
    """Verify files on disk for these results; mark vanished ones deleted and tell where moved files went."""
    now = now or time.time()
    for r in results:
        if r['deleted_at'] is None and not os.path.exists(r['path']):
            r['deleted_at'] = now
            conn.execute('UPDATE images SET deleted_at=? WHERE id=? AND deleted_at IS NULL', (now, r['id']))
        r['moved_to'] = None
        if r['deleted_at'] is not None and r['hash']:
            row = conn.execute('SELECT path FROM images WHERE hash=? AND deleted_at IS NULL AND id!=? LIMIT 1',
                               (r['hash'], r['id'])).fetchone()
            if row and os.path.exists(row['path']):
                r['moved_to'] = row['path']
    conn.commit()
    return results


def format_result(i, r, color, now=None):
    now = now or time.time()
    b, dim, red, yel, end = ('\033[1m', '\033[2m', '\033[31m', '\033[33m', '\033[0m') if color else ('',) * 5
    tier = TIER_LABEL[r['tier']] + (f" {r['score']}%" if r['tier'] == 'fuzzy' else '')
    when = ''
    if r.get('mtime_ns'):
        when = datetime.datetime.fromtimestamp(r['mtime_ns'] / 1e9).strftime('%Y-%m-%d')
    where = short_path(os.path.dirname(r['path']))
    if r['source'] == 'cache':
        where = 'browser cache: ' + where
    lines = [f"{b}[{i}]{end} {r['filename']}  {yel}[{tier}]{end}  {dim}{where}{'  ' + when if when else ''}{end}"]
    if r['deleted_at'] is not None:
        ago = fmt_ago(now - r['deleted_at'])
        extra = f"  {dim}moved to {short_path(r['moved_to'])}{end}" if r.get('moved_to') else ''
        lines.append(f"    {red}DELETED - file no longer on disk (detected {ago}){end}{extra}")
    snip = r['snippet'].replace('\x01', b + yel if color else '').replace('\x02', end if color else '')
    lines.append(f'    {snip}')
    return lines


# ----------------------------------------------------------------------------
# Opening files, clipboard
# ----------------------------------------------------------------------------

def open_path(path):
    """Open a file or folder with the OS handler. Returns (ok, message)."""
    try:
        system = platform.system()
        if system == 'Windows':
            os.startfile(path)  # noqa: S606
        elif system == 'Darwin':
            subprocess.run(['open', path], check=True, timeout=15)
        else:
            if not shutil.which('xdg-open'):
                return False, 'xdg-open is not installed; open the path manually.'
            subprocess.run(['xdg-open', path], check=True, timeout=15, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        return True, ''
    except subprocess.TimeoutExpired:
        return True, ''
    except Exception as e:  # noqa: BLE001
        return False, f'Could not open: {e}'


def open_result(r):
    """Open a search result's image. Handles deleted files and browser-cache entries."""
    if r['deleted_at'] is not None:
        return False, 'That file is deleted. The indexed text is still available (use t<number>).'
    if not os.path.exists(r['path']):
        return False, 'That file is no longer on disk.'
    path = r['path']
    if r['source'] == 'cache':
        try:
            with Image.open(io.BytesIO(open(path, 'rb').read()[r['data_offset']:])) as im:
                ext = {'JPEG': 'jpg', 'PNG': 'png', 'GIF': 'gif', 'WEBP': 'webp'}.get(im.format, 'png')
                out = os.path.join(tempfile.gettempdir(), f"mirrorwalk-{r['hash'] or r['id']}.{ext}")
                im.save(out)
            path = out
        except Exception as e:  # noqa: BLE001
            return False, f'Could not decode the cached image: {e}'
    return open_path(path)


def write_text_view(r, now=None):
    """Write a result's full OCR text to a temp .txt file (with a short header) and return its path.
    Works for deleted files too, since the text stays in the index."""
    now = now or time.time()
    state = 'present'
    if r['deleted_at'] is not None:
        state = f"DELETED (detected {fmt_ago(now - r['deleted_at'])})"
    out = os.path.join(tempfile.gettempdir(), f"mirrorwalk-text-{r['id']}.txt")
    with open(out, 'w', encoding='utf-8') as f:
        f.write(f"File:   {r['filename']}\nPath:   {r['path']}\nStatus: {state}\n{'-' * 60}\n\n")
        f.write((r['text'] or '(no text found)') + '\n')
    return out


def open_text(r):
    """Open the full OCR text of a result in the system's default text viewer."""
    try:
        return open_path(write_text_view(r))
    except OSError as e:
        return False, f'Could not write the text file: {e}'


def copy_to_clipboard(text):
    for cmd in (['pbcopy'], ['wl-copy'], ['xclip', '-selection', 'clipboard'], ['xsel', '-b', '-i'], ['clip']):
        if shutil.which(cmd[0]):
            try:
                subprocess.run(cmd, input=text.encode(), check=True, timeout=5)
                return True
            except Exception:  # noqa: BLE001
                continue
    return False


# ----------------------------------------------------------------------------
# Status, export
# ----------------------------------------------------------------------------

def index_status(conn, settings):
    q = lambda sql, *a: conn.execute(sql, a).fetchone()[0]  # noqa: E731
    path = db_file(conn)
    info = {
        'db': path, 'db_size': os.path.getsize(path) if os.path.exists(path) else 0,
        'live': q("SELECT COUNT(*) FROM images WHERE deleted_at IS NULL AND status='ok'"),
        'with_text': q("SELECT COUNT(*) FROM images WHERE deleted_at IS NULL AND status='ok' AND text!=''"),
        'deleted': q('SELECT COUNT(*) FROM images WHERE deleted_at IS NOT NULL'),
        'skipped': q("SELECT COUNT(*) FROM images WHERE status='skipped'"),
        'errors': q("SELECT COUNT(*) FROM images WHERE status='error'"),
        'cache': q("SELECT COUNT(*) FROM images WHERE source='cache' AND status='ok'"),
        'roots': [dict(r) for r in conn.execute("SELECT * FROM roots WHERE kind='file' ORDER BY path")],
        'blocked': sum(len(v) for v in find_prune_candidates(conn, settings, blocked=True).values()),
    }
    for r in info['roots']:
        lo, hi = scope_range(r['path'])
        r['count'] = q("SELECT COUNT(*) FROM images WHERE source='file' AND path>=? AND path<? AND deleted_at IS NULL", lo, hi)
    return info


def print_status(conn, settings):
    s = index_status(conn, settings)
    ok, version, hint = check_tesseract(settings['lang'])
    print(f"Index:      {short_path(s['db'])}  ({human_bytes(s['db_size'])})")
    print(f"Images:     {s['live']:,} indexed ({s['with_text']:,} with text) | {s['deleted']:,} deleted | "
          f"{s['skipped']:,} skipped | {s['errors']:,} failed")
    if s['cache']:
        print(f"Browser cache images: {s['cache']:,}")
    if s['roots']:
        print('Folders:')
        for r in s['roots']:
            ago = fmt_ago(time.time() - r['last_indexed']) if r['last_indexed'] else 'never'
            print(f"  {short_path(r['path'])}  ({'recursive' if r['recursive'] else 'top-level'}, "
                  f"{r['count']:,} images, refreshed {ago})")
    else:
        print('Folders:    none registered (run: mirrorwalk.py setup, or index a folder)')
    print(f"Settings:   auto-refresh {'on' if settings['auto_refresh'] else 'off'} | "
          f"browser cache {'on' if settings['include_browser_cache'] else 'off'} | "
          f"fuzzy {'on' if settings['fuzzy'] else 'off'} | language {settings['lang']}")
    print(f"Tesseract:  {version if ok or version else 'NOT FOUND'}" + (f'  ! {hint}' if hint else ''))
    if s['blocked']:
        print(f"Noise:      {s['blocked']:,} records come from folders that are now ignored "
              f"-> mirrorwalk.py prune --blocked")
    if s['deleted']:
        print('Deleted:    run `mirrorwalk.py status --deleted` to list them')


def print_deleted(conn, limit=50, now=None):
    now = now or time.time()
    rows = conn.execute('SELECT * FROM images WHERE deleted_at IS NOT NULL ORDER BY deleted_at DESC LIMIT ?',
                        (limit,)).fetchall()
    if not rows:
        print('No deleted files recorded.')
        return
    for r in rows:
        print(f"{r['filename']}  -  detected deleted {fmt_ago(now - r['deleted_at'])}  ({short_path(os.path.dirname(r['path']))})")


def export_index(conn, path, fmt=None):
    fmt = fmt or ('json' if path.lower().endswith('.json') else 'csv')
    rows = conn.execute('SELECT * FROM images WHERE text != "" ORDER BY path').fetchall()
    cols = ['filename', 'path', 'text', 'status', 'source', 'deleted_at', 'indexed_at']
    with open(path, 'w', encoding='utf-8', newline='') as f:
        if fmt == 'json':
            json.dump([{c: r[c] for c in cols} for r in rows], f, indent=2)
        else:
            w = csv.writer(f)
            w.writerow(['Filename', 'Path', 'Extracted Text', 'Status', 'Source', 'Deleted At', 'Indexed At'])
            for r in rows:
                w.writerow([r[c] for c in cols])
    return len(rows)


# ----------------------------------------------------------------------------
# Autostart and setup
# ----------------------------------------------------------------------------

def refresh_command(db=None):
    script = os.path.abspath(__file__)
    cmd = [sys.executable, script]
    if db:
        cmd += ['--db', db]
    return cmd + ['refresh', '--quiet']


def autostart_target():
    system = platform.system()
    if system == 'Darwin':
        return os.path.expanduser('~/Library/LaunchAgents/com.mirrorwalk.refresh.plist')
    if system == 'Windows':
        return 'task:MirrorWalkRefresh'
    return os.path.join(os.environ.get('XDG_CONFIG_HOME') or os.path.expanduser('~/.config'), 'autostart',
                        'mirrorwalk-refresh.desktop')


def autostart_content(cmd, system=None):
    system = system or platform.system()
    if system == 'Darwin':
        args = ''.join(f'        <string>{a}</string>\n' for a in cmd)
        return ('<?xml version="1.0" encoding="UTF-8"?>\n'
                '<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">\n'
                '<plist version="1.0">\n<dict>\n    <key>Label</key><string>com.mirrorwalk.refresh</string>\n'
                f'    <key>ProgramArguments</key>\n    <array>\n{args}    </array>\n'
                '    <key>RunAtLoad</key><true/>\n    <key>Nice</key><integer>19</integer>\n'
                '    <key>LowPriorityIO</key><true/>\n</dict>\n</plist>\n')
    quoted = ' '.join('"' + a.replace('"', '\\"') + '"' if (' ' in a) else a for a in cmd)
    return ('[Desktop Entry]\nType=Application\nName=MirrorWalk index refresh\n'
            f'Exec=nice -n 19 {quoted}\nTerminal=false\nX-GNOME-Autostart-enabled=true\n')


def install_autostart(db=None):
    """Run a quiet refresh at every login. Returns a description of what was installed."""
    cmd = refresh_command(db)
    system = platform.system()
    target = autostart_target()
    if system == 'Windows':
        exe = cmd[0]
        pyw = os.path.join(os.path.dirname(exe), 'pythonw.exe')
        if os.path.exists(pyw):
            cmd[0] = pyw
        line = ' '.join(f'"{a}"' for a in cmd)
        subprocess.run(['schtasks', '/Create', '/F', '/SC', 'ONLOGON', '/TN', 'MirrorWalkRefresh', '/TR', line],
                       check=True, capture_output=True)
        return 'Scheduled task "MirrorWalkRefresh" (runs at logon)'
    os.makedirs(os.path.dirname(target), exist_ok=True)
    with open(target, 'w', encoding='utf-8') as f:
        f.write(autostart_content(cmd, system))
    return target


def remove_autostart():
    system = platform.system()
    if system == 'Windows':
        r = subprocess.run(['schtasks', '/Delete', '/F', '/TN', 'MirrorWalkRefresh'], capture_output=True)
        return r.returncode == 0
    target = autostart_target()
    if os.path.exists(target):
        os.remove(target)
        return True
    return False


def find_legacy_json():
    for base in (os.getcwd(), os.path.dirname(os.path.abspath(__file__))):
        p = os.path.join(base, 'results.json')
        if os.path.isfile(p):
            return p
    return None


def run_setup(conn, args):
    settings = load_settings(conn)
    interactive = sys.stdin.isatty() and not args.yes
    ask_yes = (lambda q, d=True: yes_no(q, d)) if interactive else (lambda q, d=True: d)
    print(f'MirrorWalk setup - index stored at {short_path(db_file(conn))}\n')

    ok, version, hint = check_tesseract(settings['lang'])
    if version:
        print(f'[ok] Tesseract {version}')
    if hint:
        print(f'[!!] {hint}')
        if not version:
            print('     Install it, then run setup again. Your settings so far are saved.')

    legacy = find_legacy_json()
    if legacy and conn.execute('SELECT COUNT(*) FROM images').fetchone()[0] == 0:
        if ask_yes(f'Found an old index ({short_path(legacy)}). Import it (ignored folders are dropped, file is kept)?'):
            st = import_legacy(conn, legacy, settings)
            print(f"[ok] Imported {st['imported']:,} images, {st['missing']:,} deleted ones kept as history, "
                  f"dropped {st['blocked']:,} from ignored folders.")

    defaults = default_dirs()
    chosen = []
    if defaults:
        print('\nFolders where your pictures and screenshots usually live:')
        for label, path in defaults:
            if ask_yes(f'  Index {short_path(path)}?', label in ('Pictures', 'Desktop')):
                chosen.append(path)
    else:
        print('\nNo standard picture folders found. Add one with: mirrorwalk.py index PATH')

    settings = save_setting(conn, 'auto_refresh', True)
    if args.cache or (interactive and ask_yes('\nAlso index images from browser caches? (finds images you only saw online)', False)):
        settings = save_setting(conn, 'include_browser_cache', True)
        print('[ok] Browser cache indexing on.')

    for p in chosen:
        conn.execute('INSERT OR IGNORE INTO roots(path,kind,recursive,added_at) VALUES(?,?,?,?)', (p, 'file', 1, time.time()))
    conn.commit()

    if args.autostart or (interactive and ask_yes('\nRefresh the index automatically at every login?', False)):
        try:
            where = install_autostart(db_file(conn) if os.environ.get('MIRRORWALK_DB') or args.db else None)
            print(f'[ok] Login refresh installed: {short_path(where)}')
        except Exception as e:  # noqa: BLE001
            print(f'[!!] Could not install login refresh: {e}')

    if version and not args.no_index and (chosen or settings['include_browser_cache']):
        if ask_yes('\nIndex now? (the first run reads every image; later runs only read new ones)'):
            refresh_all(conn, settings)
    print('\nDone. Run `mirrorwalk.py` for the menu or `mirrorwalk.py search "words"`.')
    return 0


# ----------------------------------------------------------------------------
# Interactive menu
# ----------------------------------------------------------------------------

def show_results(conn, results, settings, raw):
    color = use_color()
    sort = settings['default_sort']
    sort_results(results, sort)
    page_size = settings['page_size'] if settings['page_size'] > 0 else max(1, len(results))
    page = 0
    pages = (len(results) + page_size - 1) // page_size
    while True:
        start = page * page_size
        shown = check_alive(conn, results[start:start + page_size])
        tiers = Counter(TIER_LABEL[r['tier']] for r in results)
        print(f"\n--- {len(results)} result(s) for: {raw}  [{', '.join(f'{v} {k}' for k, v in tiers.items())}] "
              f"page {page + 1}/{pages}  sorted by {sort} ---")
        for i, r in enumerate(shown, start + 1):
            for line in format_result(i, r, color):
                print(line)
        print('-' * 30)
        print('N open image | v N open full text | t N print text | f N folder | c N copy path | n/p page | s change sort | e export | Enter new search')
        choice = ask('>>> ').lower()
        if not choice:
            return
        m = re.fullmatch(r'([fctv]?)\s*(\d+)', choice)
        if choice == 'n':
            page = min(page + 1, pages - 1)
        elif choice == 'p':
            page = max(page - 1, 0)
        elif choice == 's' or choice.startswith('s '):
            wanted = choice[1:].strip()
            if not wanted:
                print('\nSort results by:')
                for n, (name, desc) in enumerate(SORT_MODES.items(), 1):
                    print(f"  {n}) {name:<9} {desc}{'   <- current' if name == sort else ''}")
                wanted = ask('Number or name, Enter to keep >>> ')
            if wanted:
                mode = resolve_sort(wanted)
                if mode:
                    sort = mode
                    sort_results(results, sort)
                    page = 0
                else:
                    print(f"Unknown sort. Choose: {', '.join(SORT_MODES)}")
        elif choice == 'e':
            with open('search_results.txt', 'w', encoding='utf-8') as ef:
                ef.write(f'Search results for: {raw}\n' + '=' * 40 + '\n')
                for r in results:
                    state = '' if r['deleted_at'] is None else f" (deleted, detected {fmt_ago(time.time() - r['deleted_at'])})"
                    ef.write(f"File: {r['filename']}{state}\nPath: {r['path']}\nText: {r['text']}\n" + '-' * 20 + '\n')
            print('Exported to search_results.txt')
        elif m and 1 <= int(m.group(2)) <= len(results):
            r = results[int(m.group(2)) - 1]
            check_alive(conn, [r])
            kind = m.group(1)
            if kind == 't':
                print(f"\n{r['filename']}:\n{r['text']}\n")
            elif kind == 'v':
                ok, msg = open_text(r)
                print(msg if not ok else f"Opened the full text of {r['filename']}")
            elif kind == 'c':
                print('Copied.' if copy_to_clipboard(r['path']) else f"No clipboard tool found. Path: {r['path']}")
            elif kind == 'f':
                ok, msg = open_path(os.path.dirname(r['path'])) if os.path.isdir(os.path.dirname(r['path'])) else (False, 'Folder is gone.')
                print(msg if not ok else f"Opened {short_path(os.path.dirname(r['path']))}")
            else:
                ok, msg = open_result(r)
                print(msg if not ok else f"Opening {r['filename']}...")
        else:
            print('Not a valid choice.')


def interactive_search(conn, settings):
    print('\nTip: "quotes" find an exact phrase; since:7d and in:~/Pictures narrow results. '
          'Words typed without quotes rank phrase matches first.')
    while True:
        raw = ask('\nSearch (Enter to go back) >>> ')
        if not raw:
            return
        try:
            results = search(conn, raw, settings=settings)
        except (MirrorWalkError, sqlite3.OperationalError) as e:
            print(f'Search problem: {e}')
            continue
        if not results:
            print('No matches.' + ('' if settings['fuzzy'] else ' (Fuzzy matching is off: mirrorwalk.py settings fuzzy on)'))
            continue
        show_results(conn, results, settings, raw)


def choose_folders(conn):
    roots = [r['path'] for r in conn.execute("SELECT path FROM roots WHERE kind='file' ORDER BY path")]
    defaults = [p for _, p in default_dirs() if p not in roots]
    options = roots + defaults
    print('\nWhich folder?')
    for i, p in enumerate(options, 1):
        print(f"  {i}) {short_path(p)}{'  (indexed)' if p in roots else ''}")
    print('  a) all of the above   p) type a path   Enter) back')
    c = ask('>>> ').lower()
    if c == 'a':
        return options, True
    if c == 'p':
        p = ask('Folder path >>> ')
        return ([norm_path(p)] if p else []), yes_no('Include subfolders?', True)
    if c.isdigit() and 1 <= int(c) <= len(options):
        return [options[int(c) - 1]], True
    return [], True


def health_menu(conn, settings):
    while True:
        print()
        print_status(conn, settings)
        c = ask('\n[d] list deleted files  [p] prune  [r] retry failed images  Enter) back >>> ').lower()
        if c == 'd':
            print_deleted(conn)
        elif c == 'r':
            refresh_all(conn, settings, retry_errors=True)
        elif c == 'p':
            cands = find_prune_candidates(conn, settings, blocked=True)
            n_blocked = len(cands['blocked'])
            n_deleted = conn.execute('SELECT COUNT(*) FROM images WHERE deleted_at IS NOT NULL').fetchone()[0]
            print(f'Records from ignored folders: {n_blocked:,}\nDeleted-file records (history): {n_deleted:,}')
            if n_blocked and yes_no('Remove records from ignored folders?', True):
                print(f'Removed {apply_prune(conn, cands):,}.')
            if n_deleted and yes_no('Also forget ALL deleted-file history? (you lose the ability to see what they said)', False):
                print(f"Removed {apply_prune(conn, find_prune_candidates(conn, settings, deleted=True)):,}.")
        else:
            return


def settings_menu(conn):
    while True:
        settings = load_settings(conn)
        keys = list(DEFAULT_SETTINGS)
        print('\nSettings:')
        for i, k in enumerate(keys, 1):
            v = settings[k]
            v = ('on' if v else 'off') if isinstance(v, bool) else (', '.join(v) or '-') if isinstance(v, list) else v
            print(f'  {i}) {k}: {v}')
        c = ask('Number to change, Enter to go back >>> ')
        if not c.isdigit() or not 1 <= int(c) <= len(keys):
            return
        key = keys[int(c) - 1]
        val = ask(f'New value for {key} >>> ')
        if val:
            try:
                new = save_setting(conn, key, coerce_setting(key, val))
                if key == 'include_browser_cache' and not new[key]:
                    print('Cache indexing off. Existing cache records stay until: mirrorwalk.py prune --blocked')
            except MirrorWalkError as e:
                print(e)


def interactive(conn, args):
    settings = load_settings(conn)
    ok, version, hint = check_tesseract(settings['lang'])
    if hint:
        print(f'\n[!] {hint}')
    legacy = find_legacy_json()
    if legacy and conn.execute('SELECT COUNT(*) FROM images').fetchone()[0] == 0:
        if yes_no(f'Found an old index at {short_path(legacy)}. Import it? (ignored folders are dropped; the file is kept)'):
            st = import_legacy(conn, legacy, settings)
            print(f"Imported {st['imported']:,} images; kept {st['missing']:,} deleted ones as history; "
                  f"dropped {st['blocked']:,} from ignored folders.")
    if settings['auto_refresh'] and not args.no_refresh and ok:
        has_roots = conn.execute('SELECT 1 FROM roots LIMIT 1').fetchone() or settings['include_browser_cache']
        if has_roots:
            print('\nRefreshing your indexed folders (Ctrl+C to skip)...')
            refresh_all(conn, settings)
    elif not conn.execute('SELECT 1 FROM roots LIMIT 1').fetchone():
        print('\nFirst time here? `mirrorwalk.py setup` finds your picture folders and configures everything.')
    while True:
        settings = load_settings(conn)
        print(f'\n--- MIRRORWALK {__version__} ---')
        print('1. Index a folder\n2. Refresh all folders\n3. Search\n4. Index health (deleted files, cleanup)\n'
              '5. Settings\n6. Exit')
        c = ask('Select (1-6): ')
        if c == '1':
            paths, rec = choose_folders(conn)
            if paths:
                index_paths(conn, paths, settings, recursive=rec)
        elif c == '2':
            refresh_all(conn, settings)
        elif c == '3':
            interactive_search(conn, settings)
        elif c == '4':
            health_menu(conn, settings)
        elif c == '5':
            settings_menu(conn)
        elif c == '6':
            return 0


# ----------------------------------------------------------------------------
# Command line
# ----------------------------------------------------------------------------

def build_parser():
    p = argparse.ArgumentParser(prog='mirrorwalk', description='Offline OCR search for your images.')
    p.add_argument('--db', help='index database path (default: per-user data folder, or $MIRRORWALK_DB)')
    p.add_argument('--no-refresh', action='store_true', help='skip the automatic refresh when the menu starts')
    p.add_argument('--version', action='version', version=f'mirrorwalk {__version__}')
    sub = p.add_subparsers(dest='cmd')

    s = sub.add_parser('setup', help='first-run setup: checks, default folders, optional login refresh')
    s.add_argument('--yes', action='store_true', help='accept the defaults without asking')
    s.add_argument('--autostart', action='store_true', help='refresh the index at every login')
    s.add_argument('--remove-autostart', action='store_true', help='remove the login refresh')
    s.add_argument('--cache', action='store_true', help='also index browser caches')
    s.add_argument('--no-index', action='store_true', help="don't index at the end")

    s = sub.add_parser('index', help='index one or more folders')
    s.add_argument('paths', nargs='+')
    s.add_argument('--flat', action='store_true', help='top-level only, no subfolders')
    s.add_argument('--quiet', action='store_true')

    s = sub.add_parser('refresh', help='refresh every indexed folder')
    s.add_argument('--quiet', action='store_true')
    s.add_argument('--retry-errors', action='store_true', help='retry images that failed before')

    s = sub.add_parser('search', help='search the index')
    s.add_argument('query', nargs='+', help='words; "quoted phrase"; since:7d; in:~/Pictures')
    s.add_argument('--limit', type=int, help='results to show (default: page_size setting; 0 = all)')
    s.add_argument('--all', action='store_true', help='show every result')
    s.add_argument('--fuzzy', action=argparse.BooleanOptionalAction, default=None)
    s.add_argument('--threshold', type=int)
    s.add_argument('--since')
    s.add_argument('--in', dest='in_path')
    s.add_argument('--hide-deleted', action='store_true')
    s.add_argument('--sort', choices=list(SORT_MODES), help='result order (default: the default_sort setting)')
    s.add_argument('--json', action='store_true')
    s.add_argument('--open', type=int, metavar='N', help='open the image of result number N')
    s.add_argument('--open-text', type=int, metavar='N', help='open the full OCR text of result number N')

    s = sub.add_parser('roots', help='list, add or remove indexed folders')
    s.add_argument('action', nargs='?', choices=['list', 'add', 'remove'], default='list')
    s.add_argument('path', nargs='?')
    s.add_argument('--flat', action='store_true')
    s.add_argument('--purge', action='store_true', help='with remove: also delete its records')

    s = sub.add_parser('defaults', help='show (or --add) the standard picture folders')
    s.add_argument('--add', action='store_true')

    s = sub.add_parser('settings', help='show or change settings: settings [KEY [VALUE]]')
    s.add_argument('key', nargs='?')
    s.add_argument('value', nargs='?')

    s = sub.add_parser('prune', help='remove records you no longer want')
    s.add_argument('--blocked', action='store_true', help='records from ignored folders (and caches if disabled)')
    s.add_argument('--deleted', action='store_true', help='deleted-file history')
    s.add_argument('--older-than', help='with --deleted: only history older than e.g. 30d')
    s.add_argument('--errors', action='store_true', help='failed-image records')
    s.add_argument('--dry-run', action='store_true')
    s.add_argument('--yes', action='store_true')

    s = sub.add_parser('status', help='what is indexed, deleted, failed')
    s.add_argument('--deleted', action='store_true', help='list deleted files')

    sub.add_parser('errors', help='list images that failed to index')

    s = sub.add_parser('export', help='export the index to CSV or JSON')
    s.add_argument('path')

    s = sub.add_parser('import-legacy', help='import a v1 results.json')
    s.add_argument('path', nargs='?')
    return p


def cmd_search(conn, args):
    settings = load_settings(conn)
    raw = ' '.join(args.query)
    results = search(conn, raw, fuzzy=args.fuzzy, threshold=args.threshold, hide_deleted=args.hide_deleted,
                     since=args.since, in_path=args.in_path, settings=settings, sort=args.sort)
    limit = args.limit if args.limit is not None else settings['page_size']
    limit = None if (args.all or limit <= 0) else limit
    shown = check_alive(conn, results[:limit] if limit else results)
    if args.json:
        now = time.time()
        print(json.dumps([{
            'filename': r['filename'], 'path': r['path'], 'match': r['tier'], 'score': r['score'],
            'snippet': r['snippet'].replace('\x01', '').replace('\x02', ''),
            'deleted': r['deleted_at'] is not None,
            'deleted_detected_ago': fmt_ago(now - r['deleted_at']) if r['deleted_at'] is not None else None,
            'moved_to': r.get('moved_to'), 'source': r['source']} for r in shown], indent=2))
    else:
        color = use_color()
        for i, r in enumerate(shown, 1):
            print('\n'.join(format_result(i, r, color)))
        if not shown:
            print('No matches.')
        elif len(results) > len(shown):
            print(f'\n... {len(results) - len(shown)} more. Use --all or --limit N.')
    if args.open and 1 <= args.open <= len(shown):
        ok, msg = open_result(shown[args.open - 1])
        print(msg if not ok else f"Opening {shown[args.open - 1]['filename']}...")
    if args.open_text and 1 <= args.open_text <= len(shown):
        ok, msg = open_text(shown[args.open_text - 1])
        print(msg if not ok else f"Opened the full text of {shown[args.open_text - 1]['filename']}")
    return 0 if shown else 1


def run(argv=None):
    args = build_parser().parse_args(argv)
    conn = connect(args.db)
    try:
        return dispatch(conn, args)
    finally:
        conn.close()


def dispatch(conn, args):
    settings = load_settings(conn)
    cmd = args.cmd

    if cmd is None:
        return interactive(conn, args)
    if cmd == 'setup':
        if args.remove_autostart:
            print('Login refresh removed.' if remove_autostart() else 'No login refresh was installed.')
            return 0
        return run_setup(conn, args)
    if cmd in ('index', 'refresh'):
        ok, _, hint = check_tesseract(settings['lang'])
        if not ok and hint:
            print(f'[!] {hint}', file=sys.stderr)
            if 'not found' in hint:
                return 2
        if cmd == 'index':
            missing = [p for p in args.paths if not os.path.isdir(norm_path(p))]
            if missing:
                print('Not a folder: ' + ', '.join(missing), file=sys.stderr)
                return 2
            index_paths(conn, args.paths, settings, recursive=not args.flat, quiet=args.quiet)
        else:
            refresh_all(conn, settings, quiet=args.quiet, retry_errors=args.retry_errors)
        return 0
    if cmd == 'search':
        return cmd_search(conn, args)
    if cmd == 'roots':
        if args.action == 'add':
            if not args.path or not os.path.isdir(norm_path(args.path)):
                print('Give an existing folder: mirrorwalk.py roots add PATH', file=sys.stderr)
                return 2
            conn.execute('INSERT INTO roots(path,kind,recursive,added_at) VALUES(?,?,?,?) '
                         'ON CONFLICT(path) DO UPDATE SET recursive=excluded.recursive',
                         (norm_path(args.path), 'file', 0 if args.flat else 1, time.time()))
            conn.commit()
            print(f'Added {short_path(norm_path(args.path))}. Run `refresh` to index it.')
        elif args.action == 'remove':
            p = norm_path(args.path or '')
            conn.execute('DELETE FROM roots WHERE path=?', (p,))
            if args.purge:
                lo, hi = scope_range(p)
                conn.execute('DELETE FROM images WHERE path>=? AND path<?', (lo, hi))
            conn.commit()
            print(f'Removed {short_path(p)}' + (' and its records.' if args.purge else ' (records kept).'))
        else:
            for r in index_status(conn, settings)['roots']:
                print(f"{short_path(r['path'])}  ({'recursive' if r['recursive'] else 'top-level'}, {r['count']:,} images)")
        return 0
    if cmd == 'defaults':
        for label, path in default_dirs():
            print(f'{label:10} {short_path(path)}')
            if args.add:
                conn.execute('INSERT OR IGNORE INTO roots(path,kind,recursive,added_at) VALUES(?,?,?,?)',
                             (path, 'file', 1, time.time()))
        conn.commit()
        if args.add:
            print('Registered. Run `refresh` to index them.')
        return 0
    if cmd == 'settings':
        if args.key and args.value is not None:
            try:
                new = save_setting(conn, args.key, coerce_setting(args.key, args.value))
            except MirrorWalkError as e:
                print(e, file=sys.stderr)
                return 2
            print(f'{args.key} = {new[args.key]}')
            if args.key == 'include_browser_cache' and not new[args.key]:
                print('Existing cache records stay until: mirrorwalk.py prune --blocked')
        elif args.key:
            if args.key not in settings:
                print(f"Unknown setting '{args.key}'", file=sys.stderr)
                return 2
            print(settings[args.key])
        else:
            for k, v in settings.items():
                print(f'{k} = {v}')
        return 0
    if cmd == 'prune':
        if not (args.blocked or args.deleted or args.errors):
            print('Choose what to prune: --blocked, --deleted [--older-than 30d], --errors', file=sys.stderr)
            return 2
        cands = find_prune_candidates(conn, settings, deleted=args.deleted, older_than=args.older_than,
                                      blocked=args.blocked, errors=args.errors)
        for k, v in cands.items():
            if v:
                print(f'{k}: {len(v):,} records')
        total = len({i for v in cands.values() for i in v})
        if not total:
            print('Nothing to prune.')
            return 0
        if args.dry_run:
            print(f'(dry run) would remove {total:,} records.')
            return 0
        if not args.yes and sys.stdin.isatty() and not yes_no(f'Remove {total:,} records?', False):
            print('Cancelled.')
            return 1
        if not args.yes and not sys.stdin.isatty():
            print('Refusing to prune without --yes when not interactive.', file=sys.stderr)
            return 2
        print(f'Removed {apply_prune(conn, cands):,} records.')
        return 0
    if cmd == 'status':
        if args.deleted:
            print_deleted(conn)
        else:
            print_status(conn, settings)
        return 0
    if cmd == 'errors':
        rows = conn.execute("SELECT * FROM images WHERE status='error' ORDER BY path").fetchall()
        for r in rows:
            print(f"{r['path']}\n    {r['note']}  (attempts: {r['attempts']})")
        print(f'{len(rows)} failed image(s).' + (' Retry with: mirrorwalk.py refresh --retry-errors' if rows else ''))
        return 0
    if cmd == 'export':
        print(f'Exported {export_index(conn, args.path):,} records to {args.path}')
        return 0
    if cmd == 'import-legacy':
        path = args.path or find_legacy_json()
        if not path:
            print('No results.json found. Pass its path.', file=sys.stderr)
            return 2
        st = import_legacy(conn, path, settings)
        print(f"Imported {st['imported']:,}, deleted-history {st['missing']:,}, dropped (ignored folders) "
              f"{st['blocked']:,}, dropped (missing and empty) {st['dropped_empty_missing']:,}.")
        return 0
    return 0


def main(argv=None):
    try:
        return run(argv)
    except MirrorWalkError as e:
        print(f'Error: {e}', file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print('\nInterrupted.')
        return 130
    except EOFError:
        print('\nInput closed.')
        return 0


if __name__ == '__main__':
    sys.exit(main())
