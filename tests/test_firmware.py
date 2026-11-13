import hashlib
import json

import pytest

from navkit import firmware

IMAGE = b"\xe9firmware-image-bytes" * 100
SHA = hashlib.sha256(IMAGE).hexdigest()
BASE = "https://example.invalid"


def release(tag, assets):
    return {"tag_name": tag, "published_at": "2026-11-14T00:00:00Z",
            "assets": [{"name": n, "browser_download_url": f"{BASE}/{tag}/{n}"} for n in assets]}


@pytest.fixture
def github(monkeypatch, tmp_path):
    """A fake GitHub: maps URLs to bytes and records what was requested."""
    monkeypatch.setattr(firmware.workspace, "cache_dir", lambda: tmp_path / "cache")
    pages, calls = {}, []

    def fake_get(url):
        calls.append(url)
        if url not in pages:
            raise firmware.FirmwareError(f"Not found: {url}")
        return pages[url]

    monkeypatch.setattr(firmware, "_get", fake_get)

    def add_release(tag, files, latest=True):
        rel = release(tag, files)
        pages[f"{firmware.API}/repos/{firmware.REPO}/releases/tags/{tag}"] = json.dumps(rel).encode()
        if latest:
            pages[f"{firmware.API}/repos/{firmware.REPO}/releases/latest"] = json.dumps(rel).encode()
        for name, content in files.items():
            pages[f"{BASE}/{tag}/{name}"] = content
        return rel

    return type("GitHub", (), {"add_release": staticmethod(add_release), "calls": calls, "pages": pages})


def manifest_bytes(tag="v0.3.0", sha=SHA, proto=1):
    return json.dumps({
        "schema": 1, "version": tag, "git": "abc1234", "proto": proto,
        "boards": {
            "esp32dev": {"name": "ESP32 DevKit", "chip": "esp32",
                         "image": f"msns-firmware-esp32dev-{tag}.bin", "sha256": sha, "offset": "0x0"},
            "esp32-s3-devkitc-1": {"name": "ESP32-S3 DevKitC-1", "chip": "esp32s3",
                                   "image": f"msns-firmware-esp32-s3-devkitc-1-{tag}.bin",
                                   "sha256": SHA, "offset": "0x0"},
        },
    }).encode()


def test_resolve_and_fetch_via_manifest(github):
    github.add_release("v0.3.0", {
        "manifest.json": manifest_bytes(),
        "msns-firmware-esp32dev-v0.3.0.bin": IMAGE,
        "msns-firmware-esp32-s3-devkitc-1-v0.3.0.bin": IMAGE,
    })

    image = firmware.resolve("esp32-s3-devkitc-1")
    path = firmware.fetch(image)

    assert image.version == "v0.3.0"
    assert image.chip == "esp32s3"
    assert image.proto == 1
    assert path.read_bytes() == IMAGE
    assert firmware.check_protocol(image) is None


def test_cached_image_is_not_downloaded_again(github):
    github.add_release("v0.3.0", {"manifest.json": manifest_bytes(),
                                  "msns-firmware-esp32dev-v0.3.0.bin": IMAGE})
    image = firmware.resolve("esp32dev")
    firmware.fetch(image)
    github.calls.clear()

    firmware.fetch(image)

    assert github.calls == []


def test_checksum_mismatch_refuses_and_caches_nothing(github, tmp_path):
    github.add_release("v0.3.0", {"manifest.json": manifest_bytes(sha="0" * 64),
                                  "msns-firmware-esp32dev-v0.3.0.bin": IMAGE})
    image = firmware.resolve("esp32dev")

    with pytest.raises(firmware.FirmwareError, match="Checksum mismatch"):
        firmware.fetch(image)
    assert not list((tmp_path / "cache").rglob("*.bin"))


def test_corrupt_cache_entry_is_replaced(github):
    github.add_release("v0.3.0", {"manifest.json": manifest_bytes(),
                                  "msns-firmware-esp32dev-v0.3.0.bin": IMAGE})
    image = firmware.resolve("esp32dev")
    path = firmware.fetch(image)
    path.write_bytes(b"truncated")

    assert firmware.fetch(image).read_bytes() == IMAGE


def test_unknown_board_lists_what_exists(github):
    github.add_release("v0.3.0", {"manifest.json": manifest_bytes(),
                                  "msns-firmware-esp32dev-v0.3.0.bin": IMAGE})
    with pytest.raises(firmware.FirmwareError, match="esp32-s3-devkitc-1, esp32dev"):
        firmware.resolve("pico")


def test_release_without_manifest_falls_back_to_file_names(github):
    github.add_release("v0.1.0", {
        "msns-firmware-esp32dev-v0.1.0.bin": IMAGE,
        "msns-firmware-esp32dev-v0.1.0.bin.sha256": f"{SHA}  msns-firmware-esp32dev-v0.1.0.bin\n".encode(),
    })

    image = firmware.resolve("esp32dev", "0.1.0")     # "v" prefix is optional

    assert image.sha256 == SHA and image.proto is None
    assert firmware.fetch(image).read_bytes() == IMAGE
    assert list(firmware.boards("v0.1.0")) == ["esp32dev"]


def test_newer_protocol_produces_a_warning(github):
    github.add_release("v9.0.0", {"manifest.json": manifest_bytes("v9.0.0", proto=2),
                                  "msns-firmware-esp32dev-v9.0.0.bin": IMAGE})
    assert "protocol v2" in firmware.check_protocol(firmware.resolve("esp32dev"))


def test_unknown_manifest_schema_is_refused(github):
    github.add_release("v0.3.0", {"manifest.json": json.dumps({"schema": 2, "boards": {}}).encode()})
    with pytest.raises(firmware.FirmwareError, match="schema"):
        firmware.resolve("esp32dev")
