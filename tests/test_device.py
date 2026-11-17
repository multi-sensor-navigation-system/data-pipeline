"""Per-device pin settings: command encoding, the conversation with the
board, the saved profiles and `nav pins`."""
import json

import pytest

from navkit import cli, device, wire

# Frames produced by the firmware's own encoder (firmware/include/wire.h,
# wire_frame) for the same inputs, so both ends are checked against each other.
FW_SET_PINS = bytes.fromhex("081015161011144200")          # sda 21, scl 22, gps_rx 16, gps_tx 17
FW_SET_PINS_ZEROS = bytes.fromhex("02100102040392d900")    # 0, 0, 4, 0: zero bytes are stuffed
FW_RESET_PINS = bytes.fromhex("0411e0e300")
FW_ACK_OK = bytes.fromhex("03051004ff166500")              # cmd 0x10, status 0, no signal
FW_ACK_REFUSED = bytes.fromhex("0705100201a50d00")         # cmd 0x10, status 2, signal scl
FW_HELLO = bytes.fromhex(
    "9b0101626f6172643d65737033326465763b66773d302e322e303b6769743d616263313233343b696d755f687a3d3530303b"
    "6d61675f687a3d37353b6261726f5f687a3d32353b6770735f687a3d313b73656e736f72733d696d752c6d61672c6261726f"
    "2c6770733b69643d3763646661316532623363343b70696e733d32312c32322c31362c31373b70696e5f7372633d64656661"
    "756c744b6200")

DEFAULTS = {"sda": 21, "scl": 22, "gps_rx": 16, "gps_tx": 17}
DEVICE_ID = "7cdfa1e2b3c4"

# The esp32dev rules as the firmware release manifest publishes them.
RULES = {
    "gpio": [0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19,
             21, 22, 23, 25, 26, 27, 32, 33, 34, 35, 36, 37, 38, 39],
    "input_only": [34, 35, 36, 37, 38, 39],
    "blocked": {"0": "strapping pin: selects download mode at reset", "2": "strapping pin", "5": "strapping pin",
                "12": "strapping pin", "15": "strapping pin", "1": "UART0 TX, the host link",
                "3": "UART0 RX, the host link", **{str(g): "connected to the SPI flash" for g in range(6, 12)}},
}


# ── encoding ───────────────────────────────────────────────────────────────────
def test_commands_match_the_firmware_encoder():
    assert wire.set_pins_frame(DEFAULTS) == FW_SET_PINS
    assert wire.set_pins_frame({"sda": 0, "scl": 0, "gps_rx": 4, "gps_tx": 0}) == FW_SET_PINS_ZEROS
    assert wire.reset_pins_frame() == FW_RESET_PINS


@pytest.mark.parametrize("body", [b"", b"\x00", b"\x00\x00", b"\x01\x00\x02", bytes(range(256)) * 2,
                                  b"\x07" * 254, b"\x07" * 255, b"\x00" * 300])
def test_encode_then_decode_round_trips(body):
    frame = wire.encode_frame(0x42, body)
    assert frame.endswith(b"\x00") and 0 not in frame[:-1]
    assert wire.decode_frame(frame[:-1]) == (0x42, body)


def test_firmware_frames_decode():
    dec = wire.StreamDecoder()
    events = dec.feed(FW_HELLO + FW_ACK_OK + FW_ACK_REFUSED)
    assert [e.kind for e in events] == ["hello", "ack", "ack"]
    hello = events[0].payload
    assert hello["id"] == DEVICE_ID and hello["pin_src"] == "default"
    assert wire.hello_pins(hello) == DEFAULTS
    assert events[1].payload == {"cmd": wire.SET_PINS, "status": 0, "signal": None}
    assert events[2].payload == {"cmd": wire.SET_PINS, "status": 2, "signal": "scl"}


def test_hello_without_pins_is_old_firmware():
    assert wire.hello_pins({"proto": 1, "board": "esp32dev"}) is None
    assert wire.hello_pins({"pins": "21,22,16"}) is None
    assert wire.hello_pins({"pins": "a,b,c,d"}) is None


# ── checking a choice ──────────────────────────────────────────────────────────
def test_pin_errors_explain_every_problem():
    assert device.pin_errors(DEFAULTS, RULES) == []
    assert device.pin_errors({**DEFAULTS, "gps_rx": 34}, RULES) == []       # a receive pin may be input-only
    errors = device.pin_errors({"sda": 6, "scl": 34, "gps_rx": 24, "gps_tx": 6}, RULES)
    text = "\n".join(errors)
    assert "sda: GPIO 6 is connected to the SPI flash" in text
    assert "scl: GPIO 34 is input-only" in text
    assert "gps_rx: GPIO 24 does not exist" in text
    assert "GPIO 6 is assigned to more than one signal (sda, gps_tx)" in text


# ── a stand-in for the board ───────────────────────────────────────────────────
class Clock:
    def __init__(self):
        self.now = 0.0

    def __call__(self):
        return self.now

    def advance(self, dt):
        self.now += dt


class FakeBoard:
    """Behaves like the firmware on a serial port: repeats its hello, checks
    SET_PINS against RULES, answers with an ACK and 'restarts'."""

    def __init__(self, pins=None, stored=False, drop_commands=0, firmware="new"):
        self.pins = dict(pins or DEFAULTS)
        self.stored = stored
        self.drop_commands = drop_commands      # lose this many incoming commands, as a noisy link would
        self.firmware = firmware
        self.clock = Clock()
        self.rx = bytearray()
        self.tx = bytearray()
        self.received = []
        self.next_hello = 0.0

    @property
    def in_waiting(self) -> int:
        return len(self.tx)

    def _hello(self) -> bytes:
        text = "board=esp32dev;fw=0.2.0;git=abc1234;sensors=imu,gps"
        if self.firmware == "new":
            pins = ",".join(str(self.pins[s]) for s in wire.PIN_SIGNALS)
            text += f";id={DEVICE_ID};pins={pins};pin_src={'stored' if self.stored else 'default'}"
        return wire.encode_frame(wire.HELLO, b"\x01" + text.encode())

    def write(self, data: bytes) -> None:
        self.rx += data
        while 0 in self.rx:
            end = self.rx.index(0)
            frame, self.rx = bytes(self.rx[:end]), self.rx[end + 1:]
            if not frame or self.firmware == "old":
                continue
            if self.drop_commands:
                self.drop_commands -= 1
                continue
            ftype, body = wire.decode_frame(frame)
            self.received.append(ftype)
            status, signal = 0, 0xFF
            if ftype == wire.SET_PINS:
                pins = dict(zip(wire.PIN_SIGNALS, body))
                for i, (name, gpio) in enumerate(pins.items()):     # same order of checks as the firmware
                    if gpio not in RULES["gpio"] or str(gpio) in RULES["blocked"]:
                        status = 2
                    elif name != "gps_rx" and gpio in RULES["input_only"]:
                        status = 3
                    elif gpio in body[:i]:
                        status = 4
                    if status:
                        signal = i
                        break
                if status == 0:
                    self.pins, self.stored = pins, True
            elif ftype == wire.RESET_PINS:
                self.pins, self.stored = dict(DEFAULTS), False
            else:
                status = 6
            self.tx += wire.encode_frame(wire.ACK, bytes([ftype, status, signal]))

    def read(self, n: int = 1) -> bytes:
        self.clock.advance(0.05)                # every read costs a serial timeout
        if self.clock() >= self.next_hello:     # the periodic hello, every 2 s
            self.tx += self._hello()
            self.next_hello = self.clock() + 2.0
        out, self.tx = bytes(self.tx[:n]), self.tx[n:]
        return out


def link_to(board):
    return device.Link(board, clock=board.clock)


# ── the conversation ───────────────────────────────────────────────────────────
def test_identify_reports_id_and_pins():
    info, pins = device.identify(link_to(FakeBoard()))
    assert info["id"] == DEVICE_ID and pins == DEFAULTS


def test_identify_rejects_firmware_without_pin_settings():
    with pytest.raises(device.DeviceError, match="does not support pin settings"):
        device.identify(link_to(FakeBoard(firmware="old")))


def test_set_pins_is_stored_and_confirmed_by_the_next_hello():
    board = FakeBoard()
    wanted = {"sda": 25, "scl": 26, "gps_rx": 34, "gps_tx": 27}
    hello = device.set_pins(link_to(board), wanted)
    assert board.pins == wanted and board.stored
    assert wire.hello_pins(hello) == wanted and hello["pin_src"] == "stored"


def test_refused_pins_name_the_signal_and_change_nothing():
    board = FakeBoard()
    with pytest.raises(device.DeviceError, match=r"scl: GPIO 12 is not available on this board"):
        device.set_pins(link_to(board), {**DEFAULTS, "scl": 12})
    with pytest.raises(device.DeviceError, match=r"sda: GPIO 35 is input-only"):
        device.set_pins(link_to(board), {**DEFAULTS, "sda": 35})
    with pytest.raises(device.DeviceError, match=r"scl: GPIO 21 is already used"):
        device.set_pins(link_to(board), {**DEFAULTS, "scl": 21})
    assert board.pins == DEFAULTS and not board.stored


def test_a_lost_command_is_retried():
    board = FakeBoard(drop_commands=2)
    device.set_pins(link_to(board), {**DEFAULTS, "sda": 25})
    assert board.pins["sda"] == 25 and board.received == [wire.SET_PINS]


def test_no_answer_at_all_fails_with_advice():
    with pytest.raises(device.DeviceError, match="did not answer"):
        device.set_pins(link_to(FakeBoard(drop_commands=99)), DEFAULTS)


def test_reset_returns_to_defaults():
    board = FakeBoard(pins={**DEFAULTS, "sda": 25}, stored=True)
    hello = device.reset_pins(link_to(board))
    assert board.pins == DEFAULTS and hello["pin_src"] == "default"


# ── saved profiles ─────────────────────────────────────────────────────────────
def test_profile_round_trip(tmp_path):
    info = {"id": DEVICE_ID, "board": "esp32dev"}
    assert device.load_profile(tmp_path, DEVICE_ID) is None
    path = device.save_profile(tmp_path, info, DEFAULTS)
    assert path == tmp_path / "devices" / f"{DEVICE_ID}.json"
    assert device.load_profile(tmp_path, DEVICE_ID)["pins"] == DEFAULTS
    device.delete_profile(tmp_path, DEVICE_ID)
    assert device.load_profile(tmp_path, DEVICE_ID) is None


@pytest.mark.parametrize("bad_id", ["../../etc/passwd", "", "7CDFA1E2B3C4", "7cdfa1e2b3c", "7cdfa1e2b3c4/x", "zzzzzzzzzzzz"])
def test_a_device_cannot_choose_the_profile_path(tmp_path, bad_id):
    with pytest.raises(device.DeviceError):
        device.profile_path(tmp_path, bad_id)


def test_a_damaged_profile_is_reported_not_used(tmp_path):
    path = device.profile_path(tmp_path, DEVICE_ID)
    path.parent.mkdir(parents=True)
    path.write_text(json.dumps({"pins": {"sda": "21", "scl": 22, "gps_rx": 16, "gps_tx": 17}}))
    with pytest.raises(device.DeviceError, match="not a valid device profile"):
        device.load_profile(tmp_path, DEVICE_ID)


# ── nav pins ───────────────────────────────────────────────────────────────────
@pytest.fixture
def board(monkeypatch):
    """`nav pins` talks to a FakeBoard; pin rules come from a fake release."""
    import serial

    fake = FakeBoard()

    class Port:
        def __init__(self, *a, **k): pass
        def __enter__(self): return fake
        def __exit__(self, *exc): return False

    monkeypatch.setattr(serial, "Serial", Port)
    monkeypatch.setattr(device.Link.__init__, "__defaults__", (fake.clock,))
    monkeypatch.setattr("navkit.ports.find_esp32_port", lambda: "/dev/fake")
    monkeypatch.setattr("navkit.firmware.boards", lambda version="latest": {"esp32dev": {"pin_rules": RULES}})
    return fake


def nav(tmp_path, *argv):
    return cli.main(["--workspace", str(tmp_path), *argv])


def test_nav_pins_show(board, tmp_path, capsys):
    assert nav(tmp_path, "pins") == 0
    out = capsys.readouterr().out
    assert DEVICE_ID in out and "board defaults" in out and "GPIO 21" in out


def test_nav_pins_set_changes_only_what_was_given(board, tmp_path, capsys):
    assert nav(tmp_path, "pins", "set", "--sda", "25", "--gps-rx", "34") == 0
    assert board.pins == {"sda": 25, "scl": 22, "gps_rx": 34, "gps_tx": 17}
    assert device.load_profile(tmp_path, DEVICE_ID)["pins"] == board.pins
    assert "Stored on the device" in capsys.readouterr().out


def test_nav_pins_set_refuses_invalid_pins_before_sending(board, tmp_path, capsys):
    assert nav(tmp_path, "pins", "set", "--sda", "6", "--scl", "34") == 1
    out = capsys.readouterr().out
    assert "connected to the SPI flash" in out and "input-only" in out
    assert board.received == [] and board.pins == DEFAULTS
    assert device.load_profile(tmp_path, DEVICE_ID) is None


def test_nav_pins_set_relies_on_the_device_when_rules_are_unavailable(board, tmp_path, capsys, monkeypatch):
    from navkit import firmware

    def offline(version="latest"):
        raise firmware.FirmwareError("offline")

    monkeypatch.setattr("navkit.firmware.boards", offline)
    assert nav(tmp_path, "pins", "set", "--sda", "6") == 1
    assert "sda: GPIO 6 is not available on this board" in capsys.readouterr().out
    assert board.received == [wire.SET_PINS] and board.pins == DEFAULTS
    assert device.load_profile(tmp_path, DEVICE_ID) is None


def test_nav_pins_set_needs_a_pin(board, tmp_path, capsys):
    assert nav(tmp_path, "pins", "set") == 1
    assert "at least one" in capsys.readouterr().out


def test_nav_pins_restore_after_an_erase(board, tmp_path, capsys):
    assert nav(tmp_path, "pins", "set", "--sda", "25") == 0
    board.pins, board.stored = dict(DEFAULTS), False        # a full flash erase
    capsys.readouterr()

    assert nav(tmp_path, "pins") == 0
    assert "nav pins restore" in capsys.readouterr().out
    assert nav(tmp_path, "pins", "restore") == 0
    assert board.pins["sda"] == 25 and board.stored


def test_nav_pins_restore_with_nothing_saved(board, tmp_path, capsys):
    assert nav(tmp_path, "pins", "restore") == 1
    assert "Nothing is saved" in capsys.readouterr().out


def test_nav_pins_reset_forgets_the_profile(board, tmp_path):
    assert nav(tmp_path, "pins", "set", "--sda", "25") == 0
    assert nav(tmp_path, "pins", "reset") == 0
    assert board.pins == DEFAULTS and not board.stored
    assert device.load_profile(tmp_path, DEVICE_ID) is None
