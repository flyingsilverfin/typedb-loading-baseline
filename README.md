# TypeDB loading baseline

A reproducible benchmark of bulk-loading speed with [TypeDB loader](https://github.com/typedb/typedb-console/tree/master/loader)
in a deliberately basic scenario: two entity types with an integer key and two integer
attributes each, joined by a binary relation.

```bash
python3 run_benchmark.py
```

This one command downloads TypeDB, generates the data, starts a fresh server, loads
everything, validates the result and prints a summary like:

```
TypeDB 3.13.6 on aarch64, 10 CPUs; batch-rows=1000, parallel-batches=8
phase                   rows   seconds    rows/s
first-entity       1,000,000      19.3    51,716
second-entity      1,000,000      23.3    43,003
between            1,000,000      31.3    31,981
```

Requirements: Python 3.8+ (standard library only), Linux or macOS on x86_64 or arm64, and
internet access for the first run. Everything that is downloaded, generated or written
goes into `run/`, which is git-ignored. Delete it to start from scratch.

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
   before inserting.
6. **Validate** the instance counts and spot-check that specific CSV rows were loaded with
   the right values and role players. Then stop the server.

Results, the loader's logs and checkpoints go to `run/results/<timestamp>/`.
`results.json` records the TypeDB version, machine, parameters and per-step timings. It
is the file to share when reporting numbers.

## Options

```
python3 run_benchmark.py --help

  --entities N           rows per entity table (default: 1000000)
  --relations N          rows in the relation table (default: 1000000)
  --batch-rows N         loader rows per transaction (default: 1000)
  --parallel-batches N   loader concurrent transactions (default: 8)
  --version V            TypeDB version to download (default: 3.13.6)
  --typedb-home DIR      use this TypeDB distribution instead of downloading one
  --database NAME        database name (default: loading-baseline)
  --port / --http-port   server ports (default: 1729 / 8000)
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

## Notes

- `first` and `from` are reserved TypeQL keywords. That is why the entity types are
  `first-entity` / `second-entity` and the relation roles are `source` / `target`.
- Throughput depends heavily on `--parallel-batches`. On the 10-CPU machine above, rows/s
  for first-entity / second-entity / between were 10.9k / 8.1k / 6.0k at 1, 36.6k / 28.5k /
  20.0k at 4, and 51.7k / 43.0k / 32.0k at 8. The default is fixed at 8 rather than tied
  to the CPU count, so results stay comparable across machines. Report the value you used
  (it is recorded in `results.json`).
- Timings are wall-clock per loader invocation, including loader startup (well under a
  second).
- The server uses the configuration bundled with its distribution (`server/config.yml`),
  except for ports, data and log directories, and development mode.
- Windows is untested.
