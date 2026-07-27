"""YARG's normalized `VenueTrack` model, reduced to what classification needs.

Events are plain tuples rather than dataclasses: a full library scan builds tens
of millions of them, and we only ever count and histogram them.
"""

from __future__ import annotations

from collections import Counter
from dataclasses import dataclass, field


@dataclass
class VenueTrack:
    #: (tick, lighting_type)
    lighting: list[tuple[int, str]] = field(default_factory=list)
    #: (tick, post_processing_type)
    post_processing: list[tuple[int, str]] = field(default_factory=list)
    #: (tick, "spotlight" | "singalong", frozenset[performer])
    performer: list[tuple[int, str, frozenset]] = field(default_factory=list)
    #: (tick, stage_effect, frozenset[flag])
    stage: list[tuple[int, str, frozenset]] = field(default_factory=list)
    #: (tick, subject, frozenset[constraint], tuple[random_choices])
    camera_cuts: list[tuple[int, str, frozenset, tuple]] = field(default_factory=list)

    @property
    def is_empty(self) -> bool:
        """VenueTrack.IsEmpty — gates the Milo fallback. All five must be empty."""
        return not (
            self.lighting
            or self.post_processing
            or self.performer
            or self.stage
            or self.camera_cuts
        )

    @property
    def has_fog(self) -> bool:
        """VenueAutoGenerationPreset.ChartHasFog — gates fog auto-generation."""
        return any(effect in ("fog_on", "fog_off") for _tick, effect, _f in self.stage)

    def extend(self, other: VenueTrack) -> None:
        self.lighting.extend(other.lighting)
        self.post_processing.extend(other.post_processing)
        self.performer.extend(other.performer)
        self.stage.extend(other.stage)
        self.camera_cuts.extend(other.camera_cuts)

    def counts(self) -> dict[str, int]:
        return {
            "lighting": len(self.lighting),
            "post_processing": len(self.post_processing),
            "performer": len(self.performer),
            "stage": len(self.stage),
            "camera_cuts": len(self.camera_cuts),
        }

    def histograms(self) -> dict[str, dict[str, int]]:
        """Per-category frequency of each concrete cue type."""
        performers: Counter[str] = Counter()
        for _tick, kind, members in self.performer:
            for member in members:
                performers[f"{kind}:{member}"] += 1
        return {
            "lighting": dict(Counter(t for _tick, t in self.lighting).most_common()),
            "post_processing": dict(
                Counter(t for _tick, t in self.post_processing).most_common()
            ),
            "performer": dict(performers.most_common()),
            "stage": dict(
                Counter(e for _tick, e, _f in self.stage).most_common()
            ),
            "camera_cuts": dict(
                Counter(s for _tick, s, _c, _r in self.camera_cuts).most_common()
            ),
        }
