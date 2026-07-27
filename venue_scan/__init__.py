"""Inventory the lighting content of a YARG / Clone Hero song library.

For every song, in any of the four packagings the library uses (`.sng`, song
folders, STFS `_rb3con` packages, extracted CON trees), work out whether its
venue lighting is authored in the MIDI `VENUE` track, authored in a Milo, or
absent — in which case YARG synthesizes it — and inventory the concrete cues.

Entry point::

    .venv/bin/python -m venue_scan.scan --root /path/to/library

Classification mirrors YARG's own decision tree; see `classify` for the rules
and the ways they are easy to get wrong.
"""

from .classify import SongVenueRecord, classify, richness
from .discover import SongRef, discover, iter_songs

__all__ = [
    "SongRef",
    "SongVenueRecord",
    "classify",
    "discover",
    "iter_songs",
    "richness",
]
