"""Command line for photo_to_3d."""

import argparse
import sys
import time

from pipeline import (
    DEFAULT_VIDEO_FRAMES,
    QUALITIES,
    STAGE_TITLES,
    Pipeline,
    PipelineError,
    WeakPhotos,
    format_duration,
    view_model,
)


def print_stage(stage, status):
    print(f"{STAGE_TITLES[stage]}: {status}", flush=True)


def main():
    parser = argparse.ArgumentParser(
        description="Turn a folder of photos or a video into a textured 3D model."
    )
    parser.add_argument("source", help="folder of photos, or a video file")
    parser.add_argument("output", help="folder for the model and working files")
    parser.add_argument("--quality", choices=QUALITIES, default="normal")
    parser.add_argument("--gpu", action="store_true", help="use the graphics card (experimental)")
    parser.add_argument("--restart", action="store_true", help="discard earlier progress")
    parser.add_argument(
        "--accept-weak-photos", action="store_true", help="continue if the photo check fails"
    )
    parser.add_argument(
        "--frames",
        type=int,
        default=DEFAULT_VIDEO_FRAMES,
        help=f"how many frames to use from a video (default {DEFAULT_VIDEO_FRAMES})",
    )
    parser.add_argument("--view", action="store_true", help="open the model in a viewer when done")
    parser.add_argument("--verbose", action="store_true", help="show tool output")
    args = parser.parse_args()

    pipeline = Pipeline(
        args.source,
        args.output,
        quality=args.quality,
        gpu=args.gpu,
        restart=args.restart,
        accept_weak_photos=args.accept_weak_photos,
        video_frames=args.frames,
        on_log=print if args.verbose else lambda line: None,
        on_stage=print_stage,
    )
    started = time.monotonic()
    try:
        report = pipeline.run()
    except WeakPhotos as error:
        print(f"\nStopped: {error}\nTo continue anyway, add --accept-weak-photos.", file=sys.stderr)
        return 1
    except PipelineError as error:
        print(f"\nStopped: {error}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        pipeline.cancel()
        print("\nStopped. Run again to continue from where it stopped.", file=sys.stderr)
        return 130

    print(f"\nPlaced {report['photos_placed']} of {report['photos_found']} photos.")
    for warning in report["warnings"]:
        print(f"Warning: {warning}")
    print(f"Time: {format_duration(time.monotonic() - started)}")
    print(f"Model: {report['model']}")
    print(f"STL: {report['stl']}" + (" (in mm)" if "real_size" in report else ""))
    if args.view:
        try:
            view_model(args.output, pipeline.tools)
        except PipelineError as error:
            print(f"\n{error}", file=sys.stderr)
            return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
