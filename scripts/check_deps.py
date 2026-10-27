"""Exit 1 if any dependency listed in pyproject.toml (incl. the dev extra) is missing or the
package itself is not installed - `make deps` then runs pip install."""
import re
import sys
import tomllib
from importlib import metadata
from pathlib import Path

proj = tomllib.loads((Path(__file__).resolve().parents[1] / "pyproject.toml").read_text())["project"]
reqs = proj["dependencies"] + proj.get("optional-dependencies", {}).get("dev", [])
missing = []
for req in reqs + ["quote-system"]:
    name = re.split(r"[\s<>=!~\[;]", req, maxsplit=1)[0]
    try:
        metadata.distribution(name)
    except metadata.PackageNotFoundError:
        missing.append(name)
if missing:
    print("missing: " + ", ".join(missing))
    sys.exit(1)
