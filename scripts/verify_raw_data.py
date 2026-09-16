import argparse
import json

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.inventory import check_key_files, inventory


def main() -> None:
    parser = argparse.ArgumentParser(description="Read-only full file inventory and key-file decoding")
    parser.add_argument("--profile")
    parser.add_argument("--checksums", action="store_true")
    args = parser.parse_args()
    paths = load_paths(profile=args.profile)
    report = inventory(paths.data_root, args.checksums)
    report["key_tables"] = check_key_files(paths.data_root)
    target = paths.output("verification", "local_inventory.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps({k: v for k, v in report.items() if k not in ("files", "key_tables")}, ensure_ascii=False))
    print(f"Key tables readable: {len(report['key_tables'])}; inventory: {target}")


if __name__ == "__main__":
    main()
