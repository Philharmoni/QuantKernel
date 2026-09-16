"""Check or verify Stage 1 in a new remote /tmp workspace, never the remote checkout.

Without --execute this performs only a read-only environment check. A real run
requires --execute; --install-dependencies permits an isolated venv installation.
Only an explicit source allowlist is uploaded. Raw data is read-only throughout.
"""
import argparse
from datetime import datetime, timezone
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import shlex
import subprocess
import sys
import tarfile
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(PROJECT_ROOT / "src"))
from core.config import load_paths


PROBE = r'''
import importlib.metadata
import importlib.util
import json
from pathlib import Path
import shutil
import sys

root = Path(sys.argv[1])
modules = {}
for module, distribution in [('duckdb','duckdb'),('pyarrow','pyarrow'),('yaml','PyYAML'),('pytest','pytest')]:
    present = importlib.util.find_spec(module) is not None
    try:
        version = importlib.metadata.version(distribution) if present else None
    except importlib.metadata.PackageNotFoundError:
        version = 'unavailable'
    modules[module] = {'available': present, 'version': version}
disk = shutil.disk_usage('/tmp')
print(json.dumps({'python': sys.version, 'python_executable': sys.executable,
                  'python_version': list(sys.version_info[:3]), 'modules': modules,
                  'venv_available': importlib.util.find_spec('venv') is not None,
                  'raw_root': str(root), 'raw_root_exists': root.is_dir(),
                  'tmp_total_bytes': disk.total, 'tmp_free_bytes': disk.free}))
'''


RECEIVE = r'''
import hashlib
import io
import json
from pathlib import Path, PurePosixPath
import sys
import tarfile
import tempfile

payload = sys.stdin.buffer.read()
if len(payload) > 50 * 1024 * 1024:
    raise SystemExit('Source bundle exceeds 50 MiB limit')
workspace = Path(tempfile.mkdtemp(prefix='quantkernel-stage1-', dir='/tmp')).resolve()
source = workspace / 'source'
source.mkdir()
with tarfile.open(fileobj=io.BytesIO(payload), mode='r:gz') as archive:
    for member in archive.getmembers():
        relative = PurePosixPath(member.name)
        if (relative.is_absolute() or '..' in relative.parts or ':' in member.name
                or not member.isfile()):
            raise SystemExit(f'Unsafe source archive member: {member.name}')
        target = (source / member.name).resolve()
        if not target.is_relative_to(source):
            raise SystemExit(f'Source archive member escapes workspace: {member.name}')
        target.parent.mkdir(parents=True, exist_ok=True)
        with archive.extractfile(member) as incoming, target.open('wb') as output:
            import shutil
            shutil.copyfileobj(incoming, output)
manifest = json.loads((source / '.stage1-source-manifest.json').read_text(encoding='utf-8'))
for record in manifest['files']:
    path = source / record['path']
    if hashlib.sha256(path.read_bytes()).hexdigest() != record['sha256']:
        raise SystemExit(f'Source checksum mismatch: {record["path"]}')
print(json.dumps({'workspace': str(workspace), 'source_files': len(manifest['files']),
                  'bundle_sha256': hashlib.sha256(payload).hexdigest()}))
'''


RUN = r'''
from datetime import datetime, timezone
import hashlib
import importlib.metadata
import json
import os
from pathlib import Path
import subprocess
import sys
import time

workspace = Path(sys.argv[1]).resolve()
raw = Path(sys.argv[2]).resolve()
start, end = sys.argv[3:5]
install, checksums = sys.argv[5] == '1', sys.argv[6] == '1'
if workspace.parent != Path('/tmp') or not workspace.name.startswith('quantkernel-stage1-'):
    raise SystemExit('Refusing a workspace outside a new /tmp/quantkernel-stage1-* directory')
source, middle, evidence = workspace / 'source', workspace / 'middle', workspace / 'evidence'
if not (source / '.stage1-source-manifest.json').is_file() or not raw.is_dir():
    raise SystemExit('Missing verified source bundle or remote DATA_ROOT')
if raw == workspace or raw.is_relative_to(workspace) or workspace.is_relative_to(raw):
    raise SystemExit('Remote workspace and DATA_ROOT must be disjoint')
if evidence.exists() or middle.exists():
    raise SystemExit('Refusing to reuse a previous verification workspace')
evidence.mkdir()

def save(name, value):
    (evidence / name).write_text(json.dumps(value, ensure_ascii=False, sort_keys=True, indent=2) + '\n', encoding='utf-8')

def inventory():
    def fail(error):
        raise error
    files = []
    for directory, subdirectories, names in os.walk(raw, followlinks=False, onerror=fail):
        for name in subdirectories + names:
            if (Path(directory) / name).is_symlink():
                raise RuntimeError(f'Raw source contains unsupported symbolic link: {directory}/{name}')
        for name in names:
            path = Path(directory) / name
            stat = path.stat()
            record = {'path': path.relative_to(raw).as_posix(), 'size': stat.st_size, 'mtime_ns': stat.st_mtime_ns}
            if checksums:
                digest = hashlib.sha256()
                with path.open('rb') as stream:
                    for chunk in iter(lambda: stream.read(8 * 1024 * 1024), b''):
                        digest.update(chunk)
                record['sha256'] = digest.hexdigest()
            files.append(record)
    files.sort(key=lambda row: row['path'])
    return {'root': str(raw), 'files': files, 'file_count': len(files),
            'total_bytes': sum(row['size'] for row in files), 'hashes_included': checksums}

def execute(command, logfile, environment):
    print('Remote phase: ' + logfile, file=sys.stderr, flush=True)
    with (evidence / logfile).open('w', encoding='utf-8') as log:
        process = subprocess.Popen(command, cwd=source, env=environment, stdout=log, stderr=subprocess.STDOUT)
        while True:
            try:
                return process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                print('Remote phase still running: ' + logfile, file=sys.stderr, flush=True)

result = {'workspace': str(workspace), 'raw_root': str(raw), 'middle_root': str(middle),
          'start': start, 'end': end, 'started_at_utc': datetime.now(timezone.utc).isoformat(),
          'build_exit_code': None, 'passed': False}
before = None
try:
    print('Reading remote raw-data metadata before verification', file=sys.stderr, flush=True)
    before = inventory()
    save('raw_before.json', before)
    environment = dict(os.environ, DATA_ROOT=str(raw), MIDDLE_ROOT=str(middle), DATA_PROFILE='local',
                       PYTHONDONTWRITEBYTECODE='1', PYTHONUNBUFFERED='1', PIP_DISABLE_PIP_VERSION_CHECK='1')
    python = sys.executable
    if install:
        venv = workspace / 'venv'
        code = execute([sys.executable, '-m', 'venv', str(venv)], 'venv.log', environment)
        if code:
            raise RuntimeError(f'Venv creation failed with code {code}; see venv.log')
        python = str(venv / 'bin' / 'python')
        code = execute([python, '-m', 'pip', 'install', '--no-cache-dir', '-r', 'requirements-lock.txt'],
                       'dependency_install.log', environment)
        if code:
            raise RuntimeError(f'Isolated dependency installation failed with code {code}; see dependency_install.log')
    probe = subprocess.run([python, '-c',
        "import sys,json,importlib.metadata,duckdb,pyarrow,yaml,pytest; "
        "print(json.dumps({'python':sys.version,'executable':sys.executable,'versions':"
        "{name:importlib.metadata.version(name) for name in ['duckdb','pyarrow','PyYAML','pytest']}}))"],
        cwd=source, env=environment, text=True, encoding='utf-8', capture_output=True)
    if probe.returncode:
        raise RuntimeError('Remote dependencies unavailable: ' + probe.stderr.strip())
    save('runtime.json', json.loads(probe.stdout))
    entrypoint = source / 'scripts' / 'build_stage1.py'
    if not entrypoint.is_file():
        raise RuntimeError('scripts/build_stage1.py is not present in the uploaded source bundle')
    command = [python, 'scripts/build_stage1.py', '--start', start, '--end', end, '--l0-scope', 'core']
    result['command'] = command
    result['build_exit_code'] = execute(command, 'build.log', environment)
    quality = middle / 'l2' / 'data_quality_report'
    reports = sorted(path.relative_to(workspace).as_posix() for path in quality.rglob('*')
                     if path.is_file() and path.suffix in ('.json', '.md')) if quality.exists() else []
    result['quality_reports'] = reports
    result['quality_json_present'] = any(name.endswith('.json') for name in reports)
    result['quality_markdown_present'] = any(name.endswith('.md') for name in reports)
except Exception as error:
    result['error'] = str(error)
finally:
    if before is not None:
        try:
            print('Reading remote raw-data metadata after verification', file=sys.stderr, flush=True)
            after = inventory()
            save('raw_after.json', after)
            left = {row['path']: row for row in before['files']}
            right = {row['path']: row for row in after['files']}
            shared = sorted(left.keys() & right.keys())
            comparison = {'missing_after': sorted(left.keys() - right.keys()),
                          'added_after': sorted(right.keys() - left.keys()),
                          'size_changed': [name for name in shared if left[name]['size'] != right[name]['size']],
                          'mtime_changed': [name for name in shared if left[name]['mtime_ns'] != right[name]['mtime_ns']],
                          'sha256_changed': [name for name in shared if checksums and left[name]['sha256'] != right[name]['sha256']],
                          'before_files': before['file_count'], 'after_files': after['file_count'],
                          'before_bytes': before['total_bytes'], 'after_bytes': after['total_bytes']}
            comparison['passed'] = not any(comparison[key] for key in
                ('missing_after','added_after','size_changed','mtime_changed','sha256_changed'))
            save('raw_comparison.json', comparison)
            result['raw_unchanged'] = comparison['passed']
        except Exception as error:
            result['raw_unchanged'] = False
            result['raw_comparison_error'] = str(error)
    result['passed'] = bool(result['build_exit_code'] == 0 and result.get('raw_unchanged')
                            and result.get('quality_json_present') and result.get('quality_markdown_present')
                            and not result.get('error'))
    result['finished_at_utc'] = datetime.now(timezone.utc).isoformat()
    save('run_result.json', result)
print(json.dumps(result))
'''


COLLECT = r'''
import io
from pathlib import Path
import sys
import tarfile

workspace = Path(sys.argv[1]).resolve()
if workspace.parent != Path('/tmp') or not workspace.name.startswith('quantkernel-stage1-'):
    raise SystemExit('Refusing evidence collection outside the isolated verification workspace')
files = list((workspace / 'evidence').glob('*'))
files.append(workspace / 'source' / '.stage1-source-manifest.json')
middle = workspace / 'middle'
if middle.exists():
    files.extend(path for path in middle.rglob('*')
                 if path.is_file() and (path.name == 'manifest.json' or
                    (path.suffix in ('.json','.md') and
                     ('data_quality_report' in path.parts or 'verification' in path.parts))))
with tarfile.open(fileobj=sys.stdout.buffer, mode='w|gz') as archive:
    for path in sorted(set(files)):
        if path.is_symlink() or not path.is_file() or not path.resolve().is_relative_to(workspace):
            raise SystemExit(f'Unsafe evidence path: {path}')
        if path.stat().st_size > 100 * 1024 * 1024:
            raise SystemExit(f'Evidence file exceeds 100 MiB: {path}')
        archive.add(path, arcname=path.relative_to(workspace).as_posix(), recursive=False)
'''


def _ssh(host, script, arguments=(), payload=None, timeout=3600):
    command = "python3 -c " + shlex.quote(script)
    command += "".join(" " + shlex.quote(str(argument)) for argument in arguments)
    process = subprocess.run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", host, command],
                             input=payload, stdout=subprocess.PIPE, stderr=None, timeout=timeout)
    if process.returncode:
        raise RuntimeError(f"Read-only/isolation SSH phase failed with exit code {process.returncode}")
    return process.stdout


def _source_bundle(root):
    required = ["pyproject.toml", "requirements-lock.txt", "configs/paths.yaml",
                "configs/tushare_tables.yaml", "configs/data_middle_layer.yaml", "scripts/build_stage1.py"]
    missing = [name for name in required if not (root / name).is_file()]
    if missing:
        raise ValueError(f"Server verification source is not ready: {missing}")
    candidates = {root / name for name in required}
    if (root / "README.md").is_file():
        candidates.add(root / "README.md")
    for folder in ("src", "scripts", "tests"):
        candidates.update((root / folder).rglob("*.py"))
    candidates.update(path for path in (root / "tests" / "fixtures" / "golden").glob("*")
                      if path.suffix in (".csv", ".json", ".md"))
    forbidden_roots = {"data", "data_middle"}
    forbidden_parts = {".git", ".venv", "venv", "__pycache__", ".codex", ".agents"}
    records, content = [], []
    for path in sorted(candidates):
        relative = path.relative_to(root)
        lowered_parts = tuple(part.lower() for part in relative.parts)
        if lowered_parts[0] in forbidden_roots or any(part in forbidden_parts for part in lowered_parts):
            raise ValueError(f"Forbidden source bundle path: {relative}")
        if path.is_symlink() or not path.resolve().is_relative_to(root):
            raise ValueError(f"Source bundle file must be inside the project and not a symlink: {relative}")
        if any(word in path.name.lower() for word in ("credential", "secret", "id_rsa", ".env")):
            raise ValueError(f"Potential secret file is not permitted in source bundle: {relative}")
        blob = path.read_bytes()
        if len(blob) > 5 * 1024 * 1024:
            raise ValueError(f"Source bundle file exceeds 5 MiB: {relative}")
        name = relative.as_posix()
        records.append({"path": name, "size": len(blob), "sha256": hashlib.sha256(blob).hexdigest()})
        content.append((name, blob))
    manifest = {"files": records,
                "excludes": sorted(name + "/" for name in forbidden_roots)
                            + sorted("**/" + name + "/" for name in forbidden_parts),
                "contains_raw_data": False}
    content.append((".stage1-source-manifest.json", json.dumps(manifest, sort_keys=True, indent=2).encode("utf-8")))
    target = io.BytesIO()
    with tarfile.open(fileobj=target, mode="w:gz") as archive:
        for name, blob in content:
            member = tarfile.TarInfo(name)
            member.size = len(blob)
            member.mode = 0o644
            archive.addfile(member, io.BytesIO(blob))
    return target.getvalue(), manifest


def _save_evidence(payload, output, paths):
    with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as archive:
        for member in archive.getmembers():
            relative = PurePosixPath(member.name)
            if relative.is_absolute() or ".." in relative.parts or ":" in member.name or not member.isfile():
                raise ValueError(f"Unsafe evidence archive member: {member.name}")
            target = (output / member.name).resolve()
            if not target.is_relative_to(output):
                raise ValueError(f"Evidence archive escapes output directory: {member.name}")
            target = paths.output(*target.relative_to(paths.middle_root).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            with archive.extractfile(member) as incoming, target.open("wb") as stream:
                import shutil
                shutil.copyfileobj(incoming, stream)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--host", required=True)
    parser.add_argument("--data-root", required=True, help="Existing remote raw-data root; never written")
    parser.add_argument("--start", default="20240102")
    parser.add_argument("--end", default="20240110")
    parser.add_argument("--execute", action="store_true", help="Upload and run in a new isolated /tmp directory")
    parser.add_argument("--install-dependencies", action="store_true", help="Install the lockfile into a new isolated remote venv")
    parser.add_argument("--verify-raw-sha256", action="store_true", help="Also hash all remote raw files before/after")
    args = parser.parse_args()
    if args.host.startswith("-"):
        parser.error("--host must be an SSH hostname, not an option")
    for label in (args.start, args.end):
        if len(label) != 8 or not label.isascii() or not label.isdigit():
            parser.error("--start and --end must be YYYYMMDD dates")
        datetime.strptime(label, "%Y%m%d")
    if args.start > args.end:
        parser.error("--start must not be after --end")
    environment = json.loads(_ssh(args.host, PROBE, [args.data_root], timeout=30))
    print(json.dumps(environment, ensure_ascii=False, indent=2), flush=True)
    if not args.execute:
        return
    if not environment["raw_root_exists"] or environment["python_version"] < [3, 10, 0]:
        raise SystemExit("Remote verification requires an existing raw-data root and Python >= 3.10")
    if not args.install_dependencies and not all(item["available"] for item in environment["modules"].values()):
        raise SystemExit("Remote dependencies are missing; rerun with --install-dependencies to use an isolated venv")
    paths = load_paths()
    run_id = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ") + "-" + uuid.uuid4().hex[:8]
    output = paths.output("verification", "server", run_id)
    bundle, manifest = _source_bundle(PROJECT_ROOT)
    output.mkdir(parents=True)
    (output / "source_manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    (output / "environment_before.json").write_text(json.dumps(environment, ensure_ascii=False, indent=2), encoding="utf-8")
    print("Uploading allowlisted source files to a new remote verification directory", flush=True)
    prepared = json.loads(_ssh(args.host, RECEIVE, payload=bundle, timeout=120))
    (output / "prepared.json").write_text(json.dumps(prepared, indent=2), encoding="utf-8")
    try:
        result = json.loads(_ssh(args.host, RUN,
                                [prepared["workspace"], args.data_root, args.start, args.end,
                                 int(args.install_dependencies), int(args.verify_raw_sha256)], timeout=7200))
    finally:
        payload = _ssh(args.host, COLLECT, [prepared["workspace"]], timeout=300)
        _save_evidence(payload, output, paths)
    (output / "result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2), flush=True)
    print(f"Local server-verification evidence: {output}", flush=True)
    if not result["passed"]:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
