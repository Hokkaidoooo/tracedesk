from tracedesk.evaluation import evaluate_all


def test_evaluation_executes_real_workflows_and_preserves_imperfect_retrieval():
    report = evaluate_all()
    assert report["workflow"]["passed"] == report["workflow"]["total"] == 16
    assert set(report["retrieval"]) == {"bm25", "ngram", "hybrid"}
    for metrics in report["retrieval"].values():
        assert metrics["queries"] == 60
        assert 0 < metrics["recall_at_3"] < 1
        assert 0 < metrics["mrr"] < 1
    assert report["live_model"]["status"] == "not_run"
    assert len(report["dataset_sha256"]) == len(report["knowledge_sha256"]) == 64
