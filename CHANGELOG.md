# Changelog

## 2.1.0

### Added

- Live transcription during F8 recordings, with manual start/stop boundaries and
  the complete-recording batch path retained as a fallback.
- A transparent, audio-reactive orb in an isolated GTK/Cairo renderer, without
  status text or desktop notifications.
- Automatic clipboard copy and paste after finalization. Recognized standalone
  terminals receive Ctrl+Shift+V; ordinary text fields keep Ctrl+V.
- Configuration options to disable Live transcription or automatic paste and
  to customize language hints. Azerbaijani is the default hint.
- Offline tests for recording lifecycle, streaming failures, clipboard ordering,
  terminal detection, animation, and global shortcut behavior.

### Improved

- Bounded audio buffering and per-recording session isolation, including capture
  of final microphone chunks and cancellation on shutdown.
- Standard-library F8 signal helper and startup toggle handling.
- Sanitized diagnostics without transcript, microphone content, or API-key logs.
- Installation of all runtime modules and documentation for Linux dependencies,
  setup, privacy, and compatibility limits.

### Compatibility and privacy

- Users provide their own Gemini API key. No shared or personal key is included.
- Audio is sent to Google during user-initiated recording. Failed Live sessions
  may resend the complete recording through batch transcription.
- GNOME Wayland automatic paste uses a private Mutter API and may vary across
  desktop versions. Unknown terminals and custom paste bindings may need manual
  paste. No fixed 0.2-second clipboard delay is added.
- Automatic paste never injects Enter. Multiline terminal text may still execute
  commands depending on the shell and terminal settings.
