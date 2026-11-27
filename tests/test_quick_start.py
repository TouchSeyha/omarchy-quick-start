import json
import os
import re
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), ".."))
import quick_start as qs  # noqa: E402


MONITORS = [
    {"id": 0, "name": "eDP-1", "width": 2880, "height": 1800, "scale": 1.8, "transform": 0, "x": 1920, "y": 1080,
     "focused": False, "activeWorkspace": {"id": 1, "name": "1"}},
    {"id": 1, "name": "DP-2", "width": 1920, "height": 1080, "scale": 1, "transform": 0, "x": 0, "y": 1000,
     "focused": True, "activeWorkspace": {"id": 11, "name": "11"}},
]
WORKSPACES = [
    {"id": 1, "name": "1", "monitor": "eDP-1", "tiledLayout": "dwindle"},
    {"id": 11, "name": "11", "monitor": "DP-2", "tiledLayout": "scrolling"},
    {"id": -97, "name": "special:scratchpad", "monitor": "DP-2", "tiledLayout": "dwindle"},
]


def client(address, cls, title, pid, ws, monitor=1, at=(0, 1000), size=(900, 1000), floating=False, **extra):
    data = {
        "address": address, "class": cls, "initialClass": cls, "title": title, "initialTitle": title,
        "pid": pid, "workspace": {"id": ws[0], "name": ws[1]}, "monitor": monitor, "at": list(at),
        "size": list(size), "floating": floating, "pinned": False, "mapped": True, "hidden": False,
    }
    data.update(extra)
    return data


class FakeProc:
    def __init__(self, procs):
        self.procs = procs

    def get(self, pid):
        return self.procs.get(pid, {})

    def cmdline(self, pid):
        return list(self.get(pid).get("cmdline", []))

    def exe(self, pid):
        return self.get(pid).get("exe", "")

    def cwd(self, pid):
        return self.get(pid).get("cwd", "")

    def environ(self, pid):
        return dict(self.get(pid).get("env", {}))

    def parent(self, pid):
        return self.get(pid).get("ppid", 0)

    def children(self, pid):
        return list(self.get(pid).get("children", []))

    def is_session_shell(self, pid):
        return self.get(pid).get("shell", False)


class FakeHypr:
    def __init__(self, clients, monitors=None, workspaces=None, spawn=None):
        self._clients = clients
        self._monitors = monitors if monitors is not None else MONITORS
        self._workspaces = workspaces if workspaces is not None else WORKSPACES
        self.evals = []
        self.spawn = spawn or {}
        self.counter = 0

    def clients(self):
        return [dict(c) for c in self._clients]

    def monitors(self):
        return self._monitors

    def workspaces(self):
        return self._workspaces

    def eval(self, lua):
        self.evals.append(lua)
        for match in re.finditer(r'hl\.dsp\.exec_cmd\("((?:[^"\\]|\\.)*)", \{ workspace = "([^ "]+) silent" \}\)', lua):
            command = match.group(1)
            for needle, cls in self.spawn.items():
                if needle in command:
                    self.counter += 1
                    self._clients.append(client("0xnew%d" % self.counter, cls, cls + " window", 9000 + self.counter, (99, "99")))
        for match in re.finditer(r'hl\.dsp\.window\.move\(\{ workspace = "([^"]+)", follow = false, window = "address:([^"]+)" \}\)', lua):
            for c in self._clients:
                if c["address"] == match.group(2):
                    name = match.group(1)
                    c["workspace"] = {"id": int(name) if name.isdigit() else -1, "name": name}
        return "ok"

    def dispatch_all(self, calls):
        return qs.Hyprland.dispatch_all(self, calls)

    def events(self):
        raise OSError("no event socket in tests")


class FakeClock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def sleep(self, seconds):
        self.now += seconds


def desktop_dir(entries):
    root = tempfile.mkdtemp()
    apps = os.path.join(root, "applications")
    os.makedirs(apps)
    for name, body in entries.items():
        with open(os.path.join(apps, name + ".desktop"), "w") as handle:
            handle.write("[Desktop Entry]\nType=Application\n" + body)
    return root


DESKTOP = {
    "YouTube": "Name=YouTube\nExec=omarchy-launch-webapp https://youtube.com/\nStartupWMClass=chrome-youtube.com__-Default\n",
    "t3code": 'Name=T3 Code\nExec="/home/u/.local/share/t3code/T3-Code.AppImage" %U\nStartupWMClass=com.t3tools.T3Code\n',
    "dev.zed.Zed": "Name=Zed\nExec=zeditor %U\n[Desktop Action NewWorkspace]\nExec=zeditor --new %U\n",
    "org.telegram.desktop": "Name=Telegram\nExec=Telegram -- %U\nStartupWMClass=TelegramDesktop\n",
    "helium": "Name=Helium\nExec=helium %U\n",
}


class MonitorMatchTests(unittest.TestCase):
    def test_the_same_screen_is_found_on_another_port(self):
        dell = "Dell Inc. DELL S2421HN 6LMX1H3"
        saved = {"DP-2": {"name": "DP-2", "description": dell}, "eDP-1": {"name": "eDP-1", "description": "Samsung panel"}}
        live = {"HDMI-A-1": {"name": "HDMI-A-1", "description": dell}, "eDP-1": {"name": "eDP-1", "description": "Samsung panel"}}
        matched = qs.match_monitors(saved, live)
        self.assertEqual(matched["DP-2"]["name"], "HDMI-A-1")
        self.assertEqual(matched["eDP-1"]["name"], "eDP-1")

    def test_a_different_screen_on_the_old_port_is_not_mistaken_for_it(self):
        saved = {"DP-2": {"name": "DP-2", "description": "Dell A"}, "DP-1": {"name": "DP-1", "description": "LG B"}}
        live = {"DP-1": {"name": "DP-1", "description": "Dell A"}, "DP-2": {"name": "DP-2", "description": "LG B"}}
        matched = qs.match_monitors(saved, live)
        self.assertEqual((matched["DP-2"]["name"], matched["DP-1"]["name"]), ("DP-1", "DP-2"))

    def test_old_sessions_and_twin_screens_fall_back_to_the_port(self):
        saved = {"DP-2": {"name": "DP-2"}, "DP-3": {"name": "DP-3", "description": "Twin"}, "DP-4": {"name": "DP-4", "description": "Twin"}}
        live = {"DP-2": {"name": "DP-2", "description": "X"}, "DP-3": {"name": "DP-3", "description": "Twin"}, "DP-4": {"name": "DP-4", "description": "Twin"}}
        matched = qs.match_monitors(saved, live)
        self.assertEqual({k: v["name"] for k, v in matched.items()}, {"DP-2": "DP-2", "DP-3": "DP-3", "DP-4": "DP-4"})
        self.assertEqual(qs.match_monitors({"DP-9": {"name": "DP-9", "description": "Gone"}}, live), {})


class QuotingTests(unittest.TestCase):
    def test_lua_str_escapes_everything_that_could_break_out(self):
        self.assertEqual(qs.lua_str('a"b\\c'), '"a\\"b\\\\c"')
        self.assertEqual(qs.lua_str("x\ny\x01"), '"x\\ny\\001"')
        self.assertEqual(qs.lua_str("é"), '"\\195\\169"')
        quoted = qs.lua_str('"); os.execute("rm -rf ~") --')
        self.assertTrue(quoted.startswith('"\\"); os.execute(\\"rm'))
        self.assertNotIn('")', quoted[1:-1].replace('\\"', ""))

    def test_exec_field_codes_are_removed(self):
        self.assertEqual(qs.exec_to_argv('"/opt/My App/app" --flag %U'), ["/opt/My App/app", "--flag"])
        self.assertEqual(qs.exec_to_argv("app --icon=%i 100%% %f"), ["app", "100%"])
        self.assertEqual(qs.exec_to_argv("flatpak run --branch=stable org.x.Y @@u %U @@"), ["flatpak", "run", "--branch=stable", "org.x.Y"])
        self.assertEqual(qs.exec_to_argv('broken "quote'), [])

    def test_webapp_url_from_class(self):
        self.assertEqual(qs.webapp_url("chrome-x.com__-Default"), "https://x.com/")
        self.assertEqual(qs.webapp_url("chrome-discord.com__channels_@me-Default"), "https://discord.com/channels/@me")
        self.assertEqual(qs.webapp_url("chrome-music.youtube.com__-Profile_1"), "https://music.youtube.com/")
        self.assertEqual(qs.webapp_url("helium"), "")


class ResolveTests(unittest.TestCase):
    def setUp(self):
        self.desktop = qs.DesktopIndex([desktop_dir(DESKTOP)])

    def test_webapps_sharing_a_browser_pid_resolve_by_class(self):
        proc = FakeProc({16505: {"exe": "/opt/helium-browser-bin/helium", "cmdline": ["/opt/helium-browser-bin/helium", "--app=https://music.youtube.com/"]}})
        youtube = qs.resolve_launch(client("0x1", "chrome-youtube.com__-Default", "YouTube", 16505, (1, "1")), proc, self.desktop)
        self.assertEqual(youtube["command"], ["omarchy-launch-webapp", "https://youtube.com/"])
        x = qs.resolve_launch(client("0x2", "chrome-x.com__-Default", "Home / X", 16505, (11, "11")), proc, self.desktop)
        self.assertEqual(x["kind"], "webapp")
        self.assertEqual(x["command"][1:], ["https://x.com/"])
        browser = qs.resolve_launch(client("0x3", "helium", "Tab - Helium", 16505, (11, "11")), proc, self.desktop)
        self.assertEqual(browser["command"], ["helium"])

    def test_appimage_never_stores_its_temporary_mount(self):
        proc = FakeProc({48733: {"exe": "/tmp/.mount_T3-CodMLfAeI/t3code", "cmdline": ["/tmp/.mount_T3-CodMLfAeI/t3code"]}})
        launch = qs.resolve_launch(client("0x4", "com.t3tools.T3Code", "T3", 48733, (11, "11")), proc, self.desktop)
        self.assertEqual(launch["command"], ["/home/u/.local/share/t3code/T3-Code.AppImage"])

        with tempfile.NamedTemporaryFile(suffix=".AppImage") as image:
            proc = FakeProc({
                700: {"exe": "/tmp/.mount_Foo/foo", "ppid": 699},
                699: {"env": {"APPIMAGE": image.name}},
            })
            launch = qs.resolve_launch(client("0x5", "foo", "Foo", 700, (1, "1")), proc, self.desktop)
            self.assertEqual(launch, {"kind": "appimage", "command": [image.name], "perWindow": False})

        orphan = qs.resolve_launch(client("0x6", "bar", "Bar", 701, (1, "1")), FakeProc({701: {"exe": "/tmp/.mount_Bar/bar"}}), self.desktop)
        self.assertEqual(orphan["kind"], "unresolved")

    def test_cli_socket_apps_use_their_desktop_entry(self):
        proc = FakeProc({60495: {"exe": "/usr/lib/zed/zed-editor", "cmdline": ["/usr/lib/zed/zed-editor", "zed-cli:///tmp/.tmp0X9IHv/socket"]}})
        launch = qs.resolve_launch(client("0x7", "dev.zed.Zed", "proj — file", 60495, (12, "12")), proc, self.desktop)
        self.assertEqual(launch["command"], ["zeditor"])
        proc = FakeProc({61: {"exe": "/usr/bin/someapp", "cmdline": ["/usr/bin/someapp", "zed-cli:///tmp/x"]}})
        launch = qs.resolve_launch(client("0x8", "someapp", "x", 61, (1, "1")), proc, self.desktop)
        self.assertEqual(launch["command"], ["/usr/bin/someapp"])

    def test_app_name_comes_from_the_desktop_entry_not_the_title(self):
        def name(cls, title, procs, pid):
            c = client("0xa", cls, title, pid, (1, "1"))
            proc = FakeProc(procs)
            return qs.app_name(c, qs.resolve_launch(c, proc, self.desktop), self.desktop)

        self.assertEqual(name("com.t3tools.T3Code", "repo / thread", {}, 0), "T3 Code")
        self.assertEqual(name("dev.zed.Zed", "proj — file.qml", {}, 0), "Zed")
        self.assertEqual(name("org.telegram.desktop", "(1) Chat", {}, 0), "Telegram")
        self.assertEqual(name("chrome-youtube.com__-Default", "(591) YouTube", {}, 0), "YouTube")
        # A web app without its own entry is named after its site, not the browser.
        helium = {5: {"exe": "/opt/helium-browser-bin/helium", "cmdline": ["/opt/helium-browser-bin/helium"]}}
        self.assertEqual(name("chrome-x.com__-Default", "Home / X", helium, 5), "x.com")
        self.assertEqual(name("someapp", "x", {6: {"exe": "/usr/bin/some_app", "cmdline": ["/usr/bin/some_app"]}}, 6), "Some app")
        self.assertEqual(name("nothing", "x", {}, 0), "")

    def test_desktop_id_matches_class_when_wmclass_differs(self):
        launch = qs.resolve_launch(client("0x9", "org.telegram.desktop", "Chat", 1, (1, "1")), FakeProc({}), self.desktop)
        self.assertEqual(launch["command"], ["Telegram", "--"])

    def test_terminals_restore_their_shell_directory_not_the_tui(self):
        home = os.path.expanduser("~")
        proc = FakeProc({
            100: {"exe": "/usr/bin/ghostty", "cmdline": ["ghostty", "--class=org.omarchy.agent", "-e", "agy"], "children": [101, 150]},
            101: {"shell": True, "cwd": home},
            150: {"shell": False, "cwd": "/"},
        })
        launch = qs.resolve_launch(client("0xa", "org.omarchy.agent", "agy", 100, (1, "1")), proc, self.desktop)
        self.assertEqual(launch["command"], ["ghostty", "--class=org.omarchy.agent", "--working-directory=" + home])
        self.assertNotIn("agy", launch["command"])
        foot = qs.resolve_launch(client("0xb", "foot", "x", 200, (1, "1")), FakeProc({200: {"exe": "/usr/bin/foot", "children": []}}), self.desktop)
        self.assertEqual(foot["command"], ["foot"])

    def test_slashes_inside_words_are_not_directories(self):
        proc = FakeProc({300: {"exe": "/usr/bin/ghostty", "children": [301, 302]}, 301: {"shell": True}, 302: {"shell": True}})
        for title in ("feat/tmp", "build 1/var", "a/b ~nope"):
            launch = qs.resolve_launch(client("0xe", "com.mitchellh.ghostty", title, 300, (1, "1")), proc, self.desktop, windows_for_pid=2)
            self.assertEqual(launch["command"], ["ghostty"], title)
        launch = qs.resolve_launch(client("0xf", "com.mitchellh.ghostty", "u@host:/tmp", 300, (1, "1")), proc, self.desktop, windows_for_pid=2)
        self.assertEqual(launch["cwd"], os.path.realpath("/tmp"))

    def test_shared_terminal_process_uses_directory_titles(self):
        proc = FakeProc({300: {"exe": "/usr/bin/ghostty", "children": [301, 302]}, 301: {"shell": True, "cwd": "/"}, 302: {"shell": True, "cwd": "/"}})
        launch = qs.resolve_launch(client("0xc", "com.mitchellh.ghostty", "~", 300, (1, "1")), proc, self.desktop, windows_for_pid=2)
        self.assertEqual(launch["cwd"], os.path.realpath(os.path.expanduser("~")))
        launch = qs.resolve_launch(client("0xd", "com.mitchellh.ghostty", "nvim", 300, (1, "1")), proc, self.desktop, windows_for_pid=2)
        self.assertEqual(launch["command"], ["ghostty"])


class CaptureTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = self.state
        modes = os.path.join(self.state, "omarchy", "workspace-layouts")
        os.makedirs(modes)
        with open(os.path.join(modes, "11.lua"), "w") as handle:
            handle.write('o.workspace_mode({ workspace = "11", mode = "scrolling" })\n')
        with open(os.path.join(modes, "default.lua"), "w") as handle:
            handle.write('o.workspace_mode({ default = "floating" })\n')
        self.desktop = qs.DesktopIndex([desktop_dir(DESKTOP)])
        self.clients = [
            client("0x1", "chrome-youtube.com__-Default", '<img src="https://example.invalid/t"> <b>YT</b>', 5, (1, "1"), monitor=0, at=(1932, 1600), size=(681, 468)),
            client("0x2", "helium", "Browse", 5, (11, "11"), at=(1548, 1038)),
            client("0x3", "com.onepassword.OnePassword", "1Password", 6, (-97, "special:scratchpad")),
            client("0x4", "xdg-desktop-portal-gtk", "Open File", 7, (11, "11")),
            client("0x5", "org.telegram.desktop", "Chat", 8, (1, "1"), monitor=0, at=(2627, 1118)),
            client("0x6", "helium", "Picture-in-Picture", 5, (11, "11")),
            client("0x7", "org.omarchy.screensaver", "omarchy-screensaver", 9, (-96, "special:screensaver-eDP-1"), monitor=0),
            client("0x8", "Slack", "Slack", 10, (11, "11"), at=(12, 1038)),
        ]

    def snapshot(self, **kwargs):
        return qs.capture(FakeHypr(self.clients), FakeProc({}), self.desktop, **kwargs)

    def test_filters_noise_special_workspaces_and_ignored_apps(self):
        snap = self.snapshot(ignore=qs.parse_ignore("slack, Foo*"))
        classes = [w["class"] for w in snap["windows"]]
        self.assertEqual(classes, ["chrome-youtube.com__-Default", "org.telegram.desktop", "helium"])
        snap = self.snapshot(include_special=True)
        self.assertIn("com.onepassword.OnePassword", [w["class"] for w in snap["windows"]])
        self.assertNotIn("org.omarchy.screensaver", [w["class"] for w in snap["windows"]])

    def test_records_modes_monitors_and_relative_geometry(self):
        snap = self.snapshot()
        modes = {ws["name"]: ws["mode"] for ws in snap["workspaces"]}
        self.assertEqual(modes, {"1": "floating", "11": "scrolling"})
        youtube = next(w for w in snap["windows"] if w["class"].startswith("chrome-"))
        self.assertEqual(youtube["monitor"], "eDP-1")
        self.assertEqual(youtube["at"], [12, 520])
        self.assertEqual(snap["focusedMonitor"], "DP-2")
        self.assertEqual({m["name"]: (m["width"], m["height"]) for m in snap["monitors"]}, {"eDP-1": (1600, 1000), "DP-2": (1920, 1080)})
        self.assertEqual({m["name"]: m["activeWorkspace"] for m in snap["monitors"]}, {"eDP-1": "1", "DP-2": "11"})

    def test_markup_titles_are_stored_verbatim_as_data(self):
        snap = self.snapshot()
        titles = [w["title"] for w in snap["windows"]]
        self.assertIn('<img src="https://example.invalid/t"> <b>YT</b>', titles)

    def test_unresolvable_windows_are_flagged(self):
        snap = self.snapshot()
        slack = next(w for w in snap["windows"] if w["class"] == "Slack")
        self.assertEqual(slack["warning"], "unresolved")
        telegram = next(w for w in snap["windows"] if w["class"] == "org.telegram.desktop")
        self.assertIn(telegram["warning"], ("", "missing"))


class StoreCommandTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = self.state
        self.store = qs.Store()
        self.desktop_root = desktop_dir(DESKTOP)
        self.clients = [
            client("0x1", "helium", "Browse", 5, (11, "11")),
            client("0x2", "org.telegram.desktop", "Chat", 8, (1, "1"), monitor=0),
        ]

    def run_cmd(self, *argv):
        args = qs.parser().parse_args(list(argv))
        return qs.command(args, self.store, hypr_factory=lambda: FakeHypr(self.clients), proc=FakeProc({}),
                          desktop_factory=lambda: qs.DesktopIndex([self.desktop_root]))

    def test_save_rename_update_prune_and_delete(self):
        out = self.run_cmd("save", "  Work  ")
        self.assertEqual(out["saved"], "work")
        self.assertEqual(out["slots"][0]["name"], "Work")
        with open(os.path.join(self.state, "omarchy", "quick-start", "slots", "work.json")) as handle:
            self.assertEqual(json.load(handle)["windows"][0]["class"], "org.telegram.desktop")
        with self.assertRaises(qs.QuickStartError):
            self.run_cmd("save", "work")
        self.run_cmd("rename", "work", "Office <b>")
        self.assertEqual(self.store.get("work")["name"], "Office <b>")
        self.clients.append(client("0x3", "foot", "~", 11, (11, "11")))
        out = self.run_cmd("update", "work")
        self.assertEqual(len(out["slots"][0]["windows"]), 3)
        self.assertEqual(out["slots"][0]["name"], "Office <b>")
        key = next(w["key"] for w in out["slots"][0]["windows"] if w["class"] == "org.telegram.desktop")
        out = self.run_cmd("remove-window", "work", key)
        self.assertEqual([ws["name"] for ws in out["slots"][0]["workspaces"]], ["11"])
        out = self.run_cmd("delete", "work")
        self.assertEqual(out["slots"], [])

    def test_set_command_parses_into_argv_without_a_shell(self):
        self.run_cmd("save", "Personal")
        key = self.store.get("personal")["windows"][0]["key"]
        self.run_cmd("set-command", "personal", key, "sh -c 'echo $(id)' ; reboot")
        window = self.store.get("personal")["windows"][0]
        self.assertEqual(window["launch"]["command"], ["sh", "-c", "echo $(id)", ";", "reboot"])
        with self.assertRaises(qs.QuickStartError):
            self.run_cmd("set-command", "personal", key, "unterminated 'quote")

    def test_names_and_patterns_starting_with_a_dash_are_values(self):
        out = self.run_cmd("save", "--ignore=-x*", "--", "-work")
        self.assertEqual(out["slots"][0]["name"], "-work")
        out = self.run_cmd("rename", "--", out["saved"], "-h")
        self.assertEqual(out["slots"][0]["name"], "-h")

    def test_rejects_path_traversal_ids(self):
        with self.assertRaises(qs.QuickStartError):
            self.store.get("../../etc/passwd")


class SplitTreeTests(unittest.TestCase):
    def names(self, node):
        if isinstance(node, tuple):
            return (node[0], self.names(node[1]), self.names(node[2]))
        return node["key"]

    def tree(self, rects):
        return qs.split_tree([({"key": k}, x, y, w, h) for k, (x, y, w, h) in rects.items()])

    def test_stacked_pair_beside_a_column(self):
        tree = self.tree({"a": (12, 38, 681, 468), "b": (12, 520, 681, 468), "c": (707, 38, 881, 950)})
        self.assertEqual(self.names(tree), ("h", ("v", "a", "b"), "c"))
        self.assertEqual(qs.first_leaf(tree)["key"], "a")

    def test_column_beside_a_stacked_pair(self):
        tree = self.tree({"e": (12, 38, 1300, 1030), "f": (1326, 38, 582, 300), "g": (1326, 352, 582, 716)})
        self.assertEqual(self.names(tree), ("h", "e", ("v", "f", "g")))

    def test_overlapping_windows_have_no_tree(self):
        self.assertIsNone(self.tree({"a": (0, 0, 800, 800), "b": (400, 400, 800, 800)}))


class RestoreTests(unittest.TestCase):
    def setUp(self):
        self.state = tempfile.mkdtemp()
        os.environ["XDG_STATE_HOME"] = self.state
        self.events = []
        self.clock = FakeClock()
        self.slot = {
            "name": "Work",
            "monitors": [
                {"name": "DP-2", "x": 0, "y": 1000, "width": 1920, "height": 1080, "activeWorkspace": "11", "focused": True},
                {"name": "HDMI-9", "x": 0, "y": 0, "width": 3840, "height": 2160, "activeWorkspace": "3", "focused": False},
            ],
            "focusedMonitor": "DP-2",
            "workspaces": [
                {"name": "11", "id": 11, "monitor": "DP-2", "mode": "scrolling"},
                {"name": "3", "id": 3, "monitor": "HDMI-9", "mode": "floating"},
            ],
            "windows": [
                {"key": "w1", "class": "helium", "initialClass": "helium", "title": "Browse", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "focused": True, "at": [12, 38], "size": [1200, 1030], "order": 0,
                 "launch": {"kind": "desktop", "command": ["helium"], "perWindow": False}},
                {"key": "w2", "class": "foot", "initialClass": "foot", "title": "~", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "at": [1220, 38], "size": [690, 1030], "order": 1,
                 "launch": {"kind": "terminal", "command": ["foot", "--working-directory=/home/u/a b"], "perWindow": True}},
                {"key": "w3", "class": "mpv", "initialClass": "mpv", "title": "video", "workspace": "3", "monitor": "HDMI-9",
                 "floating": True, "pinned": True, "at": [3000, 2000], "size": [1920, 1080], "order": 2,
                 "launch": {"kind": "desktop", "command": ["mpv"], "perWindow": False}},
                {"key": "w4", "class": "ghost", "initialClass": "ghost", "title": "never", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "at": [0, 0], "size": [10, 10], "order": 3,
                 "launch": {"kind": "desktop", "command": ["ghost-app"], "perWindow": False}},
                {"key": "w5", "class": "nothing", "initialClass": "nothing", "title": "n", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "at": [0, 0], "size": [10, 10], "order": 4,
                 "launch": {"kind": "unresolved", "command": [], "perWindow": False}},
            ],
        }
        self.unrelated = client("0xother", "obsidian", "Notes", 3, (11, "11"))
        self.helium = client("0xhelium", "helium", "Browse", 5, (12, "12"))
        self.hypr = FakeHypr([self.unrelated, self.helium], spawn={"foot": "foot", "mpv": "mpv"})

    def restore(self):
        restorer = qs.Restorer(self.hypr, self.slot, self.events.append, clock=self.clock, sleep=self.clock.sleep, run=lambda argv: None)
        restorer.run_restore()
        return restorer

    def test_smart_merge_reuses_launches_places_and_warns(self):
        restorer = self.restore()
        lua = "\n".join(self.hypr.evals)
        # Running helium is reused and moved, never relaunched; nothing is closed.
        self.assertIn('hl.dsp.window.move({ workspace = "11", follow = false, window = "address:0xhelium" })', lua)
        self.assertNotIn('exec_cmd("helium"', lua)
        self.assertNotIn("close", lua)
        # Missing apps launch silently into their workspace, quoted safely.
        self.assertIn('hl.dsp.exec_cmd("foot \'--working-directory=/home/u/a b\'", { workspace = "11 silent" })', lua)
        # Layout mode applied before windows arrive, only where it differs.
        self.assertEqual(self.hypr.evals[0], 'o.workspace_mode({ workspace = "3", mode = "floating" })')
        self.assertNotIn('workspace = "11", mode', lua)
        with open(os.path.join(self.state, "omarchy", "workspace-layouts", "3.lua")) as handle:
            self.assertEqual(handle.read(), 'o.workspace_mode({ workspace = "3", mode = "floating" })\n')
        self.assertEqual(restorer.assigned["w1"], "0xhelium")
        self.assertIn("w2", restorer.assigned)
        self.assertIn("w3", restorer.assigned)
        codes = [e["code"] for e in self.events if e["event"] == "warning"]
        self.assertEqual(sorted(codes), ["monitorFallback", "timeout", "unresolved"])
        done = self.events[-1]
        self.assertEqual((done["event"], done["done"], done["total"]), ("done", 3, 5))
        progress = [e["done"] for e in self.events if e["event"] == "progress"]
        self.assertEqual(progress, sorted(progress))

    def test_disconnected_monitor_scales_floating_geometry_onto_the_focused_one(self):
        self.restore()
        lua = "\n".join(self.hypr.evals)
        # HDMI-9 (3840x2160) is gone; DP-2 (1920x1080 at 0,1000) takes it at half scale.
        address = self.hypr._clients[-1]["address"]
        self.assertIn('hl.dsp.window.resize({ x = 960, y = 540, relative = false, window = "address:%s" })' % address, lua)
        self.assertIn('hl.dsp.window.move({ x = 960, y = 1540, relative = false, window = "address:%s" })' % address, lua)
        self.assertIn('hl.dsp.window.pin({ action = "on", window = "address:%s" })' % address, lua)
        # Saved focused monitor's workspace is focused last.
        focus = [line for line in self.hypr.evals if "hl.dsp.focus" in line][-1]
        self.assertTrue(focus.index('workspace = "3"') < focus.index('workspace = "11"'))
        # The window focused at save time is focused last.
        self.assertTrue(focus.rstrip().endswith('hl.dispatch(hl.dsp.focus({ window = "address:0xhelium" }));'))

    def test_windows_already_in_place_get_no_configure_or_focus_events(self):
        slot = {
            "name": "Same", "focusedMonitor": "DP-2",
            "monitors": [{"name": "DP-2", "x": 0, "y": 1000, "width": 1920, "height": 1080, "activeWorkspace": "11", "focused": True}],
            "workspaces": [{"name": "11", "id": 11, "monitor": "DP-2", "mode": "scrolling"}],
            "windows": [
                {"key": "w1", "class": "helium", "initialClass": "helium", "title": "Browse", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "focused": True, "at": [12, 38], "size": [1200, 1030], "order": 0,
                 "launch": {"kind": "desktop", "command": ["helium"], "perWindow": False}},
                {"key": "w2", "class": "mpv", "initialClass": "mpv", "title": "video", "workspace": "11", "monitor": "DP-2",
                 "floating": True, "pinned": False, "at": [100, 100], "size": [640, 360], "order": 1,
                 "launch": {"kind": "desktop", "command": ["mpv"], "perWindow": False}},
            ],
        }
        hypr = FakeHypr([
            client("0xa", "helium", "Browse", 5, (11, "11"), at=(12, 1038), size=(1200, 1030), focusHistoryID=0),
            client("0xb", "mpv", "video", 6, (11, "11"), at=(100, 1100), size=(640, 360), floating=True, focusHistoryID=1),
        ])
        qs.Restorer(hypr, slot, self.events.append, clock=self.clock, sleep=self.clock.sleep, run=lambda argv: None).run_restore()
        sent = "\n".join(hypr.evals)
        for verb in ("resize", "window.move", "focus", "float", "exec_cmd"):
            self.assertNotIn(verb, sent)
        self.assertEqual(self.events[-1]["event"], "done")

    def test_each_window_keeps_its_exact_title_match_first(self):
        slot = {
            "name": "Two", "focusedMonitor": "DP-2", "monitors": [], "workspaces": [],
            "windows": [
                {"key": "w1", "class": "foot", "initialClass": "foot", "title": "old title", "workspace": "11", "monitor": "DP-2",
                 "floating": False, "pinned": False, "at": [0, 0], "size": [10, 10], "order": 0, "launch": {"command": []}},
                {"key": "w2", "class": "foot", "initialClass": "foot", "title": "build", "workspace": "12", "monitor": "DP-2",
                 "floating": False, "pinned": False, "at": [0, 0], "size": [10, 10], "order": 1, "launch": {"command": []}},
            ],
        }
        hypr = FakeHypr([client("0xbuild", "foot", "build", 1, (12, "12")), client("0xother", "foot", "renamed", 2, (13, "13"))])
        restorer = qs.Restorer(hypr, slot, self.events.append, clock=self.clock, sleep=self.clock.sleep, run=lambda argv: None)
        restorer.monitor_map = {}
        restorer.reuse_existing([dict(w) for w in slot["windows"]])
        self.assertEqual(restorer.assigned, {"w2": "0xbuild", "w1": "0xother"})

    def test_cancel_during_settle_skips_the_layout_pass(self):
        restorer = qs.Restorer(self.hypr, self.slot, self.events.append, clock=self.clock, sleep=self.clock.sleep, run=lambda argv: None)
        restorer.wait_until_settled = lambda: setattr(restorer, "cancelled", True)
        restorer.run_restore()
        self.assertEqual(self.events[-1]["event"], "cancelled")
        self.assertFalse(any("relative = false" in lua or "hl.dsp.focus" in lua for lua in self.hypr.evals))

    def test_cancel_stops_before_the_layout_pass(self):
        restorer = qs.Restorer(self.hypr, self.slot, self.events.append, clock=self.clock, sleep=self.clock.sleep, run=lambda argv: None)
        original = restorer.launch

        def launch_then_cancel(window):
            restorer.cancelled = True
            return original(window)

        restorer.launch = launch_then_cancel
        restorer.run_restore()
        self.assertEqual(self.events[-1]["event"], "cancelled")
        self.assertFalse(any("relative = false" in lua for lua in self.hypr.evals))


if __name__ == "__main__":
    unittest.main()
