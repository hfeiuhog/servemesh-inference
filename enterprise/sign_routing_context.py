"""Trusted ingress helper. The private signing key must stay outside client apps."""

import argparse
import os
import time
from vllm_router.tenant_policy import sign_context

parser = argparse.ArgumentParser()
parser.add_argument("--tenant", required=True)
parser.add_argument("--session", required=True)
args = parser.parse_args()
expires = int(time.time()) + 120
key = os.environ["SERVEMESH_SIGNING_KEY"].encode()
if len(key) < 32:
    raise ValueError("Use a private signing key of at least 32 bytes")
print("x-servemesh-tenant:", args.tenant)
print("x-servemesh-session:", args.session)
print("x-servemesh-expires:", expires)
print("x-servemesh-signature:", sign_context(key, args.tenant, args.session, expires))
