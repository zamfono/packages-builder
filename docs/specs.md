# `zamfono/packages-builder` Specification

## Purpose

`zamfono/packages-builder` defines the build hosts `packages-builder` (amd64) and
`packages-builder-arm64`, the builders of all Zamfono packages. They build Debian 13
(trixie) packages of **Asterisk 22** from Debian's `asterisk` source package, split
into functional module groups, and of **baresip** from its upstream releases (see [baresip](#baresip)), verify every build
against a set of gates, and upload the result over the private network to the
Zamfono packages host (`zamfono/packages`), which publishes each product in its own
archive.

**Scope is locked: two products, one distro** — Asterisk 22 and baresip on Debian
13, amd64 and arm64. The Asterisk build starts from Debian's source package
(building from upstream tarballs would lose AMR support and all of Debian's
packaging).

## Architectures

Each host's role follows from its own architecture (`dpkg --print-architecture`);
the code and the playbook are the same on both.

- **amd64 is the primary.** It runs the pipeline below in full and publishes the
  source, the `Architecture: all` packages (`asterisk-config`, `asterisk-dev`,
  `asterisk-doc`, the `asterisk-modules` meta package) and its amd64 binaries.
- **arm64 follows.** When the archive's published source version is newer than its
  published arm64 binaries (or none exist), it downloads exactly that source from
  the archive — `apt` verifies it against the archive key, pinned in this repository
  as `install/files/zamfono-archive-keyring.gpg` — builds it unmodified with
  `--build=any`, runs the gates, and uploads the arm64 binaries only. It never reads
  sid, never patches, and never computes a version: every architecture ships the
  same source at the same `+zamfono13.N`.
- arm64 polls the archive every five minutes, so it publishes about one arm64
  build after amd64. Until then, new arm64 installs fail (`asterisk` depends on
  `asterisk-config` at its exact version, which the archive holds only at the new
  version) and upgrades on existing arm64 hosts are held back.
- On either architecture, a failed attempt keeps its workspace and holds off the
  next attempt for that version for six hours instead of rebuilding back to back
  every five minutes; a newer version builds at once.

## baresip

Debian's `baresip` is stuck at 1.1.0 in every suite, so unlike Asterisk there is no
Debian source to follow: baresip is built from upstream.

- **Source:** the upstream release tarballs of `baresip/baresip` and its library
  `baresip/re` (libre), which upstream tags in lockstep at the same version. One
  source package `baresip` carries both — libre as its `re` component tarball
  (`baresip_<v>.orig-re.tar.gz`) — with this repository's packaging
  (`packaging/baresip/debian`) and the version `<v>-1+zamfono13.N`.
- **Trigger:** the primary compares the latest upstream GitHub release (not
  pre-releases) with the published source version; a newer release is built and
  published automatically, without review. The tags are unsigned and the
  tarballs come from GitHub over TLS — that is the trust for them; once
  published, followers verify against the archive's signed `Sources`, as for
  Asterisk. Only a plain `vX.Y.Z` tag is accepted.
- **One binary package, `baresip`**, holding the program, its modules and libre
  plus libbaresip as **private** libraries in `/usr/lib/<triplet>/baresip`,
  found through RUNPATH. libre bumps its soname with nearly every release; as a
  public library each bump would rename its package. The build is headless:
  `Build-Depends` decides which modules upstream's CMake builds, and leaves out the
  GUI and video modules (GTK, SDL, X11, FFmpeg, GStreamer, V4L2) and their
  dependency stacks. Should those be wanted, split them into their own packages
  then, as Debian does (`baresip-core` plus one package per stack).
- It conflicts with and replaces Debian's `baresip-core`, which ships the same
  program.
- **Archive:** `https://packages.zamfono.com/debian/baresip`, suites `trixie` and
  `trixie-debug`, uploaded into the upload chroot's `/incoming-baresip`.
- **Gates:** `dh_missing`, unresolved shared libraries, lintian (errors fail), a
  clean install on pristine trixie that loads every shipped module into a running
  baresip, the purge (nothing of the package may remain), and — primary only, once
  a version is published — the upgrade from it. The module check flags modules
  that do not *link* (libre's `dl:` lines), not modules that link but decline to
  start in the container (`evdev` without an input device, `stdio` without a
  terminal) — the same line the Asterisk module-load gate draws.
- A packaging change in this repository alone triggers no rebuild; the next
  upstream release carries it.
- Both products run in one timer-triggered run under the host lock, Asterisk
  first; one product's failure never keeps the other from building, and fails
  the run.

## Non-goals

- **Archive management.** The builder never runs `reprepro`, never signs
  `Release` files, and never holds the archive signing key. Its only key is its
  upload key.
- **Multi-version / multi-distro machinery.** The repository URL layout leaves room
  for more lines later; only Asterisk 22 and baresip on trixie are built.
- **Publish orchestration.** Publishing is `debsign` + an SFTP batch upload; ingest and serving
  are the packages host's job.

## Build pipeline

1. **Trigger:** a systemd timer runs the freshness check every five minutes on
   every builder, except 02:00–03:25 UTC (the hosts' time), the hour around the
   03:00 unattended reboot: a run started before 02:00 is done before the
   reboot, and none starts until the host is back. It compares
   the `asterisk` source version in Debian `sid` against the last published
   version; nothing newer → exit.
2. **Fetch:** `apt-get source asterisk` from sid. Building sid source on trixie
   requires `debhelper` (and its version-locked binaries) from **unstable**, pinned
   so only debhelper is raised.
3. **Patch:** first, each Debian patch in `REPLACED_DEBIAN_PATCHES`
   (`orchestrator/packaging_overlay.py`) is reversed, our fixed version from
   `packaging/debian-patches/` is applied with zero fuzz and overwrites Debian's
   file (the `debian/patches/series` line and `.pc/` stay; ours touches the same
   files), and the Zamfono `debian/changelog` entry gets one line per replaced
   patch naming it and why. Debian's file is pinned by SHA-256; a changed or
   missing patch fails the run, so a human decides again (when Debian ships the
   fix, our copy goes). Today that is only `2016_opus_plc.patch`, which in
   Debian's version silences transcoded legs after a far end's SSRC change
   (fixed version offered as Debian #1151451). Then the module-split packaging
   patch must apply with **zero fuzz** against the fetched source; fuzz means the
   Debian packaging moved and the patch needs a rebase, not a forced apply.
4. **Version:** append `+zamfono13.N` to the Debian source version. `N` starts at 1
   for each newly imported Debian version, and the result must compare strictly
   newer (`dpkg --compare-versions`) than the last published version.
5. **Build:** `pbuilder` with a trixie base chroot (one base tarball, kept current
   with `pbuilder update`). The build phase runs network-isolated: a networked prep
   phase fetches everything, the compile itself runs offline — cheap, real
   supply-chain hardening.
6. **Gates:** every gate must pass (see below); any failure aborts before upload.
7. **Sign and upload:** `debsign` the `.changes` with the builder upload key, then
   an OpenSSH `sftp` batch upload over the private network into the archive's own
   incoming directory in the upload chroot (asterisk `/incoming`, baresip
   `/incoming-baresip`; the upload account's forced `internal-sftp` accepts nothing
   else, and trixie's `dput` offers no sftp method). The `.changes` file is
   transferred after everything it references, because ingestion on the packages
   host triggers on `*.changes`. The run ends only once the archive's index lists
   the uploaded version (up to 10 minutes): a refused upload fails the run instead
   of passing silently, and the next run never reads an index that predates it.
8. **Upload content:** the full `.changes` — binaries, source (`.dsc` + tarballs,
   so the archive can publish `deb-src`), and the `-dbgsym` packages (routed into a
   debug suite by the packages host).

## The module split (the product)

The functional module grouping is the point of this repository:

- `manifest/modules.json` defines the functional groups (25 today) and their
  module coverage; the `asterisk` core package depends only on
  `asterisk-modules-core`.
- `app_test.so` stays in `core`: it is an operator-facing tone/echo test tool
  (`TestServer`/`TestClient`).
- `test_*.so` and `res_stasis_test.so` are excluded: Asterisk test-framework
  modules. `res_stasis_test` escapes the `test_*` glob and must be removed
  explicitly. Verified against Asterisk `loader.c`.
- There is no `asterisk-modules-voicemail-imap` group: `app_voicemail`'s IMAP
  backend links `libc-client` (uw-imap), which Debian removed. Revisit only if a
  replacement is packaged.

## Gates

The gates are the quality bar; they run on every build:

- **Manifest coverage** — every shipped module is assigned to exactly one group in
  `manifest/modules.json`, and the manifest names no module the build did not
  produce.
- **`dh_missing`** — no built file is silently dropped from all packages.
- **Dependency derivation** — package dependencies derive correctly from the split.
- **Lintian** — errors fail the build; warnings are retained in the build output
  for review, and do not fail it.
- **Install** — the packages install cleanly on a pristine trixie system.
- **Module load** — every packaged module loads in a running Asterisk. This gate
  flags **load failures only**, deliberately: `module show` prints
  `Running`/`Not Running` (never "Failed"), and a dependency-declined module is
  indistinguishable there from a config- or hardware-absent one (Debian's
  `modules.conf.sample` noloads `res_hep*`/`app_voicemail_odbc`, and modules like
  `chan_dahdi` show `Not Running` without hardware). Any "Not Running" or
  present-set check false-positives on every build. Investigated three times;
  settled — do not "fix" it.
- **Meta-package coverage** — the meta packages pull in the intended groups.
  Primary only: the meta package is `Architecture: all`, built by amd64; the arm64
  full install (module-load gate) still proves every group exists on arm64.
- **Upgrade** — upgrading from the previously published version succeeds. Primary
  only: by the time arm64 builds, the archive no longer holds the `asterisk-config`
  its previous version depends on, so that version cannot be installed to upgrade
  from; the transition is packaging, identical on every architecture.

On arm64 the install gates take the `Architecture: all` packages from the archive,
where amd64 published them at the candidate's version.
- **Purge** — removing and purging leaves the system clean.

## Keys

- Each builder holds exactly one signing identity: its own **upload key**, used by
  `debsign` on `.changes` files. The packages host authorizes it in reprepro's
  `conf/uploaders` — that listing is what makes this builder allowed to publish.
- The archive signing key never exists on this host. A builder compromise therefore
  cannot sign the archive, regardless of how the build runs.
- SSH to the packages host uses the private network with
  `StrictHostKeyChecking=yes` and a provisioned `known_hosts` entry for the packages
  host.

## Provisioning

Provisioned with Ansible, run by hand from the operator's machine — deployment is a
manual playbook run, never a service. This repository carries `install/`:

- `playbook.yaml` — the host's full configuration (full system upgrade with a
  reboot when one is required, pbuilder and its base chroot,
  the build/gate scripts, the systemd timer and service, the debhelper pin,
  unattended security upgrades with automatic reboot at 03:00 UTC — inside the
  timer's 02:00–03:30 gap, so no build runs into it). An interrupted build is
  simply repeated at the next timer firing, with one exception: a reboot during
  the base-chroot update can truncate `base-trixie.tgz`, which an operator must
  recreate (`rm` it and re-run the playbook); the gap makes that a non-event in
  practice.
- `run.sh` — entry point: creates/updates a local virtualenv from the pin file,
  runs the playbook.
- `requirements.txt` — pins the exact `ansible-core` version. `ansible.builtin`
  covers every task; no collections are installed.

No credentials in the repository: the operator's `ssh-agent` supplies the SSH key;
host verification is the operator's normal `~/.ssh/known_hosts`. The builder's
upload key (GPG) and its SSH key toward the packages host are provisioned
host-locally, outside Git.

## Network interface

- The builder offers no public services; the Hetzner Cloud Firewall blocks all
  public inbound, always. The operator administers it over the private network
  (`root@10.250.0.3`, `root@10.250.0.4` for arm64) with the packages host as SSH
  jump host; authentication
  is key-only.
- Outbound: Debian mirrors (source fetch, chroot maintenance), HTTPS to
  `packages.zamfono.com` (published versions, the arm64 source fetch, gate
  containers), HTTPS to GitHub (the primary's baresip release check and
  tarballs: `api.github.com`, `github.com`, `codeload.github.com`) and SFTP to
  the packages host (`10.250.0.2`) for uploads.
- The packages host never reaches back into the builder.

## Verification

The gates are the per-build bar. Repository-level verification runs the real flow
end to end on the real hosts — a converged `changed=0` playbook run is not proof of
correct behavior:

1. A full build on each provisioned host passes all gates.
2. The resulting upload lands on the packages host, is ingested, and the packages
   install on a fresh trixie client via `apt`.
3. A version bump produces a `+zamfono13.N` version strictly newer than the last
   published one.
4. The timer's no-op path (sid has nothing newer) exits without building or
   uploading.

## Related repositories

- `zamfono/packages` — the archive host that ingests this builder's uploads, signs
  the published indexes, and serves them.
