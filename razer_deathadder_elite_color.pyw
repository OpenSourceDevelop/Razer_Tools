#!/usr/bin/env python3
"""
Razer DeathAdder Elite - LED colour tool (PyQt5 + hidapi).

Sibling of razer_deathadder_chroma_color.py. Same 90-byte feature report
framing (OpenMouse-Project/mouse-protocol, src/razer/codec.ts):
  [0]  status        (0x00 in requests, 0x02 = ok in replies)
  [1]  transaction id (DeathAdder Elite 0x005C -> 0x3F, per OpenMouse
       devices.ts TRANSACTION_3F and OpenRazer razermouse_driver.c)
  [5]  data size   [6] command class   [7] command id   [8..] arguments
  [88] checksum = XOR of bytes 2..87

Unlike the DeathAdder Chroma (class 0x03 "standard" LED commands), the Elite
uses the extended-matrix commands (OpenRazer razerchromacommon.c,
razer_chroma_extended_matrix_*):
  0x0F/0x02  set effect      [store, led, effect, ...]
               none      0x00  size 6  [.., 0x00, 0, 0, 0]
               static    0x01  size 9  [.., 0x01, 0, 0, 1, r, g, b]
               breathing 0x02  random: size 6 [.., 0x02, 0, 0, 0]
                               single: size 9 [.., 0x02, 1, 0, 1, r, g, b]
                               dual:  size 12 [.., 0x02, 2, 0, 2, r,g,b, r,g,b]
               spectrum  0x03  size 6  [.., 0x03, 0, 0, 0]
               reactive  0x05  size 9  [.., 0x05, 0, speed 1..4, 1, r, g, b]
  0x0F/0x04  set brightness  [store, led, 0..255]
  0x0F/0x84  get brightness  [store, led, 0]
LED ids: 0x01 scroll wheel, 0x04 logo. Store 0x01 = VARSTORE (persistent).

Requirements:  pip install pyqt5 hidapi
Linux: needs access to /dev/hidraw* (udev rule, see --help) and must not be
       bound by openrazer-driver at the same time.
"""

from __future__ import annotations

import argparse
import sys
import time

from PyQt5.QtCore import QRegularExpression, QSettings, Qt, QTimer, pyqtSignal
from PyQt5.QtGui import QColor, QRegularExpressionValidator
from PyQt5.QtWidgets import (
    QApplication, QCheckBox, QColorDialog, QComboBox, QFormLayout, QFrame,
    QGridLayout, QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMainWindow,
    QPushButton, QSlider, QSpinBox, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

RAZER_VID = 0x1532
DEATHADDER_ELITE_PID = 0x005C

PACKET_LEN = 90
TRANSACTION_ID = 0x3F
VARSTORE = 0x01

STATUS_TEXT = {
    0x00: "new", 0x01: "busy", 0x02: "ok", 0x03: "failure",
    0x04: "timeout", 0x05: "unsupported",
}

LED_SCROLL = 0x01
LED_LOGO = 0x04
ZONES = {"Logo + Scrollrad": (LED_LOGO, LED_SCROLL),
         "Logo": (LED_LOGO,),
         "Scrollrad": (LED_SCROLL,)}

# GUI label -> effect key
EFFECTS = {"Statisch": "static",
           "Atmen (1 Farbe)": "breathing-single",
           "Atmen (2 Farben)": "breathing-dual",
           "Atmen (zufällig)": "breathing-random",
           "Reaktiv": "reactive",
           "Spektrum": "spectrum",
           "Aus": "none"}
REACTIVE_SPEEDS = {"Schnell": 1, "Mittel": 2, "Langsam": 3, "Sehr langsam": 4}


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


def cmd_effect(led: int, effect: str, c1: QColor | None = None,
               c2: QColor | None = None, speed: int = 2) -> bytes:
    def rgb(c: QColor) -> list[int]:
        return [c.red(), c.green(), c.blue()]

    head = [VARSTORE, led]
    if effect == "none":
        args = head + [0x00, 0x00, 0x00, 0x00]
    elif effect == "static":
        args = head + [0x01, 0x00, 0x00, 0x01] + rgb(c1)
    elif effect == "breathing-random":
        args = head + [0x02, 0x00, 0x00, 0x00]
    elif effect == "breathing-single":
        args = head + [0x02, 0x01, 0x00, 0x01] + rgb(c1)
    elif effect == "breathing-dual":
        args = head + [0x02, 0x02, 0x00, 0x02] + rgb(c1) + rgb(c2)
    elif effect == "spectrum":
        args = head + [0x03, 0x00, 0x00, 0x00]
    elif effect == "reactive":
        args = head + [0x05, 0x00, max(1, min(4, speed)), 0x01] + rgb(c1)
    else:
        raise RazerError(f"Unbekannter Effekt {effect}")
    return encode(0x0F, 0x02, args)


def cmd_brightness(led: int, value: int) -> bytes:
    return encode(0x0F, 0x04, [VARSTORE, led, max(0, min(255, value))])


def cmd_get_brightness(led: int) -> bytes:
    return encode(0x0F, 0x84, [VARSTORE, led, 0x00])


# --------------------------------------------------------------------------- #
# Device access
# --------------------------------------------------------------------------- #

class DummyMouse:
    """Stand-in used with --dry-run or when no mouse is found."""
    name = "Simulation (keine Maus)"

    def send(self, packet: bytes) -> bytes:
        print("TX", packet[:20].hex(" "), "... chk", f"{packet[88]:02x}")
        reply = bytearray(packet)
        reply[0] = 0x02
        if packet[6:8] == b"\x0f\x84":
            reply[10] = 0xFF
        reply[88] = checksum(reply)
        return bytes(reply)

    def close(self) -> None:
        pass


class DeathAdderElite:
    name = "Razer DeathAdder Elite"

    def __init__(self) -> None:
        import hid  # hidapi

        candidates = hid.enumerate(RAZER_VID, DEATHADDER_ELITE_PID)
        if not candidates:
            raise RazerError("DeathAdder Elite (1532:005C) nicht gefunden.")
        # Control channel = Generic Desktop *Mouse* collection (0x0001:0x0002)
        # on interface 0. Windows lists every top-level collection as its own
        # path and locks the keyboard ones, so try 1:2 first.
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
                self._probe()
                return
            except Exception as e:  # noqa: BLE001 - try next collection
                last_err = e
                try:
                    dev.close()
                except Exception:  # noqa: BLE001
                    pass
        raise RazerError(f"Keine Steuer-Schnittstelle nutzbar: {last_err}")

    def _probe(self) -> None:
        # Firmware read (0x00/0x81) - verifies we reached the control channel.
        # OpenMouse test tool (0x3F, Windows, wired) reports "Mouse 1.6".
        reply = self._exchange(encode(0x00, 0x81, [0x00, 0x00]))
        self.firmware = f"{reply[8]}.{reply[9]}"
        self.name = f"Razer DeathAdder Elite (FW {self.firmware})"

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

    def send(self, packet: bytes) -> bytes:
        return self._exchange(packet)

    def close(self) -> None:
        self._dev.close()


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

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


def hex_edit() -> QLineEdit:
    e = QLineEdit()
    e.setMaxLength(7)
    e.setValidator(QRegularExpressionValidator(QRegularExpression(r"#?[0-9A-Fa-f]{0,6}")))
    e.setPlaceholderText("#44D62C")
    return e


class MainWindow(QMainWindow):
    def __init__(self, mouse) -> None:
        super().__init__()
        self.mouse = mouse
        self.settings = QSettings("OpenDev", "RazerEliteColor")
        self._updating = False
        self.color2 = QColor("#0000ff")
        self.setWindowTitle("DeathAdder Elite – Farbe")

        # --- colour picker (embedded QColorDialog)
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

        # --- hex code, colour 1
        self.hex = hex_edit()
        self.hex.editingFinished.connect(self._from_hex)
        self.hex.textEdited.connect(lambda t: len(t.lstrip("#")) == 6 and self._from_hex())
        self.swatch = QFrame()
        self.swatch.setFixedSize(48, 28)
        self.swatch.setFrameShape(QFrame.Box)

        # --- colour 2 (only for dual breathing)
        self.hex2 = hex_edit()
        self.hex2.editingFinished.connect(self._from_hex2)
        self.hex2.textEdited.connect(lambda t: len(t.lstrip("#")) == 6 and self._from_hex2())
        self.swatch2 = QPushButton()
        self.swatch2.setFixedSize(48, 28)
        self.swatch2.setToolTip("Zweite Farbe wählen")
        self.swatch2.clicked.connect(self._pick_color2)

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
        self.speed = QComboBox()
        self.speed.addItems(REACTIVE_SPEEDS.keys())
        self.speed.setCurrentText("Mittel")
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

        for w in (self.zone, self.effect, self.speed):
            w.currentIndexChanged.connect(self._schedule)
        self.effect.currentIndexChanged.connect(self._update_enabled)
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
        self.row2 = QWidget()
        hex2_row = QHBoxLayout(self.row2)
        hex2_row.setContentsMargins(0, 0, 0, 0)
        hex2_row.addWidget(QLabel("2. Farbe"))
        hex2_row.addWidget(self.hex2, 1)
        hex2_row.addWidget(self.swatch2)
        rgb.addWidget(self.row2)

        dev_box = QGroupBox("Maus")
        form = QFormLayout(dev_box)
        form.addRow("Zone", self.zone)
        form.addRow("Effekt", self.effect)
        form.addRow("Tempo", self.speed)
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
        self._update_enabled()

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

    def set_color2(self, c: QColor, from_hex: bool = False) -> None:
        self.color2 = c
        if not from_hex:
            self.hex2.setText(c.name().upper())
        self.swatch2.setStyleSheet(f"background:{c.name()};border:1px solid #555;")
        self._schedule()

    def color(self) -> QColor:
        return QColor(self.r.value(), self.g.value(), self.b.value())

    def _from_picker(self, c: QColor) -> None:
        self.set_color(c, "picker")

    def _from_sliders(self, _=None) -> None:
        self.set_color(self.color(), "sliders")

    def _from_hex(self) -> None:
        text = self.hex.text().strip().lstrip("#")
        if len(text) == 6:
            self.set_color(QColor("#" + text), "hex")

    def _from_hex2(self) -> None:
        text = self.hex2.text().strip().lstrip("#")
        if len(text) == 6:
            self.set_color2(QColor("#" + text), from_hex=True)

    def _pick_color2(self) -> None:
        c = QColorDialog.getColor(self.color2, self, "Zweite Farbe")
        if c.isValid():
            self.set_color2(c)

    def _update_enabled(self, *_):
        eff = EFFECTS[self.effect.currentText()]
        self.row2.setEnabled(eff == "breathing-dual")
        self.speed.setEnabled(eff == "reactive")
        uses_color = eff in ("static", "breathing-single", "breathing-dual", "reactive")
        for w in (self.picker, self.r, self.g, self.b, self.hex):
            w.setEnabled(uses_color)

    # ---- device ----------------------------------------------------------
    def _schedule(self, *_):
        if self.live.isChecked():
            self._timer.start()

    def apply(self) -> None:
        eff = EFFECTS[self.effect.currentText()]
        speed = REACTIVE_SPEEDS[self.speed.currentText()]
        try:
            for led in ZONES[self.zone.currentText()]:
                self.mouse.send(cmd_effect(led, eff, self.color(), self.color2, speed))
                if eff != "none":
                    self.mouse.send(cmd_brightness(led, self.brightness.value()))
            self.statusBar().showMessage(
                f"{self.mouse.name}: {self.color().name().upper()} · "
                f"{self.effect.currentText()} · {self.zone.currentText()}", 3000)
        except Exception as e:  # noqa: BLE001
            self.statusBar().showMessage(f"Fehler: {e}")

    # ---- persistence -----------------------------------------------------
    def _restore(self) -> None:
        self.zone.setCurrentText(self.settings.value("zone", "Logo + Scrollrad"))
        self.effect.setCurrentText(self.settings.value("effect", "Statisch"))
        self.speed.setCurrentText(self.settings.value("speed", "Mittel"))
        self.brightness.setValue(int(self.settings.value("brightness", 255)))
        self.set_color2(QColor(self.settings.value("color2", "#0000FF")))
        self.set_color(QColor(self.settings.value("color", "#44D62C")))

    def closeEvent(self, ev) -> None:
        self.settings.setValue("color", self.color().name())
        self.settings.setValue("color2", self.color2.name())
        self.settings.setValue("zone", self.zone.currentText())
        self.settings.setValue("effect", self.effect.currentText())
        self.settings.setValue("speed", self.speed.currentText())
        self.settings.setValue("brightness", self.brightness.value())
        self.mouse.close()
        super().closeEvent(ev)


UDEV_HINT = (
    "Linux udev rule (/etc/udev/rules.d/99-razer-da-elite.rules):\n"
    '  KERNEL=="hidraw*", ATTRS{idVendor}=="1532", ATTRS{idProduct}=="005c", '
    'MODE="0660", TAG+="uaccess"\n'
    "then: sudo udevadm control --reload && sudo udevadm trigger"
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Razer DeathAdder Elite LED colour tool",
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
            mouse = DeathAdderElite()
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
            mouse.send(cmd_effect(led, "static", c))
        mouse.close()
        return 0

    app = QApplication(sys.argv)
    win = MainWindow(mouse)
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
