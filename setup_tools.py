"""Download the pinned COLMAP and OpenMVS Windows builds and record their paths."""

import argparse
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS_DIR = ROOT / "tools"
TOOLS_JSON = ROOT / "tools.json"
# The tar built into Windows unpacks both .zip and .7z archives.
TAR = Path(os.environ["SystemRoot"]) / "System32" / "tar.exe"

CPU_TOOLS = {
    "colmap": {
        "version": "4.2.1",
        "url": "https://github.com/colmap/colmap/releases/download/4.2.1/colmap-x64-windows-nocuda.zip",
        "sha256": "c493d88cb4a43f21afba42cb55bf786e5434a0420e73db0ba1454c5d8b3686ae",
        "exe": "colmap.exe",
    },
    "openmvs": {
        "version": "2.4.0",
        "url": "https://github.com/cdcseacave/openMVS/releases/download/v2.4.0/OpenMVS_Windows_x64.zip",
        "sha256": "0c31660c15c9ebc4c106873cf67564d9570d404aef7a6403451da1b6178b2167",
        "exe": "DensifyPointCloud.exe",
    },
}

# OpenMVS only has CUDA GPU code. ZLUDA runs CUDA programs on AMD graphics cards.
GPU_TOOLS = {
    "openmvs_gpu": {
        "version": "2.4.0",
        "url": "https://github.com/cdcseacave/openMVS/releases/download/v2.4.0/OpenMVS_Windows_x64_CUDA.7z",
        "sha256": "6aac6b14ef478e501d2514cd1d74ed20e659b53b3bc2835d45a473ddfb921621",
        "exe": "DensifyPointCloud.exe",
    },
    "zluda": {
        "version": "v7-preview.19",
        "url": "https://github.com/vosen/ZLUDA/releases/download/v7-preview.19/zluda-windows-08b19b2.zip",
        "sha256": "ce4f662ef9cb7ae8a3b7d6864c5c35af3c46d857b8ea896723c7f4546fd19952",
        "exe": "zluda.exe",
    },
}


def download(url, destination, expected_sha256):
    print(f"Downloading {url}")
    digest = hashlib.sha256()
    with urllib.request.urlopen(url) as response, destination.open("wb") as output:
        while chunk := response.read(1024 * 1024):
            digest.update(chunk)
            output.write(chunk)
    if digest.hexdigest() != expected_sha256:
        destination.unlink()
        raise SystemExit(f"Checksum mismatch for {url}; download discarded.")


def install(name, spec):
    """Unpack one tool into tools/<name>-<version>/ and return its folder."""
    folder = TOOLS_DIR / f"{name}-{spec['version']}"
    if folder.exists():
        print(f"{name} {spec['version']} is already installed.")
        return folder
    TOOLS_DIR.mkdir(exist_ok=True)
    archive = TOOLS_DIR / (folder.name + Path(spec["url"]).suffix)
    download(spec["url"], archive, spec["sha256"])
    print(f"Unpacking {archive.name}")
    unpacking = folder.with_name(folder.name + ".partial")
    shutil.rmtree(unpacking, ignore_errors=True)
    unpacking.mkdir()
    subprocess.run([TAR, "-xf", archive, "-C", unpacking], check=True)
    unpacking.rename(folder)
    archive.unlink()
    return folder


def find_exe(folder, name):
    # ZLUDA also ships 32-bit tools in a subfolder, so the shallowest match wins.
    exe = min(folder.rglob(name), key=lambda path: len(path.parts), default=None)
    if exe is None:
        raise SystemExit(f"{name} was not found under {folder}.")
    return exe


def check_starts(name, exe):
    """OpenMVS exits with 1 after printing help; a missing DLL gives neither code."""
    with tempfile.TemporaryDirectory() as folder:  # OpenMVS writes a log where it runs.
        result = subprocess.run([exe, "--help"], cwd=folder, capture_output=True)
    if result.returncode not in (0, 1):
        raise SystemExit(f"{name} failed to start (exit code {result.returncode}).")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--gpu", action="store_true", help="also download the AMD GPU tools")
    args = parser.parse_args()

    tools = json.loads(TOOLS_JSON.read_text(encoding="utf-8")) if TOOLS_JSON.exists() else {}
    for name, spec in {**CPU_TOOLS, **(GPU_TOOLS if args.gpu else {})}.items():
        exe = find_exe(install(name, spec), spec["exe"])
        if name in CPU_TOOLS:
            check_starts(name, exe)
        tools[name] = {"version": spec["version"], "bin": str(exe.parent), "exe": str(exe)}
    TOOLS_JSON.write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {TOOLS_JSON}")


if __name__ == "__main__":
    sys.exit(main())
