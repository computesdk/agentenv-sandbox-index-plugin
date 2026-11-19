import json
import os
import subprocess
from pathlib import Path

import pytest

LOADER = Path(__file__).resolve().parents[1] / ".computesdk/scripts/load-vault-secrets.sh"
STORAGE = ("AGENTENV_RESULTS_BUCKET", "AGENTENV_RESULTS_ACCESS_KEY_ID", "AGENTENV_RESULTS_SECRET_ACCESS_KEY")


@pytest.fixture
def vault(tmp_path):
    nsc = tmp_path / "nsc"
    nsc.write_text(
        """#!/usr/bin/env bash
set -euo pipefail
[[ "$1" == vault && -s "$NSC_TOKEN_FILE" && -z "${NSC_TOKEN+x}" ]]
case "$2" in
  list) printf '%s\n' "$MOCK_ENTRIES" ;;
  export) sed 's/=.*$/=test-value/' "$4" ;;
  *) exit 2 ;;
esac
"""
    )
    nsc.chmod(0o700)

    def run(mode, *command, storage=STORAGE):
        names = ("MODAL_TOKEN_ID", "MODAL_TOKEN_SECRET", "TIGRIS_STORAGE_ACCESS_KEY_ID", *storage)
        entries = [{"description": name, "object_id": f"entry-{index}"} for index, name in enumerate(names)]
        env = {key: value for key, value in os.environ.items() if key not in (*names, *STORAGE)}
        env.update(PATH=f"{tmp_path}:{env['PATH']}", NSC_TOKEN="fixture", MOCK_ENTRIES=json.dumps(entries))
        return subprocess.run(
            ["bash", str(LOADER), mode, "modal_vm", *command], env=env, capture_output=True, text=True, check=False
        )

    return run


def test_campaign_preflight_requires_storage_entries(vault):
    result = vault("list-campaign", storage=STORAGE[:1])
    assert result.returncode != 0
    assert all(name in result.stderr for name in STORAGE[1:])
    assert vault("list-campaign").stdout.strip() == '["modal_vm"]'
    assert vault("list", storage=()).stdout.strip() == '["modal_vm"]'


@pytest.mark.parametrize("mode", ["exec", "exec-campaign"])
def test_exports_only_credentials_for_the_requested_mode(vault, mode):
    result = vault(
        mode,
        "bash",
        "-c",
        '[[ -z "${NSC_TOKEN+x}${TIGRIS_STORAGE_ACCESS_KEY_ID+x}" && "$MODAL_TOKEN_ID" == test-value '
        '&& "$MODAL_TOKEN_SECRET" == test-value ]] && printf "%s" '
        '"${AGENTENV_RESULTS_BUCKET+x}${AGENTENV_RESULTS_ACCESS_KEY_ID+x}${AGENTENV_RESULTS_SECRET_ACCESS_KEY+x}"',
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines() == ["::add-mask::test-value"] * 2 + (
        ["::add-mask::test-value"] * 3 + ["xxx"] if mode == "exec-campaign" else []
    )
