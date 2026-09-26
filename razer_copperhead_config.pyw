#!/usr/bin/env python3
"""
Razer Copperhead (1532:0101) - profile / DPI / polling-rate tool (PyQt5).

Protocol ported from razercfg by Michael Buesch (librazer/hw_copperhead.c,
reverse engineered, GPLv2). Unlike later Razer mice this is NOT a HID feature
report protocol but raw USB class control transfers, recipient "other":

  OUT  bmRequestType 0x23, bRequest 0x09 (SET_CONFIGURATION)
  IN   bmRequestType 0xA3, bRequest 0x01 (CLEAR_FEATURE)

  read active profile    IN  wValue=1 wIndex=0  len 1
  request profile N      OUT wValue=2 wIndex=3  [N]         (N = 1..5)
  read requested profile IN  wValue=1 wIndex=0  len 342  -> struct[6:]
  upload profile chunk   OUT wValue=j wIndex=0  64 bytes    (j = 1..6)
  commit profile N       OUT wValue=2 wIndex=3  [N]
  select active profile  OUT wValue=2 wIndex=1  [N]

Profile struct (0x15C = 348 bytes, little endian):
  0 packetlength  2 magic(0x0002)  4 profilenr
  6 reply_packetlength  8 reply_magic  10 reply_profilenr
  12 dpisel (1=2000 2=1600 3=800 4=400)
  13 freq   (1=1000 2=500 3=125 Hz)
  14..345 button map (332 bytes, passed through untouched)
  346 checksum = XOR of all 16-bit LE words before it

Safety: a profile is only written back if it was read with a valid checksum,
and its button map is copied verbatim - this tool never builds one itself.

Requirements:  pip install pyqt5 libusb1
Windows: install UsbDk (github.com/daynix/UsbDk/releases). libusb then reaches
         the mouse without replacing its HID driver. The mouse drops out for a
         moment while the tool reads or writes, then comes back.
Linux:   udev rule, see --help. The kernel driver is detached for the transfer
         and re-attached afterwards.
"""

from __future__ import annotations

import argparse
import struct
import sys
import time
from dataclasses import dataclass, field

from PyQt5.QtCore import Qt
from PyQt5.QtWidgets import (
    QApplication, QButtonGroup, QGridLayout, QGroupBox, QHBoxLayout, QLabel,
    QMainWindow, QMessageBox, QPushButton, QRadioButton, QVBoxLayout, QWidget,
)

# --------------------------------------------------------------------------- #
# Protocol
# --------------------------------------------------------------------------- #

VID, PID = 0x1532, 0x0101
NR_PROFILES = 5
STRUCT_LEN = 0x15C           # 348
READ_LEN = STRUCT_LEN - 6    # 342
CHUNK, NR_CHUNKS = 64, 6     # 384 byte upload buffer
MAGIC = 0x0002
REQ_OUT = 0x23               # host->device | class | other
REQ_IN = 0xA3                # device->host | class | other
SET_CONFIGURATION = 0x09
CLEAR_FEATURE = 0x01
TIMEOUT_MS = 3000
COMMIT_SPACING_S = 0.25      # razercfg waits 250 ms between commits

DPI_TO_SEL = {400: 4, 800: 3, 1600: 2, 2000: 1}
SEL_TO_DPI = {v: k for k, v in DPI_TO_SEL.items()}
HZ_TO_SEL = {125: 3, 500: 2, 1000: 1}
SEL_TO_HZ = {v: k for k, v in HZ_TO_SEL.items()}


class CopperheadError(Exception):
    pass


def xor16(buf: bytes | bytearray) -> int:
    s = 0
    for i in range(0, len(buf), 2):
        s ^= buf[i]
        if i + 1 < len(buf):
            s ^= buf[i + 1] << 8
    return s


@dataclass
class Profile:
    nr: int                          # 1..5
    dpi: int = 400
    hz: int = 125
    buttonmap: bytes = b""           # raw, as read from the mouse
    valid: bool = False              # read with good checksum

    @classmethod
    def from_reply(cls, nr: int, data: bytes) -> "Profile":
        buf = bytes(6) + bytes(data)
        if len(buf) != STRUCT_LEN:
            raise CopperheadError(f"Profil {nr}: {len(data)} statt {READ_LEN} Bytes")
        ok = xor16(buf) == 0
        reply_nr = struct.unpack_from("<H", buf, 10)[0]
        dpisel, freq = buf[12], buf[13]
        return cls(nr=nr,
                   dpi=SEL_TO_DPI.get(dpisel, 400),
                   hz=SEL_TO_HZ.get(freq, 125),
                   buttonmap=buf[14:346],
                   valid=ok and reply_nr == nr
                   and dpisel in SEL_TO_DPI and freq in SEL_TO_HZ)

    def encode(self) -> bytes:
        if not self.valid or len(self.buttonmap) != 332:
            raise CopperheadError(f"Profil {self.nr} wurde nicht sauber gelesen "
                                  "- Schreiben verweigert.")
        buf = bytearray(CHUNK * NR_CHUNKS)
        struct.pack_into("<HHHHHH", buf, 0, STRUCT_LEN, MAGIC, self.nr,
                         0, 0, self.nr)
        buf[12] = DPI_TO_SEL[self.dpi]
        buf[13] = HZ_TO_SEL[self.hz]
        buf[14:346] = self.buttonmap
        struct.pack_into("<H", buf, 346, xor16(buf[:346]))
        return bytes(buf)


# --------------------------------------------------------------------------- #
# Transport
# --------------------------------------------------------------------------- #

class UsbLink:
    """Opens the mouse only for the duration of one read or write session,
    so it is back to being a normal mouse the rest of the time."""

    def __init__(self) -> None:
        import usb1  # python-libusb1
        self._usb1 = usb1
        self.backend = ""

    def __enter__(self) -> "UsbLink":
        usb1 = self._usb1
        errors = []
        for usbdk in ((True, False) if sys.platform == "win32" else (False,)):
            try:
                self.ctx = usb1.USBContext(use_usbdk=usbdk)
                self.ctx.open()
                self.h = self.ctx.openByVendorIDAndProductID(VID, PID)
                if self.h is None:
                    raise CopperheadError("Copperhead (1532:0101) nicht gefunden.")
                self.backend = "UsbDk" if usbdk else "libusb"
                break
            except Exception as e:  # noqa: BLE001
                errors.append(f"{'UsbDk' if usbdk else 'libusb'}: {e}")
                try:
                    self.ctx.close()
                except Exception:  # noqa: BLE001
                    pass
        else:
            raise CopperheadError("; ".join(errors))
        self._claimed = []
        if sys.platform.startswith("linux"):
            self.h.setAutoDetachKernelDriver(True)
        for iface in (0, 1):                 # razercfg claims both
            try:
                self.h.claimInterface(iface)
                self._claimed.append(iface)
            except Exception:  # noqa: BLE001 - 2nd iface may not exist
                if iface == 0:
                    raise
        return self

    def __exit__(self, *exc) -> None:
        for iface in self._claimed:
            try:
                self.h.releaseInterface(iface)
            except Exception:  # noqa: BLE001
                pass
        self.h.close()
        self.ctx.close()

    def write(self, value: int, index: int, data: bytes) -> None:
        n = self.h.controlWrite(REQ_OUT, SET_CONFIGURATION, value, index,
                                data, TIMEOUT_MS)
        if n != len(data):
            raise CopperheadError(f"USB write {value:#x}/{index:#x}: {n} von {len(data)} Bytes")

    def read(self, value: int, index: int, length: int) -> bytes:
        data = self.h.controlRead(REQ_IN, CLEAR_FEATURE, value, index,
                                  length, TIMEOUT_MS)
        if len(data) != length:
            raise CopperheadError(f"USB read {value:#x}/{index:#x}: {len(data)} von {length} Bytes")
        return bytes(data)


class FakeLink:
    """Simulated mouse for --dry-run and tests."""
    backend = "Simulation"

    def __init__(self) -> None:
        self.active = 1
        self.store = {}
        for n in range(1, NR_PROFILES + 1):
            p = Profile(n, dpi=[400, 800, 1600, 2000, 800][n - 1], hz=500,
                        buttonmap=bytes(range(1, 8)) * 47 + bytes(3), valid=True)
            self.store[n] = bytearray(p.encode()[:STRUCT_LEN])
        self._req = 1
        self._upload = bytearray(CHUNK * NR_CHUNKS)

    def __enter__(self): return self
    def __exit__(self, *e): pass

    def write(self, value, index, data):
        if value == 2 and index == 3:
            if self._upload[4:6] == bytes([data[0], 0]) and any(self._upload):
                self.store[data[0]] = bytearray(self._upload[:STRUCT_LEN])
                self._upload = bytearray(CHUNK * NR_CHUNKS)
            self._req = data[0]
        elif value == 2 and index == 1:
            self.active = data[0]
        elif 1 <= value <= 6 and index == 0:
            self._upload[(value - 1) * 64:value * 64] = data

    def read(self, value, index, length):
        if length == 1:
            return bytes([self.active])
        s = bytearray(self.store[self._req])
        s[0:6] = bytes(6)                 # device only answers bytes 6..
        struct.pack_into("<H", s, 346, 0)
        struct.pack_into("<H", s, 346, xor16(s[:346]))
        return bytes(s[6:])


# --------------------------------------------------------------------------- #
# Driver
# --------------------------------------------------------------------------- #

@dataclass
class DeviceState:
    active: int = 1
    profiles: list[Profile] = field(default_factory=list)


def read_all(link_factory) -> tuple[DeviceState, str]:
    with link_factory() as link:
        active = link.read(1, 0, 1)[0]
        if not 1 <= active <= NR_PROFILES:
            active = 1
        profiles = []
        for n in range(1, NR_PROFILES + 1):
            link.write(2, 3, bytes([n]))
            profiles.append(Profile.from_reply(n, link.read(1, 0, READ_LEN)))
        return DeviceState(active, profiles), link.backend


def write_changes(link_factory, new: DeviceState, old: DeviceState) -> list[str]:
    log = []
    changed = [p for p, o in zip(new.profiles, old.profiles)
               if (p.dpi, p.hz) != (o.dpi, o.hz)]
    with link_factory() as link:
        for p in changed:
            buf = p.encode()
            time.sleep(COMMIT_SPACING_S)
            for j in range(NR_CHUNKS):
                link.write(j + 1, 0, buf[j * CHUNK:(j + 1) * CHUNK])
            try:
                link.write(2, 3, bytes([p.nr]))      # razercfg ignores errors here
            except Exception:  # noqa: BLE001
                pass
            back = Profile.from_reply(p.nr, link.read(1, 0, READ_LEN))
            if not back.valid or (back.dpi, back.hz) != (p.dpi, p.hz) \
                    or back.buttonmap != p.buttonmap:
                raise CopperheadError(f"Profil {p.nr}: Rücklesen stimmt nicht "
                                      f"({back.dpi} DPI/{back.hz} Hz)")
            log.append(f"P{p.nr}: {p.dpi} DPI / {p.hz} Hz")
        if new.active != old.active or changed:
            link.write(2, 1, bytes([new.active]))
            if new.active != old.active:
                log.append(f"aktiv: P{new.active}")
    return log


# --------------------------------------------------------------------------- #
# GUI
# --------------------------------------------------------------------------- #

def segmented(options: list[int], unit: str) -> tuple[QWidget, QButtonGroup]:
    w = QWidget()
    lay = QHBoxLayout(w)
    lay.setContentsMargins(0, 0, 0, 0)
    lay.setSpacing(0)
    grp = QButtonGroup(w)
    for i, v in enumerate(options):
        b = QPushButton(f"{v}")
        b.setCheckable(True)
        b.setToolTip(f"{v} {unit}")
        b.setMinimumWidth(58)
        radius = ("border-top-left-radius:4px;border-bottom-left-radius:4px;" if i == 0 else
                  "border-top-right-radius:4px;border-bottom-right-radius:4px;"
                  if i == len(options) - 1 else "")
        b.setStyleSheet(
            "QPushButton{border:1px solid #888;padding:4px 8px;border-radius:0;"
            f"{radius}background:palette(button);}}"
            "QPushButton:checked{background:#2a7fd4;color:white;border-color:#1f5f9f;}")
        grp.addButton(b, v)
        lay.addWidget(b)
    return w, grp


class MainWindow(QMainWindow):
    def __init__(self, link_factory) -> None:
        super().__init__()
        self.link_factory = link_factory
        self.device: DeviceState | None = None
        self.setWindowTitle("Razer Copperhead – Profile")

        box = QGroupBox("Profile")
        grid = QGridLayout(box)
        grid.setHorizontalSpacing(14)
        for col, text in enumerate(["Aktiv", "Profil", "Empfindlichkeit (DPI)", "Abtastrate (Hz)", ""]):
            lbl = QLabel(f"<b>{text}</b>")
            grid.addWidget(lbl, 0, col)

        self.active_grp = QButtonGroup(self)
        self.dpi_grps, self.hz_grps, self.marks = [], [], []
        for n in range(1, NR_PROFILES + 1):
            rb = QRadioButton()
            self.active_grp.addButton(rb, n)
            dpi_w, dpi_g = segmented(list(DPI_TO_SEL), "DPI")
            hz_w, hz_g = segmented(list(HZ_TO_SEL), "Hz")
            mark = QLabel("")
            mark.setFixedWidth(16)
            for g in (dpi_g, hz_g):
                g.idClicked.connect(self._refresh_marks)
            grid.addWidget(rb, n, 0, Qt.AlignCenter)
            grid.addWidget(QLabel(f"Profil {n}"), n, 1)
            grid.addWidget(dpi_w, n, 2)
            grid.addWidget(hz_w, n, 3)
            grid.addWidget(mark, n, 4)
            self.dpi_grps.append(dpi_g)
            self.hz_grps.append(hz_g)
            self.marks.append(mark)
        self.active_grp.idClicked.connect(self._refresh_marks)

        hint = QLabel("Tastenbelegung wird unverändert übernommen. "
                      "Beim Lesen/Schreiben ist die Maus kurz weg.")
        hint.setStyleSheet("color:palette(mid);")
        hint.setWordWrap(True)

        self.read_btn = QPushButton("Von Maus lesen")
        self.write_btn = QPushButton("Auf Maus schreiben")
        self.revert_btn = QPushButton("Verwerfen")
        self.read_btn.clicked.connect(self.load)
        self.write_btn.clicked.connect(self.save)
        self.revert_btn.clicked.connect(lambda: self._show(self.device))
        btns = QHBoxLayout()
        btns.addWidget(self.read_btn)
        btns.addStretch()
        btns.addWidget(self.revert_btn)
        btns.addWidget(self.write_btn)

        root = QVBoxLayout()
        root.addWidget(box)
        root.addWidget(hint)
        root.addLayout(btns)
        central = QWidget()
        central.setLayout(root)
        self.setCentralWidget(central)
        self._set_enabled(False)
        self.load()

    # ---- state <-> widgets ----------------------------------------------
    def _show(self, st: DeviceState | None) -> None:
        if st is None:
            return
        self.active_grp.button(st.active).setChecked(True)
        for i, p in enumerate(st.profiles):
            self.dpi_grps[i].button(p.dpi).setChecked(True)
            self.hz_grps[i].button(p.hz).setChecked(True)
            for g in (self.dpi_grps[i], self.hz_grps[i]):
                for b in g.buttons():
                    b.setEnabled(p.valid)
            if not p.valid:
                self.marks[i].setText("⚠")
                self.marks[i].setToolTip("Checksumme ungültig – Profil gesperrt")
        self._refresh_marks()

    def _edited(self) -> DeviceState:
        profiles = [Profile(p.nr, self.dpi_grps[i].checkedId(),
                            self.hz_grps[i].checkedId(), p.buttonmap, p.valid)
                    for i, p in enumerate(self.device.profiles)]
        return DeviceState(self.active_grp.checkedId(), profiles)

    def _refresh_marks(self, *_):
        if not self.device:
            return
        new = self._edited()
        dirty = new.active != self.device.active
        for i, (p, o) in enumerate(zip(new.profiles, self.device.profiles)):
            if not o.valid:
                continue
            ch = (p.dpi, p.hz) != (o.dpi, o.hz)
            dirty |= ch
            self.marks[i].setText("●" if ch else "")
            self.marks[i].setToolTip("geändert" if ch else "")
            self.marks[i].setStyleSheet("color:#d4892a;")
        self.write_btn.setEnabled(dirty)
        self.revert_btn.setEnabled(dirty)

    def _set_enabled(self, on: bool) -> None:
        for w in (self.write_btn, self.revert_btn):
            w.setEnabled(on)
        for g in self.dpi_grps + self.hz_grps + [self.active_grp]:
            for b in g.buttons():
                b.setEnabled(on)

    # ---- device -----------------------------------------------------------
    def load(self) -> None:
        self.statusBar().showMessage("Lese Maus …")
        QApplication.processEvents()
        try:
            self.device, backend = read_all(self.link_factory)
        except Exception as e:  # noqa: BLE001
            self._set_enabled(False)
            self.statusBar().showMessage(f"Fehler: {e}")
            return
        self._set_enabled(True)
        self._show(self.device)
        bad = [p.nr for p in self.device.profiles if not p.valid]
        msg = f"Copperhead gelesen ({backend}) · aktiv: Profil {self.device.active}"
        if bad:
            msg += f" · Profil {', '.join(map(str, bad))} ungültig, gesperrt"
        self.statusBar().showMessage(msg)

    def save(self) -> None:
        new = self._edited()
        self.statusBar().showMessage("Schreibe …")
        QApplication.processEvents()
        try:
            log = write_changes(self.link_factory, new, self.device)
        except Exception as e:  # noqa: BLE001
            QMessageBox.warning(self, "Schreiben fehlgeschlagen", str(e))
            self.load()
            return
        self.load()
        self.statusBar().showMessage("Geschrieben: " + ", ".join(log), 6000)


UDEV_HINT = (
    "Windows: UsbDk installieren (github.com/daynix/UsbDk/releases), KEIN Zadig.\n"
    "Linux udev rule (/etc/udev/rules.d/99-razer-copperhead.rules):\n"
    '  SUBSYSTEM=="usb", ATTRS{idVendor}=="1532", ATTRS{idProduct}=="0101", '
    'MODE="0660", TAG+="uaccess"'
)


def main() -> int:
    ap = argparse.ArgumentParser(
        description="Razer Copperhead Profil-/DPI-Tool", epilog=UDEV_HINT,
        formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--dry-run", action="store_true", help="simulierte Maus")
    args = ap.parse_args()

    if args.dry_run:
        fake = FakeLink()
        factory = lambda: fake  # noqa: E731
    else:
        factory = UsbLink

    app = QApplication(sys.argv)
    win = MainWindow(factory)
    win.show()
    return app.exec_()


if __name__ == "__main__":
    sys.exit(main())
