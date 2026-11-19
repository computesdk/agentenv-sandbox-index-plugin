#!/usr/bin/env bash
set -euo pipefail

provider=$1
samples=$2
registry="$(dirname "${BASH_SOURCE[0]}")/../providers.json"
image=$(jq -er --arg name "$provider" '.[$name].image // ""' "$registry")
args=()
if [[ -n "$image" ]]; then
  args+=(--image "$image")
fi

if [[ "$provider" == e2b ]]; then
  python - <<'PY'
import asyncio
import os
import tomllib
from pathlib import Path

from agent_env.providers.sandbox_providers.e2b.template import resolve_e2b_template

from agentenv_sandbox_index.methodology import CPU, MEMORY_MB

config = tomllib.loads(Path(os.environ["AGENT_ENV_CONFIG"]).read_text())
template = config["sandbox"]["providers"]["e2b"]["config"]["base_template"]
asyncio.run(resolve_e2b_template(template, api_key=os.environ["E2B_API_KEY"], cpu=CPU, memory_mb=MEMORY_MB))
PY
fi

agent-env bench sandboxes --providers "$provider" --metrics S1,R1 --samples "$samples" --out "runs/$provider" "${args[@]}"
