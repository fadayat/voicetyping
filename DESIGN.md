# VoiceTyping voice indicator

The user requested a round animation with expressive edges. It expands while
speaking, becomes small during silence, and disappears when finished. The screen
must show no status text or desktop notifications. F8 remains the recording toggle.

The indicator is a small floating luminous orb on a genuinely transparent desktop
surface. Its single material continues through listening, processing, and completion.
Cool pearl/blue light marks listening and processing; warm light marks completion or
an alert. The outer contour flows gently with the microphone signal.

Audio response uses a short attack and a slower release so ordinary pauses feel
natural. Completion contracts and fades. The GTK animation preference reduces
movement while preserving state feedback. The window never accepts focus or mouse
input, and rendering stops while hidden.

`voice_visual.py` renders antialiased RGBA frames, with supersampling available for
exports. `voice_overlay.py` owns a separate
GTK/Cairo loop for transparent compositing; the application sends only states and
normalized levels, never raw microphone audio, over its local pipe.
