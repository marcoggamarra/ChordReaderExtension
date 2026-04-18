const titleElement = document.getElementById("title");
const statusElement = document.getElementById("status");
const bpmElement = document.getElementById("bpm");
const chordElement = document.getElementById("chord");
const confidenceElement = document.getElementById("confidence");
const energyElement = document.getElementById("energy");
const startButton = document.getElementById("startButton");
const stopButton = document.getElementById("stopButton");
const vizCanvas = document.getElementById("viz");
const vizContext = vizCanvas.getContext("2d");

let activeTabId = null;

function setStatus(text) {
  statusElement.textContent = `Status: ${text}`;
}

function setButtons(isRunning) {
  startButton.disabled = isRunning;
  stopButton.disabled = !isRunning;
}

function drawBars(bars) {
  vizContext.clearRect(0, 0, vizCanvas.width, vizCanvas.height);

  if (!Array.isArray(bars) || bars.length === 0) {
    return;
  }

  const barWidth = vizCanvas.width / bars.length;

  bars.forEach((value, index) => {
    const clamped = Math.max(0, Math.min(1, value));
    const height = clamped * vizCanvas.height;
    const x = index * barWidth;
    const y = vizCanvas.height - height;

    vizContext.fillStyle = "#3b82f6";
    vizContext.fillRect(x + 1, y, Math.max(1, barWidth - 2), height);
  });
}

function updateMetrics(data) {
  bpmElement.textContent = data.bpm ? `${data.bpm.toFixed(1)} BPM` : "-";
  chordElement.textContent = data.chord || "-";
  confidenceElement.textContent = data.confidence != null ? `${Math.round(data.confidence * 100)}%` : "-";
  energyElement.textContent = data.energy != null ? data.energy.toFixed(3) : "-";
  drawBars(data.spectrum || []);
}

function getActiveYouTubeTab(callback) {
  chrome.tabs.query({ active: true, currentWindow: true }, (tabs) => {
    const activeTab = tabs[0];

    if (!activeTab || !activeTab.id || !activeTab.url) {
      callback({ error: "No active tab found." });
      return;
    }

    if (!activeTab.url.startsWith("https://www.youtube.com/watch")) {
      callback({ error: "Open a YouTube video page first." });
      return;
    }

    callback({ tab: activeTab });
  });
}

function loadVideoTitle(tabId) {
  chrome.scripting.executeScript(
    {
      target: { tabId },
      func: () => {
        const heading = document.querySelector("h1.ytd-watch-metadata yt-formatted-string")
          || document.querySelector("h1.title yt-formatted-string");

        return heading?.textContent?.trim() || document.title.replace(/\s*-\s*YouTube\s*$/, "").trim();
      }
    },
    (results) => {
      if (chrome.runtime.lastError) {
        titleElement.textContent = `Error: ${chrome.runtime.lastError.message}`;
        return;
      }

      const videoTitle = results && results[0] && results[0].result;
      titleElement.textContent = videoTitle || "Could not read video title.";
    }
  );
}

function requestState() {
  chrome.runtime.sendMessage({ type: "get-state" }, (response) => {
    if (chrome.runtime.lastError) {
      setStatus(`runtime error: ${chrome.runtime.lastError.message}`);
      return;
    }

    if (!response) {
      setStatus("background unavailable");
      return;
    }

    setStatus(response.isRunning ? "running" : "idle");
    setButtons(Boolean(response.isRunning));

    if (response.lastResult) {
      updateMetrics(response.lastResult);
    }
  });
}

startButton.addEventListener("click", () => {
  if (activeTabId == null) {
    setStatus("no active YouTube tab");
    return;
  }

  setStatus("starting");
  chrome.runtime.sendMessage({ type: "start-analysis", tabId: activeTabId }, (response) => {
    if (chrome.runtime.lastError) {
      setStatus(`start failed: ${chrome.runtime.lastError.message}`);
      return;
    }

    if (!response || !response.ok) {
      setStatus(response?.error || "could not start");
      return;
    }

    setStatus("running");
    setButtons(true);
  });
});

stopButton.addEventListener("click", () => {
  chrome.runtime.sendMessage({ type: "stop-analysis" }, () => {
    if (chrome.runtime.lastError) {
      setStatus(`stop failed: ${chrome.runtime.lastError.message}`);
      return;
    }

    setStatus("idle");
    setButtons(false);
  });
});

chrome.runtime.onMessage.addListener((message) => {
  if (message.type === "analysis-update") {
    updateMetrics(message.data);
    setStatus("running");
    setButtons(true);
  }

  if (message.type === "analysis-error") {
    setStatus(message.error || "analysis error");
    setButtons(false);
  }
});

getActiveYouTubeTab((result) => {
  if (result.error) {
    titleElement.textContent = result.error;
    setStatus("idle");
    setButtons(false);
    return;
  }

  activeTabId = result.tab.id;
  loadVideoTitle(activeTabId);
  requestState();
});
