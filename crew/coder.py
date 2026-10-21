"""Coder demo. Writes one isolated candidate artifact for its mission."""

import sys
from pathlib import Path


def main() -> None:
    work = Path(sys.argv[1])
    coder_id = int(sys.argv[2])
    note = ""
    changes = work / "changes.txt"
    if changes.is_file():
        note = " " + changes.read_text(encoding="utf-8").strip()
    product = work / f"candidate-{coder_id}.txt"
    product.write_text(f"hello from coder {coder_id}{note}\n", encoding="utf-8")


if __name__ == "__main__":
    main()
