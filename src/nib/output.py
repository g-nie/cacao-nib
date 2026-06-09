import functools
import os


@functools.cache
def _color_enabled(stream) -> bool:
    """Whether ANSI colour suits `stream`: a real terminal with NO_COLOR unset.
    Cached per stream object — the answer can't change for a given stream within a run."""
    return stream.isatty() and "NO_COLOR" not in os.environ


def _color(text: str, *codes: str, enabled: bool) -> str:
    """Wrap `text` in ANSI `codes` when `enabled`, else return it unchanged."""
    return f"\x1b[{';'.join(codes)}m{text}\x1b[0m" if enabled else text
