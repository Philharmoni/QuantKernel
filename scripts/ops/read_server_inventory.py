"""Read remote raw-data metadata over SSH without modifying the server."""
import argparse
import json
from pathlib import Path
import shlex
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from core.config import load_paths


REMOTE_SCRIPT = r'''
from datetime import datetime, timezone
import hashlib
import json
import os
from pathlib import Path
import sys

root = Path(sys.argv[1])
hash_files = sys.argv[2] == "sha256"
if not root.is_dir():
    raise SystemExit(f"Remote DATA_ROOT does not exist or is not a directory: {root}")

def fail(error):
    raise error

files = []
symlinks = []
for directory, child_directories, names in os.walk(root, onerror=fail, followlinks=False):
    for name in sorted(names):
        path = Path(directory) / name
        relative = path.relative_to(root).as_posix()
        if path.is_symlink():
            symlinks.append(relative)
        item = {"relative_path": relative, "size_bytes": path.stat().st_size}
        if hash_files:
            digest = hashlib.sha256()
            with path.open("rb") as stream:
                for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b""):
                    digest.update(chunk)
            item["sha256"] = digest.hexdigest()
        files.append(item)
    for name in child_directories:
        path = Path(directory) / name
        if path.is_symlink():
            symlinks.append(path.relative_to(root).as_posix())
files.sort(key=lambda item: item["relative_path"])
print(json.dumps({
    "root": str(root),
    "generated_at_utc": datetime.now(timezone.utc).isoformat(),
    "directories": sorted(path.name for path in root.iterdir() if path.is_dir()),
    "file_count": len(files),
    "total_bytes": sum(item["size_bytes"] for item in files),
    "symlinks": sorted(symlinks),
    "hash_algorithm": "sha256" if hash_files else None,
    "files": files,
}, ensure_ascii=False))
'''


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--root", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--sha256", action="store_true",
                        help="Read every source file and include its SHA-256 digest")
    args = parser.parse_args()
    paths = load_paths()
    output = args.output.resolve()
    if output == paths.middle_root or not output.is_relative_to(paths.middle_root):
        parser.error("--output must be a file inside configured MIDDLE_ROOT; raw data is read-only")
    output = paths.output(*output.relative_to(paths.middle_root).parts)
    try:
        completed = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", args.host,
             "python3 - " + shlex.quote(args.root) + " " +
             ("sha256" if args.sha256 else "metadata")],
            input=REMOTE_SCRIPT, text=True, encoding="utf-8", capture_output=True,
            check=True, timeout=1800,
        )
    except subprocess.CalledProcessError as exc:
        raise SystemExit(f"Server inventory failed: {exc.stderr.strip()}") from exc
    inventory = json.loads(completed.stdout)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(inventory, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps({key: value for key, value in inventory.items() if key != "files"},
                     ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
