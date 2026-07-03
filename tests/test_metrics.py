from juara_station.metrics import diversity_from_counts, format_detection


def test_diversity_metrics_for_multiple_species():
    metrics = diversity_from_counts({"A": 3, "B": 1})

    assert metrics.species_richness == 2
    assert metrics.total_calls == 4
    assert metrics.top_species == "A"
    assert round(metrics.shannon, 3) == 0.562
    assert round(metrics.simpson, 3) == 0.375
    assert round(metrics.pielou_evenness, 3) == 0.811


def test_format_detection_percent():
    assert format_detection("Jaguar", "count", 1, 0.8234) == "Jaguar(count: 1, Conf. 82.3%)"
