# To do

## Add video input

Accept a video file instead of a photo folder, and produce the same `model\model.obj`.

- **Frames:** add a video-only first stage, "Extract video frames". It scores every frame with ffmpeg's `blurdetect` filter, splits the video into equal time slices, and extracts the sharpest frame from each into `work/frames`. Default 100 frames, with a `--frames` option.
- **Formats:** `.mp4 .mov .m4v .mkv .avi .webm`.
- **Tool:** pin ffmpeg 9.0.2 essentials from `GyanD/codexffmpeg` in `setup_tools.py`.
  - URL: `https://github.com/GyanD/codexffmpeg/releases/download/9.0.2/ffmpeg-9.0.2-essentials_build.zip`
  - SHA-256: `60f467265b1e312373dbcd92200c2618a74850f98d3d078e94296bb3fa2047ba`
- **Other stages:** add `--ImageReader.single_camera 1` to Find features for video only. Keep exhaustive matching, because a camera circling an object needs the end of the loop matched to the start. Everything else stays the same, with `work/frames` as the image folder.
- **Resuming:** build the run signature from the video's name and size plus the frame count.
- **Interface:** the CLI's first argument takes a folder or a video file. The window gets a "Video..." button and a frame-count box.
- **Docs:** add how to film an object to `README.md`.
- **Testing:** unit tests for blur parsing and frame selection, then an orbiting camera video rendered in Blender, then a real phone video.
- **Risks:** frames hold less detail than photos, and phone stabilization, autofocus, rolling shutter and HDR can all reduce quality.
