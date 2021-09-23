import fileinput
import sys
import os
from typing import Any

from tools.stats.scribe import (
    schema_from_sample,
    rds_write,
    register_rds_schema,
)


def sprint(*args: Any) -> None:
    print("[sccache_stats]", *args, file=sys.stderr)


def parse_value(value: str) -> Any:
    try:
        return int(value)
    except ValueError:
        if value.endswith(" s"):
            return float(value[: -len(" s")])

        byte_types = {
            "KiB": 2 ** 10,
            "MiB": 2 ** 20,
            "GiB": 2 ** 30,
            "TiB": 2 ** 40,
        }

        for type, multiplier in byte_types.items():
            if value.endswith(f" {type}"):
                data = value[: -len(f" {type}")]
                return int(data) * multiplier

    return value


if __name__ == "__main__":
    if os.getenv("IS_GHA", "0") == "1":
        data = {}
        for line in fileinput.input():
            line = line.strip()
            values = [x.strip() for x in line.split("  ")]
            values = [x for x in values if x != ""]
            if len(values) == 2:
                data[values[0]] = parse_value(values[1])

        # The data from sccache is always the same so this should be fine, if it
        # ever changes we will probably need to break this out so the fields
        # we want are hardcoded
        register_rds_schema("sccache_stats", schema_from_sample(data))

        rds_write(
            "sccache_stats", [data], only_on_master=False
        )  # TODO: disable this once it's been tested
        # rds_write("sccache_stats", [data])
        sprint("Wrote sccache stats to DB")
    else:
        sprint("Not in GitHub Actions, skipping")
