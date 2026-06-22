"""Mahalath (MPL) — multi-agent ontology builder.

See `docs/project-brief.md` for the project's purpose and
`docs/architecture-decisions.md` for the load-bearing decisions.
"""

# Single source of truth is the package metadata (pyproject `version`), so the
# installed wheel and `mahalath.__version__` can never disagree.
from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("mahalath")
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
