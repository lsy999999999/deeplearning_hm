"""The attribution experiment must be able to hold the Meta policy fixed."""

import asyncio

from test_recursive_ca_rsi import make, records


def test_fixed_meta_keeps_all_training_and_audit_tasks(tmp_path):
    algorithm = make(tmp_path, meta_self_update=False)
    assert len(algorithm.benchmark.train) == 3
    assert len(algorithm.benchmark.dev) == 4
    assert algorithm._probe_train == ()
    assert algorithm._probe_dev == ()
    original = algorithm.worker.snapshot.id
    asyncio.run(algorithm.evolve())
    assert algorithm.worker.snapshot.id == original
    assert not any(
        "improve-meta" in label or "meta-probe" in label
        for label, _ in algorithm.worker.calls
    )
    assert all(
        row["reason"] == "meta_self_update_disabled"
        for row in records(algorithm, "meta_decision")
    )
    assert len(records(algorithm, "task_feedback")) == 2
