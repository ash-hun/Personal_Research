"""Demo: the SAME tool code runs across every Hermes execution backend.

Run with:  python3 demo.py   (stdlib only — LocalEnvironment really shells out)

Demonstrates:
  1. One "agent tool" function driving Local + fake Docker/SSH/Modal identically.
  2. File write/read round-trip through each backend's interface.
  3. The Modal serverless hibernate/wake lifecycle (snapshot persists across
     two separate sessions sharing a task_id).
  4. SSH FileSyncManager mirroring a host file into the remote sandbox.
"""

from __future__ import annotations

import os
import tempfile

from execution_environments import (
    DockerEnvironment,
    Environment,
    LocalEnvironment,
    ModalEnvironment,
    SSHEnvironment,
    create_environment,
)


def hr(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def agent_tool(env: Environment, name: str) -> None:
    """A single piece of 'tool' code. It only knows the Environment interface,
    so it works unchanged on every backend."""
    env.start()
    cmd = env.execute("echo hello from the agent")
    wrote = env.write_file("/tmp/note.txt", "persisted by the agent")
    read = env.read_file("/tmp/note.txt")
    flags = f"file_sync={env.needs_file_sync} serverless={env.serverless}"
    print(f"[{name:7}] {flags}")
    print(f"          exec   -> rc={cmd.returncode} output={cmd.output!r}")
    print(f"          write  -> {wrote.bytes_written} bytes to {wrote.path}")
    print(f"          read   -> {read.content!r}")
    env.stop()


def main() -> int:
    hr("1) SAME tool code, four different backends")
    agent_tool(create_environment("local"), "local")
    agent_tool(create_environment("docker"), "docker")
    agent_tool(create_environment("ssh", host="build-01.internal"), "ssh")
    agent_tool(create_environment("modal", task_id="demo-run"), "modal")

    hr("2) Modal serverless HIBERNATE / WAKE across two sessions")
    # Session A: write a file, then hibernate (snapshot the sandbox FS).
    a = ModalEnvironment(task_id="job-42")
    a.start()
    print(f"  session A: restored_from_snapshot = {a.restored_from_snapshot}")
    a.write_file("/root/state.txt", "checkpoint from session A")
    print("  session A: wrote /root/state.txt, hibernating...")
    a.cleanup()  # snapshot persisted under task_id 'job-42'

    # Session B: brand-new environment, same task_id -> wakes from snapshot.
    b = ModalEnvironment(task_id="job-42")
    b.start()
    print(f"  session B: restored_from_snapshot = {b.restored_from_snapshot}")
    restored = b.read_file("/root/state.txt")
    print(f"  session B: read /root/state.txt -> {restored.content!r}")
    assert restored.content == "checkpoint from session A", "wake should restore FS"
    b.cleanup()
    print("  OK: serverless filesystem survived hibernate/wake")

    hr("3) SSH FileSyncManager mirrors a host file into the remote sandbox")
    with tempfile.TemporaryDirectory() as host_dir:
        host_file = os.path.join(host_dir, "data.csv")
        with open(host_file, "w") as f:
            f.write("id,value\n1,42\n")
        ssh = SSHEnvironment(host="data-host", cwd="/remote", host_dir=host_dir)
        ssh.start()  # forces initial sync: host file uploaded to sandbox
        seen = ssh.read_file("/remote/data.csv")
        print(f"  host wrote  : {host_file}")
        print(f"  sandbox sees: {seen.content!r}")
        assert "42" in seen.content, "sync should have uploaded the host file"
        print("  OK: host file synced into remote sandbox before exec")
        ssh.cleanup()

    hr("4) Docker is bind-mount => NO file sync needed")
    d = DockerEnvironment(image="python:3.12")
    print(f"  needs_file_sync = {d.needs_file_sync} (host FS visible in container)")

    print("\nAll demos passed.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
