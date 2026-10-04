#!/usr/bin/env python3
"""TypeDB loading baseline: load two entity tables and one relation table with typedb loader.

Steps (everything is written under run/):
  1. download TypeDB for this platform (or use --typedb-home)
  2. generate the CSV data, unless it already exists for these parameters
  3. start a fresh TypeDB server, in development mode, on an empty data directory
  4. create the database and define schema.tql
  5. load first-entity, second-entity, then between, timing each with typedb loader
     (or, with --loader python, with python_loader.py and the TypeDB Python driver)
  6. with --validate, check instance counts and spot-check loaded rows; then stop the server
"""

import argparse
import csv
import json
import os
import platform
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime
from pathlib import Path

from download_typedb import DEFAULT_VERSION, ROOT, RUN_DIR, ensure_typedb

USERNAME, PASSWORD = "admin", "password"
LOADER_ECHO_INTERVAL_SECONDS = 5


class TypeDBHttp:
    def __init__(self, port):
        self.base = f"http://127.0.0.1:{port}"
        self.token = None

    def request(self, method, path, body=None):
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = f"Bearer {self.token}"
        data = json.dumps(body).encode() if body is not None else None
        request = urllib.request.Request(self.base + path, data=data, headers=headers, method=method)
        try:
            with urllib.request.urlopen(request) as response:
                content = response.read()
        except urllib.error.HTTPError as e:
            sys.exit(f"HTTP {method} {path} failed ({e.code}): {e.read().decode()}")
        return json.loads(content) if content else None

    def is_healthy(self):
        try:
            with urllib.request.urlopen(self.base + "/health", timeout=1):
                return True
        except OSError:
            return False

    def sign_in(self):
        self.token = self.request("POST", "/v1/signin", {"username": USERNAME, "password": PASSWORD})["token"]

    def query(self, database, transaction_type, query):
        body = {"databaseName": database, "transactionType": transaction_type, "query": query, "commit": transaction_type != "read"}
        return self.request("POST", "/v1/query", body)

    def count(self, database, query):
        answers = self.query(database, "read", f"{query} reduce $count = count;")["answers"]
        return answers[0]["data"]["count"]["value"]


def ensure_port_free(port, wait_seconds=10):
    deadline = time.monotonic() + wait_seconds  # a just-stopped server may take a moment to release it
    while True:
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", port)) != 0:
                return
        if time.monotonic() > deadline:
            sys.exit(f"port {port} is already in use (another TypeDB server?); stop it or pass --port / --http-port")
        time.sleep(0.2)


def start_server(server_bin, server_dir, args):
    """Start the server on an empty data directory."""
    ensure_port_free(args.port)
    ensure_port_free(args.http_port)
    shutil.rmtree(server_dir, ignore_errors=True)
    server_dir.mkdir(parents=True)
    log = open(server_dir / "server.log", "w")
    process = subprocess.Popen(
        [
            str(server_bin),
            "--development-mode.enabled=true",
            f"--server.listen-address=127.0.0.1:{args.port}",
            f"--server.http.listen-address=127.0.0.1:{args.http_port}",
            "--diagnostics.monitoring.enabled=false",
            f"--storage.data-directory={server_dir / 'data'}",
            f"--logging.directory={server_dir / 'logs'}",
            *args.server_arg,
        ],
        cwd=server_bin.parent, stdout=log, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
    )
    http = TypeDBHttp(args.http_port)
    deadline = time.monotonic() + 60
    while not http.is_healthy():
        if process.poll() is not None or time.monotonic() > deadline:
            process.kill()
            sys.exit(f"TypeDB server failed to start; see {server_dir / 'server.log'}:\n"
                     + (server_dir / "server.log").read_text()[-2000:])
        time.sleep(0.2)
    return process, http


def stop_server(process):
    process.terminate()
    try:
        process.wait(timeout=60)
    except subprocess.TimeoutExpired:
        process.kill()


def generate(table, path, *generate_args):
    if path.exists():
        return
    print(f"Generating {path.relative_to(ROOT)}")
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".part")
    subprocess.run([sys.executable, str(ROOT / "generate.py"), table, *generate_args, "-o", str(partial)], check=True)
    partial.rename(path)


def ensure_python_loader():
    """Set up run/venv with the Python driver; return the command prefix running python_loader.py."""
    venv = RUN_DIR / "venv"
    python = venv / ("Scripts/python.exe" if os.name == "nt" else "bin/python")
    requirements = (ROOT / "requirements.txt").read_text()
    installed = venv / "requirements.txt"
    if not (installed.exists() and installed.read_text() == requirements):
        print(f"Installing the TypeDB Python driver into {venv.relative_to(ROOT)}")
        subprocess.run([sys.executable, "-m", "venv", str(venv)], check=True)
        subprocess.run([str(python), "-m", "pip", "install", "-q", "-r", str(ROOT / "requirements.txt")], check=True)
        installed.write_text(requirements)
    return [str(python), str(ROOT / "python_loader.py")]


def run_loader(loader_command, query, data, output_dir, args):
    output_dir.mkdir(parents=True)
    command = [
        *loader_command,
        f"--query={query}", f"--data={data}", "--header",
        f"--database={args.database}",
        f"--address=127.0.0.1:{args.port}", f"--username={USERNAME}", f"--password={PASSWORD}", "--tls-disabled",
        f"--batch-rows={args.batch_rows}", f"--parallel-batches={args.parallel_batches}",
        f"--output-dir={output_dir}",
    ]
    summary = {}
    last_echo = 0.0
    start = time.monotonic()
    with open(output_dir / "loader.log", "w") as log:
        process = subprocess.Popen(command, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL, text=True)
        for line in process.stdout:
            log.write(line)
            # The loader prints a progress line per batch; echo those only every few seconds.
            is_progress = line.startswith("[")
            if not is_progress or time.monotonic() - last_echo >= LOADER_ECHO_INTERVAL_SECONDS:
                print("  " + line, end="", flush=True)
                last_echo = time.monotonic() if is_progress else last_echo
            key, _, value = line.strip().partition(":")
            if key in ("Rows attempted", "Rows committed", "Rows rejected"):
                summary[key] = int(value)
        exit_code = process.wait()
    seconds = time.monotonic() - start
    if exit_code != 0:
        sys.exit(f"typedb loader exited with {exit_code}; see {output_dir / 'loader.log'}")
    return seconds, summary


def validate(http, args, data_files):
    database = args.database
    expected = {
        "match $x isa first-entity;": args.entities,
        "match $x isa second-entity;": args.entities,
        "match $x isa between;": args.relations,
        "match $x isa between, links (source: $f, target: $t);": args.relations,
    }
    for query, count in expected.items():
        actual = http.count(database, query)
        status = "ok" if actual == count else "MISMATCH"
        print(f"  {query:<60} {actual:>12,}  (expected {count:,}) {status}")
        if actual != count:
            return False

    # Spot-check that specific CSV rows landed with the right values and role players.
    checks = []
    for entity in ("first-entity", "second-entity"):
        with open(data_files[entity]) as f:
            for row in _head(f, 3):
                checks.append(f"match $x isa {entity}, has id {row['id']}, has A {row['A']}, has B {row['B']};")
    with open(data_files["between"]) as f:
        for row in _head(f, 3):
            checks.append(f"match $f isa first-entity, has id {row['from']}; $t isa second-entity, has id {row['to']}; "
                          f"$x isa between, links (source: $f, target: $t);")
    for query in checks:
        if http.count(database, query) < 1:
            print(f"  spot check FAILED: {query}")
            return False
    print(f"  {len(checks)} spot checks of loaded CSV rows ok")
    return True


def _head(f, n):
    reader = csv.DictReader(f)
    return [row for _, row in zip(range(n), reader)]


def machine_info():
    info = {"platform": platform.platform(), "machine": platform.machine(), "cpus": os.cpu_count()}
    try:
        info["memory_gb"] = round(os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES") / 2**30, 1)
    except (ValueError, OSError, AttributeError):
        pass
    return info


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--loader", choices=("typedb", "python"), default="typedb",
                        help="typedb: the typedb loader binary; python: python_loader.py (default: typedb)")
    parser.add_argument("--entities", type=int, default=1_000_000, help="rows per entity table (default: 1000000)")
    parser.add_argument("--relations", type=int, default=1_000_000, help="rows in the relation table (default: 1000000)")
    parser.add_argument("--batch-rows", type=int, default=1000, help="loader rows per transaction (default: 1000)")
    parser.add_argument("--parallel-batches", type=int, default=8, help="loader concurrent transactions (default: 8)")
    parser.add_argument("--validate", action="store_true",
                        help="after loading, check instance counts and spot-check loaded rows (not timed; off by default)")
    parser.add_argument("--server-arg", action="append", default=[], metavar="ARG",
                        help="extra TypeDB server argument, e.g. --server-arg=--storage.rocksdb.cache-size=4gb; repeatable")
    parser.add_argument("--version", default=DEFAULT_VERSION, help=f"TypeDB version to download (default: {DEFAULT_VERSION})")
    parser.add_argument("--typedb-home", type=Path, help="use this TypeDB distribution instead of downloading one")
    parser.add_argument("--database", default="loading-baseline", help="database name (default: loading-baseline)")
    parser.add_argument("--port", type=int, default=1729, help="server gRPC port (default: 1729)")
    parser.add_argument("--http-port", type=int, default=8000, help="server HTTP port (default: 8000)")
    args = parser.parse_args()

    server_bin, loader_bin = ensure_typedb(args.version, args.typedb_home)
    loader_command = ensure_python_loader() if args.loader == "python" else [str(loader_bin)]

    n = args.entities
    data_dir = RUN_DIR / "data"
    data_files = {
        "first-entity": data_dir / f"first-entity-n{n}.csv",
        "second-entity": data_dir / f"second-entity-n{n}.csv",
        "between": data_dir / f"between-n{args.relations}-ids{n}.csv",
    }
    # Entity tables use different seeds so that their A/B values differ.
    generate("entities", data_files["first-entity"], "--rows", str(n), "--seed", "0")
    generate("entities", data_files["second-entity"], "--rows", str(n), "--seed", "100")
    generate("relations", data_files["between"], "--rows", str(args.relations),
             "--from-range", f"0:{n}", "--to-range", f"0:{n}", "--seed", "200")

    results_dir = RUN_DIR / "results" / datetime.now().strftime("%Y%m%d-%H%M%S")
    print(f"Starting TypeDB server ({server_bin}) in development mode")
    server, http = start_server(server_bin, RUN_DIR / "server", args)
    try:
        server_version = http.request("GET", "/v1/version")
        http.sign_in()
        http.request("POST", f"/v1/databases/{args.database}")
        http.query(args.database, "schema", (ROOT / "schema.tql").read_text())

        phases = []
        for kind, name, template in (("entity", "first-entity", "load-first.tql"),
                                     ("entity", "second-entity", "load-second.tql"),
                                     ("relation", "between", "load-between.tql")):
            print(f"\nLoading {kind} {name} ({data_files[name].relative_to(ROOT)} with {template})")
            seconds, summary = run_loader(loader_command, ROOT / template, data_files[name], results_dir / name, args)
            rows = summary.get("Rows committed", 0)
            phases.append({"phase": f"{kind} {name}", "rows": rows, "rejected": summary.get("Rows rejected"),
                           "seconds": round(seconds, 3), "rows_per_second": round(rows / seconds)})
            if summary.get("Rows rejected") != 0:
                sys.exit(f"typedb loader rejected rows; see {results_dir / name}")

        valid = None  # not checked
        if args.validate:
            print("\nValidating")
            valid = validate(http, args, data_files)
    finally:
        stop_server(server)

    results = {
        "typedb": {**server_version, "home": str(server_bin.parent.parent)},
        "machine": machine_info(),
        "parameters": {"loader": args.loader, "server_args": args.server_arg, "entities": args.entities,
                       "relations": args.relations, "batch_rows": args.batch_rows, "parallel_batches": args.parallel_batches},
        "phases": phases,
        "valid": valid,
    }
    (results_dir / "results.json").write_text(json.dumps(results, indent=2) + "\n")

    print(f"\nTypeDB {server_version.get('version', '?')} on {results['machine']['machine']}, "
          f"{results['machine']['cpus']} CPUs; loader={args.loader}, batch-rows={args.batch_rows}, parallel-batches={args.parallel_batches}")
    print(f"{'phase':<24}{'rows':>12}{'seconds':>10}{'rows/s':>10}")
    for p in phases:
        print(f"{p['phase']:<24}{p['rows']:>12,}{p['seconds']:>10.1f}{p['rows_per_second']:>10,}")
    print(f"\nResults: {results_dir.relative_to(ROOT) / 'results.json'}")
    if valid is False:
        sys.exit("validation FAILED")


if __name__ == "__main__":
    main()
