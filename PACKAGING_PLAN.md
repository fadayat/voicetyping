# VoiceTyping 2.3.0 Packaging Plan

This document tracks the move to one Python source package. Developer runs,
AppImage builds, and Ubuntu packages must install the same wheel.

## Phase status

| Phase | Status | Checkpoint | Latest checkpoint commit | Next step |
| --- | --- | --- | --- | --- |
| 1. Protect existing work and select base | Complete | Dirty audio-routing and recovery work is preserved on separate branches; `feat/v2.3-unified` starts from `origin/main` at v2.2.0; audio routing is cherry-picked. | `f6f83c8` | Port recording recovery and correct route lifetime on the v2.2 streaming lifecycle. |
| 2. Integrate audio routing and recovery | Complete | Route lifetime now covers the full recording. Private spooling, pending retry, cancellation, RAM-only recovered text, and manual deletion behavior are covered by focused tests. | `8d13007` | Move the maintained modules into `src/voicetyping/` and make package imports relative. |
| 3. Create one installable Python package | Complete | All maintained modules now live under `src/voicetyping/`; imports are relative, the root launcher is thin, and the wheel exposes both console commands from `voicetyping.__version__`. | `fc5e291` | Regenerate the dependency lock/export, then make every installer consume the same wheel. |
| 4. Make wheel the source for all installers | Complete | `uv.lock` and the hash-pinned export agree. `install.sh`, AppImage, and `.deb` now validate and install one supplied wheel; the isolated installer passed twice while preserving two rollback releases. | `3a6084f` | Validate the release harness and finish the privacy, README, and changelog documentation. |
| 5. Add release harness and documentation | Complete | The isolated source and wheel harnesses pass, cover every packaged module and both entry points, and verify the installer twice without reading the user's app state. README, changelog, privacy, metainfo, and manual smoke steps describe v2.3 behavior. | `5b1b3a5` | Validate CI syntax and make PR, manual-artifact, and tag workflows consume the same tested wheel. |
| 6. Add CI and draft-release automation | Complete | PR/main runs source and wheel gates. Manual runs build AppImage and `.deb` from the tested wheel without publishing. Matching `v*` tags run both artifact harnesses, create checksums and `.zsync`, then prepare a draft release only. | `f505c9d` | Run the final local gates and leave desktop artifact smoke tests pending until CI produces release candidates. |
| 7. Release gates | Local gates complete; CI/manual pending | The source harness passes 192 tests; the rebuilt wheel passes clean install, module, entry-point, and two-run installer checks; Ubuntu 24.04 builds and installs the `.deb` successfully. Final review gaps are closed. | `17ecd80` | With explicit authorization, push the branch and run the manual artifact workflow. Validate its AppImage and complete the desktop smoke test before creating a tag or publishing a reviewed draft. |

## Decisions

- Keep one maintained source tree in `src/voicetyping/`. The root
  `voicetyping.py` remains a small developer-compatibility launcher.
- Use `voicetyping.__version__` as the runtime version. The package metadata,
  wheel, AppImage, `.deb`, and `v*` tag must agree.
- Keep `uv.lock` authoritative. Generate `requirements.txt` with hashes from
  that lock for builders that consume pip requirements.
- Build one wheel per CI/release run. Pass that exact wheel to AppImage and
  `.deb` builds.
- Keep user configuration, API-key storage, desktop shortcuts, autostart
  behavior, and release rollback directories intact.
- Support Python 3.10+, x86_64 AppImage, and Ubuntu 24.04 amd64 `.deb`.
- Use CI for AppImage toolchain installation. Do not add that toolchain to the
  Fedora developer host.
- Manual artifact builds upload workflow artifacts only. A `v*` tag may create
  a GitHub draft release after validation. Human review remains the publish
  gate. PyPI is out of scope.

## Release gates

- Source harness passes lock, export, compile, and offline unit checks.
- Wheel harness installs into a clean virtual environment and verifies every
  application module and both console commands.
- `install.sh` passes twice against one isolated home directory.
- AppImage checksum, normal launch, extract-and-run, and static runtime checks
  pass.
- `.deb` metadata, contents, clean Ubuntu install, and `--version` checks pass.
- AppImage and `.deb` both report `2.3.0` and contain the same wheel's package.
- Manual smoke test covers checksum, launch/tray, F8 start/stop, built-in and
  USB/Bluetooth microphones, successful transcription, offline failure and
  later recovery, autostart, and shortcut behavior.
- Review the draft release and its checksums before publishing it.

## Phase 7 local checkpoint

- `scripts/release_harness.py source` passes lock/export checks, compilation,
  and all 192 offline tests.
- A rebuilt `2.3.0` wheel passes clean-environment imports, both console entry
  points, and two isolated `install.sh` runs without changing the private
  configuration.
- A disposable Ubuntu 24.04 container builds the `.deb` from that wheel, checks
  exact metadata and package contents, installs dependencies and the package,
  and verifies `VoiceTyping 2.3.0`. Its checksum also passes.
- Shell syntax and all workflow YAML files parse successfully.
- Final review fixes cover AppImage toggle identity across transient mounts,
  fail-closed ELF inspection, exact `.deb` metadata comparison, recovery of
  interrupted WAV staging files, and FIFO access to every recovered text.
- AppImage artifact checks and the desktop smoke test remain pending because
  the AppImage toolchain intentionally runs in CI and no branch push, tag, or
  release action is authorized yet.
