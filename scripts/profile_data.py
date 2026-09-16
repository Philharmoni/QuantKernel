import argparse

import _bootstrap  # noqa: F401
from core.config import load_paths
from core.data.profile import profile_all
from core.data.store import Store


def main():
    parser = argparse.ArgumentParser(description="Read-only full source profiles")
    parser.add_argument("--profile")
    parser.add_argument("--tables", nargs="+")
    args = parser.parse_args()
    with Store(load_paths(profile=args.profile)) as store:
        summary = profile_all(store, args.tables)
        print(f"Passed: {len(summary['tables'])} tables")


if __name__ == "__main__":
    main()
