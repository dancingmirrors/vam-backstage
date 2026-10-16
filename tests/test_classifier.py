from vambackstage.core.classifier import ContentItem, FileEntry, classify_contents, derive_package_type


def f(path, size=0):
    return FileEntry(path, size)


def types(items):
    return [i.type for i in items]


class TestClassifyContents:
    def test_scene(self):
        items = classify_contents([f("Saves/scene/MyScene.json")])
        assert items == [ContentItem("Saves/scene/MyScene.json", "MyScene", "scene", None)]

    def test_legacy_scene(self):
        assert types(classify_contents([f("Saves/scene/Old.vac")])) == ["legacyScene"]

    def test_subscene(self):
        assert types(classify_contents([f("Custom/SubScene/Room.json")])) == ["subscene"]
        assert types(classify_contents([f("Custom/SubScenes/Room.json")])) == ["subscene"]

    def test_look(self):
        items = classify_contents([f("Custom/Atom/Person/Appearance/Cool_Look.vap")])
        assert items[0].type == "look"
        assert items[0].display_name == "Cool Look"

    def test_legacy_look(self):
        assert types(classify_contents([f("Saves/Person/Appearance/OldLook.json")])) == ["legacyLook"]

    def test_skin_preset(self):
        assert types(classify_contents([f("Custom/Atom/Person/Skin/Fair.vap")])) == ["skinPreset"]

    def test_poses(self):
        assert types(classify_contents([f("Custom/Atom/Person/Pose/Stand.vap")])) == ["pose"]
        assert types(classify_contents([f("Saves/Person/Pose/Sit.json")])) == ["legacyPose"]

    def test_clothing(self):
        assert types(classify_contents([f("Custom/Clothing/Author/Dress.vab")])) == ["clothingItem"]
        assert types(classify_contents([f("Custom/Atom/Person/Clothing/Outfit.vap")])) == ["clothingPreset"]

    def test_hair(self):
        assert types(classify_contents([f("Custom/Hair/Author/Bob.vam")])) == ["hairItem"]
        assert types(classify_contents([f("Custom/Atom/Person/Hair/Curly.vap")])) == ["hairPreset"]

    def test_hidden_types(self):
        assert types(classify_contents([f("Custom/Atom/Person/Morphs/face.vmi")])) == ["morphBinary"]
        assert types(classify_contents([f("Custom/Atom/Person/Morph/face.vmi")])) == ["morphBinary"]
        assert types(classify_contents([f("Custom/Scripts/Author/Plugin.cs")])) == ["pluginScript"]
        assert types(classify_contents([f("Custom/Scripts/Author/Plugin.cslist")])) == ["scriptList"]
        assert types(classify_contents([f("Custom/Atom/Person/Plugins/MyPlugin.vap")])) == ["pluginPreset"]
        assert types(classify_contents([f("Custom/Assets/pack.assetbundle")])) == ["assetbundle"]
        assert types(classify_contents([f("Custom/Sounds/fx.assetbundle")])) == ["assetbundle"]
        assert types(classify_contents([f("Custom/Atom/Person/Textures/skin.png")])) == ["texture"]
        assert types(classify_contents([f("Custom/Sounds/fx.wav")])) == ["audio"]
        assert types(classify_contents([f("Custom/Sound/fx.wav")])) == ["audio"]
        assert types(classify_contents([f("Custom/Audio/fx.mp3")])) == ["audio"]

    def test_atom_preset(self):
        assert types(classify_contents([f("Custom/Empty/Base.vap")])) == ["atomPreset"]
        assert types(classify_contents([f("Custom/Image/Picture.vap")])) == ["atomPreset"]

    def test_atom_preset_excluded_folders(self):
        assert classify_contents([f("Custom/Scripts/Thing.vap")]) == []
        assert classify_contents([f("Custom/Assets/Thing.vap")]) == []
        assert classify_contents([f("Custom/PluginData/Thing.vap")]) == []

    def test_atom_preset_only_one_level(self):
        assert classify_contents([f("Custom/Empty/Sub/Deep.vap")]) == []

    def test_ignores_unmatched(self):
        assert classify_contents([f("README.txt"), f("meta.json"), f("Custom/Unknown/foo.xyz")]) == []
        assert classify_contents([f("Saves/scene/MyScene.vap")]) == []
        assert classify_contents([]) == []

    def test_display_names(self):
        assert classify_contents([f("Saves/scene/My_Cool_Scene.json")])[0].display_name == "My Cool Scene"
        assert classify_contents([f("Custom/Atom/Person/Pose/Preset_EmbodyPose.vap")])[0].display_name == "EmbodyPose"
        assert classify_contents([f("Custom/Atom/Person/Clothing/preset_Outfit.vap")])[0].display_name == "Outfit"
        assert classify_contents([f("Saves/scene/Preset.json")])[0].display_name == "Preset"

    def test_thumbnails(self):
        assert (
            classify_contents([f("Saves/scene/S.json"), f("Saves/scene/S.jpg")])[0].thumbnail_path
            == "Saves/scene/S.jpg"
        )
        assert (
            classify_contents([f("Saves/scene/S.json"), f("Saves/scene/S.jpeg")])[0].thumbnail_path
            == "Saves/scene/S.jpeg"
        )
        assert (
            classify_contents([f("Saves/scene/S.json"), f("Saves/scene/S.png")])[0].thumbnail_path
            == "Saves/scene/S.png"
        )
        assert (
            classify_contents([f("Saves/scene/S.json"), f("Saves/scene/S.jpg"), f("Saves/scene/S.png")])[
                0
            ].thumbnail_path
            == "Saves/scene/S.jpg"
        )
        assert classify_contents([f("Saves/scene/S.json")])[0].thumbnail_path is None

    def test_case_insensitive_paths(self):
        assert types(classify_contents([f("SAVES/SCENE/S.json")])) == ["scene"]
        assert types(classify_contents([f("custom/clothing/A/D.vab")])) == ["clothingItem"]

    def test_same_type_dedup(self):
        items = classify_contents(
            [f("Custom/Clothing/A/Dress.vab"), f("Custom/Clothing/A/Dress.vaj"), f("Custom/Clothing/A/Dress.vam")]
        )
        assert [i.internal_path for i in items] == ["Custom/Clothing/A/Dress.vam"]
        items = classify_contents(
            [
                f("Custom/Atom/Person/Morphs/M.vmb"),
                f("Custom/Atom/Person/Morphs/M.vmi"),
                f("Custom/Atom/Person/Morphs/M.dsf"),
            ]
        )
        assert [i.internal_path for i in items] == ["Custom/Atom/Person/Morphs/M.vmi"]
        assert len(classify_contents([f("Custom/Clothing/A/Shirt.vab"), f("Custom/Clothing/A/Pants.vab")])) == 2
        assert len(classify_contents([f("Saves/scene/A.json"), f("Saves/scene/B.json")])) == 2

    def test_cross_type_dedup(self):
        items = classify_contents([f("Custom/Clothing/A/Dress.vab"), f("Custom/Atom/Person/Clothing/Dress.vap")])
        assert types(items) == ["clothingPreset"]

        items = classify_contents(
            [
                f("Custom/Hair/A/Style.vab"),
                f("Custom/Hair/A/Style.jpg"),
                f("Custom/Atom/Person/Hair/Style.vap"),
                f("Custom/Atom/Person/Hair/Style.jpg"),
            ]
        )
        assert types(items) == ["hairPreset"]
        assert items[0].thumbnail_path == "Custom/Atom/Person/Hair/Style.jpg"

        items = classify_contents(
            [
                f("Custom/Clothing/A/Dress.vab"),
                f("Custom/Clothing/A/Dress.jpg"),
                f("Custom/Atom/Person/Clothing/Dress.vap"),
            ]
        )
        assert types(items) == ["clothingItem"]
        assert items[0].thumbnail_path == "Custom/Clothing/A/Dress.jpg"

        items = classify_contents(
            [
                f("Custom/Clothing/A/Dress.vab"),
                f("Custom/Atom/Person/Clothing/Dress.vap"),
                f("Custom/Atom/Person/Clothing/Dress.jpg"),
            ]
        )
        assert types(items) == ["clothingPreset"]
        assert items[0].thumbnail_path == "Custom/Atom/Person/Clothing/Dress.jpg"

        items = classify_contents(
            [f("Custom/Hair/A/Bob.vam"), f("Custom/Hair/A/Bob.jpg"), f("Custom/Atom/Person/Hair/Bob.vap")]
        )
        assert types(items) == ["hairItem"]
        assert items[0].thumbnail_path == "Custom/Hair/A/Bob.jpg"

        assert (
            len(classify_contents([f("Custom/Clothing/A/Shirt.vab"), f("Custom/Atom/Person/Clothing/Pants.vap")])) == 2
        )

    def test_disabled_loose_preset(self):
        items = classify_contents([f("Custom/Atom/Person/Appearance/extracted/Preset_A - Look.vap.disabled")])
        assert len(items) == 1
        assert items[0].type == "look"
        assert items[0].internal_path == "Custom/Atom/Person/Appearance/extracted/Preset_A - Look.vap.disabled"
        assert items[0].display_name == "A - Look"

        items = classify_contents(
            [
                f("Custom/Atom/Person/Appearance/extracted/Look.vap.disabled"),
                f("Custom/Atom/Person/Appearance/extracted/Look.jpg"),
            ]
        )
        assert items[0].thumbnail_path == "Custom/Atom/Person/Appearance/extracted/Look.jpg"
        assert classify_contents([f("Custom/Unknown/foo.xyz.disabled")]) == []


class TestDerivePackageType:
    def mk(self, *types_):
        return [ContentItem("p", "n", t, None) for t in types_]

    def test_cases(self):
        assert derive_package_type([]) is None
        assert derive_package_type(self.mk("scene")) == "Scenes"
        assert derive_package_type(self.mk("pluginScript", "morphBinary")) is None
        assert derive_package_type(self.mk("look", "scene")) == "Scenes"
        assert derive_package_type(self.mk("hairItem", "clothingPreset")) == "Clothing"
        assert derive_package_type(self.mk("morphBinary", "pose")) == "Poses"
        assert derive_package_type(self.mk("legacyScene")) == "Scenes"
        assert derive_package_type(self.mk("legacyLook")) == "Looks"
        assert derive_package_type(self.mk("legacyPose")) == "Poses"
