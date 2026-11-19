import asyncio
import json
from dataclasses import asdict
from threading import Event
from types import SimpleNamespace

import pytest
from click.testing import CliRunner

from agentenv_sandbox_index import campaign, rows
from agentenv_sandbox_index.campaign_plan import HOUR, Plan, epoch, scheduled_window
from agentenv_sandbox_index.cli import bench

PLAN = Plan("pilot", "2027-01-02T12:00:00Z", 2, ["modal_vm"], "a" * 40)
WINDOW = epoch(PLAN.start)
PROVIDER = PLAN.providers[0]


def test_window_and_slot_plan():
    assert Plan.parse(json.dumps(asdict(PLAN))) == PLAN
    assert scheduled_window("2027-01-02T11:45:00.000Z") == WINDOW
    assert PLAN.contains(WINDOW + HOUR)
    assert not PLAN.contains(WINDOW + 2 * HOUR)
    assert not PLAN.contains(WINDOW + 1)
    for metric, count in {"S1": 10, "R1": 12}.items():
        slots = list(campaign.slots(PLAN, PROVIDER, metric, WINDOW))
        assert slots == list(campaign.slots(PLAN, PROVIDER, metric, WINDOW))
        assert len(slots) == count
        assert len({slot.key for slot in slots}) == count
        period = HOUR // count
        for slot in slots:
            assert slot.start < slot.due < slot.start + period - campaign.LATE_SECONDS


@pytest.mark.parametrize("change", [{"id": "../leak"}, {"hours": 0}, {"providers": ["modal_vm", "modal_vm"]}])
def test_reject_invalid_plan(change):
    with pytest.raises(ValueError):
        Plan.parse(json.dumps(asdict(PLAN) | change))


def test_partial_run_recovers_from_only_checkpoints(tmp_path, monkeypatch):
    source = tmp_path / "checkpoints"
    working = tmp_path / "ephemeral"
    out = tmp_path / "results"
    stored = Event()
    closed = False
    first = {metric: next(campaign.slots(PLAN, PROVIDER, metric, WINDOW)) for metric in ("S1", "R1")}

    async def measure(_backend, provider, run_id, _image):
        return rows.Row(run_id, "sample-s1", provider, "S1", PLAN.start, value=0.5)

    async def reliability(_backend, provider, run_id, _image):
        return rows.Row(run_id, "sample-r1", provider, "R1", PLAN.start, value=1.0)

    async def close():
        nonlocal closed
        closed = True

    async def no_delay(_delay):
        return

    def put(key, payload):
        if key == first["R1"].key:
            assert stored.wait(timeout=5)
            raise OSError("checkpoint upload interrupted")
        path = source / key
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("x") as f:
            json.dump(payload, f)
        if key == first["S1"].key:
            stored.set()

    with monkeypatch.context() as patch:
        patch.setattr(campaign, "slots", lambda _plan, _provider, metric, _window: iter([first[metric]]))
        patch.setattr(campaign, "time", SimpleNamespace(time=lambda: WINDOW))
        patch.setattr(campaign.asyncio, "sleep", no_delay)
        patch.setattr(campaign, "build_sandbox_provider", lambda _provider: SimpleNamespace(close=close))
        patch.setattr(campaign, "MEASURES", {"S1": measure, "R1": reliability})
        with pytest.raises(ExceptionGroup) as interrupted:
            asyncio.run(campaign.run_window(PLAN, WINDOW, PROVIDER, None, working, put))
        assert isinstance(interrupted.value.exceptions[0], OSError)
    assert closed
    assert not (source / first["R1"].key).exists()

    plan_file = tmp_path / "plan.json"
    plan_file.write_text(json.dumps(asdict(PLAN)))
    result = CliRunner().invoke(campaign.main, ["collect", str(plan_file), str(source), str(out)])
    assert result.exit_code == 1, result.output
    assert "Campaign is incomplete" in result.output
    coverage = json.loads((out / "coverage.json").read_text())
    assert coverage["expected"] == 44
    assert coverage["collected"] == 1
    assert coverage["outcomes"] == {"ok": 1}
    assert first["R1"].key in coverage["missing"]
    [run] = list((out / "runs" / PROVIDER).iterdir())
    result = CliRunner().invoke(bench, ["score", str(run), "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output) == json.loads((out / "score.json").read_text())


def test_checkpoint_is_conditional_and_uses_scoped_credentials(monkeypatch):
    boto3 = pytest.importorskip("boto3")
    captured = {}

    def put_object(**kwargs):
        captured.update(kwargs)

    def client(service, **kwargs):
        assert service == "s3"
        assert kwargs["endpoint_url"] == "https://t3.storage.dev"
        assert kwargs["aws_access_key_id"] == "test-access"
        assert kwargs["aws_secret_access_key"] == "test-secret"
        return SimpleNamespace(put_object=put_object)

    monkeypatch.setattr(boto3, "client", client)
    monkeypatch.setenv("AGENTENV_RESULTS_BUCKET", "private-test-bucket")
    monkeypatch.setenv("AGENTENV_RESULTS_ACCESS_KEY_ID", "test-access")
    monkeypatch.setenv("AGENTENV_RESULTS_SECRET_ACCESS_KEY", "test-secret")
    write = campaign.tigris_writer(PLAN)
    write("modal_vm/slots/S1/a.json", {"test": 1})
    assert captured == {
        "Bucket": "private-test-bucket",
        "Key": "sandbox-index/pilot/modal_vm/slots/S1/a.json",
        "Body": b'{"test":1}',
        "ContentType": "application/json",
        "IfNoneMatch": "*",
    }
