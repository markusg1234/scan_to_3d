# COLMAP for AMD graphics cards on Windows

GPU mode needs a copy of COLMAP compiled with AMD's HIP. COLMAP has no download for this, so `build_colmap_hip.ps1` compiles one on your PC.

## Why it runs on Windows, not WSL

The first attempt ran in WSL Ubuntu and worked, but about 5 times slower. AMD's WSL bridge has no GPU textures, and texture reads are the core of COLMAP's dense stage. Native Windows has them.

ZLUDA was also tried with the official OpenMVS CUDA build. It stops with `cuLinkCreate` error 801, because ZLUDA has not implemented linking ([issue #283](https://github.com/vosen/ZLUDA/issues/283)).

## What you need

- Windows with an AMD Radeon RX 5000 series card or newer. Only the RX 7800 XT has been tested.
- Visual Studio Build Tools 2022 with the C++ workload.
- Git and Python.
- About 25 GB of free disk space.

To install the Build Tools:

```
winget install --id Microsoft.VisualStudio.2022.BuildTools --exact --override "--passive --wait --norestart --add Microsoft.VisualStudio.Workload.VCTools --add Microsoft.VisualStudio.Component.VC.Tools.x86.x64 --add Microsoft.VisualStudio.Component.Windows11SDK.26100 --add Microsoft.VisualStudio.Component.VC.CMake.Project"
```

## Build

```
powershell -ExecutionPolicy Bypass -File gpu\build_colmap_hip.ps1
python setup_tools.py --gpu
```

The first build takes about an hour. It uses `%USERPROFILE%\colmap-hip`, and `-Root` changes that.

For a different card, pass its chip with `-GpuArch`. The AMD SDK package in the script covers the gfx110X family, so other families need a different package as well.

## What the script changes

COLMAP 4.2.1 has AMD support, but it isn't set up for this Windows toolchain. The script applies these fixes:

| Where | Fix |
|---|---|
| All of COLMAP | Compiled with AMD's Clang, because CMake cannot mix Clang and MSVC |
| VLFeat | Declares `omp_get_thread_limit`, because Microsoft's `omp.h` stops at OpenMP 2.0 |
| PoissonRecon | Adds `NOMINMAX` |
| PoseLib | Removes `-fPIC` and `-Werror` |
| FAISS | Adds its per-file AVX flags on Windows |

## Good to know

- The SDK is an AMD nightly build from June 2026, and old nightly files may disappear.
- Programs use the HIP runtime from your AMD driver, not the older copy in the SDK.
- One test folder, `colmap-hip\bench`, made every HIP program crash with `Couldn't create blit kernels`. A full copy of that folder elsewhere ran fine, and the cause is unknown. If you see that message, try another working folder.
