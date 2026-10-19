from orchestrator.published import parse_sources_versions


def test_extracts_the_asterisk_stanza_version():
    text = (
        "Package: asterisk\nBinary: asterisk, asterisk-config\n"
        "Version: 2:22.5.2~dfsg+~cs6.15.60671435-1+zamfono13.1\n"
        "\n"
        "Package: zamfono-archive-keyring\nVersion: 1.0\n"
    )
    assert parse_sources_versions(text, "asterisk") == [
        "2:22.5.2~dfsg+~cs6.15.60671435-1+zamfono13.1"
    ]


def test_empty_index_yields_no_versions():
    assert parse_sources_versions("", "asterisk") == []


def test_other_packages_only_yields_no_versions():
    assert parse_sources_versions("Package: other\nVersion: 1.0\n", "asterisk") == []
