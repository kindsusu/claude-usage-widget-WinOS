"""Claude Max plan usage widget — single-file edition.

Always-on-top desktop widget. Calls Anthropic's official OAuth usage
endpoint directly using the token Claude Code stores in
~/.claude/.credentials.json.

Features:
- Light / Dark theme toggle (☾/☀ button)
- Transparency popup with circle slider
- Smart topmost: floats only when Claude/widget is foreground
- Multi-monitor aware
- 20 pixel pets embedded as base64 — random on first run, persistent
- Gradient bar color (green → yellow → red)

Single-file portable. Requires: Python 3 + pillow + Claude Code logged in.
"""
import base64
import ctypes
from ctypes import wintypes
import hashlib
import importlib.util
import io
import json
import os
import queue
import random
import shutil
import subprocess
import sys
import threading
import tkinter as tk
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageTk, ImageFont, ImageDraw, ImageChops

try:
    import pystray
    HAS_PYSTRAY = True
except ImportError:
    HAS_PYSTRAY = False


def _base_dir():
    """Folder that holds this app's side-by-side files (widget_config.json).

    Normally that is the folder containing this script. Under PyInstaller
    onefile, __file__ points into the temporary sys._MEIPASS extraction dir,
    which is deleted on exit — so a frozen build must anchor on the .exe's
    own folder instead, or every setting would be silently thrown away."""
    if getattr(sys, "frozen", False):
        return Path(os.path.dirname(sys.executable))
    return Path(__file__).parent


BASE_DIR    = _base_dir()
CREDS_PATH  = Path.home() / ".claude" / ".credentials.json"
CONFIG_PATH = BASE_DIR / "widget_config.json"
USAGE_URL   = "https://api.anthropic.com/api/oauth/usage"

DEFAULT_CONFIG = {
    "x": 100,
    "y": 100,
    "refresh_seconds": 180,
    "plan_label": "",  # empty = auto-detect from credentials
    "smart_topmost": True,
    "claude_processes": ["claude.exe"],
    "alpha": 0.95,
    "theme": "light",
    "pet": None,
    "ui_scale": 1.0,  # baseline; user can override via menu (1.0 / 1.3 / 1.5 / 2.0)
    "minimized": False,  # iPhone-battery mini mode
    "mini_scale": 1.0,  # independent mini-mode size, adjustable live
    "auto_update": True,  # pull new releases from GitHub and self-restart
    "taskbar_visible": True,  # embedded 2-row strip inside the real taskbar
}

# Keys that never persist to widget_config.json — changes via the prompt
# apply for the current session only and revert to DEFAULT_CONFIG on restart.
EPHEMERAL_KEYS = {"refresh_seconds"}

# Bump this together with the git tag (the release workflow refuses a tag
# that does not match). Users on older versions compare against the latest
# release tag and pull the new widget.pyw automatically.
__version__ = "1.2.0"

# ---- Auto-update ----------------------------------------------------------
# Release-gated: only a published GitHub Release reaches users, never a plain
# push to main. Version discovery reads the redirect target of /releases/latest
# (github.com, not api.github.com) so there is no per-IP API rate limit to hit.
UPDATE_REPO        = "kindsusu/claude-usage-widget-WinOS"
UPDATE_LATEST_URL  = f"https://github.com/{UPDATE_REPO}/releases/latest"
UPDATE_ASSET_URL   = f"https://github.com/{UPDATE_REPO}/releases/latest/download/widget.pyw"
UPDATE_SHA_URL     = UPDATE_ASSET_URL + ".sha256"
UPDATE_FIRST_CHECK_MS = 20_000              # after launch, once the UI is up
UPDATE_INTERVAL_MS    = 12 * 60 * 60 * 1000 # then every 12 h while running


def _enable_dpi_awareness():
    """Tell Windows we render at native pixel density. Must run BEFORE tk.Tk().
    Without this, Tkinter renders at 96 DPI and the OS scales the bitmap
    bilinearly, which blurs text on >100% display scaling."""
    if sys.platform != "win32":
        return
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(1)  # Windows 8.1+
        return
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.user32.SetProcessDPIAware()  # Vista+ fallback
    except (AttributeError, OSError):
        pass


def detect_system_scale():
    """Return the system DPI as a multiplier (1.0 = 96 DPI = 100%)."""
    if sys.platform != "win32":
        return 1.0
    try:
        return ctypes.windll.user32.GetDpiForSystem() / 96.0
    except (AttributeError, OSError):
        pass
    try:
        hdc = ctypes.windll.user32.GetDC(0)
        try:
            dpi = ctypes.windll.gdi32.GetDeviceCaps(hdc, 88)  # LOGPIXELSX
        finally:
            ctypes.windll.user32.ReleaseDC(0, hdc)
        return (dpi or 96) / 96.0
    except (AttributeError, OSError):
        return 1.0


# Must execute at import time so the call happens before any tk.Tk() runs.
_enable_dpi_awareness()


def _python_exe():
    """Interpreter to spawn helper/replacement processes with. Prefer pythonw
    so nothing flashes a console; fall back to whatever is running us."""
    pythonw = Path(sys.executable).parent / "pythonw.exe"
    return str(pythonw) if pythonw.exists() else sys.executable


def _python_launcher():
    """argv that re-runs this program. Under a frozen (PyInstaller) build the
    .exe *is* the launcher and __file__ points at a temp extraction dir that
    will not exist for the next run, so only the exe itself is returned."""
    if getattr(sys, "frozen", False):
        return [sys.executable]
    return [_python_exe(), __file__]


def _update_log(msg):
    """Append one line to widget.log beside the script. Update events are the
    one thing worth a trace: a user reporting "it vanished and came back" can
    be answered from this file."""
    try:
        with open(BASE_DIR / "widget.log", "a", encoding="utf-8") as f:
            f.write(f"{datetime.now():%Y-%m-%d %H:%M:%S} [update] {msg}\n")
    except Exception:
        pass


def _parse_version(tag):
    """'v1.2.3' -> (1, 2, 3). Anything non-numeric -> None (never 'newer')."""
    tag = (tag or "").strip().lstrip("vV")
    try:
        return tuple(int(p) for p in tag.split("."))
    except ValueError:
        return None


def _http_get(url, timeout):
    req = urllib.request.Request(
        url, headers={"User-Agent": f"claude-usage-widget/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read(), r.geturl()


def fetch_latest_version(timeout=10):
    """Return the latest release tag ('v1.2.3') by following the redirect of
    /releases/latest to /releases/tag/<tag>. Empty string if no release."""
    _, final = _http_get(UPDATE_LATEST_URL, timeout)
    return final.rstrip("/").rsplit("/tag/", 1)[-1] if "/tag/" in final else ""


def stage_update(target):
    """Download the latest widget.pyw next to `target` as widget.pyw.new and
    prove it is safe to swap in. Three independent gates, any failure raises
    and leaves the running file untouched:
      1. sha256 matches the .sha256 published with the same release
      2. compile() — no SyntaxError
      3. a fresh interpreter runs it with --selftest and exits 0, which
         means every module-level statement (imports included) executed
    Returns the staged Path."""
    data, _ = _http_get(UPDATE_ASSET_URL, timeout=60)
    sha_txt, _ = _http_get(UPDATE_SHA_URL, timeout=15)
    expected = sha_txt.decode("utf-8", "replace").split()[0].strip().lower()
    actual = hashlib.sha256(data).hexdigest()
    if not expected or actual != expected:
        raise ValueError(f"sha256 mismatch (got {actual[:12]}…, want {expected[:12]}…)")
    compile(data.decode("utf-8"), "widget.pyw", "exec")
    staged = target.with_name(target.name + ".new")
    staged.write_bytes(data)
    rc = subprocess.run(
        [_python_exe(), str(staged), "--selftest"], timeout=120,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0)).returncode
    if rc != 0:
        try:
            staged.unlink()
        except OSError:
            pass
        raise RuntimeError(f"selftest exited {rc}")
    return staged


def apply_update(target, staged):
    """Keep the previous file as widget.pyw.bak (manual rollback path), then
    atomically replace the running script with the verified staged file."""
    bak = target.with_name(target.name + ".bak")
    try:
        shutil.copy2(target, bak)
    except OSError:
        pass
    os.replace(staged, target)


_singleton_handle = None


def acquire_single_instance(name="claude-usage-widget-singleton"):
    """Return True if this is the only running instance.

    The SessionStart hook launches pythonw.exe on every Claude session; if
    the widget is already up, a named mutex lets the new launch detect the
    existing one and exit silently. pythonw has no console, so the rejected
    launch flashes nothing. Holding the handle in a module global keeps the
    mutex owned for the whole process lifetime."""
    global _singleton_handle
    if sys.platform != "win32":
        return True
    ERROR_ALREADY_EXISTS = 183
    handle = ctypes.windll.kernel32.CreateMutexW(None, False, name)
    if not handle:
        return True  # fail open: if the mutex can't be created, allow launch
    if ctypes.windll.kernel32.GetLastError() == ERROR_ALREADY_EXISTS:
        ctypes.windll.kernel32.CloseHandle(handle)
        return False
    _singleton_handle = handle
    return True


def release_single_instance():
    """Drop the singleton mutex so a deliberate self-restart (scale change)
    can re-acquire it. Without this the relaunched process would see the
    still-alive old process holding the mutex and exit immediately."""
    global _singleton_handle
    if _singleton_handle:
        try:
            ctypes.windll.kernel32.CloseHandle(_singleton_handle)
        except Exception:
            pass
        _singleton_handle = None


THEMES = {
    "dark": {
        "bg":     "#1e1e2e",
        "fg":     "#cdd6f4",
        "dim":    "#7f849c",
        "muted":  "#585b70",
        "accent": "#89b4fa",
        "bar_bg": "#313244",
        "btn":    "#7f849c",
        "btn_hi": "#cdd6f4",
        "danger": "#f38ba8",
    },
    "light": {
        "bg":     "#f4f3ee",
        "fg":     "#111827",
        "dim":    "#6b7280",
        "muted":  "#9ca3af",
        "accent": "#2563eb",
        "bar_bg": "#e5e3dc",
        "btn":    "#9ca3af",
        "btn_hi": "#111827",
        "danger": "#dc2626",
    },
}

BAR_WIDTH = 220
BAR_HEIGHT = 8
PET_SIZE = 16


# ---------------- Embedded pet images (base64 PNG) ----------------
PETS_B64 = {
    'image (1)': (
        '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'
    ),
    'image (10)': (
        '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'
    ),
    'image (11)': (
        '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'
        'rkJggg=='
    ),
    'image (12)': (
        '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'
        'rkJggg=='
    ),
    'image (13)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAgAElEQVR42u19aZAd13Xed8693W+Z'
        'GQAESCwESVGkJJIiTUnU5tApR6myLJdMK3ESy5Ktyh9XOUlFSvLHcirlbK7EViqxo0pVnJQdx6qk'
        'yllUrjhKoq0kK7JWW0u00JTATdxAAiBAbDPzlu57vvy4t7tvD0AQBGYeABLvB4szeNOv3z33nvOd'
        '73zntJgZrtiXALzCbljYv2XFlfzilXfDBCAirT2ubANcqfuGbO2hl+3NnfXHy3k1L+yll9VtkWyP'
        'Zzqk7Wnt//4ycusXd2N6Wd3WZbvKW/fSK85vXjXA1cV9yRng8vQ8i9kWuvj7u1L2+2K2hS7+/i7t'
        'fr/czP+yS8TOYf5LYpurmfDmH80XZUjdiku/nFHNizWkbsWlX4b51Ga6oN7yvbz38qUxQM+BXN3L'
        'V4PwS/vlF1i8ugxLKXLJazz+3LF0s/CMiBEA8yLi5eDcrL/0svgb81uMJglRVM/y2L8DDFBCwIls'
        'u49L94LhUvpAERz9HVSPQ4Zp9W2K7e/C0lthYWG1Qv+CJZHNyEzmmD4ICCCAguscn7gsPND8GUwf'
        'go4bA6xj6eSCz4BfBIQXgYwb3+OA+lLXohtvowPKUKSEABAwLP7G/KLUGNHhCmggBSSsc8FE/s0J'
        'kpaJTkRfzLoYQ/7RZ/ytNR8iQBAJgIEEFGKXSxDeAkdEUCASEwtKAWhcdAEovYxPRTfsRBrP0y2I'
        'iBN/zszGZZoiB7a5joAX4n0uEqr4BXEJErczQUI8Zo9C/xSsACUC3DYM745vENET68e+88yfeHEk'
        'Aqtto12vu/6t51iadruIyPp87dtPfzmEOv7Cu+L11//IoBg2Rypg/VuwCeAIQTjZ2uOCXf9FQhW/'
        'UA0VARikwOqXcfrz0UPAJhjdib13AzDSCZ448fCHP//BkR/TbBomr9n9+h/a9xYnUVMmz7ddohlO'
        'TY//9pd+dTJfVXWGMB6s/Muf+uigGIKEKKzCsd9F/RykAAmU0LJxO7wkaju/sDpIlgUIpICUEAEE'
        '4qDjfAuq6FKxMiqWAPqqGBXjBkG98MqoyFK5oqKqziwMXPxbUBrTyQhuWeBJA613RinJD710M2Fp'
        'wmCe9RhIowloNKGQZrQYS40WLJA1xZM8KzsoXegGaQFmNJgEhmCBDEYzMxERmABgIAXSxhWFRFZG'
        'F5+hL8YAAbbeLZ2UEN9ud5Ai6lQBKByAgRunGCwA4MR5V54nMTjwS9LPuUs3VlF18YpLgKaQHkEB'
        'a3ACEcDB1sGqlxS/RAwgQ4xfBzqQkALVUwhHIT4hD3GTsHroxDeNwSyUxfDxE/cLXVwIp24a1h45'
        '/nUBjcGpqwKnMxNJBlXV0SDGAHPOHz11KFgNSOvyHjnxp9un2wODSOE4uTGsumheAVjDLaG8u0FB'
        'M/g951j/rchSZSHydMkwPXDsP+HUJ+FWACOgqk9OJx95+rEI2FV4cs0OH1XVuKwYDuSG3SIIgXQq'
        '6zM+fnAOAURIlqXeen1JGiGqWJ/WTx/1sKhDFhXctBfem5kYuFL4v7XvlSP1jEmJrWF0F/b+gz5W'
        '4EvPBbHxPwY4MCRI2qZGFpwUTtRAgXn1ZN3ZTtSJiDiliJiCZRHdiFBQOFGoaGGkAIUraBVAacKu'
        'SlGoGBhAL6XARcNLDxzbJWHiLtAAcl68LRNA7oEXAqRQ0gYlISQhjqIGGk0FZtZ0MQhJMyM8yEBz'
        'ApDBGuBoMIsXDSQkJXqERZYvJniMt0KANGv2Alv0SWahPbtnEdl6e/gtaosQ0c5bdt/CAYAUUIE2'
        'ua66Uj1IwkChkKDFpQJJMSNJCCXinJTNSXOuGD1VWuJgNCEIowgVAsJIxnADGYiKQmgJ84jHGYl3'
        'e88vKsm6sAixFS6IIjKtV59ZPSCi8TsIIKIAKY6rj+ukpq5HWsapPzSbkyFuXgJOMR4k2EJB6YXR'
        'MmhhevyiwnYfJ0IDKhgNJW1/owpEM19HPjZdGzglo5FmguNy+gFjLeksSrxVYz0qdlw7vOn8IdEF'
        'CsI3PQiTpuoOnn7g9/7fL3oZGCxFNlHpooHEMCiiSBZSJt9BMB5+SQRD8h4AqIK1KZ88FOL5MrIs'
        '5OZ9DtJ49c46nX0aXCqQSNVJck6gRiqCzI4TVNw0nL5zz9vffcc/CxZ0ayjS+L381pX5VApVByrA'
        'Jty1EYGg64C9wDrfm1a/8wAi7MA50ffgfR+R7MCmGY6QDf5dxTWrrdKC2UgTsrWiCFSoWxqUuVUu'
        'iGewBpGr6f6VLQdJUgSESnLjvQgvfRYpLlvunGN4jcxyU4Br97ykNHsjwuyu3XPxzDyJpNdWOP2z'
        'x4DNZ6AExmB0XZRsz0D6b7uGktIm2Fk3HMn89tKikyAD4NFy2RJhU9zBjD4/s6L0vqhklkUbXQgz'
        'UuGMgZEpkq3Vn/nN1wI0zLDTwmsJEZCkBdZMzLnkpflYDGRyBh1pBoKJVej5HVUZlgoxUgwofev8'
        'me10tgciuRY2oFS6oE0wBn5Jx0WGfuzEQ+igAzdeAC2xCUH4rBWJKsxOzp8WqiE4LX5w/Osff+g3'
        'B24UaAlkQ9j+XVaQEcRVQRYU2axDWukmSWYH9TdEArJ5tyBzUYLMWmxwVPJHVrjxu+/69Z2j64NV'
        'AEo3GvntC8oDLqasc9Y/LN3wuvEt7Y/H1p40q+FUus0ZV7rdr42r0BQxEuxvWeTWAoA1bgstvm8W'
        'NllNGB15Y1VGlMmGd85N28UmkR2DPSvltYtUGfst+iTGrFNgNKcusG5gPrMKGXlGvIY1K9RiSgrj'
        'b5Gbgy207woyELRm66oADSBqKxOSHYjoIiXma3Vtc4KRu0Zn8S3kyHQrdTcq0ATpRDPs2MIUEbCB'
        'G50HN4ASHaOmwCDacTVsiAI0K01GTruFMDhDXUwy/wzmFkl5YszeGwyERfAQC9aG8oyMuQVFkOZO'
        'GgfUen520TS+WRo/31XTY5qdlxUJGDIHF4+c5FmFbKhX45KMjtBFdWBtcLlgEoMkfifDPjE2uI7I'
        'ltY26bRk/iE6mpTzimiM4MnzMGNU0W7t7qPY8oPnZ4GtCAl+Mc0hKcyKCtswoB2sT2lW2tKxVNX4'
        'mMQRiGQQPsXeLvp2KVhidNDSUGTMB0ARsWTe5pNSipFum5eg8cQvphrc+m8yO/JCAKNlHwkh0fTO'
        'urJqanGvZkpeSQvZKzB0GQATUpKcDPeFKweu8XcWw/RsvbZASB6cpXGJeOnUhBtoy/wcsIPn6dt/'
        '69OHVo/NxaflD3Pb96qVV96zo54FqDReP2UKDUfU8UYtoo84U6BtEcCX+txTswe/8qzz2g7qIfHa'
        'H921vHMQqiAbnOLZkNJWJEkLMkCTKOXopfEXiRGigZ/67YeffWyS/+E979z/6rfurCahw4lZUEbv'
        'ECHngRq/TwA0FKU+9p2jf/gb399wY9ff9pYde4b1PObIlKwWxs3uC3rBsOEvcpzMec+ISiQnDRBr'
        'OBuhsRh5VYEKjeqEgUWhpEYf0UTM9jLcwNElz5PUJWx+JREmqVdVERWzjFCSho+SfNXRR6oLKk76'
        're+klZ4tcuKeyaGk1TFSECVBvR3J/OwIyUZPFf8Bom1RIV1dKGSMIWJGjRfvbQr2hcN9knuBpWF/'
        'wSfrRRmDHduc+LBzf9MuALP3A4ly6IpC2ZDeRs4nsSBMQPtsNs96j0x3Q+HGAgM32wm/4Cr5re5F'
        'lkxzmWTOWc4UHVFUPVjDsolKlmc1fBBBshj4A1957tGvPacesVw8GLt7f+bGYqi0JlpIts4CdU15'
        'WnK/FYEp8zJAB4o2KQifzyr5hQ82bIB6s66z9cpC+ua1EUA1CxnR1jkNM/hSD3z1yJf+61Pt5Vyp'
        'b/qp/cOxrzstRcoLBBKqYIG24S6YbYw8mrcyiAU25/qFdSL2MFGTe6ngL//SbesnKlcojSJCwzX7'
        'lqpJkAybWEs8m/lS1YmqxAxuvFx0mKhjHUQU80m45Q073/vP73Q+5n+R4tFdN4zqipGdYhuLKXIp'
        'EgG/OGG6tMXwhpIhALn9rdepk4azF4jUc6smARqLKQaFcwpIPQvRq5pRBDQ2oh8zGkSccwTNCDMR'
        'tcAdewbX3Xx9V8oUCDFbD6G2lEhAWizLS9G66RfTnRSJNMlVsw0tPVmt+2lLg1kpEIPAKq0qM2NR'
        'iKhaIIhQJ8dRV8ZAVRfmNlmvRNV5qE/dSaFmdaJqj0WMJIn0BAHNmgMyTnbzErEXjMNbaICoLycb'
        'gZVoS+OLgKIgleJ9GWXlKmpiRmuSUlrgeFv5yX//8Df+19OkvOef3nn7vbt86YqhliNngRY43l4I'
        'tBi4r33s6c/+3qOi8pMfeM0b3rFn/XQtCkE8PQ1zlycSiEXlPD4LYcZgsJinXLwgZatQ0Ivt1Srd'
        'uEuQklQ3FBi+e+29SzYMMCoLDu4ffOfzgz8qrKA0UVFw6uj0+KEpAFc6kn/xr9/85997gysKmsFM'
        'nStKLYZa13bi8BTAdD2ItEWdqAbju9b+yrX1rkoqCEu39HDx8OfKT3m4RowUC2sy9MsqTpu+pcVV'
        'xDZbGafr1akHj35R1ZHmtHjq1HdVin5QhsLt5e6hLYE1KMBom/0g1hLZSHsIOK8xrB5+eM2r1JX5'
        'UmlzkpG/q2dhuFyeODQRyYUTSVIUIf++sGdXtRdaQQXV8ASOocg0MiAhxuq7hz+zfbDbEIJVy4Nr'
        'b9nxpivPAFHqc2L6zB888A+9FBQla6e+9Etmxkw5IkBl8wGL4EjQmwsIfWiYSowkVPEHv36/REUp'
        '2zwMbWUgGDXG50RdMytGSu1IVwetY8dHxXmUBRitRWgW6k8+9OHY5zS39Zuvecut97yZtC0NzX7r'
        'Yu+43OHENQxyJBwaWXRCfCRIFaaCiiWXaQmmR5QjKhF3hkjfbaApUkIFFVHXVGLYaK9ThiVJ39vc'
        'iojmMamVM46KZREROGfFoNi21aSQQLYyCMMEzhikpcvYJ2kFQx2rjbSuoICMCxTW48QgIvNJiPH2'
        '+dO79BeRRKorSxXohGuE5MAKxUhNQYOMS5SkkS5TUjRiFcYuJpOtDwAEt9AAs3rNwafKp/hCB2n3'
        'NyJMQ3igfGAQSsLEeSfFU/q00qFRdUJQzcItr98BSszU+iWYTLWSqgxC4/5Xb2vyrET2OdGHR49u'
        'mx8xBCm8R/mEPom2NtdtEMzrCUEnbhZWZ9XaAhKDzVFHp43TlPlU9LnpwU88/BvC6BiK09MjR9Yf'
        'EXG9Ujoww4wgxCmUrIVaoGAKJMmNlAMtBi5TU+WywrZck0rzIjKb1lVV9w+KVFIbTMVBBAxqWsBb'
        'p5oQkCrF9dtu81oKUdl0z/JtP37r345dC5mdZXPV0ZtjgHOhXQKCB49+8ffv/+DILwcLXUm2C6Sp'
        'UND20oOGDaPrWj0Vs4ruRkWkSgP3W+1p84/aNKqxVwBjE5SsXip3/sI9v7My2LXI0vzmuKDT05PN'
        'iIxemU+Awg+GxZgQhTJvy0oCXmYVFvb7+di4L4G2CZR0YlsKm6rnxtpKagK2lv82GqRhOzI1pLRQ'
        'K2XqRtrp2cmeakha+S7HxbLXYhOffuMvVtYiOq0mH/rk3zt4/LGiHKTtS6iKiq5N1t735z7wjjv+'
        'Wh3mhgCQDBQIM4EISWGbOVlq3EifoNI2U3SClMSTqmTVndyu+U6X2JIWL98xTl19gS0nYTSBrs5W'
        '/8nH/+aJ1WNOXWrvVgXo1K1Xa+//0V99y81/IVit+ZCJiyCJ/cWsvjEADKFer0+vVadcKBNqVijE'
        'iZuE1Vk9MQYRUfFeBqJeRQyhHSojTd9eW+ElLCQXxKamIlnbQXNQ2Fivk3MxlwMBCtROmvSbjVq6'
        'c0ziUUJURANnCiWkCtXq5OTa/BTo0n0RouLgJmGt4swYjLYpRMVFGUBF4x0sDVeceC9++2jENPEE'
        'Agmis/X1pXJFxb1299t+aefH5nUF0Gv5x09+5GsHP1q6ZcDYHG9pUtihHw5dYbBMRiWpiy8WIsW1'
        'gurUetSEEGTzKOJhnNusQmg1FK0eS6DBwn13/P2btt9dh7mKjIrlwi1NdVK4wku5PCobBTBBwLnZ'
        '6fVCBipOndusYOAv2PMcPvX0t576UlmUs/n0xNrJbePBX339K1omYODdFx959tmTJ7/37DdUdXV6'
        '8rV733zrdbfHKwzcUhSQs1/+UNFJmL9u+bYf3/fWYFOB9p+8lRxLArK5XItZT03qtwNJJ+4Pn/ni'
        'g6tPFFowI8abmIylcsdyuRNAFarPP/TxYNW0XludTJbHxV/6oevFUiWidO7rT5w4durk/Yf+ZFqv'
        'rs/X9m2/6XX7f/gibXCBiRhpEH3s2Pf/zR/9ymg4DjWLYnhtOfZxL4qSLJwnzLvySw9++gsHPnFy'
        'euw997z/lde+pqqnhR8a60ysmLdrCARe3SBNkcnrU40P6vosugpDZkSmjr90PJx26rumBNzBSqtD'
        'Fdv2jq8d+Q9f+LV5mDrnvRsNQqFg4V3804F3oaZzg0//2Uc/wf+2Plv9kVt/IhrgYuRyF56IEXTO'
        'bxtfM5DheElr2Nh7tieWCMay9KPCOZTiXV2H5cF2FR34EUS8Fg1BD+nLzZtpZgygpgBhSMfFOvUo'
        'mhoOO06IeYcAxNAmdbnyrfuFkYUrYzgt/XDb0o7pfKLwvrSRd/EN0eAhWFnI0tCP3KCGKtx4sLxo'
        'GNqKvERUIE40MIxH8s47b/AqmcJVRFCFcPeeHXfv2faVx599+NiaiJyaP3f49FPzeu61OL5+VEVT'
        'B500YKXD+c1ZaCr0kNZUDZZpJSgtECK6eE1r9nnc+MQZKhQConpk9VCBnSry7OqhOlS1hR1L5Ttu'
        '3+MQ25oT9qqNd12//fZ927zwK48dP3Kqju3HKkosamJWs/ryu1/50KGTT63PVwFPulHhHVLHlmQ1'
        'yMLJsBiouOl0PizHnz3wsc8d+N9KCcTe3bpn56gOllcrG/ZBM6GsIJwGJ0lJwa5XJutCygUYrRFG'
        'lKFs7Fzqkox4Ugsp/uOXP3TqZHCFVlaDMBOYjb3mQtR42gqnBVk4EaDww+8d/M6vfervVmH2c296'
        '/6t333XB7cT+AlpQHzpy/4Fnvrt9aWX7cDgqXQhBVBps0eVVJIJZqVgaFgPvYvPLfB6MJF3GKOel'
        'YkhvrpNi/gjCM0DJNttqKNU86vaTvxrFK6V8FZqJH+zt/V6TGGlUwsLy0COgDhh5xzZBbnsFm080'
        'SFnoSjGo69XvHvzqtJred9f7FoiCBABGxbgsx6/Yds3bXnNdbXSJB8vRSNzJUpu98aZr33zzbgNK'
        '1QefPfF/Hz7ixAvz1uu0LrpBEpK2s4KKXLDAvF8v9+zdjMW2jUk2dFlmo8+i1VXVhHu3Dd9+x+46'
        'DnKyLK6yjShRsSRmeNP+a+69+donjq9/7sAxNyydukXD0CpUk2pSc66YCy2kmZIiEJWiaU+3wAqA'
        'RpLeTFTJahamjmIYifiOB8qXHblxDP466FID4uX5h+p30QCs4K5Br8uXGbPfAiQRyLyaT6u5iSim'
        '0oSiYHE3qUbWgTDO0hHVmNcFsJpzOp/NLpJMuxAD7Fzas2f5xr07968Mdk/DzKtnaosO0+oIaSQL'
        'tzwsdkAcSWMN6rAYXrty8sbtyyWKpcFJYC1pOqWnXOlLNw3+euSKWsmlDBta6tE/BKms014rrzMk'
        'QkNl78qNS67Yv2u0PLhmHmpVB2gcoVuF9Vn9XHzvuNxX+DGYihKF8yuDU9ctFWFclX6waDp6Vk+N'
        'wWtR+kEEwtFBT6vnvvaDfzyvV0O9/orr3nXbvp9vHX1MnepQASx8+Zkf/NsvP/n7o2IlWMi4Tgpk'
        'Us/ecs1rf3Lfvca5xLXuMaNytokI0tf4x87IpM/6709+9nunHx9omaG0hKuqMP25u//1Lde8aVZN'
        'Sj9sARmNqnr41Ne+9cS/8jIyhHte8cu7lu8ymojG01hbPaumqlq40l0EL3QhJ2BYjHoeMo2kFDJU'
        '9VpVr9VhYqxTygaougNHv/CZR39rWGwTILBaq44XOgoWWtxuvZEakjlrbUZ7nrnZibNOH4AAQVqe'
        'VUSkyShaIVZCoeXHH/rNkV9qSAr89O3/aOf4BkMNaAizKpwWr1VYr2zSHKx0eLx4H1OBntZ6IQaI'
        'hWzpAl1aFO+WXrXn3UYz1ttHr4rfPb55dX78mdPfH5c7YhO6ahF3DbMJh2xZUGmnSKiEY+Aa2mED'
        '8kLD3URAg9sB3QmGrPsr67FMH2cienzy1HOM8Sk4KWpWLV5aGb7izv1/w+moCmvL5X50vZjtlExe'
        'fInGX7jYrY/tSHod37TrJ/p5Q2MbLUu/5HSg8FAhjTmBwKZ23o71SfjGsT4k1UGIZ8qn5GzAOO+z'
        'VHCGwR1w16UZlLJhmAQzLtXiNIt4BdUydcuKklwZ3bgyuvGsX2cTq2N+U7V27CJKozSWVhONAFqq'
        '8AG9arg0wzM29C0zCAyqgJPY1iqyYdpAih2aV1p8g2IV+RC4VLPRKJyRtp8mXZbGuhVOpFAQfU4L'
        'XDfmdJswt8ZvrtZOzsgGCelVADPonhGd0nf9GbL0N8JflwyTTY7IgnA+hLf9zQgIveKxytmaMDIM'
        'JpR8kEUz8gXNEPxzFCYvho/zC2yQ728bkY2jrNDotfLv43ZAtMukRHrDJnI0JNJv/OhzSo2bYlv9'
        'kf6jurghC7Q25qjq/3ngPz/wzDcHxRCQWZjcseeN9732fWZBLrom47e6BeeMfj10KuVUuWXWw6pZ'
        '4tqMvDLLdny/zT1vsOaG6UxZKS3/016XX79RtcvzpF3Z6FQPHP7WVx//9FK5DeDq/FThy83SbPnF'
        'PDSRG0KWtDxlo4BrB3hKv8kFLjEyumGyRc4GJVTZ9NinWazs9cFI9OrNtA92QwyyHDn+vrL5oZNP'
        'BoSVwY5d490ABsV4qVwZl8tx25UyuAL6hM8qAugGGecz2tL2b0agdzZQzh+V8Fwz4FzOGGvGfmBo'
        'yDi/H8X1EvXxrQbSsGHWTauMaEcABgZjOLZ65F989gOHTz/1Y7f9zC/e+ytksFAHC8YXTXlucp/w'
        '5jSLkVmwbYcONELaXm+9CFcRnhUtaOyFk9wLsQ80ExckpElbJ27GNnXdUqKwONqpicWi42JZxS2V'
        'K4Ur1dzQj2O5xquPf7k2W12rTs1tCmQD0K6E5wekByRk4toNFP1Z80kCHm5MKZHD0J6GSM44a3Xz'
        'vWTDozEy3y95+46kqg0/+/D/GOmuU5Njk9lkMBg+dfqhT37/v4TaDq8fdM4PdPxjd/6sc+6Ga249'
        'K+S7gGfL+EU+LlA2/CJTWwmolK59MmIhGspXAbdiA/ufT7A8yyhrAgrWXQUN7CZkirQdqVFMx6S0'
        'ULPwsW9/ZH1We9FhORr60SOH/ux7T3+TxvFgyWtR+tFPv+4XClecDwA5z3q93+oeKMmmAHWTfrLt'
        'mmsatOneM5qlZj6/wfGc2X65ISSzGYdGmlIzLap1/JWw3cLCbmzi0I8FcYqpGenUj12pTkJtwSpq'
        'WJuf2jbcQfIihVmbZgBVPbepuWFAZddR0c1HbzU7USRYiKoOtRM99AtZZ3n644YJ0lkjoHht9jg6'
        'xJUP79twRbNAaCvco6hMZuvvuefv3L3/h81sZbBD0lAKXBYGOI+D1glIpAdGJeMJ0OgSWYg/ODn6'
        'x4e/GpBDjo0LnQeOxqX04nNkQIXy7Oykg8tb4rv5vdJsfcaHyyWxEsmqnlOoImK6Pl+7bnnfDTtu'
        '6RpCXmTwPYefWFAmzA1iaDaD21qFT7NDnbiDk8OPrR3Msn/Z0ODdcLBZ968I82GU1EbzpV6cU2U+'
        'eStPiTckxRABBkV5w45bnC/iWk/ma6NyibRgwanf3IRpQX3CCnXinLjkX9OU9P44rYZZ9+q9Fmh0'
        'mZHQ3zDtIW/MYMtnpgTMpJvUKvEZD40Kj5m+K07OdcoWT2nhOa/DSrnrg2//8HK5LY6ZbaKFXNjq'
        'X/JZEXEIxGy1OmFkYAA4cEtpinn70IDegjbjTNLPNqlOZTqfvnmzazDy3jpqKhZizZihnjSiGdMk'
        'orP6tDHEgWZGnp7i9GQ6Gi6pqIi6JmJxy8Yqbv0JECWxf+Wu+179y14HxuDEf+Pp/3l8etCpR6Pu'
        'aY0B6QcWwuvgbTf97KjYZgjZaFY0/98GVxZu+MTJbx84+kWvZW/J2H8v0urXYX737nfuWbq1tnmE'
        'Ts/tqqZVtW15u3eDKGiMNaUtecjsAl0Q9yzfume5hfM4cOxLRyePO5TNHqW0fS0tQOkanty9N/38'
        '0C+dz8cN3PIDRz5X6IBpImJXZ+tPmIudrfM37L3v5h2vf76uq/YRlbjSn6RnqbiRlNWxMNkNDOpR'
        '0+xxZAKCk+pk6YbnbtmNT+6Yh3XtcE43+aHH3UnXUzYP68aQ6drS5+qleqL21p2DWNyIBtAN44V7'
        'KUPT4NWj6Z2Ks3OOEyag4rTpjszaOtiXsbSTvjV2C6s4CvQSPWPa49I8zt2lVmkQeTWl2aoqCTnH'
        '0vn5uABmltZWS6pZnt1j8NqJ1rKYoW2XlwECq3mYIk0K6NEIbNGOQLo52zzP7x8Y5mGi4gJDPmq3'
        'dULshvjqrF4LrC/sCRaRa7vIx5xsbaf8OV57l18DoNRhlEfkw4LyUbuE0ELhBoUbvGBqE4mdbYPr'
        'XnnNGwduian5qZO8bJhmIdBZvbZU7LjgKYnncwJe6D1czLMksWEirZy/pIPZ82bOf13Op1r44q+8'
        'FW5qsQY4z5LZlfx6sbFBn+/ILGBW0dnqZbw813TrZtteLifgZfvSq0tw1QBXDXD1delixlUDbMnE'
        'yKsGuOqCXt6O5WINsOAk4KXnWC7WACJyBS3MVRd0iV3DWTfQlbKH9JI4vgX46yvlzvV8Hd/VqLA1'
        'MVxfJq72kiOf53OJejlDtJcq8jmXAS5sKc/nRntin0UZbItWcMNlL+brXDgd/ZIpoVypMJRXYf6i'
        'DcAX0w2zlTD/Aht0zvudssD99v8B3d0D6IvAROkAAAAASUVORK5CYII='
    ),
    'image (14)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAenUlEQVR42tVdS6wlx1n+vuo+586d'
        'x52xY+PYiTGRggMBCcSCBQg2LJGQkgWPBZECiJUFEStYIBbZwCJRJF7KJlJY8QpIPDZEiCxAgLKJ'
        'ECSQKCEhcuwZP2Y8judxT3d9LLq6u6q6qrr63AmP8ci690yfPn3q8T++7/v/orUW/1f/CODyx/+L'
        'Dzj8cMxDmsUdC7+uvr714pX7MPhRGz+38Ik69o3+r/5ws/jG+OPI7AREn8T1kZmvz00eM8+RWy/K'
        'jBRT1xcGVPlvxMLo1M0EU4PAzFgxehIpOwHcvsaZmRUupoGpiVl+QULT26ltz7B8cdUscG3yVF40'
        '0hFjpcIOYHEmVPtJUjAK0vhWZu4mtzQ4bVEBWkyv6laMKoxD4WG4WDcMJzWyJ9xiwYLVaTYuLmam'
        'QYgWAhn/ysIidc9EJjcNKiwM8+tU1d5I2a9zIVcXPV6wqMyaB1bdtmU84snRUWadKrpYy2uUtXhc'
        'uA1usTaj3QMhzs/AGq9Ws8lW3JJZ88CMDN52ZxVti8ROV/xZrFtfKj3qpj/zDZjff8tfiwGLlos4'
        '4ZZMyl8r+1XJ+mCrfhmS2LLbau6v4+NR1S821mUJpYk0FVssvX1G76+ityh9wcUuYTg0Cm6u1bGW'
        '95cZR63i0ASvkVo8BlK3ys0OtyZi5S0Wf+3w/qx3SqN15Vr0wroJjYwGi08+xLWsC/BYNAzJKdTW'
        'iJEXhiK0FqtwLVV55B96JHQhVa7aJQhx/B9z4bGoDLSZD7qPQAhWDT2PCBqZTUOVf87Ee7Tlu5hi'
        'kKM6u7J83uOCJVZ7Tq2hF6qYQV0gWmIhEWEpwY5zbFP0G4uFxqoBZcIyKuNg6m2oMtkpUslqAVNJ'
        'OurwAtZgYjwqQYujA5MavmhLcAsyyiqPegxywtKQrT+PFr66vEIL+KvWZiJnhBKxmSnvTBJrsen4'
        'ZTZsaFUtK4UvqtIlcDsopHCOV/1WMrFf7BjWxPRJJyxthIaW/3L0WHhXMjToLKM6uoChWA70EgRV'
        'YjOxsHQUT63SF5fD0KNjrGT0qZRVucBueTSPfXREqzWTyJobmoJ95zaUdTX6ZGhVmMcwwoXPi8CQ'
        'lY+6WO9KJqSqIxI2MBCmAoCsBIErvi21tv1rwDWtQ7O1dk9ZtosFbqdsopVy/lmszKToHVbzX1p7'
        'DuRs3wVIZla8up2QkbYkleWRza0zFSbALTpSmag5mR+yzpcW0l3Pv0kbQdZgCLTYmumNrIwD5xGw'
        'azIIZAYKTK8GU01Aa/H2MjTNOnTPu4xHY8tMLnkmQusy3KNwHuu9i44GhMxassAKG6qqVSsvIJPW'
        '/GGQN4WXe6msksDyklAMv8VMUIfPqmg1hAZKabiUJfz1UaKhSsX1q2okQXlk/KJIoibPzm8ZWnmx'
        '516/0mw0eTm6Zy0eza14KY9FqTKBYik04oW9fgYB1cWXzrowazX40zohMwt8lJ8gpTQ2BfgavdBD'
        'vWCBHujF6mFW+LmqCrqW98+tFdWoZgqZsGoSH9ZlwfJY+LKxGi5VZiLj9zL2tdoKgi/SclZ9zZiy'
        'UR7WLd9t/jXpA1QF1ygaieOsbSWhNg+AIV454KOvOSWXJRuCwAuP45kdrCYDwYoPKkyD6r7aEUBF'
        '8KctwTgF/wkunF+9PLh42WKtL6nCu73++A56AYChemFn8IEbfGZ+N9fTN5UcBFOjn+IsmdmprPzW'
        'bVZDEY++8io55q/S2lh4X0BjRBe9a37FZYkN8bYG5wJJQlbYcfRmpfAntBV5h880TyeSNdKi1ENk'
        'RWMm4y60Nvrh95mcJzN+L6uJUwX0F+d0FjwHBPRSJ1qyWzrTBNZX0FqxQkjLikRd8W2lMgvUVvBQ'
        'q5CnEst2iavKjz1m8c34m+Lk3nsOq2no3Ss74qGFIQD1QmNghV7o3VcWQMOFFK8KWijgB1qDFlih'
        'DgpukvMBrOU0VFYgMcAAIr8dfNTSAM7jaMJPODN4s0M35v8WuN/jskFDNm41cI5rS5GC8kY8eVE5'
        '5WQIfLHCAq9HQdVB58bwqwJDH3AG3u7xN2+CVAOKEHD7oM/ex47oJ98I/dAVc2ogyIAGOAg/cgXv'
        '2C3qnITMfs18k2AKa8KhwtAnXmzXShu4Mo5UHXxdU0kRf7QEY/C1c/3qS2iMc4MPrJ5u8bvP0Dj7'
        'Skk9zK+9rC8fcDK8T7gnfvJZvHOPXmr87cWcYcmMGv2wthKor+JHh+vauiCHVWHMEdxeOrUJLtsR'
        'jzWzqTsxuNHgLat25K4a4L50yeBGgx1gSUgnwI4FOoWh0dAK95Zdh8rropl6lxzoP6aNZks52HHs'
        'WPGCIFVO60EFHISe7AELSegFQxi4vwRacPDVFrCAgE6yAYqTKnphxJas+etQNCImgxS5Ih/mkGou'
        'a8RipIPcJBdkXpmjrTpDAXaAeuQGdNgk81MTIqwC7bIAWXkMDCVYaJgPKw0/awPRtl5XQ60GzVx1'
        'gW3yVh4xyY35VMSLsli/t7yDjAPY5389Nc740OXfMsQlDo/OqbrFGKqfqDuSPKUMsDdBcGUDm6cK'
        '9oqL5FzxBqK/frhm1YN/autsxXFVEvVVlfNjvWnx5XMMK55EQ33xASQYEpAoiufSVw7DHGmAtHvg'
        'XAG0a6AvneNG46JVAr3w3F6PNaxQNnCx1LmCImYXIoszMYah9PKXRwqrFd1D6IF7oTX4p7fwCy+q'
        'gTP0lCxgQJCahKcUBePyMhG0E688rka6rzR4ArbEGz0+9gx/4jo6i4Y1BE61kiwrbK8mZCRUAvF5'
        'Q8m8QVzC5Uqss8EEjca+4YS/GBF2NCzC8B87wBIW7Ec3YIHJAQ7uTzDDrzYjLPCKk5N6jvD5pXTx'
        'F3mkSt6LgpRRpHKjJmeNF/PtZlTgSPdADKMHUQrJXAFiOJ6DmoNztbGmO2ue7jZXjBZUwXCBhjEY'
        'aG6rCCov4raWwzvS6C9xzehxODk6EQJ7aVx1wmh0fOUKg+Wp6d2Q2x0CIQ1wOfxwm+iH6XDWjAv8'
        'YClGLljUVVxINXUPberuSlSnMokmJtFnleVTzIPDBIxnD72w0htpTjmZpr3CBX87V+Y790ZCDUmo'
        'Ac2AU07l454RV8lzcSk9YoHNq6Br2ppsz+sdsAItrG6fA2AVMB7Tt7FQC3agocty7bgcrLPtIgfO'
        'S/LK7Uav4V5xdshNqfwq8k7qxQcW7ZDpKNhEAJrBTLF2uzOwS14UozFmTnvpLCdcA8PF27YyOLJA'
        'Q/zOq/jTuzozDkcb4vppLR6EM4Ofug4zL38OZAKHMFRoyVc6/d7rMzTth30cpRbj8M++1YJPtjoz'
        'OAACDOhRyWyoNyx+7Ap/4yn0dupYsTEgnGhwcaEKZA0lWcBml8JVJXeAikK413p96QGuN+g9rzMs'
        'fwOcWz670zMN2pnJmOIlDYKuEwOK0ryLOLpajiM+WCmFI2Oomwd+w+sM4tGqMsSdHt974v7VsA5Q'
        'iR1BBHJ4O6yaE87pk5e8YwLF1EIKGlmaFjyhTgx6BZICB4YZ7Y0zU3a0stNNx0t1mKL8MIRWII8I'
        'MTAC4I5ox2RCDJ7RAJeMw+9Yzo19QHvNJwoss1oGVex12BuHKeVownX7rB3lACcN8I4FrR+/a3hx'
        'NPeeuzaefx5+NGOAM1oYMogQI/8y7xIruU+U8yLDLRyKpyHbGLSTkTh0WanIimCQC5GpChOgVUkq'
        'yQoVaVwp5VenM9b0EIAZ55XOWM/XT/ULJKdPHygX0g/fNZVViEq2GmKSiCf9KUvMZ6J0sQg7Clta'
        'fKSbdaCup1SSj45fn4bCjrvEcL5ygJetggQZbriHwSFHSlIQiYbjpnHxf2TtfM7Lg+Zzzkry385Z'
        'iTtHMItpU8lA+dGs1mvZipTk8cJWWbAhPvMmPvqKru0g0UI74KsHvt7JcHKM+OBjeKrBA2FY1N+0'
        '/Ltv6qHmaIYeqGUICefAiwcgW7bJEOoIfdUUezJmezvh8RbfscNBMEBL3O3504/hZ26wtzLEt6CY'
        'LuuEy2TXegeIYexu9frsfVw/oBvhgT3ReuS8Ad61w3M73LeO/PrPg/7tAezCfnN0nMP4nTAIMWcM'
        'LkGp+1GzghDag5aHKPl2h5cPbgoNcMfqR6+yonNNQa+Y1AhxlRNWvgQ1iVssdGEEgMsGZw1OgL2Z'
        'sif5D9ISB+Fej3MPTTs16Lz1S0/bq9njy5O1eAaHShj50EBPWIdLYEmNIawBrxi3fhqwg5ptdZo+'
        '/KAMWBmkzW0cV5Xt/pKbDl6hwoXXC51A48IbT9ToBmEAMncGnYWAhrB2UD77MKECJbvGbGv2nBOu'
        'oCDyDuJTkktB/MDOejkrZectIyu4YGkUAHDNpBelPQnhZLsSV0VcOZO83LwPzAxhDoEjjZGNlW8i'
        'SLjXB2ltMxbO7IjWoLPxjvYXiaeDlrNtBCUX2vscybwhRwCEnILpcR3IF3S7mEgantJABAxpwtC+'
        'WPDEEhiZCkP1KBguAOyBzgn2R6pWaf5g+qkhQYfpD9PQq/jQcjaJ3mqiHyOTy8xpBKupmdskF0uP'
        'U9JNN0M9KHDYlD3UB1gi1+J2vzY6TfrTWpsmdDa2LzLE3R4fegm3O+0IKxji9R4vdzSSXPzvMetj'
        'dP10i71BJzSgoe5Z3OyciRbSdRej+m3myBR4YCmBoTBZPqEE+hvAR0+0+rYGdk5o9ZGn+dwevY3g'
        'CmVLhWPtJRZOmMyUbW7rE3AufP4Bbh0G3oOCGmKPuUxjDGJcIjtMyte7AQgiBvaRaEJn6WFqE904'
        '0/Hp0IDKyEki8VpQvb9k6Rrg1gEvHiamh4IL2NYkCsu6zDQ30G7kzUtOwgCn0KnBbrS4guxotEmf'
        'nprWr3akAa1GOY1kZzXaEKVosPBeSkoEMahP28ibAYXuS5rdU9ydXaHWdbinJXbgfn5mzeaRNSIo'
        'XSAPUF17JvlZ/KjD8dJHZrmwKahRPy3oAXheJlEKcl0P7ZXIKS5atOyiv73l9WYd7mBCvDq0I5ye'
        'ecK5rPwcNSe3qdY1r0xAVeEQFz8Eer/Rj4yBCyemMAB/BjiCizCDxshzdJyIyiAIE8P1sPCBtLZ3'
        '9kquysLNX5Okhz0DNUe8mNBb1cpBuJURO5oAlg9DekPgSBSMUfgUxIzLFgCt1J40Zucomikysxa6'
        'b0MsdnBNPlGAAA6KywE0xAL7K601Y1QxQBqAevQP7IhrU/QRUHGebveeMWublxRYaC3CdFWMYqVi'
        'ewHZT2Z6Iy0qkySNxsXZt1fbN/7yU3f+9i+028OJlAjbmyeffuqXPtzQRLaFyHWOEX2ScfzCpjWv'
        'fvwjd7/wL2hbtzyM6c/Pz77vB5/8uRf6b3YwRtMyX4L5kleQ7LD0OutS0MLOm6Ot7j3DmlL0IWdB'
        'wIjMRJVvuZypkuUed//5M7c/9QfRp+7e/s5nPvRhyI8pOZq16IYKqlV9PA7oidf++o/ufe6z8dd6'
        '5eZTv/hC5xyzkt1Ton4figqfWVnlkdRPBCaIWxpKRmp6pntWKqRxOEeWEXrbWWh/iU3DtrVdx8H0'
        'W2uuXZ/S/xF2UABCJw8tmJEYR1w2RHvtBpsGTYO+B8CmUd83V65Z66Gn4/+Z6aeyQIrFxFjntbBK'
        'HMlSaYKYn8lyXZgWUPmsl5qWrhlGru9Bou8dG2UtbK9BFDdJIMCpoRHj/EuahBAhEk9A1qrvCah3'
        'jkZ9r34sr58Lypkms10THxdYS0vpJ9YLkpieUVPsiLAKdDDbkIkqd10JuneWGmwp/sARG1VKOzNK'
        'q2ebZZU5ZWSCd3y7zJRoMiemizNbpuFLVEkTWdf2Sch0Z0z27w27GfkhI/19QE7UID0ybBhLRk2F'
        'OPPFU6wyqRwmZb2Cogsy9Xf6/tOzTuZH/ixm2cbSMtWCIqxqWbaaRFSLQVMPz5keDhvCSpAk69ry'
        'WAtJ1mqK3kttkoO5lEu752uNGZa1u+30A2fPlKvBiiV6DE5E0toBVCxq2nB0HrBei47gMJC4mwgx'
        'qwIHvGioRDW71g4gkiH63uz3A8Kvfs5AaRpwmRUxuHlvB8EEQUMjwRqDxqhtByeMpsXhHI1pgAPc'
        'THilHaP8OtD1ENIIUWMhhctpkNdXba5hU33FnWOhDPF6h/d9Vbf6QV3DqBWexkt7QzvmUxTQ8uHN'
        'Fx/e/Ma+bWCt2z3W7k6vXHrXd8vKXDFo0QAi7FvQwY5nK0RSZgfmN1fJZlStvIVeeuurX8T9N8Rm'
        'CGNpzKHvT248sX/2XbYTCOPaOKuJ+Ab5yiWHRv35t/P5S+g1kB9l/eA6ntPWN7cNV1xi2ukJyhSG'
        'z/QA3uuHw6kG9Z8a8nAu87a3N0+94/UGHT2baGEf9u1pc+fP/vCtz3+u3e/VHW68/4Mn735eD3uX'
        'ZYcNCQwp27368d/vbr0s0ly+8raffcFcuXb13e9BEwSrj3VorM7v3d8ZFzQ1RE/zRrt3+J9GwoE+'
        'DshRjMSwRHKZChDc0Kyjqv2VNnD/PkWuUeyvxuruzvz61/7rfbduPtjvG4mklQzREz/5/Pd85dLp'
        'JetQIUPSWrtrbv7JJ+7//aeH+57+wA+fvPd53bdomhni4yS15aE/f+njv9nffAkAmubq+z9w6exM'
        'D7rJoPTAqbWf/Pd/ffv5+UFsdmaocLrU95+9fuPnv/O7Lss6lEQev6ZBP+lVz5GJ7tnEppaJuUSM'
        'Gd1DsfSbcy+HIHV0O1Ec8aBL0om1u653i90QgiWNIYwJjhOjIDTXzti2pmlt31kaHTr1/ViJB0nT'
        '+A/xfnP9hn3tFQDNjccNjWOLnMZIQ7nSGXja2dPG4GABWNJYu5ftonIRF1rVUSM1S37xWpu37yrG'
        'WKulB3Mp/ZjFjqo/QxnTEVMBu+EgFfUdtRf9WKuus4C6rr16bf94e2haNsEJcW6xGjSHM0OjrnPZ'
        'VpiQScTQ1sNKZO9mBJYgTT+KFacPV5jsMN25rFCelarDCqnGdnusmTVEStRfh1jxyMhMDAnHJMjJ'
        'E3yakC498PWQb/3Dp/XmKw/vnRvTjCWr02jJGHN4+OBw57afw3LyicNnWE5lZBzbvAz9DUzk0LTo'
        'KygyPr1omQnlxPsKbVe2YdNquVNNWx8tuRdO2MSkuxzzMFlhZ2C47NHLCVaQCLz82x+uckGj9IHx'
        '+g/r4ye+kM6eRUJEL+/GRGpLJZiSR/mA4zrkrPT88ulDeXnsIEXu6WneiN76C3CmdCxAY2AamAYC'
        'jSGjnoyLukHbD0NlmiYYzWFWBBLWwsLYpmFvYWhJy2FvRGeJOMbN09Vtr4wrsjTtlopflh2ApoDQ'
        'wA+BxsJet3pO1RvbXeq82WnY9rSdxYkLdifb0AC6fw+2l+0BwPb1rRH7N9+AtZzSXblAwFpdoTW2'
        'MwfrAiNj0B1OD11kre1Y/0QvFaggSFgBRMd8wNaK/Rz/4tMwo1JqbvGLnfAfly8/df1Gt9tRYmMk'
        'wFoZ87BpDRa1BT327/3+S3fvmP1e/VIymmwbTRDo+/1jj+8uncoGjolgT/OP188ev3zaWcAYQ1hg'
        'Z+0XTq82Yz3UrByYhHgR2RO3Z9paYJpoW7mlg+TiN0Pc7vC+r+nWAXszZ8CaYThnRTtyaC4pYqiC'
        'HyLvfSAccovOAmgpBcU/uXXot9E1Qx+zg0btnJwGVDDkwxFB7aWWU5sKGDuxSHN13/Q9hjzgU88N'
        'mbCXMwaBzbYaunbJpJfJ/kLnIgEPLO5ZHMbfW2I/rqYpv28leC/Ro3bFFB906EPFOc2wICdimWQo'
        'HBqghc7rczgKWwDCSvsxKtuHPK080fXwiAfgEFCcI3ftV7b6gRq4aSe0i2a0K262IIdpgPee4OkW'
        'A/faAG9afP1cfs+pQe/mo2fikEsphUZ6cZ8oet113Hqmz5mIMw2kgD2c9p9D2ibqS1gWyMzj0QlP'
        'tnh657oGDsVMl00UTIrbGoUHDVlXu6dv6R/nQYo9sCP+6i4+9A1dMbCaO/4Ue+IxJA9iqeFCgTKX'
        'piYrsxi3CaffSFITdekqDzzOThjqJn/lSf7yEzh4/T0u0Aq+1Dc0dyZgsVfEogu/kaZGVgCsPHBe'
        '4RGsfmmQIu3WlCNoghySe3Gh4aXP00YjxuAQAw+wZYBbMqTkzDhMxvVQUPmQ2a06iU2EDGMDFTe4'
        'GPzqTJyaGR8dzIYUnZumWejs8VKJT6TXWNdnBM10/DijRl+zlgRzH7EQJJlFP5MRlF9IKK/8w5Iq'
        'nf+kXLvYI3pHV24fpjTyfvuiyTnJhPHN4uSFGc7hsnnsfMvkOX6uKpKhfjfqNJGo6RlrheeMfGQF'
        'NJogwyRArzSJFvfwU41PbeM2G4xK2uoPHg03MQGwAx5IxsKOsHpr0DKt0WBMJ4tBqbxCwYWCOfBL'
        'Yby3yLNNU8sOJ3yX73fcbrDCw/FzjHCvR69KCDLV3qaiu0+bIdhZlKSkiIhg7Tr/ec3oPSe61rC3'
        '7ujWV3vc7sdWEIyLdhj0W5kRvEV+EIl5grRMC0J0GR5Gy3l4QwdcbfBsix6wUkPc7fFE69kj1px0'
        '65kC8Ygy1fLpK2ElBJI9OOeFacHzkfrugT35W7fwidd11oxKY0ZToHp5nrIRQlpiRY9kZFhbLLGB'
        '7lj8+Bk+9rTpnExRBFvOZFp2KNd7xme1um1+fjLd5ohUj7o0A2eAU064Cs3I6Tmxa1CCrnwjS/qx'
        'dio4oxb+cKmu0hTy+PtpynwJCC2woxoNyCzLxwIG+zcog2H92YBmY9gU94ogl83jgocYm6m6zhBD'
        'NqC5EQ2LJ5H5NIhyJ9TEArxJiD4L4F2Sy1hcPwdoGvuAC9MDD61Gk6KY+pMpt0VBq6lE3ONdipCp'
        'eOt5YMAolKHMTBkr7MMZxUrEXFWgsVmBL7aYZViTYED0u+1p4Zg0L/ywC65x0xXNXAaaLLRVyZqr'
        '2ubdW6vC/Rgwr2OU69p2LjwU+uCRIpfim+uBt5LCIx8VRA5TDstFAXdYJyYv3ZsAVwlEMzyVthzH'
        'koWHs9Dnas+4jQ0P8hMb126Mg3rW4NkWZ81ciu1wSjm5hyE8Tf6EZI6rd4GMjHIgIdoZHhOtxYHK'
        'c8X9xIkJZxZPNMuTDJRVtzGnxq3PpTacpHf0CVHBzD+weCiYyXorxsECwkuRf5tNmgcp+N033JiE'
        'dVKzxQnwOze287VW2pGXTVy9R17kAL1yrUDEB6RD3UdyGmBoa3xxQBSRBA3fmAr0lzKRxEQGiUHS'
        'IPndRb1kWxuO1XoERxxubdqnWlo4b63SheC5UFoLSXxwaEqAZFbkDwnZpvwulALJTX0nM3Bw7eHt'
        'hUTsggeTXKSIsJAPlk832/wkDIdtW066rK9PVOWznFAWdsAjsULfisNMsSI4UPnIxFQ1GFl78wId'
        'vnKeUfqdZlseoE0HfuIRjX5NA3HGcWnyvXN1x3LBbu9VEguC6s/M4tbjbBnmwCySEo98lBONRKRy'
        'NWiiZcAoclamumf1RGrmn1g5Wy+tJM+mdqGpul+4No+4tLEvBbntBHhNASwTYx0MkVZO88melZwE'
        'IXy6nqsTUHRfXGPHVo5WKZGdpLbMGTdfxmLLcwY9cEotgLInsedGmUcc6IxsCW7tOT7MV3ywWPO9'
        '3RbVOh7m38elnDtrjrh+lnZpnJSwVCZ39GF8ozg6VhlD3uIVlorMTa5idfIy51EQyB7F6TfdooqH'
        'WcV6lpJin0uyzOScBrl+mHvFWlO1YdFKABMXbeGIkGMNt2Eqqi/1ct4uSozthMk8k6pjg+p9qEJF'
        'eLKiPBfecAversqB2HIfFtfZtrNMzJpXUdCkW9qw5oRN3FDFEGj7kb9c048U1k1N6yVubNJUJcxK'
        'NkdPeoKaZZUir7as4ZQQRumP05rdj5t3MXOuYk4Fg5ri90yjyPTlJr/QWBdFYEtxPXF87rDsiRo3'
        'l8mYL+U6u28J51DsiqJSvJKd4JwPECusfxk/UfnQ9gvkzEw/WOWh0lXOjMUkJmm+MkKgSLZekQkz'
        'FWZx8ZFaY58LsGISv+WxzqAsBdSiSUIZSlHxFFWuM19a2DQhVbKu5EFuXMsmmDXxUjEMZ94lci2E'
        'T6bQrDiWsviKWJfBKZetVFg2eUc7ZRPmAY4+UlqdoVseLZ2wxiith2bHUYmPHGBXIQramK9mkaLK'
        'AJnZtbWezqfEFoU4WtoMu6q+I+takiGtZuzmCBhzYRyYeqBMKCWfGFnEeYkm8auZlJI1KJojBVbQ'
        'c0iDidkRZBUsnyUmWUBDy7GtihIlrkM6pSdnKnDillPej0LlCuEmC7GDYvVKccvmj7FSFddMFtTS'
        '2s4HMFvql2bScyuCdRJBFcMeFau5ktZjARak6TBl1qNvglgDjLAUFEQfxosTkRWoOjfdh9VNOQtb'
        'Utna/KyV59pbFlCEpGwjxNIRqd7/tc6U6RhXX5eRZb0qZ9cAVWQeyjAHCepd2oCzFmQpq5qOZQHC'
        'pvO98Ug1d/ifU2asy//rAydupSSz1kk5hcQKRKGVAtrSzWogKW3vvrP2kNzKeShjoFjftO9/U+Lz'
        '/+xZLvbnvwEESofnnxp1/AAAAABJRU5ErkJggg=='
    ),
    'image (15)': (
        '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'
    ),
    'image (16)': (
        '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'
        'k0HOVjvvCvw/aOsHYbxR3LUAAAAASUVORK5CYII='
    ),
    'image (17)': (
        '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'
        'Ax6rzbIJw7efAAAAAElFTkSuQmCC'
    ),
    'image (18)': (
        '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'
    ),
    'image (19)': (
        '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'
    ),
    'image (2)': (
        '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'
    ),
    'image (20)': (
        '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'
    ),
    'image (3)': (
        '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'
    ),
    'image (4)': (
        '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'
        'DqoAAAAASUVORK5CYII='
    ),
    'image (5)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAjY0lEQVR42t19WYxt2XnW//1rn3Oq'
        '6tadum93m26HHuyk24ndJgI3tomUQUqI2sjBygMSch4QQhAkJEQESDwFgZCCBH4gvCRB8GDFZCAi'
        'gxOMgmMnUYKJ7TiemtiYdMfpdrfdt+9YVafO2Wt9POy917TXHk7VbT9wZbnr3jpnD2v4h+///m/B'
        'OSffnD8Ugfx/9edevJGe9wmyH0b+YN6lJj7DM373PJ8feuWzjj6ib+rYnXiPBlfOfwV2n0H5uxwe'
        'Yo7ei+db+8Uh4uS3OTwBjG6A4aFh9uYD9yTHVh8H7s3+RzC6kJn/HlNTy9FVzOIrMHweYQTI0bvv'
        'ZoLYW01DRgbZDygPvV8mLC0VZCMNkukG7T7C0ks1v+OZNiDSxxvcIhREg41sO3b/Spk2JAPLTgs3'
        'Rn98s3ecYYiRbh//VyQjHqYHuXHMHqm001B+AvaW+TyTF+0osllDLH4Y+WUxfCNMLA8dG1UOWRjM'
        'NeLR4Iqw3QfBfHV/B1LDykEjhuH7MjWMg06SQ1cImx9A89gYGVomWyExRJzv7TArDD2zx5/3RZIA'
        '7tmNJ7/EbspH/rH4md3uSCHC5gDD1kH4is6KGyYjSJ4rBGxHnxxb3pwdZyAN4UYuy+5zlHy4gUE7'
        'y97WxIygmekuR2yCGFvZko2LDaK/vbcnI2YOhcCJybQxcRtZ7BtbYbB83XgQ4+EO/gyDTwW041Jy'
        'lJABX4LIbI7EfkCy5AdCSk2edcRfA+nHMLYQORKYpnETUY6Skd50ZBv690Q33Jy3A5ntMQ7b6dn2'
        'kOUIIv9FvOqsczhPXkiMPdUsS7q7oR+/7D3CPL450InidX3GoWFKPA4K22UkgmQUNU2HGaULeUvF'
        'XXLzxh7mln3Gpt8lEeNuPnRgfMnS87H4iwhgYHHzlkaGM7bIyIXA1lJhF0SisYeI3GmcH+KMgEya'
        'BwBTq3VemolShhL7Q8zIJDiMOmAqNgPKS4ol6z+SyY+k9yFa4WSqMG8C0EYUGDEywBmBNozCikXA'
        'YxLPSUaKA4koCi4XA8u1Ce1CDFNEA7NpGLHCnIML4d7VAzgwzOhhTfNyHO4IbM13muzFx9jxa2ky'
        'dZ6h0rPD5Ry2IEOuCfPQwsmh7wMk5OxBlAnwchA+wzwX5R3eKEA0Vg/ALoDitDdAPiuY7VowlMqW'
        '0pTdIHkmP2Rp7Tng5c7Jp1skXohMZljPUWXBbr9BurowG8YAytUBIBrHgdWax129KCBBbTmwvfrT'
        'Nj6vKGVkKCzKe+oDzmq7J03tYB7ANNrBtLvONkpyhWjNjruzyTQwxzEw4mnyCZhKbWc7hgZtJknO'
        'nA9Vc8Y0NMOxJz086ejywWJS52rAHlIAQHUwoJiz/pjeYmwCGJeBzoQ9FGHOHSAG5o/MtIaDeSkY'
        'MPisZDeguz7VmRCKqY/3TBDn1McxYycSql/47V/7+gt/XK32XIs4tbmGj9rRoCGOArzj2fcfXLri'
        'nMMOuDwiPHnWWEL15O6tP/xv/3m7PQU0+Ee2mSjQ1HkF0HqzvvbIE09/3/voXKmEkG6aM01DtXsI'
        'hAQ9RnkNUgiRT/3GBz/9kZ/fv3y1mYAuZ6SfABWIgnSAfvu7/+rBpSvtiDNJmuJ6RvrCrmD6hw1R'
        '81THN69/5Kf/5fr4lpqqKz6yrd0BDeJAihpd37n1Hd/17NPf9z6S8NdMMX3MQEaKsPzABIysOM6L'
        'z6LqymL/wsHl+/YOr7CxuZ2v7EquoQgMaGsZ0EOJRwwZ0fwfknFBKCeUMQ/dv3gZRtUYR4LiK6Pw'
        'eAyoalTN6uCwFIzthCKNDVp1FrvFHpg18Kp0dNaStjV0zbgDpN8o3XzBkaPgBwYhCpQfOSsxxluX'
        'zlk6a2MDzzZ6RLcqSDhrXT8LGXGQu8cvVeGhi6FYaz4QQtreMNG5QJKhc006olHcq+2abcEYtNVg'
        '0pGgs85a5yxajorAmLkvheG4W4TONi/XBD90BOBITT9IREkYOn8gbJ+KbOMiBVCi85wpeqwK7xFX'
        'vpAZlrFpVmOyf3Ewm229qJ11thgUGQVJ50jh6sIlNSa7SB6BJHFN6VH6MR+gporTzuWFi450TlSF'
        'ItYWQAMAIDZ1Da0Gn6pZbRxFhaYQsqlEbAga9FWUZu3TqZpP/voHv/Lp31kdHNCJQBzx6pc/uX7t'
        'a6ZatDYKrb31RuP0tLbOOYpRPPmuH9i7cMnZWqBCu3945Z1/4x+uDi44R6AUd01bSqpiu17//of+'
        'zdHtG2qMULSqTu7c+N+f+CitVaOk7C1NperoOp/U/ReArVdXHrz25F92dS2AGt2ujx97+195x7Pv'
        'd86hrVTjrKXAshOekdT0dlyzIr78yY994r/+h/3LV+kIyOm2ftfbn3r8O79ts9mKwntKb2ittX/0'
        'pRe3tTVGST738V+msxCIwtntpWsP/6X3/ejq4DCkucBYaJH4yLD+6+36s7/583evf02rJRu0Wc3e'
        'waEuFs6JqczT3/rwqlJrGXhwnVtemOprr974nV/8qYVRJ6LGrO/csNa+49n3d/BflrQzxEbzpqTK'
        '3yCxORz4PvLEnSIiq4OLB5fv3790RRwFsrS2Wq5qa61rdysYQCkA2621zlHEOopw78LlNgVVdfV2'
        'deGKKhLgPRAFURhxz29ABqdh7/BqvTnVatEOLoWutlZILkSdc9aKY7eSGK8TSzEXLt9XqToRY4xC'
        'V/sXCoE44ogiy65L2En326pAlfEvGSdcwCAx1H/VOWtrZ53QisBZRxKiUIduU/tkW9E4unbJgULa'
        'dnKtc65uLlLyqyjFQhjBupuYh06ZVq+AdkIAhZChGAUR0kmTFFhr0aUKzlp6oz3Blw1oRnlPoJyI'
        '9aDuHMAaJohoO6AuzqJQZk05cd3f2Fpev2q0vaVztbO1c0xLWGVsBQk5FB3mQ2ctOqMeFbPapUoH'
        '/2PY1Z0JKkHVA/RU9n/GcMwaPl1NOe5JBCpKp4jwYuiiBOYQY2UU7dq3LlwBEW2oHae9wytqKjXn'
        'AmcPLl3VCPyhULoJbWLKRbWoKqOEc2KdA5Jd0qD35UGPPXBsFMdmJd+m1W5J9LhnR88vkynkDwCv'
        '3Tk5WW8qxWbrnHM9a0I0OMzpyWc+/J+q1QHbvQL2kb4uSkVrTZKCAEUUenpytD45EtUENWpWp4Lk'
        'y9dvG1WSq2V1+XCfjAacEa24+2LBFjdLqI1KI9+A3tbvRU3VrHIKmde2UGYJ0hf9EC+g1vyTNKpf'
        'v37nldduGwUpC6MtSzqCE0hCzcnd2//jp3+8zexICDICUfuaDZSA3DgB3lxgsTpQo85R2sUfxsJa'
        '98JL10XEOd5/9cKVi/tMK1Yh10ZoVshj9Ai2EvQgbhmjrlTz4FTMAezYI0KgezgElF1UZWHUGLDH'
        'h2ho4QI2U746vIIOv/Dodrat83adbhpBdoMtzlrvfjuH47N+GqOAWOu0mzSgaRUJQwofw030RqUR'
        'I4YArLCbq6nMbX7ppzX/kR0EHTvgjQKQ0qAVJOm6sfbj2hhbBHaoszXS9gFGEB4jVA8Jsymvxgo7'
        'g4VuoyKYcDKMiGOClotfQ2jcVGyemrnE7ilYYkurXm/C9AYI/5jx5NQYY9RUDhYixqBamsoY6aJN'
        'ksaoARIALFnJaIZDYlPGrBDsPx5sfoRB+YCqZ8f93EaIW/zOqrqsjHX0mHmlMApVVWNIgWrzvzTa'
        'TEd8nI2JgUSMxQiHI+X1wuXq0+PTo9tGK+ssILV1L7382ul6u9nW8UPcOV5DwX75HOicXIT4pE1a'
        'hfp467hR5u1E8QGLDFR06Jzg6Pj0y3/6jSZMagxYVZkbt+8e372zqpRCNeb06HZ9ehzAU0LiW3MY'
        '+secihiHo6jh+L/dxaovfP5/vfbSn1SLZVMKNtXiMx/+jy8+9+lque9o/RpVhUIzZhmKwbxfskib'
        '8jqHHzxEjPOh0C6GeD9Fds+DHBA6tjl5156k9Xb94ONv+Yt//e85u22e2W639z/y+J9/6zOuzSB6'
        'w8KBNtgBJl1qgsr1lvEejOaLIPnoW5959K3PxL964ZO/+dIX/2CxqOjiaK1MQGzMNxF8QRigGH3w'
        'oac3xb6bxYf36YpHr90C3spE4Rcgi6qFSwBRGLuxVx98+Onv/aFCT9VIuMPUSBSDRhYyYcpOLqWX'
        'ZThrw0s7B2Pq7SZbrj07EDAotg0rYDpU/ruI8tUYUGC+VNB8zvvjNgBoQ5w8bujKS96pN5UDESUg'
        'dLVz1lnrawBQQHQwVMFUS2OxJDlAHeEYDoGMfwioRv8GVdP53n4ZO2KOhabGUDPOTRCT6J/BF7ND'
        'hNt8IGBZTSCaBKpMksXoCv7fo9SitW2OUDXN++xYHSoVtnoLWicYXjJvMkf6sGLign9N77iAfhda'
        'TNbuR/3oADyflnbZBqQrqLPrnmaep4M5aQZRYxTzpN7zmrLVPasvETJNRI27JOO8KENNhwiaGFBB'
        'YP4QLOAV8Ck+ezm/J/ehTFnvxhqI49jE0waGQ7tEWGro7kgQMaeW6eX6zQFRjwKj/uf5XWOlCUAP'
        'umSW1M4hISHEK8gI0swoHGjjOLbgT7SIW1+QNO53A9XYHx/0MMn8ovGJ7oJofzUjHDfCM0NafY6C'
        'dIOGRcFszpEyJCkcqG7t1qLE87B1Y18QUDlHJk/HHN2KfvLmuygGwV63Y2QYgknKPGXnowHkexHp'
        '/i01GWedOZgBX2K6rUoH+0kCu2YeBWZo8uixGW9uEXJRwFs4hhaVksVqXYYq4B2vhIFmPn8ZMAV4'
        'pipzs8rgTiTaeLmhz55sqGmOOylGjNYDJtsQkDKTYxCKSXhFV/IugkqxCFlx6xQtZVs7vxy7MAUh'
        'omcYJCLpm0YhYmoX/96iUm/YOitSu4YelGXLKPf7pXh7qd7UEyTBBBpRDY57kS7O1DYnrX1Ia+Jx'
        '0zX6hBQnUimuH21funmqCP6GlGWlT1w7CA/MpH0fQU6jg6rjeKYF3AI7z6ff/+cbJ8cb28DfAASo'
        'HR++snf/hUVtHUQijlw8y/M6cH3GmOhDJJF2EkSVqYlFShZG2c4o2sA0xo4nq0MzSS4r87kX7/yX'
        'P3wlu8D9h4t/9oNPaNIKml4gIi54BC9skAz1ASBSW37wEy9+4+42u9cPvf2hZ996bVNT4xWPiEE5'
        'zRBk1NGPYsGcw8wllSEnM9rww3IgzIHuKkpsmcA2vm7qMBCjbdlfVQBZaAeXZro1nZcIbSiRs2Sa'
        'TcbfVAhEjMLfAuj+2lTV2qdJpnW6gwzRts0+4UH4KShzvCY82A0ATNYkU+Zwr/kHaeAf92w5tvWD'
        'bvn4Kla7jzsX2y320EqWmktfquwgpLjKzqjozgBK+JI8ezV49l4QURW1wC5En8+LOV2SkridxNly'
        'QJuqkBxSCtkMB3rrxvVeGGdfbJxCTOGMcxZkvHaSE2kr4lSQ6KoXPciFmM0+Z69LEkMt0EUnjDLm'
        'AxQ0pobE5oKzDKsNKaLQZFcKqK9PIRQCI9WUkNIKpSH9NmEVe9iFQiNeTxc++dptO1+A5760j+Ci'
        'HNE/yKhA2mAvu1+4TEUNssgKPRMEmaVABAzSUjDWyO7D5CaOsra21q03p44hRm0+XFuSzkF9tFPX'
        'NQE6ocilg2WlYsku8W4jM0A2tTteb1tYm1JV2hXIICLWeStEijS883q7qe3WWqvGeESPiEHT8R43'
        'z2bsRZ9ZUjUgCVFN18BiTiAwVq5McGH2fTO70afIxcNr919YPfHG/UdfXS2MEdU7t687u3WUBy4u'
        '27gScI6rveWbnniYZG3darH4uY//8fOv3DKKKLcQBTaW3/LAxb/5vU8KxVorIs+/8PLmdIsuyL12'
        'uBRA6Ey13N+7rKpO5NE3XL3v8pVqXZ+ubzlrg2iclHsdQytTFutgsGUdWf9Cngdwqod9kOQ17GFK'
        'oQO6EJR01WL1w3/tH1zZu/Jsbf+5IwCzv/9Lv/pvn3/hi8vlSuG5cUJyb2/xxOOPqMI6d2Fv9amf'
        '/fQffeW14mr8C1w+9dRj2822mZsXX3r19HSjDWEH+FvvfsRATzYnf+6BN//wD/x9rBaiMJvNslrc'
        'Wt/8hV/7wEl9pwP9mbZIyZl7bcGoTFQm52K8FMMJYgSkvAnYkQzIFBBt/7uQqqpWRuqDyghEHPYM'
        '9le6WBjnXBPXt4lxzc1mU1XGOrdeSwVRQBXOMWpxhXNcqpyenFrnADjXsr7Y5cOVQoGl0Qt7y8uX'
        'LgqdkNxfoqoWm2PGlRlflitwFTChoZAJmqHQudUzQcVYs3XF6NkWFHNmxhlYenfmMRzIxkfSKcVa'
        'IbWqHOEcnWu41Yw0TagKeO9JcWT7/9HtHenIDi2KiBek8+UZJUmplCJ2WwMQQ+OUdP2Yva3NcRfu'
        'P6SMZBR1ekIihuFm6yLMOS72A5YtUUhbGrxSGy/ajBagptImSNEkgu5+JIWiTe405hEhgkS6A9AW'
        'x2vRHNYOjqqqqoloX9ojNiIYNQZO9pUORmdLRytquwqmcKISFP++YYWSEEJVrKNzUPSAw5BTekpt'
        'Ef4I4n2xSq+P6ZF1PhAKKkAnzkFEFTEUOir+gYLcBbOqWVGaqsCM0Vm0XM6s76A8IUBcCUZDxDGV'
        'VBVEhYAxYtTWLqrQF3EFUGhboj4zmgIpWTc16OP+Xn/XctnirmpksRCiYwB3nXmFEeTgAsSUmG9q'
        '1eL8pZKdWlsxQ98/cEMa+4KoVtg1QLL+8G/9TEM8B9BIN9y4/XJlFi5pPZOoOQkAN7X98R95143b'
        'x1cP9379D174qd/4nIj8nWff9p53PHbj9vG1K4eObHZVh8g0zZB+/blFtXz1+p996Jf/tULoqEah'
        'ujldn27XaBjU9ILcLDFBkWPR2EWcpBe2VHPb6/tdwT1+SMQrCPA9Mi+Pxvy4F1/6Ugcrt2DaYrFU'
        'aBx7IOxTQIVWSHn749cAXN5fPPfC9ea3jz908Xve9sZbxxuSJxsbUAVEJcZgoHSzPfnTP3tORNS0'
        '4wzVqlqUmTx9FhPTkiPH6dC9jDX14dVZOgPCHo2LG8yY0HEemcVIECwXex2m77n+bRHGk3U9WNFi'
        '/AqIHp/abV0bU1m0Q7a1ONry5t3TqjLNIm4go/4xAJ1J1L3VfiZUw0ZMwQPdHI03M5WhcepUsurz'
        'hLka5dQxKtXNPpajIUInbpeI6SmdOgMIZr2GIStpUCHCFy2lrdX8k5/52Oeef/Vgtbx+Z91859//'
        'ymd+4eNfOj7dvO2JB37ib39XkMopR8petz0K8YmOko4ITpsnPVTsngsoQUZuHekRQ79lvt8VzZjR'
        '0NuwbfkrYMvpE4WWlrbtnunX26qUh4waVr7rvuKcCOWFl2/9ycu3vewsRb5+8+jrN49E5HB/0XTQ'
        'eQcUsDg2ZX5ITL9GwgsLSgaRgkepBoUAZPufB1toJ1xENSYDk/fxMyv591puAu7Ikq4ei51yid5M'
        'xIZobuko0KqqVEHaxaJarRb+CR2TQVmtFouqstYJBAaIe1pTRjvBKL+Iyqhxr8FY50WKjAEy3bFf'
        'ZlBXw7fp1dl7xFIEBSBktX+MMLZ6iwVRRbWvlOKcu3X7rlFQpN7W+wbXDpfGSLc1Os0hx4tL3D0+'
        'tdaSzm7qbW0BjJ1B0XX8R52R3mkj5wjmwrZ9B1ASW6NI1l02rZYy2W6A6fJYv9EGCY0qSawZV2ZI'
        'CYxOQnFyvP7d3/1ssySc43uevPjet1wCPIkcrnMkEPzWxz7VZMq1dRCoYlDtpCywElWUs3KTToYq'
        'pXEDxhSdMChXI7upT8ySHkUinEhEkEG7FNu4kXny0yhbNRanMkDXeR/343mT4ggIGzzVhRug55IZ'
        'UxVSWihCyw1mV2Mm+llK+losT8Do0QSzgiJ/J21KXq2UZosCuDYGSj7NobsgsgNgKHX5jAHslG98'
        'y34c+iPUVBiXo3ynB2LOXECaoKqqLUDd71NIyBAD2hVk7xSaqC2EgzugX3YQKfQjj0ZpTb3p9GRz'
        'dBuq7ORqKFItVqgqxJ6jU+KJEYiIwx7cVzE5bFOQLvwNnjVuVR1aJF01DYC1db098p2Cqub06E69'
        'Ph7sfcyFLHt5WfwecbCHcVbEEOo9Kr9Y/P6Dj79lfed7FnsX2lULUVNd/+pX7t74hqmqtHEAnl2A'
        'JEJiXM1oWVYAu7gy2ilIzmaKd37XtIqkjROEZ92Bzu5fvO+hx77NdkJaADbroze8+ekyuXDIU0Km'
        '/10mxbuZYaIo/fuAUku0WIpqbx/+wI999r9/6ODSVeds7HmlZyM50BWeEEGYSuim0XqeqyBC9VKp'
        'qe3xnae++73v+bGfnJKt3EXYLbQZDJYcJ7Ui0O/wH+AAoyytIoVDO7rkyA97gJYyshMEbVmNMbkd'
        '3ZgH0ap4rNAVdHzPTqjwxIlgl7G3yZibeaLoPPgNKMeJU1hQ71i1HNzpf2AwJL35ylePbt1oUUaA'
        '1ppqeffWa1ATxdvMC2pRwkDK4eH+wlQt6zO27VGxE0FoKaTvzvHo+KQbVkgytT7x65QXgfXd2y//'
        '3y9aW7edwBRxbnXh4n0PPxavlYm54ZRYRzpE3gRxUOZ81xi0I2+qmp/7F3/3Mx/9pQuXrjSRjwLO'
        'yWqhq8o4thyokghrB0dQoHjnM09dOrxQO+eJ/fSNFWH0c+ndqsLR8eb3/ufnnXU91+s10AIPX0VO'
        'a3e0tRXEOioEak7u3Hzqnd//I//qZ521UN3tdDfZTbQP01Jg47/KZpgUEWs3dLW1Na2lSGvyF6sG'
        'j0FByIKMjz4UUahRA6DtLm4xikTgMPLXIYpsgQjXG/sMG29BOzgR0rntZtPIOEIAZ+vaO6oR6dC5'
        'y5RjLUrjWRTnnjiSC8xDmzIjFACgbSkYBYl40rUoGgW+fabrSdKEMBJ10CGiw7a+ImWCDXdvs1+4'
        'bZ5WVUQBwCiRag35LJKTR/lxTgO3zsuuEUE1pUNXS33IrSGmIz06zFa3teMqME1CA9k5U4YBmPiM'
        'jvsecElKS2dLjwRkpnNFb6nIYsGPLcMiY/BmzFP0RFXZhxFmSQzqLqdOxPueI5uGsZxG1AbJNNJp'
        'mClZIxGQEzoQPsk0ww0deGmgQHYOu701hk9WTQ5WJNKjejjEO+gfDoieqhlGINWR42w5U50WvVNZ'
        'OFSU7sLIGB1MjrSNhZ4K5W6PIcefDLdkfJqkJFTzFhEa6A32/Zu+iApKLmo9g5OCwSO1y1HMUKP2'
        'LP8+84D2Eb52qM+j17MrZZ/CfFf1tWzGXjopsDNWYmkgDGTNtIkKIyZPNuYOPJ4ZidhEUxl2O1Jm'
        'iDiErskCWdsCO8SoNfeaC8TnDWMgROP9Q89ELzFq4EtLnaxW1tfX64jv4WBlHv6MQ7zSrVtQTeyl'
        'ApgIshjbHhRPJVGoUTWdhF6vhzlCgOhdb6QGy7BdEMT52oqRZ1uDiPLquL8lbxmjP7k+1jbzXctQ'
        '9QATVDXO6pF1ho+B96kE6UgfdcOOxgB5LVELYJ/aCJRXZfiI3brtab1ZN9G0NgX2hRExCIg+EngB'
        'sQBh18rd6Vwy5mwSBcKDn4JWqrqFmh37I6Gi4UQDAHSu3qy9oI6q2Z6esN7MOyCqd2QvQsW5AJel'
        '7OgSypY3vfSkacEe69rzhdqvXX7g4YcefXJ1eNnZunGXxhie3LLroxaf6IiiqUqK1x5rBl77GVvU'
        'yii5UmMaFbDcvZbIZAJKump18MB9D6PrhwD09PjO5QffOHF6HkoYfmYBEfXcplRohKMVUOrJm3l2'
        '04DCvN1urK19YdY5Wy0WH/l3//QLH/3F1cUrtNaPKJhoAQXuqjHvfubbDw/3m1J7UrdFeyJBXwKO'
        'FFUcHa1/7/c/75zzhKjIggfcGiJQ3Zzc/dZ3Pfuef/SBVhoIXn/dVMvVLDCmfL5WHz9miRmHgSIA'
        'RlAHlkSnk2tUy1Ul4ekdnUKJFggKEviSq/hFETOrSheLStV11snlB7gg1wJypDG6XFTIDDcTbY5Y'
        'EJCkqcxitV86bYzlLsn+YR8RGNwTISuffFTtTGckC/rSQwctOZcAgs6K8Zp8AUdjUF5t+lMTEayX'
        'X7l549YxXViYPdSDWRhKUlVP1hubtZ8FyW4wxubYVpMbQU0yrdsPvW+/gwkDZ8swFfIenIDiSscY'
        'msq0INWjQqaAmw8qkvadvN4Qd3fX1n3huecbJNVLn6FDkUP/KtN21o7PZYzmR4B0Nba0Wh/5MCdQ'
        'CCd6YXarFWJQyHs2ORfl/j1FKdaf++A5Db0LcBIdn0bZNoKgY23bpBzmC8QUaQkpDFanUYhgLvMZ'
        'lXMwm+08ftLQPLXJolzNSFkYwz3OfYn3sbwsoDdIMZMefBga9YI8XIcWEBL1aCMQeiMxeonOp/Yy'
        'EwjadPHOS5kwnJHQch6tGcWumdk+YEJwJbZ9PIuiU3wMRwS99kUnmjidzknawhu+2gRaDZ9XlPmB'
        '9Uw1Z5LOTowDmWOgM1IWc9mAj6zJasJ0oMj3Yanvcrx1K4ZsAzzdxoalw1+RHY5B0WrZnQSAFBv3'
        'LFAKVOjsZhOdqBUHuhHxAvG45WfiRIy2kROSYs2oKTvMyQnAvOoOZPrg2LK4q8DlIhSJTCgCVJYH'
        'bwIRee8//smrDz/umsSiyIEgjaluvvLVX/mJH+VmDTXMGsrQ6wllBIdER3jEt8YZnNzQUmaRG8qR'
        'ecYMCekZvjat6vvAOm4fg1f3Z0Z+bY/2ufrIE1ce+pbp25mqtffxYX2e9hkI8CEDEFEOwKk4W8NW'
        'kRw9iIZCBrSnc2nPeUX5UsTM8lHhkOj4BS/UGlFTgoUC7eaUdF19PNpJfigd1ajdrAssRz8HSDSZ'
        'hENa8Du6xmSUKXOUrQpOOKM/9ukn2PHpWCheqqqJz6YjE2Z0ptDV7RBjDFQBBYhU+zsq/LrmUFA1'
        '2vI8GVVgknJXc4xTW4TRRpe+/75DB9YW5R84eKjCyOqsyhsMZ7E05dO1emFovTndntw11cK5OvBr'
        'A5jMNMXtNL1VE4JCUQu7O9Jyc3x3sz5u2EfwOmYRJSie32YCTo/v1KfrQfF4zLDAmHGy8O7MuJJF'
        'mzREGLgoICIPvuk73nTr+5cHh3TOu0QXM2+TZKllPqua1f7hoA589ONitf/Yd363rTedJhEilIzZ'
        'KYztb1Xr0+M3vPltZerbTAIKp5X+ZnNDJw1MYQ7mZsA4K7cpLN8JlvyZ7yBeyW761NB56hGTR8BH'
        'EzBxl9lH2mO4Paqvw8xI8H/sRLQSXb4cMkh/E0UdISiertENFTCTY9UXtsZU28bAlWbsgF2PKR6u'
        'juZ1oXCYDsuk+CgVmlxKo70r+ZEO6dj1QXxOtd7NfgjifMfZ7nY+6+RWHTiEuyBSP69f6qyGbAgc'
        'zqHjkSNlzzBOJV6zDjVm5KR4YAaDC3M7l2Rg9MskKgyqUp+FRoMAc3DkIDQv6QdOU9ZKigIcpo7j'
        'PE74nv/xPaJjZxeXNvX8Y5/O/mRyb684+wSNoefhvIVGTi/STOJj1uhPRYeYFNgZeuAeJN47ZGvM'
        'oO2QQ+N8YegQOs2REyhfv30ze+HP8T1n3kEzc6NZBZld529EbnpyrU3pUM92JwNzP8j3xpjg1Nhh'
        'JKOLHaO6WPdsAmRHHzgyZwl0gbPcBcOHS8wKGcrqXhM9X5MlkyyY5us3AZD5XWoTK3T2XThzbngm'
        'TzZ0IZ51UUayC2eaAL5+2wIywIQeHyAUNxOlqEExAFUOyB6OKCRiuFmVIwelzkogdGwYsfu4Y/ec'
        'aDy8mc/5HhSRZGnjcOZJCYMAkz+qundG4k6DeI/ygHsWgM+7EPOq/mjQPXXe5j18Qc4WWT+3D+DZ'
        'qpNTbmDemiw6ObJQ2CenzwOcp/QyeljCjGsNrBSdhmfP6IKngjlgLIM7wzLoUfYKuqk7u32OhXMz'
        '0qzJr+hM7J47DQ1lbjA3rf2E2W1r547b2MeaUKZqYee4jrMOchsKq7lLjeNexU7jTYPTCyJuDJt9'
        '0CNSZvZ8qhYw/gqY64THu7wH0IV7DIK9Hm6fo23+39xIY8wEcSiRxvjpSdM7IDtWeHZBY5454fDp'
        'lyyEtsRskC74LQw9OskzT8AuYWheBh8Ayr+Z22EO3sfZJKpzFaN2hwjPCUUMLk7cS/eAyfMEJ8/y'
        'xRk4HLJrQjv3IKTzTADB8y5Y3LtpnjM6uNe1mdfhLf8facMED/LZ0W4AAAAASUVORK5CYII='
    ),
    'image (6)': (
        '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'
        'cDQW2b52077KsL2EQtD0jjzPn7r71He//e3XX//Lyg+/+w9//8d/9OX79+8nSdIhKkHk3wbl8I2V'
        'zTRmsVr/7N7P//BLX95st9YGqhpj/uqb33z9L/78vfffS5O0guQj9FtlzhoxdtbfHVYcE+a16LZ/'
        'kiSWHqMQEWNceqwRWkOd2x9aIOJwQQMAIMv2qup5YKpaTwV5F6gvtoP+HRd0WlmIOh+ZrAIxDHQk'
        'WSk9qjCFKLHIQ4VCzooQxYGnsraehGp5vknMimeEGA6WAOo4/mzUjuZ4PWe2v8h2MptpXmJodMYR'
        'wUG15cmKNEqMj6DLAkaFzjEbaSGddm5RQxdQc0tRhgMAoK5MBSKABn0pIBVJS/cohhYdYyB3qcVp'
        'Er8XYRrBhhEGgVAo3EPUWHPlpcZPwoG0Qjoq01bzCnjYfBcJKQpzI4YkTZ4by9CnU6o01VqVUOdT'
        'hOT52cX6/FyKd1pbidvr69vNraoGExbzqd1paIwpKgMokmc5SRpDY1ySiEMYSCNzwgNlQYFjEXmg'
        'TpIok4Q0VHCx4Gqh6+VysdjfbhJNEsVun9WknKqzaoy5uLh443v/+nff+c5ysSjwJlXdbDd/9tU/'
        '/fxrr13f3AT7AB6ERVAmli4XaZYBEHJxvr7ZbnW9kmQhy5SaSEHWy02MVWM+0b4h6kvsMEPlHjZk'
        'mizefZj+9F2mKpQMP/3qpz73e3/9t5KmNy89ly4WEOa5ef75569vbhJNGrxPKyFhVuvVm2+++a1v'
        '/U3ldq++8vIXvvjFR48eaZoixvmL31XV/W737LPP/vM//WNucuZG03T1P+9m73zwyRde2L/xHxcm'
        'FxEY2X/0KvvkR5DlY7z+o8PCth2ADtzgLgJ+FNHi8Cj0aX7x4fqH/82zhRgh5OPp4uP6lFyc737n'
        'NVPQQCH73S7LDdCCgkFEYPJ8vV6laZokSZ7nIpKmaZZl6/WZPZRCYkv8SIZcr1af++xnXYkizt75'
        'oZwv5GcPzf9+kApEodu9/PaL2aeelSyfQz+XMTScDm4V0QMfB8SQqZrVgou04IJnYmSXGVk8vrmm'
        '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'
        'QSsy+FLFj/4PeoVJ9pYizAsAAAAASUVORK5CYII='
    ),
    'image (7)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAReklEQVR42u1dTawlx1U+X1X3fS8z'
        'zMzzG4+tJLJDLCYDSJ5ICEwAMRK2AoFIlrBFVhGLQCyyJSvLyGwJyyBs4SwseWmQbMTOAwsjGyey'
        'MlK8eHhFxpHnJ4PDzBvPvL/bVR+L6ntv3759u6u6q/vecXK98PN13+7qqlPnfOec75yCtVZEIKBQ'
        'REQg+R/r+HHjLAyx42g9fx5tTgrznH/U5A8u/NHxSb183OhZ/KLj/bxehEFviqbxT68AoFq8B5as'
        'beWbue+xmu0S4XrWinPpgsVvFu6fLyjyiwmngobbhM1CSQGW/mf4YPwvbvmO7CRfahhZQ5vZdxKD'
        'xVvQT5+gg5KCoPEbz3dDuwVAVFXqOwtzws6B7Q4XlXXtN1EmR3n+jAOIf/2v2IOFHXCFJxsIpW9U'
        '90dO0SuiTgQDpwPht0AYtkGjFDbjn8mYpuDK1wgPj15W74ws2H94iQWClJXqNL5WolT8YeU1XAtX'
        'EEAbLRhqKtZxB/Qs1yvySnqFoesxs35uLYKtDn+xAOEQtmkqGWCT0YMohC1A6y3bfa+j3glqPRj0'
        '+QoTUfAB6Ko/QB0lesGSZWOfgbfY+odNa0mBqjT/UZ7PNTMALQQIKzHCLL4M+tI5aLfB0d4AtBCg'
        'uIiJAxhhBuuWewC1rgUMhUB+jj+9vTxVzb1R5eDh53K+2DYF5rEDGLD71jdfzOH0BsPRTo1SUWX1'
        'Ugsn2KcMdsog9qAiET5IhKX5e4uGomn3lC4wxsxjr4a7hUYcl3201giJd/bBoViLYByA1QWQGN3B'
        'DFqnJPT36JBlZWXaFnj55ZevXrs6SkduOoCSIoQIXRyNFAVQhCQgIpjO4GwVOdnd8yQiANMIP0kA'
        'f/nNb963tWWtbS0BbvylVfScHxZ3gMt/t97XnjqhSpkIRR555JHLly8PL/7vv//+uXPnjDFKqVWR'
        'zJLCarD1HvRcOS7RP9vb2x9++KFSahh9CICkUipJahVAlR8cCwdOU5JJC30XDeRRCIFIZrIsywZb'
        'gByAq+pdW5/nY+y0UNKjI17FqcoxT2FpsSITTEo2Nu4zVeJKqcDhtN8S9FmA6BkSAIsbP9GJrAJ6'
        'nTx5Qmutta7BRU0MAXaUyKHf/Ojw8IeXLmXjsSglFEAA7N6+vRLn+a233nrooYeOxmMFBYgx5vz5'
        '81tbWw4jVUxwx+hopUQOpnZprdL66tWrZ8+e3dvbW4d4ERcs85tvvnnhwoUsy0rb4hOREwZyAJ6j'
        'Z6xVnLnFeKKkSZOBc41w/2DO9JJcuQeeD4mdonLBM8NWC8CuO59uxlc175ULz3xUVYkib9XPcAlQ'
        '91CUf7BUQjm0il7cYLfqSmqLOu69FF8sD7X3aciBlso5ugO+IeWTSbvz57+ioABVC/o/hg3/ri/t'
        'ruZdaom9RZuiKq+fgyhRtcjK0efAZMimoopplSSXyik+wRp/1QOehaNrIvudShJQhY5RsS201vXa'
        'CYC11sdv9wmoQcSSq80GzmXEMEnrtMjJNP6ETRQjklmWxXqxe6viIVmtrnA5gC+cPfv7Fy5kWTaV'
        '3KlAuDQkrU3T9L333nv33Xdr0gbuV+fPn/+txx7LxuP8bg5Z2mJzAyZJ+s477+zs7CjALtt5i8HL'
        'qFTFuXA0hyvDZlHVuNl8/IknXnzxxcbfvvDCC/ULoJQyxnztz7723N8813i3Z599dmdnRylljQmg'
        'nbQI89XUzgNDh6NL2tmN7GB/P8sya4xSegqSXQ6dQqUwHmdJkty5c8fnEXf37mZZlmVZkiQ5unBB'
        'ntzjhDFGa31wsF+389ke5PhX+JJMeq4snG3ayviP+39KqyRJMpFlQWCtmSSJZ+rcJXtJlu8240RI'
        'kiRALTMcBTXYlOOLYIR7gcYUS04ySuRy1FGthEkBKCLW6S16LsAk3Den6xZx0TKkZK11Q52iu1ns'
        '1n/2PayFb0pyUqAcTmBS0FKW6CRNl705Sk2BlMq/1NCiSxHs2ucqAKPRaLEcRea6xVS8kdtniysd'
        '7MDD104k0Qtfpw84ODi4ceNGURh0kly/em3Zy3D+ifsH+1PAmmXZ5ubm4eGhz6P39vZu3by1f7Cv'
        'dQKI2zpbW1uJTti07a5fv37t2rWDg0Ot8qyFMebMmTPHjh3r2Q+ICkSNMUmSfP/7P3jqqT9VShXJ'
        'a7R2f3+/VqYIqL29/a/88R/99PpPnYNGMkmSGzduTEkVy54rIt976XuvvvqqA7XOfdve3r548Y1P'
        'f/oztKzRPCLyjb/4RpqkU1WptR6Px6+88sqTTz4ZN0/JnMmLJNeZnpmvECB8eHhw8+bN0B3r7Jy1'
        '5r93dj766Gctwnm3P759++O5LP/du3czY6aMxLrf7lbQA8ZH4z6Sg06xJ/VIrG0gFI755D5l6uRi'
        'qBYTD7lgNTc3P6WUUgp24kOV7GpdjwHAzbXbAWk68qSvl5bH+Rw1a8ZonnBk6l0e7PNMPS5GQYBJ'
        'rIbLPdUmX4+5M0HSLnZOq5zWRVnpFD/3gK1Jaeq5atL5xA+TRGuttYICbcE8Wx8YNJ1qN4ZE60m3'
        'vDYZLwpbSrvHDCT9BWL9ZQczYDhLz+3eumWMMWJahONK/33r1i0XC8ptAOL19OvDEeuuhVBoEOi1'
        'VJj6nrkZSNLkr7/97d3dXZ1oWpIGenPzzg83brxhRQsAsUJxswmhtYROAEh2ZB/8A/PAE7BHLtth'
        'rT127NiJUycWirFkHVIPSX3qkh26ewZnUDHT2hujjeeff7582dV/kv98Q0apwEykfOauioJAy57I'
        'bz4hX3iuckdCzVTQSpKji1Oa1KcuO9GzGGgtmHu/7sdZZiYhMQgzqxLevI09gVVixnOdBOD+NiIi'
        'Y+H+WIwRM4ZKppZwDsKjjTHzxVGlOZwHvmxEQVGSd6ElizMyFHPuHGUWSoPA8Upl9ClJRRQn5B0l'
        'tHmkJI93QiiihFpDrChdp8kRZsyqt/UCzmGpHNEfBXU3udYaBz2NMYKAzBRYpOQs5kSnFSsW4irL'
        'k8lbKk7FTilAWyohlzdmIqcAyXsFjCsgybKp1tJa5cFUoKNzkMRKLxRjWGmaisjJkyd9ywpnPQRr'
        'J8WMmVkkB7nNsIUiPLcfRJCJtRmWOu1oYahOnDyZJEmprCGWCUm6pxdctOvy5csXL15M05QUa43L'
        'IHoO1ObFkcs7Fru/022e/Dw2jlMysW7mQQokD1wTSkZ72HzQ/aQhbkJf5fP666/95CeXx+PMpfuP'
        'jo4ef/zxRx99tGN5JRs8Yfp2DbXWKqXefvvtZ555JprLViYqaSHx+T/HL399oj3yFZrmusSthBAq'
        'ISnQ6Izr3QK89NJLpe+/+91/cAvQMTwH1KAg+HnYk3+naeoKz4vhhyCCQuNegdKkXl7ymltwT+Xg'
        'bwOKPBettTFmY2MUpZsVuRCKCPWwCzwGcQW3/bFCCvTxBaZRblzpz3T1V+KlNyoW9YVD8zJq6hSK'
        'QCGvMRSrospQN8a8ouZyA/2JeVW+MAzVvalXjK5t7BTemjUpqIPqg5DOg4+FUN0Xu21wq6L9d+So'
        'FxnURHJAIikQugA1adzSuSgd3gQ+70V/UnjfTGxId5lT0Zab91pvm873ieGLQXVabkTqlQV0pUdO'
        '56KG6CmRW40iwpkdjF8nzMFEiVXivEyuB0dBCKqS7PJ0RNIqCE1AYK2LMegnear7AxgJrTAi+7u+'
        'wmdNOnBybdrXd1cPNUqMA7ZhbaFD1PpUZnWBNwgCoPzFCRrlpHxg54KAKyM2Do+iEtnyHLFeTSIk'
        'kPy90Fexxai6gMj6zYo6JevdsAmB04f5pJiE18uBfjR75n2eohzM6nue0uRxOdeYYec21PSkTWId'
        'MZtlxrOMNGRG0COiZ5u6NveCNQztoOfErJDZuu++c+fOudogxya/ffv2Bx984I9hELkMMdq9Hn74'
        '4VOnTrnKGa314eHh9v2nJXKZagcylqPw/+GXv3zp0iXnURljRqONf/+Pi1/9k69WVFotDUUsi23A'
        'kxLio0Snql+p5uVx+aXvfOfvn376qaOjI6dgrbWjjVFFDVo4h6qsgtA2LUMRrfW0jMRliReLhFqB'
        'B3DZGY6huigvOmMoktoYjdI0VVrrgoULCp8sx2LzZarslN6cKUpjDABjbJtWNlhEOwjVLtWFGCUG'
        'lfcMuhykNQbLUFB4xm2yraOWqRbr3AG4XLYCrO9rs57JE1ACB7SOYTmck3dTUiDByRuhKdIXokI4'
        '1zs6UOHAl1VDmo5RX8RO8zbFsBzOmbA6wrRV4BlGhd7RWCA1Nv6y8ZOmo9OnT08tFUUUsLu7e3Bw'
        '4KU66qd42f/qhn02NzfP3H+/tTYnHQFZlh3/peMt3UAPOU6CDpUqr9CSOXK46Etf+u33fvQjKOUQ'
        'vqVNk+Rb3/qr11573bFrFhz1+T3eTsDR/jQNY8zv/c7vvvov/zyeNPpw9Yuntk7NYZ4YpEROtFDS'
        'yXepnaPNzc3PfPazpS+PHz++XJo7BKQjEU+SUbq9ve0Zbe1YyYLKhk1xeVSWnHCV4YqHa6ARuoRn'
        'IrnHDssZY6BQHA06s6BX0T1dRE2DNhQFtRxITAn4vR23XOSl10a/c/zWP6kC8RegRg9M6lnUBJ5i'
        'YuWUUkLXn2YJVyiKepnq9Nz4UYSLI9HxzjJpVFCxd8D8NC17/OHRUTFmNx6PRcSIFoDUee1Lsb6i'
        'om1V6HrMks2cFAMKYOxcEzrXM+1oWhSPgTLOndvX++sMikB2dnauXLmSpqlj2lISc/fKw//7d+c+'
        't22NA38KoECJAn7jH+XUr4o10pI8QEDx8P/4X1+X8S5FQ0SgKIDi/3xw5cdn/jY9+TmaIwoAjMfj'
        'Bx548ItfPF+m6vW5IZYuAGo7WBcOIAlDZVWa96b86yP82S1JZl0WBWIh8pUf4PRj7ReAFKV496r8'
        '269gvD8X+LaiTmzI01dETq+qtWxDtxSWv0e1O8ngRioskKiglL1zE0dKb6SiE6FB3lXZCWUEjQyl'
        'OTohPIKkhENlSiAmg731EU5sycTtmtmkPjjdTU37vE4RZedNN/96FChoLVoJrDATWqfgc1TEmlI7'
        'NBuDqXthjVhLZRwmnjhBSmsNrQUiUKsikSY98ZgCOgcqTbWRn2gFLQIiD8xhGVt30gzFR/Dy2j+l'
        'XJ2TQEQsSEk3RaUia9I3tB+k1USudojQIvtYjBV1VLqU1qITRZd59Yw5EGtEzDQbQIiYPa/WHz0V'
        'd3RfgAUZn8tb0ec18j5BKU/8GjZ2RW/QjmUS/xUBkuOd4EieaEvkxK+LvUPmVa0CLWIoAPQAPvay'
        's2VdRfRanKYqtPNh4kmrxVhtTWmqKABcoQpqXoBhD89GgOpqoxOwthy5ZHpYLFe5DDakKAHhZsnG'
        'DV5HmZY8B1Ojgtb2BPmaw7mHsZwRDtKeXKyW8fTWaPYZrjhCTxxp52ZXkeAxF3useSZmBRpAx8VE'
        'nClmGz0xPQvEI5+HOAf0TO8GNLZawtLyRq7iTPllmoH1LNo4Z7j3FWmXe4ie3tg2uhVS4QBsc8+2'
        '1RKcr1cVTODCaWaIXVrSn6hykBMPm/nFgRulok14TeE116/Op6dDBjvv20gqiPfUCVWzueMQdQOM'
        'QZRRfYjMqmriWJ28CBsPYo+XLRYAYQ5FrPaVA9kB9nBewnwCgvNMBJ8FwD1+VF4wEmIfASyWiWbN'
        'j1GV4+G6HU4aX5Oh+3WM8TzV/xx0rfPyMqzoa1lad6Qq1cIybAEoIZUe6HWzD6nlGC8zQ79zglVc'
        'gRoM8MBn+Ysyu2ZvVOyYFbPglr2BufojlyqIkKVgGYfYfwi/syr2DkE8aMzB8VHp/L6VdKf3P5ic'
        'izaA9bO59kczc+36wjUElJxYK28T1HvbJllx45i+2sItt+Gsg6EYvG3TOu6ePvOarEZB7C3wWRQm'
        'T8HqJn9Ykuzz7F8D/7wGW/TgQueMWIxiwb4pCOvPK1DtZXyY2Qe67EWuqB+l/4DVmmvw8tF2reeL'
        'q8HQjbTz/wdeH6qSTE4S/AAAAABJRU5ErkJggg=='
    ),
    'image (8)': (
        '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'
        'AsLzZwC9ZmFcAAAAAElFTkSuQmCC'
    ),
    'image (9)': (
        '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'
    ),
    'claudecode': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAACbElEQVR4nO3bv2sTUQDA8XchbScp'
        'Qqq0DtXFSTdFBxF0sS0Ogkv9MTm4CY79D3QT+j9Y+gcU3UTEwWK3dupiFusilJqppuQ5pPQq3LUJ'
        'RL7Pu+9nerxLcpf7Jo8kXLIYYxCnAe5bBuD5DoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZ'
        'AGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYA'
        'mAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFgBoAZAGYAmAFg'
        'BoAZAGYAmAFgBoAZAGYAmAFgBoAZANYs2/Dj7ZuQkGz6yctQqwCdzfWQkulQTS5BMAPADAAzAMwA'
        'MAOk+jG0zOT1u/1BjL1fGx9HchCT1+6ELOuP975+CHUydIDzD5/3B/GgO6oA5x48y5pj9QzgEgQz'
        'AMwAMAPADAAzACxrLy8Vbtj//q1wfuLCpcNRjPs77ZEcxMTMxaPvAafv92+zL16F/1mz7AmXGfb2'
        'Az3mThvZbwpcgmAGgBkAZgCYAWAGgDVb848LN/x8vxJS0pp7FKooizEWbtheWgwpufx6NVSRSxDM'
        'ADADwAwAMwDMAKlelnL29v3DUa+3+/ndEPOD2f20lt/31kJoNE6er12AqYWn/UHs/j5+ovP5g27h'
        '/ICOn+jWvcVsbPzk+aqq+OsrfQaAGQBmAJgBYAZI9efoXIydrfwvq2eu3jxlfjCdzS/5fa/cOLou'
        'qGy+xgH0L7kEwQwAMwDMADADwAwAMwDMADADBNYflUJ4sLWKPb4AAAAASUVORK5CYII='
    ),
    'image_add_renew(1)': (
        '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'
        'TkSuQmCC'
    ),
    'image_add_renew(2)': (
        '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'
        'arwoF5+itPT87JWHDq+RwbuVWzNW6UbujBcWj83mrI+cli3I9JFN9glD1mqHfsZfPLRV6vC5Sg+L'
        'vf11qYf35d3i65OHwH9cPIw8lGgTHakxj3riEFRfDxkm3nhkeZhII5ZOWEz7x22sFRTo6Rk2P1et'
        'Wj1kV8qy+x2UNKyYiInDZQfnKmTP55yot5RiTJQcEuZJCzdiRw8EpxK9INyUU91jD1xjHLInt84N'
        '5SCXxpoyCycLjYjrDiuOM1l5luTymIwzAke3cC5WxiUvvx9XinHKZzJK2R2wyCNyGQe4ZDoX7Iux'
        'SNyBhTmu9cnINAce1h7B+b4lnYKsJ6YCk95vfGrSNLsrMzALzi/G3GwyS4wtY/rxqYlMVag4FD6b'
        'n8tYxLXcYqZTyw9Y7JW5aA2isJriU1uGWrScf9WSRHP2h2S2O1LScwOGfj4W8oaMjvWeOb0igtc4'
        'gQXdwPRx8Zl7vI0oaFn5en115JZ81EJEJ3Oa6nInMGC5DIm6kztzwnRk9gfLoOxUFImBAim52v0s'
        'PBhQbqDjmmNHZ45uEllCkSv3EnMsecnx6c99hQyliDMr55nP5hhTcZPQGQelPbwFsCA3MZoL25HF'
        '/9fV/id+yEGDLhZN6sEZXfaAXykotTLNqsB5GduFbX2FxxjkATywrHCLVn7pvdY80wSinn3aIHE2'
        'gScvP/eOUzwHXbSgpo+cYNb6Hko4XdgouvbEp4UNoJzQfGTxgpgTXOLsaapxGs2lzQV5gm7BWhVP'
        'h1+EUhyI+qGY342jWGZatZIOWUzIw8/cGYtKkrMRJFcc9fnZhXnzWkZ/LLHmEnRkwTLTNXsah5C/'
        'ZpHTyQ9gmnMJHFRLkMOpx1w8Qlg0ZnNg3JBP9sddA1s6PpQ/oV//F326IC6HiwkaAAAAAElFTkSu'
        'QmCC'
    ),
    'image_add_renew(3)': (
        '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'
        'QmCC'
    ),
    'image_add_renew(4)': (
        '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'
    ),
    'image_add_renew(5)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAYZUlEQVR42uVdXYts6VVez3p3VXef'
        'zzmZOF8xBPQijDNKEDVEYoSISZAgSAQFY/DCO8E78QfkP0Rv9Do3eqmCIBkMokFvggkaomBIMjNm'
        'cs70Oaeru6r2Xo8X7/eu6u763HX6WAzD6e6qvXe9H2s961nPWi/MTEQgwvj/m/KCgCJbPvJqX3l/'
        'AwMtf7pBoy8iFG7/yNzRu7DWqCN/Sq+4A9Zcj9s/2aG31LYTueIVyLyGdOvVUa7Hqy6y2ddjfFg8'
        's5NKbmNC9OBr57IvE3druDaBIU0lNjAoG710J2PKHb1nyy+zQ38wmDvU7e0PBt/pN9FJYIkhxfUm'
        'CLta2utanvWHA3se6y2hKJdsMu7MB2z9dbHBUHLn62AX6wzbmyCsdlcWyxbbPRAWvioPZzqw3XSu'
        '+0H4SHgwG3qzYr1DOuED7uIhLftle/r5nIBh5mzjq2FHIHitidTnFTIekBRJE4kVHkkHQDXP5Wba'
        '4I5Y9gbd3078/7wJtjJB2/giyOaR4cZ2DCs/Bp+9LahDbnOsR7Fj45vy+YOh2M/08ObYigNMAJYx'
        'wHhOHeahXIuujmexRSj7fK/obcZE+6NzuevjM7sweYOnrek//34wKLchha7FxXiGqIV1X80zTqIB'
        'mM/O5xdnCAKOHpLqPVr6kdDm6OTuPtbTTkYjca7NujfYxg2kAVvrCjafzyZPoCoUQdAChatR6OVB'
        'KG5CobBpxkcnd/dN60JEgA22Ghd3AJZlbVZPlvJarnG1QIFLdwEUUH+D9D8kk1nBAwTFFrCnzdsf'
        'je0Mne4qCYw9knpxz1BEKPT/1XftjQSWzSe5E49zAB+wQ9CxbEWGr8xa3sT4B4QVLl6KyFqSWExB'
        'ITijxd1QDCfQ26ZLTcfArnvQjNjidwV0evGkvZhEkSTLCSEEIl3bWjf3v6U3dWHsg6EJroUEihmB'
        'Ns3YzwvZWzYARIDj2w+guoL5fMZ2wC5Za4jNZ/PpBOpIpjVaDwmi7pF9QS4pACgEwwRlcGTz2SRv'
        'OT91yMtcVeX2CyIgD0mfH3QC4lKFOsCve13uUShSLfcYsIAkgVLtWuyBErmi8hcQQt1lkY+/5sa4'
        'eduMGAYOBiHRZMeR5jKAhWi0KOkHIfwG8Ig07Yzorem9dXTbTH9KG2PpOG/jh9d1IXq1j93DZETY'
        'iDSo2Yvme7Jwy+ihGCJ8GuXizr46olR/ByRlaZzu4GLIcrMcygyhLNAYRDmD6dmjbj4TVTEKxLqW'
        '1gUcmbW4i4aZZIH/pYI0LGx5/HeBi6I/720rNzqKVkMocnznBXUjERvMCmB4GCrC+ezC5lMBfDwL'
        '1d7Or7xltUqFQuR4uPwmebRzdIpia5UzEG18Oz0HIABpIkra4KRLXSGDYW6pzke3UPjRZzI/1wQM'
        'CaEymZte/YC3/OG9LKxOMaPpJ6gGWwYFEGcbg2ULcoEG907rsopZ+9Ab1TOwtOTJj8pioUDyoqzm'
        'KHgGgkUsXQ7HUi/PPZHbvKqmCLqxenItZOaXGCLYRLFg0/CD0eku+2O+WjV3XNgvlMXJiLEbKb2r'
        'ldfx2zE96f6kOgi+itgsEsY6UmGKzCZPfCgrpAja2TkL3MlQ7+gxZfLAcehLZ+x/8GHXdYKKegBZ'
        'cBW8LNIYjY7hXPrN+OSua0ZXwcpdhHBNnp+Sy7oymuCaOH8+m8yn5xpxp/d7yeCiCnETpIkjhsJK'
        'xBgAywcgzGUB9WWRXsqRWMGQ+0/Pp5Pi5jY6uiUy2rf+rFnKFO6MkPJfB6rqMsKsQUs/giXj7MDj'
        '0oQh6YmHIrmAYg4ocV8jzBELR5KjhoJMYtptjMsiguHCzQ9LRWyAw5ZsF7LCHGSfKs5wnz3tRboa'
        'xRAvlYkcWrG2WTwwgx3LP0r6jQgoBh+bQarcTfmFY2xGUsyEJuWT7ydSa/wdi6+9g+A7Q3v/GjXC'
        'RkJKS6TtMnjsBV4UgWgzvsrnM0W9maFbCB76OwsiYq1Zh5IyDaxQRFuNi5Q1RARHR5LdeJVx2GG4'
        'sEkkfM3LrP3hD2U2F1UBaDZ98tDaOVwTtsJLL9rxEcziyDBzO4B17fTh/wgNUPr0C5DWax3YMg1W'
        'Nl/Bn0vv4mQ3uv3i6O5PiJkEs5Y/H/7x7o95fqGjhkZRHZ/cUW2EFIV0nXvlVb13T8gdWiYIml3m'
        'WEhR5Wx28fV/ksmZOCXJjnCqfjRVZd7Kxz8mH3qZZomnCdSBCKDWTh//9z+LtTFi8ibBWyoYLZmQ'
        'EHAhmfC4FxdgDlRtPr392hvj+6/SZiICFnlLf3Oj/Md/ucdP6dSzGrOu86MPVU6nx5/6VXf/XmKQ'
        'drNksQs6mj1UAOBoLNYK1IesIiLq9zZFQUXAPPVqCuucpuNjtjMVTRY8JWNcAYA0+gdoDo+Xi/gA'
        'FdFmFOGvn08AyX5ChBg3bBpp1O8JNI0gc9pomp1rkUjuJx9AY7AwHnBCOgvQk1nGkA2AESoUipmq'
        'CzwymNwi2A+z0npP8ChPdoBLMYeZ42xAYGY+C0Y/dR5+563jzR6zgzITDW/ah1Sn2WNdNSSjmAR4'
        'GGNSElCSUFW4wIrAqc2F1EjkIKTho6UPUVhJWYSMY4xkMrFUalb8+KkbkQYBVGkd/Tbt6xzYq+XF'
        'Mo6Ig2XEsK6WJGZCkCEiizyvFHQ/AdjsvD0/DYZCm3b6JAwDlwiDWWWFUVwR/r4o4Huh0hIBbHp2'
        '8fB7/q/Wzt3RrebkBVoIO/oK2DB1rC53kJQk131DRiVkMDhlLpHh64kYTZuj+cN33//Pr+noOJqU'
        'TERQ6jjdG5l6c5Q8KKrgt2AwTKBu+uj700ffFxE4Z/OL0f3XPvDG54Rz1hmfAj2RBUu4qR4NV8th'
        'dPezy9TLStBrI1TgjhjBCc2gLpFwOS+GpLmKoRuqVbqsiw2SDSqSZPGOgCAgYxEFVFgv8Cq3jLAY'
        'tk1/cKVAjCtO2kr0EwWIVLtI0CuEpIhY8G6sja6Kz4rkeCcwbwyxEby+pCAZeIkgL3ia4D16apRE'
        'FzEExgkKmHdTEdD6eURcEPV9tsnaL04A15i0Ve4KCI1dC2pSb4Y1rer9Qw7vg0nu6BjxqjSjUcFO'
        'sDN2ZoEgCuSS1GF7cgMsw7LRyPlRTkF+13UFnopp/OwrCCPRiRGAJcoWEOsK5cC26jks1YauD3Iu'
        'fQgcHamZOCdmEBHVxD+jMTZNtD/GLLOl3yGdyY9+dNp1nSqMBNztk9HdkyMzY+12ewROMjeM7v1/'
        'H57N5jN1zrpOtRmP9MG9W6wQLavUwvFY5kc+QU8AnQWGDpDZDM1oV6PEpU4YCxzuVVAHl2ryMT46'
        '+exnszVSTB6/103PA7UgAueka31OOHoLD7h5cjT61ne//zt/8meztgPQOMzm9qXf/MSX/+gLp08n'
        '6Qq9gh1UIypCUdW27X7vT7/yvXd+3DgFMG/t07/0+l98+Q+fPJ2oavJChc5I7WNvlBbu9v2X3Gic'
        'ow3XkNfYAKxfbNGsqF7mWgTHuGLT6Jw0noxjJVFAj4emiLSdPZ1cWPbSnM07qBrpwCptS/YpB08E'
        'RQQ2OZ9OZ+00/v5iNs/KFob8fgVAmyaqvyBCGY+rVb+CQ+b6hmg/9QELxWUiQrNSt8AQuAYOByFz'
        'CQCqjtYBokBnkallsaPT0LMQjHqhVjGlzgXCIVxHSg48IrRiBcTHDmJSocnlBPH2kTBXEWbtQpPi'
        '8+qBTWMCXVWatjd3ZFKhh7COQVHFMvRKSoYCPiaKOgrikqBdhKRFDa/fcBXXzxRXl+kc7rcurRlI'
        'f1RhuYhPk/RN1We+/G/ibpAiO45CYiuaCOqYzaoT/b5ECVooAaq0T57XgkyKs4Al/aOwHSWDQSfg'
        'kkVT0fOJBiUBaCb7KQA6Mz9GJp2IdJbXrP9sF9j8GFGbeWSYvatABPO2I5mApxkFKmXmEqip65Qa'
        'KKNf7LVvSbNzA8f+RSh1IpxFkhwAxYymfjAVTvGB+3c6I4TOudm8vXvnVuFucfvWkXPqubxKWKqY'
        'nE87y6v6Ay/cnsxmDhCg7boX7t0uydScLi6D7DJSr2Zkb8KR/RdoYHL6bju7ALBQkmzaHE3e/c7p'
        'd97C6Mgb+el0/ujxJCrItWvt3t3bd05GflecHB999e++8Y1vfrdxypyTCRq/P/7iZ157+cF0Ovcz'
        '/fDxWdt1ZqZQipyMx/fvndAoIuxm4/sfevHNz1rXRtjTr5659eDlZnS074KZZqCOnxHfJbMdDX5y'
        'yaAZhUfj0YdeekChHzURmrEzikJMRk6/9o1v/80/fnPpjf7gt37lw/piIsJfenC3EFHAaJbzoKWp'
        'LwstSxcsN0MVsZYoMcZdntlJCzjhd5Ccta032lLkw0TEaGbdreOxU3WqXb13nYZ8i4/ERDDvLAUH'
        'nk9C0paKLVKEsqCV5P478DcDVP2jp0hjJgAzfsmiWc9SMo9dmZeRmBmmGKsJgJeCknVUjywTQuIZ'
        'faswb8HYlzoie19sNya4HtOz2XLtr/IpWic0ihbkffaCtDnbObShkF1gUr1RSPkoCWEAAJgZKWZ9'
        '49wZfSqt6rmOonaPNGu9TsyspbWJII2ZNQtZnSJ3RNlvD4vmcvSym3tAoG7ExqJYCNa1npvzQis3'
        'vjW+9zJGx150xW5u06c+90Lpa7YEcE6bxo0a13VdCiYp4lSda5a3HoGIiLqRO/mgXxHatc2tByiT'
        'wUJX6ZE4TMFwHwXtu0IBwOTxe/Pzs1AZwFJRQmjTXjx5+K2/ZTvzKDOl10NpI+Tt994/fXqh6Ovy'
        'nHMffuXFo1HTmiWzEy6gYDu79err937qE9ZOo63TuFVClH77wcvajIZp+YiyW8ognV+WCcgL74ey'
        '6CUUPmIx5WLkT77y4kcCv90Xkc/mbWdWQpxgfgCTwvaphikvxA9DVwrHrFdzkJ4+TCqRSm1YFN0h'
        'NTLSbEgUJGazNguro1QdUKNpqXWsypXqbGZxLAeuy9nuDZpzB3HAppk5okqVLeZlSesiOjIWAlxE'
        '+kgRMosaMaorsmRMUlOARjhvbawPdoAdKv0PIEvZ1E0trZzOVKg2I3orkYXROT9eTnrM6wppKFoS'
        'sMS+EIV2FNcchfRZlrRzRe3CISfg2uzYBn6bvaL3Yk9pc/Lg9c+kCp5ImqJ3Vk7Brpa1HUUdDKtl'
        'QlLdmNZKLQOqWg0N3tdpSSC2rzLVaBM8vETJ0NXVHFCnR3e40OMp8z6MMuhE8UWkhMUnLCtgCtkP'
        'iwIcRrnRbrter3auDpsB7pSLT6IEWgS9o8cKpbk31rnog4i5Lhbl7SwTBIUutG7ZQQFCetN7ndyy'
        'JuWDQ4cnMhSrDtu3plnL4m/2TPPpOa1LF+m6eUAmuKzxFnKqBYUGPe+Ayys2WYS/lKq7FiIHJUkX'
        'XzQ6EJlfnEFd2BpkMz7pVZrsI0haj47e6Alw9v477ewiKNGQOZ/FpNEC9V75mjSsXFRy+EFPISxZ'
        'VZItVK2lqjOplrzlpD95+8ErzfgZo6O58ttQbR2FahE3Uaq2QEFQxZ5RymmTop4pKENYNqxJVJsU'
        'mwVVISTLsrDU+6mH4mJbOu+L5JCti7e8O5YLRkv9K1gkxhAkgCiV094sB9/JrArxpGbMuieyNFZ7'
        'hVQv/I8xLR/dBqVw3pftZ8TSqJt8hAkuVaswm2MWjX1QtcIiKCCqTgNVQg05sdJT5ha/9FgJkddD'
        'bBASRKMBQvWMTJwk3czec50z0S7tlrK92ePCP1nYjKLthmS1SaznzUa+CK5iKX00Q8ywn0XxQK8g'
        'Jk9VrOxAodr2TCAW0DIXSi25RZvfKw5q5iCH+BRKnQWdQKqeL30yQnlSoZTKXQ97x/Fm31u242PO'
        '3pRFM30NY6/rzfJW5dztcrzOBGHffaNZ0fx13S2r/qupvRVSXhKFGBeMJb7MXrosAEFpz+qEnJSa'
        'VJZEKeqk8P7L5XXIZIBfiQQpuQI9+VxWJpzVIcXodXlhElihSNyw9Pd+G/hZAurKkKyd65UgoSyX'
        '4OBOeD9nA2BJA+m4zEBmbxwgaTHysT6yZNmIXlkrU6My1O4YPa55ofYpl1VKKdqVIc98aLbX2nE1'
        'K8Q80HWfYeamuCn5jrJ1X5TxxhqxvgA4ZNWi4pw5wcPo1NFrxCIFn1Sd/IWy6d8eafnkmXVXWjtc'
        'qwvidQE28gJMnyhrfyNighCytAlxr3KshDTIsxzkulmNurOT2zZXR2NHp9pf9ZfFp6yba7BA++h1'
        'WU22PwhJEo4p+mIVjSlzu28mXomJi0t1e6FzFbiMBOVVDcB27gO4qsRhpWoyLEu/yJJGlEsmvzcZ'
        'ocpvsbt+xPMoVYUZoDJ3A0XefsHp5mJNFvydVPaRA535qVst80uWAhffxX6zN+n5v0XYl/FLEk73'
        'CDyIkGAP3eeiYcaijSB7iIAJS4mgAFfR6+92+RfETdKGepVDGR2ltDtKwQQoFkRusWlWRfH3XF/R'
        'xwaljwXq5j4RAPmC5NwVKOQIhL6aG0Xd0vXl2bwpE5BKj3zNsG900s2nQos9z1OVGAGFG6XQLLfE'
        'LUpQubzDZB7pKAUliQRRaGZsq9jCjPHpdHwsxtz5Zih57hDnB3jZc7ISUL149PY7//bXvmW9CBGF'
        '0DTT5ujBz/w63KjM/vQ66pJLYsYQ/Jbeg4nDNrjx2Q/+/fydb4s2vprAl4D5VkJ3XvvoS29+zqwV'
        'aLqyQreSSqyW6G8GyERDXU64kFCIdd30qairUwcQmnUz8SnJogKPdTIB9W1Cy4PyiICiCjWud2V7'
        '0Z6f6ug4lod5SQva6YRdB1XFSCqhEGX/hyo3g+gA2EuTQxXaUAROy7aepKg26VJkFsyl0UXO3WS1'
        'BMq2XYzZ0yAwjU5BVeAEyqKlHyDqmgK0Di1Paba371x/wTBppyqmIOJxj9lJ7xABvSx1XGXhUTFB'
        'BZsahNFhvnwBQd4oGpM+MvwJlEyylEuTQ9cq3MmNt6eqWqgQFot1rP70F8ChUV/IS+tYlQKk/gZl'
        'ooHleQy+O6I241ieanAN1EXfXAXPO+y8sT6mZyO7KpBf+/AYyWl0JvYYtPbRd95KgRXb+Z1XPvrg'
        'pz/O2HFyNd2XzienP/r237Odwzdagc4nj0QbFIdoIFkwkUPsAOxGF7SKP+hJWhib9oXlmJAiCeHs'
        '9O1oVdTm57c/+JFmNO63eL1CzEkK0KnOHv5A2NUkkVbNOuAb5eAgJsg7xmZgMXDV1SmrGZANgghc'
        'Ez2ksmtzI8Aeec4rTseCmYlrxBQ5xsgnlfkFQYqqXNcHfL8GqtnZGpeVTjwsi9UpKUpibh8ZQi8K'
        'SRWzbrPlCYi1nbDzYhMvsq77FISCg87sCgXE9qN/tR9tdkV/rjVI6tzxybHHo0mXgF4NB0QEpjZq'
        '3AbUi6qe3DpmN1d1ZcqHrAI3EaFyPGqGP+340m4p+7ZFEPnh2+995c+/CsA5Zz4iRYanGnVRTt35'
        '0yef/Dx++/VfM6ZSgOttK0R+/PDJV/7yr7rZRehZHdk3xs5kfgxUdTp5+vOf+vzv/+xvHAQONQep'
        'DHn46PE/vPUv6lzZEZSJ9oxMg3PNk9PZy298ao2jOOMbzybnb339Xy/OJ3C6oEfPh8I1jTt9NDt+'
        '5c1CED/4idpDhx/BPpw0TVMZnSIL6QGic43N2/FotEmnCuD45Lhx1MA41ZnAONLOuW7WRhOEgxVo'
        'YMtTxzeZBgSO2tvneJanph7EEMajZzYAJwAUSlHmerIIhtGTnQKKnXSnWTJol+DlJGBtpDroYItj'
        'HFb8CEVEunZ+9njSNFKkpKqWdaGa3cnp+3IxmWwATqxrn5w+urhoG+cPjCuY1OIu6uTxqUyenlWN'
        'W3fob6/MWYVAbJH75t7KZjyx88GXXv38735x5LQjeyugEM+Kqj55Ovm5X/jl9Q6wAETk7v0XPveF'
        'L1k3U9UwAb1CQcBIp+7s7OwXP/np4Y9yPuR5wmuLOMi1D6/f5y2uhADrFVwuVMqnnvq7q9xcahmt'
        'Og6EdSPEOktZVw6tfq/r1hbLEo7+sStlWmCf8PSaHTD42Yr7AnyQfZ2SJntVRdys0Yfs4Og/7vqd'
        '2HeBBp6llf4Mfh3uu0TpObBRO1/XsrK4XZdekAPah+fYr3CFkdTB1jFvjs/Yx7rBcCdo3JxVz7Xe'
        'w/XCVaz2Zt3r8uTzNHnrVwAMXaaKgzjDPfMHuEF1wjzE+l1KIezwSGzubbaw+gRgd8/NQVYfyQMJ'
        'HWSrCpkrS7w4/I5+niKMZRX0WMcJA0uHD88pzN95Mcwyk8itK2QqRexz+Nqrh7/kCJOlPm3XEG0w'
        'CTKuO/RINqtR7BXqkuuaAVyKgq4792o3q2Ow3BNXz5Ou43L6Rw9iY6elB7SP2L8R2PWBRLvHS7rG'
        'N9jbwQLDuMFn8/V/vhiyJts7k78AAAAASUVORK5CYII='
    ),
    'image_add_renew(6)': (
        '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'
        'gg=='
    ),
    'image_add_renew(7)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAArRElEQVR42s19Waxt2XXVHGvvc27z'
        'uqqyy51cjsGE2FXBxFUFNsGSY2wiTBAIBHLiJCZCkSx+aISEBRKRgoIUCRMhBdF8AEJgJwEBkeJE'
        'CbEFhgRIg+0AilNysCP3Tbm69253zl5r8LH3WmvOudY+9znhg6eS/e67956zz2pmM+aYYyLFKICI'
        'CLn8ZflDEeT/1f/S/z/9hySA3ku6r7vf6r1jfkERioh5x/Yxr3n95e/La5p3o4isPmHvRfW7lneg'
        'EO5p5n/qPWSo7wfMD8D8tf+o/rfzWpAULk+/vBLyX+77s9QfRX4pksu7QH9XYNer92iHXn/5u9lR'
        '9fDm780/Uv3n37Uc4/Zp5n9C5zWD0D8kzPp2ngXuW7DrPC8be5vVWSdK5/qU10XnejWvTv0qZdPs'
        '9ykHP9XyySHA6l7SHAGwXeTyAdhuonlsddWCvyt1+VCOx/If52MOEZDueEPdJKE6L+asLN+iei80'
        'n29eA66uVX70vE+UsmXldOdfL0cVxUosL7t8TC7vtSzcbFVIodsuQEBS0Gw5afYcy5/yT+XosXu0'
        'kVJyVsxay2x858XC9cZ8NozLs0JvPIrhveYV6utkQ935YesMKMXEdkxw3yNw/Uaia4HmX1EPUx6x'
        '/Ip3YCsfcz57gEjQRxWgtZas76Tu5nL6Kd1zSojMz6SP5XIoquEt20DjPuiPOqwBzxdIHfpyw6Cf'
        'wNpCirRmIT8K3dJnX0h9ro2ZoX9EkH0Dt3rI8kqE8kq0t262Quy4Xy6uHsUeUlY8Na0HrUuwhDbi'
        '17d8ozXpLP9ejBBonLY1QmVXiqFDuy7mWuvvLu8LY2yqNRJtS1n9Yo798qEw26Kslgoqigm6rxDx'
        'ulBsxcEq09OxA9L7pn5lqkvXi4nX7rm0kZ/7un7pXsPZmWtsJWXF0EhjP72JC421MQbBRsZc2x52'
        'Q7fFm61GPvmoapfVXSnkG9pJBaBcrF0SFndsfb7+eFizFe5SrsWo8/KwGNPVj1ksnY/rQnGwrNeD'
        'ym2qgIGoNjX7/vnwlgPMJkCFj1QXH908sHmF8r/lSXppmmh7VR6P1pQbO06p/15NzeHQv/0Z+iTE'
        'WFJvZ/NPoruLQaxBLX5F51WiHEX+FtwJ6p4jFRLTnYoaW2k/yHJzkeNyao+a39dtnIkdSpBul8Vd'
        'oGV3e/feByDoOA8TKGZv5rINLnkFVszzvI41DFWmrOS4cl/x3IHsnE2e1vnhg2+zFiziflGI5cPw'
        'PtLl+33rijqsuK/7f9HQHiVlqCBfT4bfuchwl0lsgqaDOx5IvFzqY+8fer+ofhgqSKWzW4ez8n42'
        'wB7oohPM+9iCVSgCrW3Vn599dMTHf3RwEqgsNbUXXQxLcUKrH6H4Lo1ULC6CPvK1DlwbbrGZsfem'
        '0vGmbDEk6E+klg5dcMQbDBsIaCiCB/cfIgTXTqjeNhVQNWZqflDUDWZxsID1qc2OK5tTExK63AGd'
        '59HhEkVINC8OvQs4gEDaA2EcNf1msnHdJqFbsKDOW7EJPbE4XjTRlc1jtTuvNqKHqan9UM8J5VOx'
        'JBBl6efXA0ADHNGDSuZGwH45O0b23Fs1aWsGVYVDbFJIn5s3sCY6kWp1wtdDM+semEJ4+KXJr3TK'
        'Q3cn3T2zf+lZkrrRWEt4erkQDbhWPZH6AQR03HabzhEERQNKPY9tH7Ctl3Q3gCUK7O8LTVzd3bf8'
        'Bp3EVQ5GRf8//EmpIPolumxDnYyMolMjmcs9dY/lUJav0VCfaq7dCrW+ch/VkBq8z692eXZ3urrU'
        'aQsAdmEs+lvAXnjk3iebMWX67bNyFfLgya07w2Zrak1m7Ztjbgp5+s7PYUUHl3Ugx6gW1EcOaye1'
        'pLf3Z7fqq6U4DePm5/7JD37s53/sxu0H4xRR0bzW+ChXjXYhHZZZ0XrooAfGztksRzS4NwzD5b3n'
        '3/kD//yb3vT2GGMIQ/VRNEXbOWRbthk6/zP+ZK2041ZszG7NIuvsldAaHA3A9XAVhWKO0cXdZ174'
        '8henqwvGvfiorFtFgsBX/aBWlr09Z0kta3IDliuSDW0FaIbh4u7zu8szE3DZvVaLjRyHGRsLeAs/'
        'f9GD1JYTP9Z6zeJVoF3UWhKXQ5QS+3tjpyvUbnuGcTNshnGzZQiHQ2A23l6vhH9G9U4W58gpPVZv'
        'KIGAMI6bMD8SejgqVPk5xx09b2dQq7qedU3Mg48accyYI6HckAfZK7LiXBO8QZ2rOA3akhLJJEyJ'
        'SRZLmQ3+ciyZ1L9IDTWlH1PBPZi6GdVZMr+cSGPXhZQBKWmf4w9TNW9YYk511GEs+Yrr7SKsY++H'
        'UAxRfVg6uM4tBTQEXXaeJFMsRiHFCASAQ0BFqbA8chAgcFknX9Oo9x6ebQADCaEky0u+gByZhALP'
        'BaqDtfw6QARJTCnGFGO5bQhDJ2yD9TUWEi5ZHTqlAn+xxgOe05x6dHNjY7zUFi7vDiCMozI+o4hQ'
        'tucXItuQYqhpgPrtcZRxZJpPNmryVeKiWvIiCJGkOALQAKmpl00Jux3C8nwVXZ1vPkLY77A9uRmG'
        'IQxDh6hgS2p6fXpJSLZY2hoCJiCc3ZAuyud7JD0KkA/G+lAhF4MBChBSmn7mH/6tpz/3f8bt0Vxp'
        'mOJ4tPtvD59+mjiSJQVZHNJc2Nke4TOfHT/x1GYcdAUT9qaxlgUhdhch6twz72qMeNFD8fHX71JC'
        'NkXU5JIhYHc1fXn35uHGK8kJQAhhf3nxtu/7G6983eMpRmSP9XXkrIe5C4TA3gCukwXq3hB59WlD'
        'g5qclSwhpfSJX/jpz37ifx2dblJKIfD8DN/1Trz5j0a5d9ZBlAk55a/84vH//PXNZnRFD1uhFC4Y'
        'kgphQeiy9fLvFIR5A9Jb334pVyshV5B4Gf7eP/iFL34pbjZCQgL25/s/8B3vltc9TiYUWPW3U501'
        'ebG2qGPPT/TZhjlZVw63u8Ez+SS/xfHNOzcfuLk5Oo0pDYNwQJTzdHE+XYQQRJ1EApKSjILdTgpC'
        'BMnElILAUDEf4IpH862GmByB84akhHSO6SrDDTShQwhycSXD0Y3TO5vNyCQSEKbtvWGzbUqJ3gKs'
        'FpB1obCkZSVT1GGoN99+22rEnN/MVVo81DN7UlLAmOKUOAnJJFOCgCEgDBKWpAY1LBeGIACYVA6m'
        'EkoPHTGvLha/rXJqaCIfSQhDkBAEge1aBUgIQibGKc2J0MAYY4xxgToL5JfpTTSFPhw+/ihAow1b'
        'g2Y8LlQUGlzVfZmdCsiVMv/8BwEAQgAkplywnX87LUaY2vea2qOrLNOjDrbETfSQiYxWg52afT1X'
        'tvgUgJQXJy1RcRAAIQjmjxUKjxYVXsUhsmVFrjtIxmjTEVWJz8YmWyWTL9uQVl2wlGJKy7XHkOL+'
        'ap/CICEIkzIWrKcahhKFcrdQEcklvEa1Rpm/mI1iSbaXKGCOetP1RNc5ZChcLuQVDjNrGYSklOK0'
        '3w1DTlrnwNQiEcoK9aqYy4bB1wOBUSGvaLEw6yX7BPHlvVPCMP7Hf/W+X/3pf3F6+0FJKQy4nNLb'
        '3/gbL3vHfop356VLSe7cYroqMYUuHlIUNqRYK3K1w7RnCPNqoV7oitlB1E2Y79bREcteK9BIBUtl'
        'K0FSthv57j97FuOCd6fEcYv/9JN//YP/+O+cbCYKKEiR3/1D73/xI69JMSIoPrQzxzRJcw7zKuZW'
        'vj96uJ2GKK5yf3omeMms1cl+5ou/9aVPfuL0zq2UYqQgyEu/ffeSVyS5SqXUxDhDvi2cUBYQGnOL'
        'ES9/5fSSFyckpIwNlfhSkxGqm4Ps9uHTnxqmSdHf2BRJ7KqFIC950d742i2e/+qnPv9UuHHClCSl'
        'hIA47VQJxkXtCvdCJoaBygfkvBqHMmEPXIBo3DxbXFZENpvj8Wgcj05SjEEwBJmmibsYJ+W01Nvn'
        'ckj2xT3kdb+Xt7xl94a37OQuJaD+Guj558jGe5Sz5zbv+7un6UrC6MuOttcjp7EiJKdYWcwUDiLb'
        'zdH2eAybBMpAzXXS91A8hwzloq0gRddlwmvImI9TS0KQo5TENP+JIohArQKicKFYHrOcT1MODKYN'
        'A0CckM4kXiAEqjBOI5/aJIYw8vLiwLb2mxvQYA4IklJKMTIxzXEwFW2tk3J1OeSHoqNxhSVJU/Dy'
        'r1LRIdTz6NgHGsyrxHEhdEico0xVfVliUCGRKCEHaAFkQAjqQGnMlhqtZgCHIAEQIuNm2SdniN+t'
        'N/WnKjBOJ5i0OH+1wwUXqoT6Xi2xxGQ+EUOHXUwD7PgCqTdObfE2aBoaIMpqLNB3XYg5sg4ikkLg'
        '0ZFstiQlBIjIOEqPabLO54FQZLuReMQwLMfj6KgWdgBhruFASig130Vmo01NMEEFeNdIIfN1hwWm'
        'zCZRFbJtItaUjRfDaFxvhydQMRoWr6iPJ5ZDZyoMNCk1BafLuwwQEt/y5O4bH9uHHPaSuHmTvJLQ'
        'oXCoWK1GT2SU2zfie95zN9XSDTcb4RZhs5i+APBcuAB5NRMs50MlGqKJ6ZbdC0iX4apsjy6+0jEm'
        'ywa05OD5OYEW6DfNK7UQrXg4AGh6C0ogUso9KIs2v0OofNeTEzm5kUzpKaryITUc16TH+dMOgzz4'
        'omRYTwQnSgBW2CMGHs2fC8sBou1grO1Y3hewEpBgWnFgdvZ6JwxX9W0rxgqZpG8WYx+ZQtsN2l4t'
        'Jlmwr7aLwoGimkdVGSj5mEwthG9A/IYUWEuMqnOsokmi6Og6wwU6Ph0NtKxK1PUDjYeDn/vBXVHy'
        'BEN8NFCp61g60FuWYyKYHWezjKVYRBWylP40XQPuvg9KDiw2maWYtoLsB0qGTk/QxEpfh6KdSQVr'
        'tVUnBQjOkXGFocm1ujl9s7IYWqyQkihMkigpSUqOXG4qfSbXY0N8KduoU/QFpmj6t8qh9URPSJeQ'
        'PKfzREpIqT5z5Yt0D6MjdmrWnmq8sLxC0+c7tuSRLuN6jYNe2Vx0jajLFTg+lnAiIVBCfohJ0r7H'
        'ekYvpGl5KIagnveAnrhCT/PudXUruz57pPFYtw+KbJnDiKUdvc2fHYUEPTja0EeswRwPdWZ5ppGo'
        'z2ktOdDw1JeT+e9+6uT46CgRAQLI2Zl829v23/TYFS+AwNxhw+7bw3exwMb+7F1PRzOCYJX0guOa'
        'i4eRF+fjB//tyfmZhKGW+r/whbDZCFOf2HU/Zhrdck1Gh8c1/prGR3XUWRIoT32xvOEc4vA3PzUk'
        'jjP0j8B7d/HY4/G1x4h7GcLCd/DopEOe9alpOhkP5AYK1KV5rdxgi1FZ8o3sKb/xyfGF5xHCguIl'
        '4dFWhkCTAXTEEdjjhThODbos2HEttmmqhVAt0F0edQ3rK1pEHh8pxiCYIrYb5R/gyHAUqBZYuqXT'
        'mBc9mc41QUm/YNrNZpaAkTw6SqcnIYQ5PVANZVgr92KFlWMT9n7koRKxhb5mKBUtTbv1DTAOt/RG'
        'zzYjzQ1lKAw1hJkUJJ7VpmN8qJDT+NW22kfDYwZtfarpDObB6vkCZUmMFJk5Geh6pUqY8UgBO1WY'
        'Ay1MnE1QiefQ9e1Ya2MzXATv/BYzTPFVO40WHUQU2NNVsKQU0xLHnkiBdxirfANzhzxLudlLp46h'
        'kw/YHE2TKftXKeiOkKbfnibFapPltkZYuzNqoVM1lFfIzeWfh3nWdEZxpQcIfXzm6+EP91LD2v46'
        'F4bpzpUv9KjSkNFY8KE8IDQdMt5akWj6RRpRAHQa/lWTL6uFy+IkKDVZNGS3lcWFxSN5bcRhDhBt'
        'CoAVYAsuYS46Fvqmud4wunzTsmK0myxyGc0NaBtkuR5Z9CNAKH2atvkaDbkQaz0LXWtbSwU18MLh'
        'jPpgrxfbfKyJF8tdo+dlE9dqcRijTRy+eiNtecpy4VmpNjaq0DJbcxaQFssS5kAmiSl0LDTC2spO'
        'WZyzys0piYoRWgrukGARMrWFbZ9IrQynWBIS6v4+iAVWVXM3FbxUA1iKJrivwJ+tlWfbwVRC9vKN'
        'USMhJR7LRcvMce3xXhyAXflrNm9HL07Wqjcl5QhHDBt3hPLvXAknmvoLWwafpvMSIQw3OjGyUGQS'
        'XnKFu6MVyWgLyhVqbwlanQq3Ja6jSiYV6kFOxFQDyVKdynmyo0Z3IyIYKKLjYrWX67O+UpThhvzs'
        'f5EPfIjbQWKipkbEhL/4Z/Cm34d4yQCNGdPx20pwFAb52vPyA39f7p3HIdSdCZD9hMd+l7z3XUgT'
        'oZtT6bstXbQLa4gL8oIuRZZZnqrJD+qtAEqHjPkMqk4IC3iu2z6DwqKN5zRvudQ7yz2NlOGIv/qJ'
        '9C8/2O9gf9sTeNMbApNIcMlBR1Juprq/cCb/6N8slVwH0j35mLz3ewYLtnYpYWhfGCo9ANaiAEW8'
        'R3ch6tdjGwP0IEN2qZAtNzSTqLCmAMlOTrEUlk+OZBxkHGSKhjGYKEfbYAMv9veYlaYyBD50W56/'
        'azCLIUhMcucmnH3pySi4uN5n0ejpDpWOCqr8usGLdF1AxnYV0TTIzhopDS1OGTnL2Sxk0rbrzulb'
        'CioCQVmWvt0Aap50GwqLa/Nb/sQoUzQbQEpMkqKu6nOlqF2ZW+KoUq1mEOCaKGov37I8qtuPilNG'
        'BtFlxSYsxGpTvL00+QHTzGjQ55Mec7YAENsmm7Ua2oquXM5KbRByIAI0UnJoaj85TtObTic70JGl'
        'MNpNlduwlDTZLZgIMFJ1VFr6A6xgIGyVn6qwZqUBrFISdLMireSkgT6h7PXaVlBEEtE30wsgxKqR'
        'wmtyX+ZmhxzJZIoWdJOhRuHNTQSqgN4ByK2B5YztG6vlcu2qKoByr93rjPSqfuz0LmrBP1U77Hfa'
        'ukJTzV2H4yCDSKIrTy7H7SrFaUHBeKh1Fj0l25qGlAfMUpWseqa+OE3TtJwr+KgqDlDKgSpOgQhl'
        'hNm6SpnykWgDtmr4cUU/5zAUQ4Mm9jgK5jcTkYRh+NTnwsXlFIYaUy9GI0iK8qqHcfNoCeGAldZ4'
        'gUgHrVaSrSgtm+VC0eBcWqXIKTBSbGezDuBztoXChx5d5d7Iyvh2CIfJiLNcCvbp8tZMC10hXZlE'
        'E52rC0icoiQJkPPz9I6/LJ/8jIyDuAgTQWKUn3qf/IlvE7kQanK6ddqKm0YnG0XOvBJFEqLr/7Ru'
        'r1rtQiSARUXZtOwZmzG2fMjKm4Dute6rVhSiJ1qlyoJHLwuEKjUKp5TIOR0rXa3q3IuIyK0n5eFH'
        'cCHp3sWUPiJyERO8ZFKCCHnrD8tLXy0XV2l3d0ofFpm0Z0lJKBKjCIaFuXqgGa5s0UpW2aMTKKnU'
        '3NfpZLBg8/yxWdRWA1OL+HRTRcO+rzJ8oHQEyzo98UMQ3uX3fDve9M2bzTh3E03cPoIXv4PTGQWv'
        '/T0v53Aip/sg07D5ZZGLOTztlO5PXi2nj03x8uWv5od+4lXTfsJwjLsf5Qu/JMMWjFPEA7fAffI9'
        'JqjiZpleBEstQa7yNR1Oqh0YWOlrVu3TxgmvoQZWboc4rI6HsvBAADAgLMeHVPKDIpBAXXkonzHK'
        'K17KV7xqoYJIpNw4lZe8StK5IMjVlHYXYaAwZrqep4MFzAjgJLxkujrehjf+wZeLiAw35Omn5DmR'
        'kUvkmMAr+kSy9BXaIuMiXkCRNfkGarkXHOqcrFo2UP0BWK2bKWlhOLmGvtqnyHR1tT+Pu829FOPS'
        '4nB8MtMfYGAi+r5DIE2SdvlFo0iKcnopnEQQAAlBAihpv4+kxJjcw0bORiZKGIDAhHhvL0wyjHI2'
        'ybnMWz/f0gDtcgobTNezlwB+d3nJFFEUVTOA72LEvjCwBr9yb51TwBg7ojnUPD12mEw+NCJklhng'
        'G//097/mibeEzVaYBEgxfvif/tDXvvDpzXbLriKbQrcgMgTUCGIQCSiygsye44E7R3de2G42Y4o7'
        'pglLZ9eWIjHx+PikEN7mV2MQBEioG9DQSWudPzftLRdzt7t683f91W949Mk07ebzFzA88NJHSAYY'
        'TQhIV8WSFf5sQQwFxuV4TrTn9Uhop0xcHHzmfD3y6BOPPPqEXt3/+q9/dG7WoCT1vGEGs1i7YCm+'
        'kAIBJClRh8TT4+E/vP/Pxbgftg/Epz+Unv/oZjtweLG87J2MO8rmoTs3eHY1BkW9brlChcZoMB4q'
        'AdyCxO5f96a3v+aJt4hX1UqlJ7Cl6Krctp114a3TaOoqpqydqTBePOiQz0gpcgGTCYQ47VKMQeF0'
        'FWqH67FdF8RQIDhCePjhB4RJwrHIkWxFAmUzyEMnEm7PWE9KrC2A0htlA9QcAHonoJA4AhKGYX95'
        'llKM0z6ULkmEBhaDFojKo2lge8ZcYcQ3asOJMggNU4hOid5gogW5BgAM2aqEgDSwNjGaXEFXPdCZ'
        'xuCbx4t9ThOFJBLSIEkkJgmUSQRJJGmSXu6BhHkXWJ6LysbpKB5c2CkhDAxp0dDSSJKmY8NWwNAM'
        'YGgpA6ZDphQBXHhJm5p5dai2Ij3fISi4Z5n+wVKmSEp0aY0yveRzEdV2KEEJJACVJAZIGERSfVxm'
        '1hEpkgxrhrDtFmbtB6xoRkGrnKhmJra9f74/2DHq8/Cm5adHO9AiV8WahmyN3BpPA7GKAjmJL2M3'
        '8r7OpybNYenctDdrJJUXVy1NQgEHwYnIftlOahpXkuHm4pxmzu9wTE7gnGFpLZ0oOAbGqmVv0qiB'
        'Emf4YaAkQWRHiKuMj0BhYmtKIJ36g+oAyD1QBYAQhfzMLzi2vET44QnoaPpY/NMj01Aq9SoRS1GO'
        'j/mL/3n8+P+4UV42pvCd77p4+OF92lM3GjKe4+I3Je2kROJkroqTssH+2eVd05WcPaU0GHSAmwTH'
        'XH6SCuekIPz4B46/+hWMG5ISIPtJLi9hEJ8OabOOMVJ1r474ttcIbLrtkKOgZu6MaJi1raeg6daD'
        'g5GcaLkm84fAZ74Wnv5KKB8qJrm6oOh0T5KEgN3n+MV/1pJ1y48gzNGMyP4Z+dL70ZEDV/YScBJ/'
        'JD/3WXzpC+NmWyvL4+iFZNmIhnsTDS/RToMgq6Ix0YKTYztICgYRMntbUoQDM0U6leDaukqIbMYF'
        'BS/GZhgUXVeTEDDksIB6xhvmfj5RtPUwaBMzdzoaMF2jVPk9tkdydMzNhsVQ6ykDWGmMsFLYHW6d'
        'lR3Qeae5AdRSBV4HPltM+gFCS8jDTvcPLA7ndB51r7Bp4KGsxaC2mrAc+2U7KKmeMh0+kDZ9TOaj'
        'qSVZfo2SWGXJATeVqSPjV7Qp9d/bm1dQfw/sW/sxthFSIVPAXgP1PtLOShSrpVL2IAxDCJswjEAs'
        'nikLUFEgmLuXKCmpblZYLsiiEQnF41ZQsutfTKzQDt38GSX3tVi3hcE9J0IhhFxSZwhDGEZl+m10'
        'rFyfEwSFgduIKnvnJoEtyrkHxpAY4prPqqmhUE+AKVfx8oVnz567N12dz+J0gGxPbg6bo/kIQxiF'
        'xycMtxg2lHVZf8XH0aeUwjVFYqdfTF+YyJMIcr+4kOnyhefSIrcjIYTdeZqmXaeypCziIVE4pRvY'
        'QzMbxSyrEbZoQmrRAiflwcplhW8anGlJIbz1+/7mvWe/PIyb+eBvjk4++rM/9tlf/+XNycksmhIg'
        'H/65oxs3N0wVAqAN52Rh7FuBsu5cLO0OdQNztXgzcoWY5O5dhCF3v6R4dHL7bd/73uNbt1OMiQSQ'
        'pullr/nmTmcEVF98ncqqW0q0OplBqosVbH1A0Yg0HUiAzqe5LtTYi1BDePyPv8v9zBc++Wuf+vhH'
        'tic3EtOsZ/Urv7ThvBV0GmQ0ApSm/cwVZFRZlrhmAAsIQUxyfMwQmCgBkmI8Ornxrd/5l45OTjt6'
        '6iH0ggw97O+wZlOn4QKGmKV154im8QjtRIB16XyDK8RpKjd/Fu++urxcTmf2A6eniu/GcruK1lsV'
        'PWlqnFQigbVrFjQPbWSBK+CzwMzFksUUL+4+t9luC9Y2n6GSf/HgUIFWGAVY12CqYagzLuKZTN02'
        '8Czx43UssiR2teZhCEZRbhgDnOg0maQwPSvj3o1krSJmtB0vix3wI239KJlFXkmU1hPgCY6z4xUk'
        'NQ+Hhtp6jSBoCXs6LNjunImxM4KgVqS70FsuSJXxWQq7XqjU6wUcEQnDEIYxhJGCWRQppamwP1hb'
        '1VXU37RwoOhKY9anqXPBirCh1yHIkH/FvzEscEYAE0MYVf9aaOZKap4DpD+vQ/P4S1JiGHhOcWns'
        'c7QbLFLD3y04p1W/qRQ5SpehftaLey+cPbMDn05pgiCRYbxDGVIx3nStf01+S1ZpP8giXqy5/Vm4'
        'r9w0an3XLHQY988GifOXaUoImyVvcIYEpiZC8fJhbuI6OpGiYIVfMvY1n6RfAOjSRqqhNoIYnTbm'
        'WRnh0Td/x80HXrw9vkEmiETK+PxPyP6rSQKcEGhtiVFqGZbQubA1CIrikzTFWGtRgkCCcH/nz2Pz'
        'cECcRfqOTm5vjm/QDoEPS7nXwo8G4aSdEXXQG2dEuHxApJTKkIGGrErxw5NwHzrJXeFdTekN/nef'
        'elzufUyGQZh64410KoBrxqK3jRId4etZMYHy5GdEXtku0ay2EMJw9uzTH/jBvxB3F+MwCMLV5dkj'
        'jz7xJ//KjzAligAHO7PvT9p4rCe3vSpEM/Ruff50Jg7pK1AMui5qMU2kIevyHnFvrgD38CUadMRA'
        'HPRUJtNQTs06aXuMN3zhBdyYhKlUgmdOR5EH2l2d/9bHPhJ3l2EIEsLl2XmY2+YL8Q+oAkGd8cgd'
        '/VbV21OK8pCVCd4OzjnQlVjLYnr9O53dWEZFVYNPkSAh6OHShfEOTZ+ou9HyOFg10IC1m+FqLWnR'
        'K06sUjdLJpUfJeDk5u391SgIIQyS0rg9BYCsb8+KkEtL4VQ5msZryieHiCNmFfEPiwKtzMZUTp/o'
        'bX53UKcGG5vpBsqMrcwdQdsIql8IuqzbwWStFNNwKggyhHaUVdaLBplSWpS3UmKaru5+7cuzDGcY'
        'htM7L8qlSqNfwLZgUimg1Iz0EU6HrwietaEsaw+eqjwuf1FhZykHOQCEajaHUYs06g+tEiV051l/'
        '0KvXGmq2wT5FXp97/5vxHJwEIeNjUcY7cvyqHIYixrRAFSlujk8+/9Sv/cj3PklKQLxx+8Hv/9Gf'
        'v/PwK1KK87XWuu1FjbQO56GOS5ZdGen0CHTJzQ3H1XNCfPNTGS1Qa9RN0oE1hRfFi9CQq67i9php'
        'ZCNf2rRzQPrtgXMY9PE/xSi5fXy2t4mbO3LyWqaEEemZnchOBMK0ZNFx2p2/ACDFKYSxNHpAA7Um'
        'UoDiTLOOV8iOc6yjl1QbrEUj1gjjGpCAKQh1au5kZxAKGslOc2no+qSbop6jZbpOVbpURcB2JiKz'
        'Gm9+Yeye4+V/RxJsJNwT8sGiuSlCBIxhOwPX26NjJ/6ztLuh1xwpuu23Qtcj1Vh3FdiuVUjQZMVw'
        '1Z/uAVZNY2gUXbSoupui1X8Mq2LSqCAuQVBplaubBaXvPSe0RHM5MGAzkCIjOY55wsucE4QUp/35'
        'PQEYp2HYmHBO6BbARgRWFaVwQ9FvmlAaLHY8hJsa1fGjdKg1nLQOPJxdzwesqv2Badudvk97F2Rt'
        'oin9XLhGHCnKPEUpCPd7kWNks5LidHz7od/76BsDJMXp6PTO9vjUC9KhM83YTBTCgSlKdNQrOJiu'
        'yWxL3Gbg6yrZji5uyqavia7RVK7TT4Gj5VeZch9xGs+nyQZUI2iXxw3CJMMtOX5ESBmiXHIzPLOL'
        'aU4Rrs4vvuH3P/ruH/5xMRooLAi+v0sm0kOH+b8mW9mMH2zlPm2XFP24JmgR5p6AjzcPer3YnSbd'
        'LD/bZNBy79UgMa0QYG+epkoEiZEPfGt48icl7mQYh698HuMfwXQRhgCEYRyHEDiPQciJgpaYQtVw'
        '63XP9eYjjd16CnqD/7BKCqWeqNU7o+ypFrBHRQToyqs0OTkG00PcU9PEorBnJlWxNFC68m3QEeyc'
        'DY4yHAu2EkKU43vPPTPtLkIYAJzf3d99/jlkKfiunqGDM3txoDnR4/3OYDosmN36Ao/GN0QWC1aX'
        '8MsIQdGyOQGJe+F1jxbyPulG9560B9JE3RGFSfbCeG+mhQpxcvPWH3vP32ac5nffXV2+5FXfSKUC'
        '2p817OmK1OqGTv19vJ9RwF0RHzNFG7JKLKHvo14ZAl37mozAr9U04IPvwPalwn0nv152eJRnf0Z2'
        'X5EQ2lOiGfAQ4a3XC26I5GK0BO4vw+1vyUOFeHLrgbe++6/5J03UQSB0Zkc3dqSGgewaJakNGmUk'
        'o+t67UGb8+RFG+r6ZVXVCDo8B13ylm1JpkZ9VRHuG35Ybr3+8DHhx9+Mqy+LBErUoyZVoSLnjm/4'
        '93L6u9fUf4SgpLnVx9S5FyBI83z8gBSTx+fesi5KNzrKr+s51oE8KVghCKzJUeSAvK9bBTV+wwfQ'
        '/eGzQDoTRkmRCFqXwXqqWBiWlngPO4IFwv08Y7TUv1RksZBqwzD2qFEd0R2FhnYBMDXmXFHkgm5M'
        'YcMQFaXL3CqNkbzGgmFNerr6Ty01uq5IW8rbQTDMPUfAIJinMwyCUP/XiAyIGy1Wv5VopwuiK8nq'
        'ClJwmi80sxOyaie6a4S20EUJM/ZtHMtKjZ9VF7QIcqCv660iHT23QwsomourxSXIXrdAh93RaW/2'
        'v2tK9b1BXbWHgQbApaWEcVVlEV5RRFUnaWduV0FCqknro65Zo6GAUc3urBNcbWtGX8qgzRvaxKT2'
        'EQwi83/QQCczBzXfwqHp6DcVi8xdgcgwB0K9ATjlwodGOUNxg5vxMitOrhusV53fpuHLn6exRXS7'
        'UYrVMbaAcwtZy3r3CxvtDwHTPcQoOSRc5dDEyQ1l8HIps2FPZzJFkbgaWc+7mURpcrs2FAc2oRly'
        '1GEHAV3JZ1PaN1IdS38A5UD/BVzHk+Bg2RN6ILdco+yp2ntu/iGGhyRsoTiwdK2kJJNguNMMlvMs'
        'Ut58o+BUwnZZXzjlRKV8OZz69BxA/4j1R8b3FFda683Oukoz0Ll/8CkrI1M14saVaVk8OCxD/XoI'
        '15X8LS2Ia7vMjuTHta8mLThCY5YOKLC5lml2ejdWP3rdgMIya5og73d6NI3vWd9IdgqEy7ep2xOt'
        'jpdWxgnt3bfV0aWNQNW1aSj9NqYvp8fkgDpcQpvrrHEyDxEj9Fbpv+sbsNLpJ1hRAWfHXa/si2Hr'
        '2cZjR+Rj6cfrjg6lXE/5IHvlkDKdzWo1NZx+Kh7R/d7L+8MQcquvpdimmATtD+E6Q8JWgVEvpbVL'
        'bSHhOoo9m5Y0mnx9VV+LlsVExwZ3dU6jN8C2QLCyAs4y98bmrY/Ztv8SBKuCUm167XI/WRvBt2wh'
        'lG5WD0xFTx9dOV1jZlDzSZ9/kD0REYhXO7OCjYQLu+FnQ7G7Ag2oWoYjHjC5bITEKnLoaghaHhd6'
        'rrYSx8V1HuH+3J6mrNLWtpZhcK2OKTuUXyiUAWt8Odg2Np2M6zSqy7QQK2FAeLIw6l2ax2D6cWSZ'
        'd4x2fkArjd4vw6KI4hzSxPenAJ1JYTioPr4IyZC8JoRCh5bS6OQi57W00mpYE+ZpZdvXBznWPhjq'
        'AXJdalgPtF7k6426XK/aXY8+lBZ/01fQfJjl3ngt/XKZSHUj4NU461qY4WnII1H6xRjR2lCOQgSr'
        '10OWsWwFiaC7AqpRQkF58NeFdmJVHeQFHXmhdzvDAdIJHee3aZlXD83OpBy4Ud+iFD2U1DrhAxK2'
        'kynQGbsNi1HRwi3FItBlhxoWh2kBpJZY9Tw+0HenKe42pEWZuGKcmc8HaUyQ2lLXGayE6HpUdcD7'
        '09Z6kI2KoJpo0Xq1w7Gfe7CVgUJw+pJEGU3IA0xXNMNQ5kkitPfdjdTqDjTjuo0tvxbaGarOvrev'
        'Tuox4s3UC3Q0wJVSkJGfVYJHRnT48HgNJ75FY8dgbweq0EmFcOFnXtaSHTtSXjZaIrXASEtTo+0E'
        'rodSDXmovxuktdTzJQFVOrlo79Gy4aymYwWDVwZClq+ru1GoVyfEKg82M9iMc6qFQAKNt2cjdiI0'
        'o3kJcl2HsnHmq7GGXzsoCVzF0gUh/Zat0qAhbeKxlj23UoAHYNQeTE0YMYC2/YJrAwCbqnd/fl1D'
        'DmvouR2JEjbzSe6v86JJl217XO176ZCKydBGSOx2gQFrZPslPuAhzoSVFkW7+lRlsSJJ0Mxy6/PN'
        'DlRrmCdxkQem6WKFiQNn91ZtPXqmctZBzsoUYBtaiR5nCyX6jpUyVGd9NWan98sEamWIj03lcmM7'
        'C21HC78vu7By+WuXLNsSWnmjOvHe1RKbiSQrGgcs/F9CT30Ae6dfDwAyE04JIycNl4jpsqU6Kbh+'
        'QKisNuXAsWi1ICPETGGxKiTWWSg5CHa7l2mkAC0JrRkL0rRbcu1gwXdpEqYDjS4eJyk9ngfXlrIk'
        'MsFdteLH3eQ2Uo/Da5N4P2jaeKlmABPQnUypFbRrDdSwAvwRQjtMUehnvSk9GkMYINRQMpX65nFh'
        'KvMHncvVKRodBdMGxOw588pYLnD0ioO6H5hV6eH3RnwfLCp0nScPTkK4Ho7vQOJdmYFDn9Y9taoq'
        'WJV+ANcCqAeCitBt5JPrxli4NlZFTQNbQAtfD8uxnWl37Y/1b6QfQ9FEQaVpwISrLq5rm9Mafrg2'
        'e1SaxS07p6OrEzyBhf30Kt80P2zBKrS0zR1uQA2baAEe6exD1vozYXXe+eqYX89Jcj+gGGJti1pT'
        '2eHa7HVYMmpJ1dmZvNZuAAxS0zBi9KRRehAfDYzhPSKhY0FT7uoOJvdnH9JvmXSzWh1ARD9/im2n'
        'pGYe0EG8XhK10BvqQMZmNoVmf7BFrnqJ2CEr73WL76vm/jup2/12f7nLfv5/9mDrIjW/05f+vyay'
        'LqGrXnFQAAAAAElFTkSuQmCC'
    ),
    'image_add_renew(8)': (
        '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'
    ),
    'image_add_renew(10)': (
        'iVBORw0KGgoAAAANSUhEUgAAAIAAAACACAIAAABMXPacAAAXcUlEQVR42u1dWc9lWVl+n3ftfb6x'
        '5u6ugu6iu5kJQ5HYoKERJ0QgJBqMMWLiAF6YwI3xHxhj4o0XBowXXuidOESiBhEuIBoM0IoMaYgE'
        'Ot3aM1Bd3VX1nWHv9T5erLX3Xnuf8w3n+87Z51RRX9KdU2fYZ581vMPzPs+7YGYiAhHKLfYHAXu5'
        '64V+UXekVW7ZP/a1Zhb6RRQRAeJsAJo8fdKZnev52+APx/6x5OJ3gB0043MtEbb/xTXegt0b5ty/'
        'lbqs5XC8gSPr7Vlv0vrRum6EE92YypJMx/FuC7exxZpzAtZu56+BLcISrqbrMbY8+XBg+aO/2CXA'
        '9ZkAHMvycNlbthMOLGeX6LKXLRa03ldgbRaxLJY4AUdctlzQeu/Zl3DlTnh9nFV/UeMqtqYuMUO5'
        '9VJb9B9T6G0WMuJWiyl0nZfSbbb5cMQJaE0geasDZGuHHR06AS0TtrS1jOWMF28bKKKf5cAf+W1x'
        'Cxdkbo9tkR3szfvMWins4i08bPXWb6geoP2ctB8nG2JddgVCTXjZgNShH+w/PV45IhLGJDu0JLKQ'
        'vczDbqXwo8KPKEC9gg9Y/q2X2LbzPNTsU5jrZu421sEwZisP4UmDuq8++amvPP7Jzfy0p0cYXyJG'
        'wRAAEMyIicNzJABWQTNFQBFAqgsIBIKw6p1mNycvvO3+X33Hgx8y83VV/OSjiYX7gD7/Sj8elzdE'
        'aLR6eVT/S+qS6c8EGkNC1sY+dQfNuKCOOtyoeMlbUXudhZhTLMwE9V6XiOMCp8iATMWEIipka3Ta'
        '6WHzi1G53UgegaBZ8WE3xOcpEBJwKtlJXA4XFKrweDuAC3OBNanAwlBTGAyKoM4S2IQx8f2ovERF'
        'liKnHICw/g/1UxBQhCI0MaMnjULSkglGn878mDtgQViRJkvQiYgiEzNxwS60ItJ2qEkG+17bnJkx'
        'aWN+OGvtMNMBoFnbAfQcF3GFPmBYXH9x+KwCFCFFgWt7zwiUJIXklMOtZwRSmZt6e2AmFkggrm+0'
        'ht+Eiuz6+AfP33jM+wLqIDArT29d3M7PrHsecPI/ozl13372C//8zT/azHfMTACjQUTVVeMcDDs7'
        'IWU9BZRZPnkqlCNrs5Z+RGKsG14XQDEubr7n9b/31vvev8C4aG2joGC4DRCIAqSIQiXY9DgkyfJH'
        'OtBtt5xG/agNPxIfkExc5YpFULl3KJyIQQBxCtfDL8e6YEFxxKtRqlZ8m7+KsJKRGBEmA0tC0tgz'
        'jdHY2iLV2NePQ3oBRkJlyCJ4uDM4mZPAOuUB7QGb8cOYjtqM+ItMkuYkJmpfvOGXg0m8BBEITQSM'
        '6RpTCvS+QeoSslRdFROLwnrg0Vkk7aXOVmTZ2PFm98SoCcGFN0Fosqlqmgkk5NVT09p2FbLOcDQW'
        'mQjEwU4NRDN07LrQ9J/VR+q1zwg/CJskIM404vAzBUNTVLQ2d7wFJoCLsoNsfny64pMVnGwHNgYc'
        'M65INIYHEUpq9hDr67eNTXcxcebqOiKVaH4PAWmHXOiVAIJ6BXIWkBlHH0ieRr3uIQJoapRbVibB'
        'g6oZbOtcpjBTnJhjcAx4g9LWB7C3+iAbG9AEaEhiTQgoSE1CyA5QoToCypSvncEqbKcKaI9msm3Y'
        'e/Ey3JmuS32QAraXeXCz3VQ3mbjKkdfxDZoxRj30iBOHWZUwrqRoCdSeCtmKwk/OfMl8GpRSBKqi'
        'qml2FiPHjuGtX629MAWAsc7064Sf6lwC99XbDseII47J6268BbM+MefOQmaSuwoly3VzJ2ftLylQ'
        'FBMbD32NgKYZMOoEiwn+Wb0p4HUbm1m+4RKTTwHGe2VZMMlBeDwp5ELKqNlqaARMrT9JyQf63BM3'
        '/utzT0IR0DgAvuTl15+58q6XlWOrzH5i6NnEOylWFz23Mdtwj375uce+dlVdhOxVURS88lOX7n/D'
        'uWJUsincxKiYvVfrs4VYtKNrBTBrn9Hocn368Rc//Vff7nzk7e+//6F33zseluogRDBEderFBvus'
        'gKJqCM2YD/TRLz3zhb95rHPN8y/fevWVC+NhWMJMlzNWRUs5CQPl6B9sp7dkrOOSEJLqoC6aDhFR'
        'B/Pc3MrC6xBNUgMmEBxTLCkaq4hoc7Dh1EEVNFJEFWbMcoQljy66urod0FstorMDLMw+rXbCSEoB'
        '5plUhCu6BpuCLxpkrZUzxcwNlWOnmMUXzDiVEmBVbGKsIgxFC8tkG6ac7S2Cor+K/1EBOiGIQfuJ'
        'GmLivn6VJGmhQpygE7IKKAK6Yh5znejOZJ3U1gqp942EE0HA4eo5SYsB7Ji8GV8fd1QnlkGfbNRu'
        'JtyLNCMu6YpSwoZTgn3eXUFyAFq4WXwKrbjwCMaEKTO4yYkWKTzhyipiB/5+tFpKVAsYCAURCFQT'
        'J6yoquttDxVzLEkKvkw3E5JcDQpVqMa7Cg8AsPEo0QhyygnPSIbSsGlBRJ6sTwUzkxJVHHNpGreU'
        'JaOrlBhHikgx9kAE8JOZsIo3R6mioBD2QARhFoUKlBNv1lw2PDBPBwcpDybrckH5KZe9Ayhz995B'
        'gxvHLeC9XLi0/dB77lOncSGr+MJedeWC9wwjmuVucztvh20VDpT4kvGo8IWpalnY5ded+bGfv+wy'
        'GCkUVfEFL10+7cvovTtVGCwah0cPE3AMCLCZDEIgUBRjf/k1Zz7yB28TaOoYfGHFyAPiMn3ph5PP'
        '//X3KAQ04gdkwo0TAejlyrsu3n15pyisGPOhd7/iJ973ABsSEUApJjYZeWggymGpkf+hrORsvo11'
        'IHd6ru5TrEH6Ko4xz+GNUuqctmKFQtWMg1yf+d9rf/+Jrx/6FZde+Y6XvfLUZFTCoZxwMvaN3Wuu'
        'iVlFgb4TscCM42qUtFPMq4oFLUSL5BnDetLl6rKQ5M6+aSgCqlEhOwTgoi8PKQ86FQiwE97w2Bin'
        '9FqUP3HmHD0qW6QRpLglm8oxjqjymOrx1i4VNBcKZgsV9z2NfRY4+kfBF7JjOvrj3iWSiD9Z7GTC'
        'rpImsmmYuYB4o5UH3V0oJ1jZguXSDAOCUDlIGb2cMwE+usk+dC4hyKbRqJ5ScM0AjfF/JN0Gklpn'
        '2cQKY1ny3F2bv/Bbr9WQbUHEWFPR643ivdx1eacsrBVcTpl21Mlz8AguV9EjZrALbWDJbN4LnrBW'
        'E8yD0SblHgRRjiF0LnfIQ1RUY5wNkUrVT+zCxe1f/thb0HjoevZQc1wAjPbKsrCKxo4W1aGyTqVN'
        'jKUAJJ3qpNzzLGQtJEpLnv9ggk5t3vXaex7Osy0zUizT7IW9p6/uPalwDcsHTFm5gJiXGy9MJI3e'
        'gQ53okl3Y0mfSMtuCMiov7B7/9nNSxSjiKoW5d6ZrYsrEU/2zY7eT+7y9ac+89lv/clGvhMUE2QC'
        '83QdRFuMOiM6SAoFU6tH4YaTaz/7+o++7RUf7LxjIZj8lNL2kMA9WyE1EU1dXL2fNOzorvoXnegx'
        'RJySiGyq6aozLtT0llmJbuCB0ViquMXioN0Sz2F+OFtV1+FKmCSkqYI1HZpiVZ02lgnRUG4rZidS'
        'L1BBaWwxVlBXjsmE7KsQo3lfsroFJlfuX5i/mglQJCtPnIhsb2wOBpkCYpHoDKdG8fTCkJ2lNcck'
        'EWGjVK0JoVXNOJh9ZqoRc4BAkGfY2thQqLrByqXb/fOCCMH14fUnrz7rVEma+UG+9djzT//ghfHZ'
        'bRcZPhTSTg/y7cE2Sal2xX5NABUxdWOaz5GAFpSrwxtiAg3Ba3H1pn/8+z845Z6YFGNVVdXS+4un'
        '7z6/e5a9k3NP6oTnBU9K87nLPvnFT/3mxz92dves954CBQqR+8+5d795YAiVYex5/5E3vuntF+8Z'
        'laWGQLPBLykJjlAnW0mFACJi5FaWffOHV//861/NnVJoXga5/Pt37btPlznpQSiyLLt2/donfueP'
        'P/wzv1b60qnrrU8BTr4DcNz3q6oKCIWDGDFmvqkuhx+bQFQFXkARs6a6SybiyRD7N7LIitjDVkZt'
        'RnpkIiosBSqqUBOYaBabx8NEQw1hFd16VkNNRJWEhnQJgGQoC3oLFBXxJX3pKSJQigENpbeudjGR'
        'rkpXEF+xGBVQR4N5iopAzKQsaBRxKhYrY1GbKevRrmbxvMSpKzKVdVFGE//QGy/87FvOOZYud0FB'
        'PTLZ3t6iWVLz7UaZbGjVlJbAoirZU3KH+y5eHOROAAp96T94Mf+3b1z98revbg0yqzWZsjYTwCUs'
        '+9J8iFE8fQgxNYtabSiosrMh95wd3Bg5xDBTxTxoUmEVjc4XIqIN6YTsxN8xfo1Gy+dZvn1qW6vP'
        '+1zO7GQXLmyaCcgALKnCzErvy4DnheBM9aBiDbCQhnpZP3lv7rLq+zIR2d3a9Z40oYgJSSkLGxdm'
        'pEKM4sSX3sMp8oGWJdDFpKN0AOwwJElqRRQyMYUK1PtSKsIEyUlp472CFBOQpNGMO5s7mXOZc0cN'
        'TE+smOxDKU9SVa/euPbxz/xF4UsQVFHR7zz1nQundgY5nEEVYswzVacore684TQb37x5fTwci2hV'
        'fWwQzClsvCG8oaJWQCbibo72EJO1Wo8hGwO3teE2t5QkiPPY/sev/cv/PPNYWRbqnJnPkH30vb99'
        '96nztpz6DBcThh4GdARd/GPPPXHl93964gsAqhgOi59868t+8eEHr98cVbUQbGbY3nQx5IGAMhF9'
        'J+3+cjIJ6Brbi72OSWOxy4SNeLXKwZCRT8N90Sno2fh+KUrZm1joeWDC3e3B5x75v3995OntzcxM'
        'vC93tna+9Iefft29r/bmdR/62kLql9lSG7RWKhxcOHO+KCaBMnJz4C+eP3tuCwPNVRFMNinGRLRX'
        'JbiaD5QtRDNcVVNBjUAYy/SawKQGUROFUjxatRdsZLI1cMFRG7mZ4/zp3fOnLmznMBEzv7u5k7ms'
        'B7Vo1g8VojRf+lKgqiiLcjIuSrLwVGONoVVcf1SEXMSYNOoFGimqVLrsaJUq64NuQ4TwTiPZ4X+a'
        'iPdRR0mBd/BmZVmUmSPFeyt82UNWjGU3pkhE020aYUeUG7s5TEuzK4tUy8aAdhyLVHfa6i/U7m/T'
        'lvFREoFNBMCNjUAclNkyqsX7yKxvOi5rdTVn4vWNaCYwPetpYHvXJ8SgRIIXJywhPRCxZGYVh1cS'
        'YU2rxVMHyebagXHzR77Nz614oDPE0JyWQjZGH3BtaSln3Q6q3VGRF7X5kKqDlTVw1FgwtJpKtKrH'
        'UZaANZsA8phivNjJAYEHE+x6NTk1sxPVyq/CM9ru2XMXtnfGMRlGeynMpDs2qUEY3gHcS5MJrz4X'
        'HXuyIqrWXDFDgKI9qZzuZLEMBm3WJwiEWsKY9GrADLF1HFDncucyp0ydR8OfbnW5aesxIBClMBOg'
        'mNS7Ca1tycCinmULFy9b4oqioHqk0CgyptuMJdSF+BANeYsUiNUZR9OsDBWzsS7TV1ioEKHgXnHg'
        '2Wi40ZTMOHNwIexRK6NL1b8w1b6waYeBhsu/jzaGHQFPQl+LMGoi9SBqklvTTAJ1NxVDp3MfZ7Xy'
        'a+R/xEKbA0EOIeculRWcaIaQqIaY9OtAq7tk+7YNodFSM5pVk1xhh+vbLN9GV0/W5PW0rt9QHxsb'
        '1nQvmtILnJgKJQeTc3tRZCPhJ1egTlQUtZY/6sAyjDXUgYaKXNtsiahbjXBIMr+1SokUgWbxK1RT'
        'bkpsoVUHPexM/IE6mVupJsw2btTSZCF1zlEr3IoytZiUY92bGKEdHlHSqSDxDIHh0ui/ISJaFhNJ'
        'jk1G1R2w6QeLNB6ok+tZA39idv4hJmgppRjpBNyhUihmZKR3oimnM4VR3c2XXvjhtbJUV9V7E7tT'
        'Y3EIzfesEWkkLYmc8QZUs8aLV0YsskkZ9EusKBXAQb1rFj367HRL4ZKackjFWKgmxDnkGTIPVQ34'
        'j5n5OEZWm3sRajQdhGqUS0LrgEqrAzGVrkksWAPUok4hajRFwnEUOg0aQBE4euZOVFtkaiTy5WXk'
        'YzVxI+uxK0cTjg/H9vxLxXDkEZF+bA90e0ONabJUmRVhKAg0KVhiPoCOUCkE9+joBWpJZIig9sZ2'
        'c1wiYn8YDHFjaFppv2VO3dIxNB08Se/oOQjyHQ26CsmNTL/x3Re/9fhLYUGrk+HQv/PN59/74/cM'
        'R2WCCaikDWn2uZGqe2jTXRQy1We9cvWAmNnuzuA/Hv3+Zx95bmcrs9JEg0qQG5maNU6ln9Ngs2NY'
        'f86viVSocy4OjhOSo4kPs6KeE28Fp1N/BIYbZca5ZkybEAdYv26VWFm6qldfjU80HsYLJwUz9awo'
        'RqrqnIYtQtDtrwhe7NE62Ums/35bD239m6e9NLpeFkVVa2GeZZv5ZlRcayjLxxRB2ze3IYjxJplE'
        'm0QUUyoA1hVI1plGLF7mIrmE2rpPpIEUE1VxqkYGTGI0GRW+jDds3psPtUIs+fCZbBmHJnb4yDsb'
        '27/00Ps8CAOFDvrUi0//9/e+mWvGWtTOrt11tO9Bnhf4erEHjyEUUdb9+2Y0lGsCIRW5GSxfdwST'
        'CrNoURZXHnjTA5deUZYloGZ+K9/Y3Tp1cOSzkKAxW3bFh+Sls/f85Uf/NH3+H77y6V9/9HfP754r'
        'q/MTQjegKAYLSBz4jOAprQFVJu04GIPZMORmRAO2taAQQmOoh9QOMalFARiWww//3Id+412/MmOL'
        'C5aqVcr6UQNYoKCIePNZlk2KiVN1CvNQIBRfq2CFNfs8jzFJfUZPHWXGCKiKUPfrM4D6EA0k5eOq'
        'K4WGvgbOqQqKsjBa4IYiAZyWXaLK+uOjIxT+oFCCIz8eTsaleZfpaFROJtsCTZt5oJtHJ/V4UaCF'
        'C4WglfTtI7A4rewQiELLSTEcDx2ciRTeDYuR0arGHtqbVKl/bmhcVac2T7325a85vXHKVAAZ79nL'
        'LwzAVjg51S8UddPuSbln9KHgXptyhQ7cVlCWsaP8rNrgh4ny5s9tn3nDvae2dlFOTInrd108d/rc'
        'Ss6dPLFGbD945EDYxEhvPoypmc/c4BtP/dPnv/Nng2zHzCdlBEx7fDN7630fuGv3AW9lnBGa0+za'
        '8Nn/fOLvqnchaQqKmFwFSA/u5viFh1/14Xc8+KGJH0GiwXGquorGZSfeAaqzS5UH/hhVdarRmqsG'
        'mjSTE27btBMk8Awo9up7Hr73zBs617x688lHnvhbROUNMXXIRgrzZW4ASO7y5uCM3unRR8qEpyOt'
        'LjZyvNtm5ZSJquLVFK2aXjRowIWE9ICiHBnNzIdVbUKFGxU3Ij5E3yraoymwJYUBi9GB2PGO95wL'
        'I5qZMB2JG3qSI+MOCJPZPpikqQ3MwB7YLcebBUdKMDhMDf5Ts6a4hsb+pNSk6cbpx25WgxMnTMs9'
        'R6wz+p30HbNQIwRQRrRCh7uvAyJGRK3XrANxgtClDktrswWtJbAChbp1OM6213PEDjWwpY2HxXUz'
        'Y4vfkPIh4gYorbRoZDqMLz8qbqhmKa+i25SDVJcNxy+Wftxbi6SeErHOkp/XM9y98+CVez+Q6yYl'
        'Oc4KzYGptSvw3u9u3iWdQwJEtvLTb7n3fUAWkIaG/Ij0YEMBdFzsXTz1mqPanxmbcQE5cAOXraRV'
        'wckR3ektdfIrrORA53WZgHZl/JAdth+nuD6Z8wCOjKQNSOuzFFY3GeszAT+if3pnCFZ7ML3uHxfe'
        'br98wXYm7aZ8XEYzAJgZVqWRPSzUm6f4vOqfcJL29exzoOfRmh2dHs513aeH3rb2HGz2fHDfim3j'
        'Ya3MZzhhpD2PRPpspr+2XzGX8zh6OsI7Yeg6uJM7YejSLR6WPQG4M2knmFFdwyVzq1iqhagZ75ig'
        'JSy7eYK9OxOw4h1zZwJ66tjP+SaAXGsju6KSRY9hKHCrhIMA7sDRq1y/M9NUALfKxOiRQNfFrV8u'
        'j/rRPR+Ft8UEoH06Y787Yy5WEm/NAobO5YJWu6v7B/0XuI32M4k611Ll0g5VWs+UuwdHovsNJecy'
        'Gke5UbL/MHdZI9i5LHns0OP4cDR+VFGgxZpTXXeDANyuUBLmngDeegDAvHTuuT6CRbi0/wdxlE6x'
        '4zkJ2wAAAABJRU5ErkJggg=='
    ),
    'image_add_renew(9)': (
        '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'
    ),
}

# ---------------- Tray icon (base64 PNG) ----------------
TRAY_ICON_B64 = (
    'iVBORw0KGgoAAAANSUhEUgAAAEAAAAAoCAYAAABOzvzpAAABC2lDQ1BJQ0MgUHJvZmlsZQAAeJyV'
    'kLFOwlAUhr+LJILBOMjAwNCBgUWCDsaBCYaGzRRJKE5tKV2gbW5rfAHZGFjZiItvIK/ghomJg5OP'
    'QEh0NtdqysLAmb785885/zkgXgCydRj7sTT0ptYz+9rhJwKhOmA5UcjuEvD9nnjfzti/8gM3coA1'
    'UJE9sw+iCBS9hKuK7YQbiu/jMAZxrVjeGC0QA6DqbbG9xU4olX8KNMajO7XrLzcF1+92gBxQJsJA'
    'p6nuTyzBI1x9wcEs1ew5LCdQ+ki1ygJOHuB5lWrpT0JLWr9SFsgMh7B5gmMTTl/h6Pb/ETuyqXll'
    'dAICPEa4aLTxcaihcUGdcy5/AKbWPz8bOFjoAAABFUlEQVR42u2ZQRKCMAxFm4wbxtNwDsettxCX'
    '3oOrOB7DwaWnaVwxIgNpKS1j7f9LEqB9TdNMSlW1N5q65iAmY9XtnTQ7m8IFAAAAAAAAACVrF/Oc'
    'FTLmeU5bN4zP9bV1StQIICl8CwilH3DsfyAJAkBmip1nEAH/PDmfhEmv60kQAQAAAAAAAAAAAEWK'
    'cC+ALQAAAAAAAAAAqAP8ZE3XHGXunGWx5nGZt4doXIfU7Y2G6+ayR44A3d0S/0DQcsotwMGtJ83H'
    '9f633TqjdPMc0HdqWcK6ub1tDoSrE+wLd8qPYzYfLa0boDZR7RvkCX7Kb5MI0FZ4OChRAIbCcfkt'
    'BGAdSXDdpQaJz3NebE8eAZ9jML864A1XcVdSTvFixwAAAABJRU5ErkJggg=='
)


# ---------------- Gradient bar color ----------------

def bar_color(pct):
    """Smooth gradient green -> yellow -> red as pct goes 0 -> 100."""
    p = max(0.0, min(100.0, float(pct)))
    if p <= 50:
        t = p / 50.0
        r = int(round(166 + (249 - 166) * t))
        g = int(round(227 + (226 - 227) * t))
        b = int(round(161 + (175 - 161) * t))
    else:
        t = (p - 50) / 50.0
        r = int(round(249 + (243 - 249) * t))
        g = int(round(226 + (139 - 226) * t))
        b = int(round(175 + (168 - 175) * t))
    return f"#{r:02x}{g:02x}{b:02x}"


# Mini-battery palette (fixed, theme-independent)
MINI_SESSION_FILL = "#a6e3a1"   # pastel green (session)
MINI_WEEKLY_FILL  = "#89b4fa"   # pastel blue (weekly)
MINI_FABLE_FILL   = "#cba6f7"   # pastel purple (Fable weekly-scoped)
MINI_LOW_FILL     = "#f38ba8"   # pastel red when remaining <= 20
# transparentcolor keys for the floating strip — theme-aware so ClearType
# fringes on the floating S/W labels blend toward the desktop brightness
# (the near-black key put a dark outline around the letters in light theme).
MINI_TRANS_KEY_DARK  = "#010203"
MINI_TRANS_KEY_LIGHT = "#fdfdfb"   # near-white: fringes go light, invisible


def _rounded_rect(canvas, x, y, w, h, r, **kw):
    """Draw a rounded rectangle on a tkinter Canvas via a smoothed polygon.
    tkinter has no native rounded-rect; a smooth polygon of the corner
    points approximates the iOS battery body cleanly."""
    p = [x + r, y, x + w - r, y, x + w, y, x + w, y + r,
         x + w, y + h - r, x + w, y + h, x + w - r, y + h,
         x + r, y + h, x, y + h, x, y + h - r, x, y + r, x, y]
    return canvas.create_polygon(p, smooth=True, **kw)


# ---------------- Circle slider ----------------

class CircleSlider:
    def __init__(self, parent, width=160, height=22,
                  min_val=30, max_val=100, value=100,
                  track_bg="#e5e7eb", track_active="#1f2937",
                  handle_fill="#ffffff", handle_border="#1f2937",
                  on_change=None, on_release=None):
        self.width = width
        self.height = height
        self.min_val = min_val
        self.max_val = max_val
        self.value = max(min_val, min(max_val, value))
        self.track_bg = track_bg
        self.track_active = track_active
        self.handle_fill = handle_fill
        self.handle_border = handle_border
        self.on_change = on_change
        self.on_release = on_release
        self.handle_r = 7
        self.line_y = height // 2
        self.padding = self.handle_r + 2

        self.canvas = tk.Canvas(parent, width=width, height=height,
                                 highlightthickness=0, bd=0, bg=parent["bg"])
        self.canvas.bind("<Button-1>", lambda e: self._set_from_x(e.x))
        self.canvas.bind("<B1-Motion>", lambda e: self._set_from_x(e.x))
        self.canvas.bind("<ButtonRelease-1>",
                          lambda e: self.on_release(self.value) if self.on_release else None)
        self._draw()

    def _value_to_x(self, v):
        usable = self.width - 2 * self.padding
        ratio = (v - self.min_val) / (self.max_val - self.min_val)
        return self.padding + ratio * usable

    def _x_to_value(self, x):
        usable = self.width - 2 * self.padding
        ratio = max(0.0, min(1.0, (x - self.padding) / usable))
        return self.min_val + ratio * (self.max_val - self.min_val)

    def _draw(self):
        c = self.canvas
        c.delete("all")
        y = self.line_y
        xh = self._value_to_x(self.value)
        c.create_line(self.padding, y, self.width - self.padding, y,
                       fill=self.track_bg, width=3, capstyle="round")
        c.create_line(self.padding, y, xh, y,
                       fill=self.track_active, width=3, capstyle="round")
        r = self.handle_r
        c.create_oval(xh - r, y - r, xh + r, y + r,
                       fill=self.handle_fill, outline=self.handle_border, width=2)

    def _set_from_x(self, x):
        self.set(self._x_to_value(x))

    def set(self, value):
        self.value = max(self.min_val, min(self.max_val, value))
        self._draw()
        if self.on_change:
            self.on_change(self.value)

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)


# ---------------- Pet animation ----------------

class AnimatedPet:
    """Canvas-based pet widget that animates position using offsets.

    Behaves like a Label for the consumer: supports .pack(), .bind(),
    .configure(bg=...). Tick loop updates position every TICK_MS.
    """

    STYLES = ["bounce", "sway", "float", "squish", "breathe"]
    TICK_MS = 80

    def __init__(self, parent, photo, size, bg, style="bounce"):
        self.size = size
        self.photo = photo
        self.style = style
        margin = 5  # extra space so pet can move +/- 4px without clipping
        cs = size + margin * 2
        self.canvas = tk.Canvas(parent, width=cs, height=cs,
                                 highlightthickness=0, bd=0, bg=bg)
        self.cx = cs // 2
        self.cy = cs // 2
        self.image_id = self.canvas.create_image(self.cx, self.cy, image=photo)
        self.frame = 0
        self._after_id = None
        self._tick()

    def _compute_offset(self):
        f = self.frame
        s = self.style
        if s == "bounce":
            # crouch -> hop -> land cycle (~1.6s)
            phase = f % 20
            if phase < 2:  return (0,  1)   # crouch (anticipation)
            if phase < 4:  return (0, -2)
            if phase < 7:  return (0, -4)   # peak
            if phase < 10: return (0, -2)
            if phase < 12: return (0, -1)
            return (0, 0)
        if s == "sway":
            # waddle left-right (~2.8s for full L-R-L)
            phase = f % 36
            if phase < 4:   return (0, 0)
            if phase < 9:   return (2, 0)
            if phase < 14:  return (3, 0)
            if phase < 18:  return (2, 0)
            if phase < 22:  return (0, 0)
            if phase < 27:  return (-2, 0)
            if phase < 32:  return (-3, 0)
            return (-2, 0)
        if s == "float":
            # slow up-down drift like a ghost (~3.5s)
            phase = f % 44
            if phase < 6:   return (0, 0)
            if phase < 12:  return (0, -1)
            if phase < 18:  return (0, -3)
            if phase < 24:  return (0, -4)
            if phase < 30:  return (0, -3)
            if phase < 36:  return (0, -1)
            return (0, 0)
        if s == "squish":
            # squash flat then pop back up (~2s)
            phase = f % 24
            if phase < 2:   return (0, 1)
            if phase < 4:   return (0, 3)   # full squash
            if phase < 6:   return (0, 2)
            if phase < 8:   return (0, 0)
            if phase < 10:  return (0, -2)  # bounce after release
            if phase < 12:  return (0, -1)
            return (0, 0)
        if s == "breathe":
            # gentle visible rise/fall (~3.6s)
            phase = f % 44
            if phase < 11:  return (0, 0)
            if phase < 16:  return (0, -1)
            if phase < 28:  return (0, -2)
            if phase < 33:  return (0, -1)
            return (0, 0)
        return (0, 0)

    def _tick(self):
        self.frame += 1
        ox, oy = self._compute_offset()
        try:
            self.canvas.coords(self.image_id, self.cx + ox, self.cy + oy)
        except Exception:
            return  # canvas destroyed
        self._after_id = self.canvas.after(self.TICK_MS, self._tick)

    def configure(self, **kwargs):
        self.canvas.configure(**kwargs)

    def cget(self, key):
        return self.canvas.cget(key)

    def update_pet(self, new_photo, new_style=None):
        self.photo = new_photo
        if new_style is not None:
            self.style = new_style
        self.canvas.itemconfig(self.image_id, image=new_photo)

    def bind(self, event, handler):
        self.canvas.bind(event, handler)

    def pack(self, **kwargs):
        self.canvas.pack(**kwargs)

    def stop(self):
        if self._after_id is not None:
            try:
                self.canvas.after_cancel(self._after_id)
            except Exception:
                pass
            self._after_id = None


def pet_style_for(pet_key):
    """Deterministic style per pet key. Same pet always gets the same style.
    Uses a stable char-sum hash (not Python's randomized hash())."""
    if not pet_key:
        return "bounce"
    # Manual overrides for special pets
    if pet_key == "claudecode":
        return "bounce"
    h = sum(ord(c) for c in pet_key)
    return AnimatedPet.STYLES[h % len(AnimatedPet.STYLES)]


# ---------------- Pet image ----------------

def ensure_pet_assigned(cfg):
    if cfg.get("pet") and cfg["pet"] in PETS_B64:
        return cfg["pet"]
    if not PETS_B64:
        return None
    chosen = random.choice(list(PETS_B64.keys()))
    cfg["pet"] = chosen
    save_config(cfg)
    return chosen


def load_pet_photo(pet_key, size=PET_SIZE, bg_tolerance=15):
    if pet_key is None or pet_key not in PETS_B64:
        return None
    try:
        raw = base64.b64decode(PETS_B64[pet_key])
        img = Image.open(io.BytesIO(raw)).convert("RGBA")
        data = img.getdata()
        new = []
        for r, g, b, a in data:
            if r >= 248 - bg_tolerance and g >= 248 - bg_tolerance and b >= 248 - bg_tolerance:
                new.append((r, g, b, 0))
            else:
                new.append((r, g, b, 255))
        img.putdata(new)
        # Crop to bounding box of opaque pixels so the character fills the
        # target size instead of leaving transparent margin from source.
        bbox = img.getbbox()
        if bbox:
            img = img.crop(bbox)
        img = img.resize((size, size), Image.LANCZOS)
        return ImageTk.PhotoImage(img)
    except Exception:
        return None


_label_img_cache = {}


def make_label_image(text, color_hex, px_size):
    """Render bold text as a hard-edged RGBA image (no partial alpha), so
    the floating labels never show ClearType fringe against the transparent
    key, regardless of desktop brightness behind the strip."""
    key = (text, color_hex, px_size)
    img = _label_img_cache.get(key)
    if img is not None:
        return img
    try:
        font = ImageFont.truetype("segoeuib.ttf", px_size)   # Segoe UI Bold
    except Exception:
        font = ImageFont.load_default()
    pad = 2
    bbox = font.getbbox(text)
    w = bbox[2] - bbox[0] + pad * 2
    h = bbox[3] - bbox[1] + pad * 2
    im = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(im)
    d.text((pad - bbox[0], pad - bbox[1]), text, font=font, fill=color_hex)
    # Threshold alpha: every pixel fully opaque or fully transparent.
    r, g, b, a = im.split()
    a = a.point(lambda v: 255 if v >= 128 else 0)
    im.putalpha(a)
    photo = ImageTk.PhotoImage(im)
    _label_img_cache[key] = photo   # cache keeps a strong ref (prevents GC)
    return photo


# ---------------- Win32 ----------------

_user32 = ctypes.windll.user32
_kernel32 = ctypes.windll.kernel32
_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_SWP_NOMOVE = 0x0002
_SWP_NOSIZE = 0x0001
_SWP_NOACTIVATE = 0x0010
_TH32CS_SNAPPROCESS = 0x00000002
_MAX_PATH = 260

# Terminal-host executables. Claude Code runs INSIDE a terminal, so when one of
# these is the foreground window we walk its process tree to see if claude is a
# descendant (mirrors the Codex usage widget's design).
_TERMINAL_NAMES = frozenset({
    "windowsterminal.exe", "wt.exe", "cmd.exe", "powershell.exe",
    "pwsh.exe", "conhost.exe", "openconsole.exe",
})

_user32.SetWindowPos.argtypes = [
    ctypes.c_void_p, ctypes.c_void_p,
    ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
    ctypes.c_uint,
]
_user32.SetWindowPos.restype = ctypes.c_bool


def get_foreground_process():
    """Return (name_lower, pid) of the foreground window's owning process, or
    (None, None) on any failure. The PID lets callers recognize our OWN window
    by identity instead of the generic pythonw.exe name (which matched every
    Python app, e.g. the separate Codex usage widget)."""
    try:
        hwnd = _user32.GetForegroundWindow()
        if not hwnd:
            return (None, None)
        pid = wintypes.DWORD()
        _user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value:
            return (None, None)
        handle = _kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not handle:
            return (None, None)
        try:
            buf = ctypes.create_unicode_buffer(512)
            size = wintypes.DWORD(512)
            if _kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
                return (buf.value.rsplit("\\", 1)[-1].lower(), pid.value)
        finally:
            _kernel32.CloseHandle(handle)
    except Exception:
        pass
    return (None, None)


class _PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", wintypes.LONG),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", wintypes.WCHAR * _MAX_PATH),
    ]


def _snapshot_processes():
    """Return [(pid, ppid, name_lower), ...] for every process, or [] on any
    failure. Uses CreateToolhelp32Snapshot so callers can walk the process tree
    (e.g. find a Claude CLI running under a terminal host)."""
    try:
        _kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
        _kernel32.CreateToolhelp32Snapshot.restype = ctypes.c_void_p
        _kernel32.Process32FirstW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.Process32FirstW.restype = wintypes.BOOL
        _kernel32.Process32NextW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_PROCESSENTRY32W)]
        _kernel32.Process32NextW.restype = wintypes.BOOL
        _kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        _kernel32.CloseHandle.restype = wintypes.BOOL
        snap = _kernel32.CreateToolhelp32Snapshot(_TH32CS_SNAPPROCESS, 0)
        if not snap or snap == ctypes.c_void_p(-1).value:
            return []
        procs = []
        try:
            entry = _PROCESSENTRY32W()
            entry.dwSize = ctypes.sizeof(_PROCESSENTRY32W)
            ok = _kernel32.Process32FirstW(snap, ctypes.byref(entry))
            while ok:
                procs.append((
                    int(entry.th32ProcessID),
                    int(entry.th32ParentProcessID),
                    str(entry.szExeFile).rsplit("\\", 1)[-1].lower(),
                ))
                ok = _kernel32.Process32NextW(snap, ctypes.byref(entry))
        finally:
            _kernel32.CloseHandle(snap)
        return procs
    except Exception:
        return []


def claude_runs_under(fg_pid):
    """True when a Claude process is a descendant of the foreground terminal
    host `fg_pid`. Claude Code runs inside a terminal, so the foreground window
    belongs to the terminal (cmd/WindowsTerminal/…), not to claude itself — walk
    the process tree to find it."""
    if not fg_pid:
        return False
    procs = _snapshot_processes()
    if not procs:
        return False
    children = {}
    name_of = {}
    for pid, ppid, nm in procs:
        children.setdefault(ppid, []).append(pid)
        name_of[pid] = nm
    seen = set()
    pending = [fg_pid]
    while pending:
        cur = pending.pop()
        for child in children.get(cur, []):
            if child in seen:
                continue
            seen.add(child)
            if "claude" in name_of.get(child, ""):
                return True
            pending.append(child)
    return False


def set_window_zorder(hwnd, mode):
    target = {"top": -1, "normal": -2, "bottom": 1}[mode]
    try:
        _user32.SetWindowPos(hwnd, target, 0, 0, 0, 0,
                              _SWP_NOMOVE | _SWP_NOSIZE | _SWP_NOACTIVATE)
    except Exception:
        pass


_GWL_EXSTYLE = -20
_WS_EX_TOOLWINDOW = 0x00000080
_WS_EX_APPWINDOW = 0x00040000
_user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
_user32.GetWindowLongW.restype = ctypes.c_long
_user32.SetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_long]
_user32.SetWindowLongW.restype = ctypes.c_long


def claim_foreground(hwnd):
    """Make `hwnd` the foreground window despite Windows' foreground lock.

    A right click on the taskbar strip lands on a WS_EX_NOACTIVATE child of
    Explorer's window, so Explorer (or whatever the user was in) keeps the
    input queue and a plain SetForegroundWindow is REFUSED. The popup menu
    then opens with the mouse captured but no keyboard focus: clicking away
    dismisses it, ESC does not. Attaching this thread to the foreground
    thread's input queue for the duration of the call is the documented way
    around the lock. Never raises: worst case the menu behaves as before.
    """
    try:
        foreground = _user32.GetForegroundWindow()
        owner_tid = (_user32.GetWindowThreadProcessId(ctypes.c_void_p(foreground),
                                                      None) if foreground else 0)
        our_tid = _kernel32.GetCurrentThreadId()
        attached = bool(owner_tid and owner_tid != our_tid
                        and _user32.AttachThreadInput(owner_tid, our_tid, True))
        try:
            _user32.SetForegroundWindow(hwnd)
            _user32.BringWindowToTop(hwnd)
        finally:
            if attached:
                _user32.AttachThreadInput(owner_tid, our_tid, False)
    except Exception:
        pass


def hide_from_taskbar(hwnd):
    """Strip WS_EX_APPWINDOW / add WS_EX_TOOLWINDOW so the widget never gets
    a taskbar button. overrideredirect alone is not reliable: Windows re-adds
    the button after withdraw/deiconify cycles (tray hide/show)."""
    try:
        style = _user32.GetWindowLongW(hwnd, _GWL_EXSTYLE)
        style = (style & ~_WS_EX_APPWINDOW) | _WS_EX_TOOLWINDOW
        _user32.SetWindowLongW(hwnd, _GWL_EXSTYLE, style)
    except Exception:
        pass


# ---------------- Monitor clamping ----------------
# Nothing else in this file stops a saved/restored/dragged window rect from
# landing partly off the monitor (e.g. a maximized-then-restored taskbar, a
# resolution change, or a drag that overshoots the edge). These helpers give
# the widget a cheap way to pull itself fully back onto whichever monitor it
# is nearest to, without ever moving it onto a *different* monitor.

_MONITOR_DEFAULTTONEAREST = 2

_user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.RECT)]
_user32.GetWindowRect.restype = wintypes.BOOL


class _MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", wintypes.DWORD),
        ("rcMonitor", wintypes.RECT),
        ("rcWork", wintypes.RECT),
        ("dwFlags", wintypes.DWORD),
    ]


_user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, ctypes.c_uint]
_user32.MonitorFromWindow.restype = ctypes.c_void_p
_user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(_MONITORINFO)]
_user32.GetMonitorInfoW.restype = wintypes.BOOL


def monitor_rect_for_window(hwnd):
    """Return the FULL monitor rect (left, top, right, bottom) — rcMonitor,
    not rcWork — of the monitor nearest `hwnd`, or None on any failure.

    MONITOR_DEFAULTTONEAREST is what makes multi-monitor setups behave: a
    window sitting on (or dragged onto) a secondary monitor resolves to THAT
    monitor's rect, so clamping keeps it there instead of snapping it back
    onto the primary monitor. The process is System-DPI-aware (see
    SetProcessDpiAwareness near the top of this file), so these are physical
    pixels, consistent with winfo_x/y and GetWindowRect — do not mix in
    winfo_screenwidth-style logical/scaled values here.
    """
    try:
        mon = _user32.MonitorFromWindow(hwnd, _MONITOR_DEFAULTTONEAREST)
        if not mon:
            return None
        info = _MONITORINFO()
        info.cbSize = ctypes.sizeof(_MONITORINFO)
        if not _user32.GetMonitorInfoW(mon, ctypes.byref(info)):
            return None
        r = info.rcMonitor
        return (r.left, r.top, r.right, r.bottom)
    except Exception:
        return None


def clamp_rect_to_monitor(x, y, w, h, mon):
    """Return (x, y) so the w x h rect at (x, y) lies fully inside `mon`
    (left, top, right, bottom).

    Pure and side-effect free so it can be unit-tested without a display.
    If the rect is larger than the monitor on an axis (a huge mini-scale on
    a small monitor, say) there is no position that fits it fully — pin to
    that axis's near edge (left/top) rather than trying to center or shrink
    the window, since this function only ever repositions, never resizes.
    """
    left, top, right, bottom = mon
    max_x = right - w
    if max_x < left:
        x = left
    else:
        x = max(left, min(x, max_x))
    max_y = bottom - h
    if max_y < top:
        y = top
    else:
        y = max(top, min(y, max_y))
    return (x, y)


# ---------------- Taskbar embedded display ----------------
# Self-contained port of the validated Codex widget surface (taskbar_native /
# taskbar_render / taskbar_placement). A WS_EX_LAYERED child window is
# SetParent-ed into Shell_TrayWnd and painted with per-pixel alpha.
#
# EVERY failure path here disables only this surface. Missing comtypes, a
# vertical taskbar, a failed embed, an Explorer restart mid-attach: the
# desktop widget keeps running untouched and no config is rewritten.

# UI Automation (needed to locate the real taskbar buttons) lives in comtypes.
# find_spec only probes — importing comtypes here would fix its apartment
# model before the observer thread can ask for MTA.
_TB_HAS_COMTYPES = importlib.util.find_spec("comtypes") is not None

_TB_CLASS_NAME = "ClaudeUsageTaskbarSurface"
# Sibling usage widgets that already live in the taskbar. They are plain child
# windows, not UIA buttons, so the button sweep below never sees them; without
# this list the two surfaces would be placed on top of each other.
_TB_SIBLING_PREFIXES = ("CodexUsageTaskbar", "ClaudeUsageTaskbarSurface")

_TB_WM_DESTROY = 0x0002
_TB_WM_PAINT = 0x000F
_TB_WM_ERASEBKGND = 0x0014
_TB_WM_LBUTTONUP = 0x0202
_TB_WM_RBUTTONUP = 0x0205
_TB_WM_APP_UPDATE = 0x8001
_TB_WM_APP_VISIBILITY = 0x8002
_TB_WM_APP_STOP = 0x8003
_TB_WM_APP_LAYOUT = 0x8004
_TB_WS_CHILD = 0x40000000
_TB_WS_POPUP = 0x80000000
_TB_WS_CLIPSIBLINGS = 0x04000000
_TB_WS_EX_LAYERED = 0x00080000
_TB_WS_EX_NOACTIVATE = 0x08000000
_TB_WS_EX_TOOLWINDOW = 0x00000080
_TB_GWL_STYLE = -16
_TB_ULW_ALPHA = 0x2
_TB_SW_HIDE = 0
_TB_SW_SHOWNOACTIVATE = 4
_TB_SWP_NOACTIVATE = 0x0010
_TB_SWP_FRAMECHANGED = 0x0020
_TB_SPI_GETHIGHCONTRAST = 0x0042
_TB_HCF_HIGHCONTRASTON = 0x1
_TB_AC_SRC_ALPHA = 0x1
_TB_DIB_RGB_COLORS = 0
_TB_WM_MOUSEMOVE = 0x0200
_TB_WM_MOUSELEAVE = 0x02A3
_TB_TME_LEAVE = 0x2
# Same translucent wash the Codex strip uses for its hover state.
_TB_HOVER_FILL = (127, 127, 127, 80)
_TB_UIA_BUTTON = 50000
_TB_POLL_MS = 1500
_TB_COINIT_MULTITHREADED = 0
_TB_RPC_E_CHANGED_MODE = -2147417850

# Logical-pixel layout, identical to the approved Codex contract:
# mark on the LEFT, usage block (label + bar + right-aligned %) on the right.
# SHARED STRIP CONTRACT — identical numbers in the Codex widget
# (taskbar_render.py / taskbar_placement.py). The two strips sit side by side
# on the taskbar, so any change here must be mirrored there or they stop
# looking like twins. Padding was trimmed on 2026-09-14 so both fit the free
# run left of the Start button at their FULL width.
_TB_MARK_W = 30
_TB_MARK_H = 44
_TB_GAP = 4
# Left sweep start. Until 2026-09-15 this was a flat 200px "Windows reserves
# the leading edge for Widgets" rule, which threw away the whole left end of
# a taskbar that has no Widgets button (measured: nothing between 0 and the
# Start cluster). Now we only keep a hairline margin and rely on measurement:
# anything that actually starts inside _TB_LEADING_BAND is treated as an
# obstacle whatever its UIA control type, so a Widgets surface that is not
# exposed as a button still cannot be covered.
_TB_EDGE_MARGIN = 8
_TB_LEADING_BAND = 200
# Usage block columns: label | bar | right-aligned %. The bar ran 45..104
# (59px) until 2026-09-14, when the user asked for 80% of that length; it is
# now 47px and the severity COLOUR carries the reading.
_TB_BAR_L = 34                                           # label column
_TB_BAR_W = 47                                           # 80% of the old 59
_TB_PCT_W = 42                                           # widest "57.5%"
_TB_PAD_R = 4
_TB_USAGE_W = _TB_BAR_L + _TB_BAR_W + _TB_PCT_W + _TB_PAD_R      # 127
_TB_HEIGHT = 46
_TB_PREF_W = _TB_MARK_W + _TB_GAP + _TB_USAGE_W          # 161
# No squeezed-bar mode any more: a slot narrower than the shared width would
# render a stubby bar next to the Codex strip's full one, so fall back to the
# next free run (and finally to no strip at all) instead.
_TB_MIN_W = _TB_PREF_W                                   # 161
_TB_SS = 3  # supersample factor for text


class _TB_RECT(ctypes.Structure):
    _fields_ = [("left", wintypes.LONG), ("top", wintypes.LONG),
                ("right", wintypes.LONG), ("bottom", wintypes.LONG)]


class _TB_PAINTSTRUCT(ctypes.Structure):
    _fields_ = [("hdc", wintypes.HDC), ("fErase", wintypes.BOOL),
                ("rcPaint", _TB_RECT), ("fRestore", wintypes.BOOL),
                ("fIncUpdate", wintypes.BOOL), ("rgbReserved", ctypes.c_byte * 32)]


_TB_LRESULT = wintypes.LPARAM
_TB_WNDPROC = ctypes.WINFUNCTYPE(_TB_LRESULT, wintypes.HWND, wintypes.UINT,
                                  wintypes.WPARAM, wintypes.LPARAM)
_TB_ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)


class _TB_WNDCLASSW(ctypes.Structure):
    _fields_ = [("style", wintypes.UINT), ("lpfnWndProc", _TB_WNDPROC),
                ("cbClsExtra", ctypes.c_int), ("cbWndExtra", ctypes.c_int),
                ("hInstance", wintypes.HINSTANCE), ("hIcon", wintypes.HICON),
                ("hCursor", wintypes.HANDLE), ("hbrBackground", wintypes.HBRUSH),
                ("lpszMenuName", wintypes.LPCWSTR),
                ("lpszClassName", wintypes.LPCWSTR)]


class _TB_HIGHCONTRASTW(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.UINT), ("dwFlags", wintypes.DWORD),
                ("lpszDefaultScheme", wintypes.LPWSTR)]


class _TB_TRACKMOUSEEVENT(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("hwndTrack", wintypes.HWND), ("dwHoverTime", wintypes.DWORD)]


class _TB_POINT(ctypes.Structure):
    _fields_ = [("x", wintypes.LONG), ("y", wintypes.LONG)]


class _TB_SIZE(ctypes.Structure):
    _fields_ = [("cx", wintypes.LONG), ("cy", wintypes.LONG)]


class _TB_BLENDFUNCTION(ctypes.Structure):
    _fields_ = [("BlendOp", wintypes.BYTE), ("BlendFlags", wintypes.BYTE),
                ("SourceConstantAlpha", wintypes.BYTE),
                ("AlphaFormat", wintypes.BYTE)]


class _TB_BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [("biSize", wintypes.DWORD), ("biWidth", wintypes.LONG),
                ("biHeight", wintypes.LONG), ("biPlanes", wintypes.WORD),
                ("biBitCount", wintypes.WORD), ("biCompression", wintypes.DWORD),
                ("biSizeImage", wintypes.DWORD), ("biXPelsPerMeter", wintypes.LONG),
                ("biYPelsPerMeter", wintypes.LONG), ("biClrUsed", wintypes.DWORD),
                ("biClrImportant", wintypes.DWORD)]


class _TB_BITMAPINFO(ctypes.Structure):
    _fields_ = [("bmiHeader", _TB_BITMAPINFOHEADER),
                ("bmiColors", wintypes.DWORD * 1)]


_TB_U32 = None
_TB_G32 = None


def _tb_u32():
    """A private user32 handle — never touch the shared `_user32` argtypes."""
    global _TB_U32
    if _TB_U32 is None:
        lib = ctypes.WinDLL("user32", use_last_error=True)
        lib.RegisterClassW.argtypes = [ctypes.POINTER(_TB_WNDCLASSW)]
        lib.RegisterClassW.restype = wintypes.ATOM
        lib.UnregisterClassW.argtypes = [wintypes.LPCWSTR, wintypes.HINSTANCE]
        lib.UnregisterClassW.restype = wintypes.BOOL
        lib.CreateWindowExW.argtypes = [
            wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR, wintypes.DWORD,
            ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
            wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, wintypes.LPVOID]
        lib.CreateWindowExW.restype = wintypes.HWND
        lib.DefWindowProcW.argtypes = [wintypes.HWND, wintypes.UINT,
                                        wintypes.WPARAM, wintypes.LPARAM]
        lib.DefWindowProcW.restype = _TB_LRESULT
        lib.PostMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                      wintypes.WPARAM, wintypes.LPARAM]
        lib.PostMessageW.restype = wintypes.BOOL
        lib.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), wintypes.HWND,
                                     wintypes.UINT, wintypes.UINT]
        lib.GetMessageW.restype = wintypes.BOOL
        lib.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
        lib.TranslateMessage.restype = wintypes.BOOL
        lib.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
        lib.DispatchMessageW.restype = _TB_LRESULT
        lib.UpdateLayeredWindow.argtypes = [
            wintypes.HWND, wintypes.HDC, ctypes.POINTER(_TB_POINT),
            ctypes.POINTER(_TB_SIZE), wintypes.HDC, ctypes.POINTER(_TB_POINT),
            wintypes.COLORREF, ctypes.POINTER(_TB_BLENDFUNCTION), wintypes.DWORD]
        lib.UpdateLayeredWindow.restype = wintypes.BOOL
        lib.GetDC.argtypes = [wintypes.HWND]
        lib.GetDC.restype = wintypes.HDC
        lib.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        lib.ReleaseDC.restype = ctypes.c_int
        lib.BeginPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_TB_PAINTSTRUCT)]
        lib.BeginPaint.restype = wintypes.HDC
        lib.EndPaint.argtypes = [wintypes.HWND, ctypes.POINTER(_TB_PAINTSTRUCT)]
        lib.EndPaint.restype = wintypes.BOOL
        lib.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(_TB_RECT)]
        lib.GetClientRect.restype = wintypes.BOOL
        lib.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
        lib.ShowWindow.restype = wintypes.BOOL
        lib.DestroyWindow.argtypes = [wintypes.HWND]
        lib.DestroyWindow.restype = wintypes.BOOL
        lib.IsWindow.argtypes = [wintypes.HWND]
        lib.IsWindow.restype = wintypes.BOOL
        lib.SetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPCWSTR]
        lib.SetWindowTextW.restype = wintypes.BOOL
        lib.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
        lib.GetCursorPos.restype = wintypes.BOOL
        lib.TrackMouseEvent.argtypes = [ctypes.POINTER(_TB_TRACKMOUSEEVENT)]
        lib.TrackMouseEvent.restype = wintypes.BOOL
        lib.GetClassNameW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
        lib.GetClassNameW.restype = ctypes.c_int
        lib.EnumChildWindows.argtypes = [wintypes.HWND, ctypes.c_void_p,
                                          wintypes.LPARAM]
        lib.EnumChildWindows.restype = wintypes.BOOL
        lib.SystemParametersInfoW.argtypes = [wintypes.UINT, wintypes.UINT,
                                               wintypes.LPVOID, wintypes.UINT]
        lib.SystemParametersInfoW.restype = wintypes.BOOL
        lib.GetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int]
        lib.GetWindowLongPtrW.restype = ctypes.c_ssize_t
        lib.SetWindowLongPtrW.argtypes = [wintypes.HWND, ctypes.c_int,
                                           ctypes.c_ssize_t]
        lib.SetWindowLongPtrW.restype = ctypes.c_ssize_t
        lib.GetParent.argtypes = [wintypes.HWND]
        lib.GetParent.restype = wintypes.HWND
        lib.SetParent.argtypes = [wintypes.HWND, wintypes.HWND]
        lib.SetParent.restype = wintypes.HWND
        lib.SetWindowPos.argtypes = [wintypes.HWND, wintypes.HWND, ctypes.c_int,
                                      ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                      wintypes.UINT]
        lib.SetWindowPos.restype = wintypes.BOOL
        lib.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
        lib.FindWindowW.restype = wintypes.HWND
        lib.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(_TB_RECT)]
        lib.GetWindowRect.restype = wintypes.BOOL
        lib.GetDpiForWindow.argtypes = [wintypes.HWND]
        lib.GetDpiForWindow.restype = wintypes.UINT
        lib.SetThreadDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        lib.SetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        _TB_U32 = lib
    return _TB_U32


def _tb_g32():
    global _TB_G32
    if _TB_G32 is None:
        lib = ctypes.WinDLL("gdi32", use_last_error=True)
        lib.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        lib.DeleteObject.restype = wintypes.BOOL
        lib.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        lib.SelectObject.restype = wintypes.HGDIOBJ
        lib.CreateCompatibleDC.argtypes = [wintypes.HDC]
        lib.CreateCompatibleDC.restype = wintypes.HDC
        lib.DeleteDC.argtypes = [wintypes.HDC]
        lib.DeleteDC.restype = wintypes.BOOL
        lib.CreateDIBSection.argtypes = [
            wintypes.HDC, ctypes.POINTER(_TB_BITMAPINFO), wintypes.UINT,
            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE, wintypes.DWORD]
        lib.CreateDIBSection.restype = wintypes.HBITMAP
        _TB_G32 = lib
    return _TB_G32


# ----- geometry (rects are plain (left, top, right, bottom) tuples) -----

def _tb_lp(value, dpi):
    """Scale logical pixels by the target taskbar DPI."""
    return max(1, (value * max(96, dpi) + 48) // 96)


def _tb_place(taskbar, notify, occupied, regions, dpi, siblings=()):
    """Pick unused taskbar space.

    With a sibling usage strip on the bar (the Codex widget lives on the left)
    we sweep the free runs left to right and take the first one that fits, so
    this strip lands beside its sibling instead of across the bar. `regions`
    carries the taskbar buttons AND the sibling rects, so no run overlaps
    anything. Without a sibling we keep the original run before the tray
    icons — grabbing the single left gap first would evict the Codex strip,
    which needs its full width and cannot fall back to the tray run.
    """
    tl, tt, tr, tb = taskbar
    tw, th = tr - tl, tb - tt
    if th > tw:
        return None                      # vertical taskbar: not supported
    height = min(th, _tb_lp(_TB_HEIGHT, dpi))
    if height < _tb_lp(32, dpi):
        return None
    gap = _tb_lp(4, dpi)
    pref = _tb_lp(_TB_PREF_W, dpi)
    floor = _tb_lp(_TB_MIN_W, dpi)
    left = width = None
    if siblings:
        # Start at the very edge: whatever really sits there (a Widgets
        # surface, a left-aligned Start cluster) arrives in `regions`.
        gap_left = tl + _tb_lp(_TB_EDGE_MARGIN, dpi)
        beside = {r[2] + gap for r in siblings}
        ordered = sorted((r for r in regions if r[2] > r[0] and r[3] > r[1]),
                         key=lambda r: r[0])
        for region in (*ordered, notify):
            gap_right = min(tr, region[0] - gap)
            if gap_right - gap_left >= floor:
                width = min(pref, gap_right - gap_left)
                # A run that starts at the sibling strip is the one the user
                # asked for: sit right against it. Any other run keeps the
                # original behaviour of hugging the obstacle on its right.
                left = gap_left if gap_left in beside else gap_right - width
                break
            gap_left = max(gap_left, region[2] + gap)
    if left is None:
        # No free run on the left: squeeze into the one before the tray icons.
        right = min(tr, notify[0] - gap)
        left_edge = tl if occupied is None else max(tl, occupied[2]) + gap
        available = right - left_edge
        if available < floor:
            return None                  # no room: caller silently gives up
        width = min(pref, available)
        left = right - width
    top = tt + max(0, (th - height) // 2)
    return (left, top, left + width, top + height)


def _tb_selftest():
    """Assert the placement preference order. Run by --selftest."""
    bar, notify = (0, 1032, 1920, 1080), (1634, 1032, 1920, 1080)
    buttons = (474, 1032, 1447, 1080)
    sibling = (251, 1033, 448, 1079)     # the Codex strip, on the left
    # Alone: the run before the tray icons, left to the sibling widget.
    alone = _tb_place(bar, notify, buttons, (buttons,), 96)
    assert alone is not None and alone[0] >= buttons[2], alone
    # Beside a sibling that starts at the edge margin: flush against it.
    wide_bar, wide_notify = (0, 1032, 2560, 1080), (2100, 1032, 2560, 1080)
    wide_buttons = (900, 1032, 2000, 1080)
    wide_sibling = (_TB_EDGE_MARGIN, 1033, _TB_EDGE_MARGIN + 161, 1079)
    beside = _tb_place(wide_bar, wide_notify, (0, 1032, 2000, 1080),
                       (wide_sibling, wide_buttons), 96, (wide_sibling,))
    assert beside is not None and beside[0] == wide_sibling[2] + 4, beside
    assert beside[2] <= wide_buttons[0], beside
    # A leading-edge obstacle (e.g. a Widgets surface) pushes the sweep past
    # it instead of the old flat 200px reserve doing it blindly.
    widgets = (0, 1032, 120, 1080)
    after = _tb_place(wide_bar, wide_notify, (0, 1032, 2000, 1080),
                      (widgets, wide_buttons), 96, (widgets,))
    assert after is not None and after[0] >= widgets[2], after
    # Sibling present but nothing fits beside it: back to the tray run.
    packed_sibling = (8, 1033, 169, 1079)
    packed_buttons = (200, 1032, 1447, 1080)
    packed = _tb_place(bar, notify, (8, 1032, 1447, 1080),
                       (packed_sibling, packed_buttons), 96, (packed_sibling,))
    assert packed is not None and packed[0] >= packed_buttons[2], packed
    full = (0, 1032, 1630, 1080)
    assert _tb_place(bar, notify, full, (full,), 96, (sibling,)) is None
    # The bar is short and its colour carries the reading (same thresholds and
    # palette as the Codex strip): 72% green, 38% amber, both inside 12px.
    image = _tb_render((("5h", 72.0), ("주간", 38.0)), "", False,
                       _TB_PREF_W, _TB_HEIGHT, 96, True, False)
    usage_l = _TB_MARK_W + _TB_GAP
    bar_l = usage_l + _TB_BAR_L
    for colour in ((24, 134, 75), (194, 118, 18), (223, 230, 239)):
        # scan the usage block only: the mark carries a green freshness dot
        xs = [x for y in range(image.height) for x in range(usage_l, image.width)
              if image.getpixel((x, y))[:3] == colour
              and image.getpixel((x, y))[3] > 200]
        assert xs and bar_l <= min(xs) and max(xs) <= bar_l + _TB_BAR_W, colour


def _tb_window_rect(hwnd):
    native = _TB_RECT()
    if not hwnd or not _tb_u32().GetWindowRect(hwnd, ctypes.byref(native)):
        return None
    return (native.left, native.top, native.right, native.bottom)


def _tb_child_rect(parent, classes):
    """Rect of the first descendant window matching one of `classes`."""
    u = _tb_u32()
    found = ctypes.c_void_p()

    @_TB_ENUMPROC
    def visit(hwnd, _):
        name = ctypes.create_unicode_buffer(128)
        u.GetClassNameW(hwnd, name, len(name))
        if name.value in classes:
            found.value = hwnd
            return False
        return True

    u.EnumChildWindows(parent, visit, 0)
    return _tb_window_rect(int(found.value or 0)) if found.value else None


def _tb_sibling_rects(taskbar, own_hwnd):
    """Rects of other embedded usage widgets, so we never place on top."""
    u = _tb_u32()
    found = []

    @_TB_ENUMPROC
    def visit(hwnd, _):
        if hwnd != own_hwnd:
            name = ctypes.create_unicode_buffer(128)
            u.GetClassNameW(hwnd, name, len(name))
            if name.value.startswith(_TB_SIBLING_PREFIXES):
                rect = _tb_window_rect(hwnd)
                if rect is not None and rect[2] > rect[0]:
                    found.append(rect)
        return True

    u.EnumChildWindows(taskbar, visit, 0)
    return tuple(found)


def _tb_uia_regions(taskbar, bounds):
    """(class_name, rect) for everything on the bar we must not cover.

    That is every real taskbar button, PLUS anything at all that starts inside
    the leading band — a Widgets surface is not always exposed as a button, so
    near the left edge we trust position over control type. Full-bar
    containers (the frame, the input site) and our own strips are skipped:
    they would swallow the whole sweep.

    Must run on a thread that is NOT the taskbar's own — asking UIA about the
    tree from inside the provider thread deadlocks. Providers live in another
    process and raise COMError (not an OSError) across Explorer restarts, so
    the guard here is deliberately broad."""
    ole32 = ctypes.WinDLL("ole32", use_last_error=True)
    ole32.CoInitializeEx.argtypes = [wintypes.LPVOID, wintypes.DWORD]
    ole32.CoInitializeEx.restype = ctypes.c_long
    ole32.CoUninitialize.argtypes = []
    ole32.CoUninitialize.restype = None
    initialized = int(ole32.CoInitializeEx(None, _TB_COINIT_MULTITHREADED))
    if initialized < 0 and initialized != _TB_RPC_E_CHANGED_MODE:
        return ()
    should_uninitialize = initialized in (0, 1)
    try:
        # comtypes latches an apartment model on first import; force MTA to
        # match this dedicated observer thread before importing it.
        sys.coinit_flags = _TB_COINIT_MULTITHREADED
        import comtypes.client
        comtypes.client.GetModule("UIAutomationCore.dll")
        from comtypes.gen import UIAutomationClient
        automation = comtypes.client.CreateObject(
            "{ff48dba4-60ef-4201-aa87-54103eef594e}",
            interface=UIAutomationClient.IUIAutomation)
        root = automation.ElementFromHandle(taskbar)
        elements = root.FindAll(4, automation.CreateTrueCondition())
        dpi = max(96, int(_tb_u32().GetDpiForWindow(taskbar) or 96))
        band = bounds[0] + _tb_lp(_TB_LEADING_BAND, dpi)
        half = (bounds[2] - bounds[0]) // 2
        found = []
        for index in range(elements.Length):
            element = elements.GetElement(index)
            native = element.CurrentBoundingRectangle
            rect = (round(native.left), round(native.top),
                    round(native.right), round(native.bottom))
            name = str(element.CurrentClassName)
            if (rect[2] <= rect[0] or rect[3] <= rect[1]
                    or not _tb_intersects(rect, bounds)
                    or name.startswith(_TB_SIBLING_PREFIXES)):
                continue
            leading = rect[0] < band and (rect[2] - rect[0]) < half
            if element.CurrentControlType == _TB_UIA_BUTTON or leading:
                found.append((name, rect))
        return tuple(found)
    except Exception:
        return ()
    finally:
        if should_uninitialize:
            ole32.CoUninitialize()


def _tb_intersects(a, b):
    return a[0] < b[2] and a[2] > b[0] and a[1] < b[3] and a[3] > b[1]


def _tb_union(rects):
    if not rects:
        return None
    return (min(r[0] for r in rects), min(r[1] for r in rects),
            max(r[2] for r in rects), max(r[3] for r in rects))


def _tb_find_target(own_hwnd):
    """(taskbar_hwnd, taskbar_rect, placement_rect, dpi) or None."""
    u = _tb_u32()
    taskbar = int(u.FindWindowW("Shell_TrayWnd", None) or 0)
    if not taskbar:
        return None
    bounds = _tb_window_rect(taskbar)
    if bounds is None:
        return None
    notify = _tb_child_rect(taskbar, {"TrayNotifyWnd", "ClockButton"})
    if notify is None:
        return None
    buttons = _tb_uia_regions(taskbar, bounds)
    # No task-list button means UIA gave us nothing usable (Explorer mid
    # restart, comtypes broken); placing blind would land on top of something.
    if not any(name == "Taskbar.TaskListButtonAutomationPeer"
               for name, _ in buttons):
        return None
    siblings = _tb_sibling_rects(taskbar, own_hwnd)
    regions = tuple(rect for _, rect in buttons) + siblings
    before = tuple(r for r in regions if r[0] < notify[0])
    dpi = max(96, int(u.GetDpiForWindow(taskbar) or 96))
    placement = _tb_place(bounds, notify, _tb_union(before), regions, dpi,
                          siblings)
    if placement is None:
        return None
    return (taskbar, bounds, placement, dpi)


# ----- rendering -----

_TB_FONTS = {}
_TB_MARKS = {}


def _tb_font(size_px, semibold, hangul):
    key = (size_px, semibold, hangul)
    font = _TB_FONTS.get(key)
    if font is not None:
        return font
    windows = Path("C:/Windows/Fonts")
    if hangul:
        names = ("malgunbd.ttf", "malgun.ttf") if semibold else ("malgun.ttf",)
    else:
        # seguisb is a real 600-weight face; the variable font's weight
        # selection is unreliable across Pillow builds.
        names = (("seguisb.ttf", "segoeui.ttf") if semibold
                 else ("SegUIVar.ttf", "segoeui.ttf"))
    for name in names:
        try:
            font = ImageFont.truetype(str(windows / name), max(1, size_px))
        except OSError:
            continue
        break
    else:
        font = ImageFont.load_default(size=max(1, size_px))
    _TB_FONTS[key] = font
    return font


def _tb_mark(target_height):
    """The embedded tray mark, scaled by a whole multiple.

    The asset is a 16x10 pixel-art grid stored at 4x. NEAREST at an integer
    multiple keeps the pixel edges crisp; a fractional LANCZOS resize turns
    the art to mush."""
    mult = max(1, int(round(target_height / 10.0)))
    image = _TB_MARKS.get(mult)
    if image is None:
        source = Image.open(io.BytesIO(base64.b64decode(TRAY_ICON_B64)))
        image = source.convert("RGBA").resize((16 * mult, 10 * mult),
                                               Image.Resampling.NEAREST)
        _TB_MARKS[mult] = image
    return image


def _tb_palette(light, high_contrast):
    if high_contrast:
        c = "#000000" if light else "#ffffff"
        return {"text": c, "muted": c, "track": c, "green": c, "amber": c, "red": c}
    return {
        "text":  "#172033" if light else "#f3f6fb",
        "muted": "#5e6b7d" if light else "#aeb8c7",
        "track": "#dfe6ef" if light else "#3a4558",
        "green": "#18864b" if light else "#47c77d",
        "amber": "#c27612" if light else "#e6a842",
        "red":   "#c53532" if light else "#ff716c",
    }


def _tb_severity(remaining, palette):
    if remaining <= 20:
        return palette["red"]
    if remaining <= 50:
        return palette["amber"]
    return palette["green"]


def _tb_percent_text(value):
    return f"{value:.1f}".rstrip("0").rstrip(".") + "%"


def _tb_capsule(image, box, color):
    """Composite one exact-color capsule through a supersampled alpha mask.

    Drawing the bars on the supersampled canvas and downsampling would blend
    the fill colour with the transparent background at the ends; masking the
    alpha only keeps the colour exact."""
    mask = Image.new("L", (image.width * _TB_SS, image.height * _TB_SS))
    scaled = tuple(int(round(v * _TB_SS)) for v in box)
    radius = max(1, int(round(min(box[2] - box[0], box[3] - box[1])
                               * _TB_SS / 2)))
    ImageDraw.Draw(mask).rounded_rectangle(scaled, radius=radius, fill=255)
    mask = mask.resize(image.size, Image.Resampling.LANCZOS)
    solid = Image.new("RGBA", image.size, color)
    solid.putalpha(mask)
    image.alpha_composite(solid)


def _tb_layout(width, height, dpi):
    """(mark_box, usage_left, content_top, content_height) in physical px."""
    scale = max(96, dpi) / 96.0
    mark_w = min(width, int(round(_TB_MARK_W * scale)))
    gap = min(max(0, width - mark_w), int(round(_TB_GAP * scale)))
    content_h = min(height, int(round(_TB_HEIGHT * scale)))
    top = max(0, (height - content_h) // 2)
    mark_h = min(content_h, int(round(_TB_MARK_H * scale)))
    mark_top = top + (content_h - mark_h) // 2
    return (0, mark_top, mark_w, mark_top + mark_h), mark_w + gap, top, content_h


def _tb_render(rows, message, stale, width, height, dpi, light, high_contrast,
               hover=False):
    """Render the 2-row taskbar strip as straight-alpha RGBA pixels."""
    width, height = max(1, width), max(1, height)
    dpi = max(96, dpi)
    scale = dpi / 96.0
    factor = scale * _TB_SS
    palette = _tb_palette(light, high_contrast)
    mark_box, usage_l, top, content_h = _tb_layout(width, height, dpi)

    # Pass 1: text only, supersampled then downsampled for clean antialiasing.
    canvas = Image.new("RGBA", (width * _TB_SS, height * _TB_SS))
    draw = ImageDraw.Draw(canvas)
    if hover:
        # Same treatment as the Codex strip: a translucent wash under the
        # content so the user can see the strip is clickable.
        draw.rounded_rectangle(
            (0, top * _TB_SS, (width - 1) * _TB_SS,
             (top + content_h - 1) * _TB_SS),
            radius=int(round(7 * factor)), fill=_TB_HOVER_FILL)

    def sx(value):
        return int(round((usage_l + value * scale) * _TB_SS))

    def sy(value):
        return int(round((top + value * scale) * _TB_SS))

    centers = (23.0,) if len(rows) == 1 else (13.5, 32.5)
    if rows:
        pct_right = min(sx(_TB_USAGE_W - _TB_PAD_R),
                        int(round((width - _TB_PAD_R * scale) * _TB_SS)))
        for (label, remaining), center in zip(rows, centers):
            draw.text((sx(8), sy(center)), label,
                      font=_tb_font(int(round(11 * factor)), False, True),
                      fill=palette["muted"], anchor="lm")
            draw.text((pct_right, sy(center)), _tb_percent_text(remaining),
                      font=_tb_font(int(round(14 * factor)), True, False),
                      fill=palette["text"], anchor="rm")
    else:
        draw.text((sx(8), sy(23)), message,
                  font=_tb_font(int(round(11 * factor)), False, True),
                  fill=palette["muted"], anchor="lm")
    image = canvas.resize((width, height), Image.Resampling.LANCZOS)

    # Pass 2: bars and mark at final resolution, so colours stay exact.
    # The right column belongs to the percentage (margin + the widest "100%");
    # the bar gives that up first when the strip is squeezed.
    track_l = usage_l + _TB_BAR_L * scale
    track_r = min(usage_l + (_TB_BAR_L + _TB_BAR_W) * scale,
                  width - (_TB_PCT_W + _TB_PAD_R) * scale)
    for (label, remaining), center in zip(rows, centers):
        cy = top + center * scale
        box = (track_l, cy - 2 * scale, track_r, cy + 2 * scale)
        if track_r > track_l:
            _tb_capsule(image, box, palette["track"])
            fill_r = track_l + (track_r - track_l) * max(0.0, remaining) / 100.0
            if fill_r > track_l:
                _tb_capsule(image, (track_l, box[1], fill_r, box[3]),
                            _tb_severity(remaining, palette))
    mark = _tb_mark(19 * scale)
    image.alpha_composite(
        mark,
        ((mark_box[0] + mark_box[2] - mark.width) // 2,
         (mark_box[1] + mark_box[3] - mark.height) // 2))
    dot = max(2.0, 5 * scale)
    dot_r = mark_box[2] - 7 * scale
    dot_b = mark_box[3] - 7 * scale
    _tb_capsule(image, (dot_r - dot, dot_b - dot, dot_r, dot_b),
                palette["amber"] if stale else palette["green"])

    # Layered windows hit-test on alpha: a fully transparent pixel passes the
    # click through to the taskbar. Floor the whole surface at alpha 1.
    floor = Image.new("L", image.size)
    ImageDraw.Draw(floor).rounded_rectangle(
        (0, top, width - 1, top + min(height, int(round(_TB_HEIGHT * scale))) - 1),
        radius=int(round(7 * scale)), fill=1)
    image.putalpha(ImageChops.lighter(image.getchannel("A"), floor))
    return image


def _tb_premultiplied_bgra(image):
    """Straight-alpha RGBA -> premultiplied BGRA for AC_SRC_ALPHA blending."""
    source = image.convert("RGBA").tobytes("raw", "RGBA")
    out = bytearray(len(source))
    for i in range(0, len(source), 4):
        r, g, b, a = source[i:i + 4]
        out[i] = b * a // 255
        out[i + 1] = g * a // 255
        out[i + 2] = r * a // 255
        out[i + 3] = a
    return bytes(out)


def _tb_publish(hwnd, image):
    """Publish one per-pixel-alpha frame, releasing every temporary GDI handle."""
    width, height = image.size
    if width <= 0 or height <= 0:
        return
    u, g = _tb_u32(), _tb_g32()
    screen_dc = u.GetDC(None)
    if not screen_dc:
        raise ctypes.WinError(ctypes.get_last_error())
    memory_dc = g.CreateCompatibleDC(screen_dc)
    if not memory_dc:
        u.ReleaseDC(None, screen_dc)
        raise ctypes.WinError(ctypes.get_last_error())
    bits = ctypes.c_void_p()
    info = _TB_BITMAPINFO(bmiHeader=_TB_BITMAPINFOHEADER(
        biSize=ctypes.sizeof(_TB_BITMAPINFOHEADER), biWidth=width,
        biHeight=-height, biPlanes=1, biBitCount=32))
    bitmap = g.CreateDIBSection(memory_dc, ctypes.byref(info),
                                 _TB_DIB_RGB_COLORS, ctypes.byref(bits), None, 0)
    if not bitmap or not bits.value:
        if bitmap:
            g.DeleteObject(bitmap)
        g.DeleteDC(memory_dc)
        u.ReleaseDC(None, screen_dc)
        raise ctypes.WinError(ctypes.get_last_error())
    old = g.SelectObject(memory_dc, bitmap)
    try:
        pixels = _tb_premultiplied_bgra(image)
        ctypes.memmove(bits, pixels, len(pixels))
        source, size = _TB_POINT(0, 0), _TB_SIZE(width, height)
        blend = _TB_BLENDFUNCTION(0, 0, 255, _TB_AC_SRC_ALPHA)
        if not u.UpdateLayeredWindow(hwnd, screen_dc, None, ctypes.byref(size),
                                      memory_dc, ctypes.byref(source), 0,
                                      ctypes.byref(blend), _TB_ULW_ALPHA):
            raise ctypes.WinError(ctypes.get_last_error())
    finally:
        g.SelectObject(memory_dc, old)
        g.DeleteObject(bitmap)
        g.DeleteDC(memory_dc)
        u.ReleaseDC(None, screen_dc)


def _tb_light_theme():
    try:
        import winreg
        key = winreg.OpenKey(
            winreg.HKEY_CURRENT_USER,
            r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize")
        try:
            value, _ = winreg.QueryValueEx(key, "SystemUsesLightTheme")
        finally:
            winreg.CloseKey(key)
        return bool(value)
    except (ImportError, OSError, AttributeError):
        return False


def _tb_high_contrast():
    value = _TB_HIGHCONTRASTW(cbSize=ctypes.sizeof(_TB_HIGHCONTRASTW))
    return bool(_tb_u32().SystemParametersInfoW(
        _TB_SPI_GETHIGHCONTRAST, value.cbSize, ctypes.byref(value), 0)
        and value.dwFlags & _TB_HCF_HIGHCONTRASTON)


def _tb_attach(hwnd, parent, rect, origin):
    """Reparent into the taskbar, restoring the original state on any failure."""
    u = _tb_u32()
    original_style = int(u.GetWindowLongPtrW(hwnd, _TB_GWL_STYLE)) & 0xFFFFFFFF
    original_parent = int(u.GetParent(hwnd) or 0)
    child_style = (original_style & ~_TB_WS_POPUP) | _TB_WS_CHILD | _TB_WS_CLIPSIBLINGS
    try:
        u.SetWindowLongPtrW(hwnd, _TB_GWL_STYLE, child_style)
        if int(u.GetWindowLongPtrW(hwnd, _TB_GWL_STYLE)) & 0xFFFFFFFF != child_style:
            raise OSError
        u.SetParent(hwnd, parent or None)
        if int(u.GetParent(hwnd) or 0) != parent:
            raise OSError
        _tb_position(hwnd, rect, origin)
    except OSError:
        try:
            u.SetParent(hwnd, original_parent or None)
            u.SetWindowLongPtrW(hwnd, _TB_GWL_STYLE, original_style)
        except OSError:
            pass
        return False
    return True


def _tb_position(hwnd, rect, origin):
    if not _tb_u32().SetWindowPos(
            hwnd, None, rect[0] - origin[0], rect[1] - origin[1],
            rect[2] - rect[0], rect[3] - rect[1],
            _TB_SWP_NOACTIVATE | _TB_SWP_FRAMECHANGED):
        raise ctypes.WinError(ctypes.get_last_error())


class TaskbarSurface:
    """A layered child window living inside the real taskbar.

    All native calls run on a private message thread; the taskbar geometry is
    observed on a second thread because probing UIA from the thread that owns
    the window would deadlock against the provider."""

    def __init__(self, on_left, on_right):
        self._on_left = on_left
        self._on_right = on_right
        self._lock = threading.Lock()
        self._ready = threading.Event()
        self._stop = threading.Event()
        self._thread = None
        self._observer = None
        self._rows = ()
        self._message = "불러오는 중"
        self._stale = False
        self._visible = True
        self._available = False
        self._attached = False
        self._hover = False
        self._hwnd = 0
        self._wndproc = None
        self._target = None

    @property
    def available(self):
        with self._lock:
            return self._available

    @property
    def attached(self):
        with self._lock:
            return self._attached

    def start(self):
        if sys.platform != "win32" or not _TB_HAS_COMTYPES:
            return False
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return True
            self._ready.clear()
            self._stop = threading.Event()
            self._thread = threading.Thread(target=self._run, args=(self._stop,),
                                             name="claude-taskbar", daemon=True)
            self._thread.start()
        self._ready.wait(1.0)
        return self.available

    def update(self, rows, message, stale):
        with self._lock:
            state = (tuple(rows), message, bool(stale))
            if state == (self._rows, self._message, self._stale):
                return
            self._rows, self._message, self._stale = state
            hwnd = self._hwnd
        if hwnd:
            _tb_u32().PostMessageW(hwnd, _TB_WM_APP_UPDATE, 0, 0)

    def set_visible(self, visible):
        with self._lock:
            if bool(visible) == self._visible:
                return
            self._visible = bool(visible)
            hwnd = self._hwnd
        if hwnd:
            _tb_u32().PostMessageW(hwnd, _TB_WM_APP_VISIBILITY, int(visible), 0)

    def stop(self):
        with self._lock:
            thread, hwnd = self._thread, self._hwnd
            self._stop.set()
        if hwnd:
            _tb_u32().PostMessageW(hwnd, _TB_WM_APP_STOP, 0, 0)
        if thread is not None and thread.ident != threading.get_ident():
            thread.join(timeout=2.0)
        observer = self._observer
        if observer is not None and observer.ident != threading.get_ident():
            observer.join(timeout=0.25)
        with self._lock:
            self._thread = self._observer = None
            self._available = self._attached = False
            self._hwnd = 0

    # ----- private: message thread -----

    def _run(self, stop):
        u = _tb_u32()
        # Keep Win32 and UIA rectangles in one physical coordinate space.
        u.SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetModuleHandleW.argtypes = [wintypes.LPCWSTR]
        kernel32.GetModuleHandleW.restype = wintypes.HMODULE
        hinstance = kernel32.GetModuleHandleW(None)
        self._wndproc = _TB_WNDPROC(self._window_proc)
        wc = _TB_WNDCLASSW(lpfnWndProc=self._wndproc, hInstance=hinstance,
                            lpszClassName=_TB_CLASS_NAME)
        atom = hwnd = 0
        try:
            atom = int(u.RegisterClassW(ctypes.byref(wc)))
            if not atom or stop.is_set():
                return
            hwnd = int(u.CreateWindowExW(
                _TB_WS_EX_LAYERED | _TB_WS_EX_NOACTIVATE | _TB_WS_EX_TOOLWINDOW,
                _TB_CLASS_NAME, "Claude usage", _TB_WS_POPUP,
                0, 0, 1, 1, None, None, hinstance, None) or 0)
            if not hwnd or stop.is_set():
                return
            with self._lock:
                self._hwnd = hwnd
                self._available = True
            self._set_accessible_name(hwnd)
            self._observer = threading.Thread(target=self._observe,
                                               args=(hwnd, stop),
                                               name="claude-taskbar-observer",
                                               daemon=True)
            self._observer.start()
            self._ready.set()
            message = wintypes.MSG()
            while u.GetMessageW(ctypes.byref(message), None, 0, 0) > 0:
                u.TranslateMessage(ctypes.byref(message))
                u.DispatchMessageW(ctypes.byref(message))
        except Exception:
            return
        finally:
            stop.set()
            if hwnd and u.IsWindow(hwnd):
                u.DestroyWindow(hwnd)
            if atom:
                u.UnregisterClassW(_TB_CLASS_NAME, hinstance)
            with self._lock:
                self._available = self._attached = False
                self._hwnd = 0
            self._ready.set()

    def _window_proc(self, hwnd, message, wparam, lparam):
        u = _tb_u32()
        if message == _TB_WM_PAINT:
            paint = _TB_PAINTSTRUCT()
            u.BeginPaint(hwnd, ctypes.byref(paint))
            u.EndPaint(hwnd, ctypes.byref(paint))
            return 0
        if message == _TB_WM_ERASEBKGND:
            return 1
        if message == _TB_WM_MOUSEMOVE:
            if not self._hover:
                self._hover = True
                self._paint(hwnd)
            # Re-arm every move: Windows cancels the leave request each time
            # it fires, and one missed WM_MOUSELEAVE leaves the wash stuck on.
            track = _TB_TRACKMOUSEEVENT(
                cbSize=ctypes.sizeof(_TB_TRACKMOUSEEVENT),
                dwFlags=_TB_TME_LEAVE, hwndTrack=hwnd)
            u.TrackMouseEvent(ctypes.byref(track))
            return 0
        if message == _TB_WM_MOUSELEAVE:
            if self._hover:
                self._hover = False
                self._paint(hwnd)
            return 0
        if message in (_TB_WM_LBUTTONUP, _TB_WM_RBUTTONUP):
            point = wintypes.POINT()
            u.GetCursorPos(ctypes.byref(point))
            callback = (self._on_left if message == _TB_WM_LBUTTONUP
                        else self._on_right)
            try:
                callback(point.x, point.y)
            except Exception:
                pass
            return 0
        if message == _TB_WM_APP_UPDATE:
            self._set_accessible_name(hwnd)
            self._paint(hwnd)
            return 0
        if message == _TB_WM_APP_VISIBILITY:
            self._apply_visibility()
            return 0
        if message == _TB_WM_APP_LAYOUT:
            self._apply_target()
            return 0
        if message == _TB_WM_APP_STOP:
            u.DestroyWindow(hwnd)
            return 0
        if message == _TB_WM_DESTROY:
            u.PostQuitMessage(0)
            return 0
        return u.DefWindowProcW(hwnd, message, wparam, lparam)

    def _set_accessible_name(self, hwnd):
        with self._lock:
            rows, message = self._rows, self._message
        parts = ["Claude"]
        parts.extend(f"{label} {_tb_percent_text(value)} 남음"
                     for label, value in rows)
        if not rows:
            parts.append(message)
        _tb_u32().SetWindowTextW(hwnd, ", ".join(parts))

    def _paint(self, hwnd):
        u = _tb_u32()
        try:
            bounds = _TB_RECT()
            if not u.GetClientRect(hwnd, ctypes.byref(bounds)):
                return
            with self._lock:
                rows, message, stale = self._rows, self._message, self._stale
            _tb_publish(hwnd, _tb_render(
                rows, message, stale, bounds.right, bounds.bottom,
                max(96, int(u.GetDpiForWindow(hwnd) or 96)),
                _tb_light_theme(), _tb_high_contrast(), self._hover))
        except Exception:
            with self._lock:
                self._attached = False
            u.ShowWindow(hwnd, _TB_SW_HIDE)

    # ----- private: observer thread -----

    def _observe(self, hwnd, stop):
        _tb_u32().SetThreadDpiAwarenessContext(ctypes.c_void_p(-4))
        while not stop.is_set():
            try:
                target = _tb_find_target(hwnd)
            except Exception:
                target = None
            if stop.is_set():
                return
            with self._lock:
                if stop.is_set() or hwnd != self._hwnd:
                    return
                self._target = target
            if not _tb_u32().PostMessageW(hwnd, _TB_WM_APP_LAYOUT, 0, 0):
                return
            if stop.wait(_TB_POLL_MS / 1000.0):
                return

    def _apply_target(self):
        u = _tb_u32()
        with self._lock:
            hwnd, target = self._hwnd, self._target
        if not hwnd or target is None:
            with self._lock:
                self._attached = False
            if hwnd:
                u.ShowWindow(hwnd, _TB_SW_HIDE)
            return
        parent, bounds, placement, _ = target
        attached = int(u.GetParent(hwnd) or 0) == parent
        if attached:
            try:
                _tb_position(hwnd, placement, bounds)
            except OSError:
                attached = False
        else:
            attached = _tb_attach(hwnd, parent, placement, bounds)
        with self._lock:
            self._attached = attached
        if attached:
            self._paint(hwnd)
        self._apply_visibility()

    def _apply_visibility(self):
        with self._lock:
            show = self._visible and self._attached
            hwnd = self._hwnd
        if hwnd:
            _tb_u32().ShowWindow(
                hwnd, _TB_SW_SHOWNOACTIVATE if show else _TB_SW_HIDE)


# ---------------- Config ----------------

def load_config():
    cfg = DEFAULT_CONFIG.copy()
    if CONFIG_PATH.exists():
        try:
            stored = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
            for k, v in stored.items():
                if k not in EPHEMERAL_KEYS:
                    cfg[k] = v
        except Exception:
            pass
    return cfg


def save_config(cfg):
    try:
        to_save = {k: v for k, v in cfg.items() if k not in EPHEMERAL_KEYS}
        CONFIG_PATH.write_text(json.dumps(to_save, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception:
        pass


# ---------------- API ----------------

def find_claude_exe():
    """Locate the npm-installed Claude Code launcher that supports the
    `auth login` subcommand. Returns a path string or None.

    Prefer claude.cmd (npm shim) over the Desktop-bundled claude.exe: the
    Desktop binary historically treated `login` as a chat prompt, while the
    npm CLI exposes the proper `claude auth login` subcommand."""
    appdata = os.environ.get("APPDATA", "")
    candidates = []
    if appdata:
        candidates.append(Path(appdata) / "npm" / "claude.cmd")
        candidates.append(Path(appdata) / "npm" / "node_modules"
                          / "@anthropic-ai" / "claude-code" / "bin" / "claude.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    p = shutil.which("claude")
    if p:
        return p
    # Fall back to the Desktop-bundled CLI (%APPDATA%\Claude\claude-code\<ver>\
    # claude.exe) — present when only the Claude desktop app is installed and
    # `claude` is not on PATH. Newer builds accept `auth login` here too.
    if appdata:
        base = Path(appdata) / "Claude" / "claude-code"
        if base.exists():
            versions = sorted(base.glob("*/claude.exe"),
                              key=lambda x: x.stat().st_mtime, reverse=True)
            if versions:
                return str(versions[0])
    return None


def launch_claude_login():
    """Open a visible console running `claude auth login` so the user can
    complete the browser OAuth flow. Returns True if launched."""
    exe = find_claude_exe()
    if not exe:
        return False
    try:
        if exe.lower().endswith(".cmd"):
            # cmd /k keeps the window open so the user sees login progress/errors
            subprocess.Popen(
                f'cmd /k ""{exe}" auth login"',
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
                shell=True,
            )
        else:
            subprocess.Popen(
                [exe, "auth", "login"],
                creationflags=getattr(subprocess, "CREATE_NEW_CONSOLE", 0),
            )
        return True
    except Exception:
        return False


_auth_fail_streak = 0
# Consecutive auth failures (missing token / 401 / 403) before the widget
# treats the token as genuinely stale. A single transient failure — e.g. a
# read landing while Claude Code rewrites .credentials.json during a token
# refresh — must NOT flip the status; only a sustained streak should.
#
# The widget never launches `claude login` itself: the on-disk token is
# refreshed by Claude Code's own usage (it keeps a fresher in-memory token
# and only periodically rewrites the file), so the widget just surfaces a
# "log in via Claude Code" hint and auto-recovers on the next good poll.
AUTH_FAIL_THRESHOLD = 3


def read_token():
    """Return (access_token, expires_at_ms). Either may be None when the
    credentials file is missing or momentarily unreadable."""
    try:
        oauth = json.loads(CREDS_PATH.read_text(encoding="utf-8")).get("claudeAiOauth", {})
        return oauth.get("accessToken"), oauth.get("expiresAt")
    except Exception:
        return None, None


# OAuth refresh — public client_id, identical for every Claude Code install.
OAUTH_CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"
OAUTH_TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
_refresh_lock = threading.Lock()
# A refresh_token the server permanently rejected (400/401/403 = rotated away
# or revoked). Remembered so we stop POSTing a known-dead grant every poll;
# cleared when a re-login writes a DIFFERENT refreshToken to disk.
_dead_refresh_token = None
# Epoch-ms before which we must NOT POST the token endpoint again. Set when a
# refresh returns 429/5xx (transient) so we back off instead of re-POSTing
# every 180s poll — which would itself keep the token endpoint rate-limited.
_refresh_backoff_until = 0.0


def refresh_token():
    """Use the stored refresh_token to obtain a fresh access_token and write
    it back to .credentials.json. Returns (access_token, expires_at_ms) on
    success, or (None, None) on failure.

    Handles refresh-token rotation: the response usually contains a NEW
    refresh_token that must be persisted, or the next refresh 400s.
    Re-reads the file inside the lock so we never clobber a token Claude
    Code itself just rotated.
    """
    global _dead_refresh_token, _refresh_backoff_until
    with _refresh_lock:
        try:
            creds = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        except Exception:
            return None, None
        oauth = creds.get("claudeAiOauth", {})
        rtok = oauth.get("refreshToken")
        if not rtok:
            return None, None

        # If another thread/Claude Code already refreshed while we waited for
        # the lock, just use the fresh on-disk token.
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        if oauth.get("accessToken") and oauth.get("expiresAt", 0) > now_ms + 30_000:
            return oauth["accessToken"], oauth["expiresAt"]

        # This grant already failed permanently; don't hammer the token
        # endpoint with it every poll. A re-login rewrites refreshToken,
        # which makes rtok differ from the remembered dead value.
        if rtok == _dead_refresh_token:
            return None, None

        # Backing off after a transient 429/5xx — re-POSTing now would only
        # keep the token endpoint rate-limited. Wait out the window.
        if now_ms < _refresh_backoff_until:
            return None, None

        body = json.dumps({
            "grant_type": "refresh_token",
            "refresh_token": rtok,
            "client_id": OAUTH_CLIENT_ID,
        }).encode()
        req = urllib.request.Request(
            OAUTH_TOKEN_URL, data=body, method="POST",
            headers={
                "Content-Type": "application/json",
                "Accept": "application/json",
                "User-Agent": "claude-usage-widget/2.0",
            },
        )
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                data = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code in (400, 401, 403):
                _dead_refresh_token = rtok  # permanent: invalid/rotated grant
            elif e.code == 429 or e.code >= 500:
                # Transient, but back off so we don't re-POST every poll and
                # keep the endpoint rate-limited. Respect Retry-After; else 15m.
                try:
                    retry_s = int(e.headers.get("Retry-After", "0") or 0)
                except (TypeError, ValueError):
                    retry_s = 0
                _refresh_backoff_until = now_ms + max(retry_s, 900) * 1000
            return None, None
        except Exception:
            return None, None  # network error — transient, retry later

        access = data.get("access_token")
        if not access:
            return None, None
        _dead_refresh_token = None
        _refresh_backoff_until = 0.0
        expires_at = int(now_ms) + int(data.get("expires_in", 28800)) * 1000
        oauth["accessToken"] = access
        oauth["refreshToken"] = data.get("refresh_token", rtok)  # rotation-safe
        oauth["expiresAt"] = expires_at
        creds["claudeAiOauth"] = oauth
        # Atomic write: a crash mid-write must never corrupt the credentials
        # file shared with Claude Code (that would break login for everything).
        try:
            tmp = CREDS_PATH.with_name(CREDS_PATH.name + ".tmp")
            tmp.write_text(json.dumps(creds, indent=2), encoding="utf-8")
            os.replace(tmp, CREDS_PATH)
        except Exception:
            pass  # token still usable in-memory even if write fails
        return access, expires_at


def detect_plan_label():
    """Read subscription tier from local credentials, return display label."""
    try:
        d = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
        oauth = d.get("claudeAiOauth", {})
        tier = (oauth.get("rateLimitTier") or "").lower()
        sub = (oauth.get("subscriptionType") or "").lower()
        if "max_20x" in tier:
            return "Max(20x)"
        if "max_5x" in tier:
            return "Max(5x)"
        if "pro" in tier or sub == "pro":
            return "Pro"
        if sub == "max":
            return "Max"
        if sub:
            return sub.title()
    except Exception:
        pass
    return "Max"


def fetch_usage():
    """Returns (data, error_msg, retry_after_seconds).
    retry_after_seconds is non-zero only on 429 with Retry-After header.
    """
    global _auth_fail_streak
    token, expires_at = read_token()
    if not token:
        # Missing/unreadable token. Could be a genuine logout OR a transient
        # read collision with Claude Code rewriting the creds file. Only flip
        # the status after a sustained streak so a single blip is ignored.
        _auth_fail_streak += 1
        if _auth_fail_streak >= AUTH_FAIL_THRESHOLD:
            return None, "LOGIN_REQUIRED", 0
        return None, "토큰 확인 중…", 0
    # Stored token expired (or about to) -> refresh it ourselves via the
    # OAuth refresh_token grant before calling the usage endpoint. This avoids
    # 401s (which feed the rate limiter) and removes the need to run Claude
    # Code just to mint a fresh token.
    if expires_at:
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        if expires_at <= now_ms + 60_000:  # refresh 60s before expiry
            new_token, new_exp = refresh_token()
            if new_token:
                token = new_token
            else:
                _auth_fail_streak += 1
                if _auth_fail_streak >= AUTH_FAIL_THRESHOLD:
                    return None, "LOGIN_REQUIRED", 0
                return None, "토큰 갱신 중…", 0
    req = urllib.request.Request(
        USAGE_URL,
        headers={
            "Authorization": f"Bearer {token}",
            "anthropic-beta": "oauth-2025-04-20",
            "Accept": "application/json",
            "User-Agent": "claude-usage-widget/2.0",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            _auth_fail_streak = 0
            return json.loads(resp.read().decode("utf-8")), None, 0
    except urllib.error.HTTPError as e:
        if e.code in (401, 403):
            # Token rejected despite not looking expired locally. Try one
            # refresh, then retry the usage call once.
            new_token, _ = refresh_token()
            if new_token:
                try:
                    req2 = urllib.request.Request(USAGE_URL, headers={
                        "Authorization": f"Bearer {new_token}",
                        "anthropic-beta": "oauth-2025-04-20",
                        "Accept": "application/json",
                        "User-Agent": "claude-usage-widget/2.0",
                    })
                    with urllib.request.urlopen(req2, timeout=10) as resp2:
                        _auth_fail_streak = 0
                        return json.loads(resp2.read().decode("utf-8")), None, 0
                except Exception:
                    pass
            _auth_fail_streak += 1
            if _auth_fail_streak >= AUTH_FAIL_THRESHOLD:
                return None, "LOGIN_REQUIRED", 0
            return None, "인증 확인 중…", 0
        if e.code == 429:
            # Respect server's Retry-After (seconds)
            try:
                retry = int(e.headers.get("Retry-After", "0") or 0)
            except (TypeError, ValueError):
                retry = 0
            msg = f"API rate limited ({retry//60}분 대기)" if retry > 60 else "API rate limited"
            return None, msg, retry
        return None, f"HTTP {e.code}", 0
    except (urllib.error.URLError, TimeoutError):
        return None, "네트워크 오류", 0
    except Exception as e:
        return None, f"오류: {e}", 0


# ---------------- Helpers ----------------

def parse_iso(s):
    if not s:
        return None
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00"))
    except Exception:
        return None


def fmt_remaining(dt):
    if not dt:
        return ""
    secs = (dt - datetime.now(timezone.utc)).total_seconds()
    if secs <= 0:
        return "리셋 임박"
    h = int(secs // 3600)
    m = int((secs % 3600) // 60)
    return f"{h}h {m}m 남음" if h else f"{m}m 남음"


def fmt_reset_local(dt):
    if not dt:
        return ""
    try:
        return dt.astimezone().strftime("%a %H:%M")
    except Exception:
        return ""


# ---------------- Widget ----------------

class Widget:
    def __init__(self):
        self.cfg = load_config()
        # One-time migration: older configs listed the generic "pythonw.exe" in
        # claude_processes, which made smart-topmost trigger for EVERY Python
        # app (the separate Codex usage widget, 규정봇, etc.). Self is now matched
        # by PID, so strip the stale entry (write only when it was present).
        _procs = self.cfg.get("claude_processes")
        if isinstance(_procs, list) and any(
                isinstance(p, str) and p.lower() == "pythonw.exe" for p in _procs):
            self.cfg["claude_processes"] = [
                p for p in _procs
                if not (isinstance(p, str) and p.lower() == "pythonw.exe")]
            save_config(self.cfg)
        self.fetching = False
        self._current_z = "top"
        self._foreground_check_id = None
        self._consec_429 = 0
        self.theme_name = self.cfg.get("theme", "light")
        self.theme = THEMES.get(self.theme_name, THEMES["light"])
        self._alpha_popup = None
        self._visible = True
        self.tray_icon = None
        self._login_dialog = None
        self._login_poll_attempts = 0
        self.minimized = bool(self.cfg.get("minimized"))
        self._mini_pcts = (0.0, 0.0, 0.0)
        self.taskbar = None
        self._taskbar_events = queue.Queue()

        self.root = tk.Tk()

        # Apply DPI scaling. ui_scale is a multiplier over the 96 DPI baseline;
        # 1.3 keeps text crisp without bloating the widget the way 1.5 (the
        # system's native scaling on a 150% display) does. tk.scaling takes
        # "pixels per point" so we multiply by 96/72.
        ui_scale = self.cfg.get("ui_scale") or DEFAULT_CONFIG["ui_scale"]
        self._ui_scale = ui_scale
        try:
            self.root.tk.call("tk", "scaling", ui_scale * (96.0 / 72.0))
        except Exception:
            pass

        # One-time migration: stored x/y were saved by a DPI-UNAWARE widget
        # (logical pixels). Re-express them in DPI-aware physical pixels so
        # the widget appears in the same visual spot after upgrade.
        if not self.cfg.get("_dpi_migrated"):
            if ui_scale != 1.0:
                self.cfg["x"] = int(self.cfg.get("x", 100) * ui_scale)
                self.cfg["y"] = int(self.cfg.get("y", 100) * ui_scale)
            self.cfg["_dpi_migrated"] = True
            save_config(self.cfg)

        # Pet image is raster, not vector — scale its pixel size with ui_scale
        # so it stays visually proportional to the (now DPI-aware) UI.
        self._pet_size = max(8, int(PET_SIZE * ui_scale))

        # IMPORTANT: pet loading must happen AFTER tk.Tk() exists, otherwise
        # ImageTk.PhotoImage creates a hidden Tcl interpreter and the resulting
        # image won't display in our real root's Labels.
        pet_key = ensure_pet_assigned(self.cfg)
        self.pet_photo = load_pet_photo(pet_key, self._pet_size)
        self.root.title("Claude Usage")
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", float(self.cfg.get("alpha", 0.95)))
        except Exception:
            self.root.attributes("-alpha", 0.95)

        vw = self.root.winfo_vrootwidth() or self.root.winfo_screenwidth()
        vh = self.root.winfo_vrootheight() or self.root.winfo_screenheight()
        x = int(self.cfg.get("x", 100))
        y = int(self.cfg.get("y", 100))
        if x > vw - 100 or x < -200 or y > vh - 100 or y < -50:
            x, y = 100, 100
        self.cfg["x"], self.cfg["y"] = x, y
        self.root.geometry(f"+{x}+{y}")

        self._build_ui()
        self._apply_theme()
        self._bind_drag()
        self._bind_menu()
        self.root.bind_all("<Button-1>", self._on_global_click, add="+")
        # Double-click the mini strip restores the full card.
        self.root.bind("<Double-Button-1>", self._on_double_click, add="+")
        if self.minimized:
            self._set_minimized(True, save=False)

        def _post_map_win32():
            set_window_zorder(self._hwnd(), "top")
            hide_from_taskbar(self._hwnd())
            # By now the window has its real on-screen size, so this is the
            # first point where the fine-grained monitor clamp (as opposed
            # to the coarse "way off-screen" reset above) can run reliably.
            self._clamp_to_screen()
        self.root.after(80, _post_map_win32)

        self._setup_tray()
        self._setup_taskbar()

        self.refresh()
        self._schedule_refresh()
        if self.cfg.get("smart_topmost"):
            self._foreground_check_id = self.root.after(700, self._check_topmost)

    def _hwnd(self):
        try:
            return int(self.root.wm_frame(), 16)
        except (TypeError, ValueError):
            return self.root.winfo_id()

    def _clamp_to_screen(self, persist=True):
        """Nudge the window fully onto its monitor if any edge is off-screen.

        Called after every drag, size change (minimize/restore, mini-scale
        change) and tray restore, plus once at startup. Deliberately never
        raises: worst case is the window stays wherever it already was,
        which is no worse than before this method existed.
        """
        try:
            self.root.update_idletasks()
            hwnd = self._hwnd()
            rect = None
            if hwnd:
                r = wintypes.RECT()
                if _user32.GetWindowRect(hwnd, ctypes.byref(r)):
                    rect = (r.left, r.top, r.right - r.left, r.bottom - r.top)
            if rect is None:
                # Fallback when the hwnd lookup fails: Tk's own idea of
                # position/size (less authoritative than GetWindowRect, but
                # good enough to avoid leaving the window unclamped).
                rect = (self.root.winfo_x(), self.root.winfo_y(),
                        self.root.winfo_width(), self.root.winfo_height())
            x, y, w, h = rect

            mon = monitor_rect_for_window(hwnd) if hwnd else None
            if mon is None:
                # Fall back to the virtual root spanning all monitors so we
                # still clamp *something* rather than doing nothing.
                vx = self.root.winfo_vrootx()
                vy = self.root.winfo_vrooty()
                vw = self.root.winfo_vrootwidth() or self.root.winfo_screenwidth()
                vh = self.root.winfo_vrootheight() or self.root.winfo_screenheight()
                mon = (vx, vy, vx + vw, vy + vh)

            new_x, new_y = clamp_rect_to_monitor(x, y, w, h, mon)
            if (new_x, new_y) != (x, y):
                self.root.geometry(f"+{new_x}+{new_y}")

            if persist and (self.cfg.get("x") != new_x or self.cfg.get("y") != new_y):
                self.cfg["x"] = new_x
                self.cfg["y"] = new_y
                save_config(self.cfg)
        except Exception:
            pass

    def _build_ui(self):
        self.outer = tk.Frame(self.root, padx=10, pady=6)
        self.outer.pack()

        title_bar = tk.Frame(self.outer)
        title_bar.pack(fill="x")

        # Title first (leftmost), then pet to its right
        self.title_lbl = tk.Label(
            title_bar,
            text=f"Claude {(self.cfg.get('plan_label') or '').strip() or detect_plan_label()}",
            font=("Segoe UI", 9, "bold"),
        )
        self.title_lbl.pack(side="left")

        if self.pet_photo is not None:
            style = pet_style_for(self.cfg.get("pet"))
            self.pet_lbl = AnimatedPet(
                title_bar, self.pet_photo, self._pet_size,
                bg=self.theme["bg"], style=style,
            )
            self.pet_lbl.pack(side="left", padx=(1, 0))
        else:
            self.pet_lbl = tk.Label(title_bar, text="●",
                                      font=("Segoe UI", 9, "bold"))
            self.pet_lbl.pack(side="left", padx=(1, 0))

        # Icons pinned to the far right of the title bar.
        # Pack order matters with side="right": first packed = rightmost.
        # Visual L->R: [theme  alpha  close]
        self.close_btn = tk.Label(title_bar, text="✕", cursor="hand2",
                                    font=("Segoe UI", 9, "bold"), padx=3)
        self.close_btn.pack(side="right")
        # X = minimize to tray (full quit via tray menu only)
        self.close_btn.bind("<Button-1>", lambda e: self.hide_to_tray())

        # Minimize to battery strip (▾). Sits between ◐ and ✕.
        self.min_btn = tk.Label(title_bar, text="▾", cursor="hand2",
                                  font=("Segoe UI", 10), padx=3)
        self.min_btn.pack(side="right")
        self.min_btn.bind("<Button-1>", lambda e: self._toggle_minimize())

        self.alpha_btn = tk.Label(title_bar, text="◐", cursor="hand2",
                                    font=("Segoe UI", 10), padx=3)
        self.alpha_btn.pack(side="right")
        self.alpha_btn.bind("<Button-1>", lambda e: self._toggle_alpha_popup())

        self.theme_btn = tk.Label(title_bar, cursor="hand2",
                                    font=("Segoe UI", 10), padx=3)
        self.theme_btn.pack(side="right")
        self.theme_btn.bind("<Button-1>", lambda e: self._toggle_theme())

        self.session_pct_lbl = tk.Label(self.outer, text="현재 세션  …",
                                          font=("Segoe UI", 9), anchor="w")
        self.session_pct_lbl.pack(fill="x", pady=(6, 1))
        self.session_canvas = tk.Canvas(self.outer, width=BAR_WIDTH, height=BAR_HEIGHT,
                                          highlightthickness=0)
        self.session_canvas.pack()
        self.session_detail_lbl = tk.Label(self.outer, text="",
                                             font=("Segoe UI", 8), anchor="w")
        self.session_detail_lbl.pack(fill="x", pady=(1, 0))

        self.weekly_pct_lbl = tk.Label(self.outer, text="주간 한도  …",
                                         font=("Segoe UI", 9), anchor="w")
        self.weekly_pct_lbl.pack(fill="x", pady=(6, 1))
        self.weekly_canvas = tk.Canvas(self.outer, width=BAR_WIDTH, height=BAR_HEIGHT,
                                         highlightthickness=0)
        self.weekly_canvas.pack()
        self.weekly_detail_lbl = tk.Label(self.outer, text="",
                                            font=("Segoe UI", 8), anchor="w")
        self.weekly_detail_lbl.pack(fill="x", pady=(1, 0))

        self.sonnet_pct_lbl = tk.Label(self.outer, text="Sonnet 주간  …",
                                         font=("Segoe UI", 9), anchor="w")
        self.sonnet_pct_lbl.pack(fill="x", pady=(6, 1))
        self.sonnet_canvas = tk.Canvas(self.outer, width=BAR_WIDTH, height=BAR_HEIGHT,
                                         highlightthickness=0)
        self.sonnet_canvas.pack()

        self.footer_lbl = tk.Label(self.outer, text="loading…",
                                     font=("Segoe UI", 7), anchor="w")
        self.footer_lbl.pack(fill="x", pady=(5, 0))

        # Minimized "battery" strip — built now, packed only in mini mode.
        s = self._ui_scale
        ms = self._ui_scale * float(self.cfg.get("mini_scale", 1.0))
        self.mini_frame = tk.Frame(self.root, padx=int(round(6 * s)),
                                    pady=int(round(4 * s)))
        self.mini_canvas = tk.Canvas(
            self.mini_frame,
            width=int(round(230 * ms)), height=int(round(40 * ms)),
            highlightthickness=0, bd=0,
        )
        self.mini_canvas.pack()

    def _apply_theme(self):
        t = self.theme
        self.root.configure(bg=t["bg"])
        self.outer.configure(bg=t["bg"])
        self.title_lbl.master.configure(bg=t["bg"])
        self.title_lbl.configure(bg=t["bg"], fg=t["accent"])
        self.pet_lbl.configure(bg=t["bg"])
        # Only the fallback dot Label has 'text' / 'fg' to update
        if isinstance(self.pet_lbl, tk.Label) and self.pet_lbl.cget("text") == "●":
            self.pet_lbl.configure(fg=t["accent"])
        self.close_btn.configure(bg=t["bg"], fg=t["btn"])
        self.alpha_btn.configure(bg=t["bg"], fg=t["btn"])
        self.min_btn.configure(bg=t["bg"], fg=t["btn"])
        self.theme_btn.configure(bg=t["bg"], fg=t["btn"],
                                  text="☀" if self.theme_name == "dark" else "☾")
        for w in (self.session_pct_lbl, self.weekly_pct_lbl, self.sonnet_pct_lbl):
            w.configure(bg=t["bg"], fg=t["fg"])
        for w in (self.session_detail_lbl, self.weekly_detail_lbl):
            w.configure(bg=t["bg"], fg=t["dim"])
        for c in (self.session_canvas, self.weekly_canvas, self.sonnet_canvas):
            c.configure(bg=t["bar_bg"])
        self.footer_lbl.configure(bg=t["bg"], fg=t["muted"])
        # Mini strip floats transparent while minimized — only repaint it with
        # the theme bg when it's NOT minimized (else keep the theme's
        # transparent key so the transparentcolor keying still hides the
        # background; _toggle_theme re-runs _set_minimized to swap keys).
        if not self.minimized:
            self.mini_frame.configure(bg=t["bg"])
            self.mini_canvas.configure(bg=t["bg"])
        self._draw_mini()

    def _draw_bar(self, canvas, pct):
        canvas.delete("all")
        fill_w = int(BAR_WIDTH * max(0, min(pct, 100)) / 100)
        canvas.create_rectangle(0, 0, fill_w, BAR_HEIGHT,
                                  fill=bar_color(pct), outline="")

    # ----- Minimized battery strip -----

    def _draw_mini(self):
        """Render two iPhone-style batteries (session, weekly) on mini_canvas.
        Batteries show REMAINING capacity with fixed pastel colors that turn
        red when the remaining charge drops to 20% or below."""
        if not hasattr(self, "mini_canvas"):
            return
        c = self.mini_canvas
        t = self.theme
        # Independent mini scale over the DPI/ui scale; adjustable live.
        ms = self._ui_scale * float(self.cfg.get("mini_scale", 1.0))
        # Resize the canvas live so mini-scale changes apply without restart.
        self.mini_canvas.config(width=int(round(230 * ms)),
                                 height=int(round(40 * ms)))
        # The resize above can push the window's right/bottom edge past the
        # monitor even though nothing moved — re-clamp once Tk has laid the
        # new canvas size out (a direct call here would still see the old
        # window geometry).
        self.root.after(50, self._clamp_to_screen)
        c.delete("all")

        def S(v):
            return int(round(v * ms))

        # No capsule/background: batteries + labels float directly over the
        # desktop (true per-pixel translucency is impossible in tkinter —
        # would require an UpdateLayeredWindow rewrite; user chose no-bg).
        # Defensive: pad to 3 in case a stale 2-tuple lingers in state.
        s_pct, w_pct, f_pct = (*self._mini_pcts, 0.0)[:3]

        # Theme-aware label colors so the floating S/W/F letters stay readable
        # on both themes (dark-mode weekly was unreadable with the old blue).
        if self.theme_name == "dark":
            session_lbl, weekly_lbl, fable_lbl = "#a6e3a1", "#89b4fa", "#cba6f7"
        else:
            session_lbl, weekly_lbl, fable_lbl = "#5aa15d", "#2563eb", "#7c3aed"

        # (label, label_color, base_fill, body_x, usage_pct)
        groups = [
            ("S", session_lbl, MINI_SESSION_FILL, 20, s_pct),
            ("W", weekly_lbl, MINI_WEEKLY_FILL, 94, w_pct),
            ("F", fable_lbl, MINI_FABLE_FILL, 168, f_pct),
        ]
        for (label, lbl_color, base_fill, body_x_raw,
             pct) in groups:
            usage = max(0.0, min(100.0, float(pct or 0)))
            remaining = max(0.0, min(100.0, 100.0 - usage))
            # Scaled body box; the inner fill box is derived FROM it so the
            # top/bottom (and left/right) margins stay symmetric at fractional
            # scales instead of rounding S(13)/S(14) independently.
            body_x = S(body_x_raw)
            body_y = S(10)
            body_w = S(48)
            body_h = S(20)
            inset = S(3)
            fill_x = body_x + inset
            fill_y = body_y + inset
            fill_h = body_h - 2 * inset
            fill_max_w = body_w - 2 * inset
            # battery body — fg outline like the reference icon
            _rounded_rect(c, body_x, body_y, body_w, body_h, S(6),
                          fill=t["bar_bg"], outline=t["fg"], width=max(1, S(1)))
            # chunky terminal nub on the right (derived from body_x)
            _rounded_rect(c, body_x + S(49), S(15), S(4), S(10), S(2),
                          fill=t["fg"], outline="")
            # inner fill proportional to REMAINING capacity (squarish block)
            if remaining > 0:
                fw_px = max(S(3), int(round(fill_max_w * remaining / 100)))
                fill_color = MINI_LOW_FILL if remaining <= 20 else base_fill
                # Plain exact rectangle (sharp corners) so the top/bottom
                # margins are pixel-symmetric; the smooth-polygon fill used to
                # render a larger gap at the bottom than the top. Tk fills an
                # outline-less rectangle only up to x2-1/y2-1 (bottom/right
                # exclusive), so +1 on both to render the full intended box.
                c.create_rectangle(fill_x, fill_y, fill_x + fw_px + 1,
                                   fill_y + fill_h + 1,
                                   fill=fill_color, outline="")
            # floating S/W/F label OUTSIDE the battery, right-anchored a fixed
            # S(6) before the body so the gap stays constant at any scale
            # and the wide W glyph can never overlap the battery outline.
            # Hard-edge PIL image (thresholded alpha) instead of create_text so
            # there is no ClearType fringe blending toward the transparent key
            # (which read as a white outline on dark desktops).
            lbl_img = make_label_image(label, lbl_color, max(8, S(12)))  # 9pt ≈ 12px @96dpi
            # +2 cancels make_label_image's built-in 2px transparent pad so the
            # INKED right edge of every glyph sits exactly S(5) from the battery
            # body's left edge — even gap for S/W/F at any scale.
            c.create_image(body_x - S(5) + 2, S(20), image=lbl_img, anchor="e")
            # % remaining text centered in body
            txt_color = "#111827" if remaining >= 50 else t["fg"]
            c.create_text(body_x + S(48) // 2, body_y + body_h // 2,
                          text=f"{remaining:.0f}%", anchor="center",
                          fill=txt_color, font=("Segoe UI", 8, "bold"))

    def _trans_key(self):
        """Transparentcolor key for the floating mini strip — the LIGHT
        (near-white) key in BOTH themes. Windows -transparentcolor
        (LWA_COLORKEY) keys out EXACTLY this RGB, not a range, so the
        near-white key never collides with dark theme's near-white fg
        (#f3f4f6 != #fdfdfb — those pixels stay opaque). Using one light
        key everywhere makes the floating S/W labels' ClearType fringe blend
        toward white (invisible on light desktops) instead of the near-black
        dark key, which drew a visible dark outline around the letters.
        MINI_TRANS_KEY_DARK is retained for reference/fallback."""
        return MINI_TRANS_KEY_LIGHT

    def _set_minimized(self, flag, save=True):
        self.minimized = bool(flag)
        # Always clear any previously-set transparent color key first. Older
        # builds keyed the strip out via -transparentcolor and may have saved
        # minimized=True; this guarantees the opaque strip renders normally.
        try:
            self.root.attributes("-transparentcolor", "")
        except Exception:
            pass
        if self.minimized:
            self.outer.pack_forget()
            # v2-style floating strip: key the background out via
            # -transparentcolor so only the batteries + labels show over the
            # desktop (no capsule). Tradeoff: the transparent gaps between
            # batteries are click-through — user chose transparency.
            key = self._trans_key()
            self.root.configure(bg=key)
            self.mini_frame.configure(bg=key)
            self.mini_canvas.configure(bg=key)
            self.mini_frame.pack()
            try:
                self.root.attributes("-transparentcolor", key)
            except Exception:
                pass
            self._draw_mini()
        else:
            self.root.configure(bg=self.theme["bg"])
            self.mini_frame.pack_forget()
            self.outer.pack()
        # attribute churn above can re-add the taskbar button
        hide_from_taskbar(self._hwnd())
        if save:
            self.cfg["minimized"] = self.minimized
            save_config(self.cfg)
        if hasattr(self, "mini_var"):
            self.mini_var.set(self.minimized)

    def _toggle_minimize(self):
        self._set_minimized(not self.minimized)
        # Switching modes is a request to LOOK at the desktop widget, but the
        # taskbar strip's left click (and the tray) may have withdrawn it.
        # _set_minimized only re-lays-out, so without this the mode change
        # reads as "the widget vanished" — the window stays hidden.
        if not self._visible:
            self.show_widget()
        # Minimizing/restoring changes the window's footprint (full card vs.
        # battery strip); re-clamp once the new size is laid out.
        self.root.after(50, self._clamp_to_screen)

    def _on_double_click(self, _e):
        if self.minimized:
            self._set_minimized(False)

    def _toggle_theme(self):
        self.theme_name = "light" if self.theme_name == "dark" else "dark"
        self.theme = THEMES[self.theme_name]
        self.cfg["theme"] = self.theme_name
        save_config(self.cfg)
        self._apply_theme()
        # While minimized, re-run the minimize path so the NEW theme's
        # transparent key is applied to the bgs and -transparentcolor;
        # otherwise old-key pixels stay opaque as a colored slab.
        if self.minimized:
            self._set_minimized(True, save=False)
        if hasattr(self, "_last_data") and self._last_data:
            self._render(self._last_data, None)

    def _toggle_alpha_popup(self):
        if self._alpha_popup is not None and self._alpha_popup.winfo_exists():
            self._close_alpha_popup()
            return
        t = self.theme
        popup = tk.Toplevel(self.root)
        popup.overrideredirect(True)
        popup.attributes("-topmost", True)
        popup.configure(bg=t["dim"])
        inner = tk.Frame(popup, bg=t["bg"], padx=10, pady=8)
        inner.pack(padx=1, pady=1)
        row = tk.Frame(inner, bg=t["bg"])
        row.pack(fill="x")
        tk.Label(row, text="투명도", bg=t["bg"], fg=t["dim"],
                  font=("Segoe UI", 8)).pack(side="left")
        value_lbl = tk.Label(row, text="--", bg=t["bg"], fg=t["fg"],
                              font=("Segoe UI Semibold", 9))
        value_lbl.pack(side="right")
        slider = CircleSlider(
            inner, width=160, height=22,
            min_val=30, max_val=100,
            value=int(float(self.cfg.get("alpha", 1.0)) * 100),
            track_bg=t["bar_bg"],
            track_active=t["accent"],
            handle_fill=t["bg"],
            handle_border=t["accent"],
            on_change=lambda v: self._on_alpha_popup_change(v, value_lbl),
            on_release=lambda v: save_config(self.cfg),
        )
        slider.pack(pady=(4, 0))
        popup.update_idletasks()
        pw = popup.winfo_width()
        bx = self.alpha_btn.winfo_rootx()
        by = self.alpha_btn.winfo_rooty()
        bw = self.alpha_btn.winfo_width()
        bh = self.alpha_btn.winfo_height()
        px = bx + bw - pw + 4
        py = by + bh + 2
        vw = self.root.winfo_vrootwidth() or self.root.winfo_screenwidth()
        px = max(0, min(px, vw - pw))
        popup.geometry(f"+{px}+{py}")
        self._alpha_popup = popup

    def _close_alpha_popup(self):
        if self._alpha_popup is not None:
            try:
                if self._alpha_popup.winfo_exists():
                    save_config(self.cfg)
                    self._alpha_popup.destroy()
            except Exception:
                pass
        self._alpha_popup = None

    def _on_alpha_popup_change(self, value, value_lbl):
        try:
            pct = int(float(value))
            a = max(0.3, min(1.0, pct / 100))
            self.cfg["alpha"] = a
            self.root.attributes("-alpha", a)
            value_lbl.config(text=f"{pct}%")
        except Exception:
            pass

    def _on_global_click(self, event):
        if self._alpha_popup is None or not self._alpha_popup.winfo_exists():
            return
        x, y = event.x_root, event.y_root
        pop = self._alpha_popup
        px, py = pop.winfo_rootx(), pop.winfo_rooty()
        pw, ph = pop.winfo_width(), pop.winfo_height()
        if px <= x <= px + pw and py <= y <= py + ph:
            return
        bx = self.alpha_btn.winfo_rootx()
        by = self.alpha_btn.winfo_rooty()
        bw = self.alpha_btn.winfo_width()
        bh = self.alpha_btn.winfo_height()
        if bx <= x <= bx + bw and by <= y <= by + bh:
            return
        self._close_alpha_popup()

    def _bind_drag(self):
        def start(e):
            self._dx = e.x_root - self.root.winfo_x()
            self._dy = e.y_root - self.root.winfo_y()
        def move(e):
            self.root.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")
        def end(_):
            # Also covers the mini (minimized) strip: root's bindtag is part
            # of every descendant widget's bindtags by default, so a drag
            # released on mini_canvas/mini_frame still calls this handler.
            self._clamp_to_screen()
        for w in (self.root, self.title_lbl, self.pet_lbl):
            w.bind("<Button-1>", start)
            w.bind("<B1-Motion>", move)
            w.bind("<ButtonRelease-1>", end)

    def _bind_menu(self):
        # The right-click menu must NOT grow with ui_scale. tk.scaling scales
        # everything specified in points, so we give the menu a PIXEL font
        # size (negative value) — pixel sizes ignore tk.scaling. The size
        # tracks the system DPI so the popup looks like a native Windows menu
        # at any ui_scale.
        menu_px = -max(11, round(12 * detect_system_scale()))
        menu_font = ("Segoe UI", menu_px)
        self.menu = tk.Menu(self.root, tearoff=0, font=menu_font)
        self.menu.add_command(label="지금 새로고침", command=self.refresh)
        self.menu.add_separator()
        self.mini_var = tk.BooleanVar(value=self.minimized)
        self.menu.add_checkbutton(label="미니 모드 (배터리)",
                                   variable=self.mini_var,
                                   command=self._toggle_minimize)
        self.menu.add_command(label="다크/라이트 전환", command=self._toggle_theme)
        self.smart_var = tk.BooleanVar(value=self.cfg.get("smart_topmost", True))
        self.menu.add_checkbutton(label="스마트 포지션 스위칭",
                                   variable=self.smart_var,
                                   command=self._toggle_smart)
        self.taskbar_var = tk.BooleanVar(
            value=bool(self.cfg.get("taskbar_visible", True)))
        self.menu.add_checkbutton(label="작업표시줄 표시",
                                   variable=self.taskbar_var,
                                   command=self._toggle_taskbar)
        self._taskbar_menu_index = self.menu.index("end")
        self.menu.add_command(label="플랜 이름 변경", command=self._prompt_plan)
        self.menu.add_command(label="새로고침 간격 변경", command=self._prompt_interval)
        self.auto_update_var = tk.BooleanVar(value=bool(self.cfg.get("auto_update", True)))
        self.menu.add_checkbutton(label="자동 업데이트",
                                   variable=self.auto_update_var,
                                   command=self._toggle_auto_update)
        # UI scale cascade — selecting a value writes ui_scale to config
        # and restarts the widget so the new tk_scaling takes effect.
        current = self.cfg.get("ui_scale") or DEFAULT_CONFIG["ui_scale"]
        scale_menu = tk.Menu(self.menu, tearoff=0, font=menu_font)
        for label, value in (("0.8×", 0.8), ("1.0× (기본)", 1.0),
                             ("1.3×", 1.3), ("1.5×", 1.5), ("2.0×", 2.0)):
            mark = " ✓" if abs(value - current) < 0.01 else ""
            scale_menu.add_command(label=f"{label}{mark}",
                                    command=lambda v=value: self._set_ui_scale(v))
        self.menu.add_cascade(label="UI 배율 (재시작 적용)", menu=scale_menu)
        # Mini-size cascade — applies live (no restart) to the battery strip.
        mini_current = self.cfg.get("mini_scale") or DEFAULT_CONFIG["mini_scale"]
        mini_menu = tk.Menu(self.menu, tearoff=0, font=menu_font)
        for label, value in (("0.8×", 0.8), ("1.0× (기본)", 1.0),
                             ("1.2×", 1.2), ("1.5×", 1.5)):
            mark = " ✓" if abs(value - mini_current) < 0.01 else ""
            mini_menu.add_command(label=f"{label}{mark}",
                                   command=lambda v=value: self._set_mini_scale(v))
        self.menu.add_cascade(label="미니 크기", menu=mini_menu)
        self.menu.add_separator()
        self.menu.add_command(label="펫 다시 뽑기", command=self._reroll_pet)
        self.menu.add_separator()
        self.menu.add_command(label=f"버전 v{__version__}", state="disabled")
        self.menu.add_command(label="종료", command=self.quit)
        self.root.bind("<Button-3>", lambda e: self._popup_menu(e.x_root, e.y_root))

    def _popup_menu(self, x, y):
        """Show the context menu, re-syncing live state first.

        Also serves the embedded taskbar strip's right click, which can arrive
        while the desktop window is withdrawn."""
        alive = self.taskbar is not None and self.taskbar.available
        self.taskbar_var.set(bool(self.cfg.get("taskbar_visible", True)) and alive)
        try:
            self.menu.entryconfigure(
                self._taskbar_menu_index,
                state="normal" if alive else "disabled",
                label="작업표시줄 표시" if alive else "작업표시줄 표시 (사용 불가)")
        except Exception:
            pass
        # tk_popup is TrackPopupMenu on Windows, which only dismisses on an
        # outside click or ESC while the owning window is the FOREGROUND one.
        # The taskbar strip is WS_EX_NOACTIVATE, so a right click there leaves
        # Explorer in front and the menu would stick. Claim the foreground
        # first (this works even while the root window is withdrawn) and close
        # the loop with the documented WM_NULL handshake.
        owner = ctypes.c_void_p(self._hwnd() or 0)
        if owner.value:
            claim_foreground(owner)
        try:
            self.menu.tk_popup(x, y)
        finally:
            self.menu.grab_release()
            if owner.value:
                _user32.PostMessageW(owner, 0, 0, 0)

    def _set_ui_scale(self, scale):
        """Persist a new UI scale and re-launch so tk.scaling can apply
        cleanly to a fresh root. Existing widgets cannot have their scaling
        retroactively changed."""
        self.cfg["ui_scale"] = scale
        # Position migration already happened on first DPI-aware run; the
        # stored x/y are now in physical-pixel space and remain correct
        # across scale changes.
        save_config(self.cfg)
        self._relaunch()

    def _relaunch(self):
        """Spawn a fresh copy of this program and exit. Used after a UI-scale
        change (tk.scaling can't be applied retroactively) and after an
        auto-update has swapped widget.pyw on disk."""
        # Release the singleton mutex BEFORE spawning the replacement so the
        # new process can acquire it; otherwise it would see this still-alive
        # process as the running instance and exit on startup.
        release_single_instance()
        try:
            subprocess.Popen(_python_launcher(),
                             creationflags=getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0))
        except Exception:
            pass
        self.quit()

    # ---- Auto-update -------------------------------------------------------
    def _schedule_update_check(self, delay_ms):
        """Arm the next release check. No-op when the user opted out or when
        running as a frozen .exe (a running exe cannot overwrite itself)."""
        if getattr(sys, "frozen", False) or not self.cfg.get("auto_update", True):
            self._update_after_id = None
            return
        self._update_after_id = self.root.after(delay_ms, self._check_update)

    def _check_update(self):
        self._update_after_id = None
        if not self.cfg.get("auto_update", True) or getattr(self, "_update_busy", False):
            return
        self._update_busy = True
        target = Path(__file__).resolve()

        def done():
            self._update_busy = False
            self._schedule_update_check(UPDATE_INTERVAL_MS)

        def worker():
            # Network + subprocess work off the Tk thread; results hop back
            # via root.after(0, ...) exactly like the usage poller does.
            try:
                tag = fetch_latest_version()
                latest, current = _parse_version(tag), _parse_version(__version__)
                if not latest or not current or latest <= current:
                    self.root.after(0, done)
                    return
                _update_log(f"found {tag} (running v{__version__}) — downloading")
                staged = stage_update(target)
                _update_log(f"{tag} passed sha256 + compile + selftest — restarting")
                self.root.after(0, lambda: self._restart_into(target, staged))
            except Exception as e:
                _update_log(f"check failed: {type(e).__name__}: {e}")
                try:
                    self.root.after(0, done)
                except Exception:
                    pass
        threading.Thread(target=worker, daemon=True).start()

    def _restart_into(self, target, staged):
        try:
            apply_update(target, staged)
        except Exception as e:
            _update_log(f"apply failed: {type(e).__name__}: {e}")
            self._update_busy = False
            self._schedule_update_check(UPDATE_INTERVAL_MS)
            return
        save_config(self.cfg)
        self._relaunch()

    def _toggle_auto_update(self):
        self.cfg["auto_update"] = bool(self.auto_update_var.get())
        save_config(self.cfg)
        pending = getattr(self, "_update_after_id", None)
        if pending is not None:
            try:
                self.root.after_cancel(pending)
            except Exception:
                pass
            self._update_after_id = None
        if self.cfg["auto_update"]:
            self._schedule_update_check(3000)

    def _set_mini_scale(self, scale):
        """Persist a new mini-mode size and redraw the battery strip live —
        no restart needed since _draw_mini resizes the canvas each call."""
        self.cfg["mini_scale"] = scale
        save_config(self.cfg)
        self._draw_mini()
        # A bigger mini strip can push past the monitor edge with no drag
        # involved; _draw_mini already schedules a clamp, this just makes
        # the intent explicit at the call site that changes the size.
        self.root.after(50, self._clamp_to_screen)

    def _reroll_pet(self):
        if not PETS_B64:
            return
        keys = list(PETS_B64.keys())
        current = self.cfg.get("pet")
        choices = [k for k in keys if k != current] or keys
        self.cfg["pet"] = random.choice(choices)
        save_config(self.cfg)
        photo = load_pet_photo(self.cfg["pet"], self._pet_size)
        if photo is not None:
            self.pet_photo = photo
            new_style = pet_style_for(self.cfg["pet"])
            if isinstance(self.pet_lbl, AnimatedPet):
                self.pet_lbl.update_pet(photo, new_style)
            else:
                self.pet_lbl.configure(image=self.pet_photo, text="")
                self.pet_lbl.image = self.pet_photo

    def _toggle_smart(self):
        self.cfg["smart_topmost"] = bool(self.smart_var.get())
        save_config(self.cfg)
        if self.cfg["smart_topmost"]:
            if self._foreground_check_id is None:
                self._foreground_check_id = self.root.after(100, self._check_topmost)
        else:
            if self._foreground_check_id is not None:
                try:
                    self.root.after_cancel(self._foreground_check_id)
                except Exception:
                    pass
                self._foreground_check_id = None
            self.root.attributes("-topmost", True)
            set_window_zorder(self._hwnd(), "top")
            self._current_z = "top"

    def _prompt_plan(self):
        # Empty value = revert to auto-detect from credentials
        self._prompt("plan_label", "플랜 이름 (빈칸 = 자동)", str,
                     on_save=lambda v: self.title_lbl.config(
                         text=f"Claude {(v.strip() if v else '') or detect_plan_label()}"))

    def _prompt_interval(self):
        self._prompt("refresh_seconds", "새로고침 간격 (초)", int)

    def _prompt(self, key, title, cast, on_save=None, default=None):
        t = self.theme
        dlg = tk.Toplevel(self.root)
        dlg.transient(self.root)  # no separate taskbar button
        dlg.title(title)
        dlg.attributes("-topmost", True)
        dlg.configure(bg=t["bg"])
        tk.Label(dlg, text=f"{title}:", bg=t["bg"], fg=t["fg"],
                  font=("Segoe UI", 9)).pack(padx=14, pady=(12, 4))
        var = tk.StringVar(value=str(self.cfg.get(key, default if default is not None else "")))
        entry = tk.Entry(dlg, textvariable=var, width=20)
        entry.pack(padx=14, pady=4)
        entry.focus_set()
        entry.select_range(0, "end")
        def ok():
            try:
                v = cast(var.get().strip())
                self.cfg[key] = v
                save_config(self.cfg)
                if on_save:
                    on_save(v)
            except (ValueError, TypeError):
                pass
            dlg.destroy()
        tk.Button(dlg, text="OK", command=ok, width=8).pack(pady=(4, 12))
        dlg.bind("<Return>", lambda _e: ok())
        dlg.bind("<Escape>", lambda _e: dlg.destroy())

    def _check_topmost(self):
        if not self.cfg.get("smart_topmost"):
            self._foreground_check_id = None
            return
        try:
            name, fg_pid = get_foreground_process()
            names = [n.lower() for n in self.cfg.get("claude_processes", [])]
            # Raise when the foreground is OUR window (matched by PID, not the
            # generic pythonw.exe name), a real Claude app (matched by name), or
            # a terminal host running the Claude CLI as a descendant process.
            if (fg_pid == os.getpid()
                    or (name and (name in names or "claude" in name))
                    or (name in _TERMINAL_NAMES and claude_runs_under(fg_pid))):
                if self._current_z != "top":
                    self.root.attributes("-topmost", True)
                    set_window_zorder(self._hwnd(), "top")
                    self._current_z = "top"
            else:
                if self._current_z != "bottom":
                    self.root.attributes("-topmost", False)
                    set_window_zorder(self._hwnd(), "bottom")
                    self._current_z = "bottom"
        finally:
            self._foreground_check_id = self.root.after(500, self._check_topmost)

    def _display_plan(self):
        """Return manual override if set, else auto-detected plan."""
        custom = (self.cfg.get("plan_label") or "").strip()
        return custom if custom else detect_plan_label()

    def refresh(self):
        if self.fetching:
            return
        # No cooldown enforcement — always attempt at the configured interval.
        # The server's Retry-After value is shown to the user for inspection
        # but does not gate the next call.
        self.fetching = True
        self.footer_lbl.config(text="새로고침 중…", fg=self.theme["dim"])
        def worker():
            data, err, retry = fetch_usage()
            self.root.after(0, lambda: self._render(data, err, retry))
        threading.Thread(target=worker, daemon=True).start()

    def _render(self, data, err, retry_after=0):
        self.fetching = False
        if err or not data:
            if err == "LOGIN_REQUIRED":
                self._consec_429 = 0
                self.footer_lbl.config(text="로그인 필요 · 클릭", fg=self.theme["danger"])
                self._prompt_login()
                return
            if err and ("429" in err or "rate limited" in err.lower()):
                self._consec_429 += 1
                if retry_after > 0:
                    from datetime import timedelta
                    retry_at = datetime.now() + timedelta(seconds=retry_after)
                    err = f"API limit · resets {retry_at.strftime('%H:%M')}"
                    self._schedule_unlock_refresh(retry_after)
                else:
                    err = "API limit"
            else:
                self._consec_429 = 0
            self.footer_lbl.config(text=err or "데이터 없음", fg=self.theme["danger"])
            # Keep the last good numbers on the taskbar strip, amber dot.
            self._taskbar_update(message=err or "데이터 없음", stale=True)
            return
        # Success path — cancel any pending unlock-refresh, nothing to retry.
        self._cancel_unlock_refresh()
        self._consec_429 = 0
        self._last_data = data
        fh = data.get("five_hour") or {}
        sd = data.get("seven_day") or {}
        ss = data.get("seven_day_sonnet") or {}
        eu = data.get("extra_usage") or {}
        s_pct = fh.get("utilization", 0) or 0
        self.session_pct_lbl.config(text=f"현재 세션  {s_pct:.0f}% 사용됨")
        self._draw_bar(self.session_canvas, s_pct)
        s_reset = parse_iso(fh.get("resets_at"))
        rem = fmt_remaining(s_reset)
        reset_local = fmt_reset_local(s_reset)
        self.session_detail_lbl.config(
            text=f"{rem}  ({reset_local} 리셋)" if reset_local else rem
        )
        w_pct = sd.get("utilization", 0) or 0
        self.weekly_pct_lbl.config(text=f"주간 한도  {w_pct:.0f}% 사용됨")
        self._draw_bar(self.weekly_canvas, w_pct)
        w_reset = parse_iso(sd.get("resets_at"))
        self.weekly_detail_lbl.config(
            text=f"{fmt_reset_local(w_reset)} 리셋" if w_reset else ""
        )
        # Third row: model-scoped weekly limit. The API moved from fixed
        # per-model fields (seven_day_sonnet, now null) to a generic `limits`
        # array whose weekly_scoped entry names the model it applies to
        # (e.g. display_name "Fable"). Prefer that; fall back to the legacy
        # field for older API responses.
        ss_label, ss_pct = "Sonnet", ss.get("utilization", 0) or 0
        for lim in (data.get("limits") or []):
            if lim.get("kind") == "weekly_scoped":
                model = ((lim.get("scope") or {}).get("model") or {})
                ss_label = model.get("display_name") or ss_label
                ss_pct = lim.get("percent", 0) or 0
                break
        self.sonnet_pct_lbl.config(text=f"{ss_label} 주간  {ss_pct:.0f}% 사용됨")
        self._draw_bar(self.sonnet_canvas, ss_pct)
        plan = self._display_plan()
        if eu.get("is_enabled"):
            plan += " +Extra"
        self.title_lbl.config(text=f"Claude {plan}")
        self.footer_lbl.config(
            text=f"업데이트 {datetime.now().strftime('%H:%M:%S')}  ·  우클릭=메뉴",
            fg=self.theme["muted"],
        )
        # keep the battery strip in sync
        self._mini_pcts = (s_pct, w_pct, ss_pct)
        self._draw_mini()
        # taskbar strip shows only the 5h session and the weekly limit
        self._taskbar_update(s_pct, w_pct)

    def _schedule_refresh(self):
        # Always schedule at the configured interval — no backoff, no
        # Retry-After enforcement (user is empirically observing the limit).
        interval = self.cfg["refresh_seconds"]
        self.root.after(interval * 1000, self._tick)

    def _schedule_unlock_refresh(self, seconds_until_unlock):
        """One-shot refresh fired the moment the rate limit window resets.
        +5s buffer so the server has time to clear state."""
        self._cancel_unlock_refresh()
        delay_ms = max(1000, (seconds_until_unlock + 5) * 1000)
        self._unlock_refresh_id = self.root.after(delay_ms, self._do_unlock_refresh)

    def _cancel_unlock_refresh(self):
        if getattr(self, "_unlock_refresh_id", None) is not None:
            try:
                self.root.after_cancel(self._unlock_refresh_id)
            except Exception:
                pass
            self._unlock_refresh_id = None

    def _do_unlock_refresh(self):
        self._unlock_refresh_id = None
        self.refresh()

    def _tick(self):
        self.refresh()
        self._schedule_refresh()

    # ----- Login prompt -----

    def _prompt_login(self):
        """Pop a small dialog when the refresh token is dead. The user clicks
        '로그인' and we open a console running `claude auth login`. Shown at most
        once until dismissed/resolved so the poll loop doesn't spam it."""
        if getattr(self, "_login_dialog", None) is not None:
            try:
                if self._login_dialog.winfo_exists():
                    return  # already open
            except Exception:
                pass
        t = self.theme
        dlg = tk.Toplevel(self.root)
        dlg.transient(self.root)  # no separate taskbar button
        self._login_dialog = dlg
        dlg.title("Claude 로그인")
        dlg.configure(bg=t["bg"])
        dlg.attributes("-topmost", True)
        dlg.resizable(False, False)

        tk.Label(dlg, text="로그인이 만료되었습니다",
                 bg=t["bg"], fg=t["fg"],
                 font=("Segoe UI", 10, "bold")).pack(padx=18, pady=(16, 4))
        tk.Label(dlg,
                 text="아래 버튼을 누르면 로그인 창이 열립니다.\n브라우저에서 로그인하면 위젯이 다시 작동합니다.",
                 bg=t["bg"], fg=t["dim"], font=("Segoe UI", 8),
                 justify="center").pack(padx=18, pady=(0, 12))

        btn_row = tk.Frame(dlg, bg=t["bg"])
        btn_row.pack(pady=(0, 16))

        def do_login():
            ok = launch_claude_login()
            dlg.destroy()
            self._login_dialog = None
            if ok:
                self.footer_lbl.config(text="로그인 창에서 진행하세요…", fg=t["dim"])
                self._auth_retry_after_login()
            else:
                self.footer_lbl.config(
                    text="claude 실행 못 찾음 · 수동 claude auth login",
                    fg=t["danger"])

        def later():
            dlg.destroy()
            self._login_dialog = None

        tk.Button(btn_row, text="로그인", command=do_login,
                  bg=t["accent"], fg="white", relief="flat",
                  font=("Segoe UI", 9, "bold"), padx=18, pady=4,
                  cursor="hand2", activebackground=t["accent"],
                  activeforeground="white").pack(side="left", padx=4)
        tk.Button(btn_row, text="나중에", command=later,
                  bg=t["bar_bg"], fg=t["fg"], relief="flat",
                  font=("Segoe UI", 9), padx=14, pady=4,
                  cursor="hand2").pack(side="left", padx=4)

        # Center over the widget
        dlg.update_idletasks()
        wx = self.root.winfo_x() + self.root.winfo_width() // 2 - dlg.winfo_width() // 2
        wy = self.root.winfo_y() + 40
        dlg.geometry(f"+{max(0, wx)}+{max(0, wy)}")

    def _auth_retry_after_login(self):
        """After launching login, poll the credentials file for a fresh token
        and refresh the widget once it lands (up to ~3 min)."""
        global _auth_fail_streak
        attempts = getattr(self, "_login_poll_attempts", 0)
        if attempts > 36:  # 36 * 5s = 3 min give-up
            self._login_poll_attempts = 0
            return
        self._login_poll_attempts = attempts + 1
        token, expires_at = read_token()
        now_ms = datetime.now(timezone.utc).timestamp() * 1000
        if token and expires_at and expires_at > now_ms:
            # fresh token landed
            self._login_poll_attempts = 0
            _auth_fail_streak = 0
            self.refresh()
            return
        self.root.after(5000, self._auth_retry_after_login)

    # ----- Tray -----

    def _setup_tray(self):
        if not HAS_PYSTRAY:
            self.tray_icon = None
            return
        try:
            raw = base64.b64decode(TRAY_ICON_B64)
            img = Image.open(io.BytesIO(raw))
            menu = pystray.Menu(
                pystray.MenuItem("Show / Hide", self._tray_toggle, default=True),
                pystray.MenuItem("Refresh now",
                                  lambda: self.root.after(0, self.refresh)),
                pystray.Menu.SEPARATOR,
                pystray.MenuItem("Quit",
                                  lambda: self.root.after(0, self.quit)),
            )
            self.tray_icon = pystray.Icon("claude-usage-widget", img,
                                           "Claude Usage Widget", menu)
            threading.Thread(target=self.tray_icon.run, daemon=True).start()
        except Exception:
            self.tray_icon = None

    # ----- Taskbar strip -----

    def _setup_taskbar(self):
        """Bring up the embedded taskbar surface, or silently do without it."""
        if sys.platform != "win32" or not _TB_HAS_COMTYPES:
            return
        try:
            surface = TaskbarSurface(
                lambda x, y: self._taskbar_events.put(("left", x, y)),
                lambda x, y: self._taskbar_events.put(("right", x, y)))
            if not surface.start():
                surface.stop()
                return
        except Exception:
            return
        self.taskbar = surface
        surface.set_visible(bool(self.cfg.get("taskbar_visible", True)))
        self.root.after(120, self._taskbar_pump)

    def _taskbar_pump(self):
        """Drain clicks from the native thread onto the Tcl thread.

        tkinter calls are not safe from another thread, so the window proc
        only enqueues and this poll does the actual UI work."""
        while True:
            try:
                kind, x, y = self._taskbar_events.get_nowait()
            except queue.Empty:
                break
            try:
                if kind == "left":
                    self._toggle_visibility()
                else:
                    self._popup_menu(x, y)
            except Exception:
                pass
        self.root.after(120, self._taskbar_pump)

    def _taskbar_update(self, session_pct=None, weekly_pct=None,
                        message=None, stale=False):
        """Push new values, or flag the existing ones stale on a failed poll."""
        if self.taskbar is None:
            return
        if session_pct is None or weekly_pct is None:
            rows = getattr(self, "_taskbar_rows", ())
        else:
            rows = (("5h", max(0.0, min(100.0, 100.0 - session_pct))),
                    ("주간", max(0.0, min(100.0, 100.0 - weekly_pct))))
            self._taskbar_rows = rows
        try:
            self.taskbar.update(rows, message or "불러오는 중", stale)
        except Exception:
            pass

    def _toggle_taskbar(self):
        self.cfg["taskbar_visible"] = bool(self.taskbar_var.get())
        save_config(self.cfg)
        if self.taskbar is not None:
            self.taskbar.set_visible(self.cfg["taskbar_visible"])

    def _tray_toggle(self):
        self.root.after(0, self._toggle_visibility)

    def _toggle_visibility(self):
        if self._visible:
            self.hide_to_tray()
        else:
            self.show_widget()

    def hide_to_tray(self):
        save_config(self.cfg)
        self.root.withdraw()
        self._visible = False

    def show_widget(self):
        self.root.deiconify()
        self._visible = True
        self.root.attributes("-topmost", True)
        set_window_zorder(self._hwnd(), "top")
        self._current_z = "top"
        # deiconify is the main trigger for Windows re-adding a taskbar button
        hide_from_taskbar(self._hwnd())
        # The screen setup (monitor count/resolution) may have changed while
        # hidden in the tray; re-clamp once the restored window is laid out.
        self.root.after(50, self._clamp_to_screen)

    def quit(self):
        save_config(self.cfg)
        if getattr(self, "taskbar", None):
            try:
                self.taskbar.stop()
            except Exception:
                pass
        if getattr(self, "tray_icon", None):
            try:
                self.tray_icon.stop()
            except Exception:
                pass
        try:
            self.root.destroy()
        except Exception:
            pass

    def run(self):
        self._schedule_update_check(UPDATE_FIRST_CHECK_MS)
        self.root.mainloop()


if __name__ == "__main__":
    if "--selftest" in sys.argv[1:]:
        # Used by the auto-updater (and the release workflow): reaching this
        # line in a fresh interpreter proves every module-level statement —
        # imports, ctypes setup, DPI call — executed without error. No window,
        # no mutex, no config touched. _tb_selftest additionally pins the
        # taskbar placement preferences (pure geometry, no Win32 calls).
        _tb_selftest()
        sys.exit(0)
    # Exit silently if another instance already owns the singleton mutex
    # (e.g. the SessionStart hook fired while the widget was already running).
    if acquire_single_instance():
        Widget().run()
