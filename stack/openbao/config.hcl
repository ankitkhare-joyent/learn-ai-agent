# OpenBao server config (local). Production differences are called out inline.
ui = true

# Integrated Raft storage: no external DB. Prod: 3 or 5 nodes for HA.
storage "raft" {
  path    = "/openbao/file"
  node_id = "bao-1"
}

# Prod: TLS on (tls_cert_file / tls_key_file); never plain HTTP outside a laptop.
listener "tcp" {
  address     = "0.0.0.0:8200"
  tls_disable = true
}

api_addr      = "http://openbao:8200"
cluster_addr  = "http://openbao:8201"
disable_mlock = true

# Prod: auto-unseal with a cloud KMS so restarts don't need a human with an unseal key:
# seal "awskms" { region = "us-east-1"  kms_key_id = "alias/openbao-unseal" }
