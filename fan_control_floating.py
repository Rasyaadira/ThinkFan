#!/usr/bin/env python3
"""ThinkPad Fan Control — Material Design 3, PySide6 + QML.

Privileged layer unchanged: the only write path stays the root helper
/usr/local/bin/fanctl (single-path NOPASSWD sudoers rule).

  QML UI  →  Backend.setLevel()  →  sudo -n fanctl set <level>
           →  /proc/acpi/ibm/fan →  EC

Material Design 3 (baseline dark) applied directly on a QML scene graph:
tonal surfaces instead of shadows, container colours instead of borders,
pill filled button, M3 slider with stops and value indicator, M3 type scale.

Run:  python3 fan_control.py
Env:  FANAPP_REVERT_MIN (default 5 minutes, 0 = disabled)
      FANAPP_POLL_MS   (default 2000 ms)
"""
from __future__ import annotations

import os
import re
import subprocess
import sys
import time
from pathlib import Path

from PySide6.QtCore import (
    Property, QObject, QTimer, QUrl, Signal, Slot,
)
from PySide6.QtGui import QFontDatabase, QGuiApplication
from PySide6.QtQml import QQmlApplicationEngine

# --- privileged path (do not change) ----------------------------------------
HELPER = "/usr/local/bin/fanctl"

# --- data sources (read-only, no root needed) -------------------------------
FAN_PROC = Path("/proc/acpi/ibm/fan")
FAN_PARAM = Path("/sys/module/thinkpad_acpi/parameters/fan_control")
HWMON_ROOT = Path("/sys/class/hwmon")

# --- behaviour --------------------------------------------------------------
REVERT_MIN = float(os.environ.get("FANAPP_REVERT_MIN", "5"))
POLL_MS = int(os.environ.get("FANAPP_POLL_MS", "2000"))
HISTORY_POINTS = 330                       # 330 × 2 s = 11 minutes
MANUAL_LEVELS = [str(i) for i in range(8)]


# =============================================================================
# Sensors — pure functions, no Qt, testable headless
# =============================================================================
def hwmon_dir(name: str) -> Path | None:
    """Find /sys/class/hwmon/*/name == name (hwmon numbers change across boots)."""
    try:
        entries = sorted(HWMON_ROOT.glob("hwmon*"))
    except OSError:
        return None
    for d in entries:
        try:
            if (d / "name").read_text().strip() == name:
                return d
        except OSError:
            continue
    return None


def read_int(path: Path | None) -> int | None:
    if path is None:
        return None
    try:
        return int(path.read_text().strip())
    except (OSError, ValueError):
        return None


def _temp_from(hwmon_name: str, input_file: str = "temp1_input") -> float | None:
    d = hwmon_dir(hwmon_name)
    if d is None:
        return None
    milli = read_int(d / input_file)
    if milli is None or not (1000 < milli < 150000):
        return None
    return round(milli / 1000, 1)


def read_cpu_temp() -> float | None:
    return _temp_from("coretemp")


def read_chipset_temp() -> float | None:
    return _temp_from("pch_cannonlake")


def read_overall_temp(cpu: float | None = None) -> float | None:
    vals = [
        cpu if cpu is not None else read_cpu_temp(),
        _temp_from("thinkpad"),
        read_chipset_temp(),
        _temp_from("iwlwifi_1"),
        _temp_from("acpitz"),
    ]
    vals = [v for v in vals if v is not None]
    return max(vals) if vals else None


def read_fan() -> dict:
    level = rpm = None
    try:
        raw = FAN_PROC.read_text()
        if m := re.search(r"^level:\s*(\S+)", raw, re.M):
            level = m.group(1)
        if m := re.search(r"^speed:\s*(\d+)", raw, re.M):
            rpm = int(m.group(1))
    except OSError:
        pass
    tp = hwmon_dir("thinkpad")
    hw_rpm = read_int(tp / "fan1_input") if tp else None
    if hw_rpm is not None:
        rpm = hw_rpm
    try:
        fan_control = FAN_PARAM.read_text().strip()
    except OSError:
        fan_control = None
    return {"level": level, "rpm": rpm, "fan_control": fan_control}


def read_sensors() -> dict:
    fan = read_fan()
    cpu = read_cpu_temp()
    return {
        "cpu": cpu,
        "pch": read_chipset_temp(),
        "overall": read_overall_temp(cpu),
        "rpm": fan["rpm"],
        "level": fan["level"],
        "fan_control": fan["fan_control"],
        "helper": os.path.exists(HELPER),
    }


# =============================================================================
# Control — the one and only write path
# =============================================================================
def apply_level(level: str) -> tuple[bool, str]:
    if level not in MANUAL_LEVELS and level != "auto":
        return False, f"invalid level: {level!r}"
    if not os.path.exists(HELPER):
        return False, f"helper missing: {HELPER}"
    try:
        proc = subprocess.run(["sudo", "-n", HELPER, "set", level],
                              capture_output=True, text=True, timeout=20)
    except subprocess.TimeoutExpired:
        return False, "helper timed out"
    except OSError as exc:
        return False, f"could not run helper: {exc}"
    if proc.returncode != 0:
        return False, (proc.stderr or proc.stdout).strip()
    return True, f"level {level}"


# =============================================================================
# Backend — QObject exposed to QML as `backend`
# =============================================================================
class Backend(QObject):
    dataChanged = Signal()
    messageChanged = Signal()
    revertChanged = Signal()

    def __init__(self) -> None:
        super().__init__()
        self._cpu: float | None = None
        self._pch: float | None = None
        self._rpm: int | None = None
        self._level: str | None = None
        self._fan_control: str | None = None
        self._helper: bool = False
        self._cpu_hist: list[float | None] = []
        self._pch_hist: list[float | None] = []
        self._message: str = ""
        self._error: bool = False
        self._revert_at: float | None = None
        self._healthy: bool = False

        s0 = read_sensors()
        if s0["fan_control"] != "Y":
            self._message = "fan_control = N — enable via modprobe.d, then reboot"
            self._error = True
        elif not s0["helper"]:
            self._message = f"helper missing: {HELPER}"
            self._error = True
        else:
            self._message = "drag the slider, or press Auto (Esc = auto)"

        self._refresh()

        self._timer = QTimer(self)
        self._timer.setInterval(POLL_MS)
        self._timer.timeout.connect(self._tick)
        self._timer.start()

    # ---- properties ------------------------------------------------------
    @Property("QVariant", notify=dataChanged)
    def cpu(self):               return self._cpu

    @Property("QVariant", notify=dataChanged)
    def pch(self):               return self._pch

    @Property("QVariant", notify=dataChanged)
    def rpm(self):               return self._rpm

    @Property("QVariant", notify=dataChanged)
    def level(self):             return self._level

    @Property(bool, notify=dataChanged)
    def healthy(self):           return self._healthy

    @Property("QVariantList", notify=dataChanged)
    def cpuHistory(self):        return list(self._cpu_hist)

    @Property("QVariantList", notify=dataChanged)
    def pchHistory(self):        return list(self._pch_hist)

    @Property(str, notify=messageChanged)
    def message(self):           return self._message

    @Property(bool, notify=messageChanged)
    def hasError(self):          return self._error

    @Property(int, notify=revertChanged)
    def revertSeconds(self):
        if self._revert_at is None:
            return -1
        return max(0, int(self._revert_at - time.monotonic()))

    @Property(int, constant=True)
    def pollMs(self):            return POLL_MS

    @Property(int, constant=True)
    def historyPoints(self):     return HISTORY_POINTS

    # ---- internals -------------------------------------------------------
    def _refresh(self) -> None:
        s = read_sensors()
        self._cpu = s["cpu"]
        self._pch = s["pch"]
        self._rpm = s["rpm"]
        self._level = s["level"]
        self._fan_control = s["fan_control"]
        self._helper = s["helper"]

        self._cpu_hist.append(s["cpu"])
        self._pch_hist.append(s["pch"])
        if len(self._cpu_hist) > HISTORY_POINTS:
            self._cpu_hist = self._cpu_hist[-HISTORY_POINTS:]
            self._pch_hist = self._pch_hist[-HISTORY_POINTS:]

        self._healthy = (s["fan_control"] == "Y" and s["helper"] and not self._error)
        self.dataChanged.emit()

    def _tick(self) -> None:
        if self._revert_at is not None and time.monotonic() >= self._revert_at:
            self._revert_at = None
            ok, _msg = apply_level("auto")
            self._message = "✓ safety: manual level reverted to auto"
            self._error = not ok
            self.messageChanged.emit()
            self.revertChanged.emit()
        elif self._revert_at is not None:
            self.revertChanged.emit()
        self._refresh()

    # ---- slots (callable from QML) --------------------------------------
    @Slot(str)
    def setLevel(self, level: str) -> None:
        ok, msg = apply_level(level)
        self._message = ("✓ " if ok else "✕ ") + msg
        self._error = not ok
        if ok:
            if level == "auto" or REVERT_MIN <= 0:
                self._revert_at = None
            else:
                self._revert_at = time.monotonic() + REVERT_MIN * 60
        self.messageChanged.emit()
        self.revertChanged.emit()
        self._refresh()


# =============================================================================
# QML — Material Design 3, baseline dark scheme
# =============================================================================
QML = r"""
import QtQuick
import QtQuick.Controls
import QtQuick.Layouts

ApplicationWindow {
    id: root
    width: 360
    height: 540
    minimumWidth: 360
    minimumHeight: 540
    maximumWidth: 360
    maximumHeight: 540
    visible: true
    flags: Qt.Window | Qt.WindowStaysOnTopHint
    title: "ThinkPad Fan Control"
    color: surface

    // ---------- M3 tokens ----------
    readonly property color surface:                    "#141218"
    readonly property color surfaceContainer:           "#211F26"
    readonly property color surfaceContainerHigh:       "#2B2930"
    readonly property color surfaceContainerHighest:    "#36343B"
    readonly property color surfaceOn:                  "#E6E1E5"
    readonly property color surfaceOnVariant:           "#CAC4D0"
    readonly property color outlineVariant:             "#49454F"
    readonly property color primary:                    "#D0BCFF"
    readonly property color primaryOn:                  "#381E72"
    readonly property color primaryContainer:           "#4F378B"
    readonly property color primaryOnContainer:         "#EADDFF"
    readonly property color tertiary:                   "#EFB8C8"
    readonly property color error:                      "#F2B8B5"
    readonly property color primaryDim:                 "#BDAAF5"

    // ---------- M3 shapes & spacing ----------

    readonly property int pad: 16
    readonly property int cardRadius: 16
    readonly property int buttonRadius: 18

        Shortcut {
        sequence: "Escape"
        onActivated: function() {
            backend.setLevel("auto")
        }
    }
    ColumnLayout {
        anchors.fill: parent
        anchors.margins: root.pad
        spacing: 12

        // ================= top app bar =================
        Item {
            Layout.fillWidth: true
            Layout.preferredHeight: 28
            Text {
                anchors.left: parent.left
                anchors.verticalCenter: parent.verticalCenter
                text: "ThinkPad Fan Control"
                color: root.surfaceOn
                font.family: uiFont
                font.pixelSize: 20
                font.weight: Font.Medium
            }
            Rectangle {
                anchors.right: parent.right
                anchors.verticalCenter: parent.verticalCenter
                width: 10; height: 10; radius: 5
                color: backend.healthy ? root.primary : root.error
            }
        }

        // ================= telemetry cards =================
        RowLayout {
            Layout.fillWidth: true
            Layout.preferredHeight: 68
            spacing: 10

            Repeater {
                model: 3
                delegate: Rectangle {
                    id: card
                    Layout.fillWidth: true
                    Layout.fillHeight: true
                    radius: root.cardRadius
                    color: root.surfaceContainerHigh

                    readonly property string cardLabel:
                        ["CPU °C", "CHIPSET °C", "FAN RPM"][index]
                    readonly property var cardValue:
                        index === 0 ? backend.cpu
                      : index === 1 ? backend.pch
                      :               backend.rpm

                    ColumnLayout {
                        anchors.fill: parent
                        anchors.margins: 12
                        spacing: 2

                        Text {
                            text: card.cardLabel
                            color: root.surfaceOnVariant
                            font.family: uiFont
                            font.pixelSize: 11
                            font.letterSpacing: 0.5
                        }
                        Text {
                            Layout.fillHeight: true
                            verticalAlignment: Text.AlignVCenter
                            color: root.surfaceOn
                            font.family: uiFont
                            font.pixelSize: 28
                            font.weight: Font.Bold
                            text: {
                                var v = card.cardValue;
                                if (v === null || v === undefined) return "—";
                                return index === 2 ? String(Math.round(Number(v)))
                                                   : Number(v).toFixed(0);
                            }
                        }
                    }
                }
            }
        }

        // ================= graph card =================
        Rectangle {
            Layout.fillWidth: true
            Layout.fillHeight: true
            Layout.minimumHeight: 150
            radius: root.cardRadius
            color: root.surfaceContainerHigh

            Canvas {
                id: graph
                anchors.fill: parent
                anchors.margins: 12
                onPaint: paintGraph()

                Connections {
                    target: backend
                    function onDataChanged() { graph.requestPaint(); }
                }

                function paintGraph() {
                    var ctx = graph.getContext("2d");
                    ctx.clearRect(0, 0, graph.width, graph.height);

                    var W = graph.width, H = graph.height;
                    var legendH = 20;
                    var left = 28, right = W - 2;
                    var top  = legendH + 6, bot = H - 18;

                    // ---- legend chips ----
                    ctx.textBaseline = "middle";
                    ctx.font = "11px " + uiFont;

                    ctx.fillStyle = root.primary;
                    ctx.beginPath(); ctx.arc(left - 16, legendH/2, 3.5, 0, 6.2832); ctx.fill();
                    ctx.fillStyle = root.surfaceOnVariant;
                    ctx.textAlign = "left";
                    ctx.fillText("CPU", left - 8, legendH/2);

                    ctx.fillStyle = root.tertiary;
                    ctx.beginPath(); ctx.arc(left + 50, legendH/2, 3.5, 0, 6.2832); ctx.fill();
                    ctx.fillStyle = root.surfaceOnVariant;
                    ctx.fillText("CHIPSET", left + 58, legendH/2);

                    ctx.textAlign = "right";
                    ctx.fillText("11m window", right, legendH/2);

                    var cpu = backend.cpuHistory;
                    var pch = backend.pchHistory;
                    var n = cpu.length;

                    // ---- y max ----
                    var ymax = 80;
                    for (var i = 0; i < n; i++) {
                        var a = cpu[i], b = pch[i];
                        if (a !== null && a !== undefined && a > ymax) ymax = 100;
                        if (b !== null && b !== undefined && b > ymax) ymax = 100;
                    }

                    // ---- y grid ----
                    ctx.lineWidth = 1;
                    ctx.strokeStyle = root.outlineVariant;
                    ctx.fillStyle = root.surfaceOnVariant;
                    ctx.textAlign = "right";
                    for (var g = 0; g <= 4; g++) {
                        var v = ymax * g / 4;
                        var y = bot - (bot - top) * g / 4;
                        ctx.fillText(v.toFixed(0), left - 6, y);
                        ctx.beginPath();
                        ctx.moveTo(left, y); ctx.lineTo(right, y); ctx.stroke();
                    }

                    // ---- x grid (elapsed minutes) ----
                    var ptsPerMin = 60000 / backend.pollMs;
                    var spanPts = Math.max(n - 1, 1);
                    ctx.textAlign = "center";
                    for (var m = 1; m < 12; m++) {
                        var mx = right - (right - left) * (m * ptsPerMin) / spanPts;
                        if (mx < left) break;
                        ctx.beginPath();
                        ctx.moveTo(mx, top); ctx.lineTo(mx, bot); ctx.stroke();
                        ctx.fillText(m + "m", mx, bot + 10);
                    }

                    if (n < 2) {
                        ctx.fillStyle = root.surfaceOnVariant;
                        ctx.textAlign = "center";
                        ctx.fillText("collecting...", (left + right) / 2, (top + bot) / 2);
                        return;
                    }

                    // ---- series ----
                    function drawSeries(data, color) {
                        ctx.strokeStyle = color;
                        ctx.lineWidth = 2;
                        ctx.lineJoin = "round";
                        ctx.lineCap = "round";
                        ctx.beginPath();
                        var denom = Math.max(n - 1, 1);
                        var started = false;
                        var lx, ly, any = false;
                        for (var i = 0; i < n; i++) {
                            var v = data[i];
                            if (v === null || v === undefined) { started = false; continue; }
                            v = Math.max(0, Math.min(ymax, v));
                            var x = left + (right - left) * (i / denom);
                            var y = bot - (bot - top) * (v / ymax);
                            if (!started) { ctx.moveTo(x, y); started = true; }
                            else          { ctx.lineTo(x, y); }
                            lx = x; ly = y; any = true;
                        }
                        ctx.stroke();
                        if (any) {
                            ctx.fillStyle = color;
                            ctx.beginPath(); ctx.arc(lx, ly, 2.5, 0, 6.2832); ctx.fill();
                        }
                    }
                    drawSeries(cpu, root.primary);
                    drawSeries(pch, root.tertiary);
                }
            }
        }

        // ================= fan level controls =================
        Rectangle {
            Layout.fillWidth: true
            Layout.preferredHeight: 170
            radius: root.cardRadius
            color: root.surfaceContainerHigh

            // --- header row: label, status, countdown ---
            Item {
                id: headerRow
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: parent.top
                anchors.margins: 16
                height: 20

                Text {
                    anchors.left: parent.left
                    anchors.verticalCenter: parent.verticalCenter
                    text: "FAN LEVEL"
                    color: root.surfaceOnVariant
                    font.family: uiFont
                    font.pixelSize: 11
                    font.letterSpacing: 0.5
                }
                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 82
                    anchors.verticalCenter: parent.verticalCenter
                    color: backend.level === null ? root.error : root.surfaceOn
                    font.family: uiFont
                    font.pixelSize: 13
                    text: {
                        var l = backend.level;
                        if (l === null || l === undefined) return "—";
                        if (l === "auto") return "Auto · firmware curve";
                        return "Level " + l;
                    }
                }
                Text {
                    anchors.right: parent.right
                    anchors.verticalCenter: parent.verticalCenter
                    visible: backend.revertSeconds >= 0
                    color: root.surfaceOnVariant
                    font.family: uiFont
                    font.pixelSize: 11
                    text: {
                        var s = backend.revertSeconds;
                        if (s < 0) return "";
                        var m = Math.floor(s / 60);
                        var ss = s % 60;
                        return "auto in " + m + ":" + (ss < 10 ? "0" : "") + ss;
                    }
                }
            }

            // --- M3 slider ---
            Item {
                id: fanSlider
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.top: headerRow.bottom
                anchors.leftMargin: 4
                anchors.rightMargin: 4
                height: 64

                readonly property int trackLeft: 16
                readonly property int trackRight: width - 16
                readonly property int trackWidth: trackRight - trackLeft
                readonly property int trackY: 22

                // -1 = auto (handle parked at left edge)
                readonly property int levelValue: {
                    var l = backend.level;
                    if (l === null || l === undefined) return -1;
                    if (l === "auto") return -1;
                    return parseInt(l);
                }

                property bool isDragging: false
                property int  dragLevel: -1
                readonly property int shown: isDragging ? dragLevel : levelValue

                function xFor(v) {
                    return trackLeft + trackWidth * Math.max(0, v) / 7;
                }

                // inactive track
                Rectangle {
                    x: fanSlider.trackLeft
                    y: fanSlider.trackY - 2
                    width: fanSlider.trackWidth
                    height: 4
                    radius: 2
                    color: root.outlineVariant
                }
                // active track
                Rectangle {
                    x: fanSlider.trackLeft
                    y: fanSlider.trackY - 2
                    width: fanSlider.shown >= 0
                           ? fanSlider.trackWidth * fanSlider.shown / 7
                           : 0
                    height: 4
                    radius: 2
                    color: root.primary
                }
                // stop dots
                Repeater {
                    model: 8
                    delegate: Rectangle {
                        x: fanSlider.xFor(index) - 3
                        y: fanSlider.trackY - 3
                        width: 6; height: 6; radius: 3
                        color: fanSlider.shown === index
                               ? root.primary
                               : root.outlineVariant
                    }
                }
                // L0..L7 labels
                Repeater {
                    model: 8
                    delegate: Text {
                        x: fanSlider.xFor(index) - 12
                        y: fanSlider.trackY + 12
                        width: 24
                        horizontalAlignment: Text.AlignHCenter
                        text: "L" + index
                        color: root.surfaceOnVariant
                        font.family: uiFont
                        font.pixelSize: 11
                    }
                }
                // halo (state layer)
                Rectangle {
                    visible: fanSlider.shown >= 0
                    x: fanSlider.xFor(fanSlider.shown) - 12
                    y: fanSlider.trackY - 12
                    width: 24; height: 24; radius: 12
                    color: root.surfaceContainerHighest
                }
                // handle (M3 vertical pill)
                Rectangle {
                    visible: fanSlider.shown >= 0
                    x: fanSlider.xFor(fanSlider.shown) - 2.5
                    y: fanSlider.trackY - 13
                    width: 5; height: 26; radius: 3
                    color: root.primary
                }
                // value indicator above the handle while dragging
                Rectangle {
                    visible: fanSlider.isDragging && fanSlider.dragLevel >= 0
                    x: Math.max(fanSlider.trackLeft - 14,
                       Math.min(fanSlider.trackRight - 46,
                                fanSlider.xFor(fanSlider.dragLevel) - 18))
                    y: fanSlider.trackY - 50
                    width: 36; height: 24; radius: 8
                    color: root.primaryContainer
                    Text {
                        anchors.centerIn: parent
                        text: "L" + fanSlider.dragLevel
                        color: root.primaryOnContainer
                        font.family: uiFont
                        font.pixelSize: 11
                    }
                }

                                MouseArea {
                    anchors.fill: parent
                    function valAt(px) {
                        var t = (px - fanSlider.trackLeft) / fanSlider.trackWidth;
                        t = Math.max(0, Math.min(1, t));
                        return Math.round(t * 7);
                    }
                    onPressed: function(m) {
                        fanSlider.isDragging = true;
                        fanSlider.dragLevel = valAt(m.x);
                    }
                    onPositionChanged: function(m) {
                        if (fanSlider.isDragging)
                            fanSlider.dragLevel = valAt(m.x);
                    }
                    onReleased: function(m) {
                        var v = valAt(m.x);
                        fanSlider.isDragging = false;
                        backend.setLevel(String(v));
                    }
                }
            }

            // --- Auto filled button (pill) ---
            Rectangle {
                id: autoBtn
                anchors.left: parent.left
                anchors.right: parent.right
                anchors.bottom: parent.bottom
                anchors.leftMargin: 16
                anchors.rightMargin: 16
                anchors.bottomMargin: 16
                height: 38
                radius: root.buttonRadius
                color: (autoMA.containsMouse || backend.level === "auto")
                       ? root.primary
                       : root.primaryDim

                Behavior on color { ColorAnimation { duration: 120 } }

                Text {
                    anchors.centerIn: parent
                    text: "Auto"
                    color: root.primaryOn
                    font.family: uiFont
                    font.pixelSize: 14
                    font.weight: Font.DemiBold
                }
                                MouseArea {
                    id: autoMA
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: function() {
                        backend.setLevel("auto")
                    }
                }
            }
        }

        // ================= status message =================
        Text {
            Layout.fillWidth: true
            Layout.preferredHeight: 30
            text: backend.message
            color: backend.hasError ? root.error : root.surfaceOnVariant
            font.family: uiFont
            font.pixelSize: 11
            wrapMode: Text.WordWrap
            verticalAlignment: Text.AlignTop
        }
    }
}
"""


# =============================================================================
# Entry point
# =============================================================================
def pick_ui_font() -> str:
    """M3 wants Roboto; fall back to whatever this machine actually has."""
    fams = set(QFontDatabase.families())
    return next((f for f in ("Roboto", "Noto Sans", "DejaVu Sans") if f in fams),
                QGuiApplication.font().family())


def main() -> int:
    app = QGuiApplication(sys.argv)
    app.setApplicationName("ThinkPad Fan Control")
    app.setApplicationDisplayName("ThinkPad Fan Control")

    backend = Backend()

    engine = QQmlApplicationEngine()
    ctx = engine.rootContext()
    ctx.setContextProperty("backend", backend)
    ctx.setContextProperty("uiFont", pick_ui_font())

    # Single-file deployment: load QML from memory.
    engine.loadData(QML.encode("utf-8"), QUrl("qrc:/Main.qml"))
    if not engine.rootObjects():
        return 1
    return app.exec()


if __name__ == "__main__":
    sys.exit(main())