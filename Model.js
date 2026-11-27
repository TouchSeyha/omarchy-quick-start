// Pure helpers for the Quick-Start panel: validating what the backend
// prints, grouping slot previews, and building display strings.
// Kept Qt-free so it can be unit-tested directly under Node.
//
// Everything that reaches here came from a subprocess and, before that,
// from window titles any app can set. Treat it as untrusted: bound sizes,
// keep only known fields, and never hand it to a rich-text sink.

var MAX_LINE = 262144
var MAX_SLOTS = 64
var MAX_WINDOWS = 128
var MAX_TEXT = 256
var MAX_NAME = 64

var WARNING_CODES = ["unresolved", "timeout", "launchFailed", "monitorFallback", "layoutFailed", "layoutUnsaved"]

function clipText(value, limit) {
  if (value === undefined || value === null) return ""
  var s = String(value)
  // Drop control characters; a title can carry anything.
  s = s.replace(/[\u0000-\u0008\u000B-\u001F\u007F]/g, "")
  return s.length > limit ? s.substring(0, limit) : s
}

function toInt(value, fallback) {
  var n = Number(value)
  return isFinite(n) ? Math.round(n) : (fallback || 0)
}

function slotId(value) {
  var s = String(value || "")
  return /^[a-z0-9-]{1,64}$/.test(s) ? s : ""
}

function windowKey(value) {
  var s = String(value || "")
  return /^w[0-9]{1,4}$/.test(s) ? s : ""
}

function cleanWindow(raw) {
  if (!raw || typeof raw !== "object") return null
  var key = windowKey(raw.key)
  if (!key) return null
  var launch = raw.launch && typeof raw.launch === "object" ? raw.launch : {}
  var command = Array.isArray(launch.command) ? launch.command.slice(0, 64).map(function(a) { return clipText(a, 4096) }) : []
  var warning = String(raw.warning || "")
  return {
    key: key,
    cls: clipText(raw["class"], 128),
    title: clipText(raw.title, MAX_TEXT),
    workspace: clipText(raw.workspace, 64),
    monitor: clipText(raw.monitor, 64),
    floating: raw.floating === true,
    order: toInt(raw.order, 0),
    kind: clipText(launch.kind, 32),
    command: command,
    commandText: commandText(command),
    warning: warning === "unresolved" || warning === "missing" ? warning : ""
  }
}

function cleanSlot(raw) {
  if (!raw || typeof raw !== "object") return null
  var id = slotId(raw.id)
  if (!id || !Array.isArray(raw.windows)) return null
  var windows = []
  for (var i = 0; i < raw.windows.length && windows.length < MAX_WINDOWS; i++) {
    var w = cleanWindow(raw.windows[i])
    if (w) windows.push(w)
  }
  var workspaces = []
  var rawWs = Array.isArray(raw.workspaces) ? raw.workspaces : []
  for (var j = 0; j < rawWs.length && j < 64; j++) {
    var ws = rawWs[j] || {}
    workspaces.push({ name: clipText(ws.name, 64), monitor: clipText(ws.monitor, 64), mode: clipText(ws.mode, 16) })
  }
  var warnings = 0
  for (var k = 0; k < windows.length; k++) if (windows[k].warning) warnings++
  return {
    id: id,
    name: clipText(raw.name, MAX_NAME) || id,
    updated: toInt(raw.updated, 0),
    windows: windows,
    workspaces: workspaces,
    warningCount: warnings
  }
}

// Parse the single JSON object a non-restore command prints.
function parseResult(raw) {
  var s = String(raw || "").trim()
  if (s === "" || s.length > 8 * MAX_LINE) return { ok: false, error: "", slots: null }
  var data
  try {
    data = JSON.parse(s.split("\n").pop())
  } catch (e) {
    return { ok: false, error: "", slots: null }
  }
  if (!data || typeof data !== "object") return { ok: false, error: "", slots: null }
  var slots = null
  if (Array.isArray(data.slots)) {
    slots = []
    for (var i = 0; i < data.slots.length && slots.length < MAX_SLOTS; i++) {
      var slot = cleanSlot(data.slots[i])
      if (slot) slots.push(slot)
    }
  }
  return {
    ok: data.ok === true,
    error: clipText(data.error, 200),
    slots: slots,
    saved: slotId(data.saved)
  }
}

// Parse one line of `restore` progress output.
function parseEvent(line) {
  var s = String(line || "")
  if (s.length === 0 || s.length > MAX_LINE) return null
  var data
  try {
    data = JSON.parse(s)
  } catch (e) {
    return null
  }
  if (!data || typeof data !== "object") return null
  var kind = String(data.event || "")
  if (["start", "progress", "warning", "done", "cancelled", "error"].indexOf(kind) === -1) return null
  var event = {
    event: kind,
    done: Math.max(0, toInt(data.done, 0)),
    total: Math.max(0, Math.min(MAX_WINDOWS, toInt(data.total, 0))),
    name: clipText(data.name, MAX_NAME),
    error: clipText(data.error, 200)
  }
  if (kind === "warning") {
    var code = String(data.code || "")
    if (WARNING_CODES.indexOf(code) === -1) return null
    event.code = code
    event.cls = clipText(data.cls, 128)
    event.title = clipText(data.title, MAX_TEXT)
    event.target = clipText(data.target, 64)
    event.monitors = Array.isArray(data.monitors) ? data.monitors.slice(0, 8).map(function(m) { return clipText(m, 64) }).join(", ") : ""
  }
  return event
}

function commandText(argv) {
  if (!Array.isArray(argv)) return ""
  return argv.map(function(arg) {
    var s = String(arg)
    if (s !== "" && /^[A-Za-z0-9_@%+=:,./-]+$/.test(s)) return s
    return "'" + s.replace(/'/g, "'\"'\"'") + "'"
  }).join(" ")
}

// A session name as the user typed it, trimmed and bounded.
function cleanName(value) {
  return clipText(value, 512).replace(/\s+/g, " ").trim().substring(0, MAX_NAME)
}

// Windows grouped by workspace, in the order the slot saved them.
function listOf(value) {
  // Values handed through a Repeater's modelData come back as Qt sequences,
  // which are array-like but fail Array.isArray.
  if (!value || typeof value.length !== "number") return []
  var out = []
  for (var i = 0; i < value.length && i < 1024; i++) out.push(value[i])
  return out
}

function groupWindows(slot) {
  if (!slot) return []
  var groups = []
  var byName = {}
  var modes = {}
  var monitors = {}
  var ws = listOf(slot.workspaces)
  for (var i = 0; i < ws.length; i++) {
    modes[ws[i].name] = ws[i].mode
    monitors[ws[i].name] = ws[i].monitor
  }
  var windows = listOf(slot.windows).sort(function(a, b) { return a.order - b.order })
  for (var j = 0; j < windows.length; j++) {
    var w = windows[j]
    var group = byName[w.workspace]
    if (!group) {
      group = { workspace: w.workspace, mode: modes[w.workspace] || "", monitor: monitors[w.workspace] || w.monitor, windows: [] }
      byName[w.workspace] = group
      groups.push(group)
    }
    group.windows.push(w)
  }
  groups.sort(function(a, b) { return workspaceRank(a.workspace) - workspaceRank(b.workspace) })
  return groups
}

function workspaceRank(name) {
  var n = Number(name)
  if (isFinite(n) && String(name) !== "") return n
  if (String(name).indexOf("special:") === 0) return 100000
  return 50000
}

// "Workspace 11", or the special/named workspace's own name.
function workspaceLabel(name, tr) {
  var s = String(name || "")
  if (s.indexOf("special:") === 0) return tr("workspace.special").arg(s.substring(8))
  if (s.indexOf("name:") === 0) return tr("workspace.named").arg(s.substring(5))
  return tr("workspace.number").arg(s)
}

function windowLabel(w) {
  if (!w) return ""
  var title = String(w.title || "").trim()
  var cls = String(w.cls || "").trim()
  return title !== "" ? title : cls
}

function appLabel(cls) {
  var s = String(cls || "")
  var webapp = /^(chrome|brave|msedge|vivaldi|chromium|helium)-(.+?)_.*-[^-]+$/.exec(s)
  if (webapp) return webapp[2]
  var parts = s.split(".")
  if (parts.length < 3) return s
  // Reverse-DNS ids end in the app's name, unless that is a generic word
  // like org.telegram.desktop.
  var last = parts[parts.length - 1]
  return ["desktop", "app", "client", "gui", "Desktop", "App"].indexOf(last) !== -1 ? parts[parts.length - 2] : last
}

function progressLabel(done, total) {
  return String(Math.min(done, total)) + "/" + String(total)
}

function progressFraction(done, total) {
  return total > 0 ? Math.max(0, Math.min(1, done / total)) : 0
}

function warningText(event, tr) {
  if (!event) return ""
  var app = appLabel(event.cls) || event.title
  if (event.code === "timeout") return tr("warning.timeout").arg(app)
  if (event.code === "unresolved") return tr("warning.unresolved").arg(app)
  if (event.code === "launchFailed") return tr("warning.launchFailed").arg(app)
  if (event.code === "monitorFallback") return tr("warning.monitorFallback").arg(event.monitors).arg(event.target)
  if (event.code === "layoutFailed") return tr("warning.layoutFailed")
  if (event.code === "layoutUnsaved") return tr("warning.layoutUnsaved")
  return ""
}

if (typeof module !== "undefined") {
  module.exports = {
    clipText: clipText,
    cleanSlot: cleanSlot,
    cleanWindow: cleanWindow,
    parseResult: parseResult,
    parseEvent: parseEvent,
    commandText: commandText,
    cleanName: cleanName,
    groupWindows: groupWindows,
    workspaceLabel: workspaceLabel,
    windowLabel: windowLabel,
    appLabel: appLabel,
    progressLabel: progressLabel,
    progressFraction: progressFraction,
    warningText: warningText
  }
}
