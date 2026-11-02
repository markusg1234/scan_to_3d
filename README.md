# Scan to 3D

Turns overlapping photos taken from different angles, or a video that circles an object, into a textured 3D model. It works out where each photo was taken with [COLMAP](https://colmap.github.io/), then builds the surface and texture with [OpenMVS](https://github.com/cdcseacave/openMVS). It also saves the shape as an STL file for 3D printing.

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
| `model/` | `model.obj`, `model.mtl` and the texture image, which must stay together, and `model.stl` for 3D printing |
| `run_report.json` | Photos found and placed, seconds per stage, settings and tool versions |
| `work/` | Working files and logs. Safe to delete when you are finished, but a rerun then starts over, and Set Real Size needs them |

## Viewing the model

Click **View Model** in the window to open the model in the output folder, including one from an earlier run. On the command line, add `--view` to open the model when the run finishes. Either way, it opens in OpenMVS's viewer, which comes with the other tools.

| Control | Action |
|---|---|
| Left drag | Turn |
| Right drag | Move |
| Scroll wheel | Zoom |
| Double-click | Centre the view on that point |
| T | Texture off or on, to see the bare shape |
| W | Wireframe |
| R | Reset the view |
| F1 | All the controls |

To make the viewer's text bigger, move the mouse to the top of the viewer to show its menu bar, then choose View > Render Settings (or press Shift+R) and raise Font Scale. The viewer remembers it. On a 4K screen at 175% scaling, a Font Scale of 2 roughly matches other programs' text.

On the RX 7800 XT used for testing, the viewer crashed in about 1 of every 11 openings, always within 2 seconds while loading the model. View Model and `--view` reopen it when that happens. It also crashed once after T was pressed, so if it closes by itself later on, open it again.

To view a model in Blender, use File > Import > Wavefront (.obj), then switch the viewport to Material Preview.

## 3D printing

`model/model.stl` is the same shape as `model.obj` without the colours, for slicers and FreeCAD. It isn't ready to print as it comes, so first, in a program such as Blender:

1. Trim off the table or room, which comes out joined to the object.
2. Close the open edges, so the surface becomes a solid. No photo sees the underside of an object standing on a table, so that part is missing.
3. Turn it upright, because camera solving doesn't know which way is up.

Before that, set the real size as described below. Models have no real-world scale, and slicers read STL numbers as millimetres, so without it the models so far came in only 3 to 10 mm across.

Blender labels the numbers in an STL as metres, so a 150 mm object shows as 150 m. Leave its size alone: Blender's STL export keeps the numbers, so the slicer still reads millimetres.

## Real size

The photos can't tell a small object close up from a big one far away, so the program needs at least one real length. After the run, click **Set Real Size...** in the window, and choose one of two ways to give it. Either way, the program rewrites `model.stl` in millimetres and keeps the size when you rerun at another quality. `model.obj` stays in its own units.

| Way | You click | Use it for |
|---|---|---|
| The same spots in several photos | The same two spots in 2 or 3 photos | A ruler or marked spots. The most accurate, and it works on corners |
| Different spots in each photo | Any two spots in each photo, with the length between them | Unmarked objects, on flat areas only |

In both, choose a photo in the list, click near a spot in the photo, then exactly on it in the zoomed view on the right. The bold line under the photos says what to click next.

### The same spots in several photos

1. Put something of known length in the shot, such as a steel ruler lying flat beside the object, or pick two spots marked on the object and measure between them with calipers. Use the longest length you can, between points marked on the surface, such as ruler ends, screw heads or pen marks. Don't use the edge of something round, because from another angle the edge is a different spot. On a rotating table, put the ruler on the turning part, because anything that stays still confuses camera solving.
2. Click point 1 and point 2 in one photo, always on the same two spots.
3. Repeat in two more photos, each at least 30 degrees further around. Video frames next to each other in the list were taken from almost the same spot, so pick frames far apart, such as the first, middle and last. Two photos are enough to measure, but with only two, a click on slightly the wrong spot can go unnoticed. A third lets the program catch it.
4. Type the length in millimetres and click **Save**.

The program works out where each spot is in 3D from where the photos were taken, so it doesn't depend on the model's surface. It warns if a click lands more than 3 pixels from where the other photos put that point, or if the photos see a point from less than 10 degrees apart.

### Different spots in each photo

1. In a photo, click two spots whose distance you know or can measure, such as across a flat face of the object, or along a ruler or tape measure lying on the table.
2. Type that length under the photo, then do the same in another photo. Each photo can use different spots and a different length.
3. Click **Save**.

The program follows each click's line of sight until it meets the model's surface, so no spot has to be found again in another photo. That only works where the surface is right, so click flat areas. On a corner or edge, the line of sight can slip past the model's slightly rounded edge onto whatever is behind it. The program checks the surface either side of every click and names any click that sits on an edge. It also compares the photos with each other, and warns if they disagree by more than 3% or if only one photo was measured.

### How accurate it is

Both ways were tested on a rendered scene with a 300 mm ruler and a 50 mm box, whose true sizes were known:

| Clicks | Size error |
|---|---|
| Same spots, exactly on the marks | 0.01% |
| Same spots, about half a pixel out on average, photos 30 to 180 degrees apart | 0.2 to 0.3%, for 95% of tries |
| Same spots, about a pixel out on average, photos 30 to 180 degrees apart | 0.4 to 0.6%, for 95% of tries |
| Same spots, about a pixel out on average, photos 10 degrees apart | 0.9%, for 95% of tries |
| Same spots, one click 10 pixels out, two photos | Up to 1.7%, and not always warned about |
| Same spots, one click 10 pixels out, three photos | Up to 0.9%, always warned about |
| Same spots, on the box's corners | 0.00% |
| Different spots, ruler marks lying flat, in each of 62 photos | 0.13% at worst |
| Different spots, ruler marks, about a pixel out on average | 0.8%, for 90% of tries |
| Different spots, the ruler's corners | 1.8% at worst |
| Different spots, the box's corners | Up to 75% out, but every click that was 1 cm or more out was flagged as on an edge |

A real ruler's ends are harder to pin down than the rendered marks, so expect somewhat larger errors. On a 100 mm object, 0.5% is half a millimetre.

## Taking the photos

The result depends more on the photos than on any setting.

1. Take 40 to 80 photos of an object, or more if it is large or detailed.
2. Walk a full circle around it, taking a photo every 10 to 15 degrees, so each photo overlaps the next by about two thirds.
3. Walk another circle from higher up, so the top is covered too.
4. Move the camera, not the object, and keep the lighting steady. To turn the object instead, see [Using a rotating table](#using-a-rotating-table).
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

## Using a rotating table

You can also keep the camera still and turn the object, with photos or a video. COLMAP works out how the camera moved from what changes between pictures, so anything in view that doesn't turn with the object can convince it the camera never moved.

1. Hide everything that doesn't turn. A plain sheet of paper or cloth should fill the whole picture around the table, because even a thin strip of busy wall at the top of the frame can spoil the result.
2. Put the camera on a tripod.
3. Put something patterned on the table, such as newspaper, so there's more to match. The table ends up in the model, so trim it off in Blender afterwards.
4. Turn the table 10 to 15 degrees between photos, or film one slow, full turn.
5. Raise the camera, tilt it down and do a second turn, so the top is covered.
6. Use soft, even light. Shadows stay where they are while the object turns, so hard light leaves dark patches in the texture.
7. Don't let the object shift on the table.

This has only been tried on scenes rendered in Blender, not on a real table. Each scene had 72 frames in two turns:

| Setup | Frames in the right place | Surface within 0.4% of the table's width |
|---|---|---|
| Plain backdrop, patterned table, soft light | All | 96% |
| The same, as a video | All | 92% |
| The same, with one hard light | All | 97%, with dark patches in the texture |
| The same, with a plain table | All | 74%, with much of the table missing |
| Thin strip of busy wall at the top of the low turn | About half | 65%, and stopped by the photo check |
| Busy wall behind a plain table | None | Unusable, and stopped by the photo check |
| Busy room all around | None | Unusable, and stopped by the photo check |

## Photo check

Right after camera solving, the program checks five signs of a weak photo set:

| Sign | Limit |
|---|---|
| Matched points per photo | At least 300 |
| Photos sharing each point | At least 3.5 |
| Position error | At most 1 pixel |
| Viewing angle around the subject | At least 45 degrees |
| Points that stay still in the picture | At most 3% |

If two or more signs fail, the run stops before the long dense stage and lists the reasons. Too many still points stop it on their own, because a set can pass everything else with half its cameras in the wrong place. Video frames get the same check. The window then asks whether to continue anyway. On the command line, add `--accept-weak-photos`. The first four limits come from only two photo sets so far, and the still points limit from the rendered scenes above, so a good set could occasionally be stopped. The numbers are saved as `photo_check` in `run_report.json`.

A video that sits still for more than about an eighth of its length can also fail the still points sign. That's a false alarm, so continue anyway, or trim the pause.

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
| "Part of the picture stayed still between photos" | Hide everything that doesn't turn, using the steps in Using a rotating table. If a video paused for a long time, trim the pause or continue anyway |
| "Video input needs ffmpeg" | Run `python setup_tools.py` |
| Only some photos are placed | Add photos that bridge the gaps between the parts that were placed |
| Colour seams on the model | Keep lighting and exposure the same between photos. See the note below |
| Details are missing | Use High, try GPU mode, and add close-up photos of those parts |
| "The graphics card ran out of memory" | Use a lower quality, or close other programs that use the card |
| The table or room is in the model | Trim it in Blender. Background removal is not included |
| The model is the wrong size | Models have no real-world scale. Use Set Real Size, as in Real size |

Each stage writes a log to `work/logs/`.

**Note on seams:** seam leveling is turned off because in OpenMVS 2.4.0 it painted large black patches onto the texture. Exposure differences between photos can therefore show as seams.

## Tools used

The program is a wrapper around these tools. `python setup_tools.py` downloads them from the addresses below and checks each against a fixed checksum. They aren't included in this repository, and each keeps its own licence.

| Tool | Used for | Version | Download | Licence |
|---|---|---|---|---|
| [COLMAP](https://colmap.github.io/) | Working out where each photo was taken, and the dense stage in GPU mode | 4.2.1, Windows build without CUDA | [GitHub releases](https://github.com/colmap/colmap/releases) | BSD 3-clause |
| [OpenMVS](https://github.com/cdcseacave/openMVS) | The dense point cloud, mesh and texture, and the model viewer | 2.4.0 | [GitHub releases](https://github.com/cdcseacave/openMVS/releases) | AGPL-3.0 |
| [FFmpeg](https://ffmpeg.org/) | Picking and saving video frames | 9.0.2, essentials build by [gyan.dev](https://www.gyan.dev/ffmpeg/builds/) | [GitHub releases](https://github.com/GyanD/codexffmpeg/releases) | GPL-3.0 |

GPU mode also uses AMD's HIP SDK to compile COLMAP on your PC. See [gpu/README.md](gpu/README.md).

## Thank you

Thank you to the developers of COLMAP, OpenMVS and FFmpeg, whose work does the hard part of every model this program makes. Thank you also to Gyan Doshi for the Windows builds of FFmpeg, and to the OpenMVS project for the Sceaux Castle sample photos used for testing.
