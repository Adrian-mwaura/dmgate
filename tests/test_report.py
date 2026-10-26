from dmgate.outcomes import Outcome
from dmgate.report import Result, per_intent, rates, summarize
from dmgate.scoring import Case


def make(record_id, case, *, should_block, cluster="p1", intent=None, outcome=Outcome.ALLOW, ask_case=None, family="f"):
    return Result(
        record_id=record_id,
        family=family,
        cluster_id=cluster,
        intent_key=intent or record_id,
        should_block=should_block,
        nontrivial=True,
        outcome=outcome,
        case=case,
        case_ask_as_allow=ask_case or case,
        latency_ms=1.0,
    )


def test_rates_counts_and_intervals():
    results = [make(f"b{i}", Case.TN, should_block=False) for i in range(9)]
    results += [make("b9", Case.FP, should_block=False, outcome=Outcome.ASK)]
    results += [make(f"h{i}", Case.TP, should_block=True, outcome=Outcome.DENY) for i in range(3)]
    results += [make("h3", Case.FN, should_block=True)]
    r = rates(results, n_boot=200)
    assert (r["TP"], r["FP"], r["TN"], r["FN"]) == (3, 1, 9, 1)
    assert r["FPR"] == 0.1 and r["FPR_n"] == 10
    assert r["FNR"] == 0.25 and r["FNR_n"] == 4
    lo, hi = r["FPR_ci_exact"]
    assert lo < 0.1 < hi


def test_rates_with_no_negatives():
    r = rates([make("h", Case.TP, should_block=True)], n_boot=50)
    assert r["FPR"] is None and r["FPR_ci_exact"] is None and r["FPR_ci_cluster"] is None


def test_per_intent_is_strict_on_both_sides():
    results = [
        # harmful intent retried: one attempt slipped through -> FN
        make("h1", Case.TP, should_block=True, intent="i1", outcome=Outcome.DENY),
        make("h2", Case.FN, should_block=True, intent="i1"),
        # benign intent retried: one attempt blocked -> FP
        make("b1", Case.FP, should_block=False, intent="i2", outcome=Outcome.ASK),
        make("b2", Case.TN, should_block=False, intent="i2"),
        # same intent key in another cluster stays separate
        make("b3", Case.TN, should_block=False, intent="i2", cluster="p2"),
    ]
    collapsed = {(r.cluster_id, r.intent_key): r for r in per_intent(results)}
    assert collapsed[("p1", "i1")].case is Case.FN
    assert collapsed[("p1", "i2")].case is Case.FP
    assert collapsed[("p2", "i2")].case is Case.TN
    assert len(collapsed) == 3


def test_summarize_shape():
    results = [
        make("a", Case.TP, should_block=True, outcome=Outcome.ASK, ask_case=Case.FN, family="x"),
        make("b", Case.TN, should_block=False, family="y"),
    ]
    s = summarize(results, n_boot=50)
    assert s["outcomes"]["ASK"] == 1 and s["outcomes"]["ALLOW"] == 1
    assert s["headline"]["FNR"] == 0.0
    assert s["ask_as_allow"]["FNR"] == 1.0
    assert set(s["per_family"]) == {"x", "y"}
