"""Download the official VerilogEval spec-to-RTL files."""

from __future__ import annotations

import argparse
import json
import time
from datetime import datetime
from pathlib import Path
from urllib.request import Request, urlopen


API_URL = "https://api.github.com/repos/NVlabs/verilog-eval/contents/dataset_spec-to-rtl?per_page=1000"
RAW_PREFIX = "https://raw.githubusercontent.com/NVlabs/verilog-eval/main/dataset_spec-to-rtl/"


def fetch(url: str, attempts: int = 4) -> bytes:
    request = Request(url, headers={"User-Agent": "siliconloop-dataset-downloader"})
    last_error = None
    for attempt in range(attempts):
        try:
            with urlopen(request, timeout=30) as response:
                return response.read()
        except OSError as exc:
            last_error = exc
            if attempt + 1 == attempts:
                break
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Could not fetch {url} after {attempts} attempts: {last_error}") from last_error


def main() -> None:
    parser = argparse.ArgumentParser(description="Download VerilogEval spec-to-RTL prompts, references, and testbenches.")
    parser.add_argument("--output", type=Path, default=Path("data/verilog_eval"))
    parser.add_argument("--limit", type=int, default=0, help="Download only the first N problems for a pilot run.")
    args = parser.parse_args()
    args.output.mkdir(parents=True, exist_ok=True)

    listing = json.loads(fetch(API_URL))
    names = sorted(item["name"] for item in listing if item["name"].endswith(("_prompt.txt", "_ref.sv", "_test.sv")))
    stems = sorted({name.rsplit("_", 1)[0] for name in names if "_" in name})
    if args.limit:
        stems = stems[: args.limit]

    downloaded = []
    for stem in stems:
        for suffix in ("_prompt.txt", "_ref.sv", "_test.sv"):
            name = f"{stem}{suffix}"
            target = args.output / name
            target.write_bytes(fetch(RAW_PREFIX + name))
            downloaded.append(name)
            time.sleep(0.02)

    manifest = {
        "source": "https://github.com/NVlabs/verilog-eval",
        "dataset": "dataset_spec-to-rtl",
        "downloaded_at": datetime.now().astimezone().isoformat(timespec="seconds"),
        "problem_count": len(stems),
        "files": downloaded,
    }
    (args.output / "manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
    print(f"Downloaded {len(stems)} problems and {len(downloaded)} files to {args.output}")


if __name__ == "__main__":
    main()
