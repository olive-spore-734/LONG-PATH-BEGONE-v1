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
9. [App class overview](#9-app-class-overview)
10. [Titlebar and navigation](#10-titlebar-and-navigation)
11. [Transfer page](#11-transfer-page)
12. [Scan page](#12-scan-page)
13. [Find and Replace](#13-find-and-replace)
14. [Apply Renames](#14-apply-renames)
15. [Settings modal](#15-settings-modal)
16. [Entry point — starting the app](#16-entry-point)

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
}
```

These are just Python dictionaries — keys map to values. `THEMES` holds two
colour palettes. `ACCENTS` holds accent colour sets: `transfer` and `scan` are
the per-page defaults; the others are user-selectable overrides. Settings have
sensible defaults that get overwritten when a `settings.json` file is found.

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

## 9. App class overview

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
| `self.current_page` | `str` | `"scan"` or `"transfer"`. |
| `self.rows` | `dict[str, dict]` | `{id: {orig, new, is_dir}}` — the scan results table data. |
| `self._busy` | `bool` | True while a background operation is running; prevents double-clicks. |
| `self._scan_cancel` | `bool` | Set to `True` by the Cancel button; the scan thread polls this. |
| `self._log_q` | `queue.Queue` | Thread-safe inbox for log messages from background workers. |
| `self._text_widgets` | `list` | Raw `tk.Text` widgets tracked so they can be re-themed. |

### The layout stack

```
App (tk.Tk root window)
├── Titlebar (TFrame, Titlebar.TFrame style)
│     ├── LPB logo (Canvas)
│     ├── Nav buttons (🔍 Scan & Rename, 📦 Transfer)
│     └── Icon buttons (⚙ Settings, [D] theme toggle)
├── Separator (hairline border below titlebar)
├── Status bar (pinned to bottom with side="bottom")
└── Main area (fills everything between titlebar and status bar)
      ├── Activity log (pinned to bottom of main)
      └── Pages host
            ├── Scan page (default visible)
            └── Transfer page (hidden until selected)
```

The status bar is packed `side="bottom"` *before* the main area, which means
tkinter reserves space for it first. This is why the status bar always appears
at the very bottom even when the window is resized.

---

## 10. Titlebar and navigation

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

---

## 11. Transfer page

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

## 12. Scan page

### Controls

The scan page has these controls, top to bottom:

1. **ROOT** — path entry + Browse + Scan + Cancel buttons
2. **Progress bar** + status text
3. **Filter row** — MIN LENGTH spinbox · Folders checkbox · Files checkbox · MAX FILES spinbox · Split path checkbox
4. **Find/Replace row** — Find entry · Replace entry · .* regex checkbox · Aa case checkbox · Apply button
5. **Regex status label** — shows error messages when a regex is invalid
6. **Treeview** — the results table with columns: LENGTH, NEW LENGTH, KIND, ORIGINAL PATH, NEW PATH

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
    entry = tk.Entry(self.tree, ...)
    entry.insert(0, self.rows[rid]["new"])
    entry.place(x=x, y=y-1, width=w, height=h+2)
    entry.bind("<Return>", commit)
    entry.bind("<FocusOut>", commit)
```

When the user double-clicks the NEW PATH cell, a borderless `tk.Entry` widget
is placed exactly over the cell using the cell's bounding box coordinates.
On `<Return>` or clicking away, `commit()` saves the new value back into
`self.rows[rid]["new"]` and calls `_refresh_view()`.

---

## 13. Find and Replace

### `_compile_find()`

Used by `_refresh_view` to filter which rows are shown. Returns either a
compiled regex pattern (if .* regex is on) or the raw find string (for literal
filtering).

### `_bulk_replace()`

The "Apply to visible" button calls this. It processes every visible row and
updates `row["new"]` in-place:

```python
repl = self.replace_var.get()

if use_regex:
    # Normalise $1/$2 backreferences to Python's \1/\2 syntax
    repl = re.sub(r'\$\{(\d+)\}',          r'\\g<\1>', repl)
    repl = re.sub(r'\$\{([A-Za-z_]\w*)\}', r'\\g<\1>', repl)
    repl = re.sub(r'\$(\d+)',              r'\\\1',     repl)

    # Validate before touching any rows
    try:
        pat.sub(repl, "")
    except re.error as e:
        self.regex_status.set(f"Invalid replacement: {e}")
        return
    ...
```

**Why `$1` → `\1` conversion?** Python's `re.sub` uses `\1`, `\2` for captured
groups. Most other tools (JavaScript, grep, sed) use `$1`, `$2`. The
normalisation step means both styles work transparently.

For the case-insensitive literal replacement path:
```python
updated, k = _case_pat.subn(lambda m: repl, new)
```

Using a **lambda** (an inline function) as the replacement argument instead of
a string tells `re.subn` to use the return value literally — no backslash
processing. This prevents Windows paths like `C:\new\folder` in the Replace
field from having `\n` and `\f` silently expanded as escape sequences.

---

## 14. Apply Renames

`_apply_renames()` is the most complex part of the app. It takes the list of
`(original path, new path)` pairs from all edited rows and applies them safely.

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

The solution is to rename **one path segment at a time, left to right**:

```
Step 1:  A\B\C        →  A\B\CC          (rename just the C segment)
Step 2:  A\B\CC\D     →  A\B\CC\DD       (rename just the D segment)
Step 3:  A\B\CC\DD\E\F\G  →  A\B\CC\DD\E\F\GG  (rename just the G segment)
```

### `done_renames` dictionary

```python
done_renames: dict[str, str] = {}   # maps "original path" → "renamed path"
```

Serves two jobs:

1. **Deduplication** — when multiple rows share the same parent (e.g. 500
   files all in `C:\LongName`), the parent rename `C:\LongName → C:\Short`
   only fires once. Sibling rows skip it when they see it's already in
   `done_renames`.

2. **Path reconciliation** — a later row's `new` path might refer to a segment
   that was already renamed by an earlier row. The lookup corrects both the
   "current" path (`cur_parts`) and the "intended new" path (`new_parts`)
   before each step so everything routes through the latest actual names.

### The segment loop

```python
for i in range(len(orig_parts)):
    src_seg = os.sep.join(cur_parts[:i + 1])   # e.g. "A\B\C" at step i=2

    if src_seg in done_renames:
        actual_dst  = done_renames[src_seg]
        actual_name = actual_dst.rsplit(os.sep, 1)[-1]
        cur_parts[i] = actual_name
        if new_parts[i] == orig_parts[i]:      # user didn't intend to rename this segment
            new_parts[i] = actual_name         # so update it to the actual current name
        src_seg = actual_dst

    if cur_parts[i] == new_parts[i]:
        continue                                # this segment hasn't changed, skip

    # This segment IS changing — do the rename
    cur_parts[i] = new_parts[i]
    dst_seg = os.sep.join(cur_parts[:i + 1])
    rename_path(src_seg, dst_seg)
    done_renames[src_seg] = dst_seg
```

After all renames complete, `_after_apply()` triggers a fresh scan so the
table reflects the new state of the filesystem.

---

## 15. Settings modal

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

## 16. Entry point

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
