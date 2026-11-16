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
| 7. Release gates | Draft PR open; package CI in progress | The source harness passes 210 tests. Fedora developer smoke confirms the Buds2 Pro route records successfully and restores its prior mute state. PR source/wheel checks started for the review branch; AppImage and `.deb` reruns remain pending. | `5d85ef1` | Complete PR and manual-artifact CI, then verify the downloaded packages before any tag or release action. |
| 8. Stabilize rapid controls | Automated checks complete; live X11 smoke pending | Shortcut, tray, signal, and portal requests use one bounded controller. X11 repeat pairs are filtered, startup feedback is immediate, and provisional audio routes are restored safely during shutdown. | `4d3aaab` | Run the fixed developer source and verify that rapid or held F8 input cannot strand the tray icon or overlay. |
| 9. Preserve mixed-language terms | Developer runtime active; live speech result pending | Azerbaijani and English hints plus configurable technical vocabulary now reach both Live and batch transcription. Generic fallback prompts preserve code-switching. | `d551ea0` | Verify embedded terms such as `fallback`, `API`, and `GitHub` in Azerbaijani sentences. |
| 10. Restore muted headset capture | Complete | PipeWire sources that begin muted are temporarily unmuted only for an explicit recording, then restored on stop, error, or shutdown. The user confirmed a real Buds2 Pro recording and the source returned to `MUTED`. | `5d85ef1` | Keep this behavior in the wheel, `.deb`, and AppImage artifact smoke tests. |
| 11. Publish review branch | Complete | `feat/v2.3-unified` is available on `origin` with all v2.3 source, tests, documentation, and local checkpoints. | `4d1cd65` | Open a draft pull request for CI and review. |
| 12. Open review pull request | Complete | Draft PR #5 targets `main` from `feat/v2.3-unified`; its source-and-wheel check started automatically. | `feadc54` | Push this checkpoint, then run the authorized manual AppImage/`.deb` workflow from the same branch. |
| 13. Bootstrap pre-merge artifacts | Complete | GitHub cannot dispatch a new workflow before it reaches the default branch. The read-only artifact workflow now also accepts an explicit `build-release-artifacts` PR label, without enabling release publication. | `de54b89` | Push this checkpoint, apply the artifact label to PR #5, and inspect all resulting jobs and packages. |

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
  and all 210 offline tests.
- The Fedora developer runtime starts with the distribution Python, GTK/Cairo
  modules, tray process, overlay process, and private legacy-spool migration.
- The previous `2.3.0` wheel and `.deb` checkpoint passed clean-environment
  imports, both console entry points, two isolated `install.sh` runs, Ubuntu
  24.04 build/install, version, and checksum checks. Repeat these gates after
  the developer smoke test because the legacy-spool runtime fix changed source.
- Shell syntax and all workflow YAML files parse successfully.
- Final review fixes cover AppImage toggle identity across transient mounts,
  fail-closed ELF inspection, exact `.deb` metadata comparison, recovery of
  interrupted WAV staging files, and FIFO access to every recovered text.
- AppImage artifact checks and the desktop smoke test remain pending. Draft PR
  #5 is open, and the authorized manual artifact workflow is the next phase.
  No tag or release action is authorized.

## Rapid-control checkpoint

- [x] Serialize shortcut, tray, signal, and portal toggle requests through one
  lifetime-scoped controller.
- [x] Preserve the requested start/stop intent across slow or failed device
  startup without creating delayed extra recordings.
- [x] Filter X11 release/press auto-repeat pairs while allowing a genuine key
  release and later press.
- [x] Show microphone preparation immediately and ignore input while a
  transcription is already processing.
- [x] Publish a provisional PipeWire route before changing the default input,
  make restoration thread-safe, and keep Quit bounded if a device backend is
  stuck.
- [x] Pass 84 focused lifecycle tests and all 206 offline source tests.
- [ ] Restart the Fedora developer runtime with commit `4d3aaab` and manually
  verify normal F8, quick double press, held F8, tray toggle, and input during
  transcription.
- [ ] Rebuild and rerun wheel and Ubuntu `.deb` gates after the live smoke test.

Next step: launch the fixed developer source for the manual rapid-control smoke
test. Draft PR #5 is open, and package CI is authorized. No tag or release
action is authorized.

## Mixed-language checkpoint

- [x] Default transcription hints include both `az-AZ` and `en-US` while an
  explicit empty language list still enables unrestricted auto-detection.
- [x] A small built-in developer vocabulary includes `fallback`, `API`, and
  other common technical terms; users can replace or disable it in config.
- [x] Live, primary batch, recovery retry, and generic batch fallback paths use
  the same language intent and preserve code-switching.
- [x] README, changelog, and privacy documentation describe vocabulary behavior
  and disclosure to Google.
- [x] Pass 94 focused transcription tests and all 208 offline source tests.
- [x] Restart the Fedora developer runtime with commit `d551ea0`; the tray and
  overlay processes are active from the unified source package.
- [ ] Manually verify Azerbaijani sentences containing `fallback`, `API`, and
  `GitHub`.
- [ ] Rebuild and rerun wheel and Ubuntu `.deb` gates after the live smoke test.

Next step: run the mixed-language speech smoke test from the active developer
service. Draft PR #5 is open, and package CI is authorized. No tag or release
action is authorized.

## Muted-headset checkpoint

- [x] Confirm the failing Buds2 Pro PipeWire source reported `Volume: 1.00
  [MUTED]` while recording startup itself succeeded.
- [x] Preserve the selected source's initial mute state before changing the
  default microphone.
- [x] Temporarily unmute only after the requested source becomes active.
- [x] Restore both the prior source mute and default-input state on stop,
  startup failure, concurrent shutdown, or normal completion.
- [x] Pass 80 focused routing/lifecycle tests and all 210 offline source tests.
- [x] Run commit `5d85ef1` from a persistent local developer environment and
  verify the real muted Buds2 Pro route becomes unmuted, then muted again.
- [x] Complete a real Buds2 Pro recording and confirm the source returns to its
  original `MUTED` state afterward.
- [ ] Verify `fallback`, `API`, and `GitHub` inside Azerbaijani speech.
- [ ] Rebuild and rerun wheel and Ubuntu `.deb` gates after the live smoke test.

Next step: keep the confirmed headset behavior in the rebuilt wheel, `.deb`,
and AppImage checks. Draft PR #5 is open, and package CI is authorized. No tag
or release action is authorized.

## Remote branch checkpoint

- [x] Push `feat/v2.3-unified` to `origin` after 210 offline tests and the
  user-confirmed Buds2 Pro recording smoke test.
- [x] Open draft PR #5 into `main`; its source-and-wheel check starts
  automatically.
- [ ] Run the manual AppImage and Ubuntu `.deb` workflow from the review branch.
- [ ] Download and smoke-test both workflow artifacts before any tag or draft
  release.

Next step: push this PR checkpoint, then run the authorized manual artifact
workflow and inspect its AppImage and `.deb` outputs. No tag, draft release, or
publication action is authorized.

## Artifact workflow bootstrap checkpoint

- [x] Confirm the initial `workflow_dispatch` request fails with HTTP 404
  because `release-artifacts.yml` is not yet present on the default branch.
- [x] Keep `workflow_dispatch` for normal post-merge manual builds.
- [x] Add a read-only PR `labeled` trigger gated by the exact
  `build-release-artifacts` label.
- [x] Keep draft-release creation disabled for the PR-label path.
- [ ] Push the checkpoint and apply the label to draft PR #5.
- [ ] Wait for wheel, AppImage, and `.deb` validation, then download and inspect
  the uploaded artifacts.

Next step: apply `build-release-artifacts` to PR #5 after this checkpoint is on
the remote branch. No tag, draft release, or publication action is authorized.
