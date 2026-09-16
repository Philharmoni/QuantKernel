import json

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.inventory import compare_inventories


def main() -> None:
    paths = load_paths()
    root = paths.output("verification")
    local = json.loads((root / "local_inventory.json").read_text(encoding="utf-8"))
    server = json.loads((root / "server_inventory.json").read_text(encoding="utf-8"))
    result = compare_inventories(local, server)
    target = paths.output("verification", "inventory_comparison.json")
    target.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2))
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
