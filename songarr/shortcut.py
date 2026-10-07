"""Songarr's shortcuts: python -m songarr --shortcut puts one on the desktop, --start-menu one in the
Start menu, and --autostart one that starts Songarr (in the background) when you sign in to
Windows. They have Songarr's icon, or the name and icon chosen in Settings → Name and icon.

Double-clicking the desktop or Start-menu shortcut starts Songarr without a console window and
opens the admin website (when Songarr is already running it just opens the website). When the name
or icon changes, Songarr renames its shortcuts and gives them the new icon.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

from .branding import DEFAULT_ICON, DEFAULT_NAME, file_name

# Windows' own folders (which may be on OneDrive), never guessed. A shortcut is this Songarr's when
# it runs "-m songarr" with this Songarr's Python (another copy of Songarr keeps its own).
FOLDERS = r"""
$folders = @('Desktop', 'Programs', 'CommonDesktopDirectory', 'CommonPrograms') |
  ForEach-Object { [Environment]::GetFolderPath($_) } | Where-Object { $_ -and (Test-Path -LiteralPath $_) }
$shell = New-Object -ComObject WScript.Shell
function Get-SongarrShortcuts {
  foreach ($dir in $folders) {
    Get-ChildItem -LiteralPath $dir -Filter *.lnk -File -Recurse -Depth 1 -ErrorAction SilentlyContinue | ForEach-Object {
      $link = $shell.CreateShortcut($_.FullName)
      if ($link.Arguments -match '(^|\s)-m songarr(\s|$)' -and $link.TargetPath -eq $env:SONGARR_TARGET) {
        [pscustomobject]@{ File = $_; Link = $link }
      }
    }
  }
}
"""

MAKE = r"""
$folder = [Environment]::GetFolderPath($env:SONGARR_FOLDER)
New-Item -ItemType Directory -Force -Path $folder | Out-Null
$link = $shell.CreateShortcut((Join-Path $folder ($env:SONGARR_NAME + '.lnk')))
$link.TargetPath = $env:SONGARR_TARGET
$link.Arguments = $env:SONGARR_ARGS
$link.WorkingDirectory = $env:SONGARR_DIR
$link.IconLocation = "$env:SONGARR_ICON,0"
$link.Description = "Start $env:SONGARR_TITLE"
$link.Save()
$link.FullName
"""

UPDATE = r"""
foreach ($s in Get-SongarrShortcuts) {
  try {
    $s.Link.IconLocation = "$env:SONGARR_ICON,0"
    $s.Link.Description = "Start $env:SONGARR_TITLE"
    $s.Link.Save()
    $want = Join-Path $s.File.DirectoryName ($env:SONGARR_NAME + '.lnk')
    if ($s.File.FullName -ne $want -and -not (Test-Path -LiteralPath $want)) {
      Move-Item -LiteralPath $s.File.FullName -Destination $want
      $want
    } else { $s.File.FullName }
  } catch { }  # all users' shortcuts need administrator rights: left as they are
}
"""

REMOVE = r"""
foreach ($s in Get-SongarrShortcuts) {
  try { Remove-Item -LiteralPath $s.File.FullName; $s.File.FullName } catch { }
}
"""

WHERE = {"desktop": "Desktop", "start-menu": "Programs", "autostart": "Startup"}


def pythonw() -> str:
    """pythonw.exe beside the running python.exe: starts Songarr without a console window."""
    exe = Path(sys.executable)
    windowless = exe.with_name("pythonw.exe")
    return str(windowless if windowless.exists() else exe)


def _powershell(script: str, name: str = DEFAULT_NAME, icon: Path = DEFAULT_ICON, **env: str) -> list[str]:
    if sys.platform != "win32":
        return []
    env = os.environ | {"SONGARR_NAME": file_name(name), "SONGARR_TITLE": name, "SONGARR_ICON": str(icon),
                        "SONGARR_TARGET": pythonw()} | env
    r = subprocess.run(["powershell", "-NoProfile", "-NonInteractive", "-Command", FOLDERS + script], env=env,
                       capture_output=True, text=True, timeout=120, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    if r.returncode != 0:
        raise RuntimeError(r.stderr.strip() or r.stdout.strip() or f"PowerShell exited with {r.returncode}")
    return [line for line in r.stdout.splitlines() if line.strip()]


def make_shortcut(where: str, extra_args: list[str], name: str = DEFAULT_NAME, icon: Path = DEFAULT_ICON) -> Path:
    """A shortcut on the desktop, in the Start menu, or in Startup (which starts Songarr without opening it)."""
    if sys.platform != "win32":
        raise SystemExit("Shortcuts are made on Windows only. Start Songarr with: python -m songarr --open")
    args = ["-m", "songarr", *([] if where == "autostart" else ["--open"]), *extra_args]
    try:
        made = _powershell(MAKE, name, icon, SONGARR_FOLDER=WHERE[where],
                           SONGARR_ARGS=subprocess.list2cmdline(args),
                           SONGARR_DIR=str(Path(__file__).resolve().parent.parent))  # the folder with the songarr package in it
    except (OSError, RuntimeError, subprocess.TimeoutExpired) as e:
        raise SystemExit(f"Couldn't make the shortcut: {e}") from e
    if not made:
        raise SystemExit("Couldn't make the shortcut.")
    return Path(made[-1])


def make_desktop_shortcut(extra_args: list[str], name: str = DEFAULT_NAME, icon: Path = DEFAULT_ICON) -> Path:
    return make_shortcut("desktop", extra_args, name, icon)


def update_shortcuts(name: str, icon: Path) -> list[str]:
    """Give Songarr's shortcuts a new name and icon. Returns where they are now."""
    return _powershell(UPDATE, name, icon)


def remove_shortcuts() -> list[str]:
    """Remove Songarr's shortcuts (when it's uninstalled)."""
    return _powershell(REMOVE)
