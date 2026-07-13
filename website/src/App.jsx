import './index.css';

function App() {
  return (
    <div className="container">
      <div className="hero">
        <div className="badge">v1.0.0 Now Available</div>
        <h1>VoiceTyping</h1>
        <p className="subtitle">
          Speak your mind, let Gemini type it out. A lightning-fast, native, cross-platform speech-to-text utility designed for modern workflows.
        </p>
        <div className="actions">
          <a href="https://github.com/fadayat/voicetyping" target="_blank" rel="noopener noreferrer" className="btn btn-primary">
            <span>⭐ View on GitHub</span>
          </a>
          <button className="btn btn-secondary" onClick={() => alert("Windows version is currently being integrated into the repository. Check GitHub for updates!")}>
            <span>🪟 Windows Version (Soon)</span>
          </button>
        </div>
      </div>

      <div className="features">
        <div className="feature-card">
          <div className="feature-icon">⚡</div>
          <h3 className="feature-title">Powered by Gemini</h3>
          <p className="feature-desc">Utilizes Google's Gemini 3.1 Flash Lite model for blazing fast and highly accurate speech transcription.</p>
        </div>
        <div className="feature-card">
          <div className="feature-icon">⌨️</div>
          <h3 className="feature-title">Global Hotkeys</h3>
          <p className="feature-desc">Start and stop recording instantly from anywhere on your system by simply pressing the F8 key.</p>
        </div>
        <div className="feature-card">
          <div className="feature-icon">🐧</div>
          <h3 className="feature-title">Native Integration</h3>
          <p className="feature-desc">Features a floating UI overlay, system tray icon, and automatic background processes tailored for Linux & Windows.</p>
        </div>
      </div>

      <footer className="footer">
        <p>Built with ❤️ by Fedayet Shemilov. Open source under the MIT License.</p>
      </footer>
    </div>
  );
}

export default App;
