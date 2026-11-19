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

bash .computesdk/scripts/prepare-provider.sh "$provider"

agent-env bench sandboxes --providers "$provider" --metrics S1,R1 --samples "$samples" --out "runs/$provider" "${args[@]}"
