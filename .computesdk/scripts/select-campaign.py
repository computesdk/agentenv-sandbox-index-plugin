import json
import os
import time
from dataclasses import asdict
from pathlib import Path

from agentenv_sandbox_index.campaign_plan import HOUR, Plan, epoch, iso, scheduled_window

plan_file = Path(".computesdk/campaign.json")
scheduled = os.environ["EVENT_NAME"] == "schedule"
raw = os.environ["PLAN_INPUT"] if not scheduled else ""
if not raw and plan_file.is_file():
    raw = plan_file.read_text()
if not raw and scheduled:
    print("No active campaign configured")
    raise SystemExit(0)
plan = Plan.parse(raw)
window = scheduled_window(os.environ["COMPUTESDK_SCHEDULED_FOR"]) if scheduled else epoch(os.environ["WINDOW_INPUT"])
if not plan.contains(window) or not window - 30 * 60 <= time.time() < window + HOUR:
    if scheduled:
        print("Scheduled window is outside the campaign or has already expired")
        raise SystemExit(0)
    raise SystemExit("Dispatch a current or imminent UTC hour from the campaign plan")

with open(os.environ["GITHUB_OUTPUT"], "a") as output:
    print("plan=" + json.dumps(asdict(plan), separators=(",", ":")), file=output)
    print("window=" + iso(window), file=output)
