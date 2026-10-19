# Security policy

## Scope

This repository builds, verifies, signs, and uploads the Asterisk packages
served at `https://packages.zamfono.com/debian/asterisk`. Security reports
belong here when they concern:

- the published packages (a package installing something it should not, a
  tampered artifact);
- the build pipeline (the pbuilder build, the gates, the signed upload);
- the builder's upload key or its handling.

Vulnerabilities in Asterisk itself are upstream software vulnerabilities:
report them to the [Asterisk project's security
process](https://docs.asterisk.org/Asterisk-Community/Asterisk-Issue-Guidelines/),
not here. This repository picks such fixes up automatically with the next
Debian `sid` upload.

## Reporting

Report privately via GitHub's ["Report a
vulnerability"](https://github.com/zamfono/packages-builder/security/advisories/new)
form. Please do not open a public issue for anything exploitable.

## Supported versions

Only the newest published version receives fixes — every publish supersedes
its predecessors. The asterisk suites serve the newest build only; the baresip
suites keep older builds installable, but they receive no fixes.

## Verifying the archive

The repository is signed; clients install the
`zamfono-archive-keyring` package following the instructions at
`https://packages.zamfono.com/debian/asterisk`, whose `Signed-By` pins the
key. APT refuses unsigned or wrongly signed indexes under that
configuration. The key's fingerprint is published in
[`README.md`](README.md#key-fingerprint) as the out-of-band anchor for
verifying an installed copy.
