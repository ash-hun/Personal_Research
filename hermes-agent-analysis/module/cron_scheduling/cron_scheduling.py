"""
cron_scheduling.py — A self-contained, stdlib-only mirror of Hermes Agent's
built-in cron scheduler.

WHAT THIS MIRRORS
-----------------
Hermes (Nous Research) lets a user say things like "every morning at 9 send me a
news digest" or "run the backup script nightly". Those natural-language tasks
become persisted *cron jobs* that run the agent **unattended** and deliver the
result to whatever platform the user is on (Telegram, Discord, Matrix, ...).

This module distills that pipeline into one runnable file:

    parse_schedule()  ─ turn a schedule string into a structured spec
    CronJob           ─ the job model (id, schedule, prompt/task, delivery target)
    JobStore          ─ persistence (JSON file, atomic-ish save)
    compute_next_run()─ when the job should fire next
    Scheduler         ─ due_jobs(now) + tick(now) loop
    cronjob_tool()    ─ the agent-facing create/list/remove tool

Everything that the real Hermes wires into the live gateway (LLM agent run,
platform adapters, file locking, profiles, skills, prompt-injection scanning)
is replaced by two *pluggable callables* you pass into the Scheduler:

    run_agent(job, now) -> AgentRunResult     # produce output for a due job
    deliver(job, content) -> DeliveryResult   # route output to a platform

SOURCE MAPPING (reference: _reference/hermes-agent/)
----------------------------------------------------
    cron/jobs.py
        parse_duration / parse_schedule      → schedule parsing
        compute_next_run / _recoverable_*    → next-run computation
        create_job / load_jobs / save_jobs   → JobStore
        get_due_jobs / _get_due_jobs_locked  → Scheduler.due_jobs
        mark_job_run / advance_next_run      → post-run bookkeeping
    cron/scheduler.py
        tick()                               → Scheduler.tick
        run_job() / _run_job_impl()          → run_agent callable (pluggable)
        _deliver_result() / _resolve_*       → deliver callable (pluggable)
    tools/cronjob_tools.py
        cronjob()                            → cronjob_tool()

Faithful names are kept where it helps a researcher cross-reference the source.

NOTE ON CRON SUPPORT
--------------------
Real Hermes uses the third-party ``croniter`` package for 5-field cron
expressions. To stay stdlib-only this mirror ships a small, dependency-free
cron evaluator (``_CronExpr``) that supports the common subset:
``minute hour day-of-month month day-of-week`` with ``*``, ``,``, ``-`` ranges,
and ``*/step``. That is enough for "0 9 * * *" style schedules.
"""

from __future__ import annotations

import json
import re
import uuid
from dataclasses import dataclass, field, asdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional


# =============================================================================
# Constants (mirrors cron/jobs.py grace-window constants)
# =============================================================================

ONESHOT_GRACE_SECONDS = 90        # cron/jobs.py: one-shot jobs may fire slightly late
MIN_GRACE_SECONDS = 120           # cron/jobs.py: _compute_grace_seconds lower bound
MAX_GRACE_SECONDS = 7200          # cron/jobs.py: _compute_grace_seconds upper bound (2h)
SILENT_MARKER = "[SILENT]"        # cron/scheduler.py: agent opt-out-of-delivery marker


# =============================================================================
# Schedule parsing  (mirrors cron/jobs.py: parse_duration / parse_schedule)
# =============================================================================

def parse_duration(s: str) -> int:
    """Parse a duration string into minutes.

    Mirrors ``cron/jobs.py::parse_duration``.

    Examples::

        "30m" -> 30      "2h" -> 120      "1d" -> 1440
    """
    s = s.strip().lower()
    match = re.match(
        r"^(\d+)\s*(m|min|mins|minute|minutes|h|hr|hrs|hour|hours|d|day|days)$", s
    )
    if not match:
        raise ValueError(f"Invalid duration: '{s}'. Use format like '30m', '2h', or '1d'")
    value = int(match.group(1))
    unit = match.group(2)[0]  # first char: m, h, or d
    multipliers = {"m": 1, "h": 60, "d": 1440}
    return value * multipliers[unit]


def parse_schedule(schedule: str, now: Optional[datetime] = None) -> Dict[str, Any]:
    """Parse a schedule string into a structured spec.

    Mirrors ``cron/jobs.py::parse_schedule``. Returns a dict with a ``kind`` of
    ``"once"`` | ``"interval"`` | ``"cron"``:

      - ``once``     → ``{"kind": "once", "run_at": <iso>, "display": ...}``
      - ``interval`` → ``{"kind": "interval", "minutes": <int>, "display": ...}``
      - ``cron``     → ``{"kind": "cron", "expr": <str>, "display": ...}``

    Supported inputs::

        "30m" / "2h" / "1d"     one-shot, that far from now
        "every 30m"             recurring interval
        "0 9 * * *"             5-field cron expression
        "2026-06-03T14:00"      one-shot at an explicit timestamp

    ``now`` is injected for deterministic tests (real Hermes calls an internal
    ``_hermes_now()``). Defaults to ``datetime.now(timezone.utc)``.
    """
    now = now or _utcnow()
    schedule = schedule.strip()
    original = schedule
    schedule_lower = schedule.lower()

    # "every X" -> recurring interval
    if schedule_lower.startswith("every "):
        minutes = parse_duration(schedule[6:].strip())
        return {"kind": "interval", "minutes": minutes, "display": f"every {minutes}m"}

    # 5-field cron expression (minute hour dom month dow)
    parts = schedule.split()
    if len(parts) >= 5 and all(re.match(r"^[\d\*\-,/]+$", p) for p in parts[:5]):
        expr = " ".join(parts[:5])
        _CronExpr(expr)  # validate — raises ValueError on bad fields
        return {"kind": "cron", "expr": expr, "display": expr}

    # ISO timestamp (contains 'T' or looks like a date)
    if "T" in schedule or re.match(r"^\d{4}-\d{2}-\d{2}", schedule):
        try:
            dt = datetime.fromisoformat(schedule.replace("Z", "+00:00"))
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return {
                "kind": "once",
                "run_at": dt.isoformat(),
                "display": f"once at {dt.strftime('%Y-%m-%d %H:%M')}",
            }
        except ValueError as e:
            raise ValueError(f"Invalid timestamp '{schedule}': {e}")

    # Duration like "30m" -> one-shot from now
    try:
        minutes = parse_duration(schedule)
        run_at = now + timedelta(minutes=minutes)
        return {"kind": "once", "run_at": run_at.isoformat(), "display": f"once in {original}"}
    except ValueError:
        pass

    raise ValueError(
        f"Invalid schedule '{original}'. Use:\n"
        f"  - Duration: '30m', '2h', '1d' (one-shot)\n"
        f"  - Interval: 'every 30m', 'every 2h' (recurring)\n"
        f"  - Cron: '0 9 * * *' (cron expression)\n"
        f"  - Timestamp: '2026-06-03T14:00:00' (one-shot at time)"
    )


# =============================================================================
# Minimal stdlib cron evaluator
# (real Hermes uses the third-party `croniter`; this is the stdlib stand-in)
# =============================================================================

class _CronExpr:
    """Tiny 5-field cron evaluator: ``minute hour day-of-month month day-of-week``.

    Supports ``*``, comma lists, ``a-b`` ranges, and ``*/step``. day-of-week is
    0-6 (Mon=0..Sun=6) to match a Python ``datetime.weekday()``; ``7`` is also
    accepted as Sunday for compatibility. This replaces ``croniter`` so the
    mirror stays stdlib-only — see module docstring.
    """

    _BOUNDS = {
        0: (0, 59),   # minute
        1: (0, 23),   # hour
        2: (1, 31),   # day of month
        3: (1, 12),   # month
        4: (0, 6),    # day of week (Mon=0)
    }

    def __init__(self, expr: str) -> None:
        fields = expr.split()
        if len(fields) != 5:
            raise ValueError(f"Invalid cron expression '{expr}': expected 5 fields, got {len(fields)}")
        self.expr = expr
        self.sets = [self._parse_field(f, i) for i, f in enumerate(fields)]

    def _parse_field(self, field_str: str, idx: int) -> set:
        lo, hi = self._BOUNDS[idx]
        allowed: set = set()
        for part in field_str.split(","):
            step = 1
            if "/" in part:
                part, step_str = part.split("/", 1)
                step = int(step_str)
            if part == "*":
                start, end = lo, hi
            elif "-" in part:
                a, b = part.split("-", 1)
                start, end = int(a), int(b)
            else:
                start = end = int(part)
            if idx == 4 and (start == 7 or end == 7):  # Sunday-as-7 compatibility
                allowed.add(6)
                start = min(start, 6)
                end = min(end, 6)
            for v in range(start, end + 1, step):
                if lo <= v <= hi:
                    allowed.add(v)
        if not allowed:
            raise ValueError(f"Invalid cron field '{field_str}'")
        return allowed

    def matches(self, dt: datetime) -> bool:
        return (
            dt.minute in self.sets[0]
            and dt.hour in self.sets[1]
            and dt.day in self.sets[2]
            and dt.month in self.sets[3]
            and dt.weekday() in self.sets[4]
        )

    def next_after(self, base: datetime) -> datetime:
        """Smallest minute-aligned datetime strictly after ``base`` that matches."""
        candidate = (base + timedelta(minutes=1)).replace(second=0, microsecond=0)
        # Bounded scan: a year of minutes is plenty for any 5-field expression.
        for _ in range(366 * 24 * 60):
            if self.matches(candidate):
                return candidate
            candidate += timedelta(minutes=1)
        raise ValueError(f"No next run found for cron '{self.expr}'")


# =============================================================================
# Time + next-run computation
# (mirrors cron/jobs.py: compute_next_run / _recoverable_oneshot_run_at /
#  _compute_grace_seconds)
# =============================================================================

def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_aware(dt: datetime) -> datetime:
    """Mirror of ``cron/jobs.py::_ensure_aware`` — naive timestamps treated as UTC."""
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _recoverable_oneshot_run_at(
    schedule: Dict[str, Any], now: datetime, last_run_at: Optional[str] = None
) -> Optional[str]:
    """Return a one-shot run time if still eligible to fire.

    Mirrors ``cron/jobs.py::_recoverable_oneshot_run_at``. A one-shot gets a
    small grace window so a job created seconds after its requested minute still
    fires on the next tick; once run, it never fires again.
    """
    if schedule.get("kind") != "once":
        return None
    if last_run_at:
        return None
    run_at = schedule.get("run_at")
    if not run_at:
        return None
    run_at_dt = _ensure_aware(datetime.fromisoformat(run_at))
    if run_at_dt >= now - timedelta(seconds=ONESHOT_GRACE_SECONDS):
        return run_at
    return None


def _compute_grace_seconds(schedule: Dict[str, Any]) -> int:
    """How late a recurring job may fire before it's fast-forwarded instead.

    Mirrors ``cron/jobs.py::_compute_grace_seconds``: half the schedule period,
    clamped to [120s, 2h]. Daily jobs can catch up for 2h; every-5-min jobs
    fast-forward quickly.
    """
    kind = schedule.get("kind")
    if kind == "interval":
        grace = schedule.get("minutes", 1) * 60 // 2
        return max(MIN_GRACE_SECONDS, min(grace, MAX_GRACE_SECONDS))
    if kind == "cron":
        try:
            expr = _CronExpr(schedule["expr"])
            base = _utcnow()
            first = expr.next_after(base)
            second = expr.next_after(first)
            grace = int((second - first).total_seconds()) // 2
            return max(MIN_GRACE_SECONDS, min(grace, MAX_GRACE_SECONDS))
        except Exception:
            pass
    return MIN_GRACE_SECONDS


def compute_next_run(
    schedule: Dict[str, Any], last_run_at: Optional[str] = None, now: Optional[datetime] = None
) -> Optional[str]:
    """Compute the next run time (ISO string) for a schedule, or None if no more runs.

    Mirrors ``cron/jobs.py::compute_next_run``. ``now`` is injectable for tests.
    """
    now = now or _utcnow()

    if schedule["kind"] == "once":
        return _recoverable_oneshot_run_at(schedule, now, last_run_at=last_run_at)

    if schedule["kind"] == "interval":
        minutes = schedule["minutes"]
        if last_run_at:
            base = _ensure_aware(datetime.fromisoformat(last_run_at))
            return (base + timedelta(minutes=minutes)).isoformat()
        return (now + timedelta(minutes=minutes)).isoformat()

    if schedule["kind"] == "cron":
        base = now
        if last_run_at:
            base = _ensure_aware(datetime.fromisoformat(last_run_at))
        return _CronExpr(schedule["expr"]).next_after(base).isoformat()

    return None


# =============================================================================
# Job model  (mirrors the dict built by cron/jobs.py::create_job)
# =============================================================================

@dataclass
class Repeat:
    """How many times a job runs. ``times=None`` means forever."""
    times: Optional[int] = None
    completed: int = 0


@dataclass
class CronJob:
    """A scheduled, unattended agent task.

    Faithful to the job record built in ``cron/jobs.py::create_job``. The fields
    a researcher cares about:

      - ``prompt``          natural-language task the agent runs unattended
      - ``schedule``        structured spec from ``parse_schedule``
      - ``deliver``         delivery target: "local" | "origin" | "telegram" | "telegram:<chat>"
      - ``next_run_at``     ISO timestamp the scheduler checks against ``now``
      - ``repeat``          run-count bookkeeping (auto-removes when limit hit)
      - ``state``           "scheduled" | "completed" | "paused" | "error"
    """
    id: str
    name: str
    prompt: str
    schedule: Dict[str, Any]
    schedule_display: str
    deliver: str = "local"
    origin: Optional[Dict[str, Any]] = None
    repeat: Repeat = field(default_factory=Repeat)
    enabled: bool = True
    state: str = "scheduled"
    created_at: str = ""
    next_run_at: Optional[str] = None
    last_run_at: Optional[str] = None
    last_status: Optional[str] = None
    last_error: Optional[str] = None
    last_delivery_error: Optional[str] = None

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        return d

    @classmethod
    def from_dict(cls, d: Dict[str, Any]) -> "CronJob":
        d = dict(d)
        rep = d.get("repeat") or {}
        if isinstance(rep, dict):
            d["repeat"] = Repeat(times=rep.get("times"), completed=rep.get("completed", 0))
        return cls(**d)


def create_job(
    prompt: str,
    schedule: str,
    *,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    now: Optional[datetime] = None,
) -> CronJob:
    """Build a :class:`CronJob` from natural-language-ish inputs.

    Mirrors ``cron/jobs.py::create_job`` (minus the skill/model/script/profile
    knobs the full agent supports). Sets ``repeat=1`` for one-shot schedules and
    defaults delivery to ``origin`` when an origin is known, else ``local``.
    """
    now = now or _utcnow()
    parsed = parse_schedule(schedule, now=now)

    if repeat is not None and repeat <= 0:
        repeat = None
    if parsed["kind"] == "once" and repeat is None:
        repeat = 1  # one-shots run once by default

    if deliver is None:
        deliver = "origin" if origin else "local"

    label = (prompt or "cron job")[:50].strip()
    return CronJob(
        id=uuid.uuid4().hex[:12],
        name=name or label,
        prompt=prompt or "",
        schedule=parsed,
        schedule_display=parsed.get("display", schedule),
        deliver=deliver,
        origin=origin,
        repeat=Repeat(times=repeat, completed=0),
        enabled=True,
        state="scheduled",
        created_at=now.isoformat(),
        next_run_at=compute_next_run(parsed, now=now),
    )


# =============================================================================
# JobStore  (mirrors cron/jobs.py: load_jobs / save_jobs over a JSON file)
# =============================================================================

class JobStore:
    """JSON-file persistence for cron jobs.

    Mirrors ``cron/jobs.py``'s ``load_jobs`` / ``save_jobs``: jobs live under a
    top-level ``{"jobs": [...]}`` object. When ``path`` is None the store is
    purely in-memory (handy for demos/tests).
    """

    def __init__(self, path: Optional[Path] = None) -> None:
        self.path = Path(path) if path else None
        self._jobs: List[CronJob] = []
        if self.path and self.path.exists():
            self._load()

    def _load(self) -> None:
        assert self.path is not None
        data = json.loads(self.path.read_text(encoding="utf-8"))
        jobs = data.get("jobs", []) if isinstance(data, dict) else data
        self._jobs = [CronJob.from_dict(j) for j in jobs]

    def _save(self) -> None:
        if not self.path:
            return
        payload = {
            "jobs": [j.to_dict() for j in self._jobs],
            "updated_at": _utcnow().isoformat(),
        }
        # Atomic-ish: write to temp then replace (mirrors save_jobs' tmpfile+rename).
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        tmp.replace(self.path)

    def add(self, job: CronJob) -> CronJob:
        self._jobs.append(job)
        self._save()
        return job

    def all(self) -> List[CronJob]:
        return list(self._jobs)

    def get(self, job_id: str) -> Optional[CronJob]:
        for j in self._jobs:
            if j.id == job_id:
                return j
        return None

    def resolve_ref(self, ref: str) -> Optional[CronJob]:
        """Resolve by exact id, else case-insensitive name (mirrors resolve_job_ref)."""
        for j in self._jobs:
            if j.id == ref:
                return j
        ref_lower = ref.lower()
        matches = [j for j in self._jobs if (j.name or "").lower() == ref_lower]
        return matches[0] if matches else None

    def remove(self, job_id: str) -> bool:
        before = len(self._jobs)
        self._jobs = [j for j in self._jobs if j.id != job_id]
        if len(self._jobs) != before:
            self._save()
            return True
        return False

    def update(self, job: CronJob) -> None:
        for i, j in enumerate(self._jobs):
            if j.id == job.id:
                self._jobs[i] = job
                break
        self._save()


# =============================================================================
# Pluggable agent-run + delivery I/O types
# (real Hermes: run_job() returns (success, output_doc, final_response, error);
#  _deliver_result() returns None on success or an error string)
# =============================================================================

@dataclass
class AgentRunResult:
    """What a ``run_agent`` callable returns for one due job.

    Mirrors the ``(success, output, final_response, error)`` tuple from
    ``cron/scheduler.py::run_job``. ``final_response`` is what gets delivered;
    ``output`` is the full document persisted to disk.
    """
    success: bool
    final_response: str
    output: str = ""
    error: Optional[str] = None


@dataclass
class DeliveryResult:
    """What a ``deliver`` callable returns.

    Mirrors ``cron/scheduler.py::_deliver_result``: ``None`` error means success.
    """
    delivered: bool
    target: Optional[str] = None
    error: Optional[str] = None


# Callable signatures the Scheduler depends on (kept pluggable, like Hermes
# injecting the live agent + platform adapters at gateway startup):
RunAgentFn = Callable[[CronJob, datetime], AgentRunResult]
DeliverFn = Callable[[CronJob, str], DeliveryResult]


# =============================================================================
# Scheduler  (mirrors cron/jobs.py::get_due_jobs + cron/scheduler.py::tick)
# =============================================================================

@dataclass
class TickReport:
    """Summary of one ``Scheduler.tick(now)`` call."""
    now: str
    due_job_ids: List[str] = field(default_factory=list)
    executed: int = 0
    delivered: int = 0
    skipped_stale: List[str] = field(default_factory=list)


class Scheduler:
    """The scheduling loop.

    Drive it by calling :meth:`tick` with an explicit ``now`` — there is **no
    real-time sleeping**. On each tick it:

      1. computes :meth:`due_jobs` (next_run_at <= now, with stale fast-forward),
      2. advances recurring jobs' ``next_run_at`` *before* running (at-most-once,
         mirroring ``advance_next_run`` called under the file lock in ``tick``),
      3. invokes the pluggable ``run_agent`` to produce output,
      4. routes output through the pluggable ``deliver`` (unless ``[SILENT]``),
      5. records the outcome via :meth:`mark_job_run`.
    """

    def __init__(self, store: JobStore, run_agent: RunAgentFn, deliver: DeliverFn) -> None:
        self.store = store
        self.run_agent = run_agent
        self.deliver = deliver

    # -- due detection --------------------------------------------------------

    def due_jobs(self, now: datetime) -> List[CronJob]:
        """Return jobs that should fire at ``now``.

        Mirrors ``cron/jobs.py::_get_due_jobs_locked``:

          - disabled jobs are skipped,
          - a missing ``next_run_at`` is recovered (one-shot grace / recompute),
          - a recurring job whose scheduled time is stale by more than its grace
            window is fast-forwarded (next_run_at bumped) instead of firing —
            this prevents a burst of missed runs after downtime.
        """
        now = _ensure_aware(now)
        due: List[CronJob] = []

        for job in self.store.all():
            if not job.enabled:
                continue

            next_run = job.next_run_at
            if not next_run:
                # Recover a missing next_run_at the way Hermes does.
                recovered = _recoverable_oneshot_run_at(
                    job.schedule, now, last_run_at=job.last_run_at
                )
                if not recovered and job.schedule.get("kind") in {"cron", "interval"}:
                    recovered = compute_next_run(job.schedule, now=now)
                if not recovered:
                    continue
                job.next_run_at = recovered
                self.store.update(job)
                next_run = recovered

            next_run_dt = _ensure_aware(datetime.fromisoformat(next_run))
            if next_run_dt <= now:
                kind = job.schedule.get("kind")
                grace = _compute_grace_seconds(job.schedule)
                if kind in {"cron", "interval"} and (now - next_run_dt).total_seconds() > grace:
                    # Stale missed run — fast-forward instead of firing.
                    new_next = compute_next_run(job.schedule, now=now)
                    if new_next:
                        job.next_run_at = new_next
                        self.store.update(job)
                        continue
                due.append(job)

        return due

    # -- post-run bookkeeping -------------------------------------------------

    def advance_next_run(self, job: CronJob, now: datetime) -> None:
        """Preemptively advance a recurring job's next_run_at before it runs.

        Mirrors ``cron/jobs.py::advance_next_run`` — converts recurring jobs from
        at-least-once to at-most-once so a crash mid-run doesn't re-fire them.
        One-shots are left untouched so they can retry.
        """
        if job.schedule.get("kind") not in {"cron", "interval"}:
            return
        new_next = compute_next_run(job.schedule, last_run_at=now.isoformat(), now=now)
        if new_next and new_next != job.next_run_at:
            job.next_run_at = new_next
            self.store.update(job)

    def mark_job_run(
        self,
        job: CronJob,
        success: bool,
        now: datetime,
        error: Optional[str] = None,
        delivery_error: Optional[str] = None,
    ) -> None:
        """Record a run's outcome and compute the next run / completion.

        Mirrors ``cron/jobs.py::mark_job_run``: updates last_* fields, increments
        the repeat counter, auto-removes the job when its repeat limit is hit,
        and recomputes ``next_run_at`` (marking one-shots ``completed``).
        """
        job.last_run_at = now.isoformat()
        job.last_status = "ok" if success else "error"
        job.last_error = error if not success else None
        job.last_delivery_error = delivery_error

        job.repeat.completed += 1
        times = job.repeat.times
        if times is not None and times > 0 and job.repeat.completed >= times:
            self.store.remove(job.id)  # repeat limit reached → auto-delete
            return

        job.next_run_at = compute_next_run(job.schedule, last_run_at=job.last_run_at, now=now)
        if job.next_run_at is None:
            kind = job.schedule.get("kind")
            if kind in {"cron", "interval"}:
                job.state = "error"  # recurring jobs must never silently disable
                if not job.last_error:
                    job.last_error = "Failed to compute next run for recurring schedule"
            else:
                job.enabled = False
                job.state = "completed"
        elif job.state != "paused":
            job.state = "scheduled"

        self.store.update(job)

    # -- the tick loop --------------------------------------------------------

    def tick(self, now: datetime, *, verbose: bool = False) -> TickReport:
        """Run every job due at ``now`` end-to-end. Returns a :class:`TickReport`.

        Mirrors ``cron/scheduler.py::tick``: due → advance → run_agent → deliver
        → mark. Delivery of failed jobs sends an error alert; a ``[SILENT]``
        final response skips delivery (mirroring SILENT_MARKER handling).
        """
        now = _ensure_aware(now)
        due = self.due_jobs(now)
        report = TickReport(now=now.isoformat(), due_job_ids=[j.id for j in due])

        # Advance recurring jobs FIRST (at-most-once), before any execution.
        for job in due:
            self.advance_next_run(job, now)

        for job in due:
            if verbose:
                print(f"  [tick {now.strftime('%H:%M')}] firing job '{job.name}' ({job.id})")
            try:
                result = self.run_agent(job, now)
            except Exception as e:  # run_agent crashed → mark failure, keep going
                self.mark_job_run(job, False, now, error=str(e))
                continue

            report.executed += 1

            # Decide what to deliver: success → final_response, failure → alert.
            if result.success:
                deliver_content = result.final_response
            else:
                deliver_content = (
                    f"⚠️ Cron job '{job.name}' failed:\n{result.error}"
                )

            success = result.success
            error = result.error
            # Empty success response is a soft failure (mirrors issue #8585).
            if success and not result.final_response.strip():
                success = False
                error = "Agent completed but produced empty response"

            should_deliver = bool(deliver_content.strip())
            if should_deliver and success and SILENT_MARKER in deliver_content.upper():
                should_deliver = False  # agent opted out of delivery

            delivery_error: Optional[str] = None
            if should_deliver:
                dr = self.deliver(job, deliver_content)
                if dr.delivered:
                    report.delivered += 1
                else:
                    delivery_error = dr.error or "delivery failed"

            self.mark_job_run(job, success, now, error=error, delivery_error=delivery_error)

        return report


# =============================================================================
# Agent-facing tool  (mirrors tools/cronjob_tools.py::cronjob)
# =============================================================================

def cronjob_tool(
    action: str,
    store: JobStore,
    *,
    job_id: Optional[str] = None,
    prompt: Optional[str] = None,
    schedule: Optional[str] = None,
    name: Optional[str] = None,
    repeat: Optional[int] = None,
    deliver: Optional[str] = None,
    origin: Optional[Dict[str, Any]] = None,
    include_disabled: bool = False,
    now: Optional[datetime] = None,
) -> str:
    """Unified create/list/remove cron tool the agent calls. Returns a JSON string.

    Mirrors ``tools/cronjob_tools.py::cronjob``. Supported actions:

      - ``create`` — needs ``schedule`` and a ``prompt``; returns the new job.
      - ``list``   — returns all jobs (or include disabled).
      - ``remove`` — needs ``job_id`` (id or name); deletes the job.

    The real tool also supports pause/resume/trigger/update and many extra knobs
    (skills, model, script, profile, toolsets); those are intentionally elided
    to keep this mirror focused on the schedule → store → fire → deliver core.
    """
    normalized = (action or "").strip().lower()

    if normalized == "create":
        if not schedule:
            return json.dumps({"success": False, "error": "schedule is required for create"})
        if not prompt:
            return json.dumps({"success": False, "error": "create requires a prompt"})
        try:
            job = create_job(
                prompt=prompt,
                schedule=schedule,
                name=name,
                repeat=repeat,
                deliver=deliver,
                origin=origin,
                now=now,
            )
        except ValueError as e:
            return json.dumps({"success": False, "error": str(e)})
        store.add(job)
        return json.dumps(
            {
                "success": True,
                "job_id": job.id,
                "name": job.name,
                "schedule": job.schedule_display,
                "deliver": job.deliver,
                "next_run_at": job.next_run_at,
                "message": f"Cron job '{job.name}' created.",
            },
            indent=2,
        )

    if normalized == "list":
        jobs = [_format_job(j) for j in store.all() if include_disabled or j.enabled]
        return json.dumps({"success": True, "count": len(jobs), "jobs": jobs}, indent=2)

    if normalized == "remove":
        if not job_id:
            return json.dumps({"success": False, "error": "job_id is required for remove"})
        job = store.resolve_ref(job_id)
        if not job:
            return json.dumps({"success": False, "error": f"Job '{job_id}' not found"})
        store.remove(job.id)
        return json.dumps(
            {"success": True, "message": f"Cron job '{job.name}' removed.", "job_id": job.id},
            indent=2,
        )

    return json.dumps({"success": False, "error": f"Unknown action '{action}'"})


def _format_job(job: CronJob) -> Dict[str, Any]:
    """Compact job view for the ``list`` action. Mirrors ``cronjob_tools._format_job``."""
    prompt = job.prompt or ""
    return {
        "job_id": job.id,
        "name": job.name,
        "prompt_preview": prompt[:100] + "..." if len(prompt) > 100 else prompt,
        "schedule": job.schedule_display,
        "repeat": "forever" if job.repeat.times is None else f"{job.repeat.completed}/{job.repeat.times}",
        "deliver": job.deliver,
        "next_run_at": job.next_run_at,
        "last_run_at": job.last_run_at,
        "last_status": job.last_status,
        "enabled": job.enabled,
        "state": job.state,
    }


__all__ = [
    "parse_duration",
    "parse_schedule",
    "compute_next_run",
    "CronJob",
    "Repeat",
    "create_job",
    "JobStore",
    "AgentRunResult",
    "DeliveryResult",
    "Scheduler",
    "TickReport",
    "cronjob_tool",
    "SILENT_MARKER",
]
