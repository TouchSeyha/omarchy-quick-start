const test = require("node:test")
const assert = require("node:assert/strict")
const fs = require("node:fs")
const path = require("node:path")
const Model = require("../Model.js")

const tr = (key) => ({
  "workspace.number": "Workspace %1",
  "workspace.special": "Special · %1",
  "workspace.named": "Workspace %1",
  "warning.timeout": "%1 did not open within 15 seconds",
  "warning.monitorFallback": "%1 is not connected; its workspaces went to %2",
}[key] || key)
// QML strings have .arg(); give plain JS strings the same for the tests.
String.prototype.arg = function(value) {
  const max = (this.match(/%(\d)/g) || []).map((m) => Number(m[1])).reduce((a, b) => Math.min(a, b), 9)
  return this.replace(new RegExp("%" + max, "g"), String(value))
}

const MARKUP = '<img src="https://example.invalid/tracker"> <b>Title</b>'

function payload(overrides = {}) {
  return JSON.stringify(Object.assign({
    ok: true,
    slots: [{
      id: "work",
      name: MARKUP,
      updated: 1795757421,
      workspaces: [{ name: "11", monitor: "DP-2", mode: "scrolling" }, { name: "special:scratchpad", monitor: "DP-2", mode: "dwindle" }, { name: "1", monitor: "eDP-1", mode: "dwindle" }],
      windows: [
        { key: "w2", class: "helium", title: MARKUP, workspace: "11", monitor: "DP-2", order: 2, launch: { kind: "desktop", command: ["helium"] }, warning: "" },
        { key: "w1", class: "org.telegram.desktop", title: "Chat\u0007", workspace: "1", monitor: "eDP-1", order: 1, launch: { kind: "desktop", command: ["Telegram", "--"] }, warning: "missing" },
        { key: "w3", class: "Slack", title: "", workspace: "special:scratchpad", monitor: "DP-2", order: 3, launch: { kind: "unresolved", command: [] }, warning: "unresolved" },
        { key: "../evil", class: "x", workspace: "1" },
      ],
    }],
  }, overrides))
}

test("parseResult keeps known fields, drops bad keys, and leaves markup as plain data", () => {
  const result = Model.parseResult(payload())
  assert.equal(result.ok, true)
  assert.equal(result.slots.length, 1)
  const slot = result.slots[0]
  assert.equal(slot.name, MARKUP)
  assert.equal(slot.windows.length, 3)
  assert.equal(slot.warningCount, 2)
  assert.equal(slot.windows[1].title, "Chat")
  assert.equal(slot.windows[0].commandText, "helium")
})

test("parseResult rejects junk, bad ids, and oversized output", () => {
  assert.equal(Model.parseResult("not json").ok, false)
  assert.equal(Model.parseResult("").slots, null)
  const bad = Model.parseResult(JSON.stringify({ ok: true, slots: [{ id: "../../etc", windows: [] }] }))
  assert.deepEqual(bad.slots, [])
  assert.equal(Model.parseResult("x".repeat(3000000)).ok, false)
  const err = Model.parseResult(JSON.stringify({ ok: false, error: "A session with that name already exists" }))
  assert.equal(err.ok, false)
  assert.equal(err.error, "A session with that name already exists")
})

test("parseEvent validates restore progress lines", () => {
  assert.deepEqual(
    Model.parseEvent('{"event": "progress", "done": 6, "total": 9}'),
    { event: "progress", done: 6, total: 9, name: "", error: "" },
  )
  assert.equal(Model.parseEvent('{"event": "rm -rf"}'), null)
  assert.equal(Model.parseEvent('{"event": "warning", "code": "<b>"}'), null)
  assert.equal(Model.parseEvent("garbage"), null)
  const warning = Model.parseEvent(JSON.stringify({ event: "warning", code: "timeout", cls: "chrome-x.com__-Default", title: MARKUP }))
  assert.equal(Model.warningText(warning, tr), "x.com did not open within 15 seconds")
  const fallback = Model.parseEvent(JSON.stringify({ event: "warning", code: "monitorFallback", monitors: ["HDMI-A-1"], target: "eDP-1" }))
  assert.equal(Model.warningText(fallback, tr), "HDMI-A-1 is not connected; its workspaces went to eDP-1")
})

test("groupWindows orders workspaces numerically with specials last and accepts array-likes", () => {
  const slot = Model.parseResult(payload()).slots[0]
  const groups = Model.groupWindows(slot)
  assert.deepEqual(groups.map((g) => g.workspace), ["1", "11", "special:scratchpad"])
  assert.equal(groups[1].mode, "scrolling")
  const arrayLike = { windows: { length: 1, 0: slot.windows[0] }, workspaces: { length: 0 } }
  assert.equal(Model.groupWindows(arrayLike).length, 1)
  assert.equal(Model.workspaceLabel("special:scratchpad", tr), "Special · scratchpad")
  assert.equal(Model.workspaceLabel("11", tr), "Workspace 11")
})

test("labels, names, and command text", () => {
  assert.equal(Model.appLabel("org.telegram.desktop"), "telegram")
  assert.equal(Model.appLabel("dev.zed.Zed"), "Zed")
  assert.equal(Model.appLabel("chrome-music.youtube.com__-Default"), "music.youtube.com")
  assert.equal(Model.appLabel("helium"), "helium")
  assert.equal(Model.cleanName("  Work \n  Laptop  "), "Work Laptop")
  assert.equal(Model.cleanName("x".repeat(100)).length, 64)
  assert.equal(Model.commandText(["foot", "--working-directory=/home/u/a b", "it's"]), "foot '--working-directory=/home/u/a b' 'it'\"'\"'s'")
  assert.equal(Model.progressLabel(6, 9), "6/9")
  assert.equal(Model.progressFraction(3, 0), 0)
})

test("every QML Text in the plugin is plain text (markup-shaped titles stay inert)", () => {
  const root = path.join(__dirname, "..")
  for (const file of fs.readdirSync(root).filter((f) => f.endsWith(".qml"))) {
    const source = fs.readFileSync(path.join(root, file), "utf8")
    const blocks = source.split(/\n/).map((line, i) => [line, i])
    for (const [line, i] of blocks) {
      if (!/^\s*(component \w+: )?Text \{/.test(line) && !/\bText \{\s*$/.test(line)) continue
      const body = source.split(/\n/).slice(i, i + 6).join("\n")
      assert.match(body, /textFormat: Text\.PlainText/, `${file}:${i + 1} Text without PlainText`)
    }
  }
})
