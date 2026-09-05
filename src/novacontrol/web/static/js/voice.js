/* ── Voice System ───────────────────────────────────────────── */

function setupVoice() {
  const SpeechRecognition = window.SpeechRecognition || window.webkitSpeechRecognition;
  const voiceButton = byId("voiceButton");
  const voiceChatButton = byId("voiceChatButton");
  const stopVoiceButton = byId("stopVoiceButton");

  if (!SpeechRecognition) {
    voiceButton.disabled = true;
    voiceChatButton.disabled = true;
    stopVoiceButton.disabled = true;
    setVoiceStatus("Voice is unavailable in this browser.");
    return;
  }

  state.recognition = new SpeechRecognition();
  state.recognition.continuous = false;
  state.recognition.interimResults = true;
  state.recognition.lang = "en-US";

  state.recognition.onstart = () => {
    state.voiceError = ""; // a successful mic acquisition clears any prior block
    setVoiceStatus("Listening...");
  };

  state.recognition.onresult = (event) => {
    let transcript = "";
    for (let index = event.resultIndex; index < event.results.length; index += 1) {
      transcript += event.results[index][0].transcript;
      if (!event.results[index].isFinal) {
        byId("chatInput").value = transcript.trim();
        return;
      }
    }
    transcript = transcript.trim();
    if (!transcript) return;
    byId("chatInput").value = transcript;
    if (state.voiceMode === "conversation") {
      setVoiceStatus("Thinking...");
      submitChat({ speak: true });
    } else {
      setVoiceStatus("Captured. Press Send when ready.");
    }
  };

  state.recognition.onerror = (event) => {
    const code = event.error || "unknown";
    if (code === "aborted") return; // abort on Stop is intentional, not a failure
    // Permission/device failures persist until fixed; transient ones (no speech,
    // network) only flash guidance and let the status return to ready.
    const persistent = ["not-allowed", "service-not-allowed", "security", "audio-capture"].includes(code);
    setVoiceError(code, persistent);
  };

  state.recognition.onend = () => {
    // A permission block must not be masked by the normal ready state.
    if (state.voiceError) {
      const [statusText] = voiceGuidance(state.voiceError);
      setVoiceStatus(statusText);
      return;
    }
    if (state.voiceMode !== "conversation" || !state.voiceChatActive) {
      setVoiceStatus("Voice ready");
    }
  };

  voiceButton.addEventListener("click", () => startVoice("dictation"));
  voiceChatButton.addEventListener("click", () => {
    state.voiceChatActive = true;
    startVoice("conversation");
  });
  stopVoiceButton.addEventListener("click", stopVoiceChat);
}

function startVoice(mode) {
  if (!state.recognition) return;
  state.voiceMode = mode;
  try { state.recognition.abort(); } catch {}
  try {
    state.recognition.start();
  } catch (error) {
    const name = error && error.name ? String(error.name).toLowerCase() : "";
    if (name === "notallowederror" || /permission/i.test((error && error.message) || "")) {
      setVoiceError("not-allowed");
    } else {
      setVoiceStatus("Voice could not start.");
      renderVoiceNotice("Voice could not start. Allow microphone access, then press the voice button again.");
    }
  }
}

function voiceGuidance(code) {
  // Returns [status headline, actionable message] for a voice failure code.
  const guidance = {
    "not-allowed": [
      "Microphone blocked",
      "Microphone access was denied. Click the lock or info icon in the address bar, open Site settings, allow the microphone for this site, then reload the page and try again.",
    ],
    "service-not-allowed": [
      "Microphone blocked",
      "This site is not allowed to use the microphone. Allow microphone access in the browser's site settings, then reload the page and try again.",
    ],
    security: [
      "Microphone blocked",
      "The browser blocked microphone access (this can happen on insecure pages). Allow the microphone and make sure the page is served over HTTPS or localhost, then reload and try again.",
    ],
    "audio-capture": [
      "No microphone found",
      "No microphone was detected. Connect one or pick it in your system sound settings, then try again.",
    ],
    network: [
      "Voice service offline",
      "The speech recognition service could not be reached. Check your internet connection and try again.",
    ],
    "no-speech": [
      "No speech heard",
      "I did not hear anything. Speak closer to the microphone, check its volume, and try again.",
    ],
  };
  return guidance[code] || [
    "Voice input failed",
    `Voice input failed (${code}). Allow microphone access in the browser and try again.`,
  ];
}

function setVoiceError(code, persistent = true) {
  state.voiceError = persistent ? code : "";
  const [statusText, noticeText] = voiceGuidance(code);
  setVoiceStatus(statusText);
  renderVoiceNotice(noticeText);
}

function stopVoiceChat() {
  state.voiceChatActive = false;
  state.voiceMode = null;
  state.pendingSpeak = false;
  if (window.speechSynthesis) window.speechSynthesis.cancel();
  if (state.recognition) {
    try { state.recognition.abort(); } catch {}
  }
  setVoiceStatus("Voice stopped");
}

function setVoiceStatus(text) {
  const status = byId("voiceStatus");
  if (status) status.textContent = text;
}

function renderVoiceNotice(text) {
  const stream = byId("chatStream");
  if (!stream) return;
  // Keep at most the latest voice notice; repeated failed attempts replace it.
  stream.querySelectorAll(".voice-notice").forEach((node) => node.remove());
  stream.classList.remove("empty-state");
  const notice = el("div", "answer-notice voice-notice");
  notice.appendChild(el("p", "", text));
  stream.appendChild(notice);
}

function speakLast() {
  if (!window.speechSynthesis) {
    renderVoiceNotice("Speech output is not available in this browser.");
    return;
  }
  const text = state.lastText || "No response yet.";
  window.speechSynthesis.cancel();
  const utterance = new SpeechSynthesisUtterance(text);
  utterance.onstart = () => setVoiceStatus("Speaking...");
  utterance.onend = () => setVoiceStatus(state.voiceChatActive ? "Voice ready" : "Voice output ready");
  window.speechSynthesis.speak(utterance);
}
