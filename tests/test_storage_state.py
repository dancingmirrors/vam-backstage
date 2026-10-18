from vambackstage.core.storage_state import (
    DisableBehavior,
    StorageTarget,
    compute_install_target,
    disable_behavior_move_to,
    next_storage_state_for_intent,
    parse_disable_behavior,
    plan_resettle,
)


def test_parse_disable_behavior():
    for v in (None, "", "suffix", "move-to:abc", "move-to:", "move-to", "garbage"):
        assert parse_disable_behavior(v) == DisableBehavior("suffix")
    assert parse_disable_behavior("move-to:7") == DisableBehavior("move-to", 7)
    assert parse_disable_behavior(disable_behavior_move_to(1)) == DisableBehavior("move-to", 1)


def test_next_storage_state_for_intent():
    suffix = StorageTarget("disabled", None)
    offload = StorageTarget("offloaded", 3)
    assert next_storage_state_for_intent("disabled", "enable") == StorageTarget("enabled", None)
    assert next_storage_state_for_intent("offloaded", "enable") == StorageTarget("enabled", None)
    assert next_storage_state_for_intent("archived", "enable") == StorageTarget("enabled", None)
    assert next_storage_state_for_intent("enabled", "enable") is None
    assert next_storage_state_for_intent("enabled", "disable", suffix) == suffix
    assert next_storage_state_for_intent("enabled", "disable") == suffix
    assert next_storage_state_for_intent("enabled", "disable", offload) == offload
    assert next_storage_state_for_intent("disabled", "disable") is None
    assert next_storage_state_for_intent("offloaded", "disable") is None
    assert next_storage_state_for_intent("archived", "disable") is None
    assert next_storage_state_for_intent("enabled", "whatever") is None


def mk(state, d=None, direct=False):
    return {"storage_state": state, "library_dir_id": d, "is_direct": 1 if direct else 0}


class TestComputeInstallTarget:
    def test_none_when_no_dependents(self):
        assert compute_install_target(None, {}) is None
        assert compute_install_target(set(), {}) is None

    def test_enabled_dominates(self):
        idx = {"a": mk("enabled"), "b": mk("disabled"), "c": mk("offloaded", 1)}
        assert compute_install_target({"a", "b", "c"}, idx) is None
        assert compute_install_target({"a", "b"}, {"a": mk("enabled"), "b": mk("archived", 8)}) is None

    def test_disabled(self):
        assert compute_install_target({"a", "b"}, {"a": mk("disabled"), "b": mk("disabled")}) == StorageTarget(
            "disabled", None
        )
        assert compute_install_target({"a", "b"}, {"a": mk("disabled"), "b": mk("offloaded", 1)}) == StorageTarget(
            "disabled", None
        )
        assert compute_install_target({"a", "b"}, {"a": mk("disabled"), "b": mk("archived", 8)}) == StorageTarget(
            "disabled", None
        )
        assert compute_install_target({"ghost", "a"}, {"a": mk("disabled")}) == StorageTarget("disabled", None)

    def test_offloaded(self):
        assert compute_install_target({"a", "b"}, {"a": mk("offloaded", 5), "b": mk("offloaded", 5)}) == StorageTarget(
            "offloaded", 5
        )
        assert compute_install_target(
            ["a", "b"], {"a": mk("offloaded", 1), "b": mk("offloaded", 2)}, 2
        ) == StorageTarget("offloaded", 2)
        assert compute_install_target({"a"}, {"a": mk("offloaded", 7)}, 99) == StorageTarget("offloaded", 7)
        assert compute_install_target({"a", "b"}, {"a": mk("offloaded", 5), "b": mk("archived", 8)}) == StorageTarget(
            "offloaded", 5
        )

    def test_archived(self):
        assert compute_install_target({"a", "b"}, {"a": mk("archived", 8), "b": mk("archived", 8)}) == StorageTarget(
            "archived", 8
        )


class TestPlanResettle:
    def test_prune_redownloadable(self):
        idx = {"A.var": mk("archived", 9), "B.var": mk("enabled")}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, {"B.var"})
        assert plan.to_prune == {"B.var"} and plan.decisions == {}

    def test_relocate_local_only(self):
        idx = {"A.var": mk("archived", 9), "B.var": mk("enabled")}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, set())
        assert plan.to_prune == set() and plan.decisions == {"B.var": StorageTarget("archived", 9)}

    def test_store_mode(self):
        idx = {"A.var": mk("archived", 9), "B.var": mk("enabled")}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, {"B.var"}, prune=False)
        assert plan.to_prune == set() and plan.decisions == {"B.var": StorageTarget("archived", 9)}

    def test_settle_down_to_offloaded(self):
        idx = {"A.var": mk("offloaded", 5), "B.var": mk("enabled")}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, {"B.var"})
        assert plan.decisions == {"B.var": StorageTarget("offloaded", 5)}

    def test_never_direct_or_pinned_or_orphan(self):
        idx = {"A.var": mk("archived", 9), "B.var": mk("enabled", direct=True)}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, {"B.var"})
        assert not plan.to_prune and not plan.decisions
        idx = {"A.var": mk("enabled"), "B.var": mk("enabled")}
        plan = plan_resettle(["B.var"], idx, {"B.var": {"A.var"}}, {"B.var"})
        assert not plan.to_prune and not plan.decisions
        plan = plan_resettle(["B.var"], {"B.var": mk("enabled")}, {}, {"B.var"})
        assert not plan.to_prune and not plan.decisions

    def test_order_independence_prune(self):
        def fresh():
            return (
                {"A.var": mk("enabled", direct=True), "B.var": mk("enabled", direct=True), "C.var": mk("enabled")},
                {"C.var": {"A.var", "B.var"}},
                {"C.var"},
            )

        idx, rev, rep = fresh()
        idx["A.var"] = mk("archived", 9, direct=True)
        assert plan_resettle(["C.var"], idx, rev, rep).to_prune == set()
        rev["C.var"] = {"A.var"}
        assert plan_resettle(["C.var"], idx, rev, rep).to_prune == {"C.var"}
        idx, rev, rep = fresh()
        rev["C.var"] = {"A.var"}
        assert plan_resettle(["C.var"], idx, rev, rep).to_prune == set()
        idx["A.var"] = mk("archived", 9, direct=True)
        assert plan_resettle(["C.var"], idx, rev, rep).to_prune == {"C.var"}

    def test_chain_propagation(self):
        idx = {"A.var": mk("archived", 9, direct=True), "B.var": mk("enabled"), "C.var": mk("enabled")}
        rev = {"B.var": {"A.var"}, "C.var": {"B.var"}}
        plan = plan_resettle(["C.var", "B.var"], idx, rev, set())
        assert plan.to_prune == set()
        assert plan.decisions == {"B.var": StorageTarget("archived", 9), "C.var": StorageTarget("archived", 9)}

    def test_prune_supersedes_settle(self):
        idx = {"A.var": mk("archived", 9, direct=True), "B.var": mk("offloaded", 5), "C.var": mk("enabled")}
        rev = {"B.var": {"A.var"}, "C.var": {"A.var", "B.var"}}
        plan = plan_resettle(["C.var", "B.var"], idx, rev, {"B.var", "C.var"})
        assert plan.to_prune == {"B.var", "C.var"} and plan.decisions == {}

    def test_revert_when_drivers_pruned(self):
        idx = {"A.var": mk("archived", 9, direct=True), "B.var": mk("offloaded", 5), "C.var": mk("enabled")}
        rev = {"B.var": {"A.var"}, "C.var": {"B.var"}}
        plan = plan_resettle(["C.var", "B.var"], idx, rev, {"B.var", "C.var"})
        assert plan.to_prune == {"B.var"} and plan.decisions == {}

    def test_order_independence_relocate(self):
        for first in ("archive", "lose"):
            idx = {"A.var": mk("enabled", direct=True), "B.var": mk("enabled", direct=True), "C.var": mk("enabled")}
            rev = {"C.var": {"A.var", "B.var"}}
            if first == "archive":
                idx["A.var"] = mk("archived", 9, direct=True)
                rev["C.var"] = {"A.var"}
            else:
                rev["C.var"] = {"A.var"}
                idx["A.var"] = mk("archived", 9, direct=True)
            plan = plan_resettle(["C.var"], idx, rev, set())
            assert plan.to_prune == set() and plan.decisions == {"C.var": StorageTarget("archived", 9)}
