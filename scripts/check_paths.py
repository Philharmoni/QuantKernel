import argparse
import json

import _bootstrap  # noqa: F401
from core.config import load_paths


def main() -> None:
    parser = argparse.ArgumentParser(description="Validate raw/derived roots without writing raw data")
    parser.add_argument("--paths")
    parser.add_argument("--profile")
    args = parser.parse_args()
    paths = load_paths(args.paths, args.profile)
    print(json.dumps({"DATA_ROOT": str(paths.data_root), "MIDDLE_ROOT": str(paths.middle_root)}, indent=2))


if __name__ == "__main__":
    main()
