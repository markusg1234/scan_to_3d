import json
import math
import re
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pipeline import (
    NO_PLACEMENT_HELP,
    STILL_BACKGROUND_HELP,
    WEAK_PHOTOS_HELP,
    WEAK_VIDEO_HELP,
    Pipeline,
    PipelineError,
    ToolResult,
    WeakPhotos,
    facet_normal,
    format_duration,
    length_problems,
    measure_length,
    parse_blur_scores,
    parse_model_stats,
    parse_registered_images,
    photo_problems,
    pick_sharpest,
    read_camera_centers,
    project_vertices,
    ray_triangle,
    read_cameras,
    read_obj,
    read_obj_triangles,
    select_expression,
    set_real_size,
    set_real_size_on_surface,
    stage_title,
    surface_point,
    surface_problems,
    still_share,
    view_model,
    viewing_angle,
    write_stl,
)

TOOLS = {
    "colmap": {"version": "1", "bin": "colmap_bin"},
    "openmvs": {"version": "2", "bin": "openmvs_bin"},
    "ffmpeg": {"version": "4", "exe": "ffmpeg.exe"},
}
GPU_TOOLS = {
    **TOOLS,
    "colmap_gpu": {"version": "3", "exe": "colmap_gpu.exe", "dll_paths": ["gpu_dlls"]},
}
ALL_BEFORE_DENSIFY = ["features", "matching", "mapping", "select_model", "undistort", "import"]
DENSIFY_ONWARD = ["densify", "mesh", "texture"]
WEAK_STATS = "Points: 700\nMean track length: 3.04\nMean reprojection error: 1.26"
ONE_WEAK_SIGN_STATS = "Points: 3600\nMean track length: 3.04\nMean reprojection error: 0.54"
TRIANGLE_OBJ = "v 0 0 0\nv 2 0 0\nv 0 2 0\nf 1 2 3\n"
SQUARE_OBJ = "v 0 0 0\nv 2 0 0\nv 2 2 0\nv 0 2 0\nf 1 2 3 4\n"


CAMERA = (1000.0, 1000.0, 640.0, 360.0)
CAMERA_DISTANCE = 5


def write_dense_model(folder, angles, camera=CAMERA, model=1):
    """Write undistorted COLMAP files for photos taken from these angles around the y axis,
    each looking at the origin from CAMERA_DISTANCE away."""
    sparse = folder / "sparse"
    sparse.mkdir(parents=True, exist_ok=True)
    cameras = struct.pack("<QIiQQ", 1, 1, model, 1280, 720)
    (sparse / "cameras.bin").write_bytes(cameras + struct.pack(f"<{len(camera)}d", *camera))
    images = struct.pack("<Q", len(angles))
    for index, angle in enumerate(angles):
        half = math.radians(angle) / 2
        images += struct.pack(
            "<I7dI", index, math.cos(half), 0, math.sin(half), 0, 0, 0, CAMERA_DISTANCE, 1
        )
        images += f"photo{index}.jpg".encode() + b"\0" + struct.pack("<Q", 0)
    (sparse / "images.bin").write_bytes(images)


def project(angle, point):
    """Where a point appears in the photo taken from this angle, in COLMAP's pixel positions."""
    cosine, sine = math.cos(math.radians(angle)), math.sin(math.radians(angle))
    x, y, z = point
    x, z = cosine * x + sine * z, -sine * x + cosine * z + CAMERA_DISTANCE
    fx, fy, cx, cy = CAMERA
    return [fx * x / z + cx, fy * y / z + cy]


POINT_1 = (-1.0, 0.2, 0.0)
POINT_2 = (1.0, -0.3, 0.5)


def clicks_from(angles):
    return {
        f"photo{index}.jpg": [project(angle, POINT_1), project(angle, POINT_2)]
        for index, angle in enumerate(angles)
    }


def read_stl(path):
    """Read a binary STL's header and each triangle's normal, corners and attribute."""
    data = path.read_bytes()
    count = struct.unpack_from("<I", data, 80)[0]
    if len(data) != 84 + 50 * count:
        raise AssertionError(f"{len(data)} bytes don't hold {count} triangles")
    return data[:80], [struct.unpack_from("<12fH", data, 84 + 50 * n) for n in range(count)]


def blur_text(scores):
    """Write scores the way ffmpeg's metadata filter does."""
    lines = []
    for frame, score in enumerate(scores):
        lines.append(f"frame:{frame:<4} pts:{frame * 512:<7} pts_time:{frame / 30}")
        lines.append(f"lavfi.blur={score}")
    return "\n".join(lines) + "\n"


def images_bin(photos, still=0, moving=0, pause=0, shift=50):
    """Build an images.bin whose points stay put or shift, once a pause at the start is over."""
    data = struct.pack("<Q", photos)
    for photo in range(photos):
        data += struct.pack("<I7dI", photo, 1, 0, 0, 0, 0, 0, 0, 1)
        data += f"photo{photo}.jpg".encode() + b"\0"
        data += struct.pack("<Q", still + moving + 1)
        data += struct.pack("<ddq", 7, 7, -1)  # A feature that isn't part of any point.
        for point in range(still):
            data += struct.pack("<ddq", 99, 99, point)
        for point in range(still, still + moving):
            data += struct.pack("<ddq", 99 + shift * max(photo - pause, 0), 99, point)
    return data


class FakeRunner:
    """Stands in for ToolRunner: records commands and creates the files tools would."""

    def __init__(
        self,
        models=None,
        fail_stage=None,
        fail_output="first line\nsecond line",
        mapper_exit=0,
        analyzer_stats="",
        blur_scores=(5.0, 4.0, 3.0, 2.0, 1.0, 5.0, 4.0, 3.0, 2.0, 1.0),
        lost_frames=0,
        images=None,
        obj=TRIANGLE_OBJ,
    ):
        self.models = {"0": 5} if models is None else models
        self.fail_stage = fail_stage
        self.fail_output = fail_output
        self.mapper_exit = mapper_exit
        self.analyzer_stats = analyzer_stats
        self.blur_scores = blur_scores
        self.lost_frames = lost_frames
        self.images = images
        self.obj = obj
        self.stages = []
        self.commands = {}
        self.calls = []
        self.output_dir = None

    def run(self, stage, command, tool_log=None, env=None):
        command = [str(part) for part in command]
        if not self.stages or self.stages[-1] != stage:
            self.stages.append(stage)
        self.commands[stage] = command
        self.calls.append((stage, command, env))
        if stage == self.fail_stage:
            return ToolResult(3, self.fail_output)
        if stage == "frames":
            self._write_video_files(command)
            return ToolResult(0, "")
        if stage == "mapping":
            for name in self.models:
                model = self.output_dir / "work" / "sparse" / name
                model.mkdir()
                if self.images:
                    (model / "images.bin").write_bytes(self.images)
            return ToolResult(self.mapper_exit, "")
        if stage == "select_model":
            name = command[-1].split("/")[-1]
            return ToolResult(0, f"Registered images: {self.models[name]}\n{self.analyzer_stats}")
        if stage == "texture":
            (self.output_dir / "model" / "model.obj").write_text(self.obj)
        return ToolResult(0, "")

    def _write_video_files(self, command):
        work = self.output_dir / "work"
        if command[-1] == "-":
            (work / "blur.txt").write_text(blur_text(self.blur_scores), encoding="utf-8")
            return
        picked = re.findall(r"eq\(n,(\d+)\)", (work / "select.txt").read_text(encoding="utf-8"))
        for number in range(1, len(picked) - self.lost_frames + 1):
            (work / "frames" / f"frame_{number:04}.jpg").write_bytes(b"frame")


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.root = Path(folder.name)
        self.photos = self.root / "photos"
        self.photos.mkdir()
        for name in "abcde":
            (self.photos / f"{name}.jpg").write_bytes(b"photo")
        self.output = self.root / "output"

    def pipeline(self, runner, **options):
        runner.output_dir = self.output.resolve()
        options.setdefault("tools", TOOLS)
        return Pipeline(self.photos, self.output, runner=runner, **options)

    def run_pipeline(self, runner=None, **options):
        runner = runner or FakeRunner()
        return self.pipeline(runner, **options).run(), runner

    def saved_report(self):
        return json.loads((self.output / "run_report.json").read_text(encoding="utf-8"))


class QualityTests(PipelineTestCase):
    def test_draft_uses_a_low_resolution_and_skips_refinement(self):
        _, runner = self.run_pipeline(quality="draft")
        densify = runner.commands["densify"]
        self.assertEqual(densify[densify.index("--resolution-level") + 1], "2")
        self.assertNotIn("refine", runner.stages)
        self.assertIn("scene_dense_mesh.ply", runner.commands["texture"])

    def test_normal_is_the_default(self):
        report, runner = self.run_pipeline()
        densify = runner.commands["densify"]
        self.assertEqual(densify[densify.index("--resolution-level") + 1], "1")
        self.assertEqual(report["settings"]["quality"], "normal")

    def test_high_uses_full_resolution_and_textures_the_refined_mesh(self):
        _, runner = self.run_pipeline(quality="high")
        densify = runner.commands["densify"]
        self.assertEqual(densify[densify.index("--resolution-level") + 1], "0")
        self.assertEqual(densify[densify.index("--max-resolution") + 1], "16384")
        self.assertIn("refine", runner.stages)
        self.assertIn("scene_dense_mesh_refine.ply", runner.commands["texture"])


    def test_seam_leveling_stays_off(self):
        _, runner = self.run_pipeline()
        texture = runner.commands["texture"]
        for option in ("--global-seam-leveling", "--local-seam-leveling"):
            self.assertEqual(texture[texture.index(option) + 1], "0")


class ModelSelectionTests(PipelineTestCase):
    def test_largest_model_wins(self):
        report, runner = self.run_pipeline(FakeRunner(models={"0": 2, "1": 4}))
        self.assertEqual(report["best_model"], "1")
        self.assertEqual(report["photos_placed"], 4)
        self.assertIn("sparse/1", runner.commands["undistort"])
        self.assertEqual(report["warnings"], [])

    def test_few_placed_photos_warn_but_continue(self):
        report, _ = self.run_pipeline(FakeRunner(models={"0": 3}))
        self.assertTrue(report["finished"])
        self.assertIn("Only 3 of 5", report["warnings"][0])

    def test_no_model_explains_the_usual_causes(self):
        for runner in (FakeRunner(models={}), FakeRunner(models={}, mapper_exit=1)):
            with self.subTest(mapper_exit=runner.mapper_exit):
                with self.assertRaises(PipelineError) as caught:
                    self.run_pipeline(runner, restart=True)
                self.assertEqual(str(caught.exception), NO_PLACEMENT_HELP)

    def test_models_with_no_placed_photos_explain_the_usual_causes(self):
        with self.assertRaises(PipelineError) as caught:
            self.run_pipeline(FakeRunner(models={"0": 0}))
        self.assertEqual(str(caught.exception), NO_PLACEMENT_HELP)


class ResumeTests(PipelineTestCase):
    def test_failure_names_the_stage_and_shows_the_log_tail(self):
        with self.assertRaises(PipelineError) as caught:
            self.run_pipeline(FakeRunner(fail_stage="densify"))
        message = str(caught.exception)
        self.assertIn("Dense point cloud failed (exit code 3)", message)
        self.assertIn("second line", message)
        self.assertFalse(self.saved_report()["finished"])

    def test_rerun_continues_from_the_failed_stage(self):
        with self.assertRaises(PipelineError):
            self.run_pipeline(FakeRunner(fail_stage="densify"))
        _, runner = self.run_pipeline()
        self.assertEqual(runner.stages, DENSIFY_ONWARD)

    def test_finished_run_does_nothing_on_rerun(self):
        self.run_pipeline()
        _, runner = self.run_pipeline()
        self.assertEqual(runner.stages, [])

    def test_quality_change_reruns_only_the_dense_stages(self):
        self.run_pipeline(quality="draft")
        stale_depth_map = self.output / "work" / "depth0001.dmap"
        stale_depth_map.write_bytes(b"old")
        _, runner = self.run_pipeline(quality="normal")
        self.assertEqual(runner.stages, DENSIFY_ONWARD)
        self.assertFalse(stale_depth_map.exists())

    def test_restart_runs_everything_again(self):
        self.run_pipeline()
        leftover = self.output / "work" / "leftover.txt"
        leftover.write_text("old")
        _, runner = self.run_pipeline(restart=True)
        self.assertEqual(runner.stages, ALL_BEFORE_DENSIFY + DENSIFY_ONWARD)
        self.assertFalse(leftover.exists())

    def test_restart_leaves_a_folder_this_program_never_used_alone(self):
        self.output.mkdir()
        (self.output / "work").mkdir()
        keep = self.output / "work" / "keep.txt"
        keep.write_text("mine")
        self.run_pipeline(restart=True)
        self.assertTrue(keep.exists())


class FolderTests(PipelineTestCase):
    def test_different_photos_are_refused(self):
        self.run_pipeline()
        (self.photos / "f.jpg").write_bytes(b"photo")
        with self.assertRaisesRegex(PipelineError, "different photos"):
            self.run_pipeline()

    def test_output_inside_photo_folder_is_refused(self):
        with self.assertRaisesRegex(PipelineError, "cannot be inside"):
            Pipeline(self.photos, self.photos / "out", tools=TOOLS, runner=FakeRunner()).run()

    def test_too_few_photos_are_refused(self):
        (self.photos / "a.jpg").unlink()
        (self.photos / "b.jpg").unlink()
        (self.photos / "c.jpg").unlink()
        with self.assertRaisesRegex(PipelineError, "Found 2 usable photos"):
            self.run_pipeline()

    def test_missing_photo_folder_is_refused(self):
        with self.assertRaisesRegex(PipelineError, "does not exist"):
            Pipeline(self.root / "nope", self.output, tools=TOOLS, runner=FakeRunner()).run()

    def test_missing_model_file_is_an_error(self):
        class NoModelRunner(FakeRunner):
            def run(self, stage, command, tool_log=None, env=None):
                if stage == "texture":
                    self.stages.append(stage)
                    return ToolResult(0, "")
                return super().run(stage, command, tool_log, env)

        with self.assertRaisesRegex(PipelineError, "missing or empty"):
            self.run_pipeline(NoModelRunner())


class StlTests(PipelineTestCase):
    def test_a_finished_run_saves_an_stl_beside_the_obj(self):
        report, _ = self.run_pipeline()
        stl = self.output.resolve() / "model" / "model.stl"
        header, triangles = read_stl(stl)
        self.assertFalse(header.startswith(b"solid"))
        self.assertEqual(triangles, [(0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 2, 0, 0)])
        self.assertEqual(report["stl"], str(stl))
        self.assertIn("stl", self.saved_report()["stages"])

    def test_a_run_finished_before_stl_output_only_saves_the_stl(self):
        self.run_pipeline()
        stl = self.output / "model" / "model.stl"
        stl.unlink()
        report = self.saved_report()
        del report["stages"]["stl"], report["stl"]
        (self.output / "run_report.json").write_text(json.dumps(report), encoding="utf-8")
        _, runner = self.run_pipeline()
        self.assertEqual(runner.stages, [])
        self.assertTrue(stl.exists())

    def test_a_quality_change_saves_the_stl_again(self):
        self.run_pipeline(quality="draft")
        self.run_pipeline(FakeRunner(obj=SQUARE_OBJ), quality="normal")
        _, triangles = read_stl(self.output / "model" / "model.stl")
        self.assertEqual(len(triangles), 2)

    def test_a_model_with_no_surface_is_an_error(self):
        with self.assertRaisesRegex(PipelineError, "no surface to save as STL"):
            self.run_pipeline(FakeRunner(obj="v 0 0 0\n"))
        stages = self.saved_report()["stages"]
        self.assertIn("texture", stages)
        self.assertNotIn("stl", stages)


def viewer_process(exit_code=None):
    """Stand in for a viewer that exits with this code as it starts, or stays open if None."""
    process = mock.Mock()
    if exit_code is None:
        process.wait.side_effect = subprocess.TimeoutExpired("Viewer.exe", 5)
    else:
        process.wait.return_value = exit_code
    return process


class ViewerTests(PipelineTestCase):
    CRASH = 0xC0000005

    def view(self, *processes):
        with mock.patch("pipeline.subprocess.Popen", side_effect=processes) as popen:
            view_model(self.output, TOOLS)
        return popen

    def test_the_viewer_runs_in_the_model_folder_so_it_finds_the_texture(self):
        self.run_pipeline()
        popen = self.view(viewer_process())
        popen.assert_called_once_with(
            [Path("openmvs_bin") / "Viewer.exe", "-i", "model.obj"],
            cwd=self.output.resolve() / "model",
        )

    def test_a_viewer_that_crashes_as_it_starts_is_opened_again(self):
        self.run_pipeline()
        popen = self.view(viewer_process(self.CRASH), viewer_process())
        self.assertEqual(popen.call_count, 2)

    def test_a_viewer_closed_straight_away_is_left_closed(self):
        self.run_pipeline()
        popen = self.view(viewer_process(0))
        self.assertEqual(popen.call_count, 1)

    def test_a_viewer_that_keeps_crashing_is_an_error(self):
        self.run_pipeline()
        with self.assertRaisesRegex(PipelineError, "crashed 3 times"):
            self.view(*[viewer_process(self.CRASH) for _ in range(3)])

    def test_a_folder_with_no_model_opens_nothing(self):
        with mock.patch("pipeline.subprocess.Popen") as popen:
            with self.assertRaisesRegex(PipelineError, "no model"):
                view_model(self.output, TOOLS)
        popen.assert_not_called()


class MeasureTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.dense = Path(folder.name)

    def test_photos_from_two_angles_give_the_true_distance(self):
        write_dense_model(self.dense, [0, 90])
        measurement = measure_length(self.dense, clicks_from([0, 90]))
        self.assertAlmostEqual(measurement.distance, math.dist(POINT_1, POINT_2))
        self.assertLess(measurement.worst_error, 1e-6)
        self.assertEqual(length_problems(measurement), [])

    def test_more_photos_and_a_point_missing_from_one_still_work(self):
        write_dense_model(self.dense, [0, 40, 80])
        clicks = clicks_from([0, 40, 80])
        clicks["photo1.jpg"][1] = None
        self.assertAlmostEqual(
            measure_length(self.dense, clicks).distance, math.dist(POINT_1, POINT_2)
        )

    def test_a_wrong_click_shows_as_a_large_error(self):
        write_dense_model(self.dense, [0, 45, 90])
        clicks = clicks_from([0, 45, 90])
        clicks["photo1.jpg"][0][0] += 20
        measurement = measure_length(self.dense, clicks)
        self.assertGreater(measurement.worst_error, 3)
        self.assertIn("pixels from where", length_problems(measurement)[0])

    def test_photos_from_nearly_the_same_angle_are_flagged(self):
        write_dense_model(self.dense, [0, 5])
        measurement = measure_length(self.dense, clicks_from([0, 5]))
        self.assertLess(measurement.narrowest_angle, 10)
        self.assertIn("degrees apart", length_problems(measurement)[0])

    def test_each_point_needs_two_photos(self):
        write_dense_model(self.dense, [0, 90])
        clicks = clicks_from([0, 90])
        clicks["photo1.jpg"][1] = None
        with self.assertRaisesRegex(PipelineError, "Point 2 has been clicked in 1 photo,"):
            measure_length(self.dense, clicks)

    def test_the_same_spot_for_both_points_is_refused(self):
        write_dense_model(self.dense, [0, 90])
        clicks = {name: [points[0], points[0]] for name, points in clicks_from([0, 90]).items()}
        with self.assertRaisesRegex(PipelineError, "same spot"):
            measure_length(self.dense, clicks)

    def test_missing_working_files_are_explained(self):
        with self.assertRaisesRegex(PipelineError, "working files"):
            measure_length(self.dense, clicks_from([0, 90]))

    def test_a_camera_with_one_focal_length_is_read(self):
        write_dense_model(self.dense, [0], camera=(1000, 640, 360), model=0)
        self.assertEqual(read_cameras(self.dense / "sparse" / "cameras.bin"), {1: CAMERA})

    def test_a_camera_with_lens_distortion_is_refused(self):
        write_dense_model(self.dense, [0], camera=(1000, 640, 360, 0.1), model=2)
        with self.assertRaisesRegex(PipelineError, "lens distortion"):
            read_cameras(self.dense / "sparse" / "cameras.bin")


class RealSizeTests(PipelineTestCase):
    def finished_run(self, **options):
        report, _ = self.run_pipeline(**options)
        write_dense_model(self.output / "work" / "dense", [0, 90])
        return report

    def set_ten_mm_per_unit(self):
        length = 10 * math.dist(POINT_1, POINT_2)
        return set_real_size(self.output, clicks_from([0, 90]), length)

    def stl_corners(self):
        _, triangles = read_stl(self.output / "model" / "model.stl")
        return [round(value, 4) for value in triangles[0][3:12]]

    def test_the_stl_is_saved_in_millimetres(self):
        self.finished_run()
        real_size = self.set_ten_mm_per_unit()
        self.assertAlmostEqual(real_size.mm_per_unit, 10)
        self.assertEqual([round(value, 6) for value in real_size.size_mm], [20, 20, 0])
        self.assertEqual(self.stl_corners(), [0, 0, 0, 20, 0, 0, 0, 20, 0])
        saved = self.saved_report()["real_size"]
        self.assertAlmostEqual(saved["mm_per_unit"], 10)
        self.assertEqual(saved["clicks"], clicks_from([0, 90]))

    def test_a_quality_change_keeps_the_real_size(self):
        self.finished_run(quality="draft")
        self.set_ten_mm_per_unit()
        report, _ = self.run_pipeline(quality="normal")
        self.assertIn("real_size", report)
        self.assertEqual(self.stl_corners(), [0, 0, 0, 20, 0, 0, 0, 20, 0])

    def test_starting_over_drops_the_real_size(self):
        self.finished_run()
        self.set_ten_mm_per_unit()
        report, _ = self.run_pipeline(restart=True)
        self.assertNotIn("real_size", report)
        self.assertEqual(self.stl_corners(), [0, 0, 0, 2, 0, 0, 0, 2, 0])

    def test_a_folder_with_no_model_is_refused(self):
        with self.assertRaisesRegex(PipelineError, "no model"):
            set_real_size(self.output, clicks_from([0, 90]), 100)

    def test_a_length_of_zero_is_refused(self):
        self.finished_run()
        with self.assertRaisesRegex(PipelineError, "more than 0"):
            set_real_size(self.output, clicks_from([0, 90]), 0)


PLANE_OBJ = "v -3 -3 0\nv 3 -3 0\nv 3 3 0\nv -3 3 0\nf 1 2 3 4\n"
# Two pairs of spots on that plane, each photo measuring its own pair.
PLANE_PAIRS = {0: ((-1.0, 0.2, 0.0), (1.0, -0.3, 0.0)), 30: ((0.5, 1.0, 0.0), (-0.5, -1.0, 0.0))}


def plane_clicks():
    return {
        f"photo{index}.jpg": [project(angle, point) for point in PLANE_PAIRS[angle]]
        for index, angle in enumerate(PLANE_PAIRS)
    }


def ten_mm_per_unit_lengths():
    return {
        f"photo{index}.jpg": 10 * math.dist(*PLANE_PAIRS[angle])
        for index, angle in enumerate(PLANE_PAIRS)
    }


class SurfaceTests(PipelineTestCase):
    def finished_run(self):
        self.run_pipeline(FakeRunner(obj=PLANE_OBJ))
        write_dense_model(self.output / "work" / "dense", list(PLANE_PAIRS))

    def test_a_ray_meets_a_triangle_at_the_right_distance(self):
        triangle = ((-1, -1, 0), (1, -1, 0), (0, 1, 0))
        self.assertAlmostEqual(ray_triangle((0, 0, -5), (0, 0, 1), *triangle), 5)
        self.assertIsNone(ray_triangle((5, 0, -5), (0, 0, 1), *triangle))
        self.assertIsNone(ray_triangle((0, 0, 5), (0, 0, 1), *triangle))  # Behind the start.
        self.assertIsNone(ray_triangle((0, 0, -5), (1, 0, 0), *triangle))  # Along its plane.

    def test_the_nearest_surface_along_the_line_of_sight_is_found(self):
        near = "v -0.2 -0.2 -1\nv 0.2 -0.2 -1\nv 0.2 0.2 -1\nv -0.2 0.2 -1\nf 5 6 7 8\n"
        path = self.root / "two.obj"
        path.write_text(PLANE_OBJ + near)
        vertices, faces = read_obj(path)
        camera, pose = CAMERA, (1, ((1, 0, 0), (0, 1, 0), (0, 0, 1)), (0, 0, CAMERA_DISTANCE))
        projection = project_vertices(vertices, camera, pose)
        found = surface_point(vertices, faces, projection, camera, pose, project(0, (0, 0, 0)))
        self.assertEqual([round(value, 9) for value in found], [0, 0, -1])
        found = surface_point(vertices, faces, projection, camera, pose, project(0, (1, 1, 0)))
        self.assertEqual([round(value, 9) for value in found], [1, 1, 0])
        off_the_model = project(0, (5, 0, 0))
        self.assertIsNone(surface_point(vertices, faces, projection, camera, pose, off_the_model))

    def test_each_photo_can_measure_its_own_pair_of_spots(self):
        self.finished_run()
        real_size = set_real_size_on_surface(self.output, plane_clicks(), ten_mm_per_unit_lengths())
        self.assertAlmostEqual(real_size.mm_per_unit, 10)
        self.assertLess(real_size.spread, 1e-9)
        self.assertEqual(surface_problems(real_size), [])
        _, triangles = read_stl(self.output / "model" / "model.stl")
        self.assertEqual([round(value, 3) for value in triangles[0][3:6]], [-30, -30, 0])
        saved = self.saved_report()["real_size"]
        self.assertEqual(saved["method"], "surface")
        self.assertEqual(saved["lengths_mm"], ten_mm_per_unit_lengths())

    def test_photos_that_disagree_are_flagged(self):
        self.finished_run()
        lengths = ten_mm_per_unit_lengths()
        lengths["photo1.jpg"] *= 1.1
        problems = surface_problems(set_real_size_on_surface(self.output, plane_clicks(), lengths))
        self.assertIn("disagree by 10%", problems[0])

    def test_one_photo_works_but_is_flagged_as_unchecked(self):
        self.finished_run()
        clicks = {"photo0.jpg": plane_clicks()["photo0.jpg"]}
        real_size = set_real_size_on_surface(self.output, clicks, ten_mm_per_unit_lengths())
        self.assertAlmostEqual(real_size.mm_per_unit, 10)
        self.assertIn("Only one photo", surface_problems(real_size)[0])

    def test_a_spot_beside_a_step_is_flagged_as_on_an_edge(self):
        raised_half = "v -3 -3 -1\nv 0 -3 -1\nv 0 3 -1\nv -3 3 -1\nf 5 6 7 8\n"
        self.run_pipeline(FakeRunner(obj=PLANE_OBJ + raised_half))
        write_dense_model(self.output / "work" / "dense", [0])
        beside_the_step, far_from_it = (0.02, 0.5, 0.0), (1.5, 0.5, 0.0)
        clicks = {"photo0.jpg": [project(0, beside_the_step), project(0, far_from_it)]}
        lengths = {"photo0.jpg": 10 * math.dist(beside_the_step, far_from_it)}
        real_size = set_real_size_on_surface(self.output, clicks, lengths)
        self.assertAlmostEqual(real_size.mm_per_unit, 10)
        self.assertEqual(real_size.edge_spots, ["point 1 in photo0.jpg"])
        self.assertIn("Point 1 in photo0.jpg is on an edge", surface_problems(real_size)[0])

    def test_a_spot_off_the_model_is_explained(self):
        self.finished_run()
        clicks = plane_clicks()
        clicks["photo0.jpg"][1] = project(0, (5, 0, 0))
        with self.assertRaisesRegex(PipelineError, "Point 2 in photo0.jpg isn't on the model"):
            set_real_size_on_surface(self.output, clicks, ten_mm_per_unit_lengths())

    def test_a_photo_without_its_length_or_second_point_is_explained(self):
        self.finished_run()
        lengths = ten_mm_per_unit_lengths()
        del lengths["photo1.jpg"]
        with self.assertRaisesRegex(PipelineError, "Type the real length .* photo1.jpg"):
            set_real_size_on_surface(self.output, plane_clicks(), lengths)
        clicks = plane_clicks()
        clicks["photo1.jpg"][1] = None
        with self.assertRaisesRegex(PipelineError, "photo1.jpg has only one point"):
            set_real_size_on_surface(self.output, clicks, ten_mm_per_unit_lengths())

    def test_the_mesh_reader_keeps_corner_numbers_and_counts_back_from_the_end(self):
        path = self.root / "model.obj"
        path.write_text("v 0 0 0\nv 1 0 0\nv 1 1 0\nv 0 1 0\nf -4 -3 -2 -1\n")
        vertices, faces = read_obj(path)
        self.assertEqual(faces, [(0, 1, 2), (0, 2, 3)])
        self.assertEqual(vertices[3], (0, 1, 0))


class StlFileTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def triangles(self, text):
        path = self.folder / "model.obj"
        path.write_bytes(text.encode())
        return read_obj_triangles(path)

    def test_corners_can_carry_texture_and_normal_numbers(self):
        vertices = "v 0 0 0\nv 1 0 0\nv 0 1 0\n"
        for face in ("f 1/1 2/2 3/3", "f 1//1 2//1 3//1", "f 1/1/1 2/2/1 3/3/1"):
            with self.subTest(face=face):
                self.assertEqual(
                    self.triangles(vertices + face + "\n"), [((0, 0, 0), (1, 0, 0), (0, 1, 0))]
                )

    def test_negative_numbers_count_back_from_the_last_vertex(self):
        text = "v 9 9 9\nv 0 0 0\nv 1 0 0\nv 0 1 0\nf -3 -2 -1\n"
        self.assertEqual(self.triangles(text), [((0, 0, 0), (1, 0, 0), (0, 1, 0))])

    def test_a_larger_face_is_split_into_triangles(self):
        self.assertEqual(
            self.triangles(SQUARE_OBJ),
            [((0, 0, 0), (2, 0, 0), (2, 2, 0)), ((0, 0, 0), (2, 2, 0), (0, 2, 0))],
        )

    def test_lines_the_way_openmvs_writes_them(self):
        text = (
            "mtllib model.mtl\r\n"
            "v -0.309924 2.643584 1.488626\r\nv 1 0 0\r\nv 0 1 0\r\n"
            "vt 0.169863 0.817055\r\nvt 0 0\r\nvt 1 1\r\n"
            "# A comment\r\n"
            "usemtl material_00\r\n"
            "f 1/1 2/2 3/3\r\n"
        )
        self.assertEqual(
            self.triangles(text), [((-0.309924, 2.643584, 1.488626), (1, 0, 0), (0, 1, 0))]
        )

    def test_the_normal_follows_the_corner_order(self):
        a, b, c = (0, 0, 0), (2, 0, 0), (0, 2, 0)
        self.assertEqual(facet_normal(a, b, c), [0, 0, 1])
        self.assertEqual(facet_normal(a, c, b), [0, 0, -1])

    def test_a_triangle_with_no_area_gets_a_zero_normal(self):
        self.assertEqual(facet_normal((0, 0, 0), (1, 1, 1), (2, 2, 2)), [0, 0, 0])

    def test_each_triangle_is_written_with_its_normal(self):
        path = self.folder / "model.stl"
        write_stl(path, [((0, 0, 0), (2, 0, 0), (0, 2, 0)), ((0, 0, 0), (0, 0, 2), (2, 0, 0))])
        header, triangles = read_stl(path)
        self.assertFalse(header.startswith(b"solid"))
        self.assertEqual(
            triangles,
            [(0, 0, 1, 0, 0, 0, 2, 0, 0, 0, 2, 0, 0), (0, 1, 0, 0, 0, 0, 0, 0, 2, 2, 0, 0, 0)],
        )


class GpuTests(PipelineTestCase):
    def test_gpu_mode_needs_its_tools_installed(self):
        with self.assertRaisesRegex(PipelineError, "setup_tools.py --gpu"):
            self.run_pipeline(gpu=True)

    def test_dense_stage_runs_on_the_card_then_hands_over_to_openmvs(self):
        report, runner = self.run_pipeline(gpu=True, tools=GPU_TOOLS)
        densify = [call for call in runner.calls if call[0] == "densify"]
        self.assertEqual(
            [command[:2] for _, command, _ in densify[:2]],
            [["colmap_gpu.exe", "patch_match_stereo"], ["colmap_gpu.exe", "stereo_fusion"]],
        )
        self.assertTrue(densify[0][2]["PATH"].startswith("gpu_dlls"))
        handover = densify[2][1]
        self.assertIn("InterfaceCOLMAP", handover[0])
        self.assertEqual(handover[handover.index("-p") + 1], "dense/fused.ply")
        self.assertEqual(handover[handover.index("-o") + 1], "scene_dense.mvs")
        self.assertEqual(report["dense_device"], "GPU")

    def test_image_size_follows_the_quality_setting(self):
        for quality, size in (("draft", "1000"), ("normal", "2000"), ("high", "-1")):
            with self.subTest(quality=quality):
                _, runner = self.run_pipeline(gpu=True, tools=GPU_TOOLS, quality=quality, restart=True)
                command = next(c for s, c, _ in runner.calls if "patch_match_stereo" in c)
                self.assertEqual(command[command.index("--PatchMatchStereo.max_image_size") + 1], size)

    def test_source_photos_per_view_are_limited(self):
        _, runner = self.run_pipeline(gpu=True, tools=GPU_TOOLS)
        command = runner.commands["undistort"]
        self.assertEqual(command[command.index("--num_patch_match_src_images") + 1], "10")

    def test_other_stages_stay_on_the_cpu(self):
        _, runner = self.run_pipeline(gpu=True, tools=GPU_TOOLS, quality="high")
        self.assertIn("colmap_bin", runner.commands["features"][0])
        for stage in ("mesh", "refine", "texture"):
            self.assertIn("openmvs_bin", runner.commands[stage][0])

    def test_cpu_mode_does_not_use_the_card(self):
        report, runner = self.run_pipeline(tools=GPU_TOOLS)
        self.assertIn("DensifyPointCloud", runner.commands["densify"][0])
        self.assertEqual(report["dense_device"], "CPU")

    def test_gpu_mode_says_the_card_is_taking_over(self):
        for gpu, says_so in ((True, True), (False, False)):
            with self.subTest(gpu=gpu):
                lines = []
                self.run_pipeline(gpu=gpu, tools=GPU_TOOLS, restart=True, on_log=lines.append)
                self.assertEqual(any("on the graphics card" in line for line in lines), says_so)

    def test_the_dense_stage_is_named_after_where_it_runs(self):
        self.assertEqual(stage_title("densify", True), "Dense point cloud (graphics card)")
        self.assertEqual(stage_title("densify", False), "Dense point cloud (CPU)")
        self.assertEqual(stage_title("mesh", True), "Build mesh")

    def test_switching_to_gpu_reruns_the_dense_stages(self):
        self.run_pipeline(tools=GPU_TOOLS)
        _, runner = self.run_pipeline(gpu=True, tools=GPU_TOOLS)
        self.assertEqual(runner.stages, DENSIFY_ONWARD)

    def test_running_out_of_memory_suggests_a_lower_quality(self):
        runner = FakeRunner(fail_stage="densify", fail_output="CUDA error - out of memory")
        with self.assertRaisesRegex(PipelineError, "Try a lower quality"):
            self.run_pipeline(runner, gpu=True, tools=GPU_TOOLS)


class PhotoCheckTests(PipelineTestCase):
    def test_weak_photos_stop_before_the_dense_stage(self):
        runner = FakeRunner(analyzer_stats=WEAK_STATS)
        with self.assertRaises(WeakPhotos) as caught:
            self.run_pipeline(runner)
        self.assertIn("140 matched points per photo", str(caught.exception))
        self.assertNotIn("undistort", runner.stages)
        report = self.saved_report()
        self.assertEqual(len(report["photo_check"]["problems"]), 3)
        self.assertNotIn("select_model", report["stages"])

    def test_accepting_weak_photos_keeps_finished_stages_and_warns(self):
        with self.assertRaises(WeakPhotos):
            self.run_pipeline(FakeRunner(analyzer_stats=WEAK_STATS))
        report, runner = self.run_pipeline(
            FakeRunner(analyzer_stats=WEAK_STATS), accept_weak_photos=True
        )
        self.assertEqual(runner.stages, ["select_model", "undistort", "import", *DENSIFY_ONWARD])
        self.assertTrue(report["finished"])
        self.assertEqual(len(report["warnings"]), 1)
        self.assertIn("looked weak", report["warnings"][0])

    def test_running_the_check_twice_does_not_repeat_warnings(self):
        with self.assertRaises(WeakPhotos):
            self.run_pipeline(FakeRunner(models={"0": 3}, analyzer_stats=WEAK_STATS))
        report, _ = self.run_pipeline(
            FakeRunner(models={"0": 3}, analyzer_stats=WEAK_STATS), accept_weak_photos=True
        )
        self.assertEqual(len(report["warnings"]), 2)

    def test_one_weak_sign_does_not_stop_a_run(self):
        report, _ = self.run_pipeline(FakeRunner(analyzer_stats=ONE_WEAK_SIGN_STATS))
        self.assertTrue(report["finished"])
        self.assertEqual(len(report["photo_check"]["problems"]), 1)
        self.assertEqual(report["warnings"], [])

    def test_missing_numbers_skip_the_check(self):
        report, _ = self.run_pipeline(FakeRunner())
        self.assertTrue(report["finished"])
        self.assertEqual(report["photo_check"]["problems"], [])

    def test_the_castle_passes_and_the_room_photos_fail(self):
        castle = {"points": 7968, "track_length": 4.81, "error": 0.54}
        room = {"points": 2107, "track_length": 3.04, "error": 1.26}
        self.assertEqual(photo_problems(castle, 71, 11), [])
        self.assertEqual(len(photo_problems(room, 32, 14)), 4)

    def test_a_still_background_stops_a_run_on_its_own(self):
        runner = FakeRunner(images=images_bin(10, still=1, moving=9))
        with self.assertRaises(WeakPhotos) as caught:
            self.run_pipeline(runner)
        self.assertIn("10% of the points stay still", str(caught.exception))
        self.assertIn(STILL_BACKGROUND_HELP, str(caught.exception))
        self.assertNotIn(WEAK_PHOTOS_HELP, str(caught.exception))
        self.assertNotIn("undistort", runner.stages)
        self.assertEqual(self.saved_report()["photo_check"]["still_share"], 0.1)

    def test_a_still_background_can_be_accepted(self):
        report, _ = self.run_pipeline(
            FakeRunner(images=images_bin(10, still=1, moving=9)), accept_weak_photos=True
        )
        self.assertTrue(report["finished"])
        self.assertIn("looked weak", report["warnings"][0])

    def test_a_few_still_points_do_not_stop_a_run(self):
        report, _ = self.run_pipeline(FakeRunner(images=images_bin(10, still=1, moving=39)))
        self.assertTrue(report["finished"])
        self.assertEqual(report["photo_check"]["still_share"], 0.025)
        self.assertEqual(report["photo_check"]["problems"], [])

    def test_weak_signs_with_a_still_background_get_the_background_advice(self):
        runner = FakeRunner(analyzer_stats=WEAK_STATS, images=images_bin(10, still=1))
        with self.assertRaises(WeakPhotos) as caught:
            self.run_pipeline(runner)
        self.assertIn(STILL_BACKGROUND_HELP, str(caught.exception))
        self.assertEqual(len(self.saved_report()["photo_check"]["problems"]), 4)


class VideoTests(PipelineTestCase):
    def setUp(self):
        super().setUp()
        self.video = self.root / "orbit.mp4"
        self.video.write_bytes(b"video")

    def run_video(self, runner=None, **options):
        runner = runner or FakeRunner()
        runner.output_dir = self.output.resolve()
        options.setdefault("tools", TOOLS)
        options.setdefault("video_frames", 3)
        return Pipeline(self.video, self.output, runner=runner, **options).run(), runner

    def picked_frames(self):
        text = (self.output / "work" / "select.txt").read_text(encoding="utf-8")
        return [int(frame) for frame in re.findall(r"eq\(n,(\d+)\)", text)]

    def test_frames_come_first_and_every_later_stage_reads_them(self):
        report, runner = self.run_video()
        self.assertEqual(runner.stages, ["frames", *ALL_BEFORE_DENSIFY, *DENSIFY_ONWARD])
        frames = str(self.output.resolve() / "work" / "frames")
        for stage in ("features", "mapping", "undistort"):
            command = runner.commands[stage]
            self.assertEqual(command[command.index("--image_path") + 1], frames)
        self.assertEqual(report["photos_found"], 3)
        self.assertEqual(report["source"], str(self.video.resolve()))

    def test_photo_runs_have_no_frames_stage(self):
        _, runner = self.run_pipeline()
        self.assertNotIn("frames", runner.stages)

    def test_the_sharpest_frame_of_each_slice_is_extracted(self):
        self.run_video()
        self.assertEqual(self.picked_frames(), [2, 4, 9])

    def test_a_long_frame_list_reaches_ffmpeg_in_groups(self):
        blur_scores = [float(frame % 7 + 1) for frame in range(600)]
        self.run_video(FakeRunner(blur_scores=blur_scores), video_frames=250)
        self.assertEqual(len(self.picked_frames()), 250)
        text = (self.output / "work" / "select.txt").read_text(encoding="utf-8")
        self.assertNotRegex(text, r"\+eq\(n,\d+\)\+")

    def test_the_video_goes_to_ffmpeg_in_both_passes(self):
        _, runner = self.run_video()
        passes = [command for stage, command, _ in runner.calls if stage == "frames"]
        self.assertEqual(len(passes), 2)
        for command in passes:
            self.assertEqual(command[command.index("-i") + 1], str(self.video.resolve()))
        self.assertEqual(passes[1][-1], "frames/frame_%04d.jpg")

    def test_one_camera_is_solved_for_a_video_only(self):
        _, video_runner = self.run_video()
        self.assertIn("--ImageReader.single_camera", video_runner.commands["features"])
        _, photo_runner = self.run_pipeline(restart=True)
        self.assertNotIn("--ImageReader.single_camera", photo_runner.commands["features"])

    def test_a_short_video_uses_every_frame_and_warns(self):
        report, _ = self.run_video(FakeRunner(blur_scores=(3.0, 2.0, 1.0, 2.0)), video_frames=100)
        self.assertEqual(self.picked_frames(), [0, 1, 2, 3])
        self.assertIn("only 4 frames", report["warnings"][0])

    def test_a_video_with_too_few_frames_is_refused(self):
        with self.assertRaisesRegex(PipelineError, "only 2 frames"):
            self.run_video(FakeRunner(blur_scores=(1.0, 2.0)))

    def test_missing_frames_are_an_error(self):
        with self.assertRaisesRegex(PipelineError, "wrote 4 frames from the video instead of 5"):
            self.run_video(FakeRunner(lost_frames=1), video_frames=5)
        self.assertNotIn("frames", self.saved_report()["stages"])

    def test_too_few_frames_asked_for_are_refused(self):
        with self.assertRaisesRegex(PipelineError, "at least 3"):
            self.run_video(video_frames=2)

    def test_other_file_types_are_refused(self):
        photo = self.root / "photo.jpg"
        photo.write_bytes(b"photo")
        with self.assertRaisesRegex(PipelineError, "not a supported video type"):
            Pipeline(photo, self.output, tools=TOOLS, runner=FakeRunner()).run()

    def test_video_needs_ffmpeg_installed(self):
        tools = {name: tool for name, tool in TOOLS.items() if name != "ffmpeg"}
        with self.assertRaisesRegex(PipelineError, "python setup_tools.py"):
            self.run_video(tools=tools)

    def test_ffmpeg_failure_names_the_stage(self):
        with self.assertRaisesRegex(PipelineError, "Extract video frames failed"):
            self.run_video(FakeRunner(fail_stage="frames"))

    def test_rerun_after_a_failure_starts_the_frames_again(self):
        with self.assertRaises(PipelineError):
            self.run_video(FakeRunner(lost_frames=1), video_frames=5)
        stale = self.output / "work" / "frames" / "stale.jpg"
        stale.write_bytes(b"old")
        _, runner = self.run_video(video_frames=5)
        self.assertEqual(runner.stages[0], "frames")
        self.assertFalse(stale.exists())

    def test_finished_frames_are_not_extracted_again(self):
        with self.assertRaises(PipelineError):
            self.run_video(FakeRunner(fail_stage="densify"))
        _, runner = self.run_video()
        self.assertEqual(runner.stages, DENSIFY_ONWARD)

    def test_quality_change_keeps_the_frames(self):
        self.run_video(quality="draft")
        report, runner = self.run_video(quality="normal")
        self.assertEqual(runner.stages, DENSIFY_ONWARD)
        self.assertEqual(report["photos_found"], 3)

    def test_a_changed_frame_count_is_refused(self):
        self.run_video()
        with self.assertRaisesRegex(PipelineError, "frame count"):
            self.run_video(video_frames=4)

    def test_a_changed_video_is_refused(self):
        self.run_video()
        self.video.write_bytes(b"a longer video")
        with self.assertRaisesRegex(PipelineError, "different"):
            self.run_video()

    def test_restart_extracts_the_frames_again(self):
        self.run_video()
        _, runner = self.run_video(video_frames=4, restart=True)
        self.assertEqual(runner.stages[0], "frames")

    def test_weak_frames_get_advice_about_filming(self):
        with self.assertRaises(WeakPhotos) as caught:
            self.run_video(FakeRunner(models={"0": 10}, analyzer_stats=WEAK_STATS))
        self.assertIn("These video frames look too weak", str(caught.exception))
        self.assertIn(WEAK_VIDEO_HELP, str(caught.exception))


class ViewingAngleTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def write_images(self, poses):
        data = struct.pack("<Q", len(poses))
        for index, (rotation, translation) in enumerate(poses):
            data += struct.pack("<I7dI", index, *rotation, *translation, 1)
            data += f"photo{index}.jpg".encode() + b"\0"
            data += struct.pack("<Q", 2) + bytes(48)
        (self.folder / "images.bin").write_bytes(data)

    def write_points(self, points):
        data = struct.pack("<Q", len(points))
        for index, point in enumerate(points):
            data += struct.pack("<Q3d3BdQ", index, *point, 0, 0, 0, 0.5, 2) + bytes(16)
        (self.folder / "points3D.bin").write_bytes(data)

    def test_camera_center_undoes_the_stored_pose(self):
        half_turn = math.sqrt(0.5)
        self.write_images([((half_turn, 0, 0, half_turn), (2, -1, -3))])
        center = read_camera_centers(self.folder / "images.bin")[0]
        for value, expected in zip(center, (1, 2, 3)):
            self.assertAlmostEqual(value, expected)

    def test_widest_angle_between_two_cameras(self):
        identity = (1, 0, 0, 0)
        self.write_images([(identity, (-5, 0, 0)), (identity, (0, -5, 0)), (identity, (-5, -5, 0))])
        self.write_points([(0, 0, 0)])
        self.assertAlmostEqual(viewing_angle(self.folder), 90)

    def test_missing_files_give_no_angle(self):
        self.assertIsNone(viewing_angle(self.folder))

    def test_damaged_files_give_no_angle(self):
        (self.folder / "images.bin").write_bytes(struct.pack("<Q", 3) + b"short")
        self.write_points([(0, 0, 0)])
        self.assertIsNone(viewing_angle(self.folder))

    def test_a_model_with_no_points_gives_no_angle(self):
        self.write_images([((1, 0, 0, 0), (-5, 0, 0)), ((1, 0, 0, 0), (0, -5, 0))])
        self.write_points([])
        self.assertIsNone(viewing_angle(self.folder))


class StillShareTests(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def share(self, *args, **kwargs):
        (self.folder / "images.bin").write_bytes(images_bin(*args, **kwargs))
        return still_share(self.folder)

    def test_a_moving_point_is_not_still(self):
        self.assertEqual(self.share(10, moving=5), 0)

    def test_still_points_are_counted_among_all_points(self):
        self.assertEqual(self.share(10, still=1, moving=3), 0.25)

    def test_too_few_photos_prove_nothing(self):
        self.assertEqual(self.share(5, still=1), 0)
        self.assertEqual(self.share(6, still=1), 1)

    def test_the_photos_needed_grow_with_the_set(self):
        self.assertEqual(self.share(100, moving=4, pause=10), 0)
        self.assertEqual(self.share(100, moving=4, pause=11), 1)

    def test_a_small_shift_still_counts_as_still(self):
        self.assertEqual(self.share(6, moving=1, shift=0.2), 1)

    def test_a_steady_drift_does_not_count_as_still(self):
        self.assertEqual(self.share(6, moving=1, shift=2), 0)

    def test_a_missing_file_gives_no_share(self):
        self.assertIsNone(still_share(self.folder))

    def test_a_damaged_file_gives_no_share(self):
        (self.folder / "images.bin").write_bytes(struct.pack("<Q", 3) + b"short")
        self.assertIsNone(still_share(self.folder))

    def test_a_model_with_no_points_gives_no_share(self):
        self.assertIsNone(self.share(3))


class ParsingTests(unittest.TestCase):
    def test_model_stats_from_real_log_lines(self):
        output = (
            "I20261101 15:26:31.699596 37088 model.cc:440] Registered images: 11\n"
            "I20261101 15:26:31.700171 37088 model.cc:442] Points: 7968\n"
            "I20261101 15:26:31.700179 37088 model.cc:443] Observations: 38355\n"
            "I20261101 15:26:31.700192 37088 model.cc:445] Mean track length: 4.813630\n"
            "I20261101 15:26:31.700206 37088 model.cc:447] Mean observations per image: 3486.818182\n"
            "I20261101 15:26:31.700216 37088 model.cc:450] Mean reprojection error: 0.537712px\n"
        )
        self.assertEqual(
            parse_model_stats(output),
            {"points": 7968, "track_length": 4.81363, "error": 0.537712},
        )

    def test_missing_model_stats_are_left_out(self):
        self.assertEqual(parse_model_stats("Registered images: 5"), {})

    def test_registered_images_from_a_real_log_line(self):
        line = "I20261101 15:26:31.700163 37088 model.cc:440] Registered images: 11"
        self.assertEqual(parse_registered_images(line), 11)

    def test_registered_images_missing(self):
        self.assertEqual(parse_registered_images("nothing useful"), 0)

    def test_blur_scores_from_real_ffmpeg_lines(self):
        text = (
            "frame:0    pts:0       pts_time:0\n"
            "lavfi.blur=5.290438\n"
            "frame:1    pts:1       pts_time:0.1\n"
            "lavfi.blur=13.103181\n"
        )
        self.assertEqual(parse_blur_scores(text), {0: 5.290438, 1: 13.103181})

    def test_a_black_frame_ranks_last(self):
        text = "frame:0    pts:0       pts_time:0\nlavfi.blur=nan\n"
        self.assertEqual(parse_blur_scores(text), {0: math.inf})

    def test_no_blur_scores(self):
        self.assertEqual(parse_blur_scores(""), {})

    def test_sharpest_frame_is_picked_from_each_equal_slice(self):
        scores = dict(enumerate([3, 1, 2, 9, 8, 7, 4, 6, 5]))
        self.assertEqual(pick_sharpest(scores, 3), [1, 5, 6])

    def test_slices_cover_every_frame_when_they_do_not_divide_evenly(self):
        scores = dict(enumerate([9, 9, 9, 9, 9, 9, 9, 1]))
        self.assertEqual(pick_sharpest(scores, 3)[-1], 7)

    def test_a_frame_with_no_score_is_only_picked_if_its_slice_has_nothing_else(self):
        scores = {0: math.inf, 1: 5.0, 2: math.inf, 3: math.inf}
        self.assertEqual(pick_sharpest(scores, 2), [1, 2])

    def test_every_frame_is_used_when_too_few_exist(self):
        self.assertEqual(pick_sharpest({0: 1.0, 1: 2.0}, 100), [0, 1])

    def test_picking_from_no_frames_gives_nothing(self):
        self.assertEqual(pick_sharpest({}, 10), [])

    def test_the_expression_selects_only_the_picked_frames(self):
        frames = [2, 7, 8, 40, 41]
        expression = select_expression(frames)
        for n in range(50):
            selected = eval(expression, {"eq": lambda a, b: a == b, "n": n})
            self.assertEqual(bool(selected), n in frames)

    def test_one_frame_needs_no_group(self):
        self.assertEqual(select_expression([7]), "eq(n,7)")

    def test_long_frame_lists_avoid_ffmpegs_limit_on_chains(self):
        expression = select_expression(list(range(1000)))
        self.assertEqual(len(re.findall(r"eq\(n,\d+\)", expression)), 1000)
        self.assertNotRegex(expression, r"\+eq\(n,\d+\)\+")
        depth = deepest = 0
        for character in expression:
            depth += {"(": 1, ")": -1}.get(character, 0)
            deepest = max(deepest, depth)
        self.assertLess(deepest, 15)

    def test_durations_read_like_a_clock(self):
        self.assertEqual(format_duration(0), "0:00")
        self.assertEqual(format_duration(8.9), "0:08")
        self.assertEqual(format_duration(228), "3:48")
        self.assertEqual(format_duration(3725), "1:02:05")


if __name__ == "__main__":
    unittest.main()
