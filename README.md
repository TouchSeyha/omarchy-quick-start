<img src="assets/icon.svg" width="64" height="64" alt="">

# Quick-Start

An [Omarchy](https://omarchy.org) bar widget that saves your open Hyprland windows into named sessions (`Work`, `Personal`, …) and restores one in a click: apps reopen on their saved workspaces and monitors, with each workspace's layout mode, floating geometry, scrolling column widths, and dwindle split ratios.

Quick-Start only works when you ask it to. It has no background service, timers, or auto-save, so it costs no CPU or battery while idle.

## Features

- **Named sessions**: save the current desktop under a name, then **Restore**, **Update** (overwrite with the current desktop), **Rename**, or **Delete** it.
- **Preview and prune**: expand a session to see its windows grouped by workspace, remove stray windows with `×`, or edit a window's launch command.
- **Reboot-safe launch commands**, worked out when you save:
  - Native apps launch from their `.desktop` entry.
  - AppImages launch from their real file, never the temporary `/tmp/.mount_*` path.
  - Single-instance apps (Zed's `zed-cli://` socket, for example) launch from their desktop entry, so their own project restore runs.
  - Chromium web apps, which share one browser process, are told apart by window class and reopen with `omarchy-launch-webapp`.
  - Terminals (Ghostty, Alacritty, Kitty, Foot) reopen in their shell's working directory. The program that was running in them is not started again.
  - Windows with no launch command are flagged in the preview, so you can set one or remove the window right away instead of finding out the next morning.
- **Smart merge restore**: windows that are already open are reused and moved into place, and missing apps are launched. Unrelated windows are never closed. Windows that are already in the right place are left alone.
- **Layout fidelity**: each workspace's layout mode (`dwindle`, `scrolling`, `floating`), exact floating position and size (pinned windows are pinned again), scrolling column order and widths, and dwindle split arrangement and ratios. The dwindle tree is rebuilt from the saved window rectangles.
- **Monitors found by screen, not port**: each monitor is saved with its make and model, so the same screen is recognised on any port or cable (`DP-2` today, `HDMI-A-1` tomorrow), and the panel shows it as `Built-in` or its model name. A screen that isn't connected falls back to the focused monitor: its workspaces move there, floating windows are scaled to fit, and the panel says so.
- **Private preview**: blur window titles and websites in the session preview with the eye button (or <kbd>b</kbd>, or the plugin setting). Hover a window to read it.
- **Quiet and cancellable**: apps launch silently into their own workspaces. New windows are tracked through Hyprland's event socket, and progress such as `6/9` shows on the bar icon. You can cancel at any time. Any app that hasn't opened within 15 seconds is reported without blocking the rest. When everything has settled, the saved workspaces are shown again and the window that had focus gets it back.
- **Filters**: transient portals, auth prompts, picture-in-picture windows, and the screensaver are never saved. Special workspaces (scratchpad and similar) are skipped unless you turn them on, and you can keep a list of app classes to always ignore.

## Requirements

- Omarchy with Hyprland 0.56 or newer (Lua config)
- `python3` (standard library only)

## Install

```bash
omarchy plugin add https://github.com/TouchSeyha/omarchy-quick-start.git --enable
```

The widget is added to the right side of the bar. To move it:

```bash
omarchy bar move touchseyha.quick-start --section right
```

## Usage

Click the bar icon to open the panel. Type a name and press <kbd>Enter</kbd> (or **Save**) to save the current desktop.

When the panel is open:

| Key | Action |
| --- | --- |
| <kbd>↑</kbd>/<kbd>↓</kbd>, <kbd>j</kbd>/<kbd>k</kbd> | Select a session |
| <kbd>Enter</kbd> / <kbd>Space</kbd> | Restore the selected session |
| <kbd>s</kbd> | Focus the name field |
| <kbd>e</kbd> | Show or hide the selected session's windows |
| <kbd>u</kbd> | Update the selected session with the current desktop |
| <kbd>r</kbd> | Rename the selected session |
| <kbd>x</kbd> / <kbd>Delete</kbd> | Delete the selected session (press twice) |
| <kbd>c</kbd> | Cancel a running restore |
| <kbd>b</kbd> | Blur or show window titles and websites |
| <kbd>?</kbd> / <kbd>i</kbd> | Show or hide how Quick-Start works |
| <kbd>Esc</kbd> | Close |

Middle-click the bar icon to cancel a running restore.

### IPC and hotkeys

```bash
omarchy-shell touchseyha.quick-start toggle        # open/close the panel
omarchy-shell touchseyha.quick-start restore Work  # restore a session by name
omarchy-shell touchseyha.quick-start cancel        # cancel a running restore
```

For example, to bind the panel to a key, add this to `~/.config/hypr/bindings.lua`:

```lua
o.bind("SUPER + ALT + R", "Quick-Start sessions", "omarchy-shell touchseyha.quick-start toggle")
```

## Configuration

These optional settings go on the widget's entry in `~/.config/omarchy/shell.json`:

```json
{
  "id": "touchseyha.quick-start",
  "includeSpecialWorkspaces": false,
  "ignoreClasses": "com.onepassword.OnePassword, steam*"
}
```

| Option | Default | Description |
| --- | --- | --- |
| `includeSpecialWorkspaces` | `false` | Also save windows on special workspaces such as the scratchpad |
| `ignoreClasses` | `""` | Comma-separated app classes that are never saved (`*` wildcards allowed) |

## Data

Sessions are human-readable JSON, one file per session:

```
~/.local/state/omarchy/quick-start/slots/<id>.json
```

(`$XDG_STATE_HOME` is respected.) A session file holds the commands it will run on restore, so only restore session files you wrote yourself. Restoring a layout mode also records it in Omarchy's `~/.local/state/omarchy/workspace-layouts/` the way the layout toggle does, so it lasts past the next login.

## Remove

```bash
omarchy plugin remove touchseyha.quick-start
rm -rf ~/.local/state/omarchy/quick-start   # optional: delete saved sessions
```

## Development

```bash
python3 -m unittest discover -s tests
node --test tests/
omarchy plugin validate .
```

If you work from a clone that is symlinked into `~/.config/omarchy/plugins/`, run `omarchy restart shell` after editing. The plugin watcher doesn't follow symlinks, so edits there don't hot-reload.

## License

MIT
