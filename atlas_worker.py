"""
The thread a desktop without GLib (Windows, macOS) works through: the page's requests, one at a time and
in order, and the timers (the host's tick, checking for drives coming and going). On Linux, GTK's own
thread does this (atlas_linux.py).
"""

import queue
import threading
import time


class Worker:
    def __init__(self):
        self.jobs = queue.Queue()
        self.timers = []
        self.thread = None

    def start_worker(self):
        self.thread = threading.Thread(target=self.run_loop, daemon=True)
        self.thread.start()

    def call_main(self, fn, *a):
        """Runs fn on the worker, soon, in the order asked."""
        self.jobs.put((fn, a))

    def call_main_wait(self, fn, *a):
        if threading.current_thread() is self.thread:
            return fn(*a)
        done, out = threading.Event(), {}

        def run():
            try:
                out["v"] = fn(*a)
            finally:
                done.set()
        self.call_main(run)
        done.wait(10)
        return out.get("v")

    def every(self, ms, fn):
        """Runs fn on the worker every ms milliseconds, until it returns False."""
        self.timers.append([ms / 1000.0, time.monotonic() + ms / 1000.0, fn])

    def run_loop(self):
        while True:
            now = time.monotonic()
            wait = max(0.0, min((t[1] for t in self.timers), default=now + 1) - now)
            try:
                fn, a = self.jobs.get(timeout=wait)
                try:
                    fn(*a)
                except Exception:  # one failed request must not stop the others
                    pass
            except queue.Empty:
                pass
            now = time.monotonic()
            for t in list(self.timers):
                if t[1] <= now:
                    t[1] = now + t[0]
                    try:
                        if t[2]() is False:   # a timer that is done
                            self.timers.remove(t)
                    except Exception:
                        pass
