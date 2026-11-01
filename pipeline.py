"""Turn a folder of photos into a textured 3D model using COLMAP and OpenMVS."""

import hashlib
import json
import os
import re
import shutil
import subprocess
import threading
import time
from collections import namedtuple
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS_JSON = ROOT / "tools.json"
REPORT_NAME = "run_report.json"

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
MIN_PHOTOS = 3
LOW_PLACEMENT_RATIO = 0.8
LOG_TAIL_LINES = 20
POLL_SECONDS = 0.25

NO_PLACEMENT_HELP = (
    "COLMAP could not work out where the photos were taken from. The usual causes "
    "are too little overlap between photos, or plain or shiny surfaces."
)


@dataclass(frozen=True)
class Quality:
    resolution_level: int
    max_resolution: int
    gpu_image_size: int
    refine: bool


QUALITIES = {
    "draft": Quality(resolution_level=2, max_resolution=2560, gpu_image_size=1000, refine=False),
    "normal": Quality(resolution_level=1, max_resolution=2560, gpu_image_size=2000, refine=False),
    # OpenMVS caps images at 2560 pixels unless told otherwise. -1 is COLMAP's full size.
    "high": Quality(resolution_level=0, max_resolution=16384, gpu_image_size=-1, refine=True),
}

STAGE_TITLES = {
    "features": "Find features",
    "matching": "Match photos",
    "mapping": "Solve cameras",
    "select_model": "Pick largest model",
    "undistort": "Undistort photos",
    "import": "Import into OpenMVS",
    "densify": "Dense point cloud",
    "mesh": "Build mesh",
    "refine": "Refine mesh",
    "texture": "Texture and export",
}

# Stages whose results depend on the quality and GPU settings.
SETTINGS_STAGES = ("densify", "mesh", "refine", "texture")

ToolResult = namedtuple("ToolResult", "exit_code output")


class PipelineError(Exception):
    """A problem to show to the person running the program."""


class Cancelled(PipelineError):
    def __init__(self):
        super().__init__("Cancelled. Run again to continue from where it stopped.")


class LogFollower:
    """Passes on new lines as another process appends them to a file."""

    def __init__(self, path, emit):
        self.path = path
        self.emit = emit
        self.offset = path.stat().st_size if path.exists() else 0
        self.partial = ""

    def poll(self):
        try:
            with self.path.open("rb") as file:
                file.seek(self.offset)
                data = file.read()
        except OSError:
            return ""
        self.offset += len(data)
        text = data.decode("utf-8", "replace")
        *lines, self.partial = (self.partial + text).split("\n")
        for line in lines:
            self.emit(line.rstrip("\r"))
        return text


class ToolRunner:
    """Runs one external tool at a time, logging its output and allowing cancellation."""

    def __init__(self, work_dir, on_log):
        self.work_dir = work_dir
        self.on_log = on_log
        self.logs_dir = work_dir / "logs"
        self._process = None
        self._cancelled = threading.Event()

    def run(self, stage, command, tool_log=None, env=None):
        """Run a command and return its exit code and output.

        OpenMVS prints nothing to a pipe and writes <tool>-<id>.log into its
        working folder instead, so that file is followed when tool_log is given.
        """
        if self._cancelled.is_set():
            raise Cancelled()
        self.logs_dir.mkdir(exist_ok=True)
        stage_log = self.logs_dir / f"{stage}.log"
        output = []
        followers = {stage_log: LogFollower(stage_log, self.on_log)}

        def poll():
            if tool_log:
                for path in self.work_dir.glob(f"{tool_log}-*.log"):
                    followers.setdefault(path, LogFollower(path, self.on_log))
            for follower in followers.values():
                output.append(follower.poll())

        with stage_log.open("ab") as log:
            process = subprocess.Popen(
                [str(part) for part in command],
                cwd=self.work_dir,
                env=env,
                stdout=log,
                stderr=subprocess.STDOUT,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        self._process = process
        try:
            while process.poll() is None:
                poll()
                time.sleep(POLL_SECONDS)
            poll()
        finally:
            self._kill(process)
            self._process = None
            self._archive_tool_logs(followers, stage_log)
        if self._cancelled.is_set():
            raise Cancelled()
        return ToolResult(process.returncode, "".join(output))

    def cancel(self):
        self._cancelled.set()
        process = self._process
        if process:
            self._kill(process)

    @staticmethod
    def _kill(process):
        if process.poll() is None:
            subprocess.run(
                ["taskkill", "/PID", str(process.pid), "/T", "/F"],
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            process.wait()

    @staticmethod
    def _archive_tool_logs(followers, stage_log):
        for path in followers:
            if path != stage_log and path.exists():
                with stage_log.open("ab") as log:
                    log.write(path.read_bytes())
                path.unlink()


def load_tools():
    if not TOOLS_JSON.exists():
        raise PipelineError("The tools are not installed yet. Run: python setup_tools.py")
    return json.loads(TOOLS_JSON.read_text(encoding="utf-8"))


def has_gpu_tools(tools):
    return "colmap_gpu" in tools


def find_photos(folder):
    return sorted(
        path
        for path in folder.rglob("*")
        if path.is_file() and path.suffix.lower() in PHOTO_EXTENSIONS
    )


def photo_signature(folder, photos):
    """Identify a photo set by its file names and sizes."""
    digest = hashlib.sha256()
    for photo in photos:
        digest.update(f"{photo.relative_to(folder)}|{photo.stat().st_size}\n".encode())
    return digest.hexdigest()


def parse_registered_images(analyzer_output):
    match = re.search(r"Registered images:\s*(\d+)", analyzer_output)
    return int(match.group(1)) if match else 0


class Pipeline:
    def __init__(
        self,
        photos_dir,
        output_dir,
        quality="normal",
        gpu=False,
        restart=False,
        tools=None,
        runner=None,
        on_log=lambda line: None,
        on_stage=lambda stage, status: None,
    ):
        self.photos_dir = Path(photos_dir).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.work_dir = self.output_dir / "work"
        self.model_dir = self.output_dir / "model"
        self.report_path = self.output_dir / REPORT_NAME
        self.quality_name = quality
        self.quality = QUALITIES[quality]
        self.gpu = gpu
        self.restart = restart
        self.tools = tools
        self.runner = runner or ToolRunner(self.work_dir, on_log)
        self.on_log = on_log
        self.on_stage = on_stage
        self.report = {}

    def cancel(self):
        self.runner.cancel()

    def stages(self):
        return [
            stage
            for stage in STAGE_TITLES
            if stage != "refine" or self.quality.refine
        ]

    def run(self):
        """Run every unfinished stage and return the run report."""
        self.tools = self.tools or load_tools()
        if self.gpu and not has_gpu_tools(self.tools):
            raise PipelineError(
                "GPU mode is not installed yet. Run: python setup_tools.py --gpu"
            )
        photos = self._check_folders()
        if self.restart:
            self._clear_previous_run()
        self.work_dir.mkdir(parents=True, exist_ok=True)
        self.model_dir.mkdir(exist_ok=True)
        self.report = self._load_report(photos)
        self._save_report()

        for stage in self.stages():
            if stage in self.report["stages"]:
                self.on_stage(stage, "skipped")
                continue
            self.on_stage(stage, "running")
            self.report["finished"] = False
            started = time.monotonic()
            try:
                getattr(self, f"_stage_{stage}")()
            except PipelineError:
                self.on_stage(stage, "stopped")
                self._save_report()
                raise
            seconds = round(time.monotonic() - started, 1)
            self.report["stages"][stage] = {"seconds": seconds}
            self._save_report()
            self.on_stage(stage, "done")

        self.report["finished"] = True
        self.report["model"] = str(self.model_dir / "model.obj")
        self._save_report()
        return self.report

    def _check_folders(self):
        if not self.photos_dir.is_dir():
            raise PipelineError(f"The photo folder does not exist: {self.photos_dir}")
        if self.output_dir.is_relative_to(self.photos_dir):
            # COLMAP scans the photo folder recursively and would read its own output.
            raise PipelineError("The output folder cannot be inside the photo folder.")
        photos = find_photos(self.photos_dir)
        if len(photos) < MIN_PHOTOS:
            types = ", ".join(sorted(PHOTO_EXTENSIONS))
            raise PipelineError(
                f"Found {len(photos)} usable photos in {self.photos_dir}. "
                f"At least {MIN_PHOTOS} are needed ({types})."
            )
        return photos

    def _clear_previous_run(self):
        if self.report_path.exists():
            shutil.rmtree(self.work_dir, ignore_errors=True)
            shutil.rmtree(self.model_dir, ignore_errors=True)
            self.report_path.unlink()

    def _load_report(self, photos):
        signature = photo_signature(self.photos_dir, photos)
        settings = {"quality": self.quality_name, "gpu": self.gpu}
        if not self.report_path.exists():
            return {
                "photos_folder": str(self.photos_dir),
                "photos_signature": signature,
                "photos_found": len(photos),
                "photos_placed": None,
                "best_model": None,
                "settings": settings,
                "dense_device": None,
                "tool_versions": {
                    name: tool["version"] for name, tool in self.tools.items()
                },
                "warnings": [],
                "stages": {},
                "finished": False,
            }
        report = json.loads(self.report_path.read_text(encoding="utf-8"))
        if report["photos_signature"] != signature:
            raise PipelineError(
                "This output folder holds a run for different photos. "
                "Choose another folder, or start over with --restart."
            )
        if report["settings"] != settings:
            for stage in SETTINGS_STAGES:
                report["stages"].pop(stage, None)
            report["settings"] = settings
        return report

    def _save_report(self):
        temporary = self.report_path.with_suffix(".tmp")
        temporary.write_text(json.dumps(self.report, indent=2) + "\n", encoding="utf-8")
        temporary.replace(self.report_path)

    def _warn(self, message):
        self.report["warnings"].append(message)
        self.on_log(f"Warning: {message}")

    def _run(self, stage, command, tool_log=None, check=True, env=None):
        result = self.runner.run(stage, command, tool_log, env)
        if check and result.exit_code != 0:
            raise self._failure(stage, result)
        return result

    def _failure(self, stage, result):
        tail = "\n".join(result.output.splitlines()[-LOG_TAIL_LINES:])
        hint = ""
        if "out of memory" in result.output:
            hint = "\nThe graphics card ran out of memory. Try a lower quality."
        return PipelineError(
            f"{STAGE_TITLES[stage]} failed (exit code {result.exit_code}).{hint}\n{tail}"
        )

    def _colmap(self, stage, *args, check=True):
        exe = Path(self.tools["colmap"]["bin"]) / "colmap.exe"
        return self._run(stage, [exe, *args], check=check)

    def _colmap_gpu(self, stage, *args):
        tool = self.tools["colmap_gpu"]
        env = dict(os.environ)
        env["PATH"] = os.pathsep.join([*tool["dll_paths"], env.get("PATH", "")])
        return self._run(stage, [tool["exe"], *args], env=env)

    def _openmvs(self, stage, tool, *args):
        exe = Path(self.tools["openmvs"]["bin"]) / f"{tool}.exe"
        return self._run(stage, [exe, *args], tool_log=tool)

    def _stage_features(self):
        self._colmap(
            "features",
            "feature_extractor",
            "--database_path", "database.db",
            "--image_path", self.photos_dir,
            "--FeatureExtraction.use_gpu", "0",
        )

    def _stage_matching(self):
        self._colmap(
            "matching",
            "exhaustive_matcher",
            "--database_path", "database.db",
            "--FeatureMatching.use_gpu", "0",
        )

    def _stage_mapping(self):
        sparse = self.work_dir / "sparse"
        shutil.rmtree(sparse, ignore_errors=True)
        sparse.mkdir()
        result = self._colmap(
            "mapping",
            "mapper",
            "--database_path", "database.db",
            "--image_path", self.photos_dir,
            "--output_path", "sparse",
            check=False,
        )
        if result.exit_code != 0 and any(sparse.iterdir()):
            raise self._failure("mapping", result)
        if not any(sparse.iterdir()):
            raise PipelineError(NO_PLACEMENT_HELP)

    def _stage_select_model(self):
        # COLMAP can split a photo set into several separate models.
        best_model, placed = None, 0
        models = sorted(path for path in (self.work_dir / "sparse").iterdir() if path.is_dir())
        for model in models:
            result = self._colmap(
                "select_model", "model_analyzer", "--path", f"sparse/{model.name}"
            )
            registered = parse_registered_images(result.output)
            if registered > placed:
                best_model, placed = model.name, registered
        if best_model is None:
            raise PipelineError(NO_PLACEMENT_HELP)
        self.report["best_model"] = best_model
        self.report["photos_placed"] = placed
        found = self.report["photos_found"]
        self.on_log(f"Placed {placed} of {found} photos.")
        if placed < found * LOW_PLACEMENT_RATIO:
            self._warn(
                f"Only {placed} of {found} photos could be placed, so the model may "
                "have gaps. More overlap between photos usually fixes this."
            )

    def _stage_undistort(self):
        shutil.rmtree(self.work_dir / "dense", ignore_errors=True)
        best_model = self.report["best_model"]
        self._colmap(
            "undistort",
            "image_undistorter",
            "--image_path", self.photos_dir,
            "--input_path", f"sparse/{best_model}",
            "--output_path", "dense",
            "--output_type", "COLMAP",
            # Limits GPU dense time on large photo sets. COLMAP's default is 20.
            "--num_patch_match_src_images", "10",
        )

    def _stage_import(self):
        # --image-folder is relative to the -i folder.
        self._openmvs(
            "import", "InterfaceCOLMAP", "-i", "dense", "-o", "scene.mvs",
            "--image-folder", "images",
        )

    def _stage_densify(self):
        if self.gpu:
            self._densify_on_gpu()
        else:
            self._densify_on_cpu()
        self.report["dense_device"] = "GPU" if self.gpu else "CPU"

    def _densify_on_cpu(self):
        # OpenMVS would reuse depth maps from an earlier quality setting.
        for depth_map in self.work_dir.glob("*.dmap"):
            depth_map.unlink()
        self._openmvs(
            "densify",
            "DensifyPointCloud",
            "scene.mvs",
            "--resolution-level", self.quality.resolution_level,
            "--max-resolution", self.quality.max_resolution,
        )

    def _densify_on_gpu(self):
        stereo = self.work_dir / "dense" / "stereo"
        for maps in ("depth_maps", "normal_maps", "consistency_graphs"):
            shutil.rmtree(stereo / maps, ignore_errors=True)
            (stereo / maps).mkdir(parents=True)
        # A sparser window and fewer iterations are about 5 times faster than
        # COLMAP's defaults, and looked almost the same on the sample photos.
        self._colmap_gpu(
            "densify",
            "patch_match_stereo",
            "--workspace_path", "dense",
            "--workspace_format", "COLMAP",
            "--PatchMatchStereo.geom_consistency", "true",
            "--PatchMatchStereo.gpu_index", "0",
            "--PatchMatchStereo.max_image_size", self.quality.gpu_image_size,
            "--PatchMatchStereo.window_step", "2",
            "--PatchMatchStereo.num_iterations", "3",
        )
        self._colmap_gpu(
            "densify",
            "stereo_fusion",
            "--workspace_path", "dense",
            "--workspace_format", "COLMAP",
            "--input_type", "geometric",
            "--output_path", "dense/fused.ply",
        )
        self._openmvs(
            "densify", "InterfaceCOLMAP", "-i", "dense", "-o", "scene_dense.mvs",
            "--image-folder", "images", "-p", "dense/fused.ply",
        )

    def _stage_mesh(self):
        self._openmvs("mesh", "ReconstructMesh", "scene_dense.mvs", "-p", "scene_dense.ply")

    def _stage_refine(self):
        self._openmvs(
            "refine",
            "RefineMesh",
            "scene_dense.mvs",
            "-m", "scene_dense_mesh.ply",
            "-o", "scene_dense_mesh_refine.mvs",
            "--scales", "1",
            "--max-face-area", "16",
        )

    def _stage_texture(self):
        mesh = "scene_dense_mesh_refine.ply" if self.quality.refine else "scene_dense_mesh.ply"
        self._openmvs(
            "texture",
            "TextureMesh",
            "scene_dense.mvs",
            "-m", mesh,
            "-o", self.model_dir / "model.mvs",
            "--export-type", "obj",
            # Seam leveling in OpenMVS 2.4.0 paints large black patches onto the texture.
            "--global-seam-leveling", "0",
            "--local-seam-leveling", "0",
        )
        model = self.model_dir / "model.obj"
        if not model.exists() or model.stat().st_size == 0:
            raise PipelineError(f"OpenMVS finished, but {model} is missing or empty.")
