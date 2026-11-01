"""Desktop window for photo_to_3d."""

import os
import queue
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

from pipeline import (
    QUALITIES,
    STAGE_TITLES,
    Cancelled,
    Pipeline,
    PipelineError,
    has_gpu_tools,
    load_tools,
)

POLL_MILLISECONDS = 100
STATUS_TEXT = {
    "running": "Running...",
    "done": "Done",
    "skipped": "Already done",
    "stopped": "Stopped",
}


class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("Photo to 3D")
        self.geometry("760x660")
        self.minsize(620, 520)
        self.events = queue.Queue()
        self.pipeline = None
        self.closing = False
        self.result_folder = None
        self.photos = tk.StringVar()
        self.output = tk.StringVar()
        self.quality = tk.StringVar(value="normal")
        self.gpu = tk.BooleanVar(value=False)
        self.restart = tk.BooleanVar(value=False)
        self._build()
        self.protocol("WM_DELETE_WINDOW", self.close)
        self.after(POLL_MILLISECONDS, self._process_events)

    def _build(self):
        form = ttk.Frame(self, padding=12)
        form.pack(fill="x")
        form.columnconfigure(1, weight=1)
        for row, (label, variable, title) in enumerate((
            ("Photo folder", self.photos, "Choose the photo folder"),
            ("Output folder", self.output, "Choose where to save the model"),
        )):
            ttk.Label(form, text=label).grid(row=row, column=0, sticky="w", pady=3)
            ttk.Entry(form, textvariable=variable).grid(row=row, column=1, sticky="ew", padx=8)
            ttk.Button(
                form,
                text="Browse...",
                command=lambda variable=variable, title=title: self._browse(variable, title),
            ).grid(row=row, column=2)

        ttk.Label(form, text="Quality").grid(row=2, column=0, sticky="w", pady=3)
        options = ttk.Frame(form)
        options.grid(row=2, column=1, columnspan=2, sticky="w", padx=8)
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
            buttons, text="Open Result Folder", command=self.open_result, state="disabled"
        )
        self.open_button.pack(side="left")

        self.stage_list = ttk.Treeview(
            self, columns=("status",), show="tree headings", height=len(STAGE_TITLES)
        )
        self.stage_list.heading("#0", text="Stage")
        self.stage_list.heading("status", text="Status")
        self.stage_list.column("status", width=130, stretch=False)
        self.stage_list.pack(fill="x", padx=12, pady=10)
        for stage, title in STAGE_TITLES.items():
            self.stage_list.insert("", "end", iid=stage, text=title, values=("",))

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
        folder = filedialog.askdirectory(title=title)
        if folder:
            variable.set(os.path.normpath(folder))

    def start(self):
        if not self.photos.get() or not self.output.get():
            messagebox.showinfo("Photo to 3D", "Choose a photo folder and an output folder first.")
            return
        for stage in STAGE_TITLES:
            self.stage_list.set(stage, "status", "")
        self.log.configure(state="normal")
        self.log.delete("1.0", "end")
        self.log.configure(state="disabled")
        self.result_folder = None
        self.pipeline = Pipeline(
            self.photos.get(),
            self.output.get(),
            quality=self.quality.get(),
            gpu=self.gpu.get(),
            restart=self.restart.get(),
            on_log=lambda line: self.events.put(("log", line)),
            on_stage=lambda stage, status: self.events.put(("stage", stage, status)),
        )
        self.start_button.state(["disabled"])
        self.open_button.state(["disabled"])
        self.cancel_button.state(["!disabled"])
        threading.Thread(target=self._work, args=(self.pipeline,), daemon=True).start()

    def _work(self, pipeline):
        try:
            self.events.put(("finished", pipeline.run()))
        except Cancelled as error:
            self.events.put(("cancelled", str(error)))
        except PipelineError as error:
            self.events.put(("failed", str(error)))
        except Exception as error:
            self.events.put(("failed", f"Unexpected error: {error!r}"))

    def cancel(self):
        if self.pipeline:
            self.cancel_button.state(["disabled"])
            self.pipeline.cancel()

    def open_result(self):
        if self.result_folder:
            os.startfile(self.result_folder)

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
        self.after(POLL_MILLISECONDS, self._process_events)

    def _on_log(self, line):
        at_end = self.log.yview()[1] >= 1.0
        self.log.configure(state="normal")
        self.log.insert("end", line + "\n")
        self.log.configure(state="disabled")
        if at_end:
            self.log.see("end")

    def _on_stage(self, stage, status):
        self.stage_list.set(stage, "status", STATUS_TEXT[status])

    def _finish_run(self):
        self.pipeline = None
        self.start_button.state(["!disabled"])
        self.cancel_button.state(["disabled"])

    def _on_finished(self, report):
        self.result_folder = os.path.dirname(report["model"])
        self._finish_run()
        self.open_button.state(["!disabled"])
        self.restart.set(False)
        lines = [
            f"Placed {report['photos_placed']} of {report['photos_found']} photos.",
            f"Dense stage ran on: {report['dense_device']}",
            *report["warnings"],
            f"Model: {report['model']}",
        ]
        for line in lines:
            self._on_log(line)
        if not self.closing:
            messagebox.showinfo("Photo to 3D", "\n".join(lines))

    def _on_cancelled(self, message):
        self._finish_run()
        self._on_log(message)

    def _on_failed(self, message):
        self._finish_run()
        self._on_log(message)
        if not self.closing:
            messagebox.showerror("Photo to 3D", message)


if __name__ == "__main__":
    App().mainloop()
