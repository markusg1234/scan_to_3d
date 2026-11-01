# Photo to 3D

Turns a folder of overlapping photos, taken from different angles, into a textured 3D model. It works out where each photo was taken with [COLMAP](https://colmap.github.io/), then builds the surface and texture with [OpenMVS](https://github.com/cdcseacave/openMVS).

## Setup

Needs Windows and Python (tested on 3.14). Nothing else has to be installed.

```
python setup_tools.py
```

This downloads the pinned official Windows builds into `tools/` and checks their checksums. Windows Defender may warn about COLMAP because its binaries are not signed.

## Use

Open the window:

```
python gui.py
```

Or use the command line:

```
python cli.py "C:\path\to\photos" "C:\path\to\output" --quality normal
```

| Quality | Use |
|---|---|
| `draft` | Quick check that the photos work |
| `normal` | Everyday use |
| `high` | Final model; slowest, and refines the mesh |

Add `--restart` (or tick Start over) to discard earlier progress. Otherwise a rerun continues from where it stopped, and changing the quality reruns only the last stages.

The output folder must not be inside the photo folder.

## What you get

| Path | Contents |
|---|---|
| `model/` | `model.obj`, `model.mtl` and the texture image. Keep these three together |
| `run_report.json` | Photos found and placed, seconds per stage, settings and tool versions |
| `work/` | Working files and logs. Safe to delete when you are finished, but a rerun then starts over |

To view a model in Blender, use File > Import > Wavefront (.obj), then switch the viewport to Material Preview.

## Taking the photos

The result depends more on the photos than on any setting.

1. Take 40 to 80 photos of an object, or more if it is large or detailed.
2. Walk a full circle around it, taking a photo every 10 to 15 degrees, so each photo overlaps the next by about two thirds.
3. Walk another circle from higher up, so the top is covered too.
4. Move the camera, not the object, and keep the lighting steady.
5. Keep every photo sharp. Delete blurry ones.

Matte, textured objects work best. Shiny, transparent or plain surfaces reconstruct badly in any photogrammetry software.

Supported photo types are JPEG, PNG and TIFF. Convert iPhone HEIC photos to JPEG first.

## Speed

Measured on a Core Ultra 9 285K with 95 GB of RAM, using the 11-photo Sceaux Castle sample and the CPU only:

| Quality | Time |
|---|---|
| Draft | About 40 seconds |
| Normal | About 1.5 minutes |
| High | About 9 minutes |

More photos take longer, and matching time grows quickly because every photo is compared with every other.

## GPU mode (experimental, not yet tested)

OpenMVS only has GPU code for NVIDIA CUDA. GPU mode runs its official CUDA build through [ZLUDA](https://github.com/vosen/ZLUDA), a translation layer that runs CUDA programs on AMD cards. It covers the two slowest stages, Dense point cloud and Refine mesh. COLMAP still runs on the CPU.

```
python setup_tools.py --gpu
python cli.py "C:\path\to\photos" "C:\path\to\output" --quality draft --gpu
```

ZLUDA is pre-release software, and its documentation also lists the AMD HIP SDK as a requirement. Check `dense_device` in `run_report.json` to see whether the card was really used. If it says CPU, OpenMVS could not reach the card.

## Troubleshooting

| Problem | What to try |
|---|---|
| "could not work out where the photos were taken from" | Take more photos with more overlap, and avoid plain or shiny subjects |
| Only some photos are placed | Add photos that bridge the gaps between the parts that were placed |
| Colour seams on the model | Keep lighting and exposure the same between photos. See the note below |
| Details are missing | Use High, and add close-up photos of those parts |
| The table or room is in the model | Trim it in Blender. Background removal is not included |
| The model is the wrong size | Models have no real-world scale. Scale it in Blender before 3D printing |

Each stage writes a log to `work/logs/`.

**Note on seams:** seam leveling is turned off because in OpenMVS 2.4.0 it painted large black patches onto the texture. Exposure differences between photos can therefore show as seams.
