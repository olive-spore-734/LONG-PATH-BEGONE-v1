# SourceCode.md — Long Path Begone
### Every part of the code explained in plain English

This document walks through `long_path_begone.py` from the very first line to
the last, explaining **what each section does and why**. No programming
background assumed.

---

## Table of contents

1. [The file header](#1-the-file-header)
2. [Imports — tools the program borrows](#2-imports)
3. [Constants — fixed values](#3-constants)
4. [Win32 bindings — talking to Windows directly](#4-win32-bindings)
5. [Path helpers — working with long paths](#5-path-helpers)
6. [Filesystem primitives — the actual file operations](#6-filesystem-primitives)
7. [Recursive helpers — walking folder trees](#7-recursive-helpers)
8. [UI theming — colours, fonts, styles](#8-ui-theming)
9. [Pure logic helpers — testable module-level functions](#9-pure-logic-helpers)
10. [App class overview](#10-app-class-overview)
11. [Titlebar and navigation](#11-titlebar-and-navigation)
12. [Transfer page](#12-transfer-page)
13. [Scan page](#13-scan-page)
14. [Find and Replace](#14-find-and-replace)
15. [Apply Renames](#15-apply-renames)
16. [Logs tab](#16-logs-tab)
17. [Settings modal](#17-settings-modal)
18. [Entry point — starting the app](#18-entry-point)

---

## 1. The file header

```python
r"""
Long Path Begone — Windows utility for files/folders with paths over MAX_PATH.
...
"""
```

The triple-quoted block at the top is a **docstring** — a plain-English
description of what the file does. The `r` before the quotes means "raw
string": backslashes inside it are treated literally rather than as escape
codes. This has no effect at runtime; it's purely documentation.

---

## 2. Imports

```python
from __future__ import annotations
import ctypes
import json
import os
import queue
import re
import threading
import datetime
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import filedialog, messagebox, ttk
import tkinter.font as tkfont
```

Python can't do everything on its own — it borrows pre-built modules (called
the **standard library**) to avoid reinventing the wheel. `import X` loads
module X so the code can use it.

| Import | What it does |
|---|---|
| `from __future__ import annotations` | Lets type hints (like `str \| None`) work on older Python 3.10 before they became native. |
| `ctypes` | Lets Python call functions in Windows DLLs (like `kernel32.dll`) as if they were Python functions. |
| `json` | Reads and writes JSON files — used for saving settings. |
| `os` | Operating system helpers: path manipulation, file checks. |
| `queue` | A thread-safe "in-box" so the background scan thread can send messages to the UI without crashing. |
| `re` | **Regular expressions** — the pattern-matching engine behind Find & Replace. |
| `threading` | Runs the scan and rename operations on a background thread so the UI doesn't freeze. |
| `datetime` / `time` | Used for timestamps in the activity log and rate-limiting progress updates. |
| `tkinter` | The GUI toolkit — windows, buttons, labels, tree views. |
| `wintypes` | Windows-specific C data types (`DWORD`, `BOOL`, `LPCWSTR` etc.) needed when calling Win32. |
| `filedialog`, `messagebox`, `ttk` | Sub-modules of tkinter: file picker dialogs, pop-up messages, and themed widgets. |
| `tkinter.font` (as `tkfont`) | Lets the code measure how wide a string is in pixels for a given font — used by column auto-fit. |

---

## 3. Constants

```python
EXT_PREFIX     = "\\\\?\\"
EXT_UNC_PREFIX = "\\\\?\\UNC\\"
```

Windows normally limits path lengths to 260 characters (the `MAX_PATH` limit
— a legacy from the DOS era). Prepending `\\?\` to any path tells Windows to
skip that check and allow paths up to ~32,767 characters. `\\?\UNC\` is the
same thing for network paths (which start with `\\server\share`).

The four backslashes in the Python source code produce two actual backslashes
when the string is used — this is just Python's way of writing a literal
backslash.

```python
def _darken(hex_color: str, factor: float = 0.82) -> str:
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"
```

A small utility: given a hex colour like `#7c5cfc`, multiply each colour
channel (red, green, blue) by 0.82 to make it slightly darker. Used to
generate button hover colours automatically from the accent colour, so every
accent theme gets matching hover states without extra work.

---

## 4. Win32 bindings

Windows exposes its low-level filesystem operations through a library called
**kernel32.dll**. Python's `ctypes` module can call into it — but you have to
tell Python exactly what types each function takes and returns, otherwise you
get crashes.

```python
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
```

Load `kernel32.dll` into Python. `use_last_error=True` means errors are stored
in a per-thread slot that Python can read with `ctypes.get_last_error()` —
important because Win32 functions signal failure via `GetLastError()`, not by
raising exceptions.

### Win32 constants

```python
INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF   # GetFileAttributesW returns this on failure
FILE_ATTRIBUTE_DIRECTORY = 0x10        # bit flag: "this is a folder"
FILE_ATTRIBUTE_READONLY  = 0x01        # bit flag: "this is read-only"
MOVEFILE_REPLACE_EXISTING = 0x1        # overwrite the destination if it exists
MOVEFILE_COPY_ALLOWED     = 0x2        # allow a copy+delete if src/dst are on different volumes
MOVEFILE_WRITE_THROUGH    = 0x8        # wait until the rename is flushed to disk
ERROR_ALREADY_EXISTS      = 183        # Win32 error code: "that folder already exists"
```

These are magic numbers from the Windows SDK. They're given names so the code
reads as English instead of a wall of unexplained numbers.

### Function prototypes

```python
_GetFileAttributesW = _k32.GetFileAttributesW
_GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_GetFileAttributesW.restype  = wintypes.DWORD
```

This pattern repeats for every Win32 function used. Reading it:

- `_k32.GetFileAttributesW` — fetch the function from the DLL.
- `.argtypes` — a list of the C types for each argument. `LPCWSTR` means
  "pointer to a wide (Unicode) string".
- `.restype` — the return type. `DWORD` is a 32-bit unsigned integer.

Without these declarations, ctypes would guess the types and likely crash or
return garbage.

The seven Win32 functions used are:

| Function | What it does |
|---|---|
| `GetFileAttributesW` | Checks whether a path exists and whether it's a file or folder. |
| `SetFileAttributesW` | Changes attributes — used to clear the read-only flag before deleting. |
| `DeleteFileW` | Permanently deletes a file (bypasses Recycle Bin). |
| `RemoveDirectoryW` | Removes an *empty* folder. |
| `MoveFileExW` | Renames or moves a file/folder with control flags. |
| `CopyFileW` | Copies a file. |
| `CreateDirectoryW` | Creates a new folder. |

### WinFsError and `_werr`

```python
class WinFsError(OSError):
    pass

def _werr(action: str, path: str) -> WinFsError:
    code = ctypes.get_last_error()
    msg  = ctypes.FormatError(code) if code else "unknown error"
    return WinFsError(f"{action} failed [{code}]: {msg}\n  path: {path}")
```

`WinFsError` is a custom exception type (a subclass of Python's built-in
`OSError`) so callers can catch filesystem errors specifically.

`_werr` creates one of those exceptions. It calls `ctypes.get_last_error()` to
read the Win32 error code, then `ctypes.FormatError(code)` to turn it into a
human-readable string (the same text you'd see in an error dialog).

---

## 5. Path helpers

### `to_extended(path)`

```python
def to_extended(path: str) -> str:
    if not path:
        return path
    if path.startswith(EXT_PREFIX):
        return path                        # already has the prefix, nothing to do
    p = os.path.abspath(path)             # turn relative paths into absolute
    if p.startswith("\\\\"):              # network path (UNC)
        return EXT_UNC_PREFIX + p[2:]
    return EXT_PREFIX + p                  # regular drive path
```

Every single filesystem call in this app goes through `to_extended` before
being passed to Win32. This is what unlocks paths longer than 260 characters.

`os.path.abspath` resolves any `..` or `.` components and ensures the path
starts with a drive letter (`C:\…`), because `\\?\` requires a fully-absolute
path — relative paths don't work with it.

### `from_extended(path)`

The reverse: strips the `\\?\` prefix back off so paths look normal in the UI.

### `exists(path)` and `is_dir(path)`

```python
def exists(path: str) -> bool:
    return _GetFileAttributesW(to_extended(path)) != INVALID_FILE_ATTRIBUTES

def is_dir(path: str) -> bool:
    a = _GetFileAttributesW(to_extended(path))
    return a != INVALID_FILE_ATTRIBUTES and bool(a & FILE_ATTRIBUTE_DIRECTORY)
```

`GetFileAttributesW` returns `0xFFFFFFFF` when a path doesn't exist. For
`is_dir`, the result is a **bitmask** — `a & FILE_ATTRIBUTE_DIRECTORY` tests
whether the "directory" bit is set in the returned flags.

### `clear_readonly(path)`

Before deleting or overwriting, we clear the read-only attribute so Windows
doesn't refuse the operation:

```python
a = _GetFileAttributesW(ext)
if a != INVALID_FILE_ATTRIBUTES and a & FILE_ATTRIBUTE_READONLY:
    _SetFileAttributesW(ext, a & ~FILE_ATTRIBUTE_READONLY)
```

`a & ~FILE_ATTRIBUTE_READONLY` — the `~` flips all the bits in
`FILE_ATTRIBUTE_READONLY`, turning it into a mask where every bit except the
read-only bit is 1. ANDing with `a` clears just that one bit while leaving the
rest unchanged.

### `listdir(path)`

```python
with os.scandir(to_extended(path)) as it:
    for e in it:
        d = e.is_dir()
        result.append((e.name, d))
```

`os.scandir` is more efficient than `os.listdir` because the Windows
`FindNextFile` call it uses already knows whether each entry is a directory —
no extra system call needed per item. Returns a list of `(name, is_dir)` pairs.

### `make_dir(path)`

```python
to_create = []
cur = path.rstrip("\\/")
while cur and not exists(cur):
    to_create.append(cur)
    parent = os.path.dirname(cur)
    if parent == cur:   # reached the drive root
        break
    cur = parent
for p in reversed(to_create):
    if not _CreateDirectoryW(to_extended(p), None):
        if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
            raise _werr("CreateDirectory", p)
```

Creates a folder and all its missing parents, without using recursion.
Why avoid recursion? Python has a default recursion limit of 1,000 — and this
app is specifically designed to handle paths that might be hundreds of folders
deep, so a recursive approach could crash with a `RecursionError`.

The loop walks **upward** collecting folders that don't exist yet, then creates
them in reverse order (top-down) so each parent exists before its child.

---

## 6. Filesystem primitives

These are thin, safe wrappers around the Win32 functions:

### `delete_file(path)` / `remove_empty_dir(path)`

Both first call `clear_readonly` (so locked files don't block), then call the
relevant Win32 function. If the Win32 call returns 0 (failure), they raise a
`WinFsError` with the error details.

### `copy_file(src, dst, overwrite)`

```python
fail_if_exists = wintypes.BOOL(0 if overwrite else 1)
if not _CopyFileW(to_extended(src), to_extended(dst), fail_if_exists):
    raise _werr("CopyFile", f"{src} -> {dst}")
```

`CopyFileW`'s third argument is "fail if destination already exists". We invert
the user's `overwrite` flag because the Win32 API uses the opposite convention.

### `move_path` vs `rename_path`

Two different wrappers around `MoveFileExW` with different flag sets:

- **`move_path`** (used by Transfer) includes `MOVEFILE_COPY_ALLOWED` — if
  source and destination are on different drives, Windows copies the file and
  deletes the original. This is what you want for a move operation.
- **`rename_path`** (used by Scan & Rename) deliberately excludes that flag —
  a rename should never silently become a copy. If somehow the user tries to
  "rename" across drives, it fails loudly so they know to use Transfer instead.

### `merge_into(src, dst, log)`

A safety net that should never be needed in normal operation. After a rename,
if both the old and new names somehow end up existing (e.g., an antivirus
scanner re-created the source), this function merges the contents of `src` into
`dst` without losing data. It recurses through the tree and either moves files
or logs conflicts where it can't resolve automatically.

---

## 7. Recursive helpers

### `_walk_topdown(root)` and `_walk_bottomup(root)`

Two iterators that yield every file and folder under a root path:

- **Top-down** (used for copy/move): yields the root first, then each
  subdirectory, then files — so parent folders exist before we try to create
  their children at the destination.
- **Bottom-up** (used for delete): yields files first, then subdirectories
  from deepest to shallowest — so folders are only deleted once they're empty.

Both use an explicit **stack** (a list used as LIFO queue) rather than
recursion, for the same reason as `make_dir` — deep trees would overflow
Python's call stack.

### `scan_tree(root, progress, cancel)`

```python
def scan_tree(root, progress=None, cancel=None):
    count = 0
    last_update = 0.0
    yield root, True              # yield the root folder itself first
    stack = [root]
    while stack:
        if cancel and cancel():   # check if user hit Cancel
            return
        current = stack.pop()
        for name, entry_is_dir in listdir(current):
            full = os.path.join(current, name)
            yield full, entry_is_dir
            count += 1
            if entry_is_dir:
                stack.append(full)
            if progress is not None:
                now = time.monotonic()
                if now - last_update > 0.05:   # update UI at most ~20 times/sec
                    progress(count, full)
                    last_update = now
```

A generator (a function that uses `yield` to produce values one at a time
instead of building a list). The calling code gets each `(path, is_dir)` pair
one at a time, which means the scan result appears incrementally in the table
rather than freezing until everything is found.

The progress callback is rate-limited to ~20 calls/second — calling it
more often would flood the UI message queue and slow everything down.

### `recursive_delete` and `recursive_copy`

`recursive_delete` walks bottom-up and deletes every item. Any individual
failure is logged and skipped — the batch continues.

`recursive_copy` walks top-down, creating destination folders as it goes,
then copies (or moves) each file. If `move=True`, it deletes the source tree
afterwards.

---

## 8. UI theming

### Palette and settings

```python
ACCENTS = {
    "transfer": {"accent": "#7c5cfc", "soft": "#efecff", "border_soft": "#c6b9ff"},
    "scan":     {"accent": "#14a3a3", ...},
    "purple":   ...,  "teal": ...,  "amber": ...,  ...
}
THEMES = {
    "light": {"bg": "#ffffff", "text": "#1a1a24", ...},
    "dark":  {"bg": "#14151a", "text": "#e9eaf0", ...},
}
DEFAULT_SETTINGS = {
    "accent_override": "",
    "ui_font_size":    10,
    "sans_family":     "Segoe UI",
    "mono_family":     "Cascadia Mono",
    "visible_cols":    ["len", "newlen", "kind", "orig", "new"],
    "col_widths":      {"len": 80, "newlen": 100, "kind": 64, "orig": 600, "new": 600},
    "last_root":       "",
}
```

These are just Python dictionaries — keys map to values. `THEMES` holds two
colour palettes. `ACCENTS` holds accent colour sets: `transfer` and `scan` are
the per-page defaults; the others are user-selectable overrides. Settings have
sensible defaults that get overwritten when a `settings.json` file is found.

Two keys were added in v1.1:

- **`col_widths`** — a nested dictionary mapping each column ID to its pixel width. Written when the window closes and after each auto-fit. Restored on startup so the table looks the same as when you left it.
- **`last_root`** — the path the user most recently scanned. Written at the start of each scan and pre-filled into the ROOT field on startup.

### `_fonts(settings)`

```python
def _fonts(settings: dict) -> dict:
    sans = settings.get("sans_family", DEFAULT_SETTINGS["sans_family"])
    size = int(settings.get("ui_font_size", 10))
    size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, size))
    return {
        "sans":      (sans, size),
        "sans_sm":   (sans, max(size - 1, 8)),
        "sans_bold": (sans, size, "bold"),
        "sans_head": (sans, size + 4, "bold"),
        "mono":      (sans, size),          # alias — same family as sans
        "mono_sm":   (sans, max(size - 1, 8)),
        ...
    }
```

Returns a dictionary of **tkinter font tuples** — `(family, size)` or
`(family, size, style)`. These are used everywhere a widget needs a font.
Because they all derive from a single `sans` family, changing one setting
in Settings → Appearance updates every widget at once when `_reapply_theme`
is called.

`max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, size))` clamps the size between 9 and
16 to prevent unusably tiny or giant text.

### `load_settings` / `save_settings`

Simple JSON read/write. `save_settings` is called every time the user changes
a setting — it never needs an explicit "Save" button. `load_settings` starts
with `DEFAULT_SETTINGS` as a base and overlays anything found in the file,
so missing keys always fall back to sensible defaults.

### `apply_theme(root, theme_name, accent_name, settings)`

The big function that configures every `ttk.Style`. Tkinter's themed widget
set (`ttk`) uses a style system — you define named styles once and assign
them to widgets. Calling `apply_theme` rebuilds every style from scratch
using the current theme colours and fonts.

Key concept: **every style that has visible text** now explicitly sets
`font=f["sans"]`. Without this, tkinter falls back to the system default font
and ignores the user's typeface choice.

Example — primary button:
```python
style.configure("Primary.TButton",
    background=acc["accent"], foreground="#ffffff",
    ...
    font=f["sans"])                  # ← respects typeface setting
style.map("Primary.TButton",
    background=[("active", _darken(acc["accent"]))])  # ← hover state
```

`style.map` defines state-dependent overrides — the `[("active", ...)]` means
"when the button is hovered or focused, use this background instead".

### `apply_window_chrome`

Calls three undocumented (but stable on Win11) `DwmSetWindowAttribute` values:

- Square corners (turns off Win11's default rounded corners)
- Immersive dark title bar (matches the app's dark theme to the system title bar)
- Optional Mica backdrop (semi-transparent background that blends with the desktop)

All DWM calls are inside try/except — if they fail (older Windows, VM), the
app still works, it just looks stock.

---

## 9. Pure logic helpers

Three module-level functions sit between the UI-theming code and the `App`
class. They contain no tkinter dependencies, so `test_logic.py` can import and
call them directly — any regression in their behaviour immediately fails the
tests.

### `normalise_replacement(repl)`

Converts the backreference syntax a user might type in the Replace field into
the form Python's `re` module understands:

| User writes | Python needs |
|---|---|
| `$1` | `\1` |
| `${1}` | `\g<1>` |
| `${name}` | `\g<name>` |

Implemented as three sequential `re.sub` calls. Applied before every regex
substitution so users can use either `$1` or `\1` interchangeably.

### `apply_replacement(find, repl, text, *, regex, case) → (result, changed)`

Applies one find/replace operation to a single path string. Returns a
`(result, changed)` tuple where `changed` is `True` if at least one
substitution was made.

Covers four combinations of `regex` × `case`:

- **regex + case-sensitive** — compile, normalise replacement, `subn`
- **regex + case-insensitive** — same with `re.IGNORECASE`
- **literal + case-sensitive** — plain `str.replace`
- **literal + case-insensitive** — `re.compile(re.escape(find), IGNORECASE).subn(lambda m: repl, text)`

The lambda in the last case is critical: passing `repl` as a callable instead
of a string tells `subn` to use it literally, so `C:\new\folder` in the
Replace field is never expanded as `C:` + newline + `ew` + form-feed + `older`.

Raises `re.error` for invalid patterns or replacements, letting `_bulk_replace`
catch and display the error before touching any rows.

`re.compile` results are cached by the standard library, so calling this in a
loop of 300 000 rows with the same `find` and `flags` is not meaningfully
slower than compiling once outside the loop.

### `rename_segment_walk(mapping, rename_fn, log_fn=None) → (ok, fail, done_renames)`

The core algorithm for safe bulk renaming. See [Apply Renames](#15-apply-renames)
for a full explanation. Extracted here so `test_logic.py` can inject a mock
`rename_fn` and assert exactly which physical renames fire and in what order,
without touching the filesystem.

---

## 10. App class overview

```python
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Long Path Begone")
        self.geometry("1180x720")
        ...
```

`App` **is** the main window — it inherits from `tk.Tk`, which is tkinter's
root window class. The `__init__` method runs once when the app starts.

### Important instance variables

| Variable | Type | Purpose |
|---|---|---|
| `self.settings` | `dict` | Mirror of `settings.json`. Written on every change. |
| `self.theme` | `str` | `"light"` or `"dark"`. |
| `self.current_page` | `str` | `"scan"`, `"transfer"`, or `"logs"`. |
| `self.rows` | `dict[str, dict]` | `{id: {orig, new, is_dir}}` — the scan results table data. |
| `self._busy` | `bool` | True while a background operation is running; prevents double-clicks. |
| `self._scan_cancel` | `bool` | Set to `True` by the Cancel button; the scan thread polls this. |
| `self._log_q` | `queue.Queue` | Thread-safe inbox for log messages from background workers. |
| `self._text_widgets` | `list` | Raw `tk.Text` widgets tracked so they can be re-themed. |
| `self._log_unread` | `int` | Count of log messages received while the Logs tab is not active. Reset to 0 when the user opens the Logs tab. |

### The layout stack

```
App (tk.Tk root window)
├── Titlebar (TFrame, Titlebar.TFrame style)
│     ├── LPB logo (Canvas)
│     ├── Nav buttons (🔍 Scan & Rename, 📦 Transfer, 📋 Logs)
│     └── Icon buttons (⚙ Settings, [D] theme toggle)
├── Separator (hairline border below titlebar)
├── Status bar (pinned to bottom with side="bottom")
└── Pages host (fills everything between titlebar and status bar)
      ├── Scan page (default visible)
      ├── Transfer page (hidden until selected)
      └── Logs page (hidden until selected)
```

The status bar is packed `side="bottom"` *before* the pages host, which means
tkinter reserves space for it first. This is why the status bar always appears
at the very bottom even when the window is resized.

In v1.0 the activity log was permanently pinned to the bottom of the main area.
In v1.1 it was moved to a dedicated Logs page so the Scan and Transfer pages
have the full vertical height available.

---

## 11. Titlebar and navigation

### `_draw_logo()`

```python
tile_w, tile_h, gap = 18, 22, 3
letters = "LPB"
for i, letter in enumerate(letters):
    x0 = i * (tile_w + gap)
    x1 = x0 + tile_w
    cv.create_rectangle(x0, 0, x1, tile_h, fill=acc, outline="")
    cv.create_text(x0 + tile_w // 2, tile_h // 2, text=letter,
                   fill="#ffffff", font=..., anchor="center")
```

Draws three coloured rectangles side by side on a tkinter `Canvas`, each with
a white letter centred inside. Uses the current accent colour so the logo
changes when you switch accents. Called again whenever the theme or accent
changes.

### `_set_page(name)`

```python
for key, frame in self.pages.items():
    if key == name:
        frame.pack(fill="both", expand=True)
    else:
        frame.pack_forget()
```

Shows one page and hides all others using `pack_forget()` — the frames still
exist in memory, they're just not attached to the layout. This is a common
tkinter pattern for tab-switching without a `Notebook` widget.

When the user navigates to the Logs page, `_set_page` also resets `_log_unread`
to 0 and calls `_update_log_badge()` to clear the badge.

### `_update_log_badge()`

```python
def _update_log_badge(self):
    if self._log_unread > 0:
        self._nav_btns["logs"].config(text=f"📋  Logs  ({self._log_unread})")
    else:
        self._nav_btns["logs"].config(text="📋  Logs")
```

Updates the Logs nav button label to show `"📋  Logs  (3)"` when there are
unread messages, or plain `"📋  Logs"` when there are none. Called from
`_drain_log` every time a message arrives while the Logs tab is not visible,
and from `_set_page` when the user navigates to the Logs tab.

---

## 12. Transfer page

The Transfer page lets the user paste (or browse for) a list of long-path
files/folders and then Copy, Move, or Delete them all.

### `_run_transfer(action)`

```python
def _run_transfer(self, action):
    paths = [p.strip() for p in self.targets.get("1.0", "end").splitlines()
             if p.strip()]
    ...
    def worker():
        ok = fail = 0
        for path in paths:
            try:
                if action == "copy":
                    recursive_copy(path, dst, overwrite, move=False, log=self._log)
                elif action == "move":
                    recursive_copy(path, dst, overwrite, move=True,  log=self._log)
                elif action == "delete":
                    recursive_delete(path, log=self._log)
                ok += 1
            except Exception as e:
                self._log(f"[FAIL] {action}: {path} — {e}")
                fail += 1
        ...
    threading.Thread(target=worker, daemon=True).start()
```

Reads the target paths from the `tk.Text` box (splitting on newlines), asks
for confirmation, then runs the operation on a **daemon thread** so the UI
stays responsive. `daemon=True` means the thread is killed if the user closes
the window without waiting for it to finish.

---

## 13. Scan page

### Controls

The scan page has these controls, top to bottom:

1. **ROOT** — path entry (pre-filled from `last_root`) + Browse + Scan + Cancel buttons
2. **Progress bar** + status text
3. **Filter row** — MIN LENGTH spinbox · Folders checkbox · Files checkbox · MAX FILES spinbox · Split path checkbox
4. **Find/Replace row** — Find entry · Replace entry · .* regex checkbox · Aa case checkbox · Apply button
5. **Regex status label** — shows error messages when a regex is invalid
6. **Treeview** — the results table with columns: LENGTH, NEW LENGTH, KIND, ORIGINAL PATH, NEW PATH
7. **Apply Renames bar** — always pinned below the treeview (packed `side="bottom"` before the treeview so it can never be pushed out of view)

### `_scan()`

```python
def _scan(self):
    root = self.root_var.get().strip()
    ...
    self.rows.clear()
    ...
    def worker():
        limit = self.scan_limit.get()    # 0 = no limit
        for i, (full, isdir) in enumerate(
                scan_tree(root, progress=on_progress,
                          cancel=lambda: self._scan_cancel)):
            self.rows[str(i)] = {"orig": full, "new": full, "is_dir": isdir}
            total = i + 1
            if limit > 0 and total >= limit:
                self._scan_cancel = True
                break
        ...
    threading.Thread(target=worker, daemon=True).start()
    self.after(60, pump)
```

The scan runs on a background thread. A separate `pump()` function runs every
60ms on the main thread, draining the `ui_q` queue for progress updates.
When the worker finishes, it puts a `("done", total)` message on the queue,
which `pump()` picks up to re-enable buttons and call `_refresh_view`.

The MAX FILES limit works by setting `_scan_cancel = True` when the item count
reaches the limit — the same mechanism the Cancel button uses.

### `_refresh_view()`

Rebuilds the treeview from `self.rows` every time filters or sorting change.
It's called many times — on every keystroke in the Find field, on every
checkbox toggle, and after every bulk replace. Performance is acceptable
because tkinter's Treeview is very fast at inserting rows.

The "Split path" display option:
```python
if split:
    disp = lambda p: p.replace("\\", "  ›  ")   # A › B › C
else:
    disp = lambda p: p.replace("\\", "＼")        # A＼B＼C (heavier backslash glyph)
```

This is purely cosmetic — `row["new"]` always stores the real path with normal
backslashes. The display transform only affects what appears in the table.

### Inline editing (double-click)

```python
def _on_double_click(self, event):
    ...
    popup = tk.Frame(self.tree, bg=c.get("border", "#ccc"), bd=0)
    entry = tk.Entry(popup, font=self._f["sans"], bd=0, relief="flat", ...)
    xsb   = ttk.Scrollbar(popup, orient="horizontal", command=entry.xview)
    entry.configure(xscrollcommand=xsb.set)
    entry.pack(fill="x", ipady=1)
    xsb.pack(fill="x")
    sb_h = xsb.winfo_reqheight() or 14
    popup.place(x=0, y=y-1, width=tree_w, height=h+sb_h+2)
    entry.insert(0, self.rows[rid]["new"])
    entry.bind("<Return>", commit)
    entry.bind("<FocusOut>", commit)
```

When the user double-clicks the NEW PATH cell, a `tk.Frame` popup is placed
over the treeview using `.place()`. The popup spans the **full width of the
treeview** (not just the clicked cell) and contains a `tk.Entry` with a
horizontal scrollbar beneath it.

Using the full width (instead of just the cell width) means the user can scroll
horizontally through very long paths — a narrow cell-width editor would clip
paths at 200–300 characters.

On `<Return>` or clicking away, `commit()` saves the new value back into
`self.rows[rid]["new"]` and calls `_refresh_view()`.

### Column auto-fit — `_autofit_path_columns()`

After every scan completes, the ORIGINAL PATH and NEW PATH columns are
automatically widened to fit the longest path in the results:

```python
def _autofit_path_columns(self):
    import heapq
    fnt    = tkfont.Font(family=fam, size=sz)
    top_orig = heapq.nlargest(5, self.rows.values(), key=lambda r: len(r["orig"]))
    top_new  = heapq.nlargest(5, self.rows.values(), key=lambda r: len(r["new"]))
    max_orig_px = max(fnt.measure(r["orig"].replace("\\", sep_char)) for r in top_orig)
    max_new_px  = max(fnt.measure(r["new"].replace( "\\", sep_char)) for r in top_new)
    for col_id, measured in (("orig", max_orig_px), ("new", max_new_px)):
        width = max(heading_min, measured + pad)
        self.tree.column(col_id, width=width)
    self._save_col_widths()
```

**Why `heapq.nlargest(5, ...)`?** Measuring the pixel width of a string
(`fnt.measure()`) is an expensive Tk round-trip. With 300 000 paths in the
table, calling it on every row would block the UI thread for tens of seconds.
`heapq.nlargest` scans all rows in O(n) time but only returns the 5 longest
ones by *character count*. The 5 longest characters are almost always the 5
widest in pixels too (fonts aren't perfectly proportional, but it's close
enough). We then call `fnt.measure()` on only those 5 strings — effectively O(1)
measuring regardless of table size.

### Saving and restoring column widths — `_save_col_widths()` / `_on_close()`

```python
def _save_col_widths(self):
    widths = {col: self.tree.column(col, "width") for col in COL_IDS}
    self.settings["col_widths"] = widths
    save_settings(self.settings)

def _on_close(self):
    self._save_col_widths()
    self.destroy()
```

`_save_col_widths` is called:
- After `_autofit_path_columns` runs (post-scan)
- On every `<ButtonRelease-1>` on the treeview header (user finished dragging a
  column divider)
- In `_on_close` just before the window is destroyed

`_on_close` is registered as the `WM_DELETE_WINDOW` protocol handler in
`__init__`. Without this, clicking the window's X button would call tkinter's
default destroy immediately — before `_save_col_widths` had a chance to run.

On startup, `_build_scan_page` reads the saved widths back:

```python
saved_widths = self.settings.get("col_widths", {})
w = saved_widths.get(col_id, default_w)
self.tree.column(col_id, width=w, ...)
```

---

## 14. Find and Replace

### `_compile_find()`

Used by `_refresh_view` to filter which rows are shown. Returns either a
compiled regex pattern (if .* regex is on) or the raw find string (for literal
filtering).

### `_bulk_replace()`

The "Apply to visible" button calls this. After reading the find/replace
strings and mode flags from the UI, it delegates all per-row work to the
module-level `apply_replacement` function:

```python
# Validate pattern and replacement before touching any rows.
try:
    apply_replacement(find, repl, "", regex=use_regex, case=case)
except re.error as e:
    self.regex_status.set(f"Invalid pattern/replacement: {e}")
    return

for rid in self.tree.get_children():
    row = self.rows[rid]
    updated, changed = apply_replacement(find, repl, row["new"],
                                         regex=use_regex, case=case)
    if changed:
        row["new"] = updated
        n += 1
```

The trial call on an empty string validates both the pattern and the
replacement in one step before any row is touched. The actual logic — backreference
normalisation, lambda trick for backslash safety, regex vs. literal branching —
all lives in `apply_replacement`. See [Pure logic helpers](#9-pure-logic-helpers)
for the full explanation.

---

## 15. Apply Renames

`_apply_renames()` validates the edits, sorts the mapping by path depth
(parents first), then spawns a background thread that delegates all renaming
work to the module-level `rename_segment_walk` function:

```python
mapping = sorted(((r["orig"], r["new"]) for r in edits),
                 key=lambda kv: (kv[0].count(os.sep), kv[0]))

def worker():
    ok, fail, _ = rename_segment_walk(mapping, rename_path, self._log)
    self._log(f"--- renames done: {ok} ok, {fail} failed ---")
    self.after(0, self._after_apply)

threading.Thread(target=worker, daemon=True).start()
```

After all renames complete, `_after_apply()` re-enables the button and
triggers a fresh scan so the table reflects the new state of the filesystem.

### Why not just rename each row directly?

Consider this example:
```
ORIGINAL                    NEW
A\B\C\D                 →   A\B\CC\DD
A\B\C\D\E\F\G           →   A\B\CC\DD\E\F\GG
```

If you rename `A\B\C\D` first, the path `A\B\C\D\E\F\G` no longer exists —
Windows moved everything inside `C\D` to `CC\DD`. You'd need to update the
second path to `A\B\CC\DD\E\F\GG` before you can rename it.

The solution — implemented in `rename_segment_walk` — is to rename **one path
segment at a time, left to right**:

```
Step 1:  A\B\C        →  A\B\CC          (rename just the C segment)
Step 2:  A\B\CC\D     →  A\B\CC\DD       (rename just the D segment)
Step 3:  A\B\CC\DD\E\F\G  →  A\B\CC\DD\E\F\GG  (rename just the G segment)
```

The `done_renames` dict inside `rename_segment_walk` serves two purposes:

1. **Deduplication** — when multiple rows share the same parent, the parent
   rename fires exactly once; sibling rows skip it via the dict lookup.

2. **Path reconciliation** — a later row's `new` path might refer to a segment
   already renamed by an earlier row. The lookup corrects both `cur_parts` and
   `new_parts` before each step so everything routes through the latest names.

Because the algorithm is extracted into `rename_segment_walk`, all of this
logic is covered by unit tests in `test_logic.py` without touching the filesystem.

---

## 16. Logs tab

The Logs tab (`_build_log_page`) holds the activity log and the error log on a
dedicated full-page view, replacing the cramped strip that was pinned to the
bottom of the main area in v1.0.

### Layout

```
Logs page (ttk.Frame, fills the pages host)
├── Activity log section (expands to fill available height)
│     ├── tk.Text (wraps=NONE, xscrollcommand, yscrollcommand)
│     ├── Vertical scrollbar (right side)
│     └── Horizontal scrollbar (bottom)
├── Separator (hairline divider between the two sections)
└── Error log section (fixed height, pinned at bottom)
      ├── Label: "Error log"
      ├── tk.Text (wraps=NONE)
      └── Horizontal scrollbar
```

Both text widgets use `wrap=tk.NONE` so long lines extend horizontally rather
than word-wrapping — paths are often wider than the window and must be readable
in full.

### Unread badge

`_drain_log` is called by a periodic `self.after(200, _drain_log)` timer on
the main thread. It reads every pending message from `self._log_q` (written by
background worker threads) and appends them to the activity log text widget.

```python
# inside _drain_log, after appending each message:
if self.current_page != "logs":
    self._log_unread += 1
    self._update_log_badge()
```

Because the user can't see the Logs tab when they're on the Scan or Transfer
page, every new message increments `_log_unread` and triggers a badge update.
The badge text `"📋  Logs  (3)"` appears on the nav button so the user knows
something happened without needing to switch tabs.

---

## 17. Settings modal

The settings window is a `tk.Toplevel` (a child window) containing a
`ttk.Notebook` with four tabs:

### Appearance tab

- **Accent colour** — radio buttons, each saving `accent_override` to settings
  and calling `_reapply_theme()`.
- **UI font size** — a `ttk.Scale` slider, debounced so it only reapplies
  after the user stops dragging.
- **Typeface** — a `ttk.Combobox` showing `(sans, mono)` pairs. Changing it
  saves both `sans_family` and `mono_family` and calls `_reapply_theme()`.

### Columns tab

Checkboxes for each of the five table columns. Unchecking a column calls
`_apply_column_visibility()` which updates `tree.configure(displaycolumns=...)`.

### Find & Replace tab

A scrollable reference guide — the regex cheatsheet table and common examples.
The cheatsheet now renders with a `ttk.Separator` between each row for
readability.

### About tab

Displays version, platform, and the path to `settings.json`.

---

## 18. Entry point

```python
def main():
    if os.name != "nt":
        print("Windows-only.")
        return
    App().mainloop()

if __name__ == "__main__":
    main()
```

`os.name != "nt"` checks that we're on Windows (`"nt"` is the internal name
for Windows NT and all successors: XP, Vista, 7, 10, 11). If not, it prints a
message and exits cleanly rather than crashing with an inscrutable error.

`App().mainloop()` creates the window and starts tkinter's event loop.
`mainloop()` runs forever, processing mouse clicks, keyboard input, and timer
callbacks, until the user closes the window. When the window closes,
`mainloop()` returns and the program exits.

`if __name__ == "__main__"` — Python sets `__name__` to `"__main__"` only when
the file is run directly (e.g. `python long_path_begone.py`). If it were
imported by another file, `__name__` would be the module name instead, and
`main()` would not be called automatically. This is a standard Python
convention.

---

## Files summary

| File | Purpose |
|---|---|
| `long_path_begone.py` | The entire application. Pure Python, no dependencies beyond stdlib. |
| `LongPathBegone.spec` | PyInstaller build config. Run `python -m PyInstaller LongPathBegone.spec` to produce `dist\LongPathBegone.exe`. |
| `settings.json` | Auto-created beside the script on first run. Stores user preferences. |
| `README.md` | Quick-start guide and build instructions. |
| `SourceCode.md` | This file. |
| `ERRORS.md` | Win32 error code reference and troubleshooting guide. |
