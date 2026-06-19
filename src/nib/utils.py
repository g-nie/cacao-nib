import functools
from importlib import metadata


@functools.cache
def nib_version() -> str:
    try:
        return metadata.version("cacao-nib")
    except metadata.PackageNotFoundError:
        return "unknown"
