# Photo to 3D

Turns overlapping photos taken from different angles, or a video that circles an object, into a textured 3D model. It works out where each photo was taken with [COLMAP](https://colmap.github.io/), then builds the surface and texture with [OpenMVS](https://github.com/cdcseacave/openMVS).

## Setup

Needs Windows and Python (tested on 3.14). Nothing else has to be installed.

```
python setup_tools.py
```

This downloads pinned Windows builds of COLMAP, OpenMVS and ffmpeg into `tools/` and checks their checksums. Windows Defender may warn about COLMAP because its binaries are not signed. If you set up the program before video input was added, run this again to add ffmpeg.

## Use

Open the window:

```
python gui.py
```

The window shows a running timer and how long each stage took. It also remembers your last photos or video, output folder and frame count in `gui_settings.json`.

Or use the command line:

```
python cli.py "C:\path\to\photos" "C:\path\to\output" --quality normal
python cli.py "C:\path\to\video.mp4" "C:\path\to\output" --quality normal
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

Resolution is rarely the problem, and most phones are more than good enough. Results depend on moving around one subject. Turning on the spot to photograph a whole room won't work, and screens, glass and plain walls give COLMAP little to match.

## Using a video

You can use a video instead of a photo folder. In the window, click **Video...**. On the command line, give the video file in place of the folder. Supported types are MP4, MOV, M4V, MKV, AVI and WebM.

The program splits the video into equal parts and keeps the sharpest frame from each. It uses 100 frames unless you change **Video frames** in the window or add `--frames`. More frames take longer, because every frame is compared with every other. A rerun can't continue with a different frame count, so tick **Start over** or add `--restart` when you change it.

The only upper limit is the number of frames in the video. The arrows on the **Video frames** box stop at 500, but you can type a higher number. Quality usually matters more than frame count: on a 29-second phone video of a door knob, High at 100 frames took about as long as Draft at 250 and came out better.

Video input has been tried on one real phone video so far. Photos should still give better models, because a video frame holds less detail than a photo. To get the most from a video:

1. Film a slow, full circle of 30 to 60 seconds around the object, then another from higher up.
2. Use the highest resolution your camera offers, in standard video rather than HDR.
3. Lock focus and exposure if your camera allows it.
4. Turn stabilization off if you can.
5. Use bright, steady light, so frames stay sharp while the camera moves.

## Photo check

Right after camera solving, the program checks four signs of a weak photo set:

| Sign | Limit |
|---|---|
| Matched points per photo | At least 300 |
| Photos sharing each point | At least 3.5 |
| Position error | At most 1 pixel |
| Viewing angle around the subject | At least 45 degrees |

If two or more signs fail, the run stops before the long dense stage and lists the reasons. Video frames get the same check. The window then asks whether to continue anyway. On the command line, add `--accept-weak-photos`. The limits come from only two photo sets so far, so a good set could occasionally be stopped. The numbers are saved as `photo_check` in `run_report.json`.

## Speed

Measured on a Core Ultra 9 285K with 95 GB of RAM and an RX 7800 XT, using the 11-photo Sceaux Castle sample:

| Quality | CPU | Graphics card |
|---|---|---|
| Draft | 35 seconds | 1 minute 40 seconds |
| Normal | 1 minute 25 seconds | 3 minutes 48 seconds |
| High | 9 minutes 18 seconds | 9 minutes 47 seconds |

More photos take longer, and matching time grows quickly because every photo is compared with every other.

Picking frames from a video takes seconds, even for a 60-second 4K clip. The rest depends on the frames: 100 frames at 1280 x 720 took about 10 minutes at Normal, and larger frames take longer.

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
| "These photos look too weak for a good model" | Retake them around one matte, textured object, using the steps in Taking the photos |
| "These video frames look too weak for a good model" | Film again, using the steps in Using a video |
| "Video input needs ffmpeg" | Run `python setup_tools.py` |
| Only some photos are placed | Add photos that bridge the gaps between the parts that were placed |
| Colour seams on the model | Keep lighting and exposure the same between photos. See the note below |
| Details are missing | Use High, try GPU mode, and add close-up photos of those parts |
| "The graphics card ran out of memory" | Use a lower quality, or close other programs that use the card |
| The table or room is in the model | Trim it in Blender. Background removal is not included |
| The model is the wrong size | Models have no real-world scale. Scale it in Blender before 3D printing |

Each stage writes a log to `work/logs/`.

**Note on seams:** seam leveling is turned off because in OpenMVS 2.4.0 it painted large black patches onto the texture. Exposure differences between photos can therefore show as seams.
