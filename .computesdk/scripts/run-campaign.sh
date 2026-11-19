#!/usr/bin/env bash
set -euo pipefail

bash .computesdk/scripts/prepare-provider.sh "$PROVIDER"
image=$(jq -er --arg name "$PROVIDER" '.[$name].image // ""' .computesdk/providers.json)
exec python -m agentenv_sandbox_index.campaign sample \
  --plan "$PLAN" --window "$WINDOW" --provider "$PROVIDER" --image "$image" --out "runs/$PROVIDER"
