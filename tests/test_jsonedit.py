import codecs
import json
import os
import stat

import pytest

from vambackstage.core.jsonedit import (
    KIND_BINARY,
    KIND_JSON,
    KIND_TEXT,
    JsonDocument,
    delete_at,
    dumps_vam,
    file_kind,
    file_kind_of,
    find_matches,
    format_path,
    get_at,
    index_in_parent,
    insert_at,
    iter_nodes,
    list_package_files,
    load_json_document,
    rename_key,
    replace_in_package,
    replace_text,
    save_json_document,
    set_at,
)
from vambackstage.core.lenient_json import loads_lenient

VAM_META = "\n".join(
    [
        "{ ",
        '   "licenseType" : "CC BY", ',
        '   "creatorName" : "MeshedVR", ',
        '   "packageName" : "3PointLightSetup", ',
        '   "standardReferenceVersionOption" : "Latest", ',
        '   "scriptReferenceVersionOption" : "Exact", ',
        '   "description" : "A simple 3 point light setup to help get started. Includes SubScene save and Template Scene example.", ',
        '   "credits" : "", ',
        '   "instructions" : "", ',
        '   "promotionalLink" : "", ',
        '   "programVersion" : "1.20.0.3", ',
        '   "contentList" : [ ',
        '      "Saves/scene/MeshedVR/3PointLightSetup", ',
        '      "Custom/SubScene/MeshedVR/3PointLightSetup"',
        "   ], ",
        '   "dependencies" : { ',
        '      "NoStage3.Hair_Long_Upswept_Top_Bun.latest" : { ',
        '         "licenseType" : "CC BY", ',
        '         "dependencies" : { ',
        "         }",
        "      }",
        "   }, ",
        '   "customOptions" : { ',
        '      "preloadMorphs" : "false"',
        "   }",
        "}",
    ]
)

VAM_SCENE_PART = "\n".join(
    [
        "[ ",
        "   { ",
        '      "id" : "rThumb2", ',
        '      "position" : { ',
        '         "x" : "0.03988018", ',
        '         "y" : "-9.506475E-05", ',
        '         "z" : "3.950484E-05"',
        "      }",
        "   }, ",
        "   { ",
        '      "id" : "rThumb3"',
        "   }",
        "]",
    ]
)


class TestDumpsVam:
    def test_reproduces_vam_layout(self):
        assert dumps_vam(loads_lenient(VAM_META)) == VAM_META
        assert dumps_vam(loads_lenient(VAM_SCENE_PART)) == VAM_SCENE_PART

    def test_numbers_stay_strings(self):
        data = loads_lenient(VAM_SCENE_PART)
        assert data[0]["position"]["y"] == "-9.506475E-05"
        assert '"y" : "-9.506475E-05"' in dumps_vam(data)

    def test_empty_containers_and_scalars(self):
        assert dumps_vam({}) == "{ \n}"
        assert dumps_vam([]) == "[ \n]"
        assert dumps_vam({"a": [], "b": {}}) == '{ \n   "a" : [ \n   ], \n   "b" : { \n   }\n}'
        assert dumps_vam("x") == '"x"'
        assert dumps_vam(None) == "null"

    def test_escapes_and_native_types(self):
        data = {"q": 'a"b\\c\n\t\x01', "u": "é→", "n": 5, "f": 1.5, "t": True, "x": None, "neg": -3}
        text = dumps_vam(data)
        assert json.loads(text) == data
        assert '"q" : "a\\"b\\\\c\\n\\t\\u0001"' in text
        assert '"u" : "é→"' in text
        assert '"n" : 5' in text and '"f" : 1.5' in text and '"t" : true' in text and '"x" : null' in text

    def test_unknown_type(self):
        with pytest.raises(TypeError):
            dumps_vam({"s": {1, 2}})


class TestDocuments:
    def test_roundtrip_keeps_bom_and_mode(self, tmp_path):
        p = tmp_path / "meta.json"
        p.write_bytes(codecs.BOM_UTF8 + VAM_META.encode("utf-8"))
        os.chmod(p, 0o640)
        doc = load_json_document(p)
        assert doc.bom and doc.data["packageName"] == "3PointLightSetup"
        doc.data["description"] = "changed"
        n = save_json_document(doc)
        raw = p.read_bytes()
        assert n == len(raw) and raw.startswith(codecs.BOM_UTF8)
        assert raw == codecs.BOM_UTF8 + VAM_META.replace(
            "A simple 3 point light setup to help get started. Includes SubScene save and Template Scene example.",
            "changed",
        ).encode("utf-8")
        assert stat.S_IMODE(p.stat().st_mode) == 0o640
        assert not [n for n in os.listdir(tmp_path) if n.startswith(".")]

    def test_lenient_load_and_plain_save(self, tmp_path):
        p = tmp_path / "scene.json"
        p.write_text('{"atoms": [ {"id": "a",}, ], // trailing commas and a comment\n}')
        doc = load_json_document(p)
        assert not doc.bom and doc.data == {"atoms": [{"id": "a"}]}
        save_json_document(doc)
        assert p.read_text() == '{ \n   "atoms" : [ \n      { \n         "id" : "a"\n      }\n   ]\n}'

    def test_not_json(self, tmp_path):
        p = tmp_path / "x.json"
        p.write_bytes(b"\x00\x01 nope {{{")
        with pytest.raises(ValueError, match="x.json is not JSON"):
            load_json_document(p)

    def test_save_new_file(self, tmp_path):
        p = tmp_path / "new.vap"
        save_json_document(JsonDocument(str(p), {"a": "1"}))
        assert p.read_text() == '{ \n   "a" : "1"\n}'


class TestTreeEdits:
    def data(self):
        return {"a": {"b": ["x", {"c": "y"}]}, "k": "v"}

    def test_get_set_delete(self):
        d = self.data()
        assert get_at(d, ("a", "b", 1, "c")) == "y"
        assert get_at(d, ()) is d
        assert set_at(d, ("a", "b", 0), "z") is d and d["a"]["b"][0] == "z"
        assert set_at(d, (), {"new": "root"}) == {"new": "root"}
        assert delete_at(d, ("a", "b", 1)) == {"c": "y"} and d["a"]["b"] == ["z"]
        assert delete_at(d, ("k",)) == "v" and "k" not in d
        with pytest.raises(KeyError):
            delete_at(d, ("nope",))
        with pytest.raises(IndexError):
            delete_at(d, ("a", "b", 5))
        with pytest.raises(ValueError):
            delete_at(d, ())
        with pytest.raises(TypeError):
            set_at(d, ("a", "b", 0, "x"), 1)

    def test_insert(self):
        d = self.data()
        assert insert_at(d, ("a",), "z", "1") == "z" and list(d["a"]) == ["b", "z"]
        assert insert_at(d, (), "first", "0", position=0) == "first" and list(d) == ["first", "a", "k"]
        assert insert_at(d, ("a", "b"), None, "end") == 2 and d["a"]["b"][2] == "end"
        assert insert_at(d, ("a", "b"), None, "front", position=0) == 0 and d["a"]["b"][0] == "front"
        with pytest.raises(KeyError):
            insert_at(d, ("a",), "z", "dup")
        with pytest.raises(ValueError):
            insert_at(d, ("a",), "", "no key")
        with pytest.raises(TypeError):
            insert_at(d, ("k",), "x", "scalar parent")

    def test_rename_keeps_order(self):
        d = {"one": "1", "two": "2", "three": "3"}
        rename_key(d, ("two",), "deux")
        assert list(d.items()) == [("one", "1"), ("deux", "2"), ("three", "3")]
        rename_key(d, ("deux",), "deux")
        with pytest.raises(KeyError):
            rename_key(d, ("one",), "three")
        with pytest.raises(ValueError):
            rename_key(d, ("one",), "")
        with pytest.raises(TypeError):
            rename_key({"l": ["a"]}, ("l", 0), "x")

    def test_index_and_format(self):
        d = self.data()
        assert index_in_parent(d, ("k",)) == 1 and index_in_parent(d, ("a", "b", 1)) == 1
        assert format_path(()) == "(root)"
        assert format_path(("a", "b", 1, "c")) == "a.b[1].c"
        assert format_path(("Custom/Scripts", "x y")) == '["Custom/Scripts"]["x y"]'

    def test_iter_nodes_order(self):
        paths = [p for p, _v in iter_nodes(self.data())]
        assert paths == [(), ("a",), ("a", "b"), ("a", "b", 0), ("a", "b", 1), ("a", "b", 1, "c"), ("k",)]


class TestFindReplace:
    def test_find(self):
        d = {"dependencies": {"Old.Dep.3": {"licenseType": "CC BY"}}, "scene": ["Old.Dep.3:/Custom/x.vap", "none"]}
        assert find_matches(d, "old.dep") == [("dependencies", "Old.Dep.3"), ("scene", 0)]
        assert find_matches(d, "Old.Dep", case_sensitive=True) == [("dependencies", "Old.Dep.3"), ("scene", 0)]
        assert find_matches(d, "old.dep", case_sensitive=True) == []
        assert find_matches(d, "old.dep", in_keys=False) == [("scene", 0)]
        assert find_matches(d, "old.dep", in_values=False) == [("dependencies", "Old.Dep.3")]
        assert find_matches(d, "") == []

    def test_replace_values_and_keys(self):
        d = {
            "dependencies": {"Old.Dep.3": {"dependencies": {}}, "Keep.Me.1": {}},
            "atoms": [{"id": "Old.Dep.3:/a", "x": "Old.Dep.3 Old.Dep.3"}, "Old.Dep.3"],
            "tail": "z",
        }
        root, n = replace_text(d, "Old.Dep.3", "Old.Dep.latest")
        assert root is d and n == 5
        assert list(d["dependencies"]) == ["Old.Dep.latest", "Keep.Me.1"]
        assert d["atoms"] == [{"id": "Old.Dep.latest:/a", "x": "Old.Dep.latest Old.Dep.latest"}, "Old.Dep.latest"]
        assert replace_text(d, "Keep.Me", "X", in_keys=False)[1] == 0
        assert replace_text("a-b-a", "a", "c") == ("c-b-c", 2)
        with pytest.raises(ValueError):
            replace_text(d, "", "x")

    def test_replace_in_package(self, tmp_path):
        folder = tmp_path / "Auth.Pkg.1.var"
        (folder / "Saves" / "scene").mkdir(parents=True)
        (folder / "Custom").mkdir()
        meta = folder / "meta.json"
        meta.write_text('{ \n   "dependencies" : { \n      "Old.Dep.3" : { \n      }\n   }\n}')
        scene = folder / "Saves" / "scene" / "S.json"
        scene.write_text('{"atoms": [{"id": "Old.Dep.3:/Custom/x.vap"}, {"id": "fine"}]}')
        untouched = folder / "Saves" / "scene" / "T.json"
        untouched.write_text('{"a": "nothing here",}')
        binary = folder / "Custom" / "m.vmb"
        binary.write_bytes(b"Old.Dep.3\x00binary")
        broken = folder / "Custom" / "broken.vap"
        broken.write_text("{{{ not json")
        res = replace_in_package(folder, "Old.Dep.3", "Old.Dep.latest")
        assert res.files == {"meta.json": 1, "Saves/scene/S.json": 1} and res.total == 2
        assert set(res.errors) == {"Custom/broken.vap"}
        assert meta.read_text() == '{ \n   "dependencies" : { \n      "Old.Dep.latest" : { \n      }\n   }\n}'
        assert json.loads(scene.read_text()) == {"atoms": [{"id": "Old.Dep.latest:/Custom/x.vap"}, {"id": "fine"}]}
        assert untouched.read_text() == '{"a": "nothing here",}'
        assert binary.read_bytes() == b"Old.Dep.3\x00binary"
        with pytest.raises(ValueError):
            replace_in_package(folder, "", "x")


class TestFileKinds:
    def test_by_suffix(self):
        assert file_kind("meta.json") == KIND_JSON
        assert file_kind("Custom/Atom/Person/Appearance/Preset_X.vap") == KIND_JSON
        assert file_kind("Custom/Hair/Female/A/B.vam") == KIND_JSON
        assert file_kind("Custom/Scripts/A/B.cs") == KIND_TEXT
        assert file_kind("Custom/Scripts/A/B.cslist") == KIND_TEXT
        assert file_kind("Custom/Assets/x.assetbundle") == KIND_BINARY
        assert file_kind("Custom/Atom/Person/Morphs/female/x.vmb") == KIND_BINARY
        assert file_kind("thumb.jpg") == KIND_BINARY

    def test_sniffing_unknown_suffix(self):
        assert file_kind("x.unknown") == KIND_BINARY
        assert file_kind("x.unknown", b"\xef\xbb\xbf  {\n") == KIND_JSON
        assert file_kind("x.unknown", b"[1, 2]") == KIND_JSON
        assert file_kind("x.unknown", b"hello world\n") == KIND_TEXT
        assert file_kind("x.unknown", b"\x89PNG\r\n\x1a\n\x00") == KIND_BINARY
        assert file_kind("x.unknown", b"\xff\xfe\x00\x00 bad utf-8") == KIND_BINARY
        assert file_kind("x.unknown", "text cut mid-char é".encode()[:-1]) == KIND_TEXT

    def test_on_disk(self, tmp_path):
        (tmp_path / "a.json").write_text("{}")
        (tmp_path / "b.data").write_text("[]")
        (tmp_path / "c.data").write_bytes(b"\x00\x01")
        (tmp_path / "d.txt").write_text("hi")
        assert file_kind_of(str(tmp_path / "a.json")) == KIND_JSON
        assert file_kind_of(str(tmp_path / "b.data")) == KIND_JSON
        assert file_kind_of(str(tmp_path / "c.data")) == KIND_BINARY
        assert file_kind_of(str(tmp_path / "d.txt")) == KIND_TEXT
        assert file_kind_of(str(tmp_path / "missing.data")) == KIND_BINARY
        files = list_package_files(tmp_path)
        assert [(f.path, f.kind) for f in files] == [
            ("a.json", KIND_JSON),
            ("b.data", KIND_JSON),
            ("c.data", KIND_BINARY),
            ("d.txt", KIND_TEXT),
        ]
        assert files[0].size == 2 and files[0].mtime > 0
