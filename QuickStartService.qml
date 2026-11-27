pragma Singleton

import QtQuick
import Quickshell
import Quickshell.Io
import "Model.js" as Model

// One backend for every bar: a bar surface exists per monitor, and they all
// show the same slots and the same restore. Nothing here runs on a timer;
// the backend only runs when the user asks for something.
Item {
  id: root

  readonly property string python3Path: "/usr/bin/python3"
  readonly property string backendPath: {
    var s = String(Qt.resolvedUrl("./quick_start.py"))
    if (s.indexOf("file://") === 0) s = s.substring(7)
    if (!s || s.charAt(0) !== "/" || s.indexOf("..") !== -1) return ""
    return s
  }

  property var slots: []
  property bool loaded: false
  property bool busy: false
  property string lastError: ""
  property string lastSaved: ""
  property int revision: 0

  property bool restoring: false
  property string restoreId: ""
  property string restoreName: ""
  property int progressDone: 0
  property int progressTotal: 0
  property var restoreWarnings: []
  property string restoreOutcome: ""   // "", "done", "cancelled", "error"
  property string restoreError: ""

  property var queue: []

  function ensureLoaded() {
    if (!loaded && !busy) refresh()
  }

  function refresh() { enqueue(["list"]) }

  function flags(includeSpecial, ignore) {
    var out = []
    if (includeSpecial === true) out.push("--include-special")
    var list = Model.clipText(ignore, 2048)
    if (list !== "") out.push("--ignore", list)
    return out
  }

  function save(name, includeSpecial, ignore) {
    var clean = Model.cleanName(name)
    if (clean === "") return false
    enqueue(["save", clean].concat(flags(includeSpecial, ignore)))
    return true
  }

  function update(id, includeSpecial, ignore) { enqueue(["update", id].concat(flags(includeSpecial, ignore))) }

  function rename(id, name) {
    var clean = Model.cleanName(name)
    if (clean === "") return false
    enqueue(["rename", id, clean])
    return true
  }

  function remove(id) { enqueue(["delete", id]) }
  function removeWindow(id, key) { enqueue(["remove-window", id, key]) }
  function setCommand(id, key, command) {
    var text = Model.clipText(command, 4096).trim()
    if (text === "") return false
    enqueue(["set-command", id, key, text])
    return true
  }

  function slotById(id) {
    for (var i = 0; i < slots.length; i++) if (slots[i].id === id) return slots[i]
    return null
  }

  function slotByName(name) {
    var wanted = String(name || "").toLowerCase()
    for (var i = 0; i < slots.length; i++) if (slots[i].name.toLowerCase() === wanted) return slots[i]
    return null
  }

  function enqueue(args) {
    if (!root.backendPath) {
      root.lastError = "backend"
      return
    }
    var next = root.queue.slice()
    next.push(args)
    root.queue = next
    pump()
  }

  function pump() {
    if (actionProc.running || root.queue.length === 0) return
    var args = root.queue[0]
    root.queue = root.queue.slice(1)
    root.busy = true
    actionProc.command = [root.python3Path, "-I", "-S", "-B", root.backendPath].concat(args)
    actionProc.running = true
  }

  function handleResult(raw) {
    var result = Model.parseResult(raw)
    if (result.slots !== null) {
      root.slots = result.slots
      root.loaded = true
      root.revision++
    }
    root.lastError = result.ok ? "" : (result.error || "generic")
    if (result.ok && result.saved) root.lastSaved = result.saved
  }

  function restore(id) {
    var slot = slotById(id)
    if (!slot || root.restoring || !root.backendPath) return false
    root.restoring = true
    root.restoreId = slot.id
    root.restoreName = slot.name
    root.progressDone = 0
    root.progressTotal = slot.windows.length
    root.restoreWarnings = []
    root.restoreOutcome = ""
    root.restoreError = ""
    restoreProc.command = [root.python3Path, "-I", "-S", "-B", root.backendPath, "restore", slot.id]
    restoreProc.running = true
    return true
  }

  function cancelRestore() {
    if (restoreProc.running) restoreProc.signal(15)
  }

  function dismissOutcome() {
    if (root.restoring) return
    root.restoreOutcome = ""
    root.restoreWarnings = []
    root.restoreError = ""
  }

  function handleEvent(line) {
    var event = Model.parseEvent(line)
    if (!event) return
    if (event.event === "start" || event.event === "progress") {
      if (event.total > 0) root.progressTotal = event.total
      root.progressDone = Math.min(event.done, root.progressTotal)
    } else if (event.event === "warning") {
      if (root.restoreWarnings.length < 32) {
        var next = root.restoreWarnings.slice()
        next.push(event)
        root.restoreWarnings = next
      }
    } else if (event.event === "done") {
      root.progressDone = Math.min(event.done, root.progressTotal)
      root.restoreOutcome = "done"
    } else if (event.event === "cancelled") {
      root.restoreOutcome = "cancelled"
    } else if (event.event === "error") {
      root.restoreOutcome = "error"
      root.restoreError = event.error
    }
  }

  Process {
    id: actionProc
    command: []
    running: false
    stdout: StdioCollector {
      waitForEnd: true
      onStreamFinished: root.handleResult(text)
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      root.busy = false
      Qt.callLater(root.pump)
    }
  }

  Process {
    id: restoreProc
    command: []
    running: false
    stdout: SplitParser {
      onRead: function(line) { root.handleEvent(line) }
    }
    stderr: StdioCollector { waitForEnd: true }
    onExited: function(exitCode) {
      if (root.restoreOutcome === "") root.restoreOutcome = exitCode === 0 ? "done" : "error"
      root.restoring = false
    }
  }
}
