let mediaStream = null;
let audioContext = null;
let sourceNode = null;
let analyserNode = null;
let processorNode = null;
let muteGainNode = null;

let sampleBuffer = [];
let lastSent = 0;

const TARGET_SAMPLES = 22050;

function stopCapture() {
  if (processorNode) {
    processorNode.disconnect();
    processorNode.onaudioprocess = null;
    processorNode = null;
  }

  if (muteGainNode) {
    muteGainNode.disconnect();
    muteGainNode = null;
  }

  if (analyserNode) {
    analyserNode.disconnect();
    analyserNode = null;
  }

  if (sourceNode) {
    sourceNode.disconnect();
    sourceNode = null;
  }

  if (mediaStream) {
    mediaStream.getTracks().forEach((track) => track.stop());
    mediaStream = null;
  }

  if (audioContext) {
    audioContext.close();
    audioContext = null;
  }

  sampleBuffer = [];
  lastSent = 0;
}

function downsample(array, limit) {
  if (array.length <= limit) {
    return array;
  }

  const output = [];
  const step = array.length / limit;

  for (let i = 0; i < limit; i += 1) {
    output.push(array[Math.floor(i * step)]);
  }

  return output;
}

function computeEnergy(samples) {
  if (!samples.length) {
    return 0;
  }

  let sum = 0;
  for (let i = 0; i < samples.length; i += 1) {
    sum += samples[i] * samples[i];
  }

  return Math.sqrt(sum / samples.length);
}

function spectrumBars(analyser) {
  const bins = new Uint8Array(analyser.frequencyBinCount);
  analyser.getByteFrequencyData(bins);

  const bars = [];
  const size = Math.floor(bins.length / 16);

  for (let i = 0; i < 16; i += 1) {
    const start = i * size;
    const end = Math.min(bins.length, start + size);
    let total = 0;

    for (let j = start; j < end; j += 1) {
      total += bins[j];
    }

    const average = end > start ? total / (end - start) : 0;
    bars.push(average / 255);
  }

  return bars;
}

async function startCapture(streamId) {
  stopCapture();

  mediaStream = await navigator.mediaDevices.getUserMedia({
    audio: {
      mandatory: {
        chromeMediaSource: "tab",
        chromeMediaSourceId: streamId
      }
    },
    video: false
  });

  audioContext = new AudioContext();
  sourceNode = audioContext.createMediaStreamSource(mediaStream);
  analyserNode = audioContext.createAnalyser();
  analyserNode.fftSize = 512;

  processorNode = audioContext.createScriptProcessor(4096, 1, 1);
  muteGainNode = audioContext.createGain();
  muteGainNode.gain.value = 0;

  // Pass-through: keep tab audio audible while capturing
  sourceNode.connect(audioContext.destination);

  // Analysis chain (muted output so ScriptProcessor fires without doubling audio)
  sourceNode.connect(analyserNode);
  analyserNode.connect(processorNode);
  processorNode.connect(muteGainNode);
  muteGainNode.connect(audioContext.destination);

  processorNode.onaudioprocess = (event) => {
    const input = event.inputBuffer.getChannelData(0);

    for (let i = 0; i < input.length; i += 1) {
      sampleBuffer.push(input[i]);
    }

    if (sampleBuffer.length < TARGET_SAMPLES) {
      return;
    }

    const now = Date.now();
    if (now - lastSent < 200) {
      return;
    }

    const current = sampleBuffer.slice(-TARGET_SAMPLES);
    sampleBuffer = sampleBuffer.slice(-TARGET_SAMPLES * 2);
    lastSent = now;

    chrome.runtime.sendMessage({
      type: "offscreen-audio-chunk",
      sampleRate: audioContext.sampleRate,
      samples: Array.from(current),
      energy: computeEnergy(current),
      spectrum: spectrumBars(analyserNode),
      timestampMs: now
    });
  };
}

chrome.runtime.onMessage.addListener((message, _sender, sendResponse) => {
  if (message.type === "offscreen-start-capture") {
    startCapture(message.streamId)
      .then(() => sendResponse({ ok: true }))
      .catch((error) => sendResponse({ ok: false, error: error.message }));
    return true;
  }

  if (message.type === "offscreen-stop-capture") {
    stopCapture();
    sendResponse({ ok: true });
  }
});
