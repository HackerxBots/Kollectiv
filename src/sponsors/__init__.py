"""The sponsor line: one labelled, text-only sponsor message the dashboard can show.

Nothing here tracks anyone. The catalogue is a local file the operator curates,
the line is chosen by the clock (not by the viewer), and nothing about the
viewer or the project is an input.
"""

from src.sponsors.catalog import LABEL, SponsorLine, load_catalog, validate_entry
from src.sponsors.line import current_line

__all__ = ["LABEL", "SponsorLine", "current_line", "load_catalog", "validate_entry"]
