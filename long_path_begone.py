r"""
Long Path Begone — Windows utility for files/folders with paths over MAX_PATH.

Pages:
  • Scan & Rename — scan a tree, edit any path inline (or via regex
    find/replace), then apply renames parents-first.
  • Transfer — Copy / Move / Delete batches of long-path items.

All filesystem calls use the \\?\ extended-length prefix via kernel32, so
works regardless of LongPathsEnabled.
"""

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

EXT_PREFIX = "\\\\?\\"
EXT_UNC_PREFIX = "\\\\?\\UNC\\"




def _darken(hex_color: str, factor: float = 0.82) -> str:
    """Return a slightly darkened version of a hex color for hover states."""
    h = hex_color.lstrip("#")
    r, g, b = int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16)
    return f"#{int(r*factor):02x}{int(g*factor):02x}{int(b*factor):02x}"


# =============================================================================
# kernel32 bindings
# =============================================================================

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

INVALID_FILE_ATTRIBUTES = 0xFFFFFFFF
FILE_ATTRIBUTE_DIRECTORY = 0x10
FILE_ATTRIBUTE_READONLY = 0x01
MOVEFILE_REPLACE_EXISTING = 0x1
MOVEFILE_COPY_ALLOWED = 0x2
MOVEFILE_WRITE_THROUGH = 0x8
ERROR_ALREADY_EXISTS = 183

_GetFileAttributesW = _k32.GetFileAttributesW
_GetFileAttributesW.argtypes = [wintypes.LPCWSTR]
_GetFileAttributesW.restype = wintypes.DWORD

_SetFileAttributesW = _k32.SetFileAttributesW
_SetFileAttributesW.argtypes = [wintypes.LPCWSTR, wintypes.DWORD]
_SetFileAttributesW.restype = wintypes.BOOL

_DeleteFileW = _k32.DeleteFileW
_DeleteFileW.argtypes = [wintypes.LPCWSTR]
_DeleteFileW.restype = wintypes.BOOL

_RemoveDirectoryW = _k32.RemoveDirectoryW
_RemoveDirectoryW.argtypes = [wintypes.LPCWSTR]
_RemoveDirectoryW.restype = wintypes.BOOL

_MoveFileExW = _k32.MoveFileExW
_MoveFileExW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD]
_MoveFileExW.restype = wintypes.BOOL

_CopyFileW = _k32.CopyFileW
_CopyFileW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.BOOL]
_CopyFileW.restype = wintypes.BOOL

_CreateDirectoryW = _k32.CreateDirectoryW
_CreateDirectoryW.argtypes = [wintypes.LPCWSTR, ctypes.c_void_p]
_CreateDirectoryW.restype = wintypes.BOOL


class WinFsError(OSError):
    pass


def _werr(action: str, path: str) -> WinFsError:
    code = ctypes.get_last_error()
    msg = ctypes.FormatError(code) if code else "unknown error"
    return WinFsError(f"{action} failed [{code}]: {msg}\n  path: {path}")


# =============================================================================
# path helpers
# =============================================================================

def to_extended(path: str) -> str:
    if not path:
        return path
    if path.startswith(EXT_PREFIX):
        return path
    p = os.path.abspath(path)
    if p.startswith("\\\\"):
        return EXT_UNC_PREFIX + p[2:]
    return EXT_PREFIX + p


def from_extended(path: str) -> str:
    if path.startswith(EXT_UNC_PREFIX):
        return "\\\\" + path[len(EXT_UNC_PREFIX):]
    if path.startswith(EXT_PREFIX):
        return path[len(EXT_PREFIX):]
    return path


def exists(path: str) -> bool:
    return _GetFileAttributesW(to_extended(path)) != INVALID_FILE_ATTRIBUTES


def is_dir(path: str) -> bool:
    a = _GetFileAttributesW(to_extended(path))
    return a != INVALID_FILE_ATTRIBUTES and bool(a & FILE_ATTRIBUTE_DIRECTORY)


def clear_readonly(path: str) -> None:
    ext = to_extended(path)
    a = _GetFileAttributesW(ext)
    if a != INVALID_FILE_ATTRIBUTES and a & FILE_ATTRIBUTE_READONLY:
        _SetFileAttributesW(ext, a & ~FILE_ATTRIBUTE_READONLY)


def listdir(path: str) -> list[tuple[str, bool]]:
    """Return [(name, is_dir), …] for every entry under path.

    Uses DirEntry.is_dir() which is backed by the OS FindNext call, so no
    extra GetFileAttributesW syscall is needed per item.
    """
    result = []
    with os.scandir(to_extended(path)) as it:
        for e in it:
            try:
                d = e.is_dir()
            except OSError:
                d = False
            result.append((e.name, d))
    return result


def delete_file(path: str) -> None:
    clear_readonly(path)
    if not _DeleteFileW(to_extended(path)):
        raise _werr("DeleteFile", path)


def remove_empty_dir(path: str) -> None:
    clear_readonly(path)
    if not _RemoveDirectoryW(to_extended(path)):
        raise _werr("RemoveDirectory", path)


def make_dir(path: str) -> None:
    """Create path and all missing parents without recursion.

    Iterative so it never hits Python's recursion limit — relevant here
    because this tool specifically targets very deep path trees.
    """
    # Collect the chain of missing ancestors bottom-up, then create top-down.
    to_create = []
    cur = path.rstrip("\\/")
    while cur and not exists(cur):
        to_create.append(cur)
        parent = os.path.dirname(cur)
        if parent == cur:   # drive root
            break
        cur = parent
    for p in reversed(to_create):
        if not _CreateDirectoryW(to_extended(p), None):
            if ctypes.get_last_error() != ERROR_ALREADY_EXISTS:
                raise _werr("CreateDirectory", p)


def copy_file(src: str, dst: str, overwrite: bool) -> None:
    fail_if_exists = wintypes.BOOL(0 if overwrite else 1)
    if not _CopyFileW(to_extended(src), to_extended(dst), fail_if_exists):
        raise _werr("CopyFile", f"{src} -> {dst}")


def move_path(src: str, dst: str, overwrite: bool) -> None:
    """Move (or copy-then-delete across volumes) — used by Transfer."""
    flags = MOVEFILE_COPY_ALLOWED | MOVEFILE_WRITE_THROUGH
    if overwrite:
        flags |= MOVEFILE_REPLACE_EXISTING
    if not _MoveFileExW(to_extended(src), to_extended(dst), flags):
        raise _werr("MoveFileEx", f"{src} -> {dst}")


def rename_path(src: str, dst: str) -> None:
    """In-place rename only — never creates a new file/folder.

    Used by Scan & Rename. Deliberately drops MOVEFILE_COPY_ALLOWED so a
    cross-volume "rename" fails loudly instead of silently turning into
    a copy + delete that leaves a brand-new inode at the target. The
    user asked for renames, not duplicates: if dst is on a different
    volume, the caller should use Transfer (Move) instead.

    Also refuses to overwrite an existing target — Scan & Rename's
    duplicate-target check should already catch that, but a defensive
    failure here means no row ever silently clobbers an unrelated file.
    """
    flags = MOVEFILE_WRITE_THROUGH       # NO MOVEFILE_COPY_ALLOWED
    if not _MoveFileExW(to_extended(src), to_extended(dst), flags):
        raise _werr("MoveFileEx (rename)", f"{src} -> {dst}")


def merge_into(src: str, dst: str, log) -> tuple[int, int]:
    """Defensive cleanup after a rename — merge src into dst when both
    paths still exist.

    Should never be needed if `rename_path` worked correctly (a true NTFS
    rename leaves no duplicate). It runs as a safety net: if a buggy
    filesystem driver, antivirus, or third-party tool somehow left both
    paths behind, fold them together rather than leaving the user with
    two copies of the same data.

    Files: dst is canonical (it's the rename target); src is deleted.
    Dirs:  every entry under src is moved into dst recursively; on a
           name collision, recurse and merge subdirectories or
           overwrite-by-rename leaf files. The (now-empty) src dir is
           removed last. Returns (files_merged, conflicts).
    """
    files_merged = 0
    conflicts    = 0

    if not exists(src):
        return (0, 0)
    if not exists(dst):
        # The rename "duplicate" claim was wrong — only src exists, do
        # the rename now and bail. Caller will re-verify.
        rename_path(src, dst)
        return (0, 0)

    src_is_dir = is_dir(src)
    dst_is_dir = is_dir(dst)

    if src_is_dir != dst_is_dir:
        # Mixed file/dir collision — too dangerous to auto-merge.
        log(f"WARN: {src} and {dst} disagree on file vs. dir; leaving both in place.")
        return (0, 1)

    if not src_is_dir:
        # Two leftover files. dst is the canonical rename target; drop src.
        log(f"merge: dropping leftover file {src} (canonical at {dst})")
        delete_file(src)
        files_merged += 1
        return (files_merged, conflicts)

    # Both are dirs — recurse.
    for name, _ in listdir(src):
        s = os.path.join(src, name)
        d = os.path.join(dst, name)
        if exists(d):
            fm, cf = merge_into(s, d, log)
            files_merged += fm
            conflicts    += cf
        else:
            try:
                rename_path(s, d)
                files_merged += 1
            except Exception as e:
                log(f"merge: rename {s} -> {d} failed: {e}")
                conflicts += 1
    try:
        remove_empty_dir(src)
    except Exception as e:
        log(f"merge: could not remove emptied {src}: {e}")
        conflicts += 1
    return (files_merged, conflicts)


# =============================================================================
# recursive ops
# =============================================================================

def _walk_topdown(root: str):
    yield "dir", root
    stack = [root]
    while stack:
        current = stack.pop()
        try:
            entries = listdir(current)
        except OSError:
            continue
        for name, entry_is_dir in entries:
            full = os.path.join(current, name)
            if entry_is_dir:
                yield "dir", full
                stack.append(full)
            else:
                yield "file", full


def _walk_bottomup(root: str):
    dirs = []
    stack = [root]
    while stack:
        current = stack.pop()
        dirs.append(current)
        try:
            for name, entry_is_dir in listdir(current):
                full = os.path.join(current, name)
                if entry_is_dir:
                    stack.append(full)
                else:
                    yield "file", full
        except OSError:
            continue
    for d in reversed(dirs):
        yield "dir", d


def recursive_delete(root: str, log):
    if not exists(root):
        log(f"skip (not found): {root}")
        return
    if not is_dir(root):
        try:
            delete_file(root)
            log(f"deleted file: {root}")
        except OSError as e:
            log(f"ERROR {e}")
        return
    for kind, path in _walk_bottomup(root):
        try:
            if kind == "file":
                delete_file(path)
                log(f"deleted file: {path}")
            else:
                remove_empty_dir(path)
                log(f"deleted dir : {path}")
        except OSError as e:
            log(f"ERROR {e}")


def recursive_copy(src: str, dst: str, overwrite: bool, move: bool, log):
    if not exists(src):
        raise WinFsError(f"source not found: {src}")
    if not is_dir(src):
        parent = os.path.dirname(dst)
        if parent:
            make_dir(parent)
        if move:
            move_path(src, dst, overwrite)
            log(f"moved: {src} -> {dst}")
        else:
            copy_file(src, dst, overwrite)
            log(f"copied: {src} -> {dst}")
        return

    make_dir(dst)
    src_root = src.rstrip("\\/")
    for kind, path in _walk_topdown(src_root):
        rel = os.path.relpath(path, src_root)
        target = os.path.join(dst, rel) if rel != "." else dst
        try:
            if kind == "dir":
                make_dir(target)
            else:
                if move:
                    move_path(path, target, overwrite)
                    log(f"moved: {path} -> {target}")
                else:
                    copy_file(path, target, overwrite)
                    log(f"copied: {path} -> {target}")
        except OSError as e:
            log(f"ERROR {e}")

    if move:
        recursive_delete(src, log)


def scan_tree(root: str, progress=None, cancel=None):
    """Yield (full_path, is_dir) for every item under root, root first.

    progress(count, current_path) is called periodically. cancel() -> bool.
    """
    count = 0
    last_update = 0.0
    yield root, True
    count += 1
    stack = [root]
    while stack:
        if cancel and cancel():
            return
        current = stack.pop()
        try:
            entries = listdir(current)
        except OSError:
            continue
        for name, entry_is_dir in entries:
            full = os.path.join(current, name)
            yield full, entry_is_dir
            count += 1
            if entry_is_dir:
                stack.append(full)
            if progress is not None:
                now = time.monotonic()
                if now - last_update > 0.05:
                    progress(count, full)
                    last_update = now
    if progress is not None:
        progress(count, root)


# =============================================================================
# UI theming — glassy, square corners, light/dark
# =============================================================================


# =============================================================================
# UI — Friday-style (light-first, purple accent, square, hairline borders)
# =============================================================================

# Friday's canonical palette. Per-page default accents follow Friday's
# per-mode convention (Transfer → purple / Code, Scan → teal / Chat). A
# global accent override from Settings can replace both.
ACCENTS = {
    "transfer": {"accent": "#7c5cfc", "soft": "#efecff", "border_soft": "#c6b9ff"},
    "scan":     {"accent": "#14a3a3", "soft": "#e5f5f5", "border_soft": "#8ec9c9"},
    # Picker options (Settings → Appearance → Accent)
    "purple":   {"accent": "#7c5cfc", "soft": "#efecff", "border_soft": "#c6b9ff"},
    "teal":     {"accent": "#14a3a3", "soft": "#e5f5f5", "border_soft": "#8ec9c9"},
    "amber":    {"accent": "#d97706", "soft": "#fdf1dd", "border_soft": "#f0c680"},
    "magenta":  {"accent": "#d63384", "soft": "#fbe3ee", "border_soft": "#ecaacd"},
    "blue":     {"accent": "#2f6feb", "soft": "#e5eeff", "border_soft": "#a6bffc"},
    "green":    {"accent": "#16a34a", "soft": "#e4f6ec", "border_soft": "#9ed8b4"},
}

TYPEFACE_OPTIONS = [
    ("Segoe UI",      "Cascadia Mono"),
    ("Inter",         "JetBrains Mono"),
    ("Segoe UI",      "Consolas"),
    ("Arial",         "Courier New"),
    ("Calibri",       "Cascadia Code"),
]

FONT_SIZE_MIN = 9
FONT_SIZE_MAX = 16

DEFAULT_SETTINGS = {
    "accent_override": "",     # "" = use per-page; else one of ACCENTS picker keys
    "ui_font_size":    10,
    "sans_family":     "Segoe UI",
    "mono_family":     "Cascadia Mono",
    "visible_cols":    ["len", "newlen", "kind", "orig", "new"],
}

# All data files live beside the script — fully portable.
# If the script is run from a read-only location saves silently no-op.
_APP_DIR      = os.path.dirname(os.path.abspath(__file__))
SETTINGS_FILE = os.path.join(_APP_DIR, "settings.json")
ACTIVITY_LOG_FILE = os.path.join(_APP_DIR, "activity_log.txt")
ERROR_LOG_FILE    = os.path.join(_APP_DIR, "error_log.txt")

THEMES = {
    "light": {
        "bg":          "#ffffff",
        "bg_sidebar":  "#f6f7f9",
        "bg_surface":  "#ffffff",
        "bg_hover":    "#edeef3",
        "bg_input":    "#ffffff",
        "bg_code":     "#f6f7fa",
        "bg_active":   "#efecff",   # accent-soft (purple default)
        "text":        "#1a1a24",
        "text_sec":    "#60607a",
        "text_muted":  "#9095ad",
        "border":      "#e5e6eb",   # rgba(0,0,0,0.08) flattened
        "border_lt":   "#ededf0",
        "danger":      "#c0392b",
        "danger_bg":   "#fbeceb",
        "ok":          "#27ae60",
        "warn":        "#b4791a",
        "sel":         "#efecff",
        "changed":     "#fff4c2",
        "over260":     "#ffd9dc",
        "accent":      "#7c5cfc",
        "accent_soft": "#efecff",
        "accent_bdr":  "#c6b9ff",
        "alpha":       1.0,
        "mica":        0,
        "immersive":   False,
    },
    "dark": {
        "bg":          "#14151a",
        "bg_sidebar":  "#1a1c22",
        "bg_surface":  "#1f2128",
        "bg_hover":    "#262932",
        "bg_input":    "#1f2128",
        "bg_code":     "#16181d",
        "bg_active":   "#2a2447",
        "text":        "#e9eaf0",
        "text_sec":    "#a0a8bc",
        "text_muted":  "#8892a4",
        "border":      "#2a2d36",
        "border_lt":   "#22252d",
        "danger":      "#e5484d",
        "danger_bg":   "#3a1a1d",
        "ok":          "#3dba7a",
        "warn":        "#f5a524",
        "sel":         "#2a2447",
        "changed":     "#3d3814",
        "over260":     "#521e24",
        "accent":      "#a38bff",
        "accent_soft": "#2a2447",
        "accent_bdr":  "#5a4c99",
        "alpha":       0.97,
        "mica":        2,
        "immersive":   True,
    },
}

def _fonts(settings: dict) -> dict:
    """Derive the font-family tuples the rest of the UI uses from settings.

    Single-typeface policy: every named slot resolves to the user's chosen
    sans family. The legacy `mono*` keys are kept as aliases so existing
    `font=self._f["mono"]` call sites still work, but they no longer pick
    a different family — paths, code blocks, the activity log, headings,
    and labels all use the same face. Path columns rely on the table's
    own column widths for alignment, not on glyph-cell uniformity.
    """
    sans = settings.get("sans_family", DEFAULT_SETTINGS["sans_family"])
    size = int(settings.get("ui_font_size", DEFAULT_SETTINGS["ui_font_size"]))
    size = max(FONT_SIZE_MIN, min(FONT_SIZE_MAX, size))
    return {
        "sans":       (sans, size),
        "sans_sm":    (sans, max(size - 1, 8)),
        "sans_bold":  (sans, size, "bold"),
        "sans_head":  (sans, size + 4, "bold"),
        # Aliases — same family, no monospace split.
        "mono":       (sans, size),
        "mono_sm":    (sans, max(size - 1, 8)),
        "mono_logo":  (sans, size + 1, "bold"),
        "label":      (sans, max(size - 2, 8), "bold"),
    }


def load_settings() -> dict:
    s = dict(DEFAULT_SETTINGS)
    try:
        if os.path.exists(SETTINGS_FILE):
            with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
            if isinstance(data, dict):
                for k in DEFAULT_SETTINGS:
                    if k in data:
                        s[k] = data[k]
    except (OSError, ValueError):
        pass
    return s


def save_settings(s: dict) -> None:
    try:
        with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2)
    except OSError:
        pass


# -- DWM (square corners + optional glass) ------------------------------------

_DWMWA_USE_IMMERSIVE_DARK_MODE = 20
_DWMWA_WINDOW_CORNER_PREFERENCE = 33
_DWMWA_SYSTEMBACKDROP_TYPE = 38
_DWMWCP_DONOTROUND = 1

try:
    _dwm = ctypes.WinDLL("dwmapi")
    _user32 = ctypes.WinDLL("user32")
    _GetAncestor = _user32.GetAncestor
    _GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
    _GetAncestor.restype = wintypes.HWND
except OSError:
    _dwm = None


def _root_hwnd(tk_window) -> int:
    try:
        hwnd = int(tk_window.winfo_id())
    except Exception:
        return 0
    if _dwm is None:
        return 0
    root_hwnd = _GetAncestor(hwnd, 2)
    return int(root_hwnd or hwnd)


def _dwm_set(tk_window, attr: int, value: int):
    if _dwm is None:
        return
    hwnd = _root_hwnd(tk_window)
    if not hwnd:
        return
    val = ctypes.c_int(value)
    try:
        _dwm.DwmSetWindowAttribute(
            wintypes.HWND(hwnd), wintypes.DWORD(attr),
            ctypes.byref(val), ctypes.sizeof(val),
        )
    except OSError:
        pass


def apply_window_chrome(tk_window, theme_name: str):
    t = THEMES[theme_name]
    _dwm_set(tk_window, _DWMWA_WINDOW_CORNER_PREFERENCE, _DWMWCP_DONOTROUND)
    _dwm_set(tk_window, _DWMWA_USE_IMMERSIVE_DARK_MODE, 1 if t["immersive"] else 0)
    if t["mica"]:
        _dwm_set(tk_window, _DWMWA_SYSTEMBACKDROP_TYPE, t["mica"])
    try:
        tk_window.attributes("-alpha", t["alpha"])
    except tk.TclError:
        pass


def apply_theme(root: tk.Tk, theme_name: str, accent_name: str = "transfer",
                settings: dict | None = None):
    c = THEMES[theme_name]
    settings = settings or DEFAULT_SETTINGS
    # Global accent override (from Settings) wins over per-page default.
    override = settings.get("accent_override", "") or ""
    if override and override in ACCENTS:
        acc = ACCENTS[override]
    else:
        acc = ACCENTS[accent_name]

    f = _fonts(settings)
    style = ttk.Style(root)
    style.theme_use("clam")

    root.configure(bg=c["bg"])
    root.option_add("*Font", f["sans"])

    # baseline
    style.configure(".", background=c["bg"], foreground=c["text"],
                    fieldbackground=c["bg_input"], bordercolor=c["border"],
                    lightcolor=c["border"], darkcolor=c["border"],
                    troughcolor=c["bg_hover"], insertcolor=c["text"],
                    borderwidth=0, relief="flat")

    # frames
    style.configure("TFrame", background=c["bg"])
    style.configure("Sidebar.TFrame", background=c["bg_sidebar"])
    style.configure("Titlebar.TFrame", background=c["bg_sidebar"])
    style.configure("Statusbar.TFrame", background=c["bg_sidebar"])
    style.configure("Surface.TFrame", background=c["bg_surface"])

    # labels
    style.configure("TLabel", background=c["bg"], foreground=c["text"])
    style.configure("Muted.TLabel", background=c["bg"], foreground=c["text_muted"])
    style.configure("Sec.TLabel", background=c["bg"], foreground=c["text_sec"])
    style.configure("SidebarMuted.TLabel", background=c["bg_sidebar"], foreground=c["text_muted"])
    style.configure("SidebarSec.TLabel", background=c["bg_sidebar"], foreground=c["text_sec"])
    style.configure("SidebarAccent.TLabel", background=c["bg_sidebar"],
                    foreground=acc["accent"], font=f["mono_logo"])
    style.configure("TitlebarAccent.TLabel", background=c["bg_sidebar"],
                    foreground=acc["accent"], font=f["mono_logo"])
    style.configure("TitlebarName.TLabel", background=c["bg_sidebar"],
                    foreground=c["text"], font=f["sans_bold"])
    style.configure("StatusMono.TLabel", background=c["bg_sidebar"],
                    foreground=c["text_sec"], font=f["mono_sm"])
    style.configure("StatusMuted.TLabel", background=c["bg_sidebar"],
                    foreground=c["text_muted"], font=f["mono_sm"])
    # Uppercase mini field label (Friday .field-label)
    style.configure("Field.TLabel", background=c["bg"], foreground=c["text_sec"],
                    font=f["label"])
    style.configure("FieldSurface.TLabel", background=c["bg_surface"],
                    foreground=c["text_sec"], font=f["label"])
    # Page heading
    style.configure("H1.TLabel", background=c["bg"], foreground=c["text"],
                    font=f["sans_head"])
    style.configure("Warn.TLabel", background=c["bg"], foreground=c["warn"])

    # buttons
    # Ghost / secondary (Friday .btn-ghost / .conv-item / .chip)
    style.configure("TButton", background=c["bg_surface"], foreground=c["text_sec"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    focusthickness=0, padding=(12, 6), relief="flat", borderwidth=1,
                    font=f["sans"])
    style.map("TButton",
              background=[("active", c["bg_hover"]), ("pressed", c["bg_hover"])],
              foreground=[("active", c["text"])],
              bordercolor=[("active", acc["border_soft"])])

    # Primary (Friday .btn-primary)
    style.configure("Primary.TButton", background=acc["accent"], foreground="#ffffff",
                    bordercolor=acc["accent"], lightcolor=acc["accent"], darkcolor=acc["accent"],
                    padding=(14, 7), relief="flat", borderwidth=1,
                    font=f["sans"])
    style.map("Primary.TButton",
              background=[("active", _darken(acc["accent"])), ("pressed", acc["accent"])])

    # Danger (Friday .btn-danger: outlined, red on hover)
    style.configure("Danger.TButton", background=c["bg"], foreground=c["danger"],
                    bordercolor=c["danger"], lightcolor=c["danger"], darkcolor=c["danger"],
                    padding=(12, 6), relief="flat", borderwidth=1,
                    font=f["sans"])
    style.map("Danger.TButton",
              background=[("active", c["danger_bg"])])

    # Titlebar pill nav — tab-like buttons in the top bar
    style.configure("Nav.TButton", background=c["bg_sidebar"], foreground=c["text_sec"],
                    bordercolor=c["bg_sidebar"], lightcolor=c["bg_sidebar"],
                    darkcolor=c["bg_sidebar"], padding=(14, 6),
                    relief="flat", borderwidth=0, anchor="center",
                    font=f["sans"])
    style.map("Nav.TButton",
              background=[("active", c["bg_hover"])],
              foreground=[("active", c["text"])])
    style.configure("NavActive.TButton", background=c["bg_active"], foreground=acc["accent"],
                    bordercolor=c["bg_active"], lightcolor=c["bg_active"],
                    darkcolor=c["bg_active"], padding=(14, 6),
                    relief="flat", borderwidth=0, anchor="center",
                    font=f["sans_bold"])
    style.map("NavActive.TButton", background=[("active", c["bg_active"])])

    # Icon button (Friday .btn-icon — small square with border)
    style.configure("Icon.TButton", background=c["bg_surface"], foreground=c["text_sec"],
                    bordercolor=c["border"], padding=(8, 6),
                    relief="flat", borderwidth=1,
                    font=f["sans"])
    style.map("Icon.TButton",
              background=[("active", acc["soft"])],
              bordercolor=[("active", acc["border_soft"])],
              foreground=[("active", c["text"])])

    # inputs
    style.configure("TEntry", fieldbackground=c["bg_input"], foreground=c["text"],
                    bordercolor=c["border"], lightcolor=c["border"], darkcolor=c["border"],
                    insertcolor=c["text"], padding=6, relief="flat", borderwidth=1,
                    font=f["sans"])
    style.map("TEntry", bordercolor=[("focus", acc["accent"])])

    style.configure("TSpinbox", fieldbackground=c["bg_input"], foreground=c["text"],
                    background=c["bg_input"], bordercolor=c["border"],
                    arrowcolor=c["text_sec"], relief="flat", borderwidth=1, padding=4,
                    font=f["sans"])
    style.map("TSpinbox", bordercolor=[("focus", acc["accent"])])

    style.configure("TCheckbutton", background=c["bg"], foreground=c["text"],
                    indicatorcolor=c["bg_surface"], focuscolor=c["bg"],
                    font=f["sans"])
    style.map("TCheckbutton",
              indicatorcolor=[("selected", acc["accent"])],
              background=[("active", c["bg"])])

    # treeview (Friday table rows)
    size = int(settings.get("ui_font_size", 10))
    # Slightly taller rows: at 2.3× the font size the inline edit-Entry
    # (placed via row bbox on double-click) clipped descenders/ascenders.
    # 2.7× gives the editor enough headroom for both light and dark
    # themes without changing the visual rhythm of the table.
    style.configure("Treeview", background=c["bg_surface"], fieldbackground=c["bg_surface"],
                    foreground=c["text"], bordercolor=c["border"],
                    rowheight=int(size * 2.7), borderwidth=0, relief="flat",
                    font=f["sans_sm"])
    style.map("Treeview",
              background=[("selected", acc["soft"])],
              foreground=[("selected", c["text"])])
    style.configure("Treeview.Heading", background=c["bg_sidebar"], foreground=c["text_sec"],
                    bordercolor=c["border"], padding=(10, 8), relief="flat",
                    borderwidth=0, font=f["label"])
    style.map("Treeview.Heading", background=[("active", c["bg_hover"])])

    # progressbar (thin)
    style.configure("TProgressbar", background=acc["accent"], troughcolor=c["bg_hover"],
                    bordercolor=c["bg_hover"], lightcolor=acc["accent"],
                    darkcolor=acc["accent"], thickness=4)

    # scrollbars
    for orient in ("Vertical", "Horizontal"):
        s = f"{orient}.TScrollbar"
        style.configure(s, background=c["bg_hover"], troughcolor=c["bg"],
                        bordercolor=c["bg"], arrowcolor=c["text_muted"], relief="flat")
        style.map(s, background=[("active", c["text_muted"])])

    style.configure("TSeparator", background=c["border"])
    style.configure("TNotebook", background=c["bg"], borderwidth=0)
    style.configure("TNotebook.Tab", background=c["bg"], foreground=c["text_sec"],
                    padding=(14, 8), borderwidth=0, font=f["sans"])
    style.map("TNotebook.Tab",
              background=[("selected", c["bg"])],
              foreground=[("selected", acc["accent"])])


# =============================================================================
# app
# =============================================================================

class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Long Path Begone")
        self.geometry("1180x720")
        self.minsize(880, 560)

        self.settings = load_settings()
        self.theme = "light"
        self.current_page = "scan"
        self.C = THEMES[self.theme]
        self._f = _fonts(self.settings)
        apply_theme(self, self.theme, self.current_page, self.settings)

        self._log_q: queue.Queue[str] = queue.Queue()
        self._busy = False
        self.rows: dict[str, dict] = {}
        self._scan_cancel = False
        self._sort_state = {"col": "len", "desc": True}
        self._text_widgets: list[tk.Text] = []

        # created lazily by _build_main_pages
        self.pages: dict[str, ttk.Frame] = {}
        self.nav_buttons: dict[str, ttk.Button] = {}

        # placeholder so any _build_* that references sb_mode won't crash
        self.sb_mode = tk.StringVar(value="SCAN · IDLE")

        self._build_ui()
        self.after(50, lambda: apply_window_chrome(self, self.theme))
        self.after(80, self._drain_log)

    # ---- theme / settings -----------------------------------------------------

    def _toggle_theme(self):
        self.theme = "dark" if self.theme == "light" else "light"
        self.C = THEMES[self.theme]
        self._reapply_theme()
        apply_window_chrome(self, self.theme)
        if hasattr(self, "btn_theme"):
            self.btn_theme.configure(text="[D]" if self.theme == "light" else "[L]")

    def _reapply_theme(self):
        """Apply theme + settings and restyle manual widgets. Called on theme
        toggle, page switch, and any Settings change."""
        self._f = _fonts(self.settings)
        self.C = THEMES[self.theme]
        apply_theme(self, self.theme, self.current_page, self.settings)
        self._restyle_raw_widgets()
        self._draw_logo()

    def _set_page(self, name: str):
        if name == self.current_page:
            return
        self.current_page = name
        self._reapply_theme()
        for key, btn in self.nav_buttons.items():
            btn.configure(style="NavActive.TButton" if key == name else "Nav.TButton")
        for key, frame in self.pages.items():
            if key == name:
                frame.pack(fill="both", expand=True)
            else:
                frame.pack_forget()
        self._update_statusbar()

    def _restyle_raw_widgets(self):
        c = self.C
        f = self._f
        for t in self._text_widgets:
            try:
                t.configure(bg=c["bg_input"], fg=c["text"],
                            insertbackground=c["text"], font=f["mono"])
            except tk.TclError:
                pass
        for cv in getattr(self, "_scroll_canvases", []):
            try:
                cv.configure(bg=c["bg"])
            except tk.TclError:
                pass
        if hasattr(self, "log"):
            self.log.configure(bg=c["bg_code"], fg=c["text_sec"], font=f["mono_sm"])
            self.log.tag_configure("err", foreground=c["danger"])
            self.log.tag_configure("ok", foreground=c["ok"])
            self.log.tag_configure("dim", foreground=c["text_muted"])
        if hasattr(self, "err_log"):
            self.err_log.configure(bg=c["bg_code"], fg=c["danger"], font=f["mono_sm"])
        if hasattr(self, "tree"):
            self.tree.tag_configure("changed", background=c["changed"])
            over_fg = "#b91c1c" if self.theme == "light" else "#ff7a7a"
            self.tree.tag_configure("over260", foreground=over_fg)
            self.tree.tag_configure(
                "over260_changed", background=c["over260"],
                foreground=over_fg if self.theme == "light" else "#ffbdbd",
            )
        # Re-apply column visibility after any tree restyle
        if hasattr(self, "tree"):
            self._apply_column_visibility()

    # ---- logo ----------------------------------------------------------------

    def _draw_logo(self):
        """Draw the LPB badge: three coloured square tiles with white letters."""
        if not hasattr(self, "_logo_canvas"):
            return
        cv = self._logo_canvas
        acc = self.C.get("accent", "#7c5cfc")
        # Recolour canvas bg to match titlebar
        cv.configure(bg=self.C["bg_sidebar"])
        cv.delete("all")
        tile_w, tile_h, gap = 18, 22, 3
        letters = "LPB"
        for i, letter in enumerate(letters):
            x0 = i * (tile_w + gap)
            x1 = x0 + tile_w
            cv.create_rectangle(x0, 0, x1, tile_h, fill=acc, outline="")
            cv.create_text(
                x0 + tile_w // 2, tile_h // 2,
                text=letter, fill="#ffffff",
                font=(self._f["sans_bold"][0], self._f["sans_bold"][1] + 1, "bold"),
                anchor="center",
            )

    # ---- layout ---------------------------------------------------------------

    def _build_ui(self):
        self._build_titlebar()
        ttk.Separator(self).pack(fill="x")

        # Status bar first (reserves bottom) so body can't eat it.
        self._build_statusbar()

        # No sidebar any more — main fills everything left.
        main = ttk.Frame(self)
        main.pack(fill="both", expand=True)
        self.main = main

        self._build_main_pages(main)

    def _build_titlebar(self):
        bar = ttk.Frame(self, style="Titlebar.TFrame")
        bar.pack(fill="x")

        # Left: LPB logo — three coloured square tiles, accent-matched
        left = ttk.Frame(bar, style="Titlebar.TFrame")
        left.pack(side="left")
        self._logo_canvas = tk.Canvas(
            left, width=64, height=26,
            bg=self.C["bg_sidebar"], highlightthickness=0, bd=0,
        )
        self._logo_canvas.pack(side="left", padx=(14, 12), pady=7)
        self._draw_logo()

        # Center: page nav — Scan & Rename is the primary tab
        nav = ttk.Frame(bar, style="Titlebar.TFrame")
        nav.pack(side="left", padx=(12, 0))

        def add_nav(key, label):
            b = ttk.Button(nav, text=label,
                           style="NavActive.TButton" if key == self.current_page else "Nav.TButton",
                           command=lambda k=key: self._set_page(k))
            b.pack(side="left", padx=2)
            self.nav_buttons[key] = b

        add_nav("scan",     "🔍  Scan & Rename")
        add_nav("transfer", "📦  Transfer")

        # Right: settings + theme
        right = ttk.Frame(bar, style="Titlebar.TFrame")
        right.pack(side="right")
        self.btn_theme = ttk.Button(right, text="[D]" if self.theme == "light" else "[L]",
                                    style="Icon.TButton",
                                    command=self._toggle_theme, width=3)
        self.btn_theme.pack(side="right", padx=(0, 8), pady=5)
        ttk.Button(right, text="⚙", style="Icon.TButton",
                   command=self._open_settings, width=3).pack(
            side="right", padx=(0, 6), pady=5)

    def _build_main_pages(self, main):
        # Log pinned to the bottom of main so it never gets pushed off-screen;
        # pages above it fill the remaining space and scroll internally.
        self._build_log(main)

        pages_host = ttk.Frame(main)
        pages_host.pack(side="top", fill="both", expand=True)

        page_transfer = ttk.Frame(pages_host)
        page_scan = ttk.Frame(pages_host)
        self.pages["transfer"] = page_transfer
        self.pages["scan"] = page_scan

        # Transfer page: short, scrollable wrapper — if the window shrinks
        # we still want the controls reachable.
        inner_transfer = self._make_scrollable(page_transfer)
        self._build_transfer_page(inner_transfer)

        # Scan page: NO outer scroll wrapper. The page's centerpiece is the
        # Treeview, which has its own scrollbar. Wrapping the whole page in
        # another Canvas-scroller forced the tree to its default ~10-row
        # height and left the rest of the window blank. Building directly
        # into page_scan lets the table fill all available vertical space.
        self._build_scan_page(page_scan)

        # Show initial page — Scan & Rename is the default
        page_scan.pack(fill="both", expand=True)

    def _make_scrollable(self, parent) -> ttk.Frame:
        """Wrap `parent` with a scrollable canvas and return an inner frame."""
        c = self.C
        canvas = tk.Canvas(parent, bg=c["bg"], highlightthickness=0, borderwidth=0)
        vsb = ttk.Scrollbar(parent, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)

        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)

        inner = ttk.Frame(canvas)
        window_id = canvas.create_window((0, 0), window=inner, anchor="nw")

        def on_inner_configure(_=None):
            canvas.configure(scrollregion=canvas.bbox("all"))

        def on_canvas_configure(event):
            # Make inner frame match the canvas width so content flows horizontally.
            canvas.itemconfigure(window_id, width=event.width)

        inner.bind("<Configure>", on_inner_configure)
        canvas.bind("<Configure>", on_canvas_configure)

        # Mouse-wheel: scroll only the canvas whose page is currently shown.
        def on_mousewheel(event):
            # Only scroll if this canvas's page is the one visible.
            if parent.winfo_ismapped():
                canvas.yview_scroll(int(-event.delta / 120), "units")
                return "break"
            return None

        # Bind to the canvas and all its descendants so wheel works anywhere on the page.
        def bind_wheel(widget):
            widget.bind("<MouseWheel>", on_mousewheel, add="+")

        bind_wheel(canvas)
        # Rebind on children after build by traversing the tree once idle.
        def bind_all_descendants(_=None):
            for w in self._walk(inner):
                bind_wheel(w)
        inner.bind("<Map>", bind_all_descendants, add="+")
        # Also bind periodically after widgets are added (one-shot after build).
        self.after(200, bind_all_descendants)

        # Track canvas so theme toggle can repaint its bg.
        if not hasattr(self, "_scroll_canvases"):
            self._scroll_canvases = []
        self._scroll_canvases.append(canvas)

        return inner

    @staticmethod
    def _walk(widget):
        yield widget
        for child in widget.winfo_children():
            yield from App._walk(child)

    def _build_log(self, main):
        # Two-row log strip pinned to the bottom of the main area.
        #
        # Row 1 — ACTIVITY LOG: every message auto-saved to activity_log.txt.
        #   [Clear log] wipes both the widget and the file.
        #
        # Row 2 — ERROR LOG: error/fail messages only, auto-saved to
        #   error_log.txt.  [Clear errors] wipes widget + file.
        c = self.C
        ttk.Separator(main).pack(fill="x")
        log_wrap = ttk.Frame(main)
        log_wrap.pack(fill="x")

        # ── Activity log bar ──────────────────────────────────────────────
        act_bar = ttk.Frame(log_wrap)
        act_bar.pack(fill="x", padx=18, pady=(4, 1))
        ttk.Label(act_bar, text="ACTIVITY LOG", style="Field.TLabel").pack(side="left")
        ttk.Label(act_bar, text=f"  → {ACTIVITY_LOG_FILE}",
                  style="Muted.TLabel", font=self._f["mono_sm"]).pack(side="left")
        ttk.Button(act_bar, text="Clear log", style="TButton",
                   command=self._clear_log).pack(side="right")

        self.log = tk.Text(log_wrap, height=3, wrap="none", state="disabled",
                           bg=c["bg_code"], fg=c["text_sec"],
                           insertbackground=c["text"],
                           relief="flat", borderwidth=0, highlightthickness=0,
                           font=self._f["mono_sm"], padx=12, pady=4)
        self.log.pack(fill="x", padx=18, pady=(0, 4))
        self.log.tag_configure("err", foreground=c["danger"])
        self.log.tag_configure("ok", foreground=c["ok"])
        self.log.tag_configure("dim", foreground=c["text_muted"])

        # ── Error log bar ─────────────────────────────────────────────────
        ttk.Separator(log_wrap).pack(fill="x", padx=18)
        err_bar = ttk.Frame(log_wrap)
        err_bar.pack(fill="x", padx=18, pady=(3, 1))
        ttk.Label(err_bar, text="ERROR LOG", style="Field.TLabel").pack(side="left")
        ttk.Label(err_bar, text=f"  → {ERROR_LOG_FILE}",
                  style="Muted.TLabel", font=self._f["mono_sm"]).pack(side="left")
        ttk.Button(err_bar, text="Clear errors", style="TButton",
                   command=self._clear_error_log).pack(side="right")

        self.err_log = tk.Text(log_wrap, height=2, wrap="none", state="disabled",
                               bg=c["bg_code"], fg=c["danger"],
                               insertbackground=c["text"],
                               relief="flat", borderwidth=0, highlightthickness=0,
                               font=self._f["mono_sm"], padx=12, pady=4)
        self.err_log.pack(fill="x", padx=18, pady=(0, 6))

    def _build_statusbar(self):
        sb = ttk.Frame(self, style="Statusbar.TFrame")
        sb.pack(side="bottom", fill="x")
        ttk.Separator(self).pack(side="bottom", fill="x")
        self.sb_state_var = tk.StringVar(value="● idle")
        self.sb_info_var = tk.StringVar(value="ready")
        ttk.Label(sb, textvariable=self.sb_state_var,
                  style="StatusMono.TLabel").pack(side="left", padx=(14, 10), pady=4)
        ttk.Label(sb, text="·", style="StatusMuted.TLabel").pack(side="left")
        ttk.Label(sb, textvariable=self.sb_info_var,
                  style="StatusMono.TLabel").pack(side="left", padx=10)

        self.sb_right_var = tk.StringVar(value="long paths · \\\\?\\")
        ttk.Label(sb, textvariable=self.sb_right_var,
                  style="StatusMuted.TLabel").pack(side="right", padx=14)

    def _update_statusbar(self, state: str | None = None, info: str | None = None):
        if state is not None:
            self.sb_state_var.set(state)
        if info is not None:
            self.sb_info_var.set(info)
        mode = "TRANSFER" if self.current_page == "transfer" else "SCAN"
        bite = "BUSY" if self._busy else "IDLE"
        self.sb_mode.set(f"{mode} · {bite}")

    # ---- transfer page --------------------------------------------------------

    def _build_transfer_page(self, page):
        c = self.C
        inner = ttk.Frame(page)
        inner.pack(fill="both", expand=True, padx=28, pady=20)

        ttk.Label(inner, text="Transfer", style="H1.TLabel").pack(anchor="w")

        # Experimental warning banner
        warn_frame = ttk.Frame(inner, style="Surface.TFrame")
        warn_frame.pack(fill="x", pady=(6, 18))
        ttk.Label(warn_frame,
                  text="⚠️  EXPERIMENTAL",
                  style="Warn.TLabel").pack(anchor="w", padx=12, pady=(8, 2))
        ttk.Label(warn_frame,
                  text="Transfer has been lightly tested. It works, but Copy is much safer "
                       "than Move — a copy can be verified and the source deleted "
                       "manually, while a failed move may leave data in an inconsistent state.",
                  style="Muted.TLabel", wraplength=680, justify="left").pack(
            anchor="w", padx=12, pady=(0, 8))

        ttk.Label(inner, text="TARGETS", style="Field.TLabel").pack(anchor="w", pady=(4, 4))

        self.targets = tk.Text(inner, height=7, wrap="none",
                               bg=c["bg_input"], fg=c["text"],
                               insertbackground=c["text"],
                               relief="flat", borderwidth=1, highlightthickness=1,
                               highlightbackground=c["border"],
                               highlightcolor=ACCENTS["transfer"]["accent"],
                               font=self._f["mono"], padx=10, pady=8)
        self._text_widgets.append(self.targets)
        self.targets.pack(fill="x")

        btns = ttk.Frame(inner)
        btns.pack(fill="x", pady=(8, 0))
        ttk.Button(btns, text="Add files…", command=self._add_files).pack(side="left")
        ttk.Button(btns, text="Add folder…", command=self._add_folder).pack(side="left", padx=6)
        ttk.Button(btns, text="Clear",
                   command=lambda: self.targets.delete("1.0", "end")).pack(side="left")

        ttk.Label(inner, text="DESTINATION  (COPY / MOVE)",
                  style="Field.TLabel").pack(anchor="w", pady=(22, 4))
        dst = ttk.Frame(inner)
        dst.pack(fill="x")
        self.dest_var = tk.StringVar()
        ttk.Entry(dst, textvariable=self.dest_var, font=self._f["mono"]).pack(
            side="left", fill="x", expand=True)
        ttk.Button(dst, text="Browse…", command=self._pick_dest).pack(side="left", padx=(6, 0))

        opt = ttk.Frame(inner)
        opt.pack(fill="x", pady=(10, 0))
        self.overwrite = tk.BooleanVar(value=False)
        ttk.Checkbutton(opt, text="Overwrite existing files at destination",
                        variable=self.overwrite).pack(anchor="w")

        ttk.Separator(inner).pack(fill="x", pady=18)

        actions = ttk.Frame(inner)
        actions.pack(fill="x")
        self.btn_copy = ttk.Button(actions, text="Copy", style="Primary.TButton",
                                   command=lambda: self._run_transfer("copy"))
        self.btn_move = ttk.Button(actions, text="Move", style="Primary.TButton",
                                   command=lambda: self._run_transfer("move"))
        self.btn_delete = ttk.Button(actions, text="Delete", style="Danger.TButton",
                                     command=lambda: self._run_transfer("delete"))
        self.btn_copy.pack(side="left")
        self.btn_move.pack(side="left", padx=8)
        self.btn_delete.pack(side="right")

    # ---- scan page ------------------------------------------------------------

    def _build_scan_page(self, page):
        c = self.C
        inner = ttk.Frame(page)
        inner.pack(fill="both", expand=True, padx=28, pady=20)

        # Root selector line
        ttk.Label(inner, text="ROOT", style="Field.TLabel").pack(anchor="w", pady=(0, 4))
        line1 = ttk.Frame(inner)
        line1.pack(fill="x")
        self.root_var = tk.StringVar()
        ttk.Entry(line1, textvariable=self.root_var, font=self._f["mono"]).pack(
            side="left", fill="x", expand=True)
        ttk.Button(line1, text="Browse…", command=self._pick_root).pack(side="left", padx=(6, 0))
        self.btn_scan = ttk.Button(line1, text="Scan", style="Primary.TButton",
                                   command=self._scan)
        self.btn_scan.pack(side="left", padx=(6, 0))
        self.btn_cancel_scan = ttk.Button(line1, text="Cancel", command=self._cancel_scan)
        self.btn_cancel_scan.pack(side="left", padx=(6, 0))
        self.btn_cancel_scan.state(["disabled"])

        # Progress + status
        prog = ttk.Frame(inner)
        prog.pack(fill="x", pady=(10, 0))
        self.scan_progress = ttk.Progressbar(prog, mode="indeterminate", length=260)
        self.scan_progress.pack(side="left")
        self.scan_status = tk.StringVar(value="Idle.")
        ttk.Label(prog, textvariable=self.scan_status,
                  style="Sec.TLabel", font=self._f["mono_sm"]).pack(side="left", padx=12)

        # Filters — all on one row: MIN LENGTH + folder/file kind toggles.
        # Used to be three vertical lines (label / spinbox / kinds), which
        # was pure waste; one inline strip saves two rows for the table.
        ttk.Separator(inner).pack(fill="x", pady=10)

        frow = ttk.Frame(inner)
        frow.pack(fill="x")
        ttk.Label(frow, text="MIN LENGTH", style="Field.TLabel").pack(side="left")
        # Default 250 — the legacy MAX_PATH limit is 260, so 250 surfaces
        # exactly the rows that are interesting (close to the limit or
        # already past it). 0 still works to show everything.
        self.min_len = tk.IntVar(value=250)
        sp = ttk.Spinbox(frow, from_=0, to=32000, width=7, textvariable=self.min_len,
                         command=self._refresh_view, font=self._f["mono_sm"])
        sp.pack(side="left", padx=(8, 18))
        sp.bind("<KeyRelease>", lambda _: self._refresh_view())

        self.show_dirs = tk.BooleanVar(value=True)
        self.show_files = tk.BooleanVar(value=True)
        ttk.Checkbutton(frow, text="Folders", variable=self.show_dirs,
                        command=self._refresh_view).pack(side="left")
        ttk.Checkbutton(frow, text="Files", variable=self.show_files,
                        command=self._refresh_view).pack(side="left", padx=(14, 0))

        ttk.Separator(frow, orient="vertical").pack(side="left", fill="y", padx=(18, 0), pady=3)
        ttk.Label(frow, text="MAX FILES", style="Field.TLabel").pack(side="left", padx=(14, 0))
        self.scan_limit = tk.IntVar(value=0)

        def _sanitise_limit(*_):
            """If the spinbox is left empty or non-numeric, fall back to 0."""
            try:
                v = int(sp_lim.get())
                if v < 0:
                    raise ValueError
            except (ValueError, tk.TclError):
                self.scan_limit.set(0)
                sp_lim.set(0)

        sp_lim = ttk.Spinbox(frow, from_=0, to=10_000_000, width=9,
                             textvariable=self.scan_limit,
                             font=self._f["mono_sm"])
        sp_lim.pack(side="left", padx=(8, 0))
        sp_lim.bind("<FocusOut>", _sanitise_limit)
        sp_lim.bind("<Return>",   _sanitise_limit)
        ttk.Label(frow, text="(0 = unlimited)", style="Muted.TLabel",
                  font=self._f["sans_sm"]).pack(side="left", padx=(6, 0))

        # Split path fields — when checked, replace each "\" with a visible
        # breadcrumb separator so segments stand apart at a glance. When
        # unchecked, swap the regular backslash for FULLWIDTH REVERSE
        # SOLIDUS (U+FF3C "＼") so it reads as a heavier glyph than a
        # standard `\`. tkinter's Treeview can't bold individual chars
        # inside a cell, so this is the closest "make \ bold" we can get
        # without reimplementing the row renderer on a Canvas.
        self.split_fields = tk.BooleanVar(value=False)
        ttk.Checkbutton(frow,
                        text="Split path  (A › B › C vs A＼b)",
                        variable=self.split_fields,
                        command=self._refresh_view).pack(side="left", padx=(14, 0))

        # Find/replace
        ttk.Separator(inner).pack(fill="x", pady=16)
        fr = ttk.Frame(inner)
        fr.pack(fill="x")
        ttk.Label(fr, text="FIND", style="Field.TLabel").grid(row=0, column=0, sticky="w")
        self.find_var = tk.StringVar()
        self.find_var.trace_add("write", lambda *_: self._refresh_view())
        ttk.Entry(fr, textvariable=self.find_var, font=self._f["mono"]).grid(
            row=1, column=0, sticky="ew", padx=(0, 8))

        ttk.Label(fr, text="REPLACE", style="Field.TLabel").grid(row=0, column=1, sticky="w")
        self.replace_var = tk.StringVar()
        ttk.Entry(fr, textvariable=self.replace_var, font=self._f["mono"]).grid(
            row=1, column=1, sticky="ew", padx=(0, 8))

        opts_col = ttk.Frame(fr)
        opts_col.grid(row=1, column=2, sticky="w")
        self.regex_on = tk.BooleanVar(value=False)
        self.case_sensitive = tk.BooleanVar(value=True)
        ttk.Checkbutton(opts_col,
                        text=".* regex",
                        variable=self.regex_on,
                        command=self._refresh_view).pack(side="left")
        ttk.Checkbutton(opts_col,
                        text="Aa case",
                        variable=self.case_sensitive,
                        command=self._refresh_view).pack(side="left", padx=(10, 0))

        ttk.Button(fr, text="Apply to visible", style="Primary.TButton",
                   command=self._bulk_replace).grid(row=1, column=3, padx=(10, 0))

        fr.columnconfigure(0, weight=1)
        fr.columnconfigure(1, weight=1)

        self.regex_status = tk.StringVar(value="")
        ttk.Label(inner, textvariable=self.regex_status,
                  style="Warn.TLabel", font=self._f["mono_sm"]).pack(anchor="w", pady=(4, 0))

        # Table
        table_wrap = ttk.Frame(inner)
        table_wrap.pack(fill="both", expand=True, pady=(12, 0))

        cols = ("len", "newlen", "kind", "orig", "new")
        tree = ttk.Treeview(table_wrap, columns=cols, show="headings", selectmode="extended")
        # Path columns are intentionally wide (600 px each) so their total
        # always exceeds typical window widths — this is what makes the
        # horizontal scrollbar functional.  stretch=False on every column
        # prevents tkinter from expanding them to fit the widget (which
        # would eliminate the overflow and break xview scrolling).
        for col_id, title, w, anchor in [
            ("len",    "LENGTH",                           80, "e"),
            ("newlen", "NEW LENGTH",                      100, "e"),
            ("kind",   "KIND",                             64, "center"),
            ("orig",   "ORIGINAL PATH",                   600, "w"),
            ("new",    "NEW PATH  (double-click to edit)",600, "w"),
        ]:
            tree.heading(col_id, text=title, command=lambda cc=col_id: self._sort_by(cc))
            tree.column(col_id, width=w, anchor=anchor, stretch=False)

        ysb = ttk.Scrollbar(table_wrap, orient="vertical", command=tree.yview)
        xsb = ttk.Scrollbar(table_wrap, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=ysb.set, xscrollcommand=xsb.set)
        tree.grid(row=0, column=0, sticky="nsew")
        ysb.grid(row=0, column=1, sticky="ns")
        xsb.grid(row=1, column=0, sticky="ew")
        table_wrap.rowconfigure(0, weight=1)
        table_wrap.columnconfigure(0, weight=1)

        tree.tag_configure("changed", background=c["changed"])
        tree.tag_configure("over260", foreground="#b91c1c")
        tree.tag_configure("over260_changed", background=c["over260"], foreground="#b91c1c")
        tree.bind("<Double-1>", self._on_double_click)
        self.tree = tree
        self._apply_column_visibility()

        # Bottom actions
        bar = ttk.Frame(inner)
        bar.pack(fill="x", pady=(12, 0))
        self.scan_summary = tk.StringVar(value="No scan yet.")
        ttk.Label(bar, textvariable=self.scan_summary,
                  style="Sec.TLabel", font=self._f["mono_sm"]).pack(side="left")
        self.btn_apply = ttk.Button(bar, text="Apply renames", style="Primary.TButton",
                                    command=self._apply_renames)
        self.btn_apply.pack(side="right")
        ttk.Button(bar, text="Reset edits", command=self._reset_edits).pack(
            side="right", padx=(0, 8))

    # ---- log ------------------------------------------------------------------

    def _log(self, msg: str):
        self._log_q.put(msg)

    def _drain_log(self):
        try:
            while True:
                msg = self._log_q.get_nowait()
                low = msg.lower()
                is_err = (
                    low.startswith(("error", "fail", "warn"))
                    or " error " in low or " fail" in low
                )
                tag = ""
                if is_err:
                    tag = "err"
                elif low.startswith("---"):
                    tag = "dim"
                elif low.startswith(("deleted", "copied", "moved", "renamed")):
                    tag = "ok"

                # Activity log widget + file
                self.log.configure(state="normal")
                self.log.insert("end", msg + "\n", tag)
                self.log.see("end")
                self.log.configure(state="disabled")
                self._append_to_file(ACTIVITY_LOG_FILE, msg)

                # Error log widget + file (errors only)
                if is_err and hasattr(self, "err_log"):
                    self.err_log.configure(state="normal")
                    self.err_log.insert("end", msg + "\n")
                    self.err_log.see("end")
                    self.err_log.configure(state="disabled")
                    self._append_to_file(ERROR_LOG_FILE, msg)

        except queue.Empty:
            pass
        self.after(80, self._drain_log)

    @staticmethod
    def _append_to_file(path: str, msg: str) -> None:
        """Append a single log line to a file, silently ignoring I/O errors."""
        try:
            with open(path, "a", encoding="utf-8") as f:
                f.write(msg + "\n")
        except OSError:
            pass

    def _clear_log(self):
        """Clear the activity log widget and truncate activity_log.txt."""
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        try:
            open(ACTIVITY_LOG_FILE, "w", encoding="utf-8").close()
        except OSError:
            pass

    def _clear_error_log(self):
        """Clear the error log widget and truncate error_log.txt."""
        if hasattr(self, "err_log"):
            self.err_log.configure(state="normal")
            self.err_log.delete("1.0", "end")
            self.err_log.configure(state="disabled")
        try:
            open(ERROR_LOG_FILE, "w", encoding="utf-8").close()
        except OSError:
            pass

    # ---- transfer actions -----------------------------------------------------

    def _add_files(self):
        paths = filedialog.askopenfilenames(title="Select file(s)")
        if paths:
            self.targets.insert("end", "\n".join(paths) + "\n")

    def _add_folder(self):
        p = filedialog.askdirectory(title="Select folder")
        if p:
            self.targets.insert("end", p + "\n")

    def _pick_dest(self):
        p = filedialog.askdirectory(title="Destination folder")
        if p:
            self.dest_var.set(p)

    def _gather_targets(self):
        lines = self.targets.get("1.0", "end").splitlines()
        return [from_extended(line.strip().strip('"')) for line in lines if line.strip()]

    def _set_transfer_busy(self, busy):
        state = ["disabled"] if busy else ["!disabled"]
        for b in (self.btn_copy, self.btn_move, self.btn_delete):
            b.state(state)

    def _run_transfer(self, op: str):
        if self._busy:
            messagebox.showinfo("Busy", "Another operation is running.")
            return
        targets = self._gather_targets()
        if not targets:
            messagebox.showwarning("No targets", "Add at least one file or folder.")
            return
        dest = self.dest_var.get().strip().strip('"')
        if op in ("copy", "move"):
            if not dest:
                messagebox.showwarning("No destination", "Pick a destination folder.")
                return
            if not exists(dest):
                if not messagebox.askyesno("Create destination?",
                                           f"{dest} does not exist. Create it?"):
                    return
                try:
                    make_dir(dest)
                except OSError as e:
                    messagebox.showerror("Error", str(e))
                    return
        if op == "delete":
            if not messagebox.askyesno("Confirm delete",
                                       f"Permanently delete {len(targets)} item(s)?\n"
                                       "This bypasses the Recycle Bin."):
                return

        self._busy = True
        self._set_transfer_busy(True)
        self._update_statusbar(state="● busy", info=f"{op} · {len(targets)} item(s)")
        self._log(f"--- {op.upper()} starting ({len(targets)} item(s)) ---")

        def worker():
            try:
                for src in targets:
                    try:
                        if op == "delete":
                            recursive_delete(src, self._log)
                        else:
                            target = os.path.join(dest, os.path.basename(src.rstrip("\\/")))
                            recursive_copy(src, target,
                                           overwrite=self.overwrite.get(),
                                           move=(op == "move"),
                                           log=self._log)
                    except Exception as e:
                        self._log(f"FAILED ({src}): {e}")
                self._log(f"--- {op.upper()} done ---")
            finally:
                def _finish():
                    self._set_transfer_busy(False)
                    self._busy = False
                    self._update_statusbar(state="● idle", info=f"{op} done")
                self.after(0, _finish)

        threading.Thread(target=worker, daemon=True).start()

    # ---- scan -----------------------------------------------------------------

    def _pick_root(self):
        p = filedialog.askdirectory(title="Folder to scan")
        if p:
            self.root_var.set(p)

    def _scan(self):
        if self._busy:
            return
        root = self.root_var.get().strip().strip('"')
        if not root or not exists(root):
            messagebox.showerror("Bad root", "Pick an existing folder.")
            return

        self._busy = True
        self._scan_cancel = False
        self.btn_scan.state(["disabled"])
        self.btn_cancel_scan.state(["!disabled"])
        self.btn_apply.state(["disabled"])
        self.rows.clear()
        self.tree.delete(*self.tree.get_children())
        self.scan_status.set("Scanning…")
        self.scan_progress.configure(mode="indeterminate")
        self.scan_progress.start(12)
        self._update_statusbar(state="● scanning", info="walking tree")

        ui_q: queue.Queue = queue.Queue()

        def on_progress(count, path):
            ui_q.put(("p", count, path))

        def pump():
            try:
                while True:
                    kind, *rest = ui_q.get_nowait()
                    if kind == "p":
                        count, path = rest
                        short = path if len(path) < 90 else "…" + path[-87:]
                        self.scan_status.set(f"Scanning…  {count:,} items   {short}")
                    elif kind == "done":
                        total = rest[0]
                        self.scan_progress.stop()
                        self.scan_progress.configure(mode="determinate", value=100, maximum=100)
                        self.scan_status.set(f"Done. {total:,} items.")
                        self.btn_scan.state(["!disabled"])
                        self.btn_cancel_scan.state(["disabled"])
                        self.btn_apply.state(["!disabled"])
                        self._busy = False
                        self._update_statusbar(state="● idle", info=f"{total:,} items scanned")
                        self._refresh_view()
                        return
                    elif kind == "err":
                        self._log(f"Scan error: {rest[0]}")
            except queue.Empty:
                pass
            self.after(60, pump)

        def worker():
            total = 0
            try:
                limit = self.scan_limit.get()
            except (tk.TclError, ValueError):
                limit = 0
            try:
                for i, (full, isdir) in enumerate(
                        scan_tree(root, progress=on_progress,
                                  cancel=lambda: self._scan_cancel)):
                    self.rows[str(i)] = {
                        "orig": full, "new": full,
                        "is_dir": isdir,
                    }
                    total = i + 1
                    if limit > 0 and total >= limit:
                        self._scan_cancel = True   # stops scan_tree on next yield
                        break
                self._log(f"Scanned {total:,} items under {root}"
                          + (" (limit reached)" if limit > 0 and total >= limit
                             else " (cancelled)" if self._scan_cancel else ""))
            except Exception as e:
                ui_q.put(("err", str(e)))
            finally:
                ui_q.put(("done", total))

        threading.Thread(target=worker, daemon=True).start()
        self.after(60, pump)

    def _cancel_scan(self):
        self._scan_cancel = True
        self.scan_status.set("Cancelling…")

    # ---- view -----------------------------------------------------------------

    def _compile_find(self):
        pat = self.find_var.get()
        if not pat:
            self.regex_status.set("")
            return None
        try:
            if self.regex_on.get():
                flags = 0 if self.case_sensitive.get() else re.IGNORECASE
                self.regex_status.set("")
                return re.compile(pat, flags)
            else:
                # literal string, case handled manually
                self.regex_status.set("")
                return pat
        except re.error as e:
            self.regex_status.set(f"Invalid regex: {e}")
            return None

    def _match(self, pat, text: str) -> bool:
        if pat is None:
            return True
        if isinstance(pat, re.Pattern):
            return pat.search(text) is not None
        if self.case_sensitive.get():
            return pat in text
        return pat.lower() in text.lower()

    def _refresh_view(self):
        self.tree.delete(*self.tree.get_children())
        min_len = self.min_len.get() or 0
        show_d = self.show_dirs.get()
        show_f = self.show_files.get()
        pat = self._compile_find()

        items = []
        for rid, row in self.rows.items():
            if row["is_dir"] and not show_d:
                continue
            if not row["is_dir"] and not show_f:
                continue
            if max(len(row["orig"]), len(row["new"])) < min_len:
                continue
            if pat is not None and not (self._match(pat, row["orig"]) or self._match(pat, row["new"])):
                continue
            items.append((rid, row))

        col = self._sort_state["col"]
        desc = self._sort_state["desc"]

        def key(item):
            _, r = item
            return {
                "len": len(r["orig"]),
                "newlen": len(r["new"]),
                "kind": r["is_dir"],
                "orig": r["orig"].lower(),
                "new": r["new"].lower(),
            }[col]

        items.sort(key=key, reverse=desc)

        # Path display transform — see the "Split path fields" checkbox.
        # Length columns still use the canonical raw path so they keep
        # showing the true Win32 character count. Inline editing also
        # operates on the raw `row["new"]`, so the display transform is
        # render-only.
        split = bool(self.split_fields.get())
        if split:
            sep = "  ›  "   # "  ›  "
            disp = lambda p: p.replace("\\", sep)
        else:
            # FULLWIDTH REVERSE SOLIDUS — a visually heavier `\` glyph,
            # the closest tkinter Treeview can offer to "bold backslash".
            disp = lambda p: p.replace("\\", "＼")

        for rid, r in items:
            over = len(r["orig"]) >= 260 or len(r["new"]) >= 260
            changed = r["new"] != r["orig"]
            tag = ""
            if over and changed:
                tag = "over260_changed"
            elif over:
                tag = "over260"
            elif changed:
                tag = "changed"
            self.tree.insert(
                "", "end", iid=rid,
                values=(len(r["orig"]), len(r["new"]),
                        "DIR" if r["is_dir"] else "FILE",
                        disp(r["orig"]), disp(r["new"])),
                tags=(tag,) if tag else (),
            )

        total = len(self.rows)
        changed_n = sum(1 for r in self.rows.values() if r["new"] != r["orig"])
        self.scan_summary.set(
            f"{total:,} scanned · {len(items):,} shown · {changed_n:,} edited")

    def _sort_by(self, col):
        if self._sort_state["col"] == col:
            self._sort_state["desc"] = not self._sort_state["desc"]
        else:
            self._sort_state["col"] = col
            self._sort_state["desc"] = col in ("len", "newlen")
        self._refresh_view()

    # ---- inline edit + bulk replace -------------------------------------------

    def _on_double_click(self, event):
        # Edit on double-click for the "new" column. Resolve the click's
        # column by **name**, not positional `#N` — once the user hides
        # columns via Settings, displaycolumns no longer matches the
        # full column tuple, and a hardcoded `#5` either points at the
        # wrong column or doesn't exist at all.
        if self.tree.identify("region", event.x, event.y) != "cell":
            return
        col_token = self.tree.identify_column(event.x)   # like "#1", "#2", …
        try:
            disp_idx = int(col_token.lstrip("#")) - 1
        except ValueError:
            return
        # `displaycolumns` is "#all" until we set it, then a tuple of ids.
        displayed = self.tree.cget("displaycolumns")
        if displayed in ("", "#all", ("#all",)):
            displayed = self.tree.cget("columns")
        if not (0 <= disp_idx < len(displayed)):
            return
        col_name = displayed[disp_idx]
        if col_name != "new":
            return

        rid = self.tree.identify_row(event.y)
        if not rid:
            return
        bbox = self.tree.bbox(rid, col_token)
        if not bbox:
            return
        x, y, w, h = bbox

        # Use a raw tk.Entry rather than ttk.Entry: ttk's themed padding
        # eats vertical space and clips text top/bottom inside a tight row
        # bbox. tk.Entry with bd=0 + matching font + a 1-px bleed above
        # and below shows the glyph cell intact.
        c = self.C
        entry = tk.Entry(
            self.tree,
            font=self._f["sans"],
            bd=0,
            relief="flat",
            highlightthickness=1,
            highlightcolor=c.get("text", "#000"),
            highlightbackground=c.get("border", "#ccc"),
            bg=c.get("bg_surface", "#fff"),
            fg=c.get("text", "#000"),
            insertbackground=c.get("text", "#000"),
        )
        entry.insert(0, self.rows[rid]["new"])
        entry.select_range(0, "end")
        entry.focus_set()
        # Bleed by 1 px on top/bottom so the ascender/descender of the
        # font glyph isn't clipped by the row's tight bbox.
        entry.place(x=x, y=y - 1, width=w, height=h + 2)

        _committed = [False]

        def commit(_=None):
            if _committed[0]:
                return
            _committed[0] = True
            val = entry.get()
            entry.destroy()
            self.rows[rid]["new"] = val
            self._refresh_view()

        def cancel(_=None):
            _committed[0] = True
            entry.destroy()

        entry.bind("<Return>", commit)
        entry.bind("<FocusOut>", commit)
        entry.bind("<Escape>", cancel)

    def _bulk_replace(self):
        find = self.find_var.get()
        if not find:
            return
        repl = self.replace_var.get()
        use_regex = self.regex_on.get()
        case = self.case_sensitive.get()

        if use_regex:
            try:
                pat = re.compile(find, 0 if case else re.IGNORECASE)
            except re.error as e:
                self.regex_status.set(f"Invalid regex: {e}")
                return
            # Normalise replacement: convert $1/$2/… and ${1}/${name} to the
            # \1/\g<1>/\g<name> syntax that Python's re module expects, so
            # users can write either convention.
            repl = re.sub(r'\$\{(\d+)\}', r'\\g<\1>', repl)
            repl = re.sub(r'\$\{([A-Za-z_]\w*)\}', r'\\g<\1>', repl)
            repl = re.sub(r'\$(\d+)', r'\\\1', repl)
            # Validate the replacement string early (e.g. \1 with no group)
            # by doing a no-op trial sub before touching any rows.
            try:
                pat.sub(repl, "")
            except re.error as e:
                self.regex_status.set(f"Invalid replacement: {e}")
                return
        else:
            pat = None
            if not case:
                # Compile the escaped literal pattern once, outside the loop.
                _case_pat = re.compile(re.escape(find), re.IGNORECASE)

        visible = self.tree.get_children()
        n = 0
        for rid in visible:
            row = self.rows[rid]
            new = row["new"]
            if use_regex:
                try:
                    updated, k = pat.subn(repl, new)
                except re.error as e:
                    self.regex_status.set(f"Invalid replacement: {e}")
                    return
                if k:
                    row["new"] = updated
                    n += 1
            else:
                if case:
                    if find in new:
                        row["new"] = new.replace(find, repl)
                        n += 1
                else:
                    # Use a lambda so `repl` is treated as a literal string —
                    # prevents backslashes in the replacement (e.g. Windows
                    # paths like C:\new\folder) from being interpreted as
                    # regex escape sequences by re.subn.
                    updated, k = _case_pat.subn(lambda m: repl, new)
                    if k:
                        row["new"] = updated
                        n += 1
        self._log(f"Find/replace touched {n} visible row(s).")
        self._refresh_view()

    def _reset_edits(self):
        for r in self.rows.values():
            r["new"] = r["orig"]
        self._refresh_view()

    # ---- apply renames --------------------------------------------------------

    def _apply_renames(self):
        if self._busy:
            return
        edits = [r for r in self.rows.values() if r["new"] != r["orig"]]
        if not edits:
            messagebox.showinfo("Nothing to do", "No edited paths.")
            return

        seen: set[str] = set()
        for r in edits:
            t = r["new"]
            if not t.strip():
                messagebox.showerror("Bad target", f"Empty new path for:\n{r['orig']}")
                return
            if t in seen:
                messagebox.showerror("Duplicate target",
                                     f"Two items map to:\n{t}")
                return
            seen.add(t)

        if not messagebox.askyesno("Confirm", f"Apply {len(edits)} rename(s)?"):
            return

        self._busy = True
        self.btn_apply.state(["disabled"])

        mapping = sorted(((r["orig"], r["new"]) for r in edits),
                         key=lambda kv: (kv[0].count(os.sep), kv[0]))

        def worker():
            # Rename each path one segment at a time, left to right.
            #
            # For a row like  A\B\C\D\E\F\G  ->  A\B\CC\DD\E\F\GG  we emit
            # three physical renames in order:
            #   1. A\B\C        ->  A\B\CC
            #   2. A\B\CC\D     ->  A\B\CC\DD
            #   3. A\B\CC\DD\E\F\G  ->  A\B\CC\DD\E\F\GG
            #
            # This is safe even when intermediate directories are NOT in
            # `mapping` (e.g. the user edited only a leaf row): we discover
            # and rename them on the way down.
            #
            # done_renames  (src_path -> dst_path)  serves two purposes:
            #
            #   1. Deduplication — when a bulk find/replace puts every item
            #      under the same dirs into `mapping`, the shared parent rename
            #      only fires once; sibling rows skip it via the dict lookup.
            #
            #   2. Path reconciliation — a later row may have a `new` path that
            #      was computed before an earlier row's rename was known.
            #      Example:
            #        row A:  A\B\C\D\E        ->  A\B\C\D\EE   (E renamed)
            #        row B:  A\B\C\D\E\F\G    ->  A\B\C\D\E\FF\G  (F renamed,
            #                                     but new still says E not EE)
            #      Before comparing segment i, we check whether that prefix
            #      path was already renamed and, if so, update both cur_parts
            #      and new_parts to route through the new name.  This
            #      naturally handles chains (C->CC, then CC->CCC) because
            #      cur_parts is rebuilt from scratch at each segment.
            #
            # Because each individual rename moves exactly one path segment,
            # make_dir is never needed — the parent always exists after the
            # preceding step.

            done_renames: dict[str, str] = {}   # src_path -> dst_path
            ok = fail = 0

            for orig, new in mapping:
                orig_parts = orig.split(os.sep)
                new_parts  = list(new.split(os.sep))   # mutable: reconciled below

                if len(orig_parts) != len(new_parts):
                    self._log(
                        f"FAIL: path depth changed (not a rename): "
                        f"{orig}  ->  {new}"
                    )
                    fail += 1
                    continue

                cur_parts = list(orig_parts)

                for i in range(len(orig_parts)):
                    op      = orig_parts[i]
                    src_seg = os.sep.join(cur_parts[:i + 1])

                    # Reconcile: if a prior row already renamed this prefix,
                    # update cur_parts and (when the user didn't intend to
                    # rename this segment themselves) new_parts so that the
                    # rest of the path routes through the new name.
                    if src_seg in done_renames:
                        actual_dst  = done_renames[src_seg]
                        actual_name = actual_dst.rsplit(os.sep, 1)[-1]
                        cur_parts[i] = actual_name
                        if new_parts[i] == op:        # user left this segment unchanged
                            new_parts[i] = actual_name
                        src_seg = actual_dst

                    np = new_parts[i]
                    if cur_parts[i] == np:
                        continue                       # segment unchanged

                    cur_parts[i] = np
                    dst_seg = os.sep.join(cur_parts[:i + 1])

                    if done_renames.get(src_seg) == dst_seg:
                        continue                       # sibling already did this

                    try:
                        rename_path(src_seg, dst_seg)
                        done_renames[src_seg] = dst_seg
                        self._log(f"renamed: {src_seg}  ->  {dst_seg}")
                        ok += 1
                    except Exception as e:
                        self._log(f"FAIL: {src_seg}  ->  {dst_seg}  ::  {e}")
                        fail += 1
                        break                          # can't continue this row

            self._log(f"--- renames done: {ok} ok, {fail} failed ---")
            self.after(0, self._after_apply)

        threading.Thread(target=worker, daemon=True).start()

    def _after_apply(self):
        self._busy = False
        self.btn_apply.state(["!disabled"])
        if self.root_var.get():
            self._scan()

    # ---- column visibility ----------------------------------------------------

    ALL_COLS = [
        ("len",    "Length"),
        ("newlen", "New length"),
        ("kind",   "Kind"),
        ("orig",   "Original path"),
        ("new",    "New path"),
    ]
    _ALL_COL_KEYS: frozenset[str] = frozenset(k for k, _ in ALL_COLS)

    def _apply_column_visibility(self):
        if not hasattr(self, "tree"):
            return
        visible = [c for c in self.settings.get("visible_cols", [])
                   if c in self._ALL_COL_KEYS]
        if not visible:
            visible = [self.ALL_COLS[0][0]]
        self.tree.configure(displaycolumns=visible)

    # ---- settings modal -------------------------------------------------------

    def _open_settings(self):
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.lift()
            self._settings_win.focus_force()
            return

        c = self.C
        win = tk.Toplevel(self)
        self._settings_win = win
        win.title("Settings — Long Path Begone")
        win.geometry("640x560")
        win.minsize(560, 460)
        win.transient(self)
        win.configure(bg=c["bg"])

        apply_window_chrome(win, self.theme)

        header = ttk.Frame(win)
        header.pack(fill="x", padx=20, pady=(16, 4))
        ttk.Label(header, text="Settings", style="H1.TLabel").pack(side="left")

        # Tabs (Notebook)
        nb = ttk.Notebook(win)
        nb.pack(fill="both", expand=True, padx=16, pady=(8, 8))

        self._build_settings_appearance(nb)
        self._build_settings_columns(nb)
        self._build_settings_findreplace(nb)
        self._build_settings_about(nb)

        # Footer
        ttk.Separator(win).pack(fill="x")
        foot = ttk.Frame(win)
        foot.pack(fill="x", padx=20, pady=12)
        ttk.Label(foot, text=f"Stored at {SETTINGS_FILE}",
                  style="Muted.TLabel", font=self._f["mono_sm"]).pack(side="left")
        ttk.Button(foot, text="Reset defaults",
                   command=self._settings_reset).pack(side="right", padx=(6, 0))
        ttk.Button(foot, text="Close", style="Primary.TButton",
                   command=win.destroy).pack(side="right")

    def _build_settings_appearance(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Appearance  ")

        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=18, pady=14)

        ttk.Label(body, text="ACCENT COLOR", style="Field.TLabel").pack(anchor="w")
        ttk.Label(body, text="Overrides the per-page accent (Transfer=purple, Scan=teal).",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 6))

        accent_row = ttk.Frame(body)
        accent_row.pack(fill="x", pady=(0, 14))

        self._accent_var = tk.StringVar(value=self.settings.get("accent_override", ""))
        options = [("", "Per-page (default)"),
                   ("purple", "Purple"),
                   ("teal", "Teal"),
                   ("amber", "Amber"),
                   ("magenta", "Magenta"),
                   ("blue", "Blue"),
                   ("green", "Green")]

        def on_accent(*_):
            self.settings["accent_override"] = self._accent_var.get()
            save_settings(self.settings)
            self._reapply_theme()

        for val, label in options:
            rb = ttk.Radiobutton(accent_row, text=label, value=val,
                                 variable=self._accent_var, command=on_accent)
            rb.pack(side="left", padx=(0, 10))
            if val:
                # Little swatch next to the label
                sw = tk.Frame(accent_row, width=10, height=10,
                              bg=ACCENTS[val]["accent"], highlightthickness=0)
                sw.pack(side="left", padx=(0, 12))

        # UI font size
        ttk.Label(body, text="UI FONT SIZE", style="Field.TLabel").pack(anchor="w")
        size_row = ttk.Frame(body)
        size_row.pack(fill="x", pady=(4, 14))

        self._size_var = tk.IntVar(value=int(self.settings.get("ui_font_size", 10)))
        self._size_readout = tk.StringVar(value=f"{self._size_var.get()} pt")

        def on_size(_=None):
            v = int(float(self._size_var.get()))
            self._size_readout.set(f"{v} pt")
            self.settings["ui_font_size"] = v
            save_settings(self.settings)
            self._reapply_theme()

        scale = ttk.Scale(size_row, from_=FONT_SIZE_MIN, to=FONT_SIZE_MAX,
                          orient="horizontal", variable=self._size_var,
                          command=on_size)
        scale.pack(side="left", fill="x", expand=True)
        ttk.Label(size_row, textvariable=self._size_readout,
                  style="Sec.TLabel", font=self._f["mono_sm"],
                  width=6, anchor="e").pack(side="left", padx=(10, 0))

        # Typeface combo
        ttk.Label(body, text="TYPEFACE  (SANS · MONO)",
                  style="Field.TLabel").pack(anchor="w")
        tf_row = ttk.Frame(body)
        tf_row.pack(fill="x", pady=(4, 0))

        def current_pair_label():
            return f"{self.settings['sans_family']}  ·  {self.settings['mono_family']}"

        self._tf_var = tk.StringVar(value=current_pair_label())

        def on_tf(*_):
            label = self._tf_var.get()
            for sans, mono in TYPEFACE_OPTIONS:
                if f"{sans}  ·  {mono}" == label:
                    self.settings["sans_family"] = sans
                    self.settings["mono_family"] = mono
                    save_settings(self.settings)
                    self._reapply_theme()
                    return

        values = [f"{sans}  ·  {mono}" for sans, mono in TYPEFACE_OPTIONS]
        combo = ttk.Combobox(tf_row, values=values, textvariable=self._tf_var,
                             state="readonly")
        combo.pack(side="left", fill="x", expand=True)
        combo.bind("<<ComboboxSelected>>", on_tf)

    def _build_settings_columns(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Columns  ")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=18, pady=14)

        ttk.Label(body, text="VISIBLE COLUMNS (Scan & Rename)",
                  style="Field.TLabel").pack(anchor="w")
        ttk.Label(body, text="Uncheck to hide a column in the scan results table.",
                  style="Muted.TLabel").pack(anchor="w", pady=(0, 10))

        self._col_vars: dict[str, tk.BooleanVar] = {}
        visible = set(self.settings.get("visible_cols", [c for c, _ in self.ALL_COLS]))

        def on_toggle():
            picked = [c for c, _ in self.ALL_COLS if self._col_vars[c].get()]
            if not picked:
                picked = [self.ALL_COLS[0][0]]
                self._col_vars[picked[0]].set(True)
            self.settings["visible_cols"] = picked
            save_settings(self.settings)
            self._apply_column_visibility()

        for key, label in self.ALL_COLS:
            v = tk.BooleanVar(value=key in visible)
            self._col_vars[key] = v
            ttk.Checkbutton(body, text=label, variable=v,
                            command=on_toggle).pack(anchor="w", pady=2)

    def _build_settings_findreplace(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  Find & Replace  ")

        # Make the tab scrollable so the cheatsheet doesn't get cut off
        canvas = tk.Canvas(tab, bg=self.C["bg"], highlightthickness=0, bd=0)
        vsb = ttk.Scrollbar(tab, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=vsb.set)
        vsb.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        body = ttk.Frame(canvas)
        win_id = canvas.create_window((0, 0), window=body, anchor="nw")
        body.bind("<Configure>", lambda _: canvas.configure(
            scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(
            win_id, width=e.width))
        canvas.bind("<MouseWheel>", lambda e: canvas.yview_scroll(
            int(-e.delta / 120), "units"))

        p = 18
        # ── Mode ──────────────────────────────────────────────────────────
        ttk.Label(body, text="SEARCH MODE", style="Field.TLabel").pack(
            anchor="w", padx=p, pady=(14, 2))
        ttk.Label(body,
                  text=(
                      ".* regex  —  treats the Find field as a Python regular expression.\n"
                      "             Groups, alternation (a|b), anchors (^ $) all work.\n"
                      "             Use \\1 \\2 etc. in Replace to insert captured groups.\n\n"
                      "Aa case   —  when checked, the match is case-sensitive (ABC ≠ abc).\n"
                      "             When unchecked, ABC and abc both match."
                  ),
                  style="Sec.TLabel", justify="left").pack(
            anchor="w", padx=p, pady=(0, 12))

        ttk.Separator(body).pack(fill="x", padx=p, pady=(0, 10))

        # ── Cheatsheet ────────────────────────────────────────────────────
        ttk.Label(body, text="REGEX CHEATSHEET", style="Field.TLabel").pack(
            anchor="w", padx=p, pady=(0, 6))

        cheat = ttk.Frame(body)
        cheat.pack(fill="x", padx=p, pady=(0, 12))

        rows = [
            ("PATTERN",    "MATCHES",                        "EXAMPLE"),
            (".",          "any single character",           ". \u2192 a  b  1  _"),
            (".*",         "zero or more of anything",       "a.* \u2192 abc, a, a123"),
            (".+",         "one or more of anything",        ".+ \u2192 a, abc  (not empty)"),
            (r"\d",        "a digit  (0\u20139)",               r"\d\d\d \u2192 123"),
            (r"\w",        "word char  (letter/digit/_)",    r"\w+ \u2192 hello_1"),
            (r"\s",        "whitespace",                     r"a\sb \u2192 a b"),
            ("^",          "start of string",                "^C:\\\\ \u2192 paths from C:\\\\"),
            ("$",          "end of string",                  r"\.txt$ \u2192 ends in .txt"),
            ("[abc]",      "any one of a, b, or c",          "[aeiou] \u2192 vowels"),
            ("[^abc]",     "anything except a, b, c",        "[^0-9] \u2192 non-digit"),
            ("(group)",    r"capture group  \u2192 \1 in replace", r"(\d+) \u2192 \1"),
            ("a|b",        "a or b",                         "cat|dog \u2192 cat or dog"),
            ("a?",         "zero or one a",                  "colou?r \u2192 color/colour"),
            ("a{3}",       "exactly 3 a's",                  "a{3} \u2192 aaa"),
            (r"\.",        "literal dot  (escape special)",   r"\. \u2192 only a dot"),
        ]

        col_widths = [14, 32, 26]
        # Header
        hdr = ttk.Frame(cheat, style="Surface.TFrame")
        hdr.pack(fill="x")
        for i, (text, w) in enumerate(zip(rows[0], col_widths)):
            ttk.Label(hdr, text=text, style="Field.TLabel",
                      width=w, anchor="w").grid(row=0, column=i, padx=(0, 8), pady=(4, 4))

        ttk.Separator(cheat).pack(fill="x", pady=(0, 4))

        cheat_rows = rows[1:]
        for ri, (pat, desc, ex) in enumerate(cheat_rows):
            row_f = ttk.Frame(cheat)
            row_f.pack(fill="x")
            for ci, (text, w) in enumerate(zip((pat, desc, ex), col_widths)):
                style = "Sec.TLabel" if ci > 0 else "TLabel"
                fnt   = self._f["mono_sm"] if ci in (0, 2) else self._f["sans_sm"]
                lbl = ttk.Label(row_f, text=text, style=style,
                                width=w, anchor="w", font=fnt)
                lbl.grid(row=0, column=ci, padx=(0, 8), pady=(4, 4))
            if ri < len(cheat_rows) - 1:
                ttk.Separator(cheat).pack(fill="x")

        ttk.Separator(body).pack(fill="x", padx=p, pady=(8, 10))

        # ── Common examples ───────────────────────────────────────────────
        ttk.Label(body, text="COMMON EXAMPLES", style="Field.TLabel").pack(
            anchor="w", padx=p, pady=(0, 6))

        examples = [
            ("Remove all spaces",
             r"Find: \s+",
             "Replace: (empty)"),
            ("Replace backslash with forward slash",
             r"Find: \\\\",
             "Replace: /"),
            ("Add prefix to filenames ending in .txt",
             r"Find: ^(.*\.txt)$",
             r"Replace: ARCHIVE_\1"),
            ("Trim trailing digits from folder names",
             r"Find: \d+$",
             "Replace: (empty)"),
            ("Date YYYY-MM-DD → DD-MM-YYYY",
             r"Find: (\d{4})-(\d{2})-(\d{2})",
             r"Replace: \3-\2-\1"),
            ("Replace spaces with underscores (case-insensitive)",
             r"Find: \s+   [uncheck Aa]",
             "Replace: _"),
        ]

        for title, find, repl in examples:
            ef = ttk.Frame(body)
            ef.pack(fill="x", padx=p, pady=(0, 8))
            ttk.Label(ef, text=title, style="TLabel",
                      font=self._f["sans_sm"]).pack(anchor="w")
            ttk.Label(ef, text=find, style="Sec.TLabel",
                      font=self._f["mono_sm"]).pack(anchor="w", padx=(12, 0))
            ttk.Label(ef, text=repl, style="Sec.TLabel",
                      font=self._f["mono_sm"]).pack(anchor="w", padx=(12, 0))

    def _build_settings_about(self, nb):
        tab = ttk.Frame(nb)
        nb.add(tab, text="  About  ")
        body = ttk.Frame(tab)
        body.pack(fill="both", expand=True, padx=18, pady=14)

        ttk.Label(body, text="Long Path Begone", style="H1.TLabel").pack(anchor="w")
        ttk.Label(body,
                  text="Handles paths >260 chars via the \\\\?\\ Win32 prefix. "
                       "No pip install — pure Python stdlib.",
                  style="Sec.TLabel", wraplength=460,
                  justify="left").pack(anchor="w", pady=(4, 12))

        tech = ttk.Frame(body)
        tech.pack(fill="x")
        for k, v in [
            ("VERSION",    "1.0.0"),
            ("PLATFORM",   "Windows 10 / 11"),
            ("RUNTIME",    "Python stdlib only"),
            ("SETTINGS",   SETTINGS_FILE),
        ]:
            row = ttk.Frame(tech)
            row.pack(fill="x", pady=2)
            ttk.Label(row, text=k, style="Field.TLabel", width=12,
                      anchor="w").pack(side="left")
            ttk.Label(row, text=v, style="Sec.TLabel",
                      font=self._f["mono_sm"]).pack(side="left")

    def _settings_reset(self):
        if not messagebox.askyesno("Reset settings",
                                   "Restore all settings to defaults?"):
            return
        self.settings = dict(DEFAULT_SETTINGS)
        save_settings(self.settings)
        # Refresh widgets to reflect defaults
        self._reapply_theme()
        self._apply_column_visibility()
        # Tear down and rebuild the settings window
        if getattr(self, "_settings_win", None) and self._settings_win.winfo_exists():
            self._settings_win.destroy()
        self._open_settings()


def main():
    if os.name != "nt":
        print("Windows-only.")
        return
    App().mainloop()


if __name__ == "__main__":
    main()
