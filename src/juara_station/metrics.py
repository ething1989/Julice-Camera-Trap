from __future__ import annotations

from dataclasses import dataclass
from math import log


@dataclass(frozen=True)
class DiversityMetrics:
    shannon: float | None
    simpson: float | None
    pielou_evenness: float | None
    species_richness: int
    total_calls: int
    top_species: str | None


def diversity_from_counts(counts: dict[str, int]) -> DiversityMetrics:
    cleaned = {species: count for species, count in counts.items() if species and count > 0}
    total = sum(cleaned.values())
    richness = len(cleaned)
    if total == 0:
        return DiversityMetrics(None, None, None, 0, 0, None)

    proportions = [count / total for count in cleaned.values()]
    shannon = -sum(p * log(p) for p in proportions)
    simpson = 1 - sum(p * p for p in proportions)
    pielou = shannon / log(richness) if richness > 1 else 0.0
    top_species = max(cleaned.items(), key=lambda item: item[1])[0]
    return DiversityMetrics(shannon, simpson, pielou, richness, total, top_species)


def format_detection(name: str, count_label: str, count: int, confidence: float | None) -> str:
    if confidence is None:
        return f"{name}({count_label}: {count}, Conf. )"
    return f"{name}({count_label}: {count}, Conf. {confidence * 100:.1f}%)"
