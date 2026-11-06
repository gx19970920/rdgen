"""Disable the update check without deleting any upstream Rust code."""
from pathlib import Path
import sys

SIGNATURE = "pub async fn do_check_software_update() -> hbb_common::ResultType<()> {"
INSERTION = "\n    return Ok(()); // rdgen: update check disabled\n"


def patch(source):
    if source.count(SIGNATURE) != 1:
        raise ValueError("Expected exactly one software update function")
    if SIGNATURE + INSERTION in source:
        return source
    return source.replace(SIGNATURE, SIGNATURE + INSERTION, 1)


if __name__ == "__main__":
    path = Path(sys.argv[1]) if len(sys.argv) > 1 else Path("src/common.rs")
    path.write_text(patch(path.read_text(encoding="utf-8")), encoding="utf-8")
