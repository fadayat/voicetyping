# Privacy

VoiceTyping records only after the user starts a recording. It sends audio to
Google for transcription. A failed Live session can also send the complete
recording through the batch transcription path. Starting a recording may
temporarily unmute the selected PipeWire microphone; VoiceTyping restores its
previous mute state when recording ends or the application closes.

Successful recordings are not archived. When transcription fails, VoiceTyping
may keep a temporary recovery recording so it can retry later, including after
the original recording session has ended. By default, the files are under
`~/Documents/VoiceTyping Recovery` on Linux and
`%LOCALAPPDATA%\VoiceTyping\Recovery` on Windows. Linux applies owner-only
permissions (`0700` for the folder and `0600` for pending audio). Windows relies
on the current user's account permissions. A pending recording remains until a
retry succeeds or the user manually deletes it. A successful retry deletes the
audio and keeps the recovered text in application memory for copying. If
several recordings recover, all recovered texts remain queued in memory until
they are successfully copied or the application exits.

VoiceTyping does not log audio, transcript text, or API keys. On Linux, the
Gemini API key is stored as plain text in
`~/.config/voicetyping/config.json` with owner-only permissions (`0600`). On
Windows, it is stored in Windows Credential Manager and is not written to the
settings file or package. Anyone with access to the unlocked local account may
still be able to use locally available credentials. Language hints and
configured custom-vocabulary terms are sent to Google with transcription
requests. Do not place confidential text in the custom vocabulary. Keep the
configuration, recovery folder, and any diagnostic files on the local device.
