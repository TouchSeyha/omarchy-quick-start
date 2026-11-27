import QtQuick
import QtQuick.Controls
import Quickshell
import Quickshell.Io
import qs.Commons
import qs.Commons as Commons
import qs.Ui
import "." as Backend
import "Model.js" as Model

// Bar icon and popup for saving the open desktop into named sessions and
// bringing a session back in one click. No timers and no background work:
// the backend runs only when the user saves, edits, or restores.
Panel {
  id: root
  moduleName: "touchseyha.quick-start"
  ipcTarget: "touchseyha.quick-start"
  manageIpc: false

  readonly property var service: Backend.QuickStartService
  Commons.Translations { id: strings; directory: Qt.resolvedUrl("i18n") }

  readonly property bool includeSpecial: setting("includeSpecialWorkspaces", false) === true
  readonly property string ignoreClasses: String(setting("ignoreClasses", ""))

  readonly property var slots: service ? service.slots : []
  readonly property bool restoring: service ? service.restoring : false
  readonly property string progressText: service ? Model.progressLabel(service.progressDone, service.progressTotal) : ""

  // Panel-local UI state. Ids are slot ids; editKey is "<slot>/<window>".
  property int cursor: 0
  property string expandedId: ""
  property string renamingId: ""
  property string confirmDeleteId: ""
  property string editKey: ""
  property bool textFocus: false
  property bool helpOpen: false

  readonly property color foreground: bar ? bar.foreground : Commons.Color.foreground
  readonly property color dim: Qt.darker(foreground, 1.4)
  readonly property color warn: bar && bar.urgent ? bar.urgent : Commons.Color.urgent
  readonly property string fontFamily: bar ? bar.fontFamily : Style.font.family
  readonly property color accent: Commons.Color.accent
  readonly property string home: Quickshell.env("HOME") || ""

  // Workspace tints, read from the theme's named colors so every theme
  // brings its own. Re-read when the theme swaps (the accent changes then).
  property var themeHues: []
  FileView {
    id: themeColors
    path: Commons.Color.currentThemePath + "/colors.toml"
    printErrors: false
    onLoaded: root.themeHues = Model.themeHues(text())
    onLoadFailed: root.themeHues = []
  }
  Connections {
    target: Commons.Color
    function onAccentChanged() { themeColors.reload() }
  }

  // A theme without named colors still gets distinct tints: spin the
  // accent around the color wheel, keeping it readable on the panel.
  function hueFor(workspace) {
    var count = themeHues.length > 0 ? themeHues.length : 6
    var i = Model.workspaceHueIndex(workspace, count)
    if (themeHues.length > 0) return themeHues[i]
    var base = accent.hslHue < 0 ? 0.6 : accent.hslHue
    return Qt.hsla((base + i / 6) % 1, Math.max(0.45, accent.hslSaturation), Math.min(0.72, Math.max(0.58, accent.hslLightness)), 1)
  }
  function tint(c, a) { return Qt.rgba(c.r, c.g, c.b, a) }

  readonly property string iconGlyph: "󰁯"      // md-backup_restore
  readonly property string saveGlyph: "󰆓"      // md-content_save
  readonly property string renameGlyph: "󰏫"    // md-pencil
  readonly property string deleteGlyph: "󰆴"    // md-delete
  readonly property string closeGlyph: "󰅖"     // md-close
  readonly property string warnGlyph: "󰀦"      // md-alert
  readonly property string terminalGlyph: "󰆍"  // md-console
  readonly property string chevronDown: "󰅀"    // md-chevron_down
  readonly property string chevronUp: "󰅃"      // md-chevron_up
  readonly property string infoGlyph: "󰋽"      // md-information_outline

  // Give keys back to the panel after an inline field closes.
  function refocus() {
    textFocus = false
    if (panel.loadedContent) panel.loadedContent.forceActiveFocus()
  }

  function saveCurrent(name) {
    var clean = Model.cleanName(name)
    if (clean === "") return false
    if (service.slotByName(clean)) {
      saveNotice = strings.t("save.duplicate").arg(clean)
      return false
    }
    saveNotice = ""
    return service.save(clean, root.includeSpecial, root.ignoreClasses)
  }
  property string saveNotice: ""

  function restoreSlot(id) {
    confirmDeleteId = ""
    if (service) service.restore(id)
  }

  function updateSlot(id) {
    confirmDeleteId = ""
    if (service) service.update(id, root.includeSpecial, root.ignoreClasses)
  }

  function deleteSlot(id) {
    if (confirmDeleteId !== id) {
      confirmDeleteId = id
      return
    }
    confirmDeleteId = ""
    if (expandedId === id) expandedId = ""
    service.remove(id)
  }

  function toggleExpanded(id) {
    expandedId = expandedId === id ? "" : id
    editKey = ""
  }

  function moveCursor(dy) {
    if (slots.length === 0) return
    cursor = Math.max(0, Math.min(slots.length - 1, cursor + dy))
    confirmDeleteId = ""
  }

  function selectedId() {
    return cursor >= 0 && cursor < slots.length ? slots[cursor].id : ""
  }

  function modeLabel(mode) {
    var key = "mode." + mode
    var label = strings.t(key)
    return label === key ? mode : label
  }

  function slotMeta(slot) {
    var date = slot.updated > 0
      ? new Date(slot.updated * 1000).toLocaleString(Qt.locale(Commons.I18n.formatLocale), Locale.ShortFormat)
      : ""
    return strings.tn("slot.meta", slot.windows.length).arg(Commons.I18n.formatNumber(slot.windows.length)).arg(date)
  }

  readonly property string heroMeta: {
    if (!service) return ""
    if (restoring) return strings.t("hero.restoring").arg(service.restoreName)
    if (slots.length === 0) return strings.t("hero.empty")
    return strings.tn("hero.count", slots.length).arg(Commons.I18n.formatNumber(slots.length))
  }

  readonly property string tooltip: {
    if (!service) return ""
    if (restoring) return strings.t("tooltip.restoring").arg(service.restoreName).arg(root.progressText)
    return strings.tn("tooltip.idle", slots.length).arg(Commons.I18n.formatNumber(slots.length))
  }

  readonly property string outcomeText: {
    if (!service) return ""
    if (service.restoreOutcome === "done") return strings.t("restore.done").arg(service.restoreName)
    if (service.restoreOutcome === "cancelled") return strings.t("restore.cancelled")
    if (service.restoreOutcome === "error") return strings.t("restore.failed").arg(service.restoreError || strings.t("error.generic"))
    return ""
  }

  readonly property var warningLines: {
    var out = []
    var list = service ? service.restoreWarnings : []
    for (var i = 0; i < list.length; i++) {
      var line = Model.warningText(list[i], strings.t)
      if (line !== "") out.push(line)
    }
    return out
  }

  // Closing an inline editor, however it closes, hands keys back to the panel.
  onRenamingIdChanged: if (renamingId === "") refocus()
  onEditKeyChanged: if (editKey === "") refocus()
  onSlotsChanged: cursor = Math.max(0, Math.min(cursor, slots.length - 1))

  Component.onCompleted: if (service) service.ensureLoaded()
  onOpenedChanged: {
    if (opened) {
      if (service) service.refresh()
      confirmDeleteId = ""
      saveNotice = ""
      helpOpen = false
      cursor = Math.max(0, Math.min(cursor, slots.length - 1))
    } else {
      renamingId = ""
      editKey = ""
      textFocus = false
    }
  }

  ShellIpc {
    target: "touchseyha.quick-start"

    function open(): void { root.open() }
    function close(): void { root.close() }
    function show(): void { root.open() }
    function hide(): void { root.close() }
    function toggle(): void { root.toggle() }
    function restore(name: string): void {
      var slot = root.service ? root.service.slotByName(name) : null
      if (slot) root.restoreSlot(slot.id)
    }
    function cancel(): void { if (root.service) root.service.cancelRestore() }
  }

  implicitWidth: button.implicitWidth
  implicitHeight: button.implicitHeight

  BarIconButton {
    id: button
    anchors.fill: parent
    bar: root.bar
    text: root.iconGlyph
    labelText: root.restoring && !vertical ? root.progressText : ""
    slotSize: Style.bar.iconSlot * (root.restoring && !vertical ? 2.2 : 1)
    tooltipText: root.tooltip
    onPressed: function(b) {
      if (b === Qt.MiddleButton && root.restoring) root.service.cancelRestore()
      else root.toggle()
    }
  }

  KeyboardPanel {
    id: panel
    anchorItem: button
    owner: root
    bar: root.bar
    open: root.opened
    focusTarget: panel.loadedContent
    contentWidth: panel.fittedContentWidth(Style.space(420))
    contentHeight: panel.fittedContentHeight(panel.loadedContentHeight, Style.space(760))

    content: PanelKeyCatcher {
      id: keyCatcher
      anchors.fill: parent
      implicitHeight: column.implicitHeight
      blocked: root.textFocus

      onMoveRequested: function(dx, dy) { if (dy !== 0) root.moveCursor(dy) }
      onActivateRequested: { var id = root.selectedId(); if (id) root.restoreSlot(id) }
      onDeleteRequested: { var id = root.selectedId(); if (id) root.deleteSlot(id) }
      onCloseRequested: {
        if (root.confirmDeleteId !== "") root.confirmDeleteId = ""
        else if (root.helpOpen) root.helpOpen = false
        else root.close()
      }
      onTabRequested: function(direction) { root.switchPanel(direction) }
      onTextKey: function(t) {
        var id = root.selectedId()
        if (t === "s" || t === "S") saveField.forceActiveFocus()
        else if ((t === "e" || t === "E") && id) root.toggleExpanded(id)
        else if ((t === "u" || t === "U") && id) root.updateSlot(id)
        else if ((t === "r" || t === "R") && id) root.renamingId = id
        else if (t === "c" || t === "C") root.service.cancelRestore()
        else if (t === "?" || t === "i" || t === "I") root.helpOpen = !root.helpOpen
      }

      Flickable {
        id: flick
        anchors.fill: parent
        contentWidth: width
        contentHeight: column.implicitHeight
        clip: true
        boundsBehavior: Flickable.StopAtBounds
        flickableDirection: Flickable.VerticalFlick
        interactive: contentHeight > height
        ScrollBar.vertical: ScrollBar { id: scroll; policy: ScrollBar.AsNeeded }

        Column {
          id: column
          width: flick.width - (flick.interactive ? scroll.width + Style.space(6) : 0)
          spacing: Style.space(14)

          PanelHero {
            width: parent.width
            title: strings.t("title")
            meta: root.heroMeta
            foreground: root.foreground
            fontFamily: root.fontFamily
            iconComponent: Component {
              Text {
                textFormat: Text.PlainText
                text: root.iconGlyph
                color: root.accent
                font.family: root.fontFamily
                font.pixelSize: Style.font.display
              }
            }
            trailingControl: Component {
              PanelActionButton {
                iconText: root.infoGlyph
                tooltipText: root.helpOpen ? strings.t("help.hide") : strings.t("help.show")
                foreground: root.helpOpen ? root.accent : root.dim
                hoverColor: root.accent
                fontFamily: root.fontFamily
                onClicked: root.helpOpen = !root.helpOpen
              }
            }
          }

          // ---------- What Quick-Start does and how to use it ----------
          Rectangle {
            width: parent.width
            visible: root.helpOpen
            implicitHeight: helpColumn.implicitHeight + Style.space(24)
            radius: Style.cornerRadius
            color: root.tint(root.accent, 0.06)
            border.width: Math.max(1, Style.normalBorderWidth)
            border.color: root.tint(root.accent, 0.3)

            Column {
              id: helpColumn
              x: Style.space(12)
              y: Style.space(12)
              width: parent.width - Style.space(24)
              spacing: Style.space(10)

              PlainLabel {
                width: parent.width
                text: strings.t("help.what")
                wrapMode: Text.WordWrap
                lineHeight: 1.15
              }

              PlainLabel {
                width: parent.width
                text: strings.t("help.limits")
                color: root.dim
                font.pixelSize: Style.font.caption
                wrapMode: Text.WordWrap
                lineHeight: 1.15
              }

              // The steps really are a sequence, so they are numbered.
              Column {
                width: parent.width
                spacing: Style.space(6)

                Repeater {
                  model: ["help.step1", "help.step2", "help.step3"]
                  Row {
                    required property string modelData
                    required property int index
                    width: helpColumn.width
                    spacing: Style.space(8)

                    Rectangle {
                      id: stepBadge
                      width: Style.space(18)
                      height: width
                      radius: width / 2
                      color: root.tint(root.accent, 0.18)
                      PlainLabel {
                        anchors.centerIn: parent
                        text: Commons.I18n.formatNumber(index + 1)
                        color: root.accent
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }
                    }
                    PlainLabel {
                      width: parent.width - stepBadge.width - parent.spacing
                      anchors.verticalCenter: parent.verticalCenter
                      text: strings.t(modelData)
                      wrapMode: Text.WordWrap
                    }
                  }
                }
              }

              Flow {
                width: parent.width
                spacing: Style.space(6)

                Repeater {
                  model: [
                    ["↑ ↓", "help.keySelect"], ["Enter", "help.keyRestore"], ["E", "help.keyWindows"],
                    ["U", "help.keyUpdate"], ["R", "help.keyRename"], ["X", "help.keyDelete"],
                    ["S", "help.keyName"], ["?", "help.keyHelp"], ["Esc", "help.keyClose"]
                  ]
                  Row {
                    required property var modelData
                    spacing: Style.space(4)

                    Rectangle {
                      anchors.verticalCenter: parent.verticalCenter
                      implicitWidth: Math.max(Style.space(18), keyText.implicitWidth + Style.space(8))
                      implicitHeight: keyText.implicitHeight + Style.space(2)
                      radius: Style.cornerRadius
                      color: "transparent"
                      border.width: Math.max(1, Style.normalBorderWidth)
                      border.color: Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.3)
                      PlainLabel {
                        id: keyText
                        anchors.centerIn: parent
                        text: modelData[0]
                        font.pixelSize: Style.font.caption
                        font.bold: true
                      }
                    }
                    PlainLabel {
                      anchors.verticalCenter: parent.verticalCenter
                      text: strings.t(modelData[1])
                      color: root.dim
                      font.pixelSize: Style.font.caption
                    }
                  }
                }
              }
            }
          }

          // ---------- Restore progress and outcome ----------
          Column {
            width: parent.width
            spacing: Style.space(8)
            visible: root.restoring || root.outcomeText !== ""

            Item {
              width: parent.width
              implicitHeight: Style.space(6)
              visible: root.restoring

              Rectangle {
                id: track
                anchors.fill: parent
                radius: height / 2
                color: root.tint(root.accent, 0.16)
              }
              Rectangle {
                anchors.left: track.left
                height: track.height
                radius: track.radius
                color: root.accent
                width: Math.max(track.height, track.width * Model.progressFraction(root.service.progressDone, root.service.progressTotal))
                Behavior on width { NumberAnimation { duration: Style.duration(200); easing.type: Easing.OutCubic } }
              }
            }

            Row {
              width: parent.width
              spacing: Style.space(8)

              PlainLabel {
                width: parent.width - actionButton.width - parent.spacing
                anchors.verticalCenter: parent.verticalCenter
                text: root.restoring
                  ? strings.t("restore.progress").arg(Commons.I18n.formatNumber(root.service.progressDone)).arg(Commons.I18n.formatNumber(root.service.progressTotal))
                  : root.outcomeText
                elide: Text.ElideRight
              }

              Button {
                id: actionButton
                text: root.restoring ? strings.t("restore.cancel") : strings.t("restore.dismiss")
                fontSize: Style.font.bodySmall
                foreground: root.foreground
                fontFamily: root.fontFamily
                bordered: true
                onClicked: root.restoring ? root.service.cancelRestore() : root.service.dismissOutcome()
              }
            }

            Repeater {
              model: root.warningLines
              Row {
                required property string modelData
                width: column.width
                spacing: Style.space(6)
                PlainLabel { text: root.warnGlyph; color: root.warn }
                PlainLabel {
                  width: parent.width - Style.space(24)
                  text: modelData
                  color: root.dim
                  wrapMode: Text.WordWrap
                  font.pixelSize: Style.font.caption
                }
              }
            }
          }

          // ---------- Save the current desktop ----------
          Row {
            width: parent.width
            spacing: Style.space(8)

            TextField {
              id: saveField
              width: parent.width - saveButton.width - parent.spacing
              placeholderText: strings.t("save.placeholder")
              foreground: root.foreground
              font.family: root.fontFamily
              maximumLength: 64
              onActiveFocusChanged: root.textFocus = activeFocus
              onTextEdited: root.saveNotice = ""
              onAccepted: if (root.saveCurrent(text)) { text = ""; root.refocus() }
              Keys.onEscapePressed: function(event) { text = ""; root.saveNotice = ""; root.refocus(); event.accepted = true }
            }

            Button {
              id: saveButton
              iconText: root.saveGlyph
              text: strings.t("save.button")
              foreground: enabled ? root.accent : root.foreground
              fontFamily: root.fontFamily
              bordered: true
              enabled: Model.cleanName(saveField.text) !== "" && !root.service.busy
              onClicked: if (root.saveCurrent(saveField.text)) saveField.text = ""
            }
          }

          PlainLabel {
            width: parent.width
            visible: text !== ""
            color: root.warn
            wrapMode: Text.WordWrap
            font.pixelSize: Style.font.caption
            text: root.saveNotice !== "" ? root.saveNotice
              : (root.service.lastError !== "" ? (root.service.lastError === "generic" || root.service.lastError === "backend" ? strings.t("error.generic") : root.service.lastError) : "")
          }

          PanelSeparator { foreground: root.foreground }

          PanelSectionHeader {
            text: strings.t("sessions.heading")
            foreground: root.foreground
            fontFamily: root.fontFamily
          }

          PlainLabel {
            width: parent.width
            visible: root.slots.length === 0
            text: strings.t("sessions.empty")
            color: root.dim
            wrapMode: Text.WordWrap
          }

          Repeater {
            model: root.slots
            SlotCard { width: column.width }
          }
        }
      }
    }
  }

  // ---------------------------------------------------------- components

  component PlainLabel: Text {
    textFormat: Text.PlainText
    color: root.foreground
    font.family: root.fontFamily
    font.pixelSize: Style.font.bodySmall
  }

  component SlotCard: Rectangle {
    id: card
    required property var modelData
    required property int index
    readonly property var slot: modelData
    readonly property bool expanded: root.expandedId === slot.id
    readonly property bool renaming: root.renamingId === slot.id
    readonly property bool selected: root.cursor === index
    readonly property bool restoringThis: root.restoring && root.service.restoreId === slot.id

    implicitHeight: cardColumn.implicitHeight + Style.space(20)
    radius: Style.cornerRadius
    readonly property var groups: Model.groupWindows(slot)
    color: selected || restoringThis ? root.tint(root.accent, 0.07) : "transparent"
    border.width: Math.max(1, Style.normalBorderWidth)
    border.color: restoringThis ? root.accent
      : (selected ? root.tint(root.accent, 0.6) : Qt.rgba(root.foreground.r, root.foreground.g, root.foreground.b, 0.14))
    Behavior on border.color { ColorAnimation { duration: Style.duration(120) } }

    HoverHandler { onHoveredChanged: if (hovered) root.cursor = card.index }

    Column {
      id: cardColumn
      x: Style.space(10)
      y: Style.space(10)
      width: parent.width - Style.space(20)
      spacing: Style.space(8)

      // Header: the whole block toggles the window list; the chevron on the
      // right shows which way it will go.
      Item {
        id: header
        width: parent.width
        implicitHeight: info.implicitHeight

        MouseArea {
          anchors.fill: parent
          enabled: !card.renaming
          cursorShape: Qt.PointingHandCursor
          onClicked: root.toggleExpanded(card.slot.id)
        }

        // Lines up with the name, not the whole block under it.
        PanelActionButton {
          id: chevron
          anchors.right: parent.right
          y: Math.max(0, ((card.renaming ? renameField.height : nameLabel.height) - height) / 2)
          iconText: card.expanded ? root.chevronUp : root.chevronDown
          tooltipText: card.expanded ? strings.t("slot.collapse") : strings.t("slot.expand")
          foreground: card.expanded ? root.accent : root.dim
          hoverColor: root.accent
          fontFamily: root.fontFamily
          onClicked: root.toggleExpanded(card.slot.id)
        }

        Column {
          id: info
          width: parent.width - chevron.width - Style.space(6)
          spacing: Style.space(2)

          PlainLabel {
            id: nameLabel
            visible: !card.renaming
            width: parent.width
            text: card.slot.name
            font.pixelSize: Style.font.subtitle
            font.bold: true
            elide: Text.ElideRight
          }

          TextField {
            id: renameField
            visible: card.renaming
            width: parent.width
            placeholderText: strings.t("slot.renamePlaceholder")
            foreground: root.foreground
            font.family: root.fontFamily
            maximumLength: 64
            onVisibleChanged: if (visible) { text = card.slot.name; selectAll(); forceActiveFocus() }
            onActiveFocusChanged: root.textFocus = activeFocus
            onAccepted: if (root.service.rename(card.slot.id, text)) root.renamingId = ""
            Keys.onEscapePressed: function(event) { root.renamingId = ""; event.accepted = true }
          }

          PlainLabel {
            width: parent.width
            text: root.slotMeta(card.slot)
            color: root.dim
            font.pixelSize: Style.font.caption
            elide: Text.ElideRight
          }

          // One chip per saved workspace, tinted like its group below,
          // so a session's shape reads at a glance even when collapsed.
          Flow {
            width: parent.width
            spacing: Style.space(4)
            visible: card.groups.length > 0
            topPadding: Style.space(2)

            Repeater {
              model: card.groups
              WorkspaceChip {
                required property var modelData
                workspace: modelData.workspace
                label: Model.workspaceShort(modelData.workspace)
                count: modelData.windows.length
              }
            }
          }

          PlainLabel {
            visible: card.slot.warningCount > 0
            width: parent.width
            text: root.warnGlyph + " " + strings.tn("slot.warnings", card.slot.warningCount).arg(Commons.I18n.formatNumber(card.slot.warningCount))
            color: root.warn
            font.pixelSize: Style.font.caption
          }
        }
      }

      // Actions.
      Row {
        width: parent.width
        spacing: Style.space(6)

        Button {
          text: strings.t("slot.restore")
          iconText: root.iconGlyph
          fontSize: Style.font.bodySmall
          foreground: enabled || card.restoringThis ? root.accent : root.foreground
          accent: root.accent
          fontFamily: root.fontFamily
          bordered: true
          active: card.restoringThis
          enabled: !root.restoring
          onClicked: root.restoreSlot(card.slot.id)
        }
        Button {
          text: strings.t("slot.update")
          tooltipText: strings.t("slot.updateTip")
          iconText: root.saveGlyph
          fontSize: Style.font.bodySmall
          foreground: root.foreground
          fontFamily: root.fontFamily
          enabled: !root.service.busy && !root.restoring
          onClicked: root.updateSlot(card.slot.id)
        }
        Button {
          text: strings.t("slot.rename")
          iconText: root.renameGlyph
          fontSize: Style.font.bodySmall
          foreground: root.foreground
          fontFamily: root.fontFamily
          onClicked: root.renamingId = card.renaming ? "" : card.slot.id
        }
        Button {
          text: root.confirmDeleteId === card.slot.id ? strings.t("slot.confirmDelete") : strings.t("slot.delete")
          iconText: root.deleteGlyph
          fontSize: Style.font.bodySmall
          foreground: root.confirmDeleteId === card.slot.id ? root.warn : root.foreground
          background: root.confirmDeleteId === card.slot.id ? root.tint(root.warn, 0.14) : "transparent"
          bordered: root.confirmDeleteId === card.slot.id
          fontFamily: root.fontFamily
          enabled: !card.restoringThis
          onClicked: root.deleteSlot(card.slot.id)
        }
      }

      // Expanded preview: windows grouped by workspace.
      Column {
        visible: card.expanded
        width: parent.width
        spacing: Style.space(8)

        Repeater {
          model: card.expanded ? card.groups : []

          Column {
            id: group
            required property var modelData
            readonly property color hue: root.hueFor(modelData.workspace)
            width: cardColumn.width
            spacing: Style.space(6)

            Row {
              width: parent.width
              spacing: Style.space(8)

              Rectangle {
                id: groupBadge
                anchors.verticalCenter: parent.verticalCenter
                implicitWidth: badgeText.implicitWidth + Style.space(12)
                implicitHeight: badgeText.implicitHeight + Style.space(4)
                radius: Style.cornerRadius
                color: root.tint(group.hue, 0.16)

                PlainLabel {
                  id: badgeText
                  anchors.centerIn: parent
                  text: Model.workspaceLabel(group.modelData.workspace, strings.t)
                  color: group.hue
                  font.pixelSize: Style.font.caption
                  font.bold: true
                }
              }

              PlainLabel {
                width: parent.width - groupBadge.width - parent.spacing
                anchors.verticalCenter: parent.verticalCenter
                text: (group.modelData.mode ? root.modeLabel(group.modelData.mode) : "")
                  + (group.modelData.mode && group.modelData.monitor ? " · " : "")
                  + (group.modelData.monitor || "")
                color: root.dim
                font.pixelSize: Style.font.caption
                elide: Text.ElideRight
              }
            }

            // The group's windows hang off a rail in its tint.
            Item {
              width: parent.width
              implicitHeight: groupWindows.implicitHeight

              Rectangle {
                x: Style.space(4)
                width: Math.max(2, Style.normalBorderWidth * 2)
                height: parent.height
                radius: width / 2
                color: root.tint(group.hue, 0.55)
              }

              Column {
                id: groupWindows
                x: Style.space(14)
                width: parent.width - x
                spacing: Style.space(4)

                Repeater {
                  model: group.modelData.windows
                  WindowRow { width: groupWindows.width; slotId: card.slot.id }
                }
              }
            }
          }
        }
      }
    }
  }

  component WorkspaceChip: Rectangle {
    id: chip
    property string workspace: ""
    property string label: ""
    property int count: 0
    readonly property color hue: root.hueFor(workspace)

    implicitWidth: chipRow.implicitWidth + Style.space(10)
    implicitHeight: chipRow.implicitHeight + Style.space(2)
    radius: height / 2
    color: root.tint(hue, 0.14)

    Row {
      id: chipRow
      anchors.centerIn: parent
      spacing: Style.space(4)

      Rectangle {
        anchors.verticalCenter: parent.verticalCenter
        width: Style.space(6)
        height: width
        radius: width / 2
        color: chip.hue
      }
      PlainLabel {
        text: chip.label
        color: chip.hue
        font.pixelSize: Style.font.caption
        font.bold: true
      }
      PlainLabel {
        text: Commons.I18n.formatNumber(chip.count)
        color: root.dim
        font.pixelSize: Style.font.caption
      }
    }
  }

  component WindowRow: Column {
    id: row
    required property var modelData
    property string slotId: ""
    readonly property var win: modelData
    readonly property string rowKey: slotId + "/" + win.key
    readonly property bool editing: root.editKey === rowKey
    spacing: Style.space(4)

    Row {
      width: parent.width
      spacing: Style.space(6)

      PlainLabel {
        id: warnMark
        visible: row.win.warning !== ""
        anchors.verticalCenter: parent.verticalCenter
        text: root.warnGlyph
        color: root.warn
      }

      Column {
        width: parent.width - (warnMark.visible ? warnMark.width + parent.spacing : 0) - editButton.width - removeButton.width - parent.spacing * 2
        anchors.verticalCenter: parent.verticalCenter

        PlainLabel {
          width: parent.width
          text: Model.windowApp(row.win)
          elide: Text.ElideRight
        }
        PlainLabel {
          width: parent.width
          visible: text !== ""
          text: row.win.warning === "unresolved" ? strings.t("window.unresolved")
            : (row.win.warning === "missing" ? strings.t("window.missing") : Model.windowDetail(row.win, root.home))
          color: row.win.warning !== "" ? root.warn : root.dim
          font.pixelSize: Style.font.caption
          wrapMode: row.win.warning !== "" ? Text.WordWrap : Text.NoWrap
          elide: row.win.warning !== "" ? Text.ElideNone : Text.ElideRight
        }
      }

      PanelActionButton {
        id: editButton
        anchors.verticalCenter: parent.verticalCenter
        iconText: root.terminalGlyph
        tooltipText: strings.t("window.editCommand")
        foreground: row.win.warning !== "" ? root.warn : root.dim
        hoverColor: row.win.warning !== "" ? root.warn : root.accent
        fontFamily: root.fontFamily
        onClicked: root.editKey = row.editing ? "" : row.rowKey
      }

      PanelActionButton {
        id: removeButton
        anchors.verticalCenter: parent.verticalCenter
        iconText: root.closeGlyph
        tooltipText: strings.t("window.remove")
        foreground: root.dim
        hoverColor: root.warn
        fontFamily: root.fontFamily
        onClicked: root.service.removeWindow(row.slotId, row.win.key)
      }
    }

    TextField {
      id: commandField
      visible: row.editing
      width: parent.width
      placeholderText: strings.t("window.commandPlaceholder")
      foreground: root.foreground
      font.family: root.fontFamily
      font.pixelSize: Style.font.bodySmall
      onVisibleChanged: if (visible) { text = row.win.commandText; forceActiveFocus() }
      onActiveFocusChanged: root.textFocus = activeFocus
      onAccepted: if (root.service.setCommand(row.slotId, row.win.key, text)) root.editKey = ""
      Keys.onEscapePressed: function(event) { root.editKey = ""; event.accepted = true }
    }
  }
}
