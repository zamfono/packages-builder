"""Applies Zamfono's own packaging changes to a dpkg-source -x extracted
Debian source tree, and stamps the computed Zamfono version into
debian/changelog before the build ever starts — dpkg-buildpackage reads the
package version solely from the top changelog entry, so skipping this step
leaves every built artifact carrying the raw sid version instead of
+zamfono13.N. This module only edits the source tree and builds the dch
command line; orchestrator.cli._fetch_and_patch runs both, in this order, on
the host before the source tree is bind-mounted into the container."""
import hashlib
import shutil
import subprocess
from pathlib import Path

# Debian patches replaced with a fixed version from packaging/debian-patches/:
# name -> (SHA-256 of Debian's file as it was reviewed, the reason recorded in
# debian/changelog). A changed or vanished Debian patch stops the build for a
# human to decide again: when Debian ships the fix, that is the signal to
# drop our copy.
REPLACED_DEBIAN_PATCHES = {
    "2016_opus_plc.patch": (
        "63d984e124dcb6aa3265b4cd2b2d48266f50c036208c8f049319a483f4118b9f",
        "Debian's version silently drops every frame on a transcoded call leg"
        " after the far end changes SSRC to a slightly lower RTP sequence"
        " number, often for the rest of the call (asterisk/asterisk#1324). The"
        " fixed version only acts on translation paths starting with a"
        " native-PLC decoder (Opus, Speex, iLBC) and treats a sequence number"
        " jump of more than 50 packets as a new stream (Debian #1151451).",
    ),
}


def replace_debian_patches(
    source_dir: "str | Path",
    replacements_dir: "str | Path",
    replaced: "dict[str, tuple[str, str]]" = REPLACED_DEBIAN_PATCHES,
) -> None:
    """Swaps each replaced patch in a dpkg-source -x extracted tree, which
    already has every series patch applied: Debian's is reversed, ours
    applied with zero fuzz, and ours overwrites Debian's file, so the series
    line stays and `apt source` shows the patch that was built. .pc/ keeps
    the pristine copies of the files Debian's patch touched; a replacement
    must touch the same files, or dpkg-source -b sees unrecorded changes."""
    source_dir = Path(source_dir)
    # patch runs with cwd=source_dir, so resolve against the caller's cwd
    replacements_dir = Path(replacements_dir).resolve()
    patches = source_dir / "debian" / "patches"
    for name, (sha256, _reason) in replaced.items():
        patch_file = patches / name
        if not patch_file.is_file():
            raise RuntimeError(f"replaced Debian patch {name} is no longer in the source; review the replacement")
        if hashlib.sha256(patch_file.read_bytes()).hexdigest() != sha256:
            raise RuntimeError(f"replaced Debian patch {name} changed in Debian; review it before replacing again")
        for direction, patch in (["--reverse"], patch_file), ([], replacements_dir / name):
            subprocess.run(
                [
                    "patch", *direction, "--strip=1", "--fuzz=0", "--no-backup-if-mismatch",
                    "--input", str(patch),
                ],
                cwd=source_dir,
                check=True,
            )
        shutil.copyfile(replacements_dir / name, patch_file)


def changelog_replace_cmds(
    replaced: "dict[str, tuple[str, str]]" = REPLACED_DEBIAN_PATCHES,
) -> "list[list[str]]":
    """dch lines appended to the Zamfono changelog entry, one per replaced
    patch, so `apt changelog` states every deviation from Debian's source."""
    return [
        ["dch", "--append", f"Replace Debian's {name} with a fixed version: {reason}"]
        for name, (_sha256, reason) in replaced.items()
    ]


def install_patches(source_dir: "str | Path", overlay_dir: "str | Path") -> "list[str]":
    """Applies every overlay patch in sorted-filename order, with zero fuzz
    tolerance, so a rejected or ambiguous change aborts before the container
    ever starts. The overlay edits debian/ packaging files, which the quilt
    patch system does not cover: dpkg-source -b takes debian/ verbatim into
    the .debian.tar and then applies debian/patches/series on top of it, so a
    series patch touching debian/ is applied a second time and fails."""
    source_dir = Path(source_dir)
    # patch runs with cwd=source_dir, so a caller-relative overlay path is
    # resolved here, against the caller's cwd, and not inside the source
    # tree.
    overlay_dir = Path(overlay_dir).resolve()
    applied = sorted(p.name for p in overlay_dir.glob("*.patch"))
    for name in applied:
        subprocess.run(
            [
                "patch", "--strip=1", "--fuzz=0", "--no-backup-if-mismatch",
                "--input", str(overlay_dir / name),
            ],
            cwd=source_dir,
            check=True,
        )
    return applied


def set_changelog_version_cmd(new_version: str) -> "list[str]":
    return [
        "dch",
        "--newversion", new_version,
        "--distribution", "trixie",
        "--force-distribution",
        "Zamfono rebuild for Debian 13",
    ]
