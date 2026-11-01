"""Command line for photo_to_3d."""

import argparse
import sys
import time

from pipeline import QUALITIES, STAGE_TITLES, Pipeline, PipelineError, format_duration


def print_stage(stage, status):
    print(f"{STAGE_TITLES[stage]}: {status}", flush=True)


def main():
    parser = argparse.ArgumentParser(description="Turn a folder of photos into a textured 3D model.")
    parser.add_argument("photos", help="folder of photos")
    parser.add_argument("output", help="folder for the model and working files")
    parser.add_argument("--quality", choices=QUALITIES, default="normal")
    parser.add_argument("--gpu", action="store_true", help="use the graphics card (experimental)")
    parser.add_argument("--restart", action="store_true", help="discard earlier progress")
    parser.add_argument("--verbose", action="store_true", help="show tool output")
    args = parser.parse_args()

    pipeline = Pipeline(
        args.photos,
        args.output,
        quality=args.quality,
        gpu=args.gpu,
        restart=args.restart,
        on_log=print if args.verbose else lambda line: None,
        on_stage=print_stage,
    )
    started = time.monotonic()
    try:
        report = pipeline.run()
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
    return 0


if __name__ == "__main__":
    sys.exit(main())
