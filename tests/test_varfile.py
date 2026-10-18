import pytest

from vambackstage.core.varfile import (
    IntegrityError,
    canonical_var_filename,
    extract_file,
    extract_files,
    is_var_filename,
    parse_var_filename,
    qvaro_disabled_name,
    read_var,
    verify_package_full,
    verify_zip_file,
)

from .conftest import build_var


class TestParseVarFilename:
    def test_standard(self):
        r = parse_var_filename("AcidBubbles.Timeline.249.var")
        assert (r.creator, r.package_name, r.version) == ("AcidBubbles", "AcidBubbles.Timeline", "249")

    def test_strips_disabled(self):
        r = parse_var_filename("Author.Pkg.5.var.disabled")
        assert (r.creator, r.package_name, r.version) == ("Author", "Author.Pkg", "5")

    def test_disabled_case_insensitive(self):
        assert parse_var_filename("Author.Pkg.5.var.DISABLED").version == "5"
        assert parse_var_filename("Author.Pkg.5.var.Disabled").version == "5"
        assert parse_var_filename("Author.Pkg.5.VAR.disabled").version == "5"

    def test_multi_dot_disabled(self):
        r = parse_var_filename("A.B.C.42.var.disabled")
        assert (r.creator, r.package_name, r.version) == ("A", "A.B.C", "42")

    def test_rejects_latest(self):
        assert parse_var_filename("Author.Pkg.latest.var") is None
        assert parse_var_filename("Author.Pkg.LATEST.var") is None
        assert parse_var_filename("Author.Pkg.latest.var.disabled") is None

    def test_rejects_min(self):
        for n in ("Author.Pkg.min5.var", "X.Y.min0.var", "A.B.MIN10.var", "A.B.min7.var.disabled"):
            assert parse_var_filename(n) is None

    def test_rejects_two_segments(self):
        assert parse_var_filename("OnlyTwo.var") is None
        assert parse_var_filename("OnlyTwo.var.disabled") is None

    def test_multi_dot(self):
        r = parse_var_filename("A.B.C.123.var")
        assert (r.creator, r.package_name, r.version) == ("A", "A.B.C", "123")

    def test_rejects_non_numeric_versions(self):
        for n in ("Author.Pkg.beta.var", "Author.Pkg.alpha.var", "Author.Pkg.-1.var", "Author.Pkg.1a.var"):
            assert parse_var_filename(n) is None

    def test_qvaro_rename(self):
        r = parse_var_filename("Author.Pkg.5.DISABLED")
        assert (r.creator, r.package_name, r.version) == ("Author", "Author.Pkg", "5")
        r = parse_var_filename("A.B.C.42.disabled")
        assert (r.creator, r.package_name, r.version) == ("A", "A.B.C", "42")

    def test_rejects_qvaro_non_var(self):
        assert parse_var_filename("notes.DISABLED") is None
        assert parse_var_filename("Author.Pkg.latest.DISABLED") is None


class TestCanonicalVarFilename:
    def test_strips_disabled(self):
        assert canonical_var_filename("Foo.Bar.1.var.disabled") == "Foo.Bar.1.var"

    def test_case_insensitive(self):
        assert canonical_var_filename("Foo.Bar.1.var.DISABLED") == "Foo.Bar.1.var"
        assert canonical_var_filename("Foo.Bar.1.var.Disabled") == "Foo.Bar.1.var"

    def test_unchanged(self):
        assert canonical_var_filename("Foo.Bar.1.var") == "Foo.Bar.1.var"

    def test_qvaro(self):
        assert canonical_var_filename("Foo.Bar.1.DISABLED") == "Foo.Bar.1.var"
        assert canonical_var_filename("Foo.Bar.1.disabled") == "Foo.Bar.1.var"
        assert canonical_var_filename("Foo.Bar.1.Disabled") == "Foo.Bar.1.var"


def test_qvaro_disabled_name():
    assert qvaro_disabled_name("Foo.Bar.1.var") == "Foo.Bar.1.DISABLED"


class TestIsVarFilename:
    def test_bare_and_disabled(self):
        assert is_var_filename("Foo.Bar.1.var")
        assert is_var_filename("Foo.Bar.1.var.disabled")
        assert is_var_filename("Foo.Bar.1.var.DISABLED")

    def test_qvaro_only_when_real_var(self):
        assert is_var_filename("Foo.Bar.1.DISABLED")
        assert is_var_filename("A.B.C.42.disabled")
        assert not is_var_filename("notes.DISABLED")
        assert not is_var_filename("Author.Pkg.latest.DISABLED")

    def test_rejects_other_extensions(self):
        assert not is_var_filename("Foo.Bar.1.zip")
        assert not is_var_filename("readme.txt")


class TestReadVar:
    def test_standard_meta_and_files(self, vam):
        p = vam.addon_packages / "Author.Pkg.1.var"
        p.write_bytes(
            build_var(
                meta={"packageName": "Author.Pkg", "creator": "Author", "licenseType": "CC BY"},
                files={"Saves/scene/Demo.json": '{"atoms":[]}'},
            )
        )
        r = read_var(p)
        assert r.meta["packageName"] == "Author.Pkg"
        assert r.meta["licenseType"] == "CC BY"
        assert "Saves/scene/Demo.json" in [f.path for f in r.file_list]

    def test_lenient_trailing_comma(self, vam):
        p = vam.addon_packages / "Author.Pkg.2.var"
        p.write_bytes(build_var(meta_raw='{\n  "packageName": "Author.Pkg",\n  "licenseType": "FC",\n}\n'))
        r = read_var(p)
        assert r.meta == {"packageName": "Author.Pkg", "licenseType": "FC"}

    def test_lenient_comments_and_bom(self, vam):
        p = vam.addon_packages / "Author.Pkg.3.var"
        raw = '﻿{ // comment\n "packageName": "A.B", /* block */ "dependencies": { "X.Y.1": "", }, }'
        p.write_bytes(build_var(meta_raw=raw.encode("utf-8")))
        r = read_var(p)
        assert r.meta == {"packageName": "A.B", "dependencies": {"X.Y.1": ""}}

    def test_meta_none_when_absent(self, vam):
        p = vam.addon_packages / "NoMeta.1.var"
        p.write_bytes(build_var(files={"Saves/scene/Z.json": "{}"}))
        r = read_var(p)
        assert r.meta is None
        assert any(f.path == "Saves/scene/Z.json" for f in r.file_list)

    def test_meta_none_when_invalid(self, vam):
        p = vam.addon_packages / "BadMeta.1.var"
        p.write_bytes(build_var(meta_raw=b"\x00\xff\xfe not json {{{"))
        assert read_var(p).meta is None

    def test_extract_scene_jsons(self, vam):
        p = vam.addon_packages / "Scene.Pkg.1.var"
        p.write_bytes(
            build_var(
                meta={"packageName": "Scene.Pkg"},
                files={
                    "Saves/scene/S1.json": '{"atoms":[]}',
                    "Saves/Person/Appearance/Legacy.json": '{"atoms":[]}',
                    "Custom/Atom/Person/Morphs/x.vmi": "bin",
                },
            )
        )
        r = read_var(p, extract_scene_jsons=True)
        assert r.extracts["Saves/scene/S1.json"] == b'{"atoms":[]}'
        assert "Custom/Atom/Person/Morphs/x.vmi" not in r.extracts
        assert "Saves/Person/Appearance/Legacy.json" in r.extracts

    def test_extract_scene_jsons_skips_other_types(self, vam):
        p = vam.addon_packages / "P.1.var"
        p.write_bytes(
            build_var(
                meta={"packageName": "P"},
                files={
                    "Custom/Atom/Person/Appearance/Look.vap": "vap",
                    "Custom/SubScene/Room.json": "{}",
                },
            )
        )
        assert read_var(p, extract_scene_jsons=True).extracts == {}

    def test_empty_zip(self, vam):
        p = vam.addon_packages / "Empty.1.var"
        p.write_bytes(build_var())
        r = read_var(p)
        assert r.meta is None
        assert r.file_list == []

    def test_directories_skipped(self, vam):
        import io
        import zipfile

        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as zf:
            zf.writestr("Saves/scene/", "")
            zf.writestr("Saves/scene/A.json", "{}")
        p = vam.addon_packages / "Dirs.1.var"
        p.write_bytes(buf.getvalue())
        assert [f.path for f in read_var(p).file_list] == ["Saves/scene/A.json"]


class TestExtract:
    def test_extract_file_missing(self, vam):
        p = vam.addon_packages / "Q.1.var"
        p.write_bytes(build_var(meta={"packageName": "Q"}, files={"a.txt": "x"}))
        assert extract_file(p, "nope.txt") is None

    def test_extract_files_only_found(self, vam):
        p = vam.addon_packages / "R.1.var"
        p.write_bytes(build_var(files={"u.txt": "1", "v.txt": "2"}))
        m = extract_files(p, ["u.txt", "ghost.txt", "v.txt"])
        assert sorted(m) == ["u.txt", "v.txt"]
        assert m["u.txt"] == b"1"

    def test_extract_files_empty(self, vam):
        p = vam.addon_packages / "S.1.var"
        p.write_bytes(build_var(files={"a.txt": "z"}))
        assert extract_files(p, []) == {}

    def test_fixture_roundtrip(self, vam):
        p = vam.addon_packages / "X.Y.1.var"
        p.write_bytes(build_var(meta={"packageName": "X.Y"}, files={"a.txt": "hello"}))
        assert extract_file(p, "a.txt") == b"hello"


class TestIntegrity:
    def test_verify_zip_file(self, vam):
        good = vam.addon_packages / "G.1.var"
        good.write_bytes(build_var(files={"a.txt": "a"}))
        bad = vam.addon_packages / "B.1.var"
        bad.write_bytes(b"not a zip at all")
        assert verify_zip_file(good)
        assert not verify_zip_file(bad)
        assert not verify_zip_file(vam.addon_packages / "missing.var")

    def test_verify_package_full_detects_corruption(self, vam):
        data = bytearray(build_var(files={"a.txt": "hello world " * 50}))
        p = vam.addon_packages / "C.1.var"
        p.write_bytes(bytes(data))
        verify_package_full(p)
        data[40] ^= 0xFF
        data[41] ^= 0xFF
        p.write_bytes(bytes(data))
        with pytest.raises(IntegrityError):
            verify_package_full(p)
