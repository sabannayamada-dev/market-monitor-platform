from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path


ENV_PATH = Path("/etc/patent-news-monitor.env")


def main() -> int:
    stage = int(sys.argv[1])
    if stage not in {1, 2, 3, 4}:
        raise ValueError("stage must be 1, 2, 3, or 4")
    stat = ENV_PATH.stat()
    lines = ENV_PATH.read_text(encoding="utf-8").splitlines()
    replacement = f"PAPER_OPERATION_STAGE={stage}"
    changed = False
    output: list[str] = []
    for line in lines:
        if line.startswith("PAPER_OPERATION_STAGE="):
            if not changed:
                output.append(replacement)
                changed = True
        else:
            output.append(line)
    if not changed:
        output.append(replacement)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix="patent-news-monitor.env.", dir=str(ENV_PATH.parent), text=True
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="\n") as handle:
            handle.write("\n".join(output) + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.chmod(temporary_name, stat.st_mode & 0o777)
        os.chown(temporary_name, stat.st_uid, stat.st_gid)
        os.replace(temporary_name, ENV_PATH)
    finally:
        if os.path.exists(temporary_name):
            os.unlink(temporary_name)
    print(f"PAPER_OPERATION_STAGE={stage}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
