import json
import tempfile
import unittest
from pathlib import Path

from pipeline import (
    NO_PLACEMENT_HELP,
    Pipeline,
    PipelineError,
    ToolResult,
    parse_gpu_name,
    parse_registered_images,
)

TOOLS = {
    "colmap": {"version": "1", "bin": "colmap_bin"},
    "openmvs": {"version": "2", "bin": "openmvs_bin"},
}
GPU_TOOLS = {
    **TOOLS,
    "openmvs_gpu": {"version": "2", "bin": "openmvs_gpu_bin"},
    "zluda": {"version": "3", "exe": "zluda.exe"},
}
ALL_BEFORE_DENSIFY = ["features", "matching", "mapping", "select_model", "undistort", "import"]
DENSIFY_ONWARD = ["densify", "mesh", "texture"]
GPU_LOG = "CUDA device 0 initialized: AMD Radeon RX 7800 XT (compute capability 8.8; memory 16GB)"


class FakeRunner:
    """Stands in for ToolRunner: records commands and creates the files tools would."""

    def __init__(self, models=None, fail_stage=None, densify_output="", mapper_exit=0):
        self.models = {"0": 5} if models is None else models
        self.fail_stage = fail_stage
        self.densify_output = densify_output
        self.mapper_exit = mapper_exit
        self.stages = []
        self.commands = {}
        self.output_dir = None

    def run(self, stage, command, tool_log=None):
        command = [str(part) for part in command]
        self.stages.append(stage)
        self.commands[stage] = command
        if stage == self.fail_stage:
            return ToolResult(3, "first line\nsecond line")
        if stage == "mapping":
            for name in self.models:
                (self.output_dir / "work" / "sparse" / name).mkdir()
            return ToolResult(self.mapper_exit, "")
        if stage == "select_model":
            name = command[-1].split("/")[-1]
            return ToolResult(0, f"Registered images: {self.models[name]}")
        if stage == "densify":
            return ToolResult(0, self.densify_output)
        if stage == "texture":
            (self.output_dir / "model" / "model.obj").write_text("v 0 0 0\n")
        return ToolResult(0, "")


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
            def run(self, stage, command, tool_log=None):
                if stage == "texture":
                    self.stages.append(stage)
                    return ToolResult(0, "")
                return super().run(stage, command, tool_log)

        with self.assertRaisesRegex(PipelineError, "missing or empty"):
            self.run_pipeline(NoModelRunner())


class GpuTests(PipelineTestCase):
    def test_gpu_mode_needs_its_tools_installed(self):
        with self.assertRaisesRegex(PipelineError, "setup_tools.py --gpu"):
            self.run_pipeline(gpu=True)

    def test_gpu_stages_run_through_the_launcher(self):
        report, runner = self.run_pipeline(
            FakeRunner(densify_output=GPU_LOG), gpu=True, tools=GPU_TOOLS, quality="high"
        )
        for stage in ("densify", "refine"):
            command = runner.commands[stage]
            self.assertEqual(command[:2], ["zluda.exe", "--"])
            self.assertIn("openmvs_gpu_bin", command[2])
            self.assertEqual(command[-2:], ["--cuda-device", "-1"])
        self.assertIn("openmvs_bin", runner.commands["mesh"][0])
        self.assertIn("colmap_bin", runner.commands["features"][0])
        self.assertEqual(report["dense_device"], "AMD Radeon RX 7800 XT")
        self.assertEqual(report["warnings"], [])

    def test_cpu_fallback_is_reported(self):
        report, _ = self.run_pipeline(FakeRunner(), gpu=True, tools=GPU_TOOLS)
        self.assertEqual(report["dense_device"], "CPU")
        self.assertIn("ran on the CPU", report["warnings"][0])

    def test_cpu_mode_does_not_use_the_launcher(self):
        report, runner = self.run_pipeline(FakeRunner())
        self.assertIn("openmvs_bin", runner.commands["densify"][0])
        self.assertEqual(report["dense_device"], "CPU")

    def test_switching_to_gpu_reruns_the_dense_stages(self):
        self.run_pipeline(tools=GPU_TOOLS)
        _, runner = self.run_pipeline(FakeRunner(densify_output=GPU_LOG), gpu=True, tools=GPU_TOOLS)
        self.assertEqual(runner.stages, DENSIFY_ONWARD)


class ParsingTests(unittest.TestCase):
    def test_registered_images_from_a_real_log_line(self):
        line = "I20261101 15:26:31.700163 37088 model.cc:440] Registered images: 11"
        self.assertEqual(parse_registered_images(line), 11)

    def test_registered_images_missing(self):
        self.assertEqual(parse_registered_images("nothing useful"), 0)

    def test_gpu_name(self):
        self.assertEqual(parse_gpu_name(GPU_LOG), "AMD Radeon RX 7800 XT")
        self.assertIsNone(parse_gpu_name("no device line"))


if __name__ == "__main__":
    unittest.main()
