#!/usr/bin/env python3
"""
Razer DeathAdder Chroma - LED colour tool (PyQt5 + hidapi).

Packet format taken from OpenMouse-Project/mouse-protocol (src/razer/codec.ts):
  90-byte feature report on report id 0
  [0]  status        (0x00 in requests)
  [1]  transaction id (DeathAdder Chroma 0x0043 -> 0xff)
  [5]  data size
  [6]  command class
  [7]  command id
  [8.. ] arguments
  [88] checksum = XOR of bytes 2..87

OpenMouse only implements the extended-matrix LED command (0x0f/0x02) of newer
mice. The DeathAdder Chroma is a first-generation Chroma device and uses the
"standard" LED commands (class 0x03), as in OpenRazer's razer_chroma_standard_*:
  0x03/0x00  set LED state      [varstore, led, on/off]
  0x03/0x01  set LED RGB        [varstore, led, r, g, b]
  0x03/0x02  set LED effect     [varstore, led, effect]
  0x03/0x03  set LED brightness [varstore, led, 0..255]

Requirements:  pip install pyqt5 hidapi
Linux: needs access to /dev/hidraw* (udev rule, see --help) and must not be
       bound by openrazer-driver at the same time.
"""

from __future__ import annotations

import argparse
import sys
import time
from dataclasses import dataclass

from PyQt5.QtCore import QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QRegularExpressionValidator
from PyQt5.QtCore import QRegularExpression
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

RAZER_VID = 0x1532
DEATHADDER_CHROMA_PID = 0x0043

PACKET_LEN = 90
TRANSACTION_ID = 0xFF          # per OpenMouse devices.ts fallback for 0x0043
VARSTORE = 0x01                # persistent store

STATUS_TEXT = {
    0x00: "new", 0x01: "busy", 0x02: "ok", 0x03: "failure",
    0x04: "timeout", 0x05: "unsupported",
}

LED_SCROLL = 0x01
LED_LOGO = 0x04
ZONES = {"Logo + Scrollrad": (LED_LOGO, LED_SCROLL),
         "Logo": (LED_LOGO,),
         "Scrollrad": (LED_SCROLL,)}

EFFECT_STATIC = 0x00
EFFECT_BLINKING = 0x01
EFFECT_BREATHING = 0x02
EFFECT_SPECTRUM = 0x04
EFFECTS = {"Statisch": EFFECT_STATIC,
           "Atmen": EFFECT_BREATHING,
           "Blinken": EFFECT_BLINKING,
           "Spektrum": EFFECT_SPECTRUM,
           "Aus": None}


class RazerError(Exception):
    pass


def checksum(packet: bytes | bytearray) -> int:
    c = 0
    for b in packet[2:88]:
        c ^= b
    return c


def encode(cmd_class: int, cmd_id: int, args: list[int]) -> bytes:
    p = bytearray(PACKET_LEN)
    p[1] = TRANSACTION_ID
    p[5] = len(args)
    p[6] = cmd_class
    p[7] = cmd_id
    p[8:8 + len(args)] = bytes(args)
    p[88] = checksum(p)
    return bytes(p)


def cmd_led_state(led: int, on: bool) -> bytes:
    return encode(0x03, 0x00, [VARSTORE, led, 0x01 if on else 0x00])


def cmd_led_rgb(led: int, r: int, g: int, b: int) -> bytes:
    return encode(0x03, 0x01, [VARSTORE, led, r & 0xFF, g & 0xFF, b & 0xFF])


def cmd_led_effect(led: int, effect: int) -> bytes:
    return encode(0x03, 0x02, [VARSTORE, led, effect])


def cmd_led_brightness(led: int, value: int) -> bytes:
    return encode(0x03, 0x03, [VARSTORE, led, max(0, min(255, value))])


# --------------------------------------------------------------------------- #
# Device access
# --------------------------------------------------------------------------- #

class DummyMouse:
    """Stand-in used with --dry-run or when no mouse is found."""
    name = "Simulation (keine Maus)"

    def send(self, packet: bytes) -> None:
        print("TX", packet[:12].hex(" "), "... chk", f"{packet[88]:02x}")

    def close(self) -> None:
        pass


class DeathAdderChroma:
    name = "Razer DeathAdder Chroma"

    def __init__(self) -> None:
        import hid  # hidapi

        candidates = hid.enumerate(RAZER_VID, DEATHADDER_CHROMA_PID)
        if not candidates:
            raise RazerError("DeathAdder Chroma (1532:0043) nicht gefunden.")
        # Control channel = the Generic Desktop *Mouse* collection (0x0001:0x0002)
        # on interface 0. On Windows the DA Chroma exposes 7 top-level
        # collections (1:6 twice, C:1, 1:80, 1:0 twice, 1:2) as separate paths;
        # the keyboard ones are locked by the OS, so try 1:2 first, rest after.
        candidates.sort(key=lambda d: (
            not (d.get("usage_page") == 0x0001 and d.get("usage") == 0x0002),
            d.get("interface_number", 0) not in (0, -1),
        ))
        last_err = None
        for info in candidates:
            dev = hid.device()
            try:
                dev.open_path(info["path"])
                self._dev = dev
                self._try_probe()
                return
            except Exception as e:  # noqa: BLE001 - try next interface
                last_err = e
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
        raise RazerError(f"Keine Steuer-Schnittstelle nutzbar: {last_err}")

    def _try_probe(self) -> None:
        # Firmware read (0x00/0x81) - verifies we reached the control channel.
        # OpenMouse test tool reports "Mouse 1.8" for this unit.
        reply = self._exchange(encode(0x00, 0x81, [0x00, 0x00]))
        self.firmware = f"{reply[8]}.{reply[9]}"
        self.name = f"Razer DeathAdder Chroma (FW {self.firmware})"

    def _exchange(self, packet: bytes, retries: int = 5) -> bytes:
        self._dev.send_feature_report(b"\x00" + packet)
        for _ in range(retries):
            time.sleep(0.008)
            reply = bytes(self._dev.get_feature_report(0x00, PACKET_LEN + 1))
            if len(reply) == PACKET_LEN + 1:
                reply = reply[1:]
            if len(reply) != PACKET_LEN:
                raise RazerError(f"Antwort mit {len(reply)} Bytes")
            status = reply[0]
            if status == 0x01:          # busy -> poll again, never resend write
                continue
            if reply[6:8] != packet[6:8]:
                continue                # stale reply of an earlier exchange
            if status != 0x02:
                raise RazerError(f"Befehl {packet[6]:02x}/{packet[7]:02x}: "
                                 f"Status {STATUS_TEXT.get(status, hex(status))}")
            if reply[88] != checksum(reply):
                raise RazerError("Antwort mit falscher Checksumme")
            return reply
        raise RazerError("Maus antwortet nicht (busy/timeout)")

    def send(self, packet: bytes) -> None:
        self._exchange(packet)

    def close(self) -> None:
        self._dev.close()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

@dataclass
class LedState:
    color: QColor
    effect: str
    zone: str
    brightness: int


class ChannelSlider(QWidget):
    valueChanged = pyqtSignal(int)

    def __init__(self, label: str, css_color: str) -> None:
        super().__init__()
        lay = QHBoxLayout(self)
        lay.setContentsMargins(0, 0, 0, 0)
        name = QLabel(label)
        name.setFixedWidth(14)
        self.slider = QSlider(Qt.Horizontal)
        self.slider.setRange(0, 255)
        self.slider.setStyleSheet(
            f"QSlider::groove:horizontal{{height:8px;border-radius:4px;"
            f"background:qlineargradient(x1:0,y1:0,x2:1,y2:0,stop:0 #000,stop:1 {css_color});}}"
            "QSlider::handle:horizontal{width:14px;margin:-5px 0;border-radius:7px;"
            "background:#eee;border:1px solid #555;}")
        self.spin = QSpinBox()
        self.spin.setRange(0, 255)
        self.slider.valueChanged.connect(self.spin.setValue)
        self.spin.valueChanged.connect(self.slider.setValue)
        self.slider.valueChanged.connect(self.valueChanged)
        lay.addWidget(name)
        lay.addWidget(self.slider, 1)
        lay.addWidget(self.spin)

    def value(self) -> int:
        return self.slider.value()

    def setValue(self, v: int) -> None:
        self.slider.blockSignals(True)
        self.spin.blockSignals(True)
        self.slider.setValue(v)
        self.spin.setValue(v)
        self.slider.blockSignals(False)
        self.spin.blockSignals(False)


PRESETS = ["#ff0000", "#ff8000", "#ffff00", "#00ff00", "#00ffff",
           "#0000ff", "#8000ff", "#ff00ff", "#ffffff", "#44d62c"]


class MainWindow(QMainWindow):
    def __init__(self, mouse) -> None:
        super().__init__()
        self.mouse = mouse
        self.settings = QSettings("OpenDev", "RazerChromaColor")
        self._updating = False
        self.setWindowTitle("DeathAdder Chroma – Farbe")

        # --- colour picker (embedded QColorDialog: hue field + value strip)
        self.picker = QColorDialog()
        self.picker.setOptions(QColorDialog.NoButtons | QColorDialog.DontUseNativeDialog)
        self.picker.setWindowFlags(Qt.Widget)
        self.picker.currentColorChanged.connect(self._from_picker)

        # --- RGB sliders
        self.r = ChannelSlider("R", "#f00")
        self.g = ChannelSlider("G", "#0f0")
        self.b = ChannelSlider("B", "#00f")
        for s in (self.r, self.g, self.b):
            s.valueChanged.connect(self._from_sliders)

        # --- hex code
        self.hex = QLineEdit()
        self.hex.setMaxLength(7)
        self.hex.setValidator(QRegularExpressionValidator(
            QRegularExpression(r"#?[0-9A-Fa-f]{0,6}")))
        self.hex.setPlaceholderText("#44D62C")
        self.hex.editingFinished.connect(self._from_hex)
        self.hex.textEdited.connect(lambda t: len(t.lstrip("#")) == 6 and self._from_hex())

        self.swatch = QFrame()
        self.swatch.setFixedSize(48, 28)
        self.swatch.setFrameShape(QFrame.Box)

        # --- presets
        presets = QGridLayout()
        for i, c in enumerate(PRESETS):
            btn = QPushButton()
            btn.setFixedSize(28, 22)
            btn.setToolTip(c.upper())
            btn.setStyleSheet(f"background:{c};border:1px solid #555;border-radius:3px;")
            btn.clicked.connect(lambda _=False, c=c: self.set_color(QColor(c)))
            presets.addWidget(btn, 0, i)

        # --- device options
        self.zone = QComboBox()
        self.zone.addItems(ZONES.keys())
        self.effect = QComboBox()
        self.effect.addItems(EFFECTS.keys())
        self.brightness = QSlider(Qt.Horizontal)
        self.brightness.setRange(0, 255)
        self.brightness.setValue(255)
        self.bright_lbl = QLabel("100 %")
        self.brightness.valueChanged.connect(
            lambda v: self.bright_lbl.setText(f"{round(v / 2.55)} %"))
        self.live = QCheckBox("Live übernehmen")
        self.live.setChecked(True)
        apply_btn = QPushButton("Übernehmen")
        apply_btn.clicked.connect(self.apply)

        for w in (self.zone, self.effect):
            w.currentIndexChanged.connect(self._schedule)
        self.brightness.valueChanged.connect(self._schedule)

        # --- layout
        rgb_box = QGroupBox("Farbe")
        rgb = QVBoxLayout(rgb_box)
        rgb.addWidget(self.r)
        rgb.addWidget(self.g)
        rgb.addWidget(self.b)
        hex_row = QHBoxLayout()
        hex_row.addWidget(QLabel("Farbcode"))
        hex_row.addWidget(self.hex, 1)
        hex_row.addWidget(self.swatch)
        rgb.addLayout(hex_row)
        rgb.addLayout(presets)

        dev_box = QGroupBox("Maus")
        form = QFormLayout(dev_box)
        form.addRow("Zone", self.zone)
        form.addRow("Effekt", self.effect)
        b_row = QHBoxLayout()
        b_row.addWidget(self.brightness, 1)
        b_row.addWidget(self.bright_lbl)
        form.addRow("Helligkeit", b_row)
        btn_row = QHBoxLayout()
        btn_row.addWidget(self.live)
        btn_row.addStretch()
        btn_row.addWidget(apply_btn)
        form.addRow(btn_row)

        right = QVBoxLayout()
        right.addWidget(rgb_box)
        right.addWidget(dev_box)
        right.addStretch()

        root = QHBoxLayout()
        root.addWidget(self.picker)
        root.addLayout(right)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self.statusBar().showMessage(f"Verbunden: {mouse.name}")

        # debounce: sliders fire on every pixel, the mouse needs ~10 ms per packet
        self._timer = QTimer(self, singleShot=True, interval=60)
        self._timer.timeout.connect(self.apply)

        self._restore()

    # ---- colour sync -----------------------------------------------------
    def set_color(self, c: QColor, source: str = "") -> None:
        if self._updating:
            return
        self._updating = True
        try:
            if source != "picker":
                self.picker.setCurrentColor(c)
            if source != "sliders":
                self.r.setValue(c.red())
                self.g.setValue(c.green())
                self.b.setValue(c.blue())
            if source != "hex":
                self.hex.setText(c.name().upper())
            self.swatch.setStyleSheet(f"background:{c.name()};")
        finally:
            self._updating = False
        self._schedule()

    def color(self) -> QColor:
        return QColor(self.r.value(), self.g.value(), self.b.value())

    def _from_picker(self, c: QColor) -> None:
        self.set_color(c, "picker")

    def _from_sliders(self, _=None) -> None:
        self.set_color(self.color(), "sliders")

    def _from_hex(self) -> None:
        text = self.hex.text().strip().lstrip("#")
        if len(text) != 6:
            return
        self.set_color(QColor("#" + text), "hex")

    # ---- device ----------------------------------------------------------
    def _schedule(self, *_):
        if self.live.isChecked():
            self._timer.start()

    def apply(self) -> None:
        c = self.color()
        effect = EFFECTS[self.effect.currentText()]
        leds = ZONES[self.zone.currentText()]
        try:
            for led in leds:
                if effect is None:
                    self.mouse.send(cmd_led_state(led, False))
                    continue
                self.mouse.send(cmd_led_rgb(led, c.red(), c.green(), c.blue()))
                self.mouse.send(cmd_led_effect(led, effect))
                self.mouse.send(cmd_led_brightness(led, self.brightness.value()))
                self.mouse.send(cmd_led_state(led, True))
            self.statusBar().showMessage(
                f"{self.mouse.name}: {c.name().upper()} · {self.effect.currentText()}"
                f" · {self.zone.currentText()}", 3000)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Fehler: {e}")

    # ---- persistence -----------------------------------------------------
    def _restore(self) -> None:
        self.zone.setCurrentText(self.settings.value("zone", "Logo + Scrollrad"))
        self.effect.setCurrentText(self.settings.value("effect", "Statisch"))
        self.brightness.setValue(int(self.settings.value("brightness", 255)))
        self.set_color(QColor(self.settings.value("color", "#44D62C")))

    def closeEvent(self, ev) -> None:
        self.settings.setValue("color", self.color().name())
        self.settings.setValue("zone", self.zone.currentText())
        self.settings.setValue("effect", self.effect.currentText())
        self.settings.setValue("brightness", self.brightness.value())
        self.mouse.close()
        super().closeEvent(ev)


UDEV_HINT = (
    "Linux udev rule (/etc/udev/rules.d/99-razer-da-chroma.rules):\n"
    '  KERNEL=="hidraw*", ATTRS{idVendor}=="1532", ATTRS{idProduct}=="0043", '
    'MODE="0660", TAG+="uaccess"\n'
    "then: sudo udevadm control --reload && sudo udevadm trigger"
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Razer DeathAdder Chroma LED colour tool",
        epilog=UDEV_HINT, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true",
                    help="GUI ohne Maus; Pakete werden nur auf stdout ausgegeben")
    ap.add_argument("--set", metavar="#RRGGBB",
                    help="Farbe ohne GUI setzen (statisch, Logo + Scrollrad)")
    args = ap.parse_args()

    if args.dry_run:
        mouse = DummyMouse()
    else:
        try:
            mouse = DeathAdderChroma()
        except Exception as e:  # noqa: BLE001
            print(f"[!] {e}\n    -> Simulationsmodus. {UDEV_HINT}", file=sys.stderr)
            if args.set:
                return 1
            mouse = DummyMouse()

    if args.set:
        c = QColor(args.set if args.set.startswith("#") else "#" + args.set)
        if not c.isValid():
            print("Ungültiger Farbcode", file=sys.stderr)
            return 2
        for led in (LED_LOGO, LED_SCROLL):
            mouse.send(cmd_led_rgb(led, c.red(), c.green(), c.blue()))
            mouse.send(cmd_led_effect(led, EFFECT_STATIC))
            mouse.send(cmd_led_state(led, True))
        mouse.close()
        return 0

    app = QApplication(sys.argv)
    win = MainWindow(mouse)
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
