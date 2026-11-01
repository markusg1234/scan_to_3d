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

# COLMAP has no AMD GPU download, so gpu/build_colmap_hip.ps1 compiles it here.
DEFAULT_GPU_BUILD = Path.home() / "colmap-hip"
GPU_VERSION = "4.2.1"
RETIRED_TOOLS = ("openmvs_gpu", "zluda")


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


def find_openmp_folder():
    """Find the OpenMP runtime that Visual Studio installs but does not put on PATH."""
    studio = Path(os.environ["ProgramFiles(x86)"]) / "Microsoft Visual Studio"
    matches = sorted(studio.glob("*/*/VC/Redist/MSVC/*/*/x64/Microsoft.VC*.OpenMP.LLVM/libomp140.x86_64.dll"))
    if not matches:
        raise SystemExit("libomp140.x86_64.dll was not found. Install Visual Studio Build Tools.")
    return matches[-1].parent


def register_gpu_build(folder):
    """Check a local COLMAP build has AMD GPU support and return its tools entry."""
    exe = folder / "build2" / "src" / "colmap" / "exe" / "colmap.exe"
    if not exe.exists():
        raise SystemExit(f"No COLMAP GPU build in {folder}. See gpu/README.md.")
    dll_paths = [str(folder / "vi" / "x64-windows" / "bin"), str(find_openmp_folder())]
    env = dict(os.environ)
    env["PATH"] = os.pathsep.join([*dll_paths, env["PATH"]])
    result = subprocess.run([exe, "-h"], capture_output=True, text=True, env=env)
    if "with HIP" not in result.stdout + result.stderr:
        raise SystemExit(f"{exe} does not report AMD GPU support (exit code {result.returncode}).")
    return {"version": GPU_VERSION, "bin": str(exe.parent), "exe": str(exe), "dll_paths": dll_paths}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--gpu",
        nargs="?",
        const=DEFAULT_GPU_BUILD,
        type=Path,
        metavar="FOLDER",
        help=f"also use the local COLMAP GPU build (default folder: {DEFAULT_GPU_BUILD})",
    )
    args = parser.parse_args()

    tools = json.loads(TOOLS_JSON.read_text(encoding="utf-8")) if TOOLS_JSON.exists() else {}
    for name in RETIRED_TOOLS:
        tools.pop(name, None)
    for name, spec in CPU_TOOLS.items():
        exe = find_exe(install(name, spec), spec["exe"])
        check_starts(name, exe)
        tools[name] = {"version": spec["version"], "bin": str(exe.parent), "exe": str(exe)}
    if args.gpu:
        tools["colmap_gpu"] = register_gpu_build(args.gpu)
    TOOLS_JSON.write_text(json.dumps(tools, indent=2) + "\n", encoding="utf-8")
    print(f"Wrote {TOOLS_JSON}")


if __name__ == "__main__":
    sys.exit(main())
