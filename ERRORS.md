# ERRORS.md — Long Path Begone

Troubleshooting guide. Every failure surfaced in the activity log originates
from one of the `kernel32` calls wrapped in `long_path_begone.py`. This doc
maps the Win32 error codes you are most likely to see onto concrete causes
and fixes.

## How errors surface

All filesystem calls raise `WinFsError` (a subclass of `OSError`) with the
Win32 error code embedded in the message. The activity log prints them as:

```
[FAIL] <operation>: <path> — [code] <message>
```

The app never aborts on a single failure; the batch continues and the log
tallies successes vs. failures at the end.

## Common Win32 error codes

| Code | Name | Meaning & fix |
|---:|---|---|
| 2 | `ERROR_FILE_NOT_FOUND` | The file vanished between scan and action. Re-scan. |
| 3 | `ERROR_PATH_NOT_FOUND` | A parent folder is missing. Usually means a parent rename hasn't been applied yet — apply renames parents-first (the app already does this). |
| 5 | `ERROR_ACCESS_DENIED` | Permissions, or the file is open in another process, or it lives under `C:\Windows` / `Program Files`. Run as Administrator, or close the holding process. |
| 19 | `ERROR_WRITE_PROTECT` | Volume is read-only (e.g. mounted ISO, locked SD card). |
| 32 | `ERROR_SHARING_VIOLATION` | Another process has the file open with a share mode that blocks your action. Close the app holding it (Process Explorer → Find Handle or DLL). |
| 33 | `ERROR_LOCK_VIOLATION` | Like 32, but a byte-range lock. Same fix. |
| 80 | `ERROR_FILE_EXISTS` | Destination already exists and **Overwrite** is off. Tick Overwrite and retry. |
| 112 | `ERROR_DISK_FULL` | Target volume is out of space. |
| 123 | `ERROR_INVALID_NAME` | Path contains characters illegal on NTFS (`< > : " / \ | ? *`) or ends in space/period. Rename to a legal name. |
| 145 | `ERROR_DIR_NOT_EMPTY` | `RemoveDirectoryW` on a non-empty folder. The recursive delete helper should prevent this; if you see it, a file reappeared mid-delete (e.g. AV scanner). |
| 183 | `ERROR_ALREADY_EXISTS` | `CreateDirectoryW` target already exists. Usually harmless — the app treats it as "already there" during copy. |
| 206 | `ERROR_FILENAME_EXCED_RANGE` | Path is longer than `MAX_PATH` **and** the `\\?\` prefix was stripped. This is a bug — please report it (every call should go through `to_extended()`). |
| 267 | `ERROR_DIRECTORY` | Called a file op on a directory (or vice versa). Usually a scan-vs-state drift; re-scan. |
| 1223 | `ERROR_CANCELLED` | You hit Cancel. Not really an error. |

## Symptom → cause

### "Access is denied" (5) on delete

Almost always one of:

1. **File is open.** Word, Explorer preview pane, indexer, antivirus. Close
   the holder. Explorer's preview pane is the usual culprit — disable it or
   navigate away.
2. **Read-only attribute.** The app clears this before delete via
   `SetFileAttributesW`; if it still fails, the file is open in another process.
3. **Permissions.** You don't have Delete on the DACL. Run as Administrator,
   or take ownership (`takeown /f <path>` then
   `icacls <path> /grant *S-1-5-32-544:F`).
4. **Reparse point / junction.** Some junctions refuse deletion without
   elevation. The app treats them as directories and recurses into them — if
   that's not what you want, delete the link target instead.

### "The system cannot find the path specified" (3) on rename

You renamed a parent in the same batch and a descendant row still points at the
old path. The app's `_apply_renames` does ancestor translation — if you see
this, the parent rename itself failed (scroll up in the log).

### Scan finds fewer items than Explorer shows

- Hidden and system items are included, but symlinks and junctions are **not**
  followed across volumes.
- Offline/placeholder files (OneDrive, iCloud) appear but may fail to copy
  because the cloud provider refuses to hydrate them under `\\?\`. Set them to
  "Always keep on this device" first.

### App opens but titlebar has the default Windows chrome (rounded, light)

- Not running on Windows 11, or your Windows 11 build predates the DWM
  attributes used. The app silently ignores DWM failures — everything still
  works, it just looks stock.

### Settings didn't persist

- `settings.json` is written beside `long_path_begone.py`. If the script is in
  a read-only location (e.g. `Program Files` without elevation), writes silently
  no-op. Move the script to a writable folder or run as Administrator.

## Diagnosing a stuck operation

1. Hit **Cancel** — the worker thread checks the flag ~20×/s.
2. If the UI remains busy, the thread is blocked inside a single kernel32
   call (usually `CopyFileW` on a huge file, or a network hang). Wait or
   kill the process.
3. Check the status bar: `● busy` vs `● scanning` tells you which phase is
   stuck.
