"""Flash a firmware image onto a board with esptool."""

from pathlib import Path

DEFAULT_BAUD = 921600

# esptool reports chips as "ESP32", "ESP32-S3", …; its --chip option wants "esp32", "esp32s3".
def chip_id(name: str) -> str:
    return name.lower().replace("-", "").split("(")[0].strip()


def detect_chip(port: str) -> str:
    """Ask the chip on `port` what it is. Returns an esptool chip id such as "esp32"."""
    from esptool.cmds import detect_chip as _detect

    esp = _detect(port)
    try:
        return chip_id(esp.CHIP_NAME)
    finally:
        # Reset back into the application and release the port.
        try:
            esp.hard_reset()
        finally:
            esp._port.close()


def esptool_args(image: Path, port: str, chip: str = "esp32", offset: str = "0x0",
                 baud: int = DEFAULT_BAUD) -> list[str]:
    return ["--chip", chip, "--port", port, "--baud", str(baud),
            "write-flash", offset, str(image)]


def flash(image: Path, port: str, chip: str = "esp32", offset: str = "0x0",
          baud: int = DEFAULT_BAUD) -> None:
    """Write `image` at `offset`. Raises SystemExit / esptool.FatalError on failure."""
    import esptool

    esptool.main(esptool_args(image, port, chip, offset, baud))
