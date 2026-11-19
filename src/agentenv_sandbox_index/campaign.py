"""Run bounded campaign windows and reconstruct scores from private checkpoints."""

import asyncio
import json
import os
import random
import re
import time
import uuid
from collections import Counter
from collections.abc import Callable, Iterator
from dataclasses import asdict, dataclass
from pathlib import Path

import click

from . import methodology, rows
from .campaign_plan import HOUR, Plan, epoch, iso
from .cli import _show
from .runner import MEASURES, build_sandbox_provider
from .summary import summarize

LATE_SECONDS = 30


@dataclass(frozen=True)
class Slot:
    provider: str
    metric: str
    start: int
    due: float

    @property
    def key(self) -> str:
        return f"{self.provider}/slots/{self.metric}/{self.start}.json"


def slots(plan: Plan, provider: str, metric: str, window: int) -> Iterator[Slot]:
    period, count = methodology.CADENCE[metric]
    if count != 1 or HOUR % period:
        raise ValueError(f"{metric} cadence must have one sample per slot and divide an hour")
    seed = f"{plan.id}/{provider}/{metric}"
    margin = 1 + period * methodology.CADENCE_JITTER
    offset = random.Random(seed).uniform(margin, period - margin - LATE_SECONDS)
    for start in range(window, window + HOUR, period):
        jitter = random.Random(f"{seed}/{start}").uniform(1 - margin, margin - 1)
        yield Slot(provider, metric, start, start + offset + jitter)


def tigris_writer(plan: Plan) -> Callable[[str, dict], None]:
    import boto3
    from botocore.config import Config

    names = ("AGENTENV_RESULTS_BUCKET", "AGENTENV_RESULTS_ACCESS_KEY_ID", "AGENTENV_RESULTS_SECRET_ACCESS_KEY")
    if missing := [name for name in names if not os.environ.get(name)]:
        raise ValueError("Configure campaign storage: " + ", ".join(missing))
    bucket, access_key, secret_key = (os.environ.pop(name) for name in names)
    client = boto3.client(
        "s3",
        endpoint_url="https://t3.storage.dev",
        region_name="auto",
        aws_access_key_id=access_key,
        aws_secret_access_key=secret_key,
        config=Config(retries={"total_max_attempts": 3}, connect_timeout=5, read_timeout=10),
    )

    def put(path: str, payload: dict) -> None:
        client.put_object(
            Bucket=bucket,
            Key=f"sandbox-index/{plan.id}/{path}",
            Body=json.dumps(payload, separators=(",", ":")).encode(),
            ContentType="application/json",
            IfNoneMatch="*",
        )

    return put


async def run_window(
    plan: Plan, window: int, provider: str, image: str | None, out: Path, put: Callable[[str, dict], None]
) -> None:
    if not plan.contains(window) or provider not in plan.providers:
        raise ValueError("Window or provider is outside the campaign plan")
    if not window - 30 * 60 <= time.time() < window + HOUR:
        raise ValueError("Only dispatch current or imminent windows; expired samples cannot be replayed")
    out.mkdir(parents=True, exist_ok=False)
    run_id = f"{iso(window).replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:12]}"
    rows.write_manifest(out, run_id, [provider], list(MEASURES))
    manifest_path = out / rows.MANIFEST
    manifest = json.loads(manifest_path.read_text())
    manifest["campaign"] = {
        "plan": asdict(plan),
        "window": iso(window),
        "image": image,
        "actions_run": os.environ.get("COMPUTESDK_RUN_ID"),
    }
    manifest_path.write_text(json.dumps(manifest, indent=2) + "\n")
    await asyncio.to_thread(put, f"{provider}/manifests/{run_id}.json", manifest)
    backend = build_sandbox_provider(provider)
    missed = []

    async def sample_metric(metric: str) -> None:
        for slot in slots(plan, provider, metric, window):
            await asyncio.sleep(max(0, slot.due - time.time()))
            if time.time() > slot.due + LATE_SECONDS:
                missed.append(slot.key)
                click.echo(f"{provider} {metric} missed slot at {iso(slot.start)}", err=True)
                continue
            row = await MEASURES[metric](backend, provider, run_id, image)
            rows.append(out, row)
            await asyncio.to_thread(put, slot.key, {"row": asdict(row), "due": slot.due})
            _show(row)

    try:
        async with asyncio.TaskGroup() as group:
            for metric in MEASURES:
                group.create_task(sample_metric(metric))
    finally:
        await backend.close()
    if missed:
        raise ValueError(f"Missed {len(missed)} planned samples for {provider}")


def collect(plan: Plan, source: Path, out: Path) -> dict:
    out.mkdir(parents=True, exist_ok=False)
    missing = []
    outcomes: Counter = Counter()
    records = []
    manifests: dict[tuple[str, str], tuple[Path, int]] = {}
    by_provider_metric: dict[str, Counter] = {}
    start = epoch(plan.start)
    for window in range(start, start + plan.hours * HOUR, HOUR):
        for provider in plan.providers:
            for metric in MEASURES:
                counts = by_provider_metric.setdefault(f"{provider}/{metric}", Counter())
                for slot in slots(plan, provider, metric, window):
                    path = source / slot.key
                    if not path.is_file():
                        missing.append(slot.key)
                        counts["missing"] += 1
                        continue
                    checkpoint = json.loads(path.read_text())
                    row = rows.Row.from_dict(checkpoint["row"])
                    if (
                        (row.provider, row.metric, row.schema) != (provider, metric, rows.SCHEMA)
                        or checkpoint["due"] != slot.due
                        or not re.fullmatch(r"[\w-]+", row.run_id)
                    ):
                        raise ValueError(f"Invalid checkpoint: {slot.key}")
                    key = (provider, row.run_id)
                    if key not in manifests:
                        manifest = json.loads((source / provider / "manifests" / f"{row.run_id}.json").read_text())
                        if (
                            manifest["campaign"]["plan"] != asdict(plan)
                            or manifest["campaign"]["window"] != iso(window)
                            or manifest["run_id"] != row.run_id
                            or manifest["providers"] != [provider]
                            or manifest["methodology"] != methodology.VERSION
                        ):
                            raise ValueError(f"Unexpected manifest for {slot.key}")
                        dest = out / "runs" / provider / row.run_id
                        dest.mkdir(parents=True)
                        (dest / rows.MANIFEST).write_text(json.dumps(manifest, indent=2) + "\n")
                        manifests[key] = dest, window
                    dest, manifest_window = manifests[key]
                    if manifest_window != window:
                        raise ValueError(f"Manifest spans multiple windows: {slot.key}")
                    rows.append(dest, row)
                    records.append(row)
                    outcomes[row.outcome] += 1
                    counts[row.outcome] += 1
    report = {
        "expected": len(records) + len(missing),
        "collected": len(records),
        "outcomes": dict(outcomes),
        "by_provider_metric": by_provider_metric,
        "missing": missing,
    }
    (out / "campaign.json").write_text(json.dumps(asdict(plan), indent=2) + "\n")
    (out / "coverage.json").write_text(json.dumps(report, indent=2) + "\n")
    if records:
        score = {"methodology": methodology.VERSION, "summaries": [asdict(s) for s in summarize(records)]}
        (out / "score.json").write_text(json.dumps(score, indent=2) + "\n")
    return report


@click.group()
def main():
    """Operate a private Sandbox Index campaign."""


@main.command()
@click.option("--plan", "plan_json", required=True)
@click.option("--window", required=True)
@click.option("--provider", required=True)
@click.option("--image")
@click.option("--out", required=True, type=click.Path(path_type=Path))
def sample(plan_json: str, window: str, provider: str, image: str | None, out: Path):
    plan = Plan.parse(plan_json)
    asyncio.run(run_window(plan, epoch(window), provider, image or None, out, tigris_writer(plan)))


@main.command("collect")
@click.argument("plan", type=click.Path(path_type=Path, exists=True, dir_okay=False))
@click.argument("source", type=click.Path(path_type=Path, exists=True, file_okay=False))
@click.argument("out", type=click.Path(path_type=Path))
def collect_command(plan: Path, source: Path, out: Path):
    report = collect(Plan.parse(plan.read_text()), source, out)
    click.echo(f"Collected {report['collected']}/{report['expected']} planned samples")
    if report["missing"]:
        raise click.ClickException("Campaign is incomplete; see coverage.json before using these scores")


if __name__ == "__main__":
    main()
