from vambackstage.core.graph import (
    DepEdge,
    build_forward_deps,
    build_group_index,
    build_reverse_deps,
    compute_cascade_disable,
    compute_cascade_enable,
    compute_orphan_cascade,
    compute_removable_deps,
    detect_leaves,
    extract_dep_refs,
    get_transitive_deps,
    is_flexible_ref,
    parse_dep_ref,
    resolve_ref,
)


def pkg(filename, **opts):
    parts = filename.removesuffix(".var").split(".")
    return {
        "filename": filename,
        "package_name": opts.get("package_name", ".".join(parts[:-1])),
        "version": opts.get("version", parts[-1]),
        "dep_refs": opts.get("dep_refs", "[]"),
        "is_direct": opts.get("is_direct", 0),
        "size_bytes": opts.get("size_bytes", 100),
        "storage_state": opts.get("storage_state", "enabled"),
    }


def edge(ref, resolved, resolution="exact"):
    return DepEdge(ref, resolved, resolution)


class TestParseDepRef:
    def test_numeric(self):
        r = parse_dep_ref("Author.Pkg.42")
        assert (r.creator, r.package_name, r.version, r.raw, r.min_version) == (
            "Author",
            "Author.Pkg",
            "42",
            "Author.Pkg.42",
            None,
        )

    def test_latest(self):
        r = parse_dep_ref("Author.Pkg.latest")
        assert (r.version, r.package_name) == ("latest", "Author.Pkg")
        assert parse_dep_ref("Author.Pkg.LATEST").version == "latest"
        assert parse_dep_ref("Author.Pkg.Latest").version == "latest"
        assert parse_dep_ref("Author.Pkg.LATEST").raw == "Author.Pkg.LATEST"

    def test_multi_dot(self):
        r = parse_dep_ref("A.B.C.123")
        assert (r.creator, r.package_name, r.version) == ("A", "A.B.C", "123")

    def test_zero(self):
        assert parse_dep_ref("A.B.0").version == "0"

    def test_invalid(self):
        for v in ("OnlyTwo.Parts", "NoDots", "", None, 42, "A.B.beta", "A.B.1a", "A.B.v2"):
            assert parse_dep_ref(v) is None

    def test_min(self):
        r = parse_dep_ref("Author.Pkg.min5")
        assert (r.creator, r.package_name, r.version, r.min_version, r.raw) == (
            "Author",
            "Author.Pkg",
            "min",
            5,
            "Author.Pkg.min5",
        )
        assert (parse_dep_ref("A.B.MIN10").version, parse_dep_ref("A.B.MIN10").min_version) == ("min", 10)
        assert parse_dep_ref("A.B.Min42").min_version == 42
        assert parse_dep_ref("A.B.min0").min_version == 0
        r = parse_dep_ref("A.B.C.min7")
        assert (r.creator, r.package_name, r.version, r.min_version) == ("A", "A.B.C", "min", 7)
        for v in ("A.B.min", "A.B.minbeta", "A.B.min-1", "A.B.min5x"):
            assert parse_dep_ref(v) is None


def test_is_flexible_ref():
    assert is_flexible_ref(parse_dep_ref("A.B.latest"))
    assert is_flexible_ref(parse_dep_ref("A.B.min5"))
    assert not is_flexible_ref(parse_dep_ref("A.B.42"))
    assert not is_flexible_ref(None)


class TestExtractDepRefs:
    def test_dict_and_array(self):
        assert extract_dep_refs({"dependencies": {"A.B.1": "https://example.com", "C.D.2": ""}}, "Self.Pkg.1.var") == [
            "A.B.1",
            "C.D.2",
        ]
        assert extract_dep_refs({"dependencies": ["A.B.1", "C.D.2"]}, "Self.Pkg.1.var") == ["A.B.1", "C.D.2"]

    def test_normalises_casing(self):
        assert extract_dep_refs({"dependencies": ["A.B.LATEST", "C.D.Latest"]}, "S.P.1.var") == [
            "A.B.latest",
            "C.D.latest",
        ]
        assert extract_dep_refs({"dependencies": ["A.B.MIN5", "C.D.Min10"]}, "S.P.1.var") == ["A.B.min5", "C.D.min10"]
        assert extract_dep_refs({"dependencies": ["A.B.42"]}, "X.Y.1.var") == ["A.B.42"]

    def test_self_refs(self):
        assert extract_dep_refs({"dependencies": ["A.B.1", "Self.Pkg.1"]}, "Self.Pkg.1.var") == ["A.B.1"]
        assert extract_dep_refs({"dependencies": ["Self.Pkg.1"]}, "Self.Pkg.1.VAR") == []

    def test_empty(self):
        assert extract_dep_refs({}, "X.Y.1.var") == []
        assert extract_dep_refs(None, "X.Y.1.var") == []
        assert extract_dep_refs({"dependencies": {}}, "X.Y.1.var") == []
        assert extract_dep_refs({"dependencies": []}, "X.Y.1.var") == []

    def test_skips_non_strings_and_keeps_short(self):
        assert extract_dep_refs({"dependencies": [123, None, "A.B.1"]}, "S.P.1.var") == ["A.B.1"]
        assert extract_dep_refs({"dependencies": ["TwoParts", "A.B.1"]}, "S.P.1.var") == ["TwoParts", "A.B.1"]

    def test_nested(self):
        meta = {
            "dependencies": {
                "A.B.3": {"licenseType": "CC BY-SA", "dependencies": {}},
                "C.D.latest": {
                    "licenseType": "CC BY-NC",
                    "dependencies": {
                        "E.F.latest": {"licenseType": "FC", "dependencies": {}},
                        "G.H.2": {"licenseType": "CC BY", "dependencies": {}},
                    },
                },
            }
        }
        assert sorted(extract_dep_refs(meta, "S.P.1.var")) == ["A.B.3", "C.D.latest", "E.F.latest", "G.H.2"]

    def test_nested_dedup_and_deep(self):
        meta = {
            "dependencies": {
                "A.B.latest": {"licenseType": "FC", "dependencies": {}},
                "C.D.latest": {
                    "licenseType": "CC BY-NC",
                    "dependencies": {"A.B.latest": {"licenseType": "FC", "dependencies": {}}},
                },
            }
        }
        assert extract_dep_refs(meta, "S.P.1.var") == ["A.B.latest", "C.D.latest"]
        meta = {
            "dependencies": {
                "A.B.1": {
                    "licenseType": "FC",
                    "dependencies": {
                        "C.D.1": {
                            "licenseType": "FC",
                            "dependencies": {"E.F.1": {"licenseType": "FC", "dependencies": {}}},
                        }
                    },
                }
            }
        }
        assert sorted(extract_dep_refs(meta, "X.Y.1.var")) == ["A.B.1", "C.D.1", "E.F.1"]
        meta = {
            "dependencies": {
                "A.B.LATEST": {
                    "licenseType": "FC",
                    "dependencies": {"C.D.Latest": {"licenseType": "FC", "dependencies": {}}},
                }
            }
        }
        assert extract_dep_refs(meta, "X.Y.1.var") == ["A.B.latest", "C.D.latest"]
        meta = {
            "dependencies": {
                "A.B.1": {
                    "licenseType": "FC",
                    "dependencies": {"Self.Pkg.1": {"licenseType": "FC", "dependencies": {}}},
                }
            }
        }
        assert extract_dep_refs(meta, "Self.Pkg.1.var") == ["A.B.1"]
        meta = {"dependencies": {"A.B.1": {"licenseType": "CC BY-SA"}, "C.D.2": {"licenseType": "FC"}}}
        assert extract_dep_refs(meta, "X.Y.1.var") == ["A.B.1", "C.D.2"]


class TestResolveRef:
    def test_exact(self):
        pi = {"A.Pkg.5.var": pkg("A.Pkg.5.var")}
        gi = {"A.Pkg": ["A.Pkg.5.var"]}
        assert resolve_ref("A.Pkg.5", pi, gi) == ("A.Pkg.5.var", "exact")

    def test_latest(self):
        pi = {n: pkg(n) for n in ("A.Pkg.3.var", "A.Pkg.10.var", "A.Pkg.7.var")}
        gi = {"A.Pkg": list(pi)}
        assert resolve_ref("A.Pkg.latest", pi, gi) == ("A.Pkg.10.var", "latest")

    def test_fallback(self):
        pi = {n: pkg(n) for n in ("A.Pkg.3.var", "A.Pkg.7.var")}
        gi = {"A.Pkg": list(pi)}
        assert resolve_ref("A.Pkg.5", pi, gi) == ("A.Pkg.7.var", "fallback")

    def test_missing_and_invalid(self):
        assert resolve_ref("A.Pkg.5", {}, {}) == (None, "missing")
        assert resolve_ref("garbage", {}, {}) == (None, "invalid")
        assert resolve_ref("A.B.beta", {}, {}) == (None, "invalid")

    def test_non_numeric_candidates_skipped(self):
        pi = {"A.Pkg.nightly.var": pkg("A.Pkg.nightly.var", version="nightly")}
        gi = {"A.Pkg": ["A.Pkg.nightly.var"]}
        assert resolve_ref("A.Pkg.latest", pi, gi) == (None, "missing")

    def test_latest_single(self):
        pi = {"A.Pkg.1.var": pkg("A.Pkg.1.var")}
        assert resolve_ref("A.Pkg.latest", pi, {"A.Pkg": ["A.Pkg.1.var"]}) == ("A.Pkg.1.var", "latest")

    def test_latest_never_literal_file(self):
        pi = {"A.Pkg.latest.var": pkg("A.Pkg.latest.var", version="latest"), "A.Pkg.5.var": pkg("A.Pkg.5.var")}
        gi = {"A.Pkg": list(pi)}
        assert resolve_ref("A.Pkg.latest", pi, gi) == ("A.Pkg.5.var", "latest")
        pi = {"A.Pkg.latest.var": pkg("A.Pkg.latest.var", version="latest")}
        assert resolve_ref("A.Pkg.latest", pi, {"A.Pkg": list(pi)}) == (None, "missing")

    def test_exact_preferred(self):
        pi = {n: pkg(n) for n in ("A.Pkg.5.var", "A.Pkg.10.var")}
        assert resolve_ref("A.Pkg.5", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.5.var", "exact")

    def test_min(self):
        pi = {n: pkg(n) for n in ("A.Pkg.7.var", "A.Pkg.9.var")}
        assert resolve_ref("A.Pkg.min5", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.9.var", "latest")
        pi = {n: pkg(n) for n in ("A.Pkg.3.var", "A.Pkg.6.var", "A.Pkg.4.var")}
        assert resolve_ref("A.Pkg.min5", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.6.var", "latest")
        pi = {n: pkg(n) for n in ("A.Pkg.3.var", "A.Pkg.4.var")}
        assert resolve_ref("A.Pkg.min5", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.4.var", "fallback")
        assert resolve_ref("A.Pkg.min5", {}, {}) == (None, "missing")
        pi = {"A.Pkg.min5.var": pkg("A.Pkg.min5.var", version="min5"), "A.Pkg.8.var": pkg("A.Pkg.8.var")}
        assert resolve_ref("A.Pkg.min5", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.8.var", "latest")
        pi = {n: pkg(n) for n in ("A.Pkg.1.var", "A.Pkg.3.var")}
        assert resolve_ref("A.Pkg.min0", pi, {"A.Pkg": list(pi)}) == ("A.Pkg.3.var", "latest")


class TestBuilders:
    def test_forward(self):
        pi = {
            "A.Main.1.var": pkg("A.Main.1.var", dep_refs='["B.Lib.1", "C.Missing.1"]'),
            "B.Lib.1.var": pkg("B.Lib.1.var"),
        }
        fwd = build_forward_deps(pi, build_group_index(pi))
        assert fwd["A.Main.1.var"] == [edge("B.Lib.1", "B.Lib.1.var"), edge("C.Missing.1", None, "missing")]
        assert fwd["B.Lib.1.var"] == []
        assert len(fwd) == 2

    def test_forward_accepts_list_dep_refs(self):
        pi = {"A.Main.1.var": pkg("A.Main.1.var", dep_refs=["B.Lib.1"]), "B.Lib.1.var": pkg("B.Lib.1.var")}
        fwd = build_forward_deps(pi, build_group_index(pi))
        assert fwd["A.Main.1.var"] == [edge("B.Lib.1", "B.Lib.1.var")]

    def test_reverse(self):
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        assert build_reverse_deps(fwd) == {"B.var": {"A.var"}}
        fwd = {"A.var": [edge("D.Lib.1", "D.var")], "B.var": [edge("D.Lib.1", "D.var")], "D.var": []}
        assert build_reverse_deps(fwd) == {"D.var": {"A.var", "B.var"}}
        assert build_reverse_deps({"A.var": [edge("M.L.1", None, "missing")]}) == {}
        assert build_reverse_deps({"A.var": [], "B.var": []}) == {}

    def test_group_index(self):
        pi = {"A.Pkg.1.var": pkg("A.Pkg.1.var"), "A.Pkg.2.var": pkg("A.Pkg.2.var")}
        assert build_group_index(pi) == {"A.Pkg": ["A.Pkg.1.var", "A.Pkg.2.var"]}
        pi = {"A.Foo.1.var": pkg("A.Foo.1.var"), "B.Bar.1.var": pkg("B.Bar.1.var")}
        assert build_group_index(pi) == {"A.Foo": ["A.Foo.1.var"], "B.Bar": ["B.Bar.1.var"]}
        assert build_group_index({}) == {}


class TestDetectLeaves:
    def test_cases(self):
        pi = {n: pkg(n) for n in ("A.var", "B.var", "C.var")}
        assert detect_leaves(pi, {"B.var": {"A.var"}}) == {"A.var", "C.var"}
        pi = {n: pkg(n) for n in ("A.var", "B.var")}
        assert detect_leaves(pi, {}) == {"A.var", "B.var"}
        pi = {n: pkg(n) for n in ("A.var", "B.var", "C.var")}
        assert detect_leaves(pi, {"B.var": {"A.var"}, "C.var": {"B.var"}}) == {"A.var"}
        pi = {n: pkg(n) for n in ("A.var", "B.var", "C.var", "D.var")}
        assert detect_leaves(pi, {"B.var": {"A.var"}, "C.var": {"A.var"}, "D.var": {"B.var", "C.var"}}) == {"A.var"}
        assert detect_leaves({}, {}) == set()


class TestTransitiveDeps:
    def test_cases(self):
        assert get_transitive_deps("A", {"A": [edge("", "B")], "B": [edge("", "C")], "C": []}) == {"B", "C"}
        fwd = {"A": [edge("", "B"), edge("", "C")], "B": [edge("", "D")], "C": [edge("", "D")], "D": []}
        assert get_transitive_deps("A", fwd) == {"B", "C", "D"}
        assert get_transitive_deps("A", {"A": [edge("", "B")], "B": [edge("", "A")]}) == {"A", "B"}
        assert get_transitive_deps("A", {"A": [edge("", "B")], "B": [edge("", "C")], "C": [edge("", "A")]}) == {
            "A",
            "B",
            "C",
        }
        assert get_transitive_deps("A", {"A": []}) == set()
        assert get_transitive_deps("X", {}) == set()
        assert "A" not in get_transitive_deps("A", {"A": [edge("", "B")], "B": []})
        assert get_transitive_deps(
            "A", {"A": [edge("", "B"), edge("", None, "missing")], "B": [edge("", "C")], "C": []}
        ) == {"B", "C"}

    def test_with_real_refs(self):
        pi = {
            "A.Main.1.var": pkg("A.Main.1.var", is_direct=1, dep_refs='["B.Lib.1"]'),
            "B.Lib.1.var": pkg("B.Lib.1.var", dep_refs='["C.Util.1"]'),
            "C.Util.1.var": pkg("C.Util.1.var", dep_refs='["D.Core.1"]'),
            "D.Core.1.var": pkg("D.Core.1.var"),
        }
        fwd = build_forward_deps(pi, build_group_index(pi))
        assert get_transitive_deps("A.Main.1.var", fwd) == {"B.Lib.1.var", "C.Util.1.var", "D.Core.1.var"}
        pi = {
            "A.Pkg.1.var": pkg("A.Pkg.1.var", dep_refs='["B.Pkg.1"]'),
            "B.Pkg.1.var": pkg("B.Pkg.1.var", dep_refs='["A.Pkg.1"]'),
        }
        fwd = build_forward_deps(pi, build_group_index(pi))
        assert get_transitive_deps("A.Pkg.1.var", fwd) == {"A.Pkg.1.var", "B.Pkg.1.var"}


class TestRemovableDeps:
    def test_exclusive_chain(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "B.var": pkg("B.var", size_bytes=200),
            "C.var": pkg("C.var", size_bytes=300),
        }
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "B.var": [edge("C.Lib.1", "C.var")], "C.var": []}
        r = compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd))
        assert r.removable_filenames == {"B.var", "C.var"}
        assert r.removable_size == 500

    def test_shared_not_removable(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "X.var": pkg("X.var", is_direct=1),
            "B.var": pkg("B.var", size_bytes=200),
        }
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "X.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        r = compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd))
        assert r.removable_filenames == set() and r.removable_size == 0

    def test_direct_not_removable(self):
        pi = {"A.var": pkg("A.var", is_direct=1), "B.var": pkg("B.var", is_direct=1, size_bytes=500)}
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        assert compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd)).removable_filenames == set()

    def test_no_deps_and_target_excluded(self):
        pi = {"A.var": pkg("A.var", is_direct=1)}
        r = compute_removable_deps("A.var", pi, {"A.var": []}, {})
        assert r.removable_filenames == set() and r.removable_size == 0
        pi = {"A.var": pkg("A.var", is_direct=1, size_bytes=999), "B.var": pkg("B.var", size_bytes=50)}
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        r = compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd))
        assert "A.var" not in r.removable_filenames and r.removable_size == 50

    def test_partial(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "X.var": pkg("X.var", is_direct=1),
            "B.var": pkg("B.var", size_bytes=100),
            "C.var": pkg("C.var", size_bytes=200),
            "D.var": pkg("D.var", size_bytes=300),
        }
        fwd = {
            "A.var": [edge("B.Lib.1", "B.var"), edge("D.Lib.1", "D.var")],
            "X.var": [edge("D.Lib.1", "D.var")],
            "B.var": [edge("C.Lib.1", "C.var")],
            "C.var": [],
            "D.var": [],
        }
        r = compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd))
        assert r.removable_filenames == {"B.var", "C.var"} and r.removable_size == 300

    def test_deep_chain(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "B.var": pkg("B.var", size_bytes=10),
            "C.var": pkg("C.var", size_bytes=20),
            "D.var": pkg("D.var", size_bytes=30),
            "E.var": pkg("E.var", size_bytes=40),
        }
        fwd = {
            "A.var": [edge("", "B.var")],
            "B.var": [edge("", "C.var")],
            "C.var": [edge("", "D.var")],
            "D.var": [edge("", "E.var")],
            "E.var": [],
        }
        r = compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd))
        assert r.removable_filenames == {"B.var", "C.var", "D.var", "E.var"} and r.removable_size == 100


class TestDemandRule1:
    def build(self):
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "X.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        return fwd, build_reverse_deps(fwd)

    def test_archived_does_not_pin_replaceable(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "X.var": pkg("X.var", is_direct=1, storage_state="archived"),
            "B.var": pkg("B.var", size_bytes=200),
        }
        fwd, rev = self.build()
        assert compute_removable_deps("A.var", pi, fwd, rev, {"B.var"}).removable_filenames == {"B.var"}

    def test_archived_pins_local_only(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "X.var": pkg("X.var", is_direct=1, storage_state="archived"),
            "B.var": pkg("B.var", size_bytes=200),
        }
        fwd, rev = self.build()
        assert compute_removable_deps("A.var", pi, fwd, rev, set()).removable_filenames == set()
        assert compute_removable_deps("A.var", pi, fwd, rev).removable_filenames == set()

    def test_never_sweeps_archived_dep(self):
        pi = {"A.var": pkg("A.var", is_direct=1), "B.var": pkg("B.var", storage_state="archived", size_bytes=200)}
        fwd = {"A.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        assert compute_removable_deps("A.var", pi, fwd, build_reverse_deps(fwd), {"B.var"}).removable_filenames == set()

    def test_orphan_cascade(self):
        pi = {"H.var": pkg("H.var", storage_state="archived")}
        assert "H.var" not in compute_orphan_cascade(pi, {"H.var": []}, {}, set()).orphans

        pi = {"X.var": pkg("X.var", is_direct=1, storage_state="archived"), "B.var": pkg("B.var")}
        fwd = {"X.var": [edge("B.Lib.1", "B.var")], "B.var": []}
        assert "B.var" in compute_orphan_cascade(pi, fwd, build_reverse_deps(fwd), {"B.var"}).orphans
        assert "B.var" not in compute_orphan_cascade(pi, fwd, build_reverse_deps(fwd), set()).orphans


class TestOrphanCascade:
    def test_direct_and_cascade(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "B.var": pkg("B.var", size_bytes=10),
            "C.var": pkg("C.var", size_bytes=20),
            "D.var": pkg("D.var", size_bytes=40),
        }
        fwd = {"A.var": [edge("", "B.var")], "B.var": [], "C.var": [edge("", "D.var")], "D.var": []}
        r = compute_orphan_cascade(pi, fwd, build_reverse_deps(fwd))
        assert r.direct_orphans == {"C.var"}
        assert r.orphans == {"C.var", "D.var"}
        assert r.total_size == 60


class TestCascades:
    def test_cascade_disable(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1),
            "X.var": pkg("X.var", is_direct=1),
            "B.var": pkg("B.var"),
            "C.var": pkg("C.var"),
            "D.var": pkg("D.var", storage_state="disabled"),
        }
        fwd = {
            "A.var": [edge("", "B.var"), edge("", "D.var")],
            "X.var": [edge("", "C.var")],
            "B.var": [edge("", "C.var")],
            "C.var": [],
            "D.var": [],
        }
        assert compute_cascade_disable("A.var", pi, fwd, build_reverse_deps(fwd)) == {"B.var"}

    def test_cascade_disable_fixpoint(self):
        pi = {"A.var": pkg("A.var", is_direct=1), "B.var": pkg("B.var"), "C.var": pkg("C.var")}
        fwd = {"A.var": [edge("", "B.var")], "B.var": [edge("", "C.var")], "C.var": []}
        assert compute_cascade_disable("A.var", pi, fwd, build_reverse_deps(fwd)) == {"B.var", "C.var"}

    def test_cascade_enable(self):
        pi = {
            "A.var": pkg("A.var", is_direct=1, storage_state="disabled"),
            "B.var": pkg("B.var", storage_state="disabled"),
            "C.var": pkg("C.var"),
            "D.var": pkg("D.var", storage_state="offloaded"),
        }
        fwd = {"A.var": [edge("", "B.var"), edge("", "C.var")], "B.var": [edge("", "D.var")], "C.var": [], "D.var": []}
        assert compute_cascade_enable("A.var", pi, fwd) == {"B.var", "D.var"}


def test_extract_refs_from_text():
    from vambackstage.core.graph import extract_refs_from_text

    data = (
        b'{"a":"Acid.Timeline.5:/Custom/x.cslist","b":"Acid.Timeline.5:/again","c":"SELF:/y",'
        b'"d":"Some.Pkg.Latest:/z","e":"Min.Pkg.MIN3:/w","f":"not.a.ref","g":"path/Creator.Pkg.2:/inside"}'
    )
    assert extract_refs_from_text(data) == ["Acid.Timeline.5", "Some.Pkg.latest", "Min.Pkg.min3"]
    odd = b'{"a":"Tobi19.GentianaFF15(Updated).1:/Custom/x.vap","b":"Some Body.Pkg-Name.2:/y","c":"https://hub.virtamate.com/x"}'
    assert extract_refs_from_text(odd) == ["Tobi19.GentianaFF15(Updated).1", "Some Body.Pkg-Name.2"]
