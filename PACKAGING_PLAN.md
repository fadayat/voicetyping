# VoiceTyping 2.3.0 Packaging Plan

This document tracks the move to one Python source package. Developer runs,
AppImage builds, and Ubuntu packages must install the same wheel.

## Phase status

| Phase | Status | Checkpoint | Latest checkpoint commit | Next step |
| --- | --- | --- | --- | --- |
| 1. Protect existing work and select base | Complete | Dirty audio-routing and recovery work is preserved on separate branches; `feat/v2.3-unified` starts from `origin/main` at v2.2.0; audio routing is cherry-picked. | `f6f83c8` | Port recording recovery and correct route lifetime on the v2.2 streaming lifecycle. |
| 2. Integrate audio routing and recovery | Complete | Route lifetime now covers the full recording. Private spooling, pending retry, cancellation, RAM-only recovered text, and manual deletion behavior are covered by focused tests. | `8d13007` | Move the maintained modules into `src/voicetyping/` and make package imports relative. |
| 3. Create one installable Python package | Pending | Keep Python modules under `src/voicetyping/`; preserve a thin root launcher for developer compatibility. | Pending | Add package metadata, relative imports, console scripts, and a wheel-based developer installer. |
| 4. Make wheel the source for all installers | Pending | `uv.lock` remains the dependency lock; generate hash-pinned `requirements.txt` from it. | Pending | Install the same wheel from `install.sh`, AppImage, and `.deb` builders. |
| 5. Add release harness and documentation | In progress | Harness, privacy, and plan documents are being prepared. | Pending | Validate local commands and update README and changelog for the completed implementation. |
| 6. Add CI and draft-release automation | In progress | CI design requires source/wheel checks, manual artifact builds, and tag-triggered draft releases. | Pending | Validate workflow syntax and exercise each harness subcommand on built artifacts. |
| 7. Release gates | Pending | No public release or PyPI publication is part of this milestone. | Pending | Pass all automated checks, then complete the manual desktop smoke test before publishing a reviewed draft. |

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
