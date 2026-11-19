#!/usr/bin/env bash
set -euo pipefail

mode=$1
selection=$2
shift 2
registry="$(dirname "${BASH_SOURCE[0]}")/../providers.json"
providers=$(jq -cer --arg selection "$selection" '
  if $selection == "" then keys_unsorted
  else ($selection | split(",") | map(gsub("^\\s+|\\s+$"; "")) | unique) as $selected
    | ($selected - keys) as $unknown
    | if $unknown == [] then $selected
      else error("Unknown provider(s): " + ($unknown | join(", ")))
      end
  end
' "$registry")

: "${NSC_TOKEN:?Configure the Namespace vault token before dispatching}"
vault_dir=$(mktemp -d)
trap 'rm -rf -- "$vault_dir"' EXIT

if [[ "$NSC_TOKEN" == \{* ]]; then
  printf '%s\n' "$NSC_TOKEN" > "$vault_dir/token.json"
else
  jq -n '{bearer_token: env.NSC_TOKEN}' > "$vault_dir/token.json"
fi
unset NSC_TOKEN

vault() {
  NSC_TOKEN_FILE="$vault_dir/token.json" nsc vault "$@"
}

vault list --output json | jq -e '
  map(select(.deleted_at == null)
    | {name: ([.labels[]? | select(.name == "name") | .value][0] // .description),
       object_id}
    | select(.name | strings | test("^[A-Z][A-Z0-9_]*$"))
    | select(.object_id | type == "string" and length > 0))
  | group_by(.name) | map(last)
  | map({key: .name, value: .object_id}) | from_entries
' > "$vault_dir/entries.json"

storage_keys='[]'
if [[ "$mode" == list-campaign || "$mode" == exec-campaign ]]; then
  storage_keys='["AGENTENV_RESULTS_BUCKET","AGENTENV_RESULTS_ACCESS_KEY_ID","AGENTENV_RESULTS_SECRET_ACCESS_KEY"]'
  jq -er --argjson required "$storage_keys" '
    ($required - keys) as $missing
    | if $missing == [] then true
      else error("Missing Namespace campaign storage entries: " + ($missing | join(", ")))
      end
  ' "$vault_dir/entries.json" >/dev/null
fi

case "$mode" in
  list|list-campaign)
    report=$(jq -ce --argjson providers "$providers" --slurpfile vault "$vault_dir/entries.json" '
      . as $registry
      | $providers
      | map(. as $name | {name: $name, missing: (($registry[$name].secrets | keys) - ($vault[0] | keys))})
    ' "$registry")
    jq -r '.[] | select(.missing != []) | "Skipping \(.name): missing \(.missing | join(", "))"' <<<"$report" >&2
    jq -ce '
      [ .[] | select(.missing == []) | .name ]
      | if length > 0 then . else error("No requested backends have all required Namespace vault entries") end
    ' <<<"$report"
    exit
    ;;
  exec|exec-campaign)
    jq -er --arg name "$selection" --argjson storage "$storage_keys" --slurpfile vault "$vault_dir/entries.json" '
      (((.[$name].secrets | keys) + $storage) | unique)[] as $key
      | if $vault[0][$key] then "\($key)=\($vault[0][$key])"
        else error("Missing Namespace vault entry: " + $key)
        end
    ' "$registry" > "$vault_dir/envdef"
    ;;
  *) printf 'Unsupported vault loader mode: %s\n' "$mode" >&2; exit 2 ;;
esac

mapfile -t keys < <(cut -d= -f1 "$vault_dir/envdef")
unset "${keys[@]}"
assignments=$(vault export --envdef "$vault_dir/envdef" --shell=bash)
eval "$assignments"
unset assignments
for key in "${keys[@]}"; do
  if [[ -z "${!key:-}" ]]; then
    printf 'Namespace vault returned an empty %s\n' "$key" >&2
    exit 1
  fi
  printf '::add-mask::%s\n' "${!key}"
  export "$key"
done

rm -f -- "$vault_dir/token.json" "$vault_dir/envdef" "$vault_dir/entries.json"
jq -r --arg name "$selection" '
  .[$name]
  | (.config // {}) + (.secrets | to_entries | map(select(.value != null) | {key: .value, value: ("env:" + .key)}) | from_entries)
  | if length == 0 then "" else
      "[sandbox.providers.\($name).config]", (to_entries[] | "\(.key) = \(.value | @json)")
    end
' "$registry" > "$vault_dir/config.toml"

AGENT_ENV_CONFIG="$vault_dir/config.toml" "$@"
