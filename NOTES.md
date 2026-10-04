# Notes

Investigation details behind the Lessons in the [README](README.md). Measurements are from
a 10-CPU, 32 GB arm64 Linux machine running TypeDB 3.13.6.

## typedb loader and the server

- **The loader is its own binary.** It is not console's `load` command. It ships in the
  `typedb-all-<os>-<arch>` archive and as a standalone `typedb-loader-<os>-<arch>` package:
  `.tar.gz` on Linux, `.zip` on macOS and Windows.
- **Development mode is a hidden flag,** `--development-mode.enabled=true`. In the server
  code, it only disables diagnostics and error reporting, so it doesn't affect load speed.
  The benchmark also passes `--diagnostics.monitoring.enabled=false`, which turns off the
  server's monitoring endpoint (port 4104 by default).
- **Parallelism.** Going from `--parallel-batches` 1 to 4 to 8 gave about
  11k → 37k → 52k rows/s for first-entity, and 6k → 20k → 32k rows/s for relations.
- **Starting the relation load right after the entity loads can stall.** On a fresh
  20k-entity database, about half of such runs stalled for 20–30s, versus 0.7s normally.
  During the stall the server burned about 150s of CPU on 8 cores. The first in-flight
  batches were slow and everything after them was fast. The query profile
  (`RUST_LOG=info,query::query_manager=trace,database::query=trace`) confirmed the cause:
  those batches were planned from statistics that didn't yet include the second entity
  load. The planner then scanned every `second-entity` `has` edge for each input row (60M
  storage advances per 1,000-row batch) instead of looking up `id` by value. A 3s pause
  prevented it at small scale, but at 1M rows it still hit 4 of 8 runs with 10k-row
  batches. Stalled batches can run into the 300s transaction timeout and be rejected.
  The benchmark doesn't pause, so a run that hits this shows a much slower relation
  step. An earlier version had a `--restart-after-schema` option, working around a
  related statistics issue (TypeDB PR #7981, where a schema commit stopped the statistics
  updater applying data commits until a restart); it was removed once no longer needed.
- The package repository (repo.typedb.com) returns 403 to Python's default `urllib`
  User-Agent. `download_typedb.py` sends its own.

## Python driver

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
- **After that fix, it matches typedb loader.** At 8 threads, across 1M-row runs, both
  loaders landed in the same range: 42–55k rows/s for the first entity type, 34–51k for
  the second, and 27–37k for relations. The differences were within run-to-run noise.
  Parsing and batch building on the producer takes about 5ms per 1,000 rows (about 200k
  rows/s), which is well above what the server absorbs here.
- **A Python without pip still works.** `python3 -m venv` bootstraps pip into the
  virtualenv, so the benchmark installs the pinned driver (`requirements.txt`) into
  `run/venv` even where the system Python has no `pip` module.
