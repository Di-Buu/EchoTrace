from app.services.retrieval import PersonalMemoryRetriever


def test_temporal_diversification_keeps_semantic_hits_and_time_span() -> None:
    rows = [
        {
            "moment_id": f"moment-{index}",
            "occurred_at": f"2026-{index + 1:02d}-01T00:00:00Z",
            "score": 1 - index / 20,
        }
        for index in range(10)
    ]

    result = PersonalMemoryRetriever._diversify_time(rows, limit=6)
    ids = {item["moment_id"] for item in result}

    assert len(result) == 6
    assert {"moment-0", "moment-1", "moment-2"}.issubset(ids)
    assert "moment-9" in ids
    assert any(item in ids for item in {"moment-4", "moment-5"})
