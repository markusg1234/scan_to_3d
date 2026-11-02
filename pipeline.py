"""Turn a folder of photos or a video into a textured 3D model using COLMAP and OpenMVS."""

import hashlib
import itertools
import json
import math
import os
import re
import shutil
import statistics
import struct
import subprocess
import threading
import time
from collections import Counter, namedtuple
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent
TOOLS_JSON = ROOT / "tools.json"
REPORT_NAME = "run_report.json"

PHOTO_EXTENSIONS = {".jpg", ".jpeg", ".png", ".tif", ".tiff"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".m4v", ".mkv", ".avi", ".webm"}
MIN_PHOTOS = 3
DEFAULT_VIDEO_FRAMES = 100
BLUR_SCORING_WIDTH = 640
LOW_PLACEMENT_RATIO = 0.8
LOG_TAIL_LINES = 20
POLL_SECONDS = 0.25

# OpenMVS's viewer sometimes crashes in AMD's OpenGL driver while it loads a model. Every
# crash seen came within 2 seconds of starting, and opening it again worked.
VIEWER_START_SECONDS = 5
VIEWER_ATTEMPTS = 3

# COLMAP's numbers for its camera models without lens distortion.
SIMPLE_PINHOLE = 0
PINHOLE = 1

# Real size checks. A clicked point should land within a few pixels of where the other photos
# put it, and the photos need to see it from well apart, or its depth is uncertain.
MAX_CLICK_ERROR = 3
MIN_CLICK_ANGLE = 10
# Photos measured on the surface should agree. Flat spots agreed within about 2% in testing,
# while corners of a small box, where the surface was rough, were up to 75% out.
MAX_SURFACE_SPREAD = 0.03
# A spot on an edge or corner can slip past the model's slightly rounded edge onto what's
# behind it, which put clicks on a box's corners 6 to 15 cm out. The surface either side of a
# click shows it: flat spots on a ruler bent at most 1.2% over 8 pixels, and every click that
# slipped bent 7% or more.
EDGE_CHECK_PIXELS = 8
MAX_SURFACE_BEND = 0.03

# 80 bytes of free text. They must not start with "solid", which marks a text STL.
STL_HEADER = b"photo_to_3d".ljust(80, b"\0")
# Each triangle is stored as its normal, its three corners and an unused attribute.
STL_TRIANGLE = struct.Struct("<12fH")

# Photo check limits. They sit between the good castle sample and one weak set of
# room photos, so they may need tuning as more photo sets are tried.
MIN_POINTS_PER_PHOTO = 300
MIN_TRACK_LENGTH = 3.5
MAX_REPROJECTION_ERROR = 1.0
MIN_VIEWING_ANGLE = 45
WEAK_SIGNS_TO_STOP = 2

# A point is still if enough photos show it within a few pixels of one spot. Good sets
# had about 0.1% of these at most, and rotating table sets with the background in view had
# 7% or more. A pause in a video stills every point for a moment, so the count grows with
# the set.
STILL_PIXELS = 3
MIN_STILL_PHOTOS = 6
STILL_PHOTOS_DIVISOR = 8
MAX_STILL_SHARE = 0.03

NO_PLACEMENT_HELP = (
    "COLMAP could not work out where the photos were taken from. The usual causes "
    "are too little overlap between photos, or plain or shiny surfaces."
)
WEAK_PHOTOS_HELP = (
    "Walk around one matte, textured object, keeping it in view, and take more "
    "overlapping photos."
)
WEAK_VIDEO_HELP = (
    "Film a slow, full circle around one matte, textured object, keeping it in view."
)
STILL_BACKGROUND_HELP = (
    "Part of the picture stayed still between photos, so COLMAP can't tell how the camera "
    "moved. On a rotating table, hide everything that doesn't turn behind a plain backdrop."
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
    "frames": "Extract video frames",
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
    "stl": "Save STL",
}

# Stages whose results depend on the quality and GPU settings.
SETTINGS_STAGES = ("densify", "mesh", "refine", "texture", "stl")

ToolResult = namedtuple("ToolResult", "exit_code output")


class PipelineError(Exception):
    """A problem to show to the person running the program."""


class Cancelled(PipelineError):
    def __init__(self):
        super().__init__("Cancelled. Run again to continue from where it stopped.")


class WeakPhotos(PipelineError):
    """The cameras solved, but too weakly to expect a good model."""


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


def view_model(output_dir, tools=None):
    """Open a finished model in OpenMVS's viewer, reopening it if it crashes as it starts."""
    output_dir = Path(output_dir).resolve()
    model_dir = output_dir / "model"
    if not (model_dir / "model.obj").exists():
        raise PipelineError(f"There's no model in {output_dir} yet.")
    tools = tools or load_tools()
    viewer = Path(tools["openmvs"]["bin"]) / "Viewer.exe"
    for _ in range(VIEWER_ATTEMPTS):
        # The viewer looks for the texture in its working folder, not beside the model.
        process = subprocess.Popen([viewer, "-i", "model.obj"], cwd=model_dir)
        try:
            if process.wait(VIEWER_START_SECONDS) == 0:
                return  # Closed already, but not by a crash.
        except subprocess.TimeoutExpired:
            return  # Still open, so it started.
    raise PipelineError(
        f"The viewer crashed {VIEWER_ATTEMPTS} times while opening the model. Try again."
    )


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


def video_signature(video, frame_count):
    """Identify a video run by the file's name and size and the number of frames."""
    text = f"{video.name}|{video.stat().st_size}|{frame_count}\n"
    return hashlib.sha256(text.encode()).hexdigest()


def parse_blur_scores(text):
    """Read each frame's blur score from ffmpeg. Lower scores are sharper."""
    scores = {}
    for frame, score in re.findall(r"frame:(\d+).*\nlavfi\.blur=(\S+)", text):
        # A frame with no edges, such as a black one, scores nan.
        scores[int(frame)] = math.inf if math.isnan(float(score)) else float(score)
    return scores


def pick_sharpest(scores, count):
    """Split the frames into equal slices and return the sharpest frame of each."""
    frames = sorted(scores)
    slices = min(count, len(frames))
    return [
        min(
            frames[part * len(frames) // slices : (part + 1) * len(frames) // slices],
            key=scores.get,
        )
        for part in range(slices)
    ]


def select_expression(frames):
    """Build an ffmpeg expression that is true only for these frames."""
    # ffmpeg 9.0.2 refuses a plain chain of over 100 terms, but nested pairs have no limit.
    if len(frames) == 1:
        return f"eq(n,{frames[0]})"
    middle = len(frames) // 2
    return f"({select_expression(frames[:middle])}+{select_expression(frames[middle:])})"


def format_duration(seconds):
    """Format a duration like a clock: 0:08, 3:48 or 1:02:05."""
    minutes, seconds = divmod(int(seconds), 60)
    hours, minutes = divmod(minutes, 60)
    return f"{hours}:{minutes:02}:{seconds:02}" if hours else f"{minutes}:{seconds:02}"


def parse_registered_images(analyzer_output):
    match = re.search(r"Registered images:\s*(\d+)", analyzer_output)
    return int(match.group(1)) if match else 0


def parse_model_stats(analyzer_output):
    """Read the numbers the photo check needs, leaving out any that are missing."""
    patterns = {
        "points": r"\bPoints:\s*(\d+)",
        "track_length": r"Mean track length:\s*([\d.]+)",
        "error": r"Mean reprojection error:\s*([\d.]+)",
    }
    stats = {}
    for name, pattern in patterns.items():
        match = re.search(pattern, analyzer_output)
        if match:
            stats[name] = float(match.group(1))
    return stats


def read_poses(path):
    """Read each photo's camera number and world-to-camera rotation and translation."""
    poses = {}
    with path.open("rb") as file:
        count = struct.unpack("<Q", file.read(8))[0]
        for _ in range(count):
            _, w, x, y, z, *translation, camera = struct.unpack("<I7dI", file.read(64))
            name = bytearray()
            while (byte := file.read(1)) not in (b"\0", b""):
                name += byte
            observations = struct.unpack("<Q", file.read(8))[0]
            file.seek(observations * 24, 1)
            rotation = (
                (1 - 2 * (y * y + z * z), 2 * (x * y - z * w), 2 * (x * z + y * w)),
                (2 * (x * y + z * w), 1 - 2 * (x * x + z * z), 2 * (y * z - x * w)),
                (2 * (x * z - y * w), 2 * (y * z + x * w), 1 - 2 * (x * x + y * y)),
            )
            poses[name.decode()] = (camera, rotation, translation)
    return poses


def camera_center(rotation, translation):
    """The files store the world-to-camera pose, so the center is -R^T * t."""
    return tuple(-sum(rotation[row][i] * translation[row] for row in range(3)) for i in range(3))


def read_camera_centers(path):
    """Read camera positions from COLMAP's images.bin."""
    poses = read_poses(path).values()
    return [camera_center(rotation, translation) for _, rotation, translation in poses]


def read_cameras(path):
    """Read each camera's focal lengths and center from COLMAP's cameras.bin."""
    cameras = {}
    with path.open("rb") as file:
        count = struct.unpack("<Q", file.read(8))[0]
        for _ in range(count):
            camera, model, _, _ = struct.unpack("<IiQQ", file.read(24))
            if model == SIMPLE_PINHOLE:
                focal, cx, cy = struct.unpack("<3d", file.read(24))
                cameras[camera] = (focal, focal, cx, cy)
            elif model == PINHOLE:
                cameras[camera] = struct.unpack("<4d", file.read(32))
            else:
                # Undistorted models only use the two above, so this file is the wrong one.
                raise PipelineError(f"The camera in {path} has lens distortion left in it.")
    return cameras


def read_point_positions(path):
    """Read point positions from COLMAP's points3D.bin."""
    points = []
    with path.open("rb") as file:
        count = struct.unpack("<Q", file.read(8))[0]
        for _ in range(count):
            _, x, y, z, _, _, _, _, track_length = struct.unpack("<Q3d3BdQ", file.read(51))
            points.append((x, y, z))
            file.seek(track_length * 8, 1)
    return points


def read_point_tracks(path):
    """Read how many photos COLMAP placed and where each point appears in them."""
    tracks = {}
    with path.open("rb") as file:
        count = struct.unpack("<Q", file.read(8))[0]
        for _ in range(count):
            file.seek(64, 1)
            while file.read(1) not in (b"\0", b""):  # Skip the image name.
                pass
            observations = struct.unpack("<Q", file.read(8))[0]
            for x, y, point in struct.iter_unpack("<ddq", file.read(observations * 24)):
                if point != -1:
                    tracks.setdefault(point, []).append((x, y))
    return count, tracks


def still_share(sparse_dir):
    """Return the share of points stuck to one spot in the photos, or None if unknown."""
    try:
        photos, tracks = read_point_tracks(sparse_dir / "images.bin")
    except (OSError, struct.error):
        return None
    if not tracks:
        return None
    needed = max(MIN_STILL_PHOTOS, photos // STILL_PHOTOS_DIVISOR)
    still = 0
    for track in tracks.values():
        spots = Counter((round(x / STILL_PIXELS), round(y / STILL_PIXELS)) for x, y in track)
        still += max(spots.values()) >= needed
    return still / len(tracks)


def viewing_angle(sparse_dir):
    """Return the widest angle between two cameras around the scene, or None if unknown."""
    try:
        cameras = read_camera_centers(sparse_dir / "images.bin")
        points = read_point_positions(sparse_dir / "points3D.bin")
    except (OSError, struct.error):
        return None
    if not points:
        return None
    target = [statistics.median(axis) for axis in zip(*points)]
    directions = [[value - middle for value, middle in zip(camera, target)] for camera in cameras]
    widest = None
    for first, second in itertools.combinations(directions, 2):
        lengths = math.hypot(*first) * math.hypot(*second)
        if lengths == 0:
            continue
        cosine = sum(a * b for a, b in zip(first, second)) / lengths
        angle = math.degrees(math.acos(max(-1.0, min(1.0, cosine))))
        widest = angle if widest is None else max(widest, angle)
    return widest


def photo_problems(stats, angle, placed, still=None):
    """Return a sentence for each sign of a weak photo set, skipping unknown numbers."""
    problems = []
    if "points" in stats and placed:
        per_photo = stats["points"] / placed
        if per_photo < MIN_POINTS_PER_PHOTO:
            problems.append(
                f"Only {per_photo:.0f} matched points per photo (aim for "
                f"{MIN_POINTS_PER_PHOTO} or more)."
            )
    if stats.get("track_length", MIN_TRACK_LENGTH) < MIN_TRACK_LENGTH:
        problems.append(
            f"Each point appears in only {stats['track_length']:.1f} photos (aim for "
            f"{MIN_TRACK_LENGTH} or more)."
        )
    if stats.get("error", 0) > MAX_REPROJECTION_ERROR:
        problems.append(
            f"Points are {stats['error']:.1f} pixels out of place (aim for "
            f"{MAX_REPROJECTION_ERROR:.0f} or less)."
        )
    if angle is not None and angle < MIN_VIEWING_ANGLE:
        problems.append(
            f"The photos only cover {angle:.0f} degrees around the subject (aim for "
            f"{MIN_VIEWING_ANGLE} or more)."
        )
    if still is not None and still > MAX_STILL_SHARE:
        problems.append(
            f"{still:.0%} of the points stay still in the picture (aim for "
            f"{MAX_STILL_SHARE:.0%} or less)."
        )
    return problems


def read_obj(path):
    """Read an OBJ model's corner positions, and its faces as triangles of corner numbers."""
    vertices, faces = [], []
    with path.open("rb") as file:
        for line in file:
            if line.startswith(b"v "):
                vertices.append(tuple(map(float, line.split()[1:4])))
            elif line.startswith(b"f "):
                # A corner can also carry texture and normal numbers, as in 4/1 or 4/1/2.
                # Negative numbers count back from the last vertex read.
                numbers = [int(part.partition(b"/")[0]) for part in line.split()[1:]]
                corners = [
                    number - 1 if number > 0 else len(vertices) + number for number in numbers
                ]
                for second, third in zip(corners[1:], corners[2:]):
                    faces.append((corners[0], second, third))
    return vertices, faces


def read_obj_triangles(path):
    """Read an OBJ model's faces as triangles of corner positions, splitting larger faces."""
    vertices, faces = read_obj(path)
    return [(vertices[a], vertices[b], vertices[c]) for a, b, c in faces]


def facet_normal(a, b, c):
    """Return a triangle's unit normal by the right-hand rule, or zeros if it has no area."""
    (ax, ay, az), (bx, by, bz), (cx, cy, cz) = a, b, c
    ux, uy, uz = bx - ax, by - ay, bz - az
    vx, vy, vz = cx - ax, cy - ay, cz - az
    normal = (uy * vz - uz * vy, uz * vx - ux * vz, ux * vy - uy * vx)
    length = math.hypot(*normal) or 1  # A triangle with no area keeps its zero normal.
    return [value / length for value in normal]


def write_stl(path, triangles, scale=1):
    """Write triangles as a binary STL, the file type 3D printing software reads."""
    with path.open("wb") as file:
        file.write(STL_HEADER + struct.pack("<I", len(triangles)))
        for a, b, c in triangles:
            corners = [value * scale for corner in (a, b, c) for value in corner]
            file.write(STL_TRIANGLE.pack(*facet_normal(a, b, c), *corners, 0))


def write_report(path, report):
    """Save the run report whole, so stopping halfway can't leave half a file."""
    temporary = path.with_suffix(".tmp")
    temporary.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    temporary.replace(path)


Measurement = namedtuple("Measurement", "distance worst_error narrowest_angle")
RealSize = namedtuple("RealSize", "mm_per_unit size_mm photo_mm_per_unit spread edge_spots")


def click_ray(camera, pose, click):
    """Return where a photo was taken from and the unit direction a pixel in it looks along."""
    fx, fy, cx, cy = camera
    _, rotation, translation = pose
    local = ((click[0] - cx) / fx, (click[1] - cy) / fy, 1)
    direction = [sum(rotation[row][i] * local[row] for row in range(3)) for i in range(3)]
    length = math.hypot(*direction)
    return camera_center(rotation, translation), [value / length for value in direction]


def determinant(matrix):
    (a, b, c), (d, e, f), (g, h, i) = matrix
    return a * (e * i - f * h) - b * (d * i - f * g) + c * (d * h - e * g)


def triangulate(rays):
    """Return the point nearest to every ray, or None if the rays are parallel."""
    matrix = [[0.0] * 3 for _ in range(3)]
    vector = [0.0] * 3
    for start, direction in rays:
        for i in range(3):
            for j in range(3):
                weight = (i == j) - direction[i] * direction[j]
                matrix[i][j] += weight
                vector[i] += weight * start[j]
    whole = determinant(matrix)
    if abs(whole) < 1e-12:
        return None
    # Cramer's rule: each coordinate swaps one column of the matrix for the vector.
    return [
        determinant([row[:i] + [value] + row[i + 1:] for row, value in zip(matrix, vector)]) / whole
        for i in range(3)
    ]


def reprojection_error(camera, pose, point, click):
    """Return how many pixels a 3D point lands from where it was clicked in a photo."""
    fx, fy, cx, cy = camera
    _, rotation, translation = pose
    x, y, z = (sum(rotation[i][j] * point[j] for j in range(3)) + translation[i] for i in range(3))
    if z <= 0:
        return math.inf  # Behind the camera.
    return math.hypot(fx * x / z + cx - click[0], fy * y / z + cy - click[1])


def widest_angle(rays):
    """Return the widest angle in degrees between any two of the rays."""
    widest = 0.0
    for (_, first), (_, second) in itertools.combinations(rays, 2):
        cosine = sum(a * b for a, b in zip(first, second))
        widest = max(widest, math.degrees(math.acos(max(-1.0, min(1.0, cosine)))))
    return widest


def read_dense_model(dense_dir):
    """Read the undistorted cameras and poses that the clicked photos belong to."""
    try:
        return (
            read_cameras(dense_dir / "sparse" / "cameras.bin"),
            read_poses(dense_dir / "sparse" / "images.bin"),
        )
    except (OSError, struct.error):
        raise PipelineError(
            f"Setting the real size needs the working files in {dense_dir.parent}, and they're "
            "missing. Run the photos again to make them."
        ) from None


def measure_length(dense_dir, clicks):
    """Find how far apart two points are in the model, from where they were clicked.

    clicks maps photo names to [point 1, point 2], each an (x, y) pixel position or None.
    COLMAP puts the center of a photo's top-left pixel at (0.5, 0.5).
    """
    cameras, poses = read_dense_model(dense_dir)
    points, errors, angles = [], [], []
    for number in (0, 1):
        views = [
            (cameras[poses[name][0]], poses[name], points_clicked[number])
            for name, points_clicked in clicks.items()
            if points_clicked[number] is not None
        ]
        if len(views) < 2:
            raise PipelineError(
                f"Point {number + 1} has been clicked in {len(views)} "
                f"photo{'' if len(views) == 1 else 's'}, and it needs at least 2."
            )
        rays = [click_ray(camera, pose, click) for camera, pose, click in views]
        point = triangulate(rays)
        if point is None:
            raise PipelineError(f"Every photo sees point {number + 1} from the same direction.")
        points.append(point)
        angles.append(widest_angle(rays))
        errors += [reprojection_error(camera, pose, point, click) for camera, pose, click in views]
    distance = math.dist(*points)
    if distance == 0:
        raise PipelineError("Points 1 and 2 are the same spot.")
    return Measurement(distance, max(errors), min(angles))


def length_problems(measurement):
    """Return a sentence for each sign that the clicks may give the wrong size."""
    problems = []
    if measurement.worst_error > MAX_CLICK_ERROR:
        problems.append(
            f"A click is {measurement.worst_error:.1f} pixels from where the other photos put "
            f"that point (aim for {MAX_CLICK_ERROR} or less). Check the same spots were clicked."
        )
    if measurement.narrowest_angle < MIN_CLICK_ANGLE:
        problems.append(
            f"The photos see a point from only {measurement.narrowest_angle:.0f} degrees apart "
            f"(aim for {MIN_CLICK_ANGLE} or more). Use photos taken further around."
        )
    return problems


def surface_problems(real_size):
    """Return a sentence for each sign that sizes found on the surface may be wrong."""
    problems = []
    if real_size.edge_spots:
        spots = real_size.edge_spots
        listed = ", ".join(spots[:-1]) + " and " + spots[-1] if len(spots) > 1 else spots[0]
        problems.append(
            f"{listed[0].upper()}{listed[1:]} {'is' if len(spots) == 1 else 'are'} on an edge or "
            "corner, where the line of sight can slip past the model onto what's behind it. Click "
            "a flat area instead, or use the same spots in several photos."
        )
    if len(real_size.photo_mm_per_unit) < 2:
        problems.append(
            "Only one photo was measured, so nothing checks it. Measure in a second photo too, "
            "so a spot the surface got wrong shows up."
        )
    elif real_size.spread > MAX_SURFACE_SPREAD:
        problems.append(
            f"The photos disagree by {real_size.spread:.0%} (aim for {MAX_SURFACE_SPREAD:.0%} or "
            "less). A spot on a corner or edge, or a wrong length, is the usual cause. Check "
            "each photo, or use the same spots in several photos instead."
        )
    return problems


def project_vertices(vertices, camera, pose):
    """Return each corner's pixel column, row and depth in one photo. Depth 0 or less is behind."""
    fx, fy, cx, cy = camera
    _, ((r00, r01, r02), (r10, r11, r12), (r20, r21, r22)), (tx, ty, tz) = pose
    columns, rows, depths = [], [], []
    for x, y, z in vertices:
        depth = r20 * x + r21 * y + r22 * z + tz
        scale = 1 / depth if depth > 0 else 0.0
        columns.append(fx * (r00 * x + r01 * y + r02 * z + tx) * scale + cx)
        rows.append(fy * (r10 * x + r11 * y + r12 * z + ty) * scale + cy)
        depths.append(depth)
    return columns, rows, depths


def ray_triangle(start, direction, a, b, c):
    """Return how far along a ray it meets a triangle, or None if it misses (Moller-Trumbore)."""
    e1 = [b[i] - a[i] for i in range(3)]
    e2 = [c[i] - a[i] for i in range(3)]
    p = (
        direction[1] * e2[2] - direction[2] * e2[1],
        direction[2] * e2[0] - direction[0] * e2[2],
        direction[0] * e2[1] - direction[1] * e2[0],
    )
    whole = e1[0] * p[0] + e1[1] * p[1] + e1[2] * p[2]
    if abs(whole) < 1e-15:
        return None  # The ray runs along the triangle's plane.
    s = [start[i] - a[i] for i in range(3)]
    u = (s[0] * p[0] + s[1] * p[1] + s[2] * p[2]) / whole
    if u < 0 or u > 1:
        return None
    q = (s[1] * e1[2] - s[2] * e1[1], s[2] * e1[0] - s[0] * e1[2], s[0] * e1[1] - s[1] * e1[0])
    v = (direction[0] * q[0] + direction[1] * q[1] + direction[2] * q[2]) / whole
    if v < 0 or u + v > 1:
        return None
    distance = (e2[0] * q[0] + e2[1] * q[1] + e2[2] * q[2]) / whole
    return distance if distance > 0 else None


def surface_point(vertices, faces, projection, camera, pose, click):
    """Return where the line of sight through a clicked pixel first meets the model, or None."""
    columns, rows, depths = projection
    u, v = click
    start, direction = click_ray(camera, pose, click)
    nearest = math.inf
    for a, b, c in faces:
        # Most faces are nowhere near the click in the photo, so rule those out cheaply first.
        ua, ub, uc = columns[a], columns[b], columns[c]
        if (u < ua and u < ub and u < uc) or (u > ua and u > ub and u > uc):
            continue
        va, vb, vc = rows[a], rows[b], rows[c]
        if (v < va and v < vb and v < vc) or (v > va and v > vb and v > vc):
            continue
        if depths[a] <= 0 or depths[b] <= 0 or depths[c] <= 0:
            continue
        distance = ray_triangle(start, direction, vertices[a], vertices[b], vertices[c])
        if distance is not None and distance < nearest:
            nearest = distance
    if nearest == math.inf:
        return None
    return [start[i] + nearest * direction[i] for i in range(3)]


def surface_bend(vertices, faces, projection, camera, pose, click, point):
    """Return how far from flat the surface is around a click, relative to its distance.

    On a flat area, even one seen at a slant, the surface on either side of the click is as far
    in front as behind. Across an edge it isn't.
    """
    start = click_ray(camera, pose, click)[0]
    centre = math.dist(start, point)
    bend = 0.0
    for dx, dy in ((1, 0), (0, 1), (1, 1), (1, -1)):
        step = EDGE_CHECK_PIXELS / math.hypot(dx, dy)
        sides = []
        for sign in (1, -1):
            beside = [click[0] + sign * dx * step, click[1] + sign * dy * step]
            found = surface_point(vertices, faces, projection, camera, pose, beside)
            if found is None:
                return math.inf  # At the edge of the model itself.
            sides.append(math.dist(start, found))
        bend = max(bend, abs(sides[0] + sides[1] - 2 * centre) / centre)
    return bend


def finished_model(output_dir):
    """Return a finished run's folder, model and report, or explain that there isn't one."""
    output_dir = Path(output_dir).resolve()
    model, report = output_dir / "model" / "model.obj", output_dir / REPORT_NAME
    if not model.exists() or not report.exists():
        raise PipelineError(f"There's no model in {output_dir} yet.")
    return output_dir, model, report


def save_real_size(report_path, model, vertices, faces, real_size):
    """Rewrite model.stl at the real size, remember how it was found, and return the size."""
    scale = real_size["mm_per_unit"]
    triangles = [(vertices[a], vertices[b], vertices[c]) for a, b, c in faces]
    write_stl(model.with_suffix(".stl"), triangles, scale)
    report = json.loads(report_path.read_text(encoding="utf-8"))
    report["real_size"] = real_size
    write_report(report_path, report)
    return [(max(axis) - min(axis)) * scale for axis in zip(*vertices)]


def set_real_size(output_dir, clicks, length_mm):
    """Save model.stl in millimetres, from the same two spots clicked in several photos."""
    output_dir, model, report_path = finished_model(output_dir)
    if length_mm <= 0:
        raise PipelineError("The length must be more than 0 mm.")
    measurement = measure_length(output_dir / "work" / "dense", clicks)
    mm_per_unit = length_mm / measurement.distance
    vertices, faces = read_obj(model)
    size = save_real_size(report_path, model, vertices, faces, {
        "method": "same_spots",
        "length_mm": length_mm,
        "mm_per_unit": mm_per_unit,
        "worst_click_error": round(measurement.worst_error, 2),
        "narrowest_angle": round(measurement.narrowest_angle, 1),
        "clicks": clicks,
    })
    return RealSize(mm_per_unit, size, {}, None, [])


def set_real_size_on_surface(output_dir, clicks, lengths_mm):
    """Save model.stl in millimetres, from two spots clicked in each of one or more photos.

    Each spot is found where its line of sight meets the model, so every photo can have its
    own pair and its own known length, and no spot has to be found again in another photo.
    """
    output_dir, model, report_path = finished_model(output_dir)
    cameras, poses = read_dense_model(output_dir / "work" / "dense")
    pairs = {name: points for name, points in clicks.items() if any(points)}
    if not pairs:
        raise PipelineError("Click two spots in a photo, and type the real length between them.")
    for name, points in pairs.items():
        if not all(points):
            raise PipelineError(f"{name} has only one point. Click the other, or clear the photo.")
        if lengths_mm.get(name, 0) <= 0:
            raise PipelineError(f"Type the real length between the two points in {name}.")
    vertices, faces = read_obj(model)
    distances, edge_spots = {}, []
    for name, points in pairs.items():
        camera, pose = cameras[poses[name][0]], poses[name]
        projection = project_vertices(vertices, camera, pose)
        found = []
        for number, click in enumerate(points):
            point = surface_point(vertices, faces, projection, camera, pose, click)
            if point is None:
                raise PipelineError(
                    f"Point {number + 1} in {name} isn't on the model. Click a spot on the object "
                    "or the table, not the background."
                )
            found.append(point)
            bend = surface_bend(vertices, faces, projection, camera, pose, click, point)
            if bend > MAX_SURFACE_BEND:
                edge_spots.append(f"point {number + 1} in {name}")
        distances[name] = math.dist(*found)
        if distances[name] == 0:
            raise PipelineError(f"Points 1 and 2 in {name} are the same spot.")
    photo_mm_per_unit = {name: lengths_mm[name] / distances[name] for name in pairs}
    # Longer lengths count for more, because the surface's errors are a fixed size.
    mm_per_unit = sum(lengths_mm[name] for name in pairs) / sum(distances.values())
    spread = (max(photo_mm_per_unit.values()) - min(photo_mm_per_unit.values())) / mm_per_unit
    size = save_real_size(report_path, model, vertices, faces, {
        "method": "surface",
        "mm_per_unit": mm_per_unit,
        "lengths_mm": {name: lengths_mm[name] for name in pairs},
        "photo_mm_per_unit": photo_mm_per_unit,
        "spread": spread,
        "edge_spots": edge_spots,
        "clicks": pairs,
    })
    return RealSize(mm_per_unit, size, photo_mm_per_unit, spread, edge_spots)


class Pipeline:
    def __init__(
        self,
        source,
        output_dir,
        quality="normal",
        gpu=False,
        restart=False,
        accept_weak_photos=False,
        video_frames=DEFAULT_VIDEO_FRAMES,
        tools=None,
        runner=None,
        on_log=lambda line: None,
        on_stage=lambda stage, status: None,
    ):
        self.source = Path(source).resolve()
        self.output_dir = Path(output_dir).resolve()
        self.work_dir = self.output_dir / "work"
        self.model_dir = self.output_dir / "model"
        self.report_path = self.output_dir / REPORT_NAME
        self.is_video = self.source.is_file()
        # Frames taken from a video stand in for photos in every later stage.
        self.photos_dir = self.work_dir / "frames" if self.is_video else self.source
        self.quality_name = quality
        self.quality = QUALITIES[quality]
        self.gpu = gpu
        self.restart = restart
        self.accept_weak_photos = accept_weak_photos
        self.video_frames = video_frames
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
            if (stage != "refine" or self.quality.refine)
            and (stage != "frames" or self.is_video)
        ]

    def run(self):
        """Run every unfinished stage and return the run report."""
        self.tools = self.tools or load_tools()
        if self.gpu and not has_gpu_tools(self.tools):
            raise PipelineError(
                "GPU mode is not installed yet. Run: python setup_tools.py --gpu"
            )
        photos = self._check_video() if self.is_video else self._check_folders()
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
        self.report["stl"] = str(self.model_dir / "model.stl")
        self._save_report()
        return self.report

    def _check_video(self):
        if self.source.suffix.lower() not in VIDEO_EXTENSIONS:
            types = ", ".join(sorted(VIDEO_EXTENSIONS))
            raise PipelineError(
                f"{self.source.name} is not a supported video type ({types}). "
                "For photos, choose the folder that holds them."
            )
        if "ffmpeg" not in self.tools:
            raise PipelineError("Video input needs ffmpeg. Run: python setup_tools.py")
        if self.video_frames < MIN_PHOTOS:
            raise PipelineError(f"Use at least {MIN_PHOTOS} video frames.")
        return []  # The frames don't exist until the first stage runs.

    def _check_folders(self):
        if not self.photos_dir.is_dir():
            raise PipelineError(f"This photo folder or video does not exist: {self.photos_dir}")
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
        if self.is_video:
            signature = video_signature(self.source, self.video_frames)
        else:
            signature = photo_signature(self.photos_dir, photos)
        settings = {"quality": self.quality_name, "gpu": self.gpu}
        if not self.report_path.exists():
            return {
                "source": str(self.source),
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
                "This output folder holds a run for different photos, or for a different "
                "video or frame count. Choose another folder, or start over with --restart."
            )
        if report["settings"] != settings:
            for stage in SETTINGS_STAGES:
                report["stages"].pop(stage, None)
            report["settings"] = settings
        return report

    def _save_report(self):
        write_report(self.report_path, self.report)

    def _warn(self, message):
        if message not in self.report["warnings"]:
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

    def _ffmpeg(self, *args):
        tool = self.tools["ffmpeg"]["exe"]
        return self._run(
            "frames",
            [tool, "-hide_banner", "-nostdin", "-loglevel", "warning", "-y", *args],
        )

    def _stage_frames(self):
        shutil.rmtree(self.photos_dir, ignore_errors=True)
        self.photos_dir.mkdir()
        # A relative path, because the colon in a full path would need escaping here.
        scoring = (
            f"scale={BLUR_SCORING_WIDTH}:-2,blurdetect,"
            "metadata=mode=print:key=lavfi.blur:file=blur.txt"
        )
        self._ffmpeg("-i", self.source, "-map", "0:v:0", "-vf", scoring, "-f", "null", "-")
        scores = parse_blur_scores((self.work_dir / "blur.txt").read_text(encoding="utf-8"))
        if len(scores) < MIN_PHOTOS:
            raise PipelineError(
                f"The video has only {len(scores)} frames. At least {MIN_PHOTOS} are needed."
            )
        if len(scores) < self.video_frames:
            self._warn(
                f"The video has only {len(scores)} frames, fewer than the "
                f"{self.video_frames} asked for."
            )
        picked = pick_sharpest(scores, self.video_frames)
        # A file, because a long list of frames could pass the command line's limit.
        (self.work_dir / "select.txt").write_text(
            f"select='{select_expression(picked)}'", encoding="utf-8"
        )
        self._ffmpeg(
            "-i", self.source,
            "-map", "0:v:0",
            "-/vf", "select.txt",
            # Without this, ffmpeg repeats frames to fill the gaps between those picked.
            "-fps_mode", "passthrough",
            "-q:v", "2",
            "frames/frame_%04d.jpg",
        )
        found = len(find_photos(self.photos_dir))
        if found != len(picked):
            raise PipelineError(
                f"ffmpeg wrote {found} frames from the video instead of {len(picked)}."
            )
        self.report["photos_found"] = found

    def _stage_features(self):
        # Every video frame comes from one camera, so its lens is solved only once.
        camera = ["--ImageReader.single_camera", "1"] if self.is_video else []
        self._colmap(
            "features",
            "feature_extractor",
            "--database_path", "database.db",
            "--image_path", self.photos_dir,
            "--FeatureExtraction.use_gpu", "0",
            *camera,
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
        best_model, best_output, placed = None, "", 0
        models = sorted(path for path in (self.work_dir / "sparse").iterdir() if path.is_dir())
        for model in models:
            result = self._colmap(
                "select_model", "model_analyzer", "--path", f"sparse/{model.name}"
            )
            registered = parse_registered_images(result.output)
            if registered > placed:
                best_model, best_output, placed = model.name, result.output, registered
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
        self._check_photos(best_model, best_output, placed)

    def _check_photos(self, model, analyzer_output, placed):
        stats = parse_model_stats(analyzer_output)
        sparse = self.work_dir / "sparse" / model
        angle = viewing_angle(sparse)
        still = still_share(sparse)
        problems = photo_problems(stats, angle, placed, still)
        self.report["photo_check"] = {
            **stats,
            "widest_angle": angle,
            "still_share": still,
            "problems": problems,
        }
        # This sign is enough alone: a set can pass the others with half its cameras wrong.
        background_stayed = still is not None and still > MAX_STILL_SHARE
        if len(problems) < WEAK_SIGNS_TO_STOP and not background_stayed:
            return
        subject, advice = (
            ("These video frames", WEAK_VIDEO_HELP)
            if self.is_video
            else ("These photos", WEAK_PHOTOS_HELP)
        )
        if background_stayed:
            advice = STILL_BACKGROUND_HELP
        if not self.accept_weak_photos:
            reasons = "\n".join(f"- {problem}" for problem in problems)
            raise WeakPhotos(f"{subject} look too weak for a good model:\n{reasons}\n{advice}")
        self._warn(f"{subject} looked weak, so the model may come out poorly.")

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

    def _stage_stl(self):
        model = self.model_dir / "model.obj"
        triangles = read_obj_triangles(model)
        if not triangles:
            raise PipelineError(f"{model} has no surface to save as STL.")
        # A real size set earlier still holds, because it depends only on the camera solve.
        scale = self.report.get("real_size", {}).get("mm_per_unit", 1)
        write_stl(self.model_dir / "model.stl", triangles, scale)
        self.on_log(f"Saved {len(triangles):,} triangles to model.stl.")
