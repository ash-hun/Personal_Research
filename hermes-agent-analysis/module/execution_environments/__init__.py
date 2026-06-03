"""Self-contained mirror of Hermes Agent's pluggable execution_environments.

Re-exports the distilled Environment interface and concrete backends. See
execution_environments.py for the full, source-cited implementation and
README.md (Korean) for the conceptual walkthrough.
"""

from execution_environments import (  # noqa: F401
    BACKENDS,
    DockerEnvironment,
    Environment,
    ExecResult,
    FileSyncManager,
    LocalEnvironment,
    ModalEnvironment,
    ReadResult,
    SSHEnvironment,
    SyncStats,
    WriteResult,
    create_environment,
)

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
