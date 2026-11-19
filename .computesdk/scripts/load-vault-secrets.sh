#!/usr/bin/env bash
set -euo pipefail

: "${NSC_TOKEN:?Configure the Namespace vault token before dispatching}"
vault_dir=$(mktemp -d)
trap 'rm -rf -- "$vault_dir"' EXIT

if [[ "$NSC_TOKEN" == \{* ]]; then
  printf '%s\n' "$NSC_TOKEN" > "$vault_dir/token.json"
else
  jq -n '{bearer_token: env.NSC_TOKEN}' > "$vault_dir/token.json"
fi
unset NSC_TOKEN MODAL_TOKEN_ID MODAL_TOKEN_SECRET

vault() {
  NSC_TOKEN_FILE="$vault_dir/token.json" nsc vault "$@"
}

vault list --output json | jq -er '
  map(select(.deleted_at == null)
    | {name: ([.labels[]? | select(.name == "name") | .value][0] // .description),
       object_id}
    | select(.name == "MODAL_TOKEN_ID" or .name == "MODAL_TOKEN_SECRET")
    | select(.object_id | type == "string" and length > 0))
  | group_by(.name) | map(last)
  | if length != 2 then error("Namespace vault must contain both Modal token entries")
    else .[] | "\(.name)=\(.object_id)"
    end
' > "$vault_dir/envdef"

assignments=$(vault export --envdef "$vault_dir/envdef" --shell=bash)
eval "$assignments"
unset assignments
: "${MODAL_TOKEN_ID:?Namespace vault returned an empty MODAL_TOKEN_ID}"
: "${MODAL_TOKEN_SECRET:?Namespace vault returned an empty MODAL_TOKEN_SECRET}"
printf '::add-mask::%s\n' "$MODAL_TOKEN_ID" "$MODAL_TOKEN_SECRET"

rm -rf -- "$vault_dir"
trap - EXIT
exec "$@"
