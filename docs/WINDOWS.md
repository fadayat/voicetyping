# VoiceTyping for Windows

The Windows edition is part of the VoiceTyping repository. It uses the shared
recording and Gemini transcription engine with Windows-specific desktop
integration. Windows 10/11 x64 is the initial target.

## Run the packaged app

Download the Windows ZIP from a project release, verify `SHA256SUMS.txt`, and
extract the complete `VoiceTyping` folder. Run `VoiceTyping.exe` from that
folder; do not move the executable by itself. Enter your own Gemini API key in
the app. It is stored in Windows Credential Manager. Non-secret settings and
failed-recording recovery audio are kept under `%LOCALAPPDATA%\VoiceTyping`.

F8 starts and stops recording by default. The shortcut can be changed in the
app. The completed transcript is copied to the clipboard. Automatic paste is
attempted only while the original external window remains active and no
modifier key is held; use Ctrl+V if it falls back to clipboard-only delivery.

## Privacy

Audio is sent to Google's Gemini service only for recordings started by the
user. Live transcription streams audio while recording; the full recording can
be sent through batch transcription if Live fails. Failed recordings stay in
local recovery storage until transcription succeeds or the user deletes them.
That folder is not encrypted by VoiceTyping. The app does not write transcript
history or API keys into its package or preferences file. Windows clipboard
history and cloud sync follow the user's Windows settings.

The Windows build workflow accepts an optional `GEMINI_API_KEY` Actions secret.
When provided, the packaged executable transcribes an included synthetic
speech clip. The workflow uses the secret only for that run; it does not store
or print the returned transcript, and it removes the secret from the process
environment after the request. No credential is included in the build
artifacts.

## Build and verification

Use Windows x64 with Python 3.12 and Tcl/Tk. From PowerShell at the repository
root, run:

```powershell
./packaging/windows/build.ps1
```

The script installs the project Windows extra, runs `tests/windows`, compiles
the Windows package, builds an onedir GUI executable with PyInstaller, and
runs its self-test. It writes the distributable ZIP, SHA-256 manifest and
non-sensitive JSON evidence under `dist/`. A real Gemini batch smoke test is
also run when the build process has a key. These checks do not exercise a
physical microphone, Gemini Live streaming, or interactive pasting into other
applications; complete those checks on a normal Windows desktop before public
distribution.

Third-party license files and resolved dependency versions are bundled for
review. The app is unsigned, so Windows SmartScreen may show a reputation
warning. Review bundled notices and redistribution terms before publishing.
