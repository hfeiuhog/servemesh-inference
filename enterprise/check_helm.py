import json
import sys
from pathlib import Path
import yaml

objects = list(yaml.safe_load_all(Path(sys.argv[1]).read_text()))
policy = next(
    o
    for o in objects
    if o and o.get("kind") == "ConfigMap" and "tenants" in str(o.get("data", {}))
)
config = json.loads(next(iter(policy["data"].values())))
assert set(config["tenants"]) == {"synthetic-finance", "synthetic-operations"}
deployments = [o for o in objects if o and o.get("kind") == "Deployment"]
router = next(
    o
    for o in deployments
    if any(
        "SERVEMESH_POLICY_FILE" == e.get("name")
        for c in o["spec"]["template"]["spec"]["containers"]
        for e in c.get("env", [])
    )
)
pod = router["spec"]["template"]["spec"]
container = router["spec"]["template"]["spec"]["containers"][0]
env = {e["name"]: e for e in container["env"]}
assert "secretKeyRef" in env["SERVEMESH_SIGNING_KEY"]["valueFrom"]
assert "tenant-affinity" in str(container["args"])
assert any(
    v.get("configMap", {}).get("name") == policy["metadata"]["name"]
    for v in pod["volumes"]
)
print("Helm policy, signed-context Secret reference and tenant-affinity route verified")
