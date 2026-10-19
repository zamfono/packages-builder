"""Reads the published versions from an archive's own
indexes (each caller names the archive and package): the source version from Sources, an architecture's binary version
from that architecture's Packages, newest first. The archive is the single source of
truth for "what is published", so the builder keeps no local state and
self-heals when the archive is restored from backup or rebuilt. A run
returns only once the archive lists its upload (cli._wait_until_published),
so the next run never reads an index that predates it."""
import email.parser
import gzip
import urllib.error
import urllib.request

from orchestrator.versioncheck import newest_first



def parse_sources_versions(text: str, package: str) -> "list[str]":
    """Works on Packages indexes too: both are Package/Version stanzas."""
    versions = []
    for stanza in text.split("\n\n"):
        if not stanza.strip():
            continue
        parsed = email.parser.Parser().parsestr(stanza, headersonly=True)
        if parsed["Package"] == package and parsed["Version"] is not None:
            versions.append(parsed["Version"].strip())
    return versions


def _index_versions(index_path: str, base_url: str, package: str) -> "list[str]":
    try:
        with urllib.request.urlopen(base_url + index_path, timeout=60) as response:
            data = response.read()
    except urllib.error.HTTPError as error:
        if error.code == 404:
            # a fresh archive (or a newly added architecture) has no index yet
            return []
        raise
    return newest_first(parse_sources_versions(gzip.decompress(data).decode(), package))


def published_source_versions(base_url: str, package: str) -> "list[str]":
    return _index_versions("source/Sources.gz", base_url, package)


def published_binary_versions(arch: str, base_url: str, package: str) -> "list[str]":
    return _index_versions(f"binary-{arch}/Packages.gz", base_url, package)
