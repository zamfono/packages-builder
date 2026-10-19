"""Queries the Debian sid source index using apt-cache/apt-get themselves
(spec: "using Debian APT tools"), scoped to a throwaway sources.list so the
host's normal (trixie) apt configuration is never touched or mixed with
sid. Callers write SID_SOURCELIST_CONTENT to that sourcelist path
themselves (Path(path).write_text(SID_SOURCELIST_CONTENT)) — one stdlib
call needs no wrapper function here. That path must end in `.list`: apt
picks its parser from the file extension, and `.sources` selects the
deb822 format this one-line content is not."""
import email.parser
from pathlib import Path

SID_SOURCELIST_CONTENT = "deb-src http://deb.debian.org/debian sid main\n"


def prepare_apt_state(state_dir: str) -> None:
    """Creates the redirected apt state directories `apt-get update`
    refuses to create for itself."""
    for subdirectory in ("lists/partial", "cache/archives/partial"):
        Path(state_dir, subdirectory).mkdir(parents=True, exist_ok=True)


def scoped_apt_opts(sourcelist_path: str, state_dir: str) -> "list[str]":
    """The sid-scoped option pair, plus apt's list and cache directories
    redirected under state_dir, so the scoped lists and downloads never
    mix into the host's own apt state in /var/lib/apt and /var/cache/apt."""
    return [
        "-o", f"Dir::Etc::sourcelist={sourcelist_path}",
        "-o", "Dir::Etc::sourceparts=/dev/null",
        "-o", f"Dir::State::Lists={state_dir}/lists",
        "-o", f"Dir::Cache={state_dir}/cache",
    ]


def parse_showsrc_version(output: str) -> str:
    # showsrc output is an RFC822-style stanza; the stdlib header parser
    # handles field folding, so no line scanning is needed.
    version = email.parser.Parser().parsestr(output, headersonly=True)["Version"]
    if version is None:
        raise LookupError("no Version: field in apt-cache showsrc output")
    return version.strip()
