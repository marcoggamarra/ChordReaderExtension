const ANALYSIS_ENDPOINT = "http://127.0.0.1:8765/analyze";
const OFFSCREEN_DOCUMENT = "offscreen.html";

let isRunning = false;
let activeTabId = null;
let lastResult = null;

async function ensureOffscreenDocument() {
  const contexts = await chrome.runtime.getContexts({
    contextTypes: ["OFFSCREEN_DOCUMENT"],
    documentUrls: [chrome.runtime.getURL(OFFSCREEN_DOCUMENT)]
  });

  if (contexts.length > 0) {
    return;
  }

  await chrome.offscreen.createDocument({
    url: OFFSCREEN_DOCUMENT,
    reasons: ["USER_MEDIA"],
    justification: "Capture tab audio for local music analysis."
  });
}

async function startAnalysis(tabId) {
  await ensureOffscreenDocument();

  const streamId = await chrome.tabCapture.getMediaStreamId({ targetTabId: tabId });

  isRunning = true;
  activeTabId = tabId;

  await chrome.runtime.sendMessage({
    type: "offscreen-start-capture",
    streamId,
    tabId
  });
}

async function stopAnalysis() {
  isRunning = false;
  activeTabId = null;

  await chrome.runtime.sendMessage({
    type: "offscreen-stop-capture"
  });
}

async function sendChunkForAnalysis(message) {
  if (!isRunning) {
    return;
  }

  try {
    const response = await fetch(ANALYSIS_ENDPOINT, {
      method: "POST",
      headers: {
        "Content-Type": "application/json"
      },
      body: JSON.stringify({
        sampleRate: message.sampleRate,
        samples: message.samples,
        spectrum: message.spectrum,
        energy: message.energy,
        timestampMs: message.timestampMs
      })
    });

    if (!response.ok) {
      throw new Error(`HTTP ${response.status}`);
    }

    const payload = await response.json();
    lastResult = {
      bpm: payload.bpm,
      chord: payload.chord,
      confidence: payload.confidence,
      energy: payload.energy ?? message.energy,
      spectrum: message.spectrum || []
    };

    chrome.runtime.sendMessage({
      type: "analysis-update",
      data: lastResult
    });
  } catch (error) {
    isRunning = false;
    chrome.runtime.sendMessage({
      type: "analysis-error",
      error: `Local API unavailable: ${error.message}`
    });
  }
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "start-analysis") {
    startAnalysis(message.tabId)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => {
        isRunning = false;
        sendResponse({ ok: false, error: error.message });
      });
    return true;
  }

  if (message.type === "stop-analysis") {
    stopAnalysis().finally(() => sendResponse({ ok: true }));
    return true;
  }

  if (message.type === "get-state") {
    sendResponse({
      isRunning,
      activeTabId,
      lastResult
    });
    return;
  }

  if (message.type === "offscreen-audio-chunk") {
    sendChunkForAnalysis(message).finally(() => sendResponse({ ok: true }));
    return true;
  }
});
