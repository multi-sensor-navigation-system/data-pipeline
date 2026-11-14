"""Find, download and verify firmware images published as GitHub Releases.

Each firmware release carries a ``manifest.json`` describing one image per
board:

    {
      "schema": 1,
      "version": "v0.3.0",
      "git": "abc1234",
      "proto": 1,
      "boards": {
        "esp32dev": {
          "name": "ESP32 DevKit (ESP32-WROOM-32)",
          "chip": "esp32",
          "image": "msns-firmware-esp32dev-v0.3.0.bin",
          "sha256": "…",
          "offset": "0x0"
        }
      }
    }

Releases made before the manifest existed are still usable: the image is then
found by its name, ``msns-firmware-<board>-<tag>.bin``, with its ``.sha256``.
"""

import hashlib
import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__, wire, workspace

REPO = os.environ.get("NAVKIT_FIRMWARE_REPO", "multi-sensor-navigation-system/firmware")
API = "https://api.github.com"
DEFAULT_BOARD = "esp32dev"


class FirmwareError(RuntimeError):
    pass


@dataclass(frozen=True)
class Image:
    version: str            # release tag, e.g. "v0.3.0"
    board: str
    chip: str               # esptool chip name, e.g. "esp32"
    filename: str
    url: str
    sha256: str | None
    offset: str = "0x0"
    proto: int | None = None
    name: str = ""


def _get(url: str) -> bytes:
    req = urllib.request.Request(url, headers={
        "User-Agent": f"navkit/{__version__}",
        "Accept": "application/vnd.github+json, application/octet-stream",
    })
    token = os.environ.get("GITHUB_TOKEN")
    if token and url.startswith(API):
        req.add_header("Authorization", f"Bearer {token}")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        if e.code == 404:
            raise FirmwareError(f"Not found: {url}") from e
        if e.code == 403:
            raise FirmwareError("GitHub refused the request (rate limit?). "
                                "Set GITHUB_TOKEN or try again later.") from e
        raise FirmwareError(f"HTTP {e.code} from {url}") from e
    except urllib.error.URLError as e:
        raise FirmwareError(f"Could not reach GitHub: {e.reason}") from e


def releases() -> list[dict]:
    """All firmware releases, newest first, as GitHub API objects."""
    return json.loads(_get(f"{API}/repos/{REPO}/releases?per_page=30"))


def _release(version: str) -> dict:
    if version == "latest":
        return json.loads(_get(f"{API}/repos/{REPO}/releases/latest"))
    tag = version if version.startswith("v") else f"v{version}"
    return json.loads(_get(f"{API}/repos/{REPO}/releases/tags/{tag}"))


def manifest(release: dict) -> dict | None:
    """The release's manifest.json, or None for releases that predate it."""
    assets = {a["name"]: a["browser_download_url"] for a in release.get("assets", [])}
    if "manifest.json" not in assets:
        return None
    data = json.loads(_get(assets["manifest.json"]))
    if data.get("schema") != 1:
        raise FirmwareError(f"Release {release['tag_name']} uses manifest schema "
                            f"{data.get('schema')}; update navkit.")
    return data


def boards(version: str = "latest") -> dict[str, dict]:
    """Boards a release has images for: {board_id: manifest entry}."""
    rel = _release(version)
    m = manifest(rel)
    if m:
        return m["boards"]
    tag = rel["tag_name"]
    found = {}
    for a in rel.get("assets", []):
        n = a["name"]
        if n.startswith("msns-firmware-") and n.endswith(f"-{tag}.bin"):
            board = n[len("msns-firmware-"):-len(f"-{tag}.bin")]
            found[board] = {"chip": "esp32", "image": n, "name": board}
    return found


def resolve(board: str = DEFAULT_BOARD, version: str = "latest") -> Image:
    """Locate the image for a board in a release, without downloading it."""
    rel = _release(version)
    tag = rel["tag_name"]
    assets = {a["name"]: a["browser_download_url"] for a in rel.get("assets", [])}

    m = manifest(rel)
    if m:
        entry = m["boards"].get(board)
        if entry is None:
            raise FirmwareError(f"Release {tag} has no image for board '{board}'. "
                                f"Available: {', '.join(sorted(m['boards'])) or 'none'}")
        if entry["image"] not in assets:
            raise FirmwareError(f"Release {tag} lists {entry['image']} but does not contain it.")
        return Image(version=tag, board=board, chip=entry.get("chip", "esp32"),
                     filename=entry["image"], url=assets[entry["image"]],
                     sha256=entry.get("sha256"), offset=entry.get("offset", "0x0"),
                     proto=m.get("proto"), name=entry.get("name", board))

    filename = f"msns-firmware-{board}-{tag}.bin"
    if filename not in assets:
        raise FirmwareError(f"Release {tag} has no image for board '{board}'.")
    sha = None
    if f"{filename}.sha256" in assets:
        sha = _get(assets[f"{filename}.sha256"]).decode().split()[0]
    return Image(version=tag, board=board, chip="esp32", filename=filename,
                 url=assets[filename], sha256=sha)


def sha256_of(path: Path) -> str:
    h = hashlib.sha256()
    with open(path, "rb") as fh:
        for block in iter(lambda: fh.read(1 << 16), b""):
            h.update(block)
    return h.hexdigest()


def fetch(image: Image) -> Path:
    """Download an image into the cache (or reuse it) and verify its checksum."""
    dest = workspace.cache_dir() / "firmware" / image.version / image.filename
    if dest.exists() and image.sha256 and sha256_of(dest) == image.sha256:
        return dest

    data = _get(image.url)
    if image.sha256:
        got = hashlib.sha256(data).hexdigest()
        if got != image.sha256:
            raise FirmwareError(f"Checksum mismatch for {image.filename}: "
                                f"expected {image.sha256}, got {got}. Not flashing.")
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.with_suffix(".part")
    tmp.write_bytes(data)
    tmp.replace(dest)
    return dest


def check_protocol(image: Image) -> str | None:
    """A warning if this navkit cannot read what the image will send, else None."""
    if image.proto is not None and image.proto != wire.PROTO_VERSION:
        return (f"Firmware {image.version} speaks wire protocol v{image.proto}; this navkit "
                f"understands v{wire.PROTO_VERSION}. Update navkit before logging.")
    return None
