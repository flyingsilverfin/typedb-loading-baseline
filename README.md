# TypeDB loading baseline

A reproducible benchmark of bulk-loading speed into TypeDB, in a deliberately basic
scenario: two entity types, each with an integer key and two integer attributes, joined by
a binary relation. Loading runs either with [TypeDB loader](https://typedb.com/docs/tools/loader/)
or with `python_loader.py`, an equivalent built on the TypeDB Python driver.

```bash
python3 run_benchmark.py                  # typedb loader
python3 run_benchmark.py --loader python  # python_loader.py
```

This one command downloads TypeDB, generates the data, starts a fresh server, loads
everything and prints a summary like:

```
TypeDB 3.13.6 on aarch64, 10 CPUs; loader=typedb, batch-rows=1000, parallel-batches=8
phase                           rows   seconds    rows/s
entity first-entity        1,000,000      23.9    41,903
entity second-entity       1,000,000      29.6    33,832
relation between           1,000,000      35.2    28,437

Results: run/results/20260930-201720/results.json
```

Each row is one load step: the 1M `first-entity` entities, then the 1M `second-entity`
entities, then 1M `between` relations, each linking a `first-entity` to a `second-entity`.

## Quick start

**Requirements.** Python 3.8 or newer (`--loader python` needs 3.9 to 3.14, the versions
the pinned driver ships wheels for), and internet access for the first run. The benchmark
itself uses only the standard library; `--loader python` installs the TypeDB Python driver
into a virtualenv, `run/venv`. Only tested on Linux arm64. The macOS download path is
implemented but untested, and Windows is untested.

**What to expect.** The first run downloads about 30 MB (100 MB unpacked) into `run/`. The
default parameters write about 70 MB of CSV and take about two minutes on the reference
machine, 90 s of it loading. Everything that is downloaded, generated or written goes into
`run/`, which is git-ignored; delete it to start from scratch. Start with a smoke run,
which takes about 15 s:

```bash
python3 run_benchmark.py --entities 20000 --relations 30000
```

**Troubleshooting.**

- *port 1729 (or 8000) is already in use*: another TypeDB server, or something else on
  port 8000, is running. Stop it, or pass `--port` / `--http-port`.
- *TypeDB server failed to start*: the message ends with the tail of
  `run/server/server.log`; the server's own log files are in `run/server/logs/`.
- *rejected rows*: the run stops after the step that rejected them. Its
  `run/results/<timestamp>/<step>/` holds the loader's output (`loader.log`) and the
  rejected rows with their errors (`rejects.csv`, `rejects.log`).
- *a much slower relation step* (minutes instead of seconds): a known planner issue, see
  Lessons below. Run the benchmark again.

## TypeDB terms used here

An *entity* is a thing (`first-entity`). An *attribute* is a typed value an entity owns
(`id`, `A`, `B`). `@key` makes `id` unique and required, so an entity can be looked up by
it. A *relation* (`between`) links entities, each playing a *role* (`source`, `target`).
These are declared in `schema.tql` with a
[`define`](https://typedb.com/docs/typeql-reference/schema/define/) query.

The load templates (`load-*.tql`) are queries that start with a `given` stage, such as
`given $id: integer, $A: integer, $B: integer;`, which declares the query's input
variables. The loader runs the query once per CSV row, sending the rows of a batch as the
query's input ("given rows") and committing each batch as one transaction. The rest of the
template is an ordinary [`insert`](https://typedb.com/docs/typeql-reference/data-pipelines/insert/),
optionally preceded by a `match`.

`typedb loader` is a CLI for loading CSV files through such templates
([reference](https://typedb.com/docs/tools/loader/reference/)). It ships in the
`typedb-all` distribution next to the server. The server runs in *development mode*,
which only turns off diagnostics reporting to TypeDB; it does not change performance.

## Reading the results

Each phase's *seconds* is the wall-clock time of one loader process, including its startup
and connection (well under a second). Server start, schema definition, data generation
and `--validate` are not timed. An entity row inserts one entity with three attributes. A
relation row looks up two entities by key and inserts one relation with two role players.

Runs vary by about 15–20%. Run the benchmark three times and report the median, and share
each run's `results.json`, which records the TypeDB version, the machine, the parameters
and the per-step timings:

```json
{
  "typedb": {"distribution": "TypeDB CE", "version": "3.13.6", "home": "/.../run/typedb-all-linux-arm64-3.13.6"},
  "machine": {"platform": "Linux-6.12.76-linuxkit-aarch64-with-glibc2.39", "machine": "aarch64", "cpus": 10, "memory_gb": 31.5},
  "parameters": {"loader": "typedb", "server_args": [], "entities": 1000000, "relations": 1000000, "batch_rows": 1000, "parallel_batches": 8},
  "phases": [
    {"phase": "entity first-entity", "rows": 1000000, "rejected": 0, "seconds": 23.864, "rows_per_second": 41903},
    ...
  ],
  "valid": true,
  "error": null
}
```

`valid: null` means the run did not use `--validate`. `typedb.home` is a path on your
machine; strip it if you don't want to share it. When a step fails, the file still records
the completed phases, with the message in `error`.

Use `--validate` for numbers you publish. It checks the instance counts and spot-checks
loaded rows after the loads, untimed. This matters because a relation row whose ids match
nothing inserts nothing, without an error.

## Architecture

The benchmark follows the practices that gave the fastest loads in our measurements.
Rates below are from a 10-CPU arm64 machine running TypeDB 3.13.6, unless stated otherwise.

- **Send rows in batches through a `given` query.** Each template starts with a `given`
  stage declaring its input variables (`given $id: integer, ...;`). The loader sends
  1,000 CSV rows at a time as the input rows of one query, in one request, and commits
  them as one write transaction. That replaces a network round trip and a commit per row
  with one per batch.
- **Run several transactions in parallel.** Batches are committed concurrently
  (`--parallel-batches`, default 8). On the reference machine this was the biggest lever:
  going from 1 to 4 to 8 parallel batches raised entity loading from about 11k to 37k to
  52k rows/s, and relation loading from 6k to 20k to 32k rows/s. With 10 CPUs, relation
  loading peaked at about 12 parallel batches and got slower beyond that, as the client
  and server competed for cores; entity loading still improved slightly up to 16.
- **Keep batches moderate.** Larger batches mean fewer commits, but each slow or failed
  batch costs more. With 10,000-row batches, 4 of 8 runs had relation loads stall for
  minutes when batches were planned badly (see [NOTES.md](NOTES.md)), some hitting the
  300s transaction timeout, and the runs that didn't stall were no faster. 1,000 rows was
  a good balance.
- **Load entities first, then relations.** A relation needs its role players to exist,
  so the relation template matches both endpoints before inserting. Loading all entities
  first means every relation batch can find its endpoints.
- **Match relation endpoints by a key attribute.** Each entity type owns `id @key`, and
  the relation template finds its endpoints with `has id == $from`. Matching by
  attribute value lets the server find each endpoint through an index lookup instead of
  scanning entities, and `@key` guarantees exactly one entity per id.
- **Pass typed values.** The server type-checks given rows, so the loaders parse each CSV
  cell into its declared type (`integer`, `string`, ...) before sending. Strings sent for
  `integer` variables are rejected.
- **Keep the client off the critical path.** `typedb loader` is a native binary. In
  `python_loader.py`, a single producer thread parses the CSV and builds each batch, and
  the consumer threads only send queries and commit. Building batches on the consumer
  threads made them contend for Python's GIL, and was 3.5× slower at 8 threads.
- **Keep extra work out of the timed path.** The server starts fresh, and the schema is
  defined before timing starts. Validation is optional (`--validate`) and runs after all
  loads, untimed. Only the loader runs themselves are timed.

## Changing the data

Three things must agree: the CSV header (`id,A,B`), the `given` variables (`$id, $A, $B`,
matched to columns **by name**, because the benchmark passes `--header`), and the
attribute labels in `schema.tql`. The attribute labels are free: `has A == $A` reads like
that by convention only, and only the variable must match the column. The spot checks in
`validate()` take the attribute names from the CSV header, so they expect a column and an
attribute with the same name, and unquoted (numeric) values.

`generate.py entities` supports any number of value columns (`-c`: A, B, C, ...) and the
value types `integer`, `double`, `string` and `boolean`; `run_benchmark.py` calls it with
two integer columns. To add a third integer column C: pass `columns=3` in both
`generate("entities", ...)` calls in `run_benchmark.py`, add `attribute C, value integer;`
and `owns C` to the schema, and add `$C: integer` and `has C == $C` to both entity
templates. The data file names spell out every generator option
(`run/data/first-entity-rows=1000000-columns=2-value_type=integer-seed=0.csv`), so a
changed shape is generated afresh and never reuses a stale file.

The pieces also work on their own:

```bash
python3 download_typedb.py [--version V] [--typedb-home DIR]
python3 generate.py entities  -n 1000000 -c 2 -t integer -o first.csv
python3 generate.py relations -n 1000000 --from-range 0:1000000 --to-range 0:1000000 -o between.csv
```

## Options

```
python3 run_benchmark.py --help

options:
  -h, --help            show this help message and exit
  --loader {typedb,python}
                        typedb: the typedb loader binary; python:
                        python_loader.py (default: typedb)
  --entities ENTITIES   rows per entity table (default: 1000000)
  --relations RELATIONS
                        rows in the relation table (default: 1000000)
  --batch-rows BATCH_ROWS
                        loader rows per transaction (default: 1000)
  --parallel-batches PARALLEL_BATCHES
                        loader concurrent transactions (default: 8)
  --validate            after loading, check instance counts and spot-check
                        loaded rows (not timed; off by default)
  --version VERSION     TypeDB version to download (default: 3.13.6)
  --database DATABASE   database name (default: loading-baseline)
  --port PORT           server gRPC port (default: 1729)
  --http-port HTTP_PORT
                        server HTTP port (default: 8000)

advanced:
  --typedb-home TYPEDB_HOME
                        use this TypeDB distribution instead of downloading
                        one
  --server-arg ARG      extra TypeDB server argument, e.g. --server-
                        arg=--storage.rocksdb.cache-size=4gb; repeatable
```

The advanced options are for comparing TypeDB builds and server settings; the defaults
are what the reported numbers use.

## How it works

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
   `run/server/`. Each run starts from a fresh database.
4. **Define** `schema.tql` in a new database, through the server's HTTP API.
5. **Load**, timing each step: `load-first.tql`, then `load-second.tql`, then
   `load-between.tql`. The relation template matches both endpoints by their `@key` id
   before inserting.
6. **Validate**, only with `--validate`: check the instance counts and spot-check that
   specific CSV rows were loaded with the right values and role players. This is untimed,
   and off by default because counting a large database takes a while. Then stop the
   server.

Results, the loaders' logs and their rejects/checkpoint files go to
`run/results/<timestamp>/`. The server uses the configuration bundled with its
distribution (`server/config.yml`), except for ports, data and log directories, and
development mode.

### Python loader

`python_loader.py` loads a CSV through the TypeDB Python driver. It takes the same
arguments as `typedb loader`, so the two are interchangeable in scripts. Against a running
server (for example `run/typedb-all-*/server/typedb_server_bin`, or the one
`run_benchmark.py` starts):

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

- **Some names are reserved.** `first` and `from` are TypeQL keywords (as are `last`,
  `of` and the other statement keywords), so they can't be used as type or role labels.
  That is why the types are `first-entity` / `second-entity` and the roles are
  `source` / `target`. `$from` is still fine as a variable name.
- **Parallelism is the main throughput knob.** The default of 8 parallel batches is fixed
  rather than tied to CPU count, so results stay comparable across machines.
- **Rows that match nothing are dropped silently.** If a relation row's `from` or `to` id
  isn't found, its `match` returns no rows and nothing is inserted. The loader only warns
  when an entire batch inserts nothing, so check counts after a load, as `--validate`
  does.
- **The relation step occasionally stalls.** Its first batches can be planned from
  statistics that don't yet include the entity loads, which makes them scan instead of
  look up by id. A run that hits this shows a much slower relation step; run it again.
- **Expect run-to-run variance of about 15–20%.** Compare several runs, not single numbers.

[NOTES.md](NOTES.md) has the investigation details behind these, and notes on the Python
driver.
