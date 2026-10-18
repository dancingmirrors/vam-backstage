from vambackstage.hub.client import hub_resource_page_url, is_hub_url, parse_hub_download_url, pick_hub_file

DL = "https://hub.virtamate.com/resources"


def test_is_hub_url_accepts_only_the_hub_over_http():
    assert is_hub_url(hub_resource_page_url(66186))
    assert is_hub_url("http://HUB.virtamate.com/threads/12/")
    assert is_hub_url("HTTPS://hub.virtamate.com:443/resources/1")
    assert not is_hub_url("https://hub.virtamate.com.evil.example/resources/1")
    assert not is_hub_url("https://evil.example\\@hub.virtamate.com/resources/1")
    assert not is_hub_url("https://user@hub.virtamate.com/resources/1")
    assert not is_hub_url("https://hub.virtamate.com:8443/resources/1")
    assert not is_hub_url("https://hub.virtamate.com:abc/resources/1")
    assert not is_hub_url("https://[::1]/resources/1")
    assert not is_hub_url("https://example.com/resources/1")
    assert not is_hub_url("mailto:someone@hub.virtamate.com")
    assert not is_hub_url("")
    assert not is_hub_url(None)
    assert not is_hub_url("http://[not-a-host/")


def test_parse_hub_download_url_reads_resource_file_and_version_ids():
    assert parse_hub_download_url(f"{DL}/66186/download?file=584887") == ("66186", "584887", None)
    assert parse_hub_download_url(f"{DL}/some-look.1125/version/21060/download?file=82325") == (
        "1125",
        "82325",
        "21060",
    )
    assert parse_hub_download_url(f"{DL}/some-look.1125/version/21060/download") == ("1125", None, "21060")
    assert parse_hub_download_url(f"{DL}/v1.2-thing.42/download/?x=1&file=7") == ("42", "7", None)
    assert parse_hub_download_url(f"{DL}/211/download") == ("211", None, None)
    assert parse_hub_download_url(f"{DL}/211/download?file=") == ("211", None, None)
    assert parse_hub_download_url(f"{DL}/211/download?file=latest") == ("211", None, None)
    assert parse_hub_download_url(f"{DL}/211/download?file=5&file=6") == ("211", "6", None)
    assert parse_hub_download_url(f"{DL}/211/download?file=007") == ("211", "7", None)


def test_parse_hub_download_url_ignores_other_links():
    assert parse_hub_download_url(f"{DL}/211") is None
    assert parse_hub_download_url(f"{DL}/211/updates-panel") is None
    assert parse_hub_download_url(f"{DL}/211/download/extra") is None
    assert parse_hub_download_url(f"{DL}/211/Download") is None
    assert parse_hub_download_url(f"{DL}/x.\u0663/download") is None
    assert parse_hub_download_url("https://cdn.example/resources/1/download?file=2") is None
    assert parse_hub_download_url("https://hub.virtamate.com/threads/5/download") is None
    assert parse_hub_download_url(None) is None


def _detail(*files, version="90612"):
    return {"resource_id": "9", "current_version_id": version, "hubFiles": list(files)}


def _file(name, file_id, version="90612"):
    return {
        "filename": name,
        "attachment_id": str(file_id),
        "current_version_id": version,
        "urlHosted": f"{DL}/9/download?file={file_id}",
    }


def test_pick_hub_file_matches_the_linked_file():
    detail = _detail(_file("A.Look.1.var", 10), _file("A.Look.2.var", 20))
    assert pick_hub_file(detail, "20")["filename"] == "A.Look.2.var"
    assert pick_hub_file(detail, "020")["filename"] == "A.Look.2.var"
    assert pick_hub_file(detail, "2") is None
    assert pick_hub_file(detail, "999") is None
    assert pick_hub_file(detail, None) is None
    assert pick_hub_file(detail, "1.0") is None
    assert pick_hub_file(detail, "(") is None


def test_pick_hub_file_takes_the_only_file_when_the_link_names_none():
    detail = _detail({"filename": "null"}, _file("A.Look.1.var", 10), "junk")
    assert pick_hub_file(detail, None)["filename"] == "A.Look.1.var"
    assert pick_hub_file(detail, "10")["filename"] == "A.Look.1.var"
    assert pick_hub_file(None, None) is None
    assert pick_hub_file({"hubFiles": None}, None) is None
    assert pick_hub_file({"hubFiles": "nope"}, None) is None
    assert pick_hub_file(["not", "a", "dict"], None) is None


def test_pick_hub_file_only_serves_the_current_version():
    detail = _detail(_file("A.Look.3.var", 30, version="500"), version="500")
    assert pick_hub_file(detail, None, "500")["filename"] == "A.Look.3.var"
    assert pick_hub_file(detail, "30", "500")["filename"] == "A.Look.3.var"
    assert pick_hub_file(detail, None, "499") is None
    assert pick_hub_file(detail, "30", "499") is None
    bare = {"hubFiles": [{"filename": "B.Scene.1.var", "attachment_id": "1"}]}
    assert pick_hub_file(bare, None, "1") is None


def test_pick_hub_file_reads_the_file_id_from_either_url_or_attachment_id():
    by_url = _detail({"filename": "B.Scene.3.var", "downloadUrl": f"{DL}/9/version/1/download?file=5#frag"})
    assert pick_hub_file(by_url, "5")["filename"] == "B.Scene.3.var"
    by_attachment = _detail({"filename": "C.Pose.2.var", "attachment_id": "77"})
    assert pick_hub_file(by_attachment, "77")["filename"] == "C.Pose.2.var"
