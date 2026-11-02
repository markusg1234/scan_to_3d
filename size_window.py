"""Window for giving a model its real size, by clicking both ends of a known length in photos."""

import json
import math
import subprocess
import tkinter as tk
from pathlib import Path
from tkinter import messagebox, ttk

from pipeline import (
    REPORT_NAME,
    PipelineError,
    click_ray,
    length_problems,
    load_tools,
    measure_length,
    read_cameras,
    read_poses,
    set_real_size,
    widest_angle,
)

TITLE = "Set Real Size"
OVERVIEW_WIDTH = 680
OVERVIEW_HEIGHT = 420
DETAIL_SIZE = 400
ZOOM = 4
SPAN = DETAIL_SIZE // ZOOM  # Photo pixels across the zoomed view.
COLOURS = ("#ff2a2a", "#1e90ff")
HELP = (
    "Click both ends of something you know the length of, such as a ruler, in three photos "
    "taken at least 30 degrees apart. Two photos are enough to measure, but a third lets the "
    "program catch a click on the wrong spot. Click near a point in the photo, then exactly on "
    "it in the zoomed view."
)


class RealSizeWindow(tk.Toplevel):
    def __init__(self, parent, output_dir):
        output_dir = Path(output_dir).resolve()
        dense = output_dir / "work" / "dense"
        try:
            poses = read_poses(dense / "sparse" / "images.bin")
            cameras = read_cameras(dense / "sparse" / "cameras.bin")
            report = json.loads((output_dir / REPORT_NAME).read_text(encoding="utf-8"))
        except OSError:
            raise PipelineError(
                f"There's no finished run with its working files in {output_dir}."
            ) from None
        tools = load_tools()
        if "ffmpeg" not in tools:
            raise PipelineError("Setting the real size needs ffmpeg. Run: python setup_tools.py")
        super().__init__(parent)
        self.title(TITLE)
        self.output_dir = output_dir
        self.dense = dense
        self.cache = output_dir / "work" / "real_size"
        self.ffmpeg = tools["ffmpeg"]["exe"]
        self.poses = poses
        self.cameras = cameras
        names = sorted(poses)
        saved = report.get("real_size", {})
        self.clicks = {name: [None, None] for name in names}
        for name, points in saved.get("clicks", {}).items():
            if name in self.clicks:
                self.clicks[name] = [tuple(point) if point else None for point in points]
        self.names = names
        self.name = None
        self.photo = None
        self.overview = None
        self.subsample = 1
        self.corner = (0, 0)  # Top left of the zoomed view, in photo pixels.
        self.detail = tk.PhotoImage(width=DETAIL_SIZE, height=DETAIL_SIZE)
        self.point = tk.IntVar(value=0)
        self.rough = False  # Whether the current point was only placed in the shrunk photo.
        self.guide = tk.StringVar()
        self.length = tk.StringVar(value=f"{saved['length_mm']:g}" if saved else "")
        self.result = tk.StringVar()
        self._build()
        self.photo_list.selection_set(0)
        self._show(names[0])

    def _build(self):
        ttk.Label(self, text=HELP, wraplength=1100, padding=(12, 12, 12, 6)).pack(fill="x")
        views = ttk.Frame(self, padding=(12, 0))
        views.pack(fill="both", expand=True)
        self.photo_list = tk.Listbox(views, width=26, exportselection=False)
        scrollbar = ttk.Scrollbar(views, command=self.photo_list.yview)
        self.photo_list.configure(yscrollcommand=scrollbar.set)
        for name in self.names:
            self.photo_list.insert("end", self._list_text(name))
        self.photo_list.pack(side="left", fill="y")
        scrollbar.pack(side="left", fill="y")
        self.photo_list.bind("<<ListboxSelect>>", self._on_select)
        self.overview_canvas = tk.Canvas(
            views, width=OVERVIEW_WIDTH, height=OVERVIEW_HEIGHT, highlightthickness=0,
            background="black", cursor="hand2",
        )
        self.overview_canvas.pack(side="left", padx=(12, 0), anchor="n")
        self.overview_canvas.bind("<Button-1>", self._on_overview_click)
        self.detail_canvas = tk.Canvas(
            views, width=DETAIL_SIZE, height=DETAIL_SIZE, highlightthickness=0,
            background="black", cursor="crosshair",
        )
        self.detail_canvas.pack(side="left", padx=(12, 0), anchor="n")
        self.detail_canvas.create_image(0, 0, image=self.detail, anchor="nw")
        self.detail_canvas.bind("<Button-1>", self._on_detail_click)

        ttk.Label(
            self, textvariable=self.guide, wraplength=1100, padding=(12, 10, 12, 0),
            font=("Segoe UI", 10, "bold"),
        ).pack(fill="x")
        controls = ttk.Frame(self, padding=12)
        controls.pack(fill="x")
        for number in (0, 1):
            ttk.Radiobutton(
                controls,
                text=f"Point {number + 1}",
                variable=self.point,
                value=number,
                command=self._on_point_chosen,
            ).pack(side="left", padx=(0, 10))
        ttk.Button(controls, text="Clear This Photo", command=self._clear).pack(side="left")
        ttk.Label(controls, text="Length (mm)").pack(side="left", padx=(24, 4))
        ttk.Entry(controls, textvariable=self.length, width=10).pack(side="left")
        ttk.Button(controls, text="Save", command=self.save).pack(side="left", padx=8)
        ttk.Label(self, textvariable=self.result, wraplength=1100, padding=(12, 0, 12, 12)).pack(
            fill="x"
        )

    def _list_text(self, name):
        marks = " ".join(str(number + 1) for number, point in enumerate(self.clicks[name]) if point)
        return f"{name}   {marks}"

    def _on_select(self, _event):
        selected = self.photo_list.curselection()
        if selected:
            self._show(self.names[selected[0]])

    def _show(self, name):
        """Load a photo, and start the zoomed view on its first clicked point or its middle."""
        self.name = name
        self.photo = tk.PhotoImage(file=self._ppm(name))
        width, height = self.photo.width(), self.photo.height()
        self.subsample = max(
            1, math.ceil(width / OVERVIEW_WIDTH), math.ceil(height / OVERVIEW_HEIGHT)
        )
        self.overview = self.photo.subsample(self.subsample)
        points = self.clicks[name]
        # Start on the first point this photo still needs, whatever the last photo was on.
        self.point.set(1 if points[0] and not points[1] else 0)
        self.rough = False
        clicked = [point for point in points if point]
        self._zoom_at(*(clicked[0] if clicked else (width / 2, height / 2)))
        self._update_guide()

    def _ppm(self, name):
        """Tk can't read JPEG, so ffmpeg converts each photo once."""
        ppm = self.cache / (name.replace("/", "_").replace("\\", "_") + ".ppm")
        if not ppm.exists():
            self.cache.mkdir(exist_ok=True)
            subprocess.run(
                [
                    self.ffmpeg, "-hide_banner", "-nostdin", "-loglevel", "error", "-y",
                    "-i", self.dense / "images" / name, "-frames:v", "1", "-update", "1", ppm,
                ],
                check=True,
                capture_output=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        return ppm

    def _zoom_at(self, x, y):
        width, height = self.photo.width(), self.photo.height()
        left = min(max(round(x - SPAN / 2), 0), max(width - SPAN, 0))
        top = min(max(round(y - SPAN / 2), 0), max(height - SPAN, 0))
        self.corner = (left, top)
        self.detail.blank()
        self.detail.copy_replace(
            self.photo,
            from_coords=(left, top, min(left + SPAN, width), min(top + SPAN, height)),
            zoom=ZOOM,
        )
        self._draw()

    def _on_overview_click(self, event):
        x, y = self.overview_canvas.canvasx(event.x), self.overview_canvas.canvasy(event.y)
        point = ((x + 0.5) * self.subsample, (y + 0.5) * self.subsample)
        number = self.point.get()
        if self.clicks[self.name][number] is None:
            # A rough first position, so the click visibly places the point.
            self.clicks[self.name][number] = point
            self.rough = True
            self._refresh_list()
        self._zoom_at(*point)
        self._update_guide()

    def _on_detail_click(self, event):
        x, y = self.detail_canvas.canvasx(event.x), self.detail_canvas.canvasy(event.y)
        left, top = self.corner
        # The middle of the clicked screen pixel, in COLMAP's pixel positions.
        point = (left + (x + 0.5) / ZOOM, top + (y + 0.5) / ZOOM)
        number = self.point.get()
        self.clicks[self.name][number] = point
        self.rough = False
        if self.clicks[self.name][1 - number] is None:
            self.point.set(1 - number)
        self._refresh_list()
        self._draw()
        self._update_guide()

    def _on_point_chosen(self):
        self.rough = False
        self._update_guide()

    def _clear(self):
        self.clicks[self.name] = [None, None]
        self.point.set(0)
        self.rough = False
        self._refresh_list()
        self._draw()
        self._update_guide()

    def _update_guide(self):
        """Say what to click next, and how far along each point is."""
        number = self.point.get()
        points = self.clicks[self.name]
        if self.rough:
            step = f"Now click exactly on point {number + 1} in the zoomed view on the right."
        elif points[number] is None:
            step = f"Click near point {number + 1} in the photo."
        elif all(points):
            step = (
                "Both points are set in this photo. Choose another photo, taken further around. "
                "To move a point, choose it below and click in the zoomed view."
            )
        else:
            step = (
                f"Point {number + 1} is set. Click in the zoomed view to move it, or choose "
                f"Point {2 - number} below."
            )
        progress = []
        for number in (0, 1):
            rays = [
                click_ray(self.cameras[self.poses[name][0]], self.poses[name], clicked[number])
                for name, clicked in self.clicks.items()
                if clicked[number]
            ]
            text = f"Point {number + 1} is in {len(rays)} photo{'' if len(rays) == 1 else 's'}"
            if len(rays) >= 2:
                text += f", seen from {widest_angle(rays):.0f} degrees apart"
            progress.append(text)
        self.guide.set(
            f"{step}\n{progress[0]}. {progress[1]}. Each needs 2 photos, better 3, seen from at "
            "least 30 degrees apart."
        )

    def _refresh_list(self):
        index = self.names.index(self.name)
        self.photo_list.delete(index)
        self.photo_list.insert(index, self._list_text(self.name))
        self.photo_list.selection_set(index)

    def _draw(self):
        """Draw the photo, the zoomed area's outline and both points in each view."""
        overview, detail = self.overview_canvas, self.detail_canvas
        overview.delete("all")
        detail.delete("marks")
        overview.create_image(0, 0, image=self.overview, anchor="nw")
        left, top = self.corner
        scale = self.subsample
        overview.create_rectangle(
            left / scale, top / scale, (left + SPAN) / scale, (top + SPAN) / scale,
            outline="yellow",
        )
        for number, point in enumerate(self.clicks[self.name]):
            if not point:
                continue
            colour, label = COLOURS[number], str(number + 1)
            self._cross(overview, point[0] / scale, point[1] / scale, 6, colour, label)
            x, y = (point[0] - left) * ZOOM, (point[1] - top) * ZOOM
            if 0 <= x <= DETAIL_SIZE and 0 <= y <= DETAIL_SIZE:
                self._cross(detail, x, y, 14, colour, label)

    @staticmethod
    def _cross(canvas, x, y, size, colour, label):
        canvas.create_line(x - size, y, x + size, y, fill=colour, width=2, tags="marks")
        canvas.create_line(x, y - size, x, y + size, fill=colour, width=2, tags="marks")
        canvas.create_text(
            x + size + 2, y - size - 2, text=label, fill=colour, anchor="sw",
            font=("Segoe UI", 10, "bold"), tags="marks",
        )

    def save(self):
        try:
            length = float(self.length.get())
        except ValueError:
            messagebox.showinfo(TITLE, "Type the real length in millimetres.", parent=self)
            return
        clicks = {
            name: [list(point) if point else None for point in points]
            for name, points in self.clicks.items()
            if any(points)
        }
        try:
            problems = length_problems(measure_length(self.dense, clicks))
            if problems and not messagebox.askyesno(
                TITLE, "\n\n".join(problems) + "\n\nSave anyway?", parent=self
            ):
                return
            self.result.set("Saving model.stl...")
            self.configure(cursor="watch")
            self.update_idletasks()
            real_size = set_real_size(self.output_dir, clicks, length)
        except PipelineError as error:
            self.result.set("")
            messagebox.showinfo(TITLE, str(error), parent=self)
            return
        finally:
            self.configure(cursor="")
        x, y, z = real_size.size_mm
        self.result.set(
            f"Saved. model.stl is now in millimetres, {x:.0f} x {y:.0f} x {z:.0f} mm including "
            f"anything around the object. 1 model unit = {real_size.mm_per_unit:.2f} mm."
        )
