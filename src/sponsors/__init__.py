"""The sponsor line: one labelled, text-only sponsor message the dashboard can show.

Campaigns are paid per click and capped (see ``catalog``). Views and clicks are
counted on the operator's own server with no identifiers (see ``ledger``). The
catalogue is a local file. Nothing about the viewer or the project is an input.
"""

from src.sponsors import ledger
from src.sponsors.catalog import LABEL, SponsorLine, load_catalog, validate_entry
from src.sponsors.line import current_line, go_target

__all__ = ["LABEL", "SponsorLine", "current_line", "go_target", "ledger", "load_catalog", "validate_entry"]
