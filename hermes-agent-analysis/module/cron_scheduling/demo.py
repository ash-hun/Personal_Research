"""
demo.py — drive the cron_scheduling mirror with simulated, advancing time.

    python3 demo.py

No external deps, and — importantly — NO real-time sleeping. We advance a fake
clock by passing an explicit ``now`` into every scheduler tick so due jobs fire
deterministically. A fake ``run_agent`` produces output, and a fake ``deliver``
prints "delivered to telegram: ...".
"""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone

from cron_scheduling import (
    AgentRunResult,
    DeliveryResult,
    JobStore,
    Scheduler,
    cronjob_tool,
)


# -----------------------------------------------------------------------------
# Pluggable callables (stand-ins for Hermes' live agent + platform adapters)
# -----------------------------------------------------------------------------

def fake_run_agent(job, now: datetime) -> AgentRunResult:
    """Pretend to run the agent on the job's prompt and return some output."""
    response = f"[{now.strftime('%H:%M')}] result for task: {job.prompt!r}"
    return AgentRunResult(success=True, final_response=response, output=response)


def fake_deliver(job, content: str) -> DeliveryResult:
    """Route output to a platform. Mirrors how Hermes hands off to a gateway adapter."""
    target = job.deliver
    # "origin" resolves to the chat the job was created in (job.origin).
    if target == "origin" and job.origin:
        target = f"{job.origin['platform']}:{job.origin['chat_id']}"
    platform = target.split(":", 1)[0] if ":" in target else target
    print(f"    -> delivered to {platform}: {content}")
    return DeliveryResult(delivered=True, target=target)


def banner(title: str) -> None:
    print("\n" + "=" * 68)
    print(title)
    print("=" * 68)


def main() -> None:
    # Anchor a fake clock. Everything below advances THIS, never real time.
    t0 = datetime(2026, 6, 3, 8, 0, tzinfo=timezone.utc)

    store = JobStore()  # in-memory store
    scheduler = Scheduler(store, run_agent=fake_run_agent, deliver=fake_deliver)

    # -------------------------------------------------------------------------
    # 1) Create jobs via the agent-facing tool (as the LLM would)
    # -------------------------------------------------------------------------
    banner("1) Creating cron jobs via cronjob_tool()")

    # A recurring interval job, delivered to the origin chat it was created in.
    res_interval = cronjob_tool(
        "create",
        store,
        prompt="summarize today's unread emails",
        schedule="every 30m",
        name="email digest",
        origin={"platform": "telegram", "chat_id": "12345"},
        now=t0,
    )
    print(res_interval)

    # A daily cron job at 09:00, delivered explicitly to a telegram channel.
    res_cron = cronjob_tool(
        "create",
        store,
        prompt="post the daily report",
        schedule="0 9 * * *",
        name="daily report",
        deliver="telegram:-1009999",
        now=t0,
    )
    print(res_cron)

    # A one-shot reminder 45 minutes out (auto repeat=1).
    res_once = cronjob_tool(
        "create",
        store,
        prompt="remind me to stretch",
        schedule="45m",
        name="stretch reminder",
        deliver="telegram:777",
        now=t0,
    )
    print(res_once)

    # -------------------------------------------------------------------------
    # 2) List jobs
    # -------------------------------------------------------------------------
    banner("2) Listing jobs via cronjob_tool('list')")
    print(cronjob_tool("list", store))

    # -------------------------------------------------------------------------
    # 3) Drive the scheduler over advancing simulated timestamps
    # -------------------------------------------------------------------------
    banner("3) Ticking the scheduler with simulated time (no real sleep)")

    # Tick every 15 simulated minutes from 08:00 to 09:30.
    # Expected fires:
    #   08:30  email digest (interval +30m)
    #   08:45  stretch reminder (one-shot at 08:45) -> then auto-removed
    #   09:00  email digest (next interval) + daily report (cron 0 9 * * *)
    #   09:30  email digest
    now = t0
    end = t0 + timedelta(hours=1, minutes=30)
    while now <= end:
        report = scheduler.tick(now, verbose=True)
        if not report.due_job_ids:
            print(f"  [tick {now.strftime('%H:%M')}] nothing due")
        else:
            print(
                f"  [tick {now.strftime('%H:%M')}] executed={report.executed} "
                f"delivered={report.delivered}"
            )
        now += timedelta(minutes=15)

    # -------------------------------------------------------------------------
    # 4) Final state — one-shot should be gone, recurring jobs remain
    # -------------------------------------------------------------------------
    banner("4) Final job state")
    final = json.loads(cronjob_tool("list", store, include_disabled=True))
    for j in final["jobs"]:
        print(
            f"  - {j['name']:<16} state={j['state']:<10} "
            f"last_status={j['last_status']} next_run_at={j['next_run_at']}"
        )
    remaining_names = sorted(j["name"] for j in final["jobs"])
    print(f"\n  remaining jobs: {remaining_names}")
    assert "stretch reminder" not in remaining_names, "one-shot should auto-remove after firing"
    print("  OK: one-shot auto-removed; recurring jobs persisted.")


if __name__ == "__main__":
    main()
