# Source this at the start of every lab sitting (bash or zsh), from the repo root:
#
#   source lab/env.sh
#
# It only sets variables and defines a few shell functions. Nothing here prints a secret unless you ask for it,
# and the functions that return one (bao_field, api_token) are meant to be used inside $( ... ) only.

export BAO_ADDR="${BAO_ADDR:-http://localhost:8200}"
export API="http://localhost:8000"                      # REST API (FastAPI + DuckDB)
export MCP="http://localhost:8001/mcp"                  # MCP server (Streamable HTTP)
export CAT="http://localhost:8181/catalog/v1"           # Iceberg REST catalog (Lakekeeper)
export BAO_TOKEN="$(cat .bao/dev-token 2>/dev/null)"    # developer token written by `mise run up`
[ -n "$BAO_TOKEN" ] || echo "lab/env.sh: no .bao/dev-token - run this from the repo root after 'mise run up'" >&2

# OpenBao ---------------------------------------------------------------------------------------------------
# bao_keys ec2-lakehouse/agent          -> which fields a secret has, and its version (never the values)
bao_keys() {
  curl -s -H "X-Vault-Token: $BAO_TOKEN" "$BAO_ADDR/v1/secret/data/$1" \
    | jq -c 'if .errors then {errors} else {fields: (.data.data | keys), version: .data.metadata.version} end'
}
# PW=$(bao_field ec2-lakehouse/agent password)   -> one value, for use inside $( ... )
bao_field() {
  curl -s -H "X-Vault-Token: $BAO_TOKEN" "$BAO_ADDR/v1/secret/data/$1" | jq -r --arg f "$2" '.data.data[$f] // empty'
}

# S3 (SeaweedFS) --------------------------------------------------------------------------------------------
# s3curl /                              -> list buckets          s3curl 'raw?list-type=2&max-keys=5'
# curl signs the request (AWS SigV4) with the keys from OpenBao. Write '=' in object keys as %3D: curl 8.x signs
# the raw '=' while S3 canonicalizes it as %3D, and the signatures stop matching.
s3curl() {
  curl -s --aws-sigv4 "aws:amz:us-east-1:s3" \
    --user "$(bao_field ec2-lakehouse/s3 access_key):$(bao_field ec2-lakehouse/s3 secret_key)" "${@:2}" "http://localhost:9000/${1#/}"
}

# Catalog ---------------------------------------------------------------------------------------------------
# The Iceberg REST paths need the warehouse's ID ("prefix"), which /config hands out.
cat_prefix() { curl -s "$CAT/config?warehouse=lakehouse" | jq -r .defaults.prefix; }
# table_state                           -> ec2.instances: current snapshot, counts, and the last 3 commits
table_state() {
  curl -s "$CAT/$(cat_prefix)/namespaces/ec2/tables/instances" | jq '.metadata as $m | {
    current_snapshot: $m."current-snapshot-id", snapshots: ($m.snapshots | length),
    total_records: ($m.snapshots[] | select(."snapshot-id" == $m."current-snapshot-id") | .summary."total-records"),
    last_commits: [$m.snapshots | sort_by(."sequence-number") | .[-3:][] | .summary as $s |
      "#\(."sequence-number") \($s.operation) +\($s."added-records" // 0) -\($s."deleted-records" // 0) total=\($s."total-records") by=\($s."etl.day" // $s."etl.days" // $s.lab)"]}'
}

# API -------------------------------------------------------------------------------------------------------
# TOKEN=$(api_token)                    -> agent user, audience ec2-api
# TOKEN=$(api_token ec2-mcp)            -> agent user, audience ec2-mcp (what the agents send to the MCP server)
# TOKEN=$(api_token ec2-api admin)      -> admin user
api_token() {
  local aud="${1:-ec2-api}" user="${2:-agent}"
  curl -s "$API/auth/token" -d "username=$user" --data-urlencode "password=$(bao_field "ec2-lakehouse/$user" password)" \
    -d "audience=$aud" | jq -r '.access_token // empty'
}
# jwt_claims "$TOKEN"                   -> the token's payload (header.PAYLOAD.signature, base64url-decoded)
jwt_claims() {
  python3 -c 'import base64, json, sys
p = sys.argv[1].split(".")[1]
print(json.dumps(json.loads(base64.urlsafe_b64decode(p + "=" * (-len(p) % 4))), indent=2))' "$1"
}

# MCP -------------------------------------------------------------------------------------------------------
# export MCP_TOKEN=$(api_token ec2-mcp)
# mcp_rpc tools/list                                    -> one JSON-RPC request, response body
# mcp_rpc tools/call '{"name":"fleet_summary","arguments":{"group_by":"region"}}'
#
# Speaks the stateless 2026-07-28 protocol: no initialize handshake; every request carries the protocol version
# and client capabilities in params._meta, and mirrors method/name into Mcp-Method/Mcp-Name headers so a load
# balancer can route without reading the body. MCP_VERBOSE=1 shows the HTTP exchange.
MCP_VERSION="${MCP_VERSION:-2026-07-28}"
mcp_rpc() {
  local method="$1" params="${2:-}" body name
  [ -n "$params" ] || params='{}'
  body=$(jq -nc --arg m "$method" --arg v "$MCP_VERSION" --argjson p "$params" '{jsonrpc: "2.0", id: 1, method: $m,
    params: ($p + {_meta: {"io.modelcontextprotocol/protocolVersion": $v,
                           "io.modelcontextprotocol/clientCapabilities": {},
                           "io.modelcontextprotocol/clientInfo": {name: "lab-curl", version: "0"}}})}')
  name=$(jq -r '.name // .uri // empty' <<<"$params")
  local -a extra=()                       # an array, so the same code works in bash and zsh
  [ -n "$name" ] && extra+=(-H "Mcp-Name: $name")
  [ -n "${MCP_VERBOSE:-}" ] && extra+=(-v)
  curl -s "${extra[@]}" "$MCP" -H "Authorization: Bearer $MCP_TOKEN" -H "Content-Type: application/json" \
    -H "Accept: application/json, text/event-stream" -H "MCP-Protocol-Version: $MCP_VERSION" \
    -H "Mcp-Method: $method" -d "$body"
}
