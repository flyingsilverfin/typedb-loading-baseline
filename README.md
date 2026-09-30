# TypeDB loading baseline

A reproducible benchmark of bulk-loading speed into TypeDB, in a deliberately basic
scenario: two entity types, each with an integer key and two integer attributes, joined by
a binary relation. Loading runs either with [TypeDB loader](https://github.com/typedb/typedb-console/tree/master/loader)
or with `python_loader.py`, an equivalent built on the TypeDB Python driver.

```bash
python3 run_benchmark.py                  # typedb loader
python3 run_benchmark.py --loader python  # python_loader.py
```

This one command downloads TypeDB, generates the data, starts a fresh server, loads
everything, validates the result and prints a summary like:

```
TypeDB 3.13.6 on aarch64, 10 CPUs; loader=typedb, batch-rows=1000, parallel-batches=8
phase                           rows   seconds    rows/s
entity first-entity        1,000,000      20.7    48,261
entity second-entity       1,000,000      23.2    43,011
relation between           1,000,000      35.8    27,940
```

Each row is one load step: the 1M `first-entity` entities, then the 1M `second-entity`
entities, then 1M `between` relations, each linking a `first-entity` to a `second-entity`.

Requirements: Python 3.8+, Linux or macOS on x86_64 or arm64, and internet access for the
first run. The benchmark itself uses only the standard library. `--loader python` installs
the TypeDB Python driver into a virtualenv, `run/venv`. Everything that is downloaded,
generated or written goes into `run/`, which is git-ignored. Delete it to start from
scratch.

## What it does

1. **Download** `typedb-all` for this platform (server + loader) into `run/`, unless it is
   already there (`download_typedb.py`). With `--typedb-home <dir>`, it uses an existing
   TypeDB distribution instead. If that distribution has no loader, it downloads the
   standalone `typedb-loader` package.
2. **Generate** the CSVs into `run/data/`, unless they already exist (`generate.py`):
   - `first-entity`, `second-entity`: `id,A,B`, where `id` counts up from 0 and A, B are
     random integers in `[0, 2^31)`.
   - `between`: `from,to`, each column a random sample (with replacement) of the dense
     IDs `[0, entities)`.
   All columns come from seeded `random.Random` streams, so the data is byte-identical
   everywhere.
3. **Start** a TypeDB server in development mode on an empty data directory,
   `run/server/`. Each run starts from a fresh database. Development mode only turns off
   diagnostics reporting to TypeDB, so benchmark runs don't send telemetry.
4. **Define** `schema.tql` in a new database.
5. **Load**, timing each step: `load-first.tql`, then `load-second.tql`, then
   `load-between.tql`. The relation template matches both endpoints by their `@key` id
   before inserting. Before the relation load there is an untimed 30s pause
   (`--settle-seconds`) so the server's statistics catch up; see the lessons below.
6. **Validate** the instance counts and spot-check that specific CSV rows were loaded with
   the right values and role players. Then stop the server.

Results, the loaders' logs and their rejects/checkpoint files go to
`run/results/<timestamp>/`. `results.json` records the TypeDB version, machine,
parameters and per-step timings. It is the file to share when reporting numbers.

## Options

```
python3 run_benchmark.py --help

  --loader typedb|python   typedb loader binary, or python_loader.py (default: typedb)
  --entities N             rows per entity table (default: 1000000)
  --relations N            rows in the relation table (default: 1000000)
  --batch-rows N           rows per transaction (default: 1000)
  --parallel-batches N     concurrent transactions (default: 8)
  --settle-seconds S       untimed pause before the relation load (default: 30)
  --version V              TypeDB version to download (default: 3.13.6)
  --typedb-home DIR        use this TypeDB distribution instead of downloading one
  --database NAME          database name (default: loading-baseline)
  --port / --http-port     server ports (default: 1729 / 8000)
```

The individual pieces also work on their own:

```bash
python3 download_typedb.py [--version V] [--typedb-home DIR]
python3 generate.py entities  -n 1000000 -c 2 -t integer -o first.csv
python3 generate.py relations -n 1000000 --from-range 0:1000000 --to-range 0:1000000 -o between.csv
```

`generate.py entities` supports any number of value columns (`-c`: A, B, C, ...) and the
value types `integer`, `double`, `string` and `boolean`. The schema and templates in this
repository cover the default: two integer columns. To benchmark other shapes, adjust
`schema.tql` and the `load-*.tql` templates to match.

## Python loader

`python_loader.py` loads a CSV through the TypeDB Python driver. It takes the same
arguments as `typedb loader`, so the two are interchangeable in scripts:

```bash
pip install -r requirements.txt
python3 python_loader.py --query=load-first.tql --data=first.csv --header \
  --database=loading-baseline --create-db --schema-file=schema.tql \
  --address=localhost:1729 --username=admin --tls-disabled \
  --batch-rows=1000 --parallel-batches=8
```

It reads the variables and value types from the query's `given` stage. One producer
thread reads the CSV, parses each cell into its declared type and builds batches of
`--batch-rows` rows. `--parallel-batches` consumer threads each take a batch, open their
own write transaction, run the template with the batch as given rows, and commit.

As with `typedb loader`, `--header` matches CSV columns to variables by name; without it,
they bind by position. `--null-values` sets the null tokens, and `?`-optional variables
accept nulls. Unparseable rows, and every row of a batch whose commit fails, go to
`rejects.csv` / `rejects.log`. `--stop-on-error` and `--max-rejects` stop the load early.

Differences from `typedb loader`: there is no checkpointing or `--resume`, `datetime-tz`
inputs are not supported, and progress is reported in rows only, not bytes.

## Lessons

### typedb loader

- **Some names are reserved.** `first` and `from` are TypeQL keywords (as are `last`,
  `of` and the other statement keywords), so they can't be used as type or role labels.
  That is why the types are `first-entity` / `second-entity` and the roles are
  `source` / `target`. `$from` is still fine as a variable name.
- **The loader is its own binary.** It is not console's `load` command. It ships in the
  `typedb-all-<os>-<arch>` archive and as a standalone `typedb-loader-<os>-<arch>` package:
  `.tar.gz` on Linux, `.zip` on macOS and Windows.
- **Development mode is a hidden flag,** `--development-mode.enabled=true`. In the server
  code, it only disables diagnostics and error reporting, so it doesn't affect load speed.
- **Parallelism is the main throughput knob.** Going from `--parallel-batches` 1 to 4 to 8
  gave about 11k → 37k → 52k rows/s for first-entity, and 6k → 20k → 32k rows/s for
  relations. The default is fixed at 8 rather than tied to CPU count, so results stay
  comparable across machines.
- **Starting the relation load right after the entity loads can stall.** On a fresh
  20k-entity database, about half of such runs stalled for 20–30s, versus 0.7s normally.
  During the stall the server burned about 150s of CPU on 8 cores. The first in-flight
  batches were slow and everything after them was fast. The query profile
  (`RUST_LOG=info,query::query_manager=trace,database::query=trace`) confirmed the cause:
  those batches were planned from statistics that didn't yet include the second entity
  load. The planner then scanned every `second-entity` `has` edge for each input row (60M
  storage advances per 1,000-row batch) instead of looking up `id` by value. A 3s pause
  prevented it at small scale, but at 1M rows it still hit 4 of 8 runs with 10k-row
  batches. Stalled batches can run into the 300s
  transaction timeout and be rejected. The benchmark therefore waits 30s before the
  relation load. Use `--settle-seconds 0` to reproduce it. This deserves a TypeDB issue: real loading
  pipelines usually load relations straight after entities.
- **Rows that match nothing are dropped silently.** If a relation row's `from` or `to` id
  isn't found, its `match` returns no rows and nothing is inserted. The loader only warns
  when an entire batch inserts nothing, so check counts after a load, as the benchmark
  does.
- **Expect run-to-run variance of about 15–20%.** Compare several runs, not single numbers.
- The package repository (repo.typedb.com) returns 403 to Python's default `urllib`
  User-Agent. `download_typedb.py` sends its own.

### Python driver

- **Given rows must be typed.** Over gRPC, the server rejects a string for an `integer`
  variable (`[GVN7] ... has type 'string' and could not be decoded as the value type
  'integer'`). The client therefore parses each cell into its declared type, as
  `typedb loader` does.
- **Both given-rows forms cost the same.** A list of dicts with plain Python values
  (`[{"id": 1, "A": 2}]`, converted by the driver) and a `(variables, rows)` tuple of
  `Concept`s (`TypeDB.Concept.new_integer(...)`) both take about 4–5ms per 1,000 rows of
  3 integers. The loader uses the dict form, since it's the documented one.
- **Build batches on one thread.** The driver's native layer releases the GIL around every
  call, and building a batch makes a native call per cell. When each consumer thread built
  its own batch, 8 threads fought over the GIL on every call. That load reached only
  16–18k rows/s for entities and spent more CPU in the kernel than in Python (11.5s system
  vs 5.5s user for 200k rows). Throughput also got worse past 4 threads. Building batches
  on the producer thread, so consumers only open, query and commit, cut system CPU to
  near zero and made 8 threads about 3.5× faster: 59k rows/s, up from 17k.
- **After that fix, it matches typedb loader.** At 8 threads, across 1M-row runs on the
  machine above, both loaders landed in the same range: 42–55k rows/s for the first entity
  type, 34–51k for the second, and 27–37k for relations. The differences were within
  run-to-run noise. Parsing and batch building on the producer takes about 5ms per 1,000
  rows (about 200k rows/s), which is well above what the server absorbs here.
- **This system Python has no pip.** The Python here has no `pip` module, but
  `python3 -m venv` still bootstraps one, so the benchmark installs the pinned driver
  (`requirements.txt`) into `run/venv`.

## Notes

- Timings are wall-clock per loader invocation, including loader startup and driver
  connection (well under a second).
- The server uses the configuration bundled with its distribution (`server/config.yml`),
  except for ports, data and log directories, and development mode.
- Only tested on Linux arm64. The macOS download path is implemented but untested, and
  Windows is untested.
