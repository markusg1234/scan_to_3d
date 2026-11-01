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

Measured on a Core Ultra 9 285K with 95 GB of RAM and an RX 7800 XT, using the 11-photo Sceaux Castle sample:

| Quality | CPU | Graphics card |
|---|---|---|
| Draft | 35 seconds | 1 minute 40 seconds |
| Normal | 1 minute 25 seconds | 3 minutes 48 seconds |
| High | 9 minutes 18 seconds | 9 minutes 47 seconds |

More photos take longer, and matching time grows quickly because every photo is compared with every other.

## GPU mode (experimental, AMD on Windows)

GPU mode runs the Dense point cloud stage on an AMD graphics card with COLMAP. OpenMVS still builds the mesh and texture on the CPU.

It's not faster, but on the sample it recovered parts that the CPU route lost, such as a roof turret and stair railings, so it's worth trying when a CPU model is missing detail. It has only been tested on that one sample, on an RX 7800 XT.

| Quality | Image size on the card |
|---|---|
| Draft | 1000 pixels |
| Normal | 2000 pixels |
| High | Full size, plus mesh refinement on the CPU |

COLMAP has no download with AMD support, so GPU mode needs a local build. See [gpu/README.md](gpu/README.md). After building:

```
python setup_tools.py --gpu
python cli.py "C:\path\to\photos" "C:\path\to\output" --quality normal --gpu
```

In the window, tick **Use graphics card**. `dense_device` in `run_report.json` shows where the dense stage ran.

## Troubleshooting

| Problem | What to try |
|---|---|
| "could not work out where the photos were taken from" | Take more photos with more overlap, and avoid plain or shiny subjects |
| Only some photos are placed | Add photos that bridge the gaps between the parts that were placed |
| Colour seams on the model | Keep lighting and exposure the same between photos. See the note below |
| Details are missing | Use High, try GPU mode, and add close-up photos of those parts |
| "The graphics card ran out of memory" | Use a lower quality, or close other programs that use the card |
| The table or room is in the model | Trim it in Blender. Background removal is not included |
| The model is the wrong size | Models have no real-world scale. Scale it in Blender before 3D printing |

Each stage writes a log to `work/logs/`.

**Note on seams:** seam leveling is turned off because in OpenMVS 2.4.0 it painted large black patches onto the texture. Exposure differences between photos can therefore show as seams.
