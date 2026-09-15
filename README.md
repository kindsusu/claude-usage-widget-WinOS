# Claude Usage Widget

📖 [한국어 사용설명서](docs/사용설명서.md)

Always-on-top Windows desktop widget showing real-time Claude Max plan usage. Matches `claude.ai/settings/usage` exactly — pulls the same numbers via the official OAuth endpoint.

![widget preview](docs/img/full-light.png)

## Features

- **Real-time usage** — 5-hour session, 7-day total, Sonnet-only weekly
- **Light / Dark theme** toggle (☾/☀ button)
- **Transparency** slider popup (30–100%)
- **Smart top-most** — floats above only when Claude (or the widget itself) is the active window; recedes when you switch to other apps
- **Multi-monitor** aware — drag to any screen
- **31 pixel pets** with whole-body animations (bounce/sway/float/squish/breathe), random assignment on first run
- **System tray** — X button minimizes to tray; left-click tray to toggle, right-click for menu
- **Independent display modes** — the right-click menu selects exactly one desktop state under “데스크톱 위젯” (normal, mini, or hidden), while “작업표시줄 표시” independently shows or hides the taskbar strip. The same menu and state are used from either surface and survive restart.
- **Taskbar strip** — a two-row 5h / weekly readout embedded in the real Windows taskbar (mark on the left, remaining % on the right). Left-click opens a compact display panel — pick exactly one of Normal / Mini / Hidden plus an independent "작업표시줄 표시" checkbox; right-click opens the same advanced menu as the desktop widget.

![taskbar strip](docs/img/taskbar-strip.png)

- **Taskbar strip placement** — right-click → **작업표시줄 위치** jumps straight to one of four spots: primary/secondary monitor × left/right end (a checkmark shows the current one; the secondary rows are disabled with no secondary taskbar). You can also drag the strip: drop it on a taskbar's left half to join the left group (measured from the real leading edge) or the right half for the right group (just before the notification area); drop it past a sibling strip in the same group — such as the Codex widget's — to swap places, and that strip steps aside within a second or two. A near-zero-distance press is still a click, not a drag, and Esc cancels a drag in progress. If the saved secondary monitor is unplugged, the strip temporarily borrows the primary taskbar without changing the saved setting, and the tray status reads "선택한 작업표시줄 없음 · 주 작업표시줄 사용 중" until it reconnects. It is skipped silently on a vertical taskbar, without `comtypes`, or when the taskbar has no free space.
- **Gradient bars** — smooth green → yellow → red as usage climbs
- **Single file** — `widget.pyw` is fully self-contained (~280 KB with embedded pet sprites)
- **Auto-update** — checks GitHub Releases on launch and every 12 h; a new version is verified (sha256 + syntax + self-test) before it replaces itself, and the previous file is kept as `widget.pyw.bak`

## Requirements

- Windows 10/11
- Python 3.8+
- [Claude Code](https://claude.com/code) installed and logged in (`claude login`)
- `pip install pillow pystray comtypes`
  - `comtypes` is only needed for the taskbar strip (it locates the real taskbar buttons via UI Automation). Without it the desktop widget still works; the taskbar strip is silently skipped.

The widget reads the OAuth token Claude Code stores in `~/.claude/.credentials.json` — no manual API key or cookie handling needed. It also **auto-refreshes the token** when it expires, so it keeps working without running Claude Code (see [Token handling](#token-handling-automatic)).

## Install

```bash
pip install pillow pystray comtypes
```

Download the latest `widget.pyw` (this link always points at the newest release):

```
https://github.com/kindsusu/claude-usage-widget-WinOS/releases/latest/download/widget.pyw
```

Put it in a folder of its own (it writes `widget_config.json` beside itself), then double-click `실행.bat` from this repo or run directly:

```bash
pythonw widget.pyw
```

Cloning the repo works too, but note that auto-update overwrites `widget.pyw` in place — run `git checkout -- widget.pyw` before a `git pull` if git complains.

### Auto-start on boot

Create a shortcut to `widget.pyw` (target: `pythonw.exe "...\widget.pyw"`) and drop it into your Startup folder:

```
Win+R → shell:startup → paste shortcut
```

## Auto-update

The widget keeps itself current without any action from you:

1. 20 s after launch, and every 12 h after that, it resolves `…/releases/latest` and compares the tag with its own `__version__` (no GitHub API call, so no rate limit).
2. If newer, it downloads `widget.pyw` and `widget.pyw.sha256` from that release.
3. Three gates must all pass before anything changes on disk: the sha256 matches, the file compiles, and a fresh `pythonw widget.pyw.new --selftest` exits 0 (every import and module-level statement ran).
4. The current file is copied to `widget.pyw.bak`, the new one is moved into place atomically, and the widget restarts itself — you see it blink once.

Every step is written to `widget.log` next to the script. Turn it off with **자동 업데이트** in the right-click menu (`"auto_update": false` in `widget_config.json`). To roll back, rename `widget.pyw.bak` to `widget.pyw`.

Only published Releases reach users; commits to `main` do not.

### Publishing a release (maintainer)

```bash
# 1. bump __version__ in widget.pyw (e.g. "1.3.0"), commit, push
# 2. tag — the tag must equal __version__ or the workflow refuses
git tag v1.3.0
git push --tags
```

[`release.yml`](.github/workflows/release.yml) then runs `py_compile` + `--selftest`, computes the sha256, and creates the Release with both assets. Users pick it up within 12 h (or on their next launch).

## How it works

The widget calls Anthropic's official OAuth usage endpoint directly:

```
GET https://api.anthropic.com/api/oauth/usage
Authorization: Bearer <token from ~/.claude/.credentials.json>
anthropic-beta: oauth-2025-04-20
```

Response shape:

```json
{
  "five_hour":        { "utilization": 18.0, "resets_at": "..." },
  "seven_day":        { "utilization": 7.0,  "resets_at": "..." },
  "seven_day_sonnet": { "utilization": 0.0,  "resets_at": "..." },
  "extra_usage":      { "is_enabled": false }
}
```

This is the same data `claude.ai/settings/usage` displays. The endpoint is undocumented but stable; it's what the [official Claude Code statusline payload](https://docs.claude.com/en/docs/claude-code/statusline) exposes via `rate_limits`.

## Configuration

Settings live in `widget_config.json` (auto-generated next to `widget.pyw`):

| Key | Default | Notes |
|---|---|---|
| `refresh_seconds` | 180 | How often to poll the API |
| `theme` | `light` | `light` or `dark` |
| `alpha` | 0.95 | Window transparency (0.3–1.0) |
| `smart_topmost` | true | Float only when Claude is active |
| `plan_label` | `Max` | Shown in title bar |
| `pet` | (random) | Key into the embedded pet sprites |
| `x`, `y` | 100, 100 | Window position |
| `taskbar_zone` | `left` | `left` or `right` end of the host taskbar |
| `taskbar_host` | `primary` | `primary` or `secondary` taskbar window |
| `taskbar_host_monitor` | `` (empty) | Device name of the chosen secondary monitor |

Right-click the widget for the full menu (refresh, theme, pet reroll, settings, quit).

## Pet system

31 pixel-art pets are embedded as base64 inside `widget.pyw`. On first run, one is randomly assigned and persisted in `widget_config.json`. To reroll, use the right-click menu → "펫 다시 뽑기".

Each pet gets a deterministic animation style (bounce, sway, float, squish, or breathe) based on its name — same pet always animates the same way.

## System tray

Click the **X** on the widget to minimize it to the system tray (it does **not** quit — the process keeps running so the widget can be brought back instantly).

- **Tray icon left-click** → toggle widget visibility
- **Tray icon right-click** → menu (Show/Hide, Refresh now, Quit)
- The only way to fully quit is the **Quit** item in the tray menu

## Token handling (automatic)

The widget refreshes its own access token, so you normally **never** have to touch it:

- The OAuth **access token** lives ~8 hours. When it expires, the widget POSTs the stored **refresh token** to `platform.claude.com/v1/oauth/token` (public Claude Code `client_id`) and writes the rotated tokens back to `~/.claude/.credentials.json`.
- This works even if **Claude Code isn't running** and even after the **computer was powered off for days/weeks** — on next launch the widget just mints a fresh token from the refresh token.
- The token refresh endpoint is **separate** from the usage endpoint's rate limit, so auto-refresh never contributes to the `API limit` 429s.
- Refresh-token **rotation** is handled correctly (the new refresh token from each response is persisted).

### When you *do* need to act

The only time manual action is needed is if the **refresh token itself** becomes invalid — which is rare (lasts months to a year):

- Very long inactivity (months)
- Password change / sign-out elsewhere / Anthropic security revocation

Then the footer shows **"토큰 갱신 실패 · Claude Code에서 /login"** — just run `claude login` (or `/login` inside Claude Code) once and the widget recovers on the next poll.

## For AI agents (Claude Code, etc.)

If you're an AI assistant installing this on behalf of a user, follow these steps in order. Each step has a verification command — stop and tell the user if a step fails.

### Prerequisites (verify, do not auto-install)

```bash
# 1. Windows? (this widget is Windows-only — Win32 APIs)
ver   # should show "Microsoft Windows"

# 2. Python 3.8+
python --version

# 2b. Required Python packages
pip install pillow pystray comtypes

# 3. Claude Code installed and logged in
test -f "$USERPROFILE/.claude/.credentials.json" && python -c "
import json
d = json.load(open(r'$USERPROFILE/.claude/.credentials.json'))
print('logged in:', 'claudeAiOauth' in d and bool(d['claudeAiOauth'].get('accessToken')))
"
```

If any prerequisite is missing, instruct the user:
- No Python → install from https://www.python.org/downloads/ with "Add Python to PATH" checked
- No Claude Code → `npm install -g @anthropic-ai/claude-code`
- Not logged in → user must run `claude login` themselves (browser OAuth, can't be automated)

### Install

```bash
# Pick a stable install location — user's home or Desktop
INSTALL_DIR="$HOME/claude-usage-widget"

# Clone
git clone https://github.com/kindsusu/claude-usage-widget.git "$INSTALL_DIR"

# Install Python deps
pip install pillow pystray comtypes

# Verify imports work
python -c "from PIL import Image, ImageTk; import pystray, comtypes; print('deps OK')"
```

### Launch

```bash
# Use cmd //c on Git Bash to avoid bash interpreting Windows paths
cmd //c start "" pythonw "$INSTALL_DIR/widget.pyw"

# Verify it stayed alive after 2 seconds
sleep 2 && tasklist | grep -i pythonw
```

If `pythonw` is not in PATH, use the full path: `C:/Users/<user>/AppData/Local/Programs/Python/Python3xx/pythonw.exe`.

### Autostart on boot (optional)

```powershell
$lnk = "$env:APPDATA\Microsoft\Windows\Start Menu\Programs\Startup\Claude Usage Widget.lnk"
$pythonw = (Get-Command pythonw).Source
$widget = "$env:USERPROFILE\claude-usage-widget\widget.pyw"
$sh = New-Object -ComObject WScript.Shell
$shortcut = $sh.CreateShortcut($lnk)
$shortcut.TargetPath = $pythonw
$shortcut.Arguments = "`"$widget`""
$shortcut.WorkingDirectory = Split-Path $widget -Parent
$shortcut.Save()
```

### Common issues

| Symptom | Likely cause | Fix |
|---|---|---|
| Widget process starts but no window visible | `widget_config.json` has off-screen `x`/`y` | Delete `widget_config.json` and relaunch |
| Pet (image) doesn't show | Old PIL or wrong widget version | `pip install --upgrade pillow`, ensure widget.pyw is fresh from this repo |
| Footer shows "토큰 갱신 실패" | refresh token invalid (rare) | User runs `claude login` once |
| Footer shows "API rate limited" | Too many polls in short time | Wait 5–10 min; widget auto-backs-off |
| Title bar shows `●` instead of pet | `ImageTk.PhotoImage` called before `tk.Tk()` | Bug in older version — pull latest |

### Do NOT

- Do NOT modify `~/.claude/.credentials.json` (Claude Code owns it)
- Do NOT bundle your own PETS_B64 unless rebuilding from `pets/` source images
- Do NOT change the API endpoint or headers — `anthropic-beta: oauth-2025-04-20` is required

## License

MIT — see [LICENSE](LICENSE).
