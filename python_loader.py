#!/usr/bin/env python3
"""Load a CSV file into TypeDB through the Python driver: an alternative to `typedb loader`
that takes the same arguments.

The query file is a TypeQL pipeline with a `given` stage naming the input columns, e.g.

    given $id: integer, $name: string?;
    insert $p isa person, has id == $id; try { $p has name == $name; };

One producer thread reads the CSV, parses each cell into the value type its `given`
variable declares, and groups rows into batches of --batch-rows. --parallel-batches
consumer threads each take a batch, open their own write transaction, run the query with
the batch as given rows, and commit. Rows that fail to parse, and all rows of a batch
that fails to commit, are written to rejects.csv / rejects.log in --output-dir.

Unlike `typedb loader`, there is no checkpointing or resume, and datetime-tz inputs are
not supported.

The core is parse_given -> Producer.run / Batch.seal -> consume; the rest is argument
parity with `typedb loader` (TLS, addresses, null tokens, limits, schema definition).
"""

import argparse
import csv
import getpass
import queue
import re
import sys
import threading
import time
from datetime import date
from decimal import Decimal
from pathlib import Path

try:
    from typedb.driver import Credentials, Datetime, DriverOptions, DriverTlsConfig, Duration, TransactionType, TypeDB
except ImportError:
    sys.exit("python_loader.py needs the TypeDB Python driver: pip install -r requirements.txt")


def parse_boolean(cell):
    lowered = cell.lower()
    if lowered in ("true", "false"):
        return lowered == "true"
    raise ValueError(f"invalid boolean (expected 'true' or 'false'): '{cell}'")


def parse_datetime(cell):
    # YYYY-MM-DDTHH:MM:SS[.fff] or YYYY-MM-DD HH:MM:SS[.fff]
    return Datetime.utcfromstring(cell.replace(" ", "T", 1))


# The server type-checks given rows, so cells are parsed to typed Python values here.
CELL_PARSERS = {
    "boolean": parse_boolean,
    "integer": int,
    "double": float,
    "decimal": Decimal,
    "string": str,
    "date": date.fromisoformat,
    "datetime": parse_datetime,
    "duration": Duration.fromstring,
}

GIVEN_STAGE = re.compile(r"\bgiven\b([^;]*);")
GIVEN_VARIABLE = re.compile(r"\s*\$([A-Za-z_][\w-]*)\s*:\s*([a-z-]+)\s*(\?)?\s*")


def parse_given(query):
    """Return [(variable, value type, optional)] from the query's `given` stage."""
    stage = GIVEN_STAGE.search(re.sub(r"#[^\n]*", "", query))
    if stage is None:
        sys.exit("query has no `given` stage; cannot bind input rows")
    specs = []
    for declaration in stage.group(1).split(","):
        match = GIVEN_VARIABLE.fullmatch(declaration)
        if match is None:
            sys.exit(f"cannot parse `given` declaration '{declaration.strip()}'")
        name, value_type, optional = match.groups()
        if value_type not in CELL_PARSERS:
            sys.exit(f"`given` input '${name}' has unsupported type '{value_type}'; "
                     f"supported: {', '.join(CELL_PARSERS)}")
        specs.append((name, value_type, optional is not None))
    return specs


class Batch:
    def __init__(self, index):
        self.index = index
        self.rows = []  # given rows: {variable: value}
        self.given_rows = None  # self.rows converted to the driver's GivenRows
        self.rejects = []  # (row number, record, message) for rows that failed to parse
        self.records = []  # (row number, record) for each given row, for rejecting a failed batch
        self.attempted = 0

    def seal(self):
        # Converting to GivenRows makes a native call per cell, each of which releases and
        # re-acquires the GIL. Doing it here, on the single producer thread, rather than on
        # the consumers avoids them contending for the GIL on every call.
        if self.rows:
            self.given_rows = TypeDB.Concept.given_rows_from_map(self.rows)
        return self


class Producer:
    """Reads the CSV into batches of parsed given rows."""

    def __init__(self, reader, header, specs, args):
        self.reader = reader
        self.specs = specs
        self.null_values = set(args.null_values) if args.null_values else {""}
        self.batch_rows = args.batch_rows
        self.max_rows = args.max_rows
        if header is not None:
            index = {name: i for i, name in enumerate(header)}
            missing = [name for name, _, _ in specs if name not in index]
            if missing:
                sys.exit(f"CSV header missing column(s) required by query: {', '.join(missing)}")
            self.columns = [index[name] for name, _, _ in specs]
            self.expected_columns = len(header)
        else:
            self.columns = list(range(len(specs)))
            self.expected_columns = len(specs)

    def parse_row(self, record):
        if len(record) != self.expected_columns:
            raise ValueError(f"expected {self.expected_columns} columns, got {len(record)}")
        row = {}
        for (name, value_type, optional), column in zip(self.specs, self.columns):
            cell = record[column]
            if cell in self.null_values:
                if not optional:
                    raise ValueError(f"column '${name}': null value in non-optional column")
                row[name] = None
                continue
            try:
                row[name] = CELL_PARSERS[value_type](cell)
            except Exception as e:
                raise ValueError(f"column '${name}': invalid {value_type} '{cell}': {e}")
        return row

    def run(self, batches, stop, consumer_count):
        row_number = 0
        batch = Batch(0)
        try:
            for record in self.reader:
                if stop.is_set() or (self.max_rows and row_number >= self.max_rows):
                    break
                row_number += 1
                batch.attempted += 1
                try:
                    batch.rows.append(self.parse_row(record))
                    batch.records.append((row_number, record))
                except ValueError as e:
                    batch.rejects.append((row_number, record, str(e)))
                if batch.attempted == self.batch_rows:
                    batches.put(batch.seal())
                    batch = Batch(batch.index + 1)
            if batch.attempted and not stop.is_set():
                batches.put(batch.seal())
        except csv.Error as e:
            print(f"CSV error after row {row_number}: {e}", file=sys.stderr)
            stop.set()
        finally:
            for _ in range(consumer_count):
                batches.put(None)


class Progress:
    """Load statistics and the rejects files, shared by the consumer threads."""

    def __init__(self, output_dir, header, args):
        output_dir.mkdir(parents=True, exist_ok=True)
        self.rejects_csv_file = open(output_dir / "rejects.csv", "w", newline="")
        self.rejects_csv = csv.writer(self.rejects_csv_file, lineterminator="\n")
        if header is not None:
            self.rejects_csv.writerow(header)
        self.rejects_log = open(output_dir / "rejects.log", "w")
        self.stop_on_error = args.stop_on_error
        self.max_rejects = args.max_rejects
        self.lock = threading.Lock()
        self.attempted = self.committed = self.rejected = 0
        self.stop_reason = None

    def finish_batch(self, batch, error, stop):
        with self.lock:
            self.attempted += batch.attempted
            for row_number, record, message in batch.rejects:
                self._reject(row_number, record, message)
            if error is None:
                self.committed += len(batch.rows)
            else:
                print(f"batch {batch.index}: {len(batch.rows)} rows rejected by commit: {error}", file=sys.stderr)
                for row_number, record in batch.records:
                    self._reject(row_number, record, f"batch {batch.index} failed: {error}")
            if self.rejected and self.stop_on_error:
                self.stop_reason = "stopping on first error (--stop-on-error)"
            elif self.max_rejects is not None and self.rejected > self.max_rejects:
                self.stop_reason = f"rejected rows exceeded --max-rejects={self.max_rejects}"
            if self.stop_reason:
                stop.set()

    def _reject(self, row_number, record, message):
        self.rejects_csv.writerow(record)
        self.rejects_log.write(f"row {row_number}: {message}\n")
        self.rejected += 1

    def close(self):
        self.rejects_csv_file.close()
        self.rejects_log.close()


def consume(driver, database, query, batches, stop, progress):
    while (batch := batches.get()) is not None:
        if stop.is_set():
            continue  # drain without loading, so the producer never blocks
        error = None
        if batch.given_rows is not None:
            try:
                with driver.transaction(database, TransactionType.WRITE) as tx:
                    answer = tx.query(query, given_rows=batch.given_rows).resolve()
                    if answer.is_concept_rows() and next(iter(answer.as_concept_rows()), None) is None:
                        print(f"WARNING: batch {batch.index} inserted zero rows", file=sys.stderr)
                    tx.commit()
            except Exception as e:
                error = str(e).strip()
        progress.finish_batch(batch, error, stop)


def format_rate(rows, seconds):
    rate = rows / seconds if seconds > 0 else 0
    return f"{rate / 1000:.1f}k" if rate >= 1000 else f"{rate:.0f}"


def load(driver, args, query, specs):
    data_path = Path(args.data)
    output_dir = Path(args.output_dir) if args.output_dir else data_path.parent / f"python_loader_{data_path.stem}_progress"
    with open(data_path, newline="") as data_file:
        reader = csv.reader(data_file)
        header = next(reader, None) if args.header else None
        producer = Producer(reader, header, specs, args)
        progress = Progress(output_dir, header, args)
        batches = queue.Queue(maxsize=2 * args.parallel_batches)
        stop = threading.Event()
        threads = [threading.Thread(target=producer.run, args=(batches, stop, args.parallel_batches), daemon=True)]
        threads += [threading.Thread(target=consume, args=(driver, args.database, query, batches, stop, progress), daemon=True)
                    for _ in range(args.parallel_batches)]

        started = time.monotonic()
        for thread in threads:
            thread.start()
        try:
            next_report = started + 1
            for thread in threads:
                while thread.is_alive():
                    thread.join(timeout=max(0.0, next_report - time.monotonic()))
                    if time.monotonic() >= next_report:
                        elapsed = time.monotonic() - started
                        print(f"[{elapsed:.1f}s] {progress.committed} rows  "
                              f"{format_rate(progress.committed, elapsed)} rows/sec", flush=True)
                        next_report += 1
        except KeyboardInterrupt:
            progress.stop_reason = "interrupted"
            stop.set()
            for thread in threads:
                thread.join()
        elapsed = time.monotonic() - started
        progress.close()

    print(f"Loaded in {elapsed:.1f}s.")
    print(f"  Rows attempted:  {progress.attempted:>7}")
    print(f"  Rows committed:  {progress.committed:>7}")
    print(f"  Rows rejected:   {progress.rejected:>7}")
    print(f"  Rejects:         {output_dir}")
    if progress.stop_reason:
        sys.exit(f"Load stopped: {progress.stop_reason}")


def main():
    def flag(text):
        if text.lower() not in ("true", "false"):
            raise argparse.ArgumentTypeError(f"expected true or false, got '{text}'")
        return text.lower() == "true"

    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--query", required=True, help="TypeQL query file with a `given` stage")
    parser.add_argument("--database", required=True, help="database to load into")
    parser.add_argument("--data", required=True, help="CSV data file")
    parser.add_argument("--header", nargs="?", const=True, default=False, type=flag,
                        help="the CSV has a header row; columns are matched to `given` variables by name (default: false)")
    parser.add_argument("--no-header", dest="header", action="store_false", help="bind CSV columns positionally")
    parser.add_argument("--null-values", action="append",
                        help="cell value to treat as null; repeatable; replaces the default (empty cells)")
    parser.add_argument("--max-rows", type=int, help="process at most this many data rows")
    parser.add_argument("--batch-rows", type=int, default=1000, help="rows per write transaction (default: 1000)")
    parser.add_argument("--parallel-batches", type=int, default=1,
                        help="consumer threads, each committing its own transactions (default: 1)")
    parser.add_argument("--stop-on-error", nargs="?", const=True, default=False, type=flag,
                        help="stop at the first rejected row (default: false)")
    parser.add_argument("--max-rejects", type=int, help="stop once more than this many rows are rejected")
    parser.add_argument("--schema-file", help="TypeQL schema to define before loading")
    parser.add_argument("--create-db", nargs="?", const=True, default=False, type=flag,
                        help="create the database if it does not exist (default: false)")
    parser.add_argument("--address", "--addresses", required=True, help="host:port[,host:port,...]")
    parser.add_argument("--username", required=True)
    parser.add_argument("--password", help="prompted for if not given")
    parser.add_argument("--tls-disabled", nargs="?", const=True, default=False, type=flag,
                        help="connect without TLS (default: false)")
    parser.add_argument("--tls-root-ca", help="root CA file for TLS")
    parser.add_argument("--output-dir", help="directory for rejects.csv and rejects.log "
                                             "(default: python_loader_<data-stem>_progress next to the data file)")
    args = parser.parse_args()
    if args.batch_rows < 1 or args.parallel_batches < 1:
        parser.error("--batch-rows and --parallel-batches must be at least 1")

    query = Path(args.query).read_text()
    specs = parse_given(query)
    password = args.password if args.password is not None else getpass.getpass(f"Password for '{args.username}': ")
    if args.tls_disabled:
        tls = DriverTlsConfig.disabled()
    elif args.tls_root_ca:
        tls = DriverTlsConfig.enabled_with_root_ca(args.tls_root_ca)
    else:
        tls = DriverTlsConfig.enabled_with_native_root_ca()
    addresses = args.address.split(",")

    with TypeDB.driver(addresses[0] if len(addresses) == 1 else addresses,
                       Credentials(args.username, password), DriverOptions(tls)) as driver:
        if args.create_db and not driver.databases.contains(args.database):
            driver.databases.create(args.database)
        if args.schema_file:
            with driver.transaction(args.database, TransactionType.SCHEMA) as tx:
                tx.query(Path(args.schema_file).read_text()).resolve()
                tx.commit()
        load(driver, args, query, specs)


if __name__ == "__main__":
    main()
