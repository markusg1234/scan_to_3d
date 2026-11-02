"""Desktop window for scan_to_3d."""

import os
import queue
import threading
import time
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pipeline import (
    DEFAULT_VIDEO_FRAMES,
    QUALITIES,
    STAGE_TITLES,
    VIDEO_EXTENSIONS,
    Cancelled,
    Pipeline,
    PipelineError,
    WeakPhotos,
    format_duration,
    has_gpu_tools,
    load_tools,
    view_model,
)
from settings import load_settings, save_settings
from size_window import RealSizeWindow

POLL_MILLISECONDS = 100
STATUS_TEXT = {
    "running": "Running...",
    "done": "Done",
    "skipped": "Already done",
    "stopped": "Stopped",
}


def existing_path(path):
    return path if isinstance(path, str) and os.path.exists(path) else ""


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Scan to 3D")
        self.geometry("820x660")
        self.minsize(660, 520)
        self.events = queue.Queue()
        self.pipeline = None
        self.closing = False
        self.run_started = None
        self.elapsed = 0
        self.running_stage = None
        self.stage_started = None
        settings = load_settings()
        self.photos = tk.StringVar(value=existing_path(settings.get("photos")))
        self.output = tk.StringVar(value=existing_path(settings.get("output")))
        self.quality = tk.StringVar(value="normal")
        self.gpu = tk.BooleanVar(value=False)
        self.restart = tk.BooleanVar(value=False)
        # Saved because resuming a video run needs the same frame count.
        self.frames = tk.StringVar(value=str(settings.get("frames", DEFAULT_VIDEO_FRAMES)))
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(POLL_MILLISECONDS, self._process_events)

    def _build(self):
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (label, variable, title) in enumerate((
            ("Photos or video", self.photos, "Choose the photo folder"),
            ("Output folder", self.output, "Choose where to save the model"),
        )):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
            ttk.Button(
                form,
                text="Browse...",
                command=lambda variable=variable, title=title: self._browse(variable, title),
            ).grid(row=row, column=2)
        ttk.Button(form, text="Video...", command=self._browse_video).grid(
            row=0, column=3, padx=(6, 0)
        )

        ttk.Label(form, text="Quality").grid(row=2, column=0, sticky="w", pady=3)
        options = ttk.Frame(form)
        options.grid(row=2, column=1, columnspan=3, sticky="w", padx=8)
        ttk.Combobox(
            options, textvariable=self.quality, values=list(QUALITIES), state="readonly", width=10
        ).pack(side="left")
        self.gpu_check = ttk.Checkbutton(
            options, text="Use graphics card (experimental)", variable=self.gpu
        )
        self.gpu_check.pack(side="left", padx=(14, 0))
        ttk.Checkbutton(options, text="Start over", variable=self.restart).pack(
            side="left", padx=(14, 0)
        )
        ttk.Label(options, text="Video frames").pack(side="left", padx=(14, 4))
        ttk.Spinbox(
            options, textvariable=self.frames, from_=20, to=500, increment=10, width=5
        ).pack(side="left")
        if not self._gpu_available():
            self.gpu_check.state(["disabled"])

        buttons = ttk.Frame(self, padding=(12, 0))
        buttons.pack(fill="x")
        self.start_button = ttk.Button(buttons, text="Start", command=self.start)
        self.start_button.pack(side="left")
        self.cancel_button = ttk.Button(
            buttons, text="Cancel", command=self.cancel, state="disabled"
        )
        self.cancel_button.pack(side="left", padx=8)
        self.open_button = ttk.Button(
            buttons, text="Open Result Folder", command=self.open_result
        )
        self.open_button.pack(side="left")
        self.view_button = ttk.Button(buttons, text="View Model", command=self.view)
        self.view_button.pack(side="left", padx=8)
        self.size_button = ttk.Button(buttons, text="Set Real Size...", command=self.set_size)
        self.size_button.pack(side="left")
        self.time_label = ttk.Label(buttons, text="Time: 0:00")
        self.time_label.pack(side="right")

        self.stage_list = ttk.Treeview(
            self, columns=("status", "time"), show="tree headings", height=len(STAGE_TITLES)
        )
        self.stage_list.heading("#0", text="Stage")
        self.stage_list.heading("status", text="Status")
        self.stage_list.heading("time", text="Time")
        self.stage_list.column("status", width=130, stretch=False)
        self.stage_list.column("time", width=80, stretch=False, anchor="e")
        self.stage_list.pack(fill="x", padx=12, pady=10)
        for stage, title in STAGE_TITLES.items():
            self.stage_list.insert("", "end", iid=stage, text=title, values=("", ""))

        log_frame = ttk.Frame(self, padding=(12, 0, 12, 12))
        log_frame.pack(fill="both", expand=True)
        self.log = tk.Text(log_frame, height=8, wrap="none", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, command=self.log.yview)
        self.log.configure(yscrollcommand=scrollbar.set)
        scrollbar.pack(side="right", fill="y")
        self.log.pack(side="left", fill="both", expand=True)

    @staticmethod
    def _gpu_available():
        try:
            return has_gpu_tools(load_tools())
        except PipelineError:
            return False

    def _browse(self, variable, title):
        folder = filedialog.askdirectory(title=title, initialdir=self._start_folder(variable))
        if folder:
            variable.set(os.path.normpath(folder))
            self._save_folders()

    def _browse_video(self):
        patterns = tuple(f"*{extension}" for extension in sorted(VIDEO_EXTENSIONS))
        video = filedialog.askopenfilename(
            title="Choose a video",
            initialdir=self._start_folder(self.photos),
            filetypes=[("Video files", patterns)],
        )
        if video:
            self.photos.set(os.path.normpath(video))
            self._save_folders()

    def _start_folder(self, variable):
        path = variable.get() or self.photos.get() or self.output.get()
        return (os.path.dirname(path) if os.path.isfile(path) else path) or None

    def _save_folders(self):
        save_settings(
            {"photos": self.photos.get(), "output": self.output.get(), "frames": self.frames.get()}
        )

    def start(self, accept_weak_photos=False):
        if not self.photos.get() or not self.output.get():
            messagebox.showinfo("Scan to 3D", "Choose photos or a video, and an output folder first.")
            return
        if not self.frames.get().isdecimal():
            messagebox.showinfo("Scan to 3D", "Video frames must be a whole number.")
            return
        self._save_folders()
        for stage in STAGE_TITLES:
            self.stage_list.set(stage, "status", "")
            self.stage_list.set(stage, "time", "")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.running_stage = None
        self.run_started = time.monotonic()
        self.elapsed = 0
        self.pipeline = Pipeline(
            self.photos.get(),
            self.output.get(),
            quality=self.quality.get(),
            gpu=self.gpu.get(),
            restart=self.restart.get(),
            accept_weak_photos=accept_weak_photos,
            video_frames=int(self.frames.get()),
            on_log=lambda line: self.events.put(("log", line)),
            on_stage=lambda stage, status: self.events.put(("stage", stage, status)),
        )
        self.start_button.state(["disabled"])
        self.view_button.state(["disabled"])
        self.size_button.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        threading.Thread(target=self._work, args=(self.pipeline,), daemon=True).start()

    def _work(self, pipeline):
        try:
            self.events.put(("finished", pipeline.run()))
        except Cancelled as error:
            self.events.put(("cancelled", str(error)))
        except WeakPhotos as error:
            self.events.put(("weak_photos", str(error)))
        except PipelineError as error:
            self.events.put(("failed", str(error)))
        except Exception as error:
            self.events.put(("failed", f"Unexpected error: {error!r}"))

    def cancel(self):
        if self.pipeline:
            self.cancel_button.state(["disabled"])
            self.pipeline.cancel()

    def open_result(self):
        output = self.output.get()
        if not output or not os.path.isdir(output):
            messagebox.showinfo("Scan to 3D", "Choose an output folder that has a run in it first.")
            return
        model = os.path.join(output, "model")
        os.startfile(model if os.path.isdir(model) else output)

    def view(self):
        if not self.output.get():
            messagebox.showinfo("Scan to 3D", "Choose the output folder that holds the model first.")
            return
        # In the background, because it waits a few seconds to see whether the viewer crashes.
        threading.Thread(target=self._view, args=(self.output.get(),), daemon=True).start()

    def set_size(self):
        if not self.output.get():
            messagebox.showinfo("Scan to 3D", "Choose the output folder that holds the model first.")
            return
        try:
            RealSizeWindow(self, self.output.get())
        except PipelineError as error:
            messagebox.showinfo("Scan to 3D", str(error))

    def _view(self, output):
        try:
            view_model(output)
        except PipelineError as error:
            self.events.put(("view_failed", str(error)))

    def close(self):
        if self.pipeline:
            self.closing = True
            self.cancel()
        else:
            self.destroy()

    def _process_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                getattr(self, f"_on_{event[0]}")(*event[1:])
        except queue.Empty:
            pass
        if self.closing and not self.pipeline:
            self.destroy()
            return
        self._update_times()
        self.after(POLL_MILLISECONDS, self._process_events)

    def _update_times(self):
        now = time.monotonic()
        if self.run_started is not None:
            self.elapsed = now - self.run_started
            self.time_label.configure(text=f"Time: {format_duration(self.elapsed)}")
        if self.running_stage:
            stage_time = format_duration(now - self.stage_started)
            self.stage_list.set(self.running_stage, "time", stage_time)

    def _on_log(self, line):
        at_end = self.log.yview()[1] >= 1.0
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.configure(state="disabled")
        if at_end:
            self.log.see("end")

    def _on_stage(self, stage, status):
        self.stage_list.set(stage, "status", STATUS_TEXT[status])
        if status == "running":
            self.running_stage = stage
            self.stage_started = time.monotonic()
        elif stage == self.running_stage:
            self._update_times()
            self.running_stage = None

    def _finish_run(self):
        self._update_times()
        self.run_started = None
        self.running_stage = None
        self.pipeline = None
        self.start_button.state(["!disabled"])
        self.view_button.state(["!disabled"])
        self.size_button.state(["!disabled"])
        self.cancel_button.state(["disabled"])

    def _on_finished(self, report):
        self._finish_run()
        self.restart.set(False)
        lines = [
            f"Placed {report['photos_placed']} of {report['photos_found']} photos.",
            f"Dense stage ran on: {report['dense_device']}",
            *report["warnings"],
            f"Time: {format_duration(self.elapsed)}",
            f"Model: {report['model']}",
            f"STL: {report['stl']}" + (" (in mm)" if "real_size" in report else ""),
        ]
        for line in lines:
            self._on_log(line)
        if not self.closing:
            messagebox.showinfo("Scan to 3D", "\n".join(lines))

    def _on_cancelled(self, message):
        self._finish_run()
        self._on_log(message)

    def _on_weak_photos(self, message):
        self._finish_run()
        self._on_log(message)
        if not self.closing and messagebox.askyesno(
            "Scan to 3D", f"{message}\n\nContinue anyway?"
        ):
            # Start Over already ran, and the finished stages should be kept.
            self.restart.set(False)
            self.start(accept_weak_photos=True)

    def _on_view_failed(self, message):
        if not self.closing:
            messagebox.showinfo("Scan to 3D", message)

    def _on_failed(self, message):
        self._finish_run()
        self._on_log(message)
        if not self.closing:
            messagebox.showerror("Scan to 3D", message)


if __name__ == "__main__":
    App().mainloop()
