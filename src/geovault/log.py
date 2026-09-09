"""Run logging and progress for the CLI.

Every ingest writes the same lines to the console and to a run log under
data/logs/<dataset>_<YYYYmmdd-HHMMSS>.log (the data root follows GEOVAULT_DATA),
each line stamped HH:MM:SS. Long runs live in the background with their output
redirected to a file, so this is plain line logging on purpose: a progress bar
(tqdm-style carriage returns) turns into noise in a file, and the numbers that
matter for a background run are elapsed, rate and ETA, which every scene line
carries via Progress.
"""

import logging
import sys
import threading
import time
from datetime import datetime
from pathlib import Path


def setup(dataset: str, to_file: bool = True) -> logging.Logger:
    """Logger 'geovault' with a console handler and, unless to_file is False, a fresh run log
    under data/logs. Calling setup twice replaces the handlers (idempotent within a process)."""
    log = logging.getLogger("geovault")
    log.setLevel(logging.INFO)
    log.propagate = False
    for h in list(log.handlers):
        log.removeHandler(h)
    fmt = logging.Formatter("%(asctime)s  %(message)s", datefmt="%H:%M:%S")
    con = logging.StreamHandler(sys.stdout)
    con.setFormatter(fmt)
    log.addHandler(con)
    if to_file:
        from .store import ROOT
        d = ROOT / "logs"
        d.mkdir(parents=True, exist_ok=True)
        path = d / f"{dataset}_{datetime.now():%Y%m%d-%H%M%S}.log"
        fh = logging.FileHandler(path, encoding="utf-8")
        fh.setFormatter(logging.Formatter("%(asctime)s  %(message)s", datefmt="%Y-%m-%d %H:%M:%S"))
        log.addHandler(fh)
        log.info(f"log file: {path}")
    return log


def fmt_duration(seconds: float) -> str:
    """90 -> '1m30s', 4000 -> '1h06m', 30 -> '30s'."""
    seconds = max(0, int(round(seconds)))
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    if h:
        return f"{h}h{m:02d}m"
    if m:
        return f"{m}m{s:02d}s"
    return f"{s}s"


class Progress:
    """Thread-safe counter over a known total; tick() returns the suffix for a progress line:
    'elapsed 12m03s · 9.8/min · eta 45m'. Rate and ETA use completed items, not the item index,
    so they stay right when items finish out of order (concurrent scene workers)."""

    def __init__(self, total: int):
        self.total = total
        self.done = 0
        self.t0 = time.time()
        self._lock = threading.Lock()

    def tick(self) -> str:
        with self._lock:
            self.done += 1
            done = self.done
        elapsed = time.time() - self.t0
        rate = done / elapsed * 60 if elapsed > 0 else 0.0
        left = self.total - done
        eta = fmt_duration(left / (rate / 60)) if rate > 0 and left else ("0s" if not left else "?")
        return f"elapsed {fmt_duration(elapsed)} · {rate:.1f}/min · eta {eta}"

    def summary(self) -> str:
        return f"{self.done}/{self.total} in {fmt_duration(time.time() - self.t0)}"
