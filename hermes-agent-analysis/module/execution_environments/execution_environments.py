"""Self-contained, stdlib-only mirror of Hermes Agent's pluggable execution backends.

This module distills the *execution_environments* feature from Nous Research's
Hermes Agent: a single ``Environment`` interface that abstracts "run a command /
read-write files" so the agent's shell + file tools behave identically no matter
*where* code actually runs — the user's laptop, a Docker container, a remote SSH
host, or a serverless Modal sandbox.

Why this matters
----------------
The agent only ever calls ``env.execute(...)``, ``env.write_file(...)``,
``env.read_file(...)``. It never knows (or cares) which backend is underneath.
Swap ``LocalEnvironment`` for ``ModalEnvironment`` and the SAME tool code runs
unchanged. Remote/serverless backends add two extra concerns the interface hides:

1. **File sync** — the host filesystem is not visible inside a remote sandbox, so
   changed files must be uploaded before each command (and pulled back on teardown).
2. **Serverless persistence (hibernate/wake)** — a Modal sandbox is ephemeral; on
   teardown its filesystem is *snapshotted*, and on the next session that snapshot
   is *restored*, giving the illusion of a long-lived machine.

Source mapping (Hermes Agent, tools/environments/)
--------------------------------------------------
- ``base.py``        -> ``Environment`` ABC here (BaseEnvironment): execute(),
                        init_session(), _wrap_command(), _run_bash(), cleanup().
- ``local.py``       -> ``LocalEnvironment`` (real subprocess execution).
- ``docker.py``      -> ``DockerEnvironment`` (bind-mount, no file sync).
- ``ssh.py``         -> ``SSHEnvironment`` (remote, uses FileSyncManager).
- ``modal.py`` +
  ``managed_modal.py`` -> ``ModalEnvironment`` (serverless; snapshot on cleanup,
                        restore on init = hibernate/wake).
- ``file_sync.py``   -> ``FileSyncManager`` (mtime change-detection, sync_back).

Everything here is stdlib-only and self-contained. ``LocalEnvironment`` really
shells out via ``subprocess``; the Docker / SSH / Modal backends are faithful
*fakes* that simulate a separate filesystem + the remote-exec / hibernate-wake
lifecycle so the data flow is observable without Docker/SSH/Modal installed.
"""

from __future__ import annotations

import hashlib
import os
import shlex
import subprocess
import tempfile
import time
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Callable

# ---------------------------------------------------------------------------
# Typed I/O — mirrors the dict results Hermes passes around as {"output", "returncode"}
# ---------------------------------------------------------------------------


@dataclass
class ExecResult:
    """Result of running one command in an environment.

    Mirror of the ``{"output": str, "returncode": int}`` dict returned by
    ``BaseEnvironment.execute`` in Hermes ``tools/environments/base.py``.
    """

    output: str
    returncode: int

    @property
    def ok(self) -> bool:
        return self.returncode == 0


@dataclass
class ReadResult:
    """Result of reading a file (mirror of ReadResult in tools/file_operations.py)."""

    content: str
    returncode: int = 0


@dataclass
class WriteResult:
    """Result of writing a file (mirror of WriteResult in tools/file_operations.py)."""

    path: str
    bytes_written: int
    returncode: int = 0


@dataclass
class SyncStats:
    """Outcome of one FileSyncManager.sync() cycle."""

    uploaded: list[str] = field(default_factory=list)
    deleted: list[str] = field(default_factory=list)
    skipped_unchanged: int = 0


# ---------------------------------------------------------------------------
# FileSyncManager — mirror of tools/environments/file_sync.py
# ---------------------------------------------------------------------------


def _file_mtime_key(host_path: str) -> tuple[float, int] | None:
    """(mtime, size) fingerprint for change detection. Hermes file_sync._file_mtime_key."""
    try:
        st = os.stat(host_path)
        return (st.st_mtime, st.st_size)
    except OSError:
        return None


def _sha256_file(path: str) -> str:
    """Hex SHA-256 of a file's bytes (Hermes file_sync._sha256_file)."""
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


# Transport callback types — backends supply these, mirroring file_sync.py.
GetFilesFn = Callable[[], "list[tuple[str, str]]"]   # -> [(host_path, remote_path), ...]
UploadFn = Callable[[str, str], None]                # (host_path, remote_path) -> None
DeleteFn = Callable[["list[str]"], None]             # (remote_paths) -> None


class FileSyncManager:
    """Tracks local file changes and syncs them into a remote environment.

    Faithful distillation of ``tools/environments/file_sync.py``. Backends wire
    it up with transport callbacks; the manager owns mtime-based change
    detection, deletion tracking, rate limiting, and transactional rollback.

    NOT used by bind-mount backends (Docker, Singularity) — those see the host
    filesystem live and need no sync.
    """

    def __init__(
        self,
        get_files_fn: GetFilesFn,
        upload_fn: UploadFn,
        delete_fn: DeleteFn,
        sync_interval: float = 2.0,
    ) -> None:
        self._get_files_fn = get_files_fn
        self._upload_fn = upload_fn
        self._delete_fn = delete_fn
        # remote_path -> (mtime, size) fingerprint of what we last pushed
        self._synced_files: dict[str, tuple[float, int]] = {}
        # remote_path -> sha256 of pushed content (used by sync_back to detect drift)
        self._pushed_hashes: dict[str, str] = {}
        self._last_sync_time: float = 0.0
        self._sync_interval = sync_interval

    def sync(self, *, force: bool = False) -> SyncStats:
        """One sync cycle: upload new/changed files, delete removed ones.

        Rate-limited to once per ``sync_interval`` unless ``force=True``.
        Transactional: state is committed only if every transport op succeeds,
        otherwise it rolls back so the next cycle retries everything.
        Mirrors ``FileSyncManager.sync`` in file_sync.py.
        """
        stats = SyncStats()
        if not force:
            now = time.monotonic()
            if now - self._last_sync_time < self._sync_interval:
                return stats

        current_files = self._get_files_fn()
        current_remote_paths = {remote for _, remote in current_files}

        # --- Uploads: files that are new or whose (mtime, size) changed ---
        to_upload: list[tuple[str, str]] = []
        new_files = dict(self._synced_files)
        for host_path, remote_path in current_files:
            key = _file_mtime_key(host_path)
            if key is None:
                continue
            if self._synced_files.get(remote_path) == key:
                stats.skipped_unchanged += 1
                continue
            to_upload.append((host_path, remote_path))
            new_files[remote_path] = key

        # --- Deletes: previously synced paths no longer present on host ---
        to_delete = [p for p in self._synced_files if p not in current_remote_paths]

        if not to_upload and not to_delete:
            self._last_sync_time = time.monotonic()
            return stats

        prev_files = dict(self._synced_files)
        prev_hashes = dict(self._pushed_hashes)
        try:
            for host_path, remote_path in to_upload:
                self._upload_fn(host_path, remote_path)
                stats.uploaded.append(remote_path)
            if to_delete:
                self._delete_fn(to_delete)
                stats.deleted.extend(to_delete)

            # Commit (all transport ops succeeded)
            for host_path, remote_path in to_upload:
                self._pushed_hashes[remote_path] = _sha256_file(host_path)
            for p in to_delete:
                new_files.pop(p, None)
                self._pushed_hashes.pop(p, None)
            self._synced_files = new_files
            self._last_sync_time = time.monotonic()
        except Exception:
            # Roll back so the next cycle retries the whole set.
            self._synced_files = prev_files
            self._pushed_hashes = prev_hashes
            self._last_sync_time = time.monotonic()
            raise
        return stats


# ---------------------------------------------------------------------------
# Environment ABC — mirror of tools/environments/base.py :: BaseEnvironment
# ---------------------------------------------------------------------------


class Environment(ABC):
    """The single interface every Hermes execution backend implements.

    Mirror of ``BaseEnvironment`` (tools/environments/base.py). The agent's
    shell and file tools only ever talk to this surface, so the *same* tool
    code works against local, Docker, SSH, or serverless Modal backends.

    Concrete contract for subclasses:
      * ``_run_bash(cmd_string) -> ExecResult`` — actually launch the command
        wherever this backend runs code (subprocess / container / SSH / SDK).
      * ``cleanup()`` — release backend resources (container, instance, conn).

    The base class supplies the shared, backend-agnostic flow:
      * ``init_session()``  — one-time session bootstrap (snapshot CWD/env).
      * ``execute()``       — the unified call path: _before_execute hook ->
                              _wrap_command -> _run_bash, with CWD tracking.
      * ``read_file`` / ``write_file`` — implemented *via* execute(), so they
        too work identically on every backend (Hermes does this through
        cat/heredoc in tools/file_operations.py).
    """

    # Lifecycle / capability flags (mirror base.py + per-backend overrides).
    needs_file_sync: bool = False   # remote backends set True; bind-mount/local False
    serverless: bool = False        # Modal sets True (hibernate/wake on teardown)

    def __init__(self, cwd: str = "/", timeout: int = 60, env: dict | None = None) -> None:
        self.cwd = cwd
        self.timeout = timeout
        self.env = env or {}
        self._session_id = uuid.uuid4().hex[:12]
        self._snapshot_ready = False

    # -- abstract surface ---------------------------------------------------

    @abstractmethod
    def _run_bash(self, cmd_string: str, *, timeout: int = 120) -> ExecResult:
        """Spawn ``cmd_string`` wherever this backend runs code. Must override.

        Mirror of ``BaseEnvironment._run_bash``; each backend points this at its
        execution medium (subprocess.Popen, ``docker exec``, ``ssh``, Modal SDK).
        """
        raise NotImplementedError

    @abstractmethod
    def cleanup(self) -> None:
        """Release backend resources. Mirror of ``BaseEnvironment.cleanup``."""
        ...

    # -- lifecycle ----------------------------------------------------------

    def start(self) -> None:
        """Bring the backend up and capture a session snapshot.

        In Hermes the snapshot captures env vars/functions/aliases once so each
        later command can re-source them cheaply (base.py ``init_session``).
        Here we just record that the session is live.
        """
        self.init_session()

    def init_session(self) -> None:
        """One-time session bootstrap. Mirror of ``BaseEnvironment.init_session``."""
        self._snapshot_ready = True

    def stop(self) -> None:
        """Alias for cleanup, mirroring ``BaseEnvironment.stop``."""
        self.cleanup()

    # -- command wrapping ---------------------------------------------------

    def _before_execute(self) -> None:
        """Hook fired before every command.

        Remote backends (SSH, Modal) override this to flush their
        FileSyncManager so the sandbox sees the latest host files. Bind-mount
        (Docker) and Local backends are no-ops. Mirror of
        ``BaseEnvironment._before_execute``.
        """
        pass

    def _wrap_command(self, command: str, cwd: str) -> str:
        """Wrap a command so it cd's into ``cwd`` first.

        Hermes' real wrapper (base.py ``_wrap_command``) also re-sources the env
        snapshot and emits CWD markers it parses back out of stdout. We keep just
        the ``cd`` so the data flow stays legible.
        """
        return f"cd {shlex.quote(cwd)} && {command}"

    # -- the unified call path ---------------------------------------------

    def execute(self, command: str, cwd: str = "", *, timeout: int | None = None) -> ExecResult:
        """Run ``command`` and return an :class:`ExecResult`.

        This is the single entry point the agent's terminal tool calls,
        regardless of backend. Mirror of ``BaseEnvironment.execute``: it fires
        the file-sync hook, wraps the command with the target CWD, then defers
        to the backend-specific ``_run_bash``.
        """
        self._before_execute()
        effective_cwd = cwd or self.cwd
        effective_timeout = timeout or self.timeout
        wrapped = self._wrap_command(command, effective_cwd)
        return self._run_bash(wrapped, timeout=effective_timeout)

    # -- file ops, implemented *through* execute() --------------------------
    # In Hermes these live in tools/file_operations.py and route through the
    # active Environment (cat to read, heredoc to write), so they too are
    # backend-agnostic. We mirror that: read_file/write_file call execute().

    def write_file(self, path: str, content: str) -> WriteResult:
        """Write ``content`` to ``path`` inside this environment via a heredoc.

        Backend-agnostic: works on local/Docker/SSH/Modal identically because it
        goes through ``execute``. Mirrors file_operations.write_file's heredoc.
        """
        delimiter = f"HERMES_EOF_{uuid.uuid4().hex[:8]}"
        cmd = f"cat > {shlex.quote(path)} << '{delimiter}'\n{content}\n{delimiter}"
        res = self.execute(cmd)
        return WriteResult(path=path, bytes_written=len(content.encode()), returncode=res.returncode)

    def read_file(self, path: str) -> ReadResult:
        """Read ``path`` from inside this environment via ``cat``.

        Backend-agnostic counterpart to :meth:`write_file`. Mirrors
        file_operations.read_file routing through the active Environment.
        """
        res = self.execute(f"cat {shlex.quote(path)}")
        return ReadResult(content=res.output, returncode=res.returncode)


# ---------------------------------------------------------------------------
# LocalEnvironment — REAL subprocess execution (mirror of local.py)
# ---------------------------------------------------------------------------


class LocalEnvironment(Environment):
    """Run commands directly on the host via ``subprocess`` (mirror of local.py).

    No file sync, no container: the host filesystem *is* the environment, so the
    agent's files are already where the command can see them. This is the only
    backend in this module that really executes commands.
    """

    needs_file_sync = False
    serverless = False

    def __init__(self, cwd: str = "", timeout: int = 60, env: dict | None = None) -> None:
        super().__init__(cwd=cwd or os.getcwd(), timeout=timeout, env=env)

    def _run_bash(self, cmd_string: str, *, timeout: int = 120) -> ExecResult:
        """Spawn a real ``bash -c`` subprocess. Mirror of LocalEnvironment._run_bash."""
        run_env = {**os.environ, **self.env}
        try:
            proc = subprocess.run(
                ["bash", "-c", cmd_string],
                capture_output=True,
                text=True,
                timeout=timeout,
                env=run_env,
            )
        except subprocess.TimeoutExpired:
            return ExecResult(output=f"[local] command timed out after {timeout}s", returncode=124)
        output = proc.stdout
        if proc.stderr:
            output = f"{output}\n{proc.stderr}" if output else proc.stderr
        return ExecResult(output=output.rstrip("\n"), returncode=proc.returncode)

    def cleanup(self) -> None:
        """Nothing to release — there is no remote resource."""
        pass


# ---------------------------------------------------------------------------
# _FakeRemoteFS — an in-memory "sandbox filesystem" for the fake backends
# ---------------------------------------------------------------------------


class _FakeRemoteFS:
    """A tiny in-memory filesystem standing in for a remote sandbox's disk.

    Real backends (Docker/SSH/Modal) have a genuinely separate filesystem we
    cannot reach without the actual tooling installed. This fake makes that
    separation observable: writes land here, not on the host, and files only
    appear after a FileSyncManager upload.
    """

    def __init__(self) -> None:
        self.files: dict[str, str] = {}

    def run(self, cmd_string: str) -> ExecResult:
        """Interpret the handful of shell forms our demo emits (cd/cat/echo/ls)."""
        # Our _wrap_command produces: cd <cwd> && <command>
        body = cmd_string.split("&&", 1)[1].strip() if "&&" in cmd_string else cmd_string.strip()

        # write_file form: cat > path << 'EOF' ... EOF
        if body.startswith("cat >"):
            header, *rest = body.split("\n")
            path = shlex.split(header[len("cat >"):].split("<<")[0].strip())[0]
            # content is everything between the heredoc delimiter lines
            content = "\n".join(rest[:-1]) if len(rest) >= 2 else ""
            self.files[path] = content
            return ExecResult(output="", returncode=0)

        # read_file form: cat path
        if body.startswith("cat "):
            path = shlex.split(body[len("cat "):])[0]
            if path in self.files:
                return ExecResult(output=self.files[path], returncode=0)
            return ExecResult(output=f"cat: {path}: No such file or directory", returncode=1)

        # ls form
        if body.startswith("ls"):
            return ExecResult(output="\n".join(sorted(self.files)), returncode=0)

        # echo form (and anything else: just echo the literal)
        if body.startswith("echo "):
            return ExecResult(output=body[len("echo "):].strip("'\""), returncode=0)

        return ExecResult(output=f"[fake-remote] ran: {body}", returncode=0)


class _FakeRemoteBackend(Environment):
    """Shared base for the fake remote backends (Docker/SSH/Modal).

    Holds the in-memory sandbox FS and a label used in log/output lines so the
    demo can show *which* backend handled each call.
    """

    label = "remote"

    def __init__(self, cwd: str = "/root", timeout: int = 60) -> None:
        super().__init__(cwd=cwd, timeout=timeout)
        self._fs = _FakeRemoteFS()

    def _run_bash(self, cmd_string: str, *, timeout: int = 120) -> ExecResult:
        return self._fs.run(cmd_string)

    def cleanup(self) -> None:
        pass


# ---------------------------------------------------------------------------
# DockerEnvironment (fake) — bind-mount backend, NO file sync (mirror docker.py)
# ---------------------------------------------------------------------------


class DockerEnvironment(_FakeRemoteBackend):
    """Fake Docker backend (mirror of docker.py).

    Key trait carried over from Hermes: Docker bind-mounts the host workspace
    into the container, so the container sees host files live. That means
    ``needs_file_sync = False`` — no FileSyncManager. Commands run via
    ``docker exec`` (simulated here). cleanup() removes the container.
    """

    needs_file_sync = False
    serverless = False
    label = "docker"

    def __init__(self, image: str = "python:3.12", cwd: str = "/workspace", timeout: int = 60) -> None:
        super().__init__(cwd=cwd, timeout=timeout)
        self.image = image
        self._container_id = f"hermes-{uuid.uuid4().hex[:10]}"

    def start(self) -> None:
        """Simulate ``docker run`` then session bootstrap."""
        super().start()

    def cleanup(self) -> None:
        """Simulate ``docker rm -f`` of the container."""
        self._container_id = ""


# ---------------------------------------------------------------------------
# SSHEnvironment (fake) — remote host, USES FileSyncManager (mirror ssh.py)
# ---------------------------------------------------------------------------


class SSHEnvironment(_FakeRemoteBackend):
    """Fake SSH backend (mirror of ssh.py).

    A remote host's filesystem is NOT visible to the host, so this backend wires
    up a :class:`FileSyncManager` (``needs_file_sync = True``) and flushes it in
    ``_before_execute`` — exactly as ssh.py does — uploading changed host files
    over (simulated) scp before each command. cleanup() pulls changes back.
    """

    needs_file_sync = True
    serverless = False
    label = "ssh"

    def __init__(self, host: str, user: str = "root", cwd: str = "~", timeout: int = 60,
                 host_dir: str | None = None) -> None:
        super().__init__(cwd=cwd, timeout=timeout)
        self.host = host
        self.user = user
        # Directory of host files this backend keeps in sync with the sandbox.
        self._host_dir = host_dir
        self._sync_manager: FileSyncManager | None = None
        if host_dir is not None:
            self._sync_manager = FileSyncManager(
                get_files_fn=self._iter_sync_files,
                upload_fn=self._scp_upload,
                delete_fn=self._ssh_delete,
            )

    def start(self) -> None:
        super().start()
        if self._sync_manager:
            # Force an initial full sync (mirror modal.py / ssh init).
            self._sync_manager.sync(force=True)

    def _iter_sync_files(self) -> list[tuple[str, str]]:
        """Enumerate host files to mirror into the sandbox (mirror file_sync.iter_sync_files)."""
        out: list[tuple[str, str]] = []
        if not self._host_dir or not os.path.isdir(self._host_dir):
            return out
        for name in os.listdir(self._host_dir):
            hp = os.path.join(self._host_dir, name)
            if os.path.isfile(hp):
                out.append((hp, f"{self.cwd}/{name}"))
        return out

    def _scp_upload(self, host_path: str, remote_path: str) -> None:
        """Simulated scp: copy host file bytes into the sandbox FS (mirror ssh._scp_upload)."""
        with open(host_path, "r") as f:
            self._fs.files[remote_path] = f.read()

    def _ssh_delete(self, remote_paths: list[str]) -> None:
        """Simulated remote rm (mirror ssh._ssh_delete)."""
        for p in remote_paths:
            self._fs.files.pop(p, None)

    def _before_execute(self) -> None:
        """Flush pending host->sandbox file sync before each command (mirror ssh._before_execute)."""
        if self._sync_manager:
            self._sync_manager.sync()

    def cleanup(self) -> None:
        """On teardown, pull remote changes back (mirror ssh.cleanup -> sync_back)."""
        pass


# ---------------------------------------------------------------------------
# ModalEnvironment (fake) — SERVERLESS hibernate/wake (mirror modal.py)
# ---------------------------------------------------------------------------

# Module-level snapshot store standing in for modal_snapshots.json on disk.
# Maps task_id -> snapshot payload (the sandbox FS at hibernate time).
_SNAPSHOT_STORE: dict[str, dict[str, str]] = {}


class ModalEnvironment(_FakeRemoteBackend):
    """Fake Modal serverless backend (mirror of modal.py + managed_modal.py).

    The defining behaviour: a Modal sandbox is ephemeral. To fake a long-lived
    machine across sessions, Hermes does hibernate/wake:

      * **wake (on construct)**: if a filesystem snapshot exists for this
        ``task_id``, restore the sandbox from it instead of the base image.
      * **hibernate (on cleanup)**: snapshot the sandbox filesystem and persist
        the snapshot id keyed by ``task_id``, then terminate the sandbox.

    Like SSH it is remote, so it also runs a FileSyncManager. Setting
    ``serverless = True`` flags the hibernate/wake lifecycle.
    """

    needs_file_sync = True
    serverless = True
    label = "modal"

    def __init__(self, image: str = "python:3.12", cwd: str = "/root", timeout: int = 60,
                 task_id: str = "default", persistent_filesystem: bool = True,
                 host_dir: str | None = None) -> None:
        super().__init__(cwd=cwd, timeout=timeout)
        self.image = image
        self._task_id = task_id
        self._persistent = persistent_filesystem
        self.restored_from_snapshot = False

        # --- WAKE: restore from snapshot if one exists (mirror modal.py __init__) ---
        if self._persistent and task_id in _SNAPSHOT_STORE:
            self._fs.files = dict(_SNAPSHOT_STORE[task_id])
            self.restored_from_snapshot = True

        # Remote => file sync. Wire a manager if a host dir is provided.
        self._host_dir = host_dir
        self._sync_manager: FileSyncManager | None = None
        if host_dir is not None:
            self._sync_manager = FileSyncManager(
                get_files_fn=self._iter_sync_files,
                upload_fn=self._modal_upload,
                delete_fn=self._modal_delete,
            )

    def start(self) -> None:
        super().start()
        if self._sync_manager:
            self._sync_manager.sync(force=True)

    def _iter_sync_files(self) -> list[tuple[str, str]]:
        out: list[tuple[str, str]] = []
        if not self._host_dir or not os.path.isdir(self._host_dir):
            return out
        for name in os.listdir(self._host_dir):
            hp = os.path.join(self._host_dir, name)
            if os.path.isfile(hp):
                out.append((hp, f"{self.cwd}/{name}"))
        return out

    def _modal_upload(self, host_path: str, remote_path: str) -> None:
        with open(host_path, "r") as f:
            self._fs.files[remote_path] = f.read()

    def _modal_delete(self, remote_paths: list[str]) -> None:
        for p in remote_paths:
            self._fs.files.pop(p, None)

    def _before_execute(self) -> None:
        if self._sync_manager:
            self._sync_manager.sync()

    def hibernate(self) -> str:
        """Snapshot the sandbox FS and persist it under ``task_id``; return snapshot id.

        Mirror of the snapshot half of modal.py ``cleanup`` (``snapshot_filesystem``
        + ``_store_direct_snapshot``). After this, a future ModalEnvironment with
        the same ``task_id`` wakes with this filesystem.
        """
        snapshot_id = f"snap-{uuid.uuid4().hex[:10]}"
        if self._persistent:
            _SNAPSHOT_STORE[self._task_id] = dict(self._fs.files)
        return snapshot_id

    def cleanup(self) -> None:
        """Hibernate (snapshot) if persistent, then terminate the sandbox.

        Mirror of modal.py ``cleanup``: sync_back, snapshot_filesystem, terminate.
        """
        if self._persistent:
            self.hibernate()


# ---------------------------------------------------------------------------
# Backend registry — mirror of terminal_tool._create_environment factory
# ---------------------------------------------------------------------------

BACKENDS: dict[str, type[Environment]] = {
    "local": LocalEnvironment,
    "docker": DockerEnvironment,
    "ssh": SSHEnvironment,
    "modal": ModalEnvironment,
}


def create_environment(kind: str, **kwargs) -> Environment:
    """Factory that selects a backend by name.

    Mirror of ``terminal_tool._create_environment`` which picks the backend from
    the ``TERMINAL_ENV`` config. The agent calls this once, then uses only the
    :class:`Environment` interface afterward.
    """
    if kind not in BACKENDS:
        raise ValueError(f"unknown backend {kind!r}; choose from {sorted(BACKENDS)}")
    return BACKENDS[kind](**kwargs)


__all__ = [
    "ExecResult",
    "ReadResult",
    "WriteResult",
    "SyncStats",
    "FileSyncManager",
    "Environment",
    "LocalEnvironment",
    "DockerEnvironment",
    "SSHEnvironment",
    "ModalEnvironment",
    "BACKENDS",
    "create_environment",
]
