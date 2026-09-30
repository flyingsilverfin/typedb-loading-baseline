#!/usr/bin/env python3
"""Generate deterministic CSV data for the TypeDB loading baseline.

  entities   an `id` column (0, 1, 2, ...) followed by value columns A, B, C, ...
             Value column i is drawn from random.Random(seed + i).
  relations  `from` and `to` columns, each a random sample of dense IDs from its own
             range (half-open LO:HI). Column i is drawn from random.Random(seed + i).

Output is identical for identical arguments, on any platform.
"""

import argparse
import csv
import random
import string
import sys

VALUE_TYPES = ("integer", "double", "string", "boolean")


def column_names(count):
    names = []
    for i in range(count):
        name, n = "", i
        while True:  # A..Z, AA, AB, ...
            name = chr(ord("A") + n % 26) + name
            n = n // 26 - 1
            if n < 0:
                break
        names.append(name)
    return names


def parse_range(text):
    try:
        lo, hi = (int(part) for part in text.split(":"))
    except ValueError:
        raise argparse.ArgumentTypeError(f"expected LO:HI, got '{text}'")
    if hi <= lo:
        raise argparse.ArgumentTypeError(f"empty range '{text}'")
    return lo, hi


def value_generator(value_type, rng, lo, hi):
    if value_type == "integer":
        return lambda: rng.randrange(lo, hi)
    if value_type == "double":
        return lambda: repr(rng.uniform(lo, hi))
    if value_type == "string":
        alphabet = string.ascii_letters + string.digits
        return lambda: "".join(rng.choices(alphabet, k=16))
    if value_type == "boolean":
        return lambda: "true" if rng.getrandbits(1) else "false"
    raise ValueError(value_type)


def generate_entities(args, out):
    names = column_names(args.columns)
    lo, hi = args.value_range
    generators = [value_generator(args.value_type, random.Random(args.seed + i), lo, hi) for i in range(args.columns)]
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["id"] + names)
    writer.writerows([row_id] + [gen() for gen in generators] for row_id in range(args.rows))


def id_sample(rng, id_range, rows, without_replacement):
    lo, hi = id_range
    if without_replacement:
        if rows > hi - lo:
            sys.exit(f"cannot sample {rows} distinct IDs from range {lo}:{hi}")
        return rng.sample(range(lo, hi), rows)
    return (rng.randrange(lo, hi) for _ in range(rows))


def generate_relations(args, out):
    ranges = [args.from_range, args.to_range]
    columns = [id_sample(random.Random(args.seed + i), r, args.rows, args.without_replacement) for i, r in enumerate(ranges)]
    writer = csv.writer(out, lineterminator="\n")
    writer.writerow(["from", "to"])
    writer.writerows(zip(*columns))


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = parser.add_subparsers(dest="table", required=True)

    entities = sub.add_parser("entities", help="entity table: id,A,B,...")
    entities.add_argument("-n", "--rows", type=int, default=1_000_000, help="number of rows (default: 1000000)")
    entities.add_argument("-c", "--columns", type=int, default=2, help="number of value columns (default: 2)")
    entities.add_argument("-t", "--value-type", choices=VALUE_TYPES, default="integer", help="value type (default: integer)")
    entities.add_argument("--value-range", type=parse_range, default=(0, 2**31),
                          help="LO:HI range for integer/double values (default: 0:2147483648)")
    entities.add_argument("--seed", type=int, default=0, help="column i is seeded with SEED + i (default: 0)")
    entities.add_argument("-o", "--output", required=True, help="output CSV path ('-' for stdout)")

    relations = sub.add_parser("relations", help="relation table: from,to")
    relations.add_argument("-n", "--rows", type=int, default=1_000_000, help="number of rows (default: 1000000)")
    relations.add_argument("--from-range", type=parse_range, default=(0, 1_000_000),
                           help="LO:HI range of dense IDs for the 'from' column (default: 0:1000000)")
    relations.add_argument("--to-range", type=parse_range, default=(0, 1_000_000),
                           help="LO:HI range of dense IDs for the 'to' column (default: 0:1000000)")
    relations.add_argument("--without-replacement", action="store_true",
                           help="sample each column without replacement (IDs within a column are distinct)")
    relations.add_argument("--seed", type=int, default=0, help="column i is seeded with SEED + i (default: 0)")
    relations.add_argument("-o", "--output", required=True, help="output CSV path ('-' for stdout)")

    args = parser.parse_args()
    generate = generate_entities if args.table == "entities" else generate_relations
    if args.output == "-":
        generate(args, sys.stdout)
    else:
        with open(args.output, "w", newline="") as out:
            generate(args, out)


if __name__ == "__main__":
    main()
