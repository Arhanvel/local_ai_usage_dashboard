"""Background jobs for the web UI: ingest, report export, model runs.

Each job runs in its own thread, appends progress lines to a log and ends
with a result or an error. The UI polls /api/jobs/<id>. Jobs are kept in
memory only - they describe work whose output lands on disk anyway.
"""
import threading
import traceback
import uuid
from datetime import datetime, timezone

_jobs = {}
_lock = threading.Lock()
MAX_KEPT = 40


def _now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Job:
    def __init__(self, kind, label):
        self.id = uuid.uuid4().hex[:10]
        self.kind = kind
        self.label = label
        self.status = "running"
        self.started_at = _now()
        self.finished_at = None
        self.log = []
        self.result = None
        self.error = None
        self._cancel = threading.Event()

    def say(self, msg):
        line = f"{datetime.now().strftime('%H:%M:%S')}  {msg}"
        self.log.append(line)
        if len(self.log) > 400:
            del self.log[:100]

    @property
    def cancelled(self):
        return self._cancel.is_set()

    def to_dict(self, tail=None):
        return {
            "id": self.id, "kind": self.kind, "label": self.label, "status": self.status,
            "started_at": self.started_at, "finished_at": self.finished_at,
            "log": self.log[-tail:] if tail else self.log,
            "result": self.result, "error": self.error,
        }


def start(kind, label, fn, *args, **kwargs):
    """Run fn(job, *args) in a thread. Returns the Job immediately."""
    job = Job(kind, label)

    def runner():
        try:
            job.result = fn(job, *args, **kwargs)
            job.status = "cancelled" if job.cancelled else "done"
        except Exception as exc:  # the UI shows this; the thread must not die silently
            job.status = "failed"
            job.error = f"{type(exc).__name__}: {exc}"
            job.say(traceback.format_exc().strip().splitlines()[-1])
        finally:
            job.finished_at = _now()

    with _lock:
        _jobs[job.id] = job
        if len(_jobs) > MAX_KEPT:
            for old in sorted(_jobs.values(), key=lambda j: j.started_at)[:len(_jobs) - MAX_KEPT]:
                if old.status != "running":
                    _jobs.pop(old.id, None)
    threading.Thread(target=runner, name=f"job-{kind}-{job.id}", daemon=True).start()
    return job


def get(job_id):
    return _jobs.get(job_id)


def cancel(job_id):
    job = _jobs.get(job_id)
    if job and job.status == "running":
        job._cancel.set()
        return True
    return False


def running(kind=None):
    return [j for j in _jobs.values() if j.status == "running" and (kind is None or j.kind == kind)]


def recent(limit=15, tail=40):
    """Newest first; the server's list is the one source of truth for the UI."""
    return [j.to_dict(tail=tail) for j in sorted(_jobs.values(), key=lambda j: j.started_at, reverse=True)[:limit]]
