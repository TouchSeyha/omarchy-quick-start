#!/usr/bin/env python3
"""Quick-Start backend: save Hyprland desktops into named slots and restore them.

Standard library only. Every command prints one JSON object to stdout, except
`restore`, which streams one JSON object per line while it runs. The QML side
treats everything printed here as untrusted data.

Slots live as human-readable JSON under
$XDG_STATE_HOME/omarchy/quick-start/slots/<id>.json (default ~/.local/state).
"""

import argparse
import configparser
import fcntl
import fnmatch
import json
import os
import re
import select
import shlex
import shutil
import signal
import socket
import sys
import tempfile
import time

SCHEMA_VERSION = 1
MAX_SLOTS = 64
MAX_WINDOWS = 128
MAX_NAME = 64
MAX_TITLE = 256
MAX_CLASS = 128
MAX_COMMAND_ARGS = 64
MAX_COMMAND_LEN = 4096
MAP_TIMEOUT = 15.0
SETTLE_SECONDS = 0.8
SETTLE_LIMIT = 5.0
STAGING_WORKSPACE = "special:quick-start-staging"
DWINDLE_EDGE = 48

TERMINALS = {
    # binary: (default class, cwd flag builder, class flag builder)
    "ghostty": ("com.mitchellh.ghostty", lambda d: ["--working-directory=" + d], lambda c: ["--class=" + c]),
    "alacritty": ("Alacritty", lambda d: ["--working-directory", d], lambda c: ["--class", c]),
    "kitty": ("kitty", lambda d: ["--directory", d], lambda c: ["--class", c]),
    "foot": ("foot", lambda d: ["--working-directory=" + d], lambda c: ["--app-id=" + c]),
}

# Transient system surfaces nobody wants reopened.
NOISE_CLASSES = [
    "xdg-desktop-portal*",
    "org.freedesktop.impl.portal.*",
    "*polkit*",
    "gcr-prompter",
    "pinentry*",
    "org.omarchy.screensaver",
    "hyprland-share-picker",
    "hyprpolkitagent",
    "org.kde.polkit-kde-authentication-agent-1",
    "nm-openconnect-auth-dialog",
    "zenity",
]
NOISE_TITLES = ["Picture-in-Picture", "Picture in picture", "Picture in Picture"]

WEBAPP_CLASS = re.compile(r"^(chrome|brave|msedge|vivaldi|chromium|helium)-(?P<app>.+)-(?P<profile>[^-]+)$")
FIELD_CODE = re.compile(r"%[fFuUdDnNickvm]")


class QuickStartError(Exception):
    pass


# ---------------------------------------------------------------- utilities


def clip(value, limit):
    text = "" if value is None else str(value)
    text = "".join(ch for ch in text if ch == "\t" or ord(ch) >= 32 and ord(ch) != 127)
    return text[:limit]


def clean_name(name):
    name = clip(name, 512).strip()
    name = re.sub(r"\s+", " ", name)
    if not name:
        raise QuickStartError("Session name is empty")
    return name[:MAX_NAME]


def slugify(name):
    slug = re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")
    return (slug or "session")[:40]


def lua_str(value):
    """Quote any string as a Lua string literal; Lua strings are bytes."""
    out = ['"']
    for byte in str(value).encode("utf-8"):
        ch = chr(byte)
        if ch == "\\":
            out.append("\\\\")
        elif ch == '"':
            out.append('\\"')
        elif ch == "\n":
            out.append("\\n")
        elif byte < 32 or byte == 127:
            out.append("\\%03d" % byte)
        elif byte >= 128:
            out.append("\\%03d" % byte)
        else:
            out.append(ch)
    out.append('"')
    return "".join(out)


def state_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy", "quick-start")


def omarchy_modes_dir():
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "omarchy", "workspace-layouts")


def write_json_atomic(path, data):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(prefix=".tmp-", dir=os.path.dirname(path))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(data, handle, indent=2, ensure_ascii=False)
            handle.write("\n")
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def parse_ignore(value):
    if isinstance(value, list):
        items = value
    else:
        items = re.split(r"[,\n]", str(value or ""))
    return [clip(item, MAX_CLASS).strip() for item in items if clip(item, MAX_CLASS).strip()][:64]


def matches_any(value, patterns):
    value = (value or "").lower()
    return any(fnmatch.fnmatchcase(value, pattern.lower()) for pattern in patterns)


# ---------------------------------------------------------------- hyprland IPC


class Hyprland:
    def __init__(self):
        runtime = os.environ.get("XDG_RUNTIME_DIR", "")
        signature = os.environ.get("HYPRLAND_INSTANCE_SIGNATURE", "")
        if not runtime or not signature:
            raise QuickStartError("Hyprland is not running")
        self.base = os.path.join(runtime, "hypr", signature)

    def request(self, command):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(5)
        try:
            sock.connect(os.path.join(self.base, ".socket.sock"))
            sock.sendall(command.encode("utf-8"))
            chunks = []
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                chunks.append(chunk)
        finally:
            sock.close()
        return b"".join(chunks).decode("utf-8", "replace")

    def query(self, what):
        raw = self.request("j/" + what)
        try:
            return json.loads(raw)
        except ValueError:
            raise QuickStartError("Hyprland returned unreadable %s" % what)

    def clients(self):
        return self.query("clients")

    def workspaces(self):
        return self.query("workspaces")

    def monitors(self):
        return self.query("monitors")

    def eval(self, lua):
        return self.request("eval " + lua).strip()

    def dispatch_all(self, calls):
        """Run several hl.dsp calls in one round trip."""
        if not calls:
            return "ok"
        return self.eval(" ".join("hl.dispatch(%s);" % call for call in calls))

    def events(self):
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.connect(os.path.join(self.base, ".socket2.sock"))
        sock.setblocking(False)
        return sock


# ---------------------------------------------------------------- desktop entries


def data_dirs():
    home = os.environ.get("XDG_DATA_HOME") or os.path.join(os.path.expanduser("~"), ".local", "share")
    dirs = [home] + (os.environ.get("XDG_DATA_DIRS") or "/usr/local/share:/usr/share").split(":")
    seen, out = set(), []
    for item in dirs:
        if item and item not in seen:
            seen.add(item)
            out.append(item)
    return out


def exec_to_argv(exec_line):
    try:
        tokens = shlex.split(exec_line)
    except ValueError:
        return []
    argv = []
    for token in tokens:
        if FIELD_CODE.fullmatch(token):
            continue
        if FIELD_CODE.search(token) or re.fullmatch(r"@@[a-zA-Z]?", token):
            continue
        argv.append(token.replace("%%", "%"))
    return argv


class DesktopIndex:
    def __init__(self, dirs=None):
        self.by_wmclass = {}
        self.by_id = {}
        self.entries = []
        for base in dirs if dirs is not None else data_dirs():
            root = os.path.join(base, "applications")
            if not os.path.isdir(root):
                continue
            for folder, _subdirs, files in os.walk(root):
                for filename in sorted(files):
                    if filename.endswith(".desktop"):
                        self._load(root, os.path.join(folder, filename))

    def _load(self, root, path):
        desktop_id = os.path.relpath(path, root).replace(os.sep, "-")[: -len(".desktop")]
        if desktop_id.lower() in self.by_id:
            return  # earlier data dirs win, like the spec says
        parser = configparser.RawConfigParser(strict=False, interpolation=None)
        parser.optionxform = str
        try:
            with open(path, encoding="utf-8", errors="replace") as handle:
                parser.read_file(handle)
        except (OSError, configparser.Error):
            return
        if not parser.has_section("Desktop Entry"):
            return
        section = parser["Desktop Entry"]
        if section.get("Hidden", "").lower() == "true":
            return
        argv = exec_to_argv(section.get("Exec", ""))
        if not argv:
            return
        entry = {
            "id": desktop_id,
            "name": section.get("Name", desktop_id),
            "argv": argv,
            "wmclass": section.get("StartupWMClass", ""),
        }
        self.entries.append(entry)
        self.by_id[desktop_id.lower()] = entry
        if entry["wmclass"]:
            self.by_wmclass.setdefault(entry["wmclass"].lower(), entry)

    def lookup(self, *classes):
        for cls in classes:
            if cls and cls.lower() in self.by_wmclass:
                return self.by_wmclass[cls.lower()]
        for cls in classes:
            if cls and cls.lower() in self.by_id:
                return self.by_id[cls.lower()]
        return None

    def lookup_binary(self, names):
        wanted = {os.path.basename(name) for name in names if name}
        for entry in self.entries:
            if os.path.basename(entry["argv"][0]) in wanted:
                return entry
        return None


# ---------------------------------------------------------------- process inspection


class Proc:
    """Reads /proc; swapped out in tests."""

    def __init__(self, root="/proc"):
        self.root = root

    def _read(self, pid, name):
        try:
            with open(os.path.join(self.root, str(pid), name), "rb") as handle:
                return handle.read()
        except OSError:
            return b""

    def cmdline(self, pid):
        raw = self._read(pid, "cmdline")
        return [part.decode("utf-8", "replace") for part in raw.split(b"\0") if part]

    def exe(self, pid):
        try:
            return os.readlink(os.path.join(self.root, str(pid), "exe"))
        except OSError:
            return ""

    def cwd(self, pid):
        try:
            return os.readlink(os.path.join(self.root, str(pid), "cwd"))
        except OSError:
            return ""

    def environ(self, pid):
        out = {}
        for part in self._read(pid, "environ").split(b"\0"):
            if b"=" in part:
                key, _sep, value = part.partition(b"=")
                out[key.decode("utf-8", "replace")] = value.decode("utf-8", "replace")
        return out

    def parent(self, pid):
        stat = self._read(pid, "stat").decode("utf-8", "replace")
        fields = stat.rsplit(")", 1)[-1].split()
        try:
            return int(fields[1])
        except (IndexError, ValueError):
            return 0

    def children(self, pid):
        out = []
        task_dir = os.path.join(self.root, str(pid), "task")
        try:
            tasks = os.listdir(task_dir)
        except OSError:
            return out
        for task in tasks:
            try:
                with open(os.path.join(task_dir, task, "children")) as handle:
                    out.extend(int(child) for child in handle.read().split())
            except (OSError, ValueError):
                continue
        return sorted(set(out))

    def is_session_shell(self, pid):
        stat = self._read(pid, "stat").decode("utf-8", "replace")
        fields = stat.rsplit(")", 1)[-1].split()
        try:
            return int(fields[3]) == pid and int(fields[4]) != 0
        except (IndexError, ValueError):
            return False


def is_ephemeral(path):
    return (
        not path
        or path.startswith("/tmp/.mount_")
        or "/.mount_" in path
        or path.endswith(" (deleted)")
        or path.startswith("/proc/")
    )


def find_appimage(proc, pid):
    for _ in range(6):
        if pid <= 1:
            break
        image = proc.environ(pid).get("APPIMAGE", "")
        if image and not is_ephemeral(image) and os.path.isfile(image):
            return image
        pid = proc.parent(pid)
    return ""


def webapp_url(cls):
    match = WEBAPP_CLASS.match(cls or "")
    if not match:
        return ""
    app = match.group("app")
    host, sep, rest = app.partition("_")
    if not sep or not re.fullmatch(r"[A-Za-z0-9.-]+(:\d+)?", host):
        return ""
    return "https://%s/%s" % (host, rest.replace("_", "/").lstrip("/"))


def terminal_cwd(proc, pid, windows_for_pid, title):
    """The cwd of the terminal's shell, when it can be told apart."""
    shells = [child for child in proc.children(pid) if proc.is_session_shell(child)]
    if windows_for_pid <= 1 and shells:
        cwd = proc.cwd(max(shells))
        if os.path.isdir(cwd):
            return cwd
    # Several windows share one process (single-instance terminals): a title
    # that is a directory is the only honest way to tell them apart.
    # Only a path that starts the title or follows a space or colon, as in
    # "user@host:~/src", never a slash inside a word like "feat/tmp".
    match = re.search(r"(?:^|[\s:])([~/][^\s]*)", (title or "").strip())
    if match:
        path = os.path.expanduser(match.group(1))
        if os.path.isdir(path):
            return os.path.realpath(path)
    return ""


def resolve_launch(client, proc, desktop, windows_for_pid=1):
    cls = client.get("class") or ""
    initial = client.get("initialClass") or cls
    pid = int(client.get("pid") or 0)
    exe = proc.exe(pid) if pid > 0 else ""
    cmdline = proc.cmdline(pid) if pid > 0 else []
    binary = os.path.basename(exe or (cmdline[0] if cmdline else ""))

    # Terminals: reopen in the shell's directory, never replay the TUI.
    term_binary = binary if binary in TERMINALS else ""
    if not term_binary and cmdline and os.path.basename(cmdline[0]) in TERMINALS:
        term_binary = os.path.basename(cmdline[0])
    if term_binary:
        default_class, cwd_flag, class_flag = TERMINALS[term_binary]
        argv = [term_binary]
        if initial and initial.lower() != default_class.lower():
            argv += class_flag(initial)
        cwd = terminal_cwd(proc, pid, windows_for_pid, client.get("title"))
        if cwd:
            argv += cwd_flag(cwd)
        return {"kind": "terminal", "command": argv, "cwd": cwd, "perWindow": True}

    # Chromium web apps share the browser's pid, so its cmdline names
    # whichever web app started the browser. Resolve them by class.
    if WEBAPP_CLASS.match(initial):
        entry = desktop.lookup(initial, cls)
        if entry:
            return {"kind": "webapp", "command": entry["argv"], "desktopId": entry["id"], "perWindow": True}
        url = webapp_url(initial)
        if url:
            launcher = "omarchy-launch-webapp" if shutil.which("omarchy-launch-webapp") else "xdg-open"
            return {"kind": "webapp", "command": [launcher, url], "perWindow": True}

    entry = desktop.lookup(cls, initial)
    if entry:
        return {"kind": "desktop", "command": entry["argv"], "desktopId": entry["id"], "perWindow": False}

    appimage = find_appimage(proc, pid) if pid > 0 else ""
    if appimage:
        return {"kind": "appimage", "command": [appimage], "perWindow": False}

    if exe and not is_ephemeral(exe):
        entry = desktop.lookup_binary([exe] + cmdline[:1])
        if entry:
            return {"kind": "desktop", "command": entry["argv"], "desktopId": entry["id"], "perWindow": False}
        args = cmdline[1:]
        # Sockets, temp mounts and helper flags are only valid for this boot.
        if any("/tmp/" in arg or "/run/" in arg or "-cli://" in arg or arg.startswith("--type=") for arg in args):
            args = []
        return {"kind": "process", "command": [exe] + args[:MAX_COMMAND_ARGS], "perWindow": False}

    return {"kind": "unresolved", "command": [], "perWindow": False}


def validate_launch(launch):
    argv = launch.get("command") or []
    if not argv:
        return "unresolved"
    head = argv[0]
    if os.path.isabs(head):
        return "" if os.access(head, os.X_OK) else "missing"
    if head == "env":
        return ""
    return "" if shutil.which(head) else "missing"


# ---------------------------------------------------------------- capture


def monitor_logical(monitor):
    scale = float(monitor.get("scale") or 1) or 1
    width = int(round(int(monitor.get("width") or 0) / scale))
    height = int(round(int(monitor.get("height") or 0) / scale))
    if int(monitor.get("transform") or 0) % 2 == 1:
        width, height = height, width
    return width, height


def default_mode():
    path = os.path.join(omarchy_modes_dir(), "default.lua")
    try:
        with open(path, encoding="utf-8") as handle:
            match = re.search(r'default = "([a-z]+)"', handle.read())
            if match:
                return match.group(1)
    except OSError:
        pass
    return ""


def workspace_mode(workspace, fallback_default=None):
    wid = int(workspace.get("id") or 0)
    tiled = workspace.get("tiledLayout") or "dwindle"
    if wid < 1:
        return tiled
    try:
        with open(os.path.join(omarchy_modes_dir(), "%d.lua" % wid), encoding="utf-8") as handle:
            match = re.search(r'mode = "([a-z]+)"', handle.read())
            if match:
                return match.group(1)
    except OSError:
        pass
    default = default_mode() if fallback_default is None else fallback_default
    return default or tiled


def workspace_target(workspace):
    name = str(workspace.get("name") or workspace.get("id"))
    wid = int(workspace.get("id") or 0)
    if name.startswith("special:") or wid >= 1:
        return name
    return "name:" + name


def is_noise(client):
    cls = client.get("class") or ""
    initial = client.get("initialClass") or ""
    if not cls and not initial:
        return True
    if matches_any(cls, NOISE_CLASSES) or matches_any(initial, NOISE_CLASSES):
        return True
    title = (client.get("title") or "").strip()
    return title in NOISE_TITLES


def capture(hypr, proc, desktop, include_special=False, ignore=()):
    clients = hypr.clients()
    workspaces = {ws.get("id"): ws for ws in hypr.workspaces()}
    monitors = hypr.monitors()
    by_index = {m.get("id"): m for m in monitors}
    by_name = {m.get("name"): m for m in monitors}

    kept = []
    for client in clients:
        if not client.get("mapped", True) or client.get("hidden"):
            continue
        ws = client.get("workspace") or {}
        name = str(ws.get("name") or "")
        if int(ws.get("id") or 0) == 0 and not name:
            continue
        if name.startswith("special:") and (not include_special or name.startswith("special:screensaver")):
            continue
        if is_noise(client):
            continue
        if matches_any(client.get("class"), ignore) or matches_any(client.get("initialClass"), ignore):
            continue
        kept.append(client)

    per_pid = {}
    for client in kept:
        per_pid[client.get("pid")] = per_pid.get(client.get("pid"), 0) + 1

    def position_key(client):
        at = client.get("at") or [0, 0]
        return (str((client.get("workspace") or {}).get("id")), at[0], at[1])

    kept.sort(key=position_key)
    kept = kept[:MAX_WINDOWS]

    default = default_mode()
    used_ws = {}
    windows = []
    for index, client in enumerate(kept):
        ws_ref = client.get("workspace") or {}
        workspace = workspaces.get(ws_ref.get("id"), {"id": ws_ref.get("id"), "name": ws_ref.get("name")})
        monitor = by_index.get(client.get("monitor")) or by_name.get(workspace.get("monitor")) or (monitors[0] if monitors else {})
        target = workspace_target(workspace)
        if target not in used_ws:
            used_ws[target] = {
                "name": target,
                "id": int(workspace.get("id") or 0),
                "monitor": monitor.get("name", ""),
                "mode": workspace_mode(workspace, default),
            }
        launch = resolve_launch(client, proc, desktop, per_pid.get(client.get("pid"), 1))
        launch["command"] = [clip(arg, MAX_COMMAND_LEN) for arg in launch.get("command", [])][:MAX_COMMAND_ARGS]
        at = client.get("at") or [0, 0]
        size = client.get("size") or [0, 0]
        windows.append({
            "key": "w%d" % (index + 1),
            "class": clip(client.get("class"), MAX_CLASS),
            "initialClass": clip(client.get("initialClass"), MAX_CLASS),
            "title": clip(client.get("title"), MAX_TITLE),
            "initialTitle": clip(client.get("initialTitle"), MAX_TITLE),
            "workspace": target,
            "monitor": monitor.get("name", ""),
            "floating": bool(client.get("floating")),
            "pinned": bool(client.get("pinned")),
            "focused": client.get("focusHistoryID") == 0,
            "at": [int(at[0]) - int(monitor.get("x") or 0), int(at[1]) - int(monitor.get("y") or 0)],
            "size": [int(size[0]), int(size[1])],
            "order": index,
            "launch": launch,
            "warning": validate_launch(launch),
        })

    active = {}
    for monitor in monitors:
        ws = monitor.get("activeWorkspace") or {}
        target = workspace_target(workspaces.get(ws.get("id"), ws)) if ws else ""
        active[monitor.get("name")] = target
    focused = next((m.get("name") for m in monitors if m.get("focused")), "")

    return {
        "workspaces": sorted(used_ws.values(), key=lambda w: (w["id"] < 1, w["id"], w["name"])),
        "monitors": [
            {
                "name": m.get("name", ""),
                "x": int(m.get("x") or 0),
                "y": int(m.get("y") or 0),
                "width": monitor_logical(m)[0],
                "height": monitor_logical(m)[1],
                "activeWorkspace": active.get(m.get("name"), ""),
                "focused": bool(m.get("focused")),
            }
            for m in monitors
        ],
        "focusedMonitor": focused,
        "windows": windows,
    }


# ---------------------------------------------------------------- slot storage


class Store:
    def __init__(self, root=None):
        self.root = root or state_dir()
        self.slots_dir = os.path.join(self.root, "slots")

    def _path(self, slot_id):
        if not re.fullmatch(r"[a-z0-9-]{1,64}", slot_id or ""):
            raise QuickStartError("Unknown session")
        return os.path.join(self.slots_dir, slot_id + ".json")

    def list(self):
        slots = []
        try:
            names = sorted(os.listdir(self.slots_dir))
        except OSError:
            return slots
        for filename in names:
            if not filename.endswith(".json") or filename.startswith("."):
                continue
            try:
                slots.append(self.get(filename[:-5]))
            except QuickStartError:
                continue
        slots.sort(key=lambda slot: (slot.get("created", 0), slot.get("name", "")))
        return slots[:MAX_SLOTS]

    def get(self, slot_id):
        path = self._path(slot_id)
        try:
            if os.path.getsize(path) > 4 * 1024 * 1024:
                raise QuickStartError("Session file is too large")
            with open(path, encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError):
            raise QuickStartError("Unknown session")
        if not isinstance(data, dict) or not isinstance(data.get("windows"), list):
            raise QuickStartError("Session file is damaged")
        data["id"] = slot_id
        return data

    def put(self, slot):
        write_json_atomic(self._path(slot["id"]), slot)

    def delete(self, slot_id):
        try:
            os.unlink(self._path(slot_id))
        except FileNotFoundError:
            raise QuickStartError("Unknown session")

    def find_by_name(self, name):
        for slot in self.list():
            if slot.get("name", "").lower() == name.lower():
                return slot
        return None

    def new_id(self, name):
        base = slugify(name)
        candidate, counter = base, 2
        while os.path.exists(self._path(candidate)):
            candidate = "%s-%d" % (base, counter)
            counter += 1
        return candidate


def summary(store, extra=None):
    out = {"ok": True, "slots": store.list()}
    if extra:
        out.update(extra)
    return out


def find_window(slot, key):
    for window in slot["windows"]:
        if window.get("key") == key:
            return window
    raise QuickStartError("Unknown window")


# ---------------------------------------------------------------- restore


def split_tree(items, tolerance=24):
    """Cut saved rectangles into a guillotine tree: (direction, near, far).

    items are (window, x, y, w, h). "h" means side by side (a vertical cut),
    "v" stacked. Returns None when no clean cut exists.
    """
    if len(items) == 1:
        return items[0][0]
    for direction, start, length in (("h", 1, 3), ("v", 2, 4)):
        ordered = sorted(items, key=lambda item: item[start])
        for k in range(1, len(ordered)):
            near, far = ordered[:k], ordered[k:]
            if max(i[start] + i[length] for i in near) <= min(i[start] for i in far) + tolerance:
                left, right = split_tree(near, tolerance), split_tree(far, tolerance)
                if left is None or right is None:
                    return None
                return (direction, left, right)
    return None


def close_to(a, b, slack=2):
    return abs(int(a[0]) - int(b[0])) <= slack and abs(int(a[1]) - int(b[1])) <= slack


def first_leaf(node):
    while isinstance(node, tuple):
        node = node[1]
    return node


class Restorer:
    def __init__(self, hypr, slot, emit, clock=time.monotonic, sleep=time.sleep, run=None):
        self.hypr = hypr
        self.slot = slot
        self.emit = emit
        self.clock = clock
        self.sleep = sleep
        self.run = run or run_quiet
        self.cancelled = False
        self.focus_moved = False
        self.warnings = []
        self.assigned = {}  # window key -> address

    def warn(self, code, **data):
        data.update({"event": "warning", "code": code})
        self.warnings.append(data)
        self.emit(data)

    # ---- monitor mapping

    def plan_monitors(self):
        live = {m.get("name"): m for m in self.hypr.monitors()}
        focused = next((m for m in live.values() if m.get("focused")), None) or (next(iter(live.values())) if live else {})
        saved = {m.get("name"): m for m in self.slot.get("monitors", [])}
        self.monitor_map = {}
        missing = []
        for name, info in saved.items():
            target = live.get(name) or focused
            if name not in live:
                missing.append(name)
            width, height = monitor_logical(target) if target else (info.get("width", 0), info.get("height", 0))
            self.monitor_map[name] = {
                "name": target.get("name", ""),
                "x": int(target.get("x") or 0),
                "y": int(target.get("y") or 0),
                "width": width,
                "height": height,
                "sx": (width / info["width"]) if info.get("width") else 1.0,
                "sy": (height / info["height"]) if info.get("height") else 1.0,
            }
        if missing:
            self.warn("monitorFallback", monitors=missing, target=focused.get("name", ""))

    def target_monitor(self, window):
        return self.monitor_map.get(window.get("monitor")) or next(iter(self.monitor_map.values()), None)

    # ---- layout modes

    def apply_modes(self):
        live = {str(ws.get("id")): ws for ws in self.hypr.workspaces()}
        default = default_mode()
        changed = False
        for ws in self.slot.get("workspaces", []):
            wid = int(ws.get("id") or 0)
            mode = ws.get("mode")
            if wid < 1 or mode not in ("dwindle", "scrolling", "floating", "master", "monocle"):
                continue
            current = workspace_mode(live.get(str(wid), {"id": wid, "tiledLayout": ""}), default)
            if current == mode:
                continue
            lua = "o.workspace_mode({ workspace = %s, mode = %s })" % (lua_str(str(wid)), lua_str(mode))
            result = self.hypr.eval(lua)
            if not result.startswith("ok"):
                self.warn("layoutFailed", workspace=ws.get("name"), mode=mode)
                continue
            changed = True
            # Keep the mode across logins the same way the layout toggle does.
            path = os.path.join(omarchy_modes_dir(), "%d.lua" % wid)
            try:
                if mode == default:
                    if os.path.exists(path):
                        os.unlink(path)
                else:
                    os.makedirs(os.path.dirname(path), exist_ok=True)
                    with open(path, "w", encoding="utf-8") as handle:
                        handle.write(lua + "\n")
            except OSError:
                self.warn("layoutUnsaved", workspace=ws.get("name"))
        if changed:
            self.run(["omarchy-shell", "-q", "omarchy.workspaces", "refresh"])
            self.run(["omarchy-shell", "-q", "omarchy.dock", "refresh"])

    def place_workspaces(self):
        live = {ws.get("name"): ws for ws in self.hypr.workspaces()}
        calls = []
        for ws in self.slot.get("workspaces", []):
            name = ws.get("name", "")
            if name.startswith("special:"):
                continue
            target = self.monitor_map.get(ws.get("monitor"))
            current = live.get(name[5:] if name.startswith("name:") else name)
            if target and current and current.get("monitor") != target["name"]:
                calls.append("hl.dsp.workspace.move({ workspace = %s, monitor = %s })" % (lua_str(name), lua_str(target["name"])))
        self.hypr.dispatch_all(calls)

    # ---- matching

    @staticmethod
    def same_app(window, client):
        return bool(
            (window.get("class") and window.get("class") == client.get("class"))
            or (window.get("initialClass") and window.get("initialClass") == client.get("initialClass"))
        )

    @staticmethod
    def client_workspace(client):
        return str((client.get("workspace") or {}).get("name") or "")

    def match_tests(self):
        """From most to least certain; every window gets a chance at each
        level before any falls back to a looser one."""
        ws = lambda w: w["workspace"].replace("name:", "", 1)
        return (
            lambda w, c: c.get("title") == w.get("title"),
            lambda w, c: c.get("initialTitle") == w.get("initialTitle") and self.client_workspace(c) == ws(w),
            lambda w, c: self.client_workspace(c) == ws(w),
            lambda w, c: True,
        )

    def move_calls(self, window, client):
        if (client.get("workspace") or {}).get("name") == window["workspace"].replace("name:", "", 1):
            return []
        return ["hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (
            lua_str(window["workspace"]), lua_str("address:" + client["address"]))]

    def eligible(self, client):
        name = str((client.get("workspace") or {}).get("name") or "")
        return client.get("mapped", True) and not name.startswith("special:screensaver") and not is_noise(client)

    def reuse_existing(self, pending):
        clients = [c for c in self.hypr.clients() if self.eligible(c)]
        include_special = any(w["workspace"].startswith("special:") for w in pending)
        pool = [
            c for c in clients
            if include_special or not str((c.get("workspace") or {}).get("name") or "").startswith("special:")
        ]
        calls = []
        still = sorted(pending, key=lambda w: w["order"])
        # Exact titles first, across all windows, so two windows of one app
        # keep their own places even when the first one's title changed.
        for test in self.match_tests():
            for window in list(still):
                client = next((c for c in pool if self.same_app(window, c) and test(window, c)), None)
                if client is None:
                    continue
                pool.remove(client)
                still.remove(window)
                self.assigned[window["key"]] = client["address"]
                calls.extend(self.move_calls(window, client))
        self.hypr.dispatch_all(calls)
        return still, {c.get("address") for c in clients}

    # ---- launching

    def launch(self, window):
        argv = window.get("launch", {}).get("command") or []
        command = shlex.join(argv)
        lua = "hl.dsp.exec_cmd(%s, { workspace = %s })" % (lua_str(command), lua_str(window["workspace"] + " silent"))
        return self.hypr.dispatch_all([lua]).startswith("ok")

    def launch_missing(self, missing):
        launched = {}
        waiting = []
        for window in sorted(missing, key=lambda w: (w["workspace"], w["order"])):
            if self.cancelled:
                break
            launch = window.get("launch") or {}
            if not launch.get("command"):
                self.warn("unresolved", key=window["key"], cls=window.get("class"), title=window.get("title"))
                continue
            group = (tuple(launch["command"]), window["workspace"]) if not launch.get("perWindow") else None
            if group is not None and group in launched:
                # Apps restore their own extra windows; one launch is enough.
                window["deadline"] = launched[group]
                waiting.append(window)
                continue
            if not self.launch(window):
                self.warn("launchFailed", key=window["key"], cls=window.get("class"))
                continue
            deadline = self.clock() + MAP_TIMEOUT
            window["deadline"] = deadline
            if group is not None:
                launched[group] = deadline
            waiting.append(window)
            self.sleep(0.15)
        return waiting

    def wait_for_windows(self, waiting, known, done, total):
        try:
            events = self.hypr.events()
        except OSError:
            events = None
        last_poll = 0.0
        try:
            while waiting and not self.cancelled:
                # Hyprland's event socket says when a window maps or renames
                # itself; between events a slow poll catches anything missed.
                wake = False
                if events is not None:
                    ready, _w, _x = select.select([events], [], [], 0.4)
                    if ready:
                        try:
                            chunk = events.recv(65536)
                        except (BlockingIOError, InterruptedError):
                            chunk = None
                        if chunk == b"":
                            # Hyprland closed the socket (a config reload):
                            # fall back to polling instead of spinning.
                            events.close()
                            events = None
                            chunk = None
                        wake = bool(chunk) and any(tag in chunk for tag in (b"openwindow>>", b"changeclass>>", b"windowtitlev2>>"))
                else:
                    self.sleep(0.4)
                if not wake and self.clock() - last_poll < 1.2:
                    self.expire(waiting)
                    continue
                last_poll = self.clock()
                fresh = [
                    c for c in self.hypr.clients()
                    if c.get("address") not in known and self.eligible(c) and (c.get("class") or c.get("initialClass"))
                ]
                calls = []
                for client in fresh:
                    window = self.pick_waiting(client, waiting)
                    if window is None:
                        continue
                    known.add(client["address"])
                    waiting.remove(window)
                    self.assigned[window["key"]] = client["address"]
                    calls.extend(self.move_calls(window, client))
                    done += 1
                    self.emit({"event": "progress", "done": done, "total": total, "key": window["key"]})
                self.hypr.dispatch_all(calls)
                self.expire(waiting)
        finally:
            if events is not None:
                events.close()
        return done

    def expire(self, waiting):
        now = self.clock()
        for window in [w for w in waiting if w["deadline"] <= now]:
            waiting.remove(window)
            self.warn("timeout", key=window["key"], cls=window.get("class"), title=window.get("title"))

    def pick_waiting(self, client, waiting):
        matching = [w for w in waiting if self.same_app(w, client)]
        if not matching:
            return None
        for window in matching:
            if window.get("title") == client.get("title"):
                return window
        return min(matching, key=lambda w: w["order"])

    # ---- final layout pass

    def wait_until_settled(self):
        """Apps keep resizing for a moment after they map (browsers restore
        their own windows); wait until the slot's windows stop moving."""
        self.sleep(SETTLE_SECONDS)
        deadline = self.clock() + SETTLE_LIMIT
        previous = None
        while not self.cancelled and self.clock() < deadline:
            live = self.live_by_address()
            shape = sorted(
                (address, tuple(live[address].get("at") or []), tuple(live[address].get("size") or []))
                for address in self.assigned.values() if address in live
            )
            if shape == previous:
                return
            previous = shape
            self.sleep(0.5)

    def live_by_address(self):
        return {c.get("address"): c for c in self.hypr.clients()}

    def in_place(self, window, client):
        monitor = self.target_monitor(window)
        if not monitor:
            return True
        x = monitor["x"] + window["at"][0] * monitor["sx"]
        y = monitor["y"] + window["at"][1] * monitor["sy"]
        at = client.get("at") or [0, 0]
        return abs(at[0] - x) <= monitor["width"] * 0.15 and abs(at[1] - y) <= monitor["height"] * 0.15

    def reorder(self, mode_of):
        """Tiled layouts place windows in arrival order; replay the saved one.

        Scrolling: every window goes out to a hidden staging workspace and
        comes back in saved order, each landing as the next column, in one
        batch between two frames. Dwindle: see rebuild_dwindle.
        """
        live = self.live_by_address()
        calls = []
        by_ws = {}
        for window in self.slot["windows"]:
            address = self.assigned.get(window["key"])
            if address in live and not window.get("floating") and mode_of.get(window["workspace"]) in ("scrolling", "dwindle"):
                by_ws.setdefault(window["workspace"], []).append(window)
        for ws, windows in by_ws.items():
            if len(windows) < 2:
                continue
            windows.sort(key=lambda w: w["order"])
            if mode_of.get(ws) == "dwindle":
                if not all(self.in_place(w, live[self.assigned[w["key"]]]) for w in windows):
                    self.rebuild_dwindle(ws, windows)
                continue
            current = sorted(windows, key=lambda w: tuple(live[self.assigned[w["key"]]].get("at") or [0, 0]))
            if [w["key"] for w in current] == [w["key"] for w in windows]:
                continue
            targets = [lua_str("address:" + self.assigned[w["key"]]) for w in windows]
            for target in targets:
                calls.append("hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (lua_str(STAGING_WORKSPACE), target))
            for target in targets:
                calls.append("hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (lua_str(ws), target))
        if calls:
            self.hypr.dispatch_all(calls)
            self.sleep(0.4)

    def rebuild_dwindle(self, ws, windows):
        """Grow the saved split tree again, one split at a time.

        The saved rectangles are cut into a guillotine tree. Dwindle splits
        the focused window, so for each split the first window of the far
        side is brought in while the first window of the near side has focus;
        then the split's direction and side are checked and flipped with
        togglesplit / swapsplit when Hyprland chose differently.
        """
        tree = split_tree([(w, w["at"][0], w["at"][1], w["size"][0], w["size"][1]) for w in windows])
        address = lambda w: lua_str("address:" + self.assigned[w["key"]])
        self.hypr.dispatch_all(["hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (
            lua_str(STAGING_WORKSPACE), address(w)) for w in windows])
        if tree is None:
            # Overlapping or otherwise unsplittable: bring them back in order.
            self.focus_moved = True
            calls = []
            for w in windows:
                calls.append("hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (lua_str(ws), address(w)))
                calls.append("hl.dsp.focus({ window = %s })" % address(w))
            self.hypr.dispatch_all(calls)
            return
        first = first_leaf(tree)
        self.hypr.dispatch_all(["hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (lua_str(ws), address(first))])
        self.grow_split(ws, tree, address)

    def grow_split(self, ws, node, address):
        if not isinstance(node, tuple):
            return
        direction, near, far = node
        a, b = first_leaf(near), first_leaf(far)
        self.focus_moved = True
        self.hypr.dispatch_all([
            "hl.dsp.focus({ window = %s })" % address(a),
            "hl.dsp.window.move({ workspace = %s, follow = false, window = %s })" % (lua_str(ws), address(b)),
        ])
        self.sleep(0.05)
        for _attempt in range(2):
            live = self.live_by_address()
            ca, cb = live.get(self.assigned[a["key"]]), live.get(self.assigned[b["key"]])
            if not ca or not cb:
                break
            (ax, ay), (bx, by) = ca.get("at") or [0, 0], cb.get("at") or [0, 0]
            side_by_side = abs(ay - by) < abs(ax - bx)
            fixes = []
            if side_by_side != (direction == "h"):
                fixes.append('hl.dsp.layout("togglesplit")')
            elif (bx < ax) if direction == "h" else (by < ay):
                fixes.append('hl.dsp.layout("swapsplit")')
            if not fixes:
                break
            self.hypr.dispatch_all(["hl.dsp.focus({ window = %s })" % address(b)] + fixes)
            self.sleep(0.05)
        self.grow_split(ws, near, address)
        self.grow_split(ws, far, address)

    def float_states(self, mode_of):
        """Float or tile each window first; sizes are only right after that."""
        live = self.live_by_address()
        calls = []
        for window in self.slot["windows"]:
            address = self.assigned.get(window["key"])
            client = live.get(address)
            if not client:
                continue
            target = lua_str("address:" + address)
            if window.get("floating") and not client.get("floating"):
                calls.append("hl.dsp.window.float({ action = \"on\", window = %s })" % target)
            elif not window.get("floating") and client.get("floating") and mode_of.get(window["workspace"]) != "floating":
                calls.append("hl.dsp.window.float({ action = \"off\", window = %s })" % target)
        if calls:
            self.hypr.dispatch_all(calls)
            self.sleep(0.3)

    def geometry(self, mode_of):
        live = self.live_by_address()
        calls = []
        dwindle = []
        columns = []
        for window in self.slot["windows"]:
            address = self.assigned.get(window["key"])
            client = live.get(address)
            monitor = self.target_monitor(window)
            if not client or not monitor:
                continue
            target = lua_str("address:" + address)
            width = max(80, int(round(window["size"][0] * monitor["sx"])))
            height = max(60, int(round(window["size"][1] * monitor["sy"])))
            width, height = min(width, monitor["width"]), min(height, monitor["height"])
            current = client.get("size") or [width, height]
            mode = mode_of.get(window["workspace"])
            # Windows already where they belong are left alone: no
            # redundant configure events for apps that are already right.
            if window.get("floating"):
                x = int(round(window["at"][0] * monitor["sx"]))
                y = int(round(window["at"][1] * monitor["sy"]))
                x = max(0, min(x, monitor["width"] - width))
                y = max(0, min(y, monitor["height"] - height))
                at = client.get("at") or [0, 0]
                if not close_to(current, (width, height)):
                    calls.append("hl.dsp.window.resize({ x = %d, y = %d, relative = false, window = %s })" % (width, height, target))
                if not close_to(at, (monitor["x"] + x, monitor["y"] + y)):
                    calls.append("hl.dsp.window.move({ x = %d, y = %d, relative = false, window = %s })" % (monitor["x"] + x, monitor["y"] + y, target))
            elif mode == "scrolling":
                if abs(int(current[0]) - width) <= 2:
                    continue
                calls.append("hl.dsp.window.resize({ x = %d, y = %d, relative = false, window = %s })" % (width, int(current[1]), target))
                columns.append((window["key"], width, monitor["width"]))
            elif mode == "dwindle":
                # A dwindle resize moves the window's edge that faces its
                # sibling; an edge on the screen border has no split behind it.
                # So each axis is set only by the window whose far edge is inside.
                saved_w = monitor["width"] / monitor["sx"] if monitor["sx"] else monitor["width"]
                saved_h = monitor["height"] / monitor["sy"] if monitor["sy"] else monitor["height"]
                inner_x = window["at"][0] + window["size"][0] < saved_w - DWINDLE_EDGE
                inner_y = window["at"][1] + window["size"][1] < saved_h - DWINDLE_EDGE
                if inner_x or inner_y:
                    dwindle.append((window["key"], width if inner_x else None, height if inner_y else None))
        self.hypr.dispatch_all(calls)
        self.fix_columns(columns)
        # Each split resize shifts its neighbours, so these go one at a time,
        # as a relative step from the size the window has right then.
        for key, width, height in dwindle:
            client = self.live_by_address().get(self.assigned[key])
            if not client:
                continue
            size = client.get("size") or [0, 0]
            dx = width - int(size[0]) if width is not None else 0
            dy = height - int(size[1]) if height is not None else 0
            if dx or dy:
                self.hypr.dispatch_all(["hl.dsp.window.resize({ x = %d, y = %d, relative = true, window = %s })" % (
                    dx, dy, lua_str("address:" + self.assigned[key]))])

    def fix_columns(self, columns):
        """Hyprland can ignore a direct resize of a scrolling column that is
        partly off screen; colresize on the focused column always lands."""
        if not columns:
            return
        self.sleep(0.2)
        for key, width, monitor_width in columns:
            address = self.assigned[key]
            fraction = None
            for _attempt in range(2):
                client = self.live_by_address().get(address)
                if not client or monitor_width <= 0:
                    break
                actual = int((client.get("size") or [0, 0])[0])
                if abs(actual - width) <= 8:
                    break
                if fraction is None or actual <= 0:
                    fraction = width / float(monitor_width)
                else:
                    fraction = fraction * width / float(actual)
                fraction = max(0.05, min(1.0, fraction))
                self.focus_moved = True
                self.hypr.dispatch_all([
                    "hl.dsp.focus({ window = %s })" % lua_str("address:" + address),
                    "hl.dsp.layout(%s)" % lua_str("colresize %.4f" % fraction),
                ])
                self.sleep(0.15)

    def focus_and_pin(self):
        """Show each monitor's saved workspace and give focus back, skipping
        whatever is already showing or focused."""
        calls = []
        live_monitors = {m.get("name"): m for m in self.hypr.monitors()}
        shown = {name: str((m.get("activeWorkspace") or {}).get("name") or "") for name, m in live_monitors.items()}
        focused_monitor = next((name for name, m in live_monitors.items() if m.get("focused")), "")
        focused_name = self.slot.get("focusedMonitor")
        ordered = sorted(self.slot.get("monitors", []), key=lambda m: m.get("name") == focused_name)
        for monitor in ordered:
            ws = monitor.get("activeWorkspace") or ""
            target = (self.monitor_map.get(monitor.get("name")) or {}).get("name")
            if not ws or ws.startswith("special:") or not target:
                continue
            last = monitor.get("name") == focused_name
            if shown.get(target) == ws.replace("name:", "", 1) and (not last or focused_monitor == target):
                continue
            calls.append("hl.dsp.focus({ workspace = %s })" % lua_str(ws))
            shown[target] = ws.replace("name:", "", 1)
            focused_monitor = target
        live = self.live_by_address()
        for window in self.slot["windows"]:
            address = self.assigned.get(window["key"])
            client = live.get(address)
            if window.get("pinned") and window.get("floating") and client and not client.get("pinned"):
                calls.append("hl.dsp.window.pin({ action = \"on\", window = %s })" % lua_str("address:" + address))
        # The window that had focus when the session was saved gets it back.
        for window in self.slot["windows"]:
            address = self.assigned.get(window["key"])
            client = live.get(address)
            if window.get("focused") and client and not window["workspace"].startswith("special:"):
                if calls or self.focus_moved or client.get("focusHistoryID") != 0:
                    calls.append("hl.dsp.focus({ window = %s })" % lua_str("address:" + address))
                break
        self.hypr.dispatch_all(calls)

    # ---- main

    def run_restore(self):
        windows = [dict(w) for w in self.slot["windows"]]
        total = len(windows)
        self.emit({"event": "start", "total": total, "name": self.slot.get("name", "")})
        self.plan_monitors()
        self.apply_modes()
        self.place_workspaces()
        missing, known = self.reuse_existing(windows)
        done = total - len(missing)
        self.emit({"event": "progress", "done": done, "total": total})
        waiting = self.launch_missing(missing)
        done = self.wait_for_windows(waiting, known, done, total)
        if self.cancelled:
            self.emit({"event": "cancelled", "done": done, "total": total})
            return
        self.wait_until_settled()
        mode_of = {ws["name"]: ws.get("mode") for ws in self.slot.get("workspaces", [])}
        # Float state first: a window that should float must leave the tiled
        # layout before the tiled windows are put in order, and vice versa.
        for phase in (
            self.place_workspaces,
            lambda: self.float_states(mode_of),
            lambda: self.reorder(mode_of),
            lambda: self.geometry(mode_of),
            self.focus_and_pin,
        ):
            if self.cancelled:
                self.emit({"event": "cancelled", "done": done, "total": total})
                return
            phase()
        self.emit({"event": "done", "done": done, "total": total, "warnings": len(self.warnings)})


def run_quiet(argv):
    if not shutil.which(argv[0]):
        return
    pid = os.fork()
    if pid == 0:
        try:
            devnull = os.open(os.devnull, os.O_RDWR)
            for fd in (0, 1, 2):
                os.dup2(devnull, fd)
            os.execvp(argv[0], argv)
        finally:
            os._exit(127)
    os.waitpid(pid, 0)


# ---------------------------------------------------------------- CLI


def emit_line(data):
    sys.stdout.write(json.dumps(data, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def build_slot(store, name, snapshot, slot_id=None, created=None, include_special=False):
    now = int(time.time())
    slot = {
        "schemaVersion": SCHEMA_VERSION,
        "id": slot_id or store.new_id(name),
        "name": name,
        "created": created or now,
        "updated": now,
        "includeSpecial": bool(include_special),
    }
    slot.update(snapshot)
    return slot


def command(args, store, hypr_factory=Hyprland, proc=None, desktop_factory=DesktopIndex):
    action = args.action
    if action == "list":
        return summary(store)

    if action in ("save", "update"):
        if action == "save":
            name = clean_name(args.name)
            if store.find_by_name(name):
                raise QuickStartError("A session with that name already exists")
            if len(store.list()) >= MAX_SLOTS:
                raise QuickStartError("Too many saved sessions")
            existing = None
        else:
            existing = store.get(args.id)
            name = existing["name"]
        snapshot = capture(hypr_factory(), proc or Proc(), desktop_factory(), args.include_special, parse_ignore(args.ignore))
        if not snapshot["windows"]:
            raise QuickStartError("No windows to save")
        slot = build_slot(
            store, name, snapshot,
            slot_id=existing["id"] if existing else None,
            created=existing.get("created") if existing else None,
            include_special=args.include_special,
        )
        store.put(slot)
        return summary(store, {"saved": slot["id"]})

    if action == "rename":
        slot = store.get(args.id)
        name = clean_name(args.name)
        other = store.find_by_name(name)
        if other and other["id"] != slot["id"]:
            raise QuickStartError("A session with that name already exists")
        slot["name"] = name
        store.put(slot)
        return summary(store)

    if action == "delete":
        store.delete(args.id)
        return summary(store)

    if action == "remove-window":
        slot = store.get(args.id)
        window = find_window(slot, args.key)
        slot["windows"].remove(window)
        used = {w["workspace"] for w in slot["windows"]}
        slot["workspaces"] = [ws for ws in slot.get("workspaces", []) if ws.get("name") in used]
        store.put(slot)
        return summary(store)

    if action == "set-command":
        slot = store.get(args.id)
        window = find_window(slot, args.key)
        text = clip(args.command, MAX_COMMAND_LEN).strip()
        try:
            argv = shlex.split(text)
        except ValueError:
            raise QuickStartError("That command has an unmatched quote")
        if not argv or len(argv) > MAX_COMMAND_ARGS:
            raise QuickStartError("Enter a command to launch")
        launch = {"kind": "custom", "command": argv, "perWindow": True}
        window["launch"] = launch
        window["warning"] = validate_launch(launch)
        store.put(slot)
        return summary(store)

    if action == "restore":
        slot = store.get(args.id)
        lock_path = os.path.join(store.root, ".restore.lock")
        os.makedirs(store.root, exist_ok=True)
        lock = open(lock_path, "w")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            raise QuickStartError("A restore is already running")
        restorer = Restorer(hypr_factory(), slot, emit_line)

        def stop(_signum, _frame):
            restorer.cancelled = True

        signal.signal(signal.SIGTERM, stop)
        signal.signal(signal.SIGINT, stop)
        try:
            restorer.run_restore()
        finally:
            lock.close()
        return None

    raise QuickStartError("Unknown command")


def parser():
    root = argparse.ArgumentParser(prog="quick_start.py", description=__doc__.splitlines()[0])
    sub = root.add_subparsers(dest="action", required=True)
    sub.add_parser("list")
    for name in ("save", "update"):
        p = sub.add_parser(name)
        p.add_argument("name" if name == "save" else "id")
        p.add_argument("--include-special", action="store_true")
        p.add_argument("--ignore", default="")
    p = sub.add_parser("rename")
    p.add_argument("id")
    p.add_argument("name")
    for name in ("delete", "restore"):
        sub.add_parser(name).add_argument("id")
    p = sub.add_parser("remove-window")
    p.add_argument("id")
    p.add_argument("key")
    p = sub.add_parser("set-command")
    p.add_argument("id")
    p.add_argument("key")
    p.add_argument("command")
    return root


def main(argv=None):
    args = parser().parse_args(argv)
    store = Store()
    try:
        result = command(args, store)
    except QuickStartError as error:
        payload = {"ok": False, "error": str(error)}
        if args.action == "restore":
            payload = {"event": "error", "error": str(error)}
        else:
            try:
                payload["slots"] = store.list()
            except Exception:
                pass
        emit_line(payload)
        return 1
    if result is not None:
        emit_line(result)
    return 0


if __name__ == "__main__":
    sys.exit(main())
