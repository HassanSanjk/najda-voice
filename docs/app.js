// Destination = the app's DID (config.telnyx_phone_number).
const DESTINATION = "+13464720939";

const q = new URLSearchParams(location.search);
const num = (v) => {
  const n = parseInt(v, 10);
  return Number.isFinite(n) ? n : null;
};
const KNOBS = {
  debug: q.has("debug"),
  noReports: q.has("noReports"),
  noReconnect: q.has("noReconnect"),
  maxReconnect: num(q.get("maxReconnect")),
  delayBeforeHangup: num(q.get("delayBeforeHangup")),
  callDebug: q.has("callDebug"),
  debugOutput: q.get("debugOutput") || null,
};

function logTrial(event, extra) {
  console.log(
    "[trial]",
    event,
    JSON.stringify({ ts: new Date().toISOString(), cfg: KNOBS, ...extra })
  );
}

function renderTrialCfg() {
  const el = $("trialCfg");
  if (!el) return;
  const parts = [];
  for (const [k, v] of Object.entries(KNOBS)) {
    if (v) parts.push(k + "=" + v);
  }
  el.textContent = parts.length
    ? "trial config: " + parts.join(" ")
    : "trial config: baseline";
  el.classList.remove("hidden");
}

function timeIt(label, fn) {
  if (!KNOBS.debug) return fn();
  console.time(label);
  try {
    return fn();
  } finally {
    console.timeEnd(label);
  }
}

let audioActive = false;
let autoHangupTimer = null;

function initAudioWatcher() {
  const el = $("remoteAudio");
  if (!el) return;
  const onPlay = () => {
    audioActive = true;
    if (KNOBS.debug) logTrial("audio-play");
  };
  const onIdle = () => {
    audioActive = false;
    if (KNOBS.debug) logTrial("audio-idle");
  };
  el.addEventListener("play", onPlay);
  el.addEventListener("playing", onPlay);
  el.addEventListener("pause", onIdle);
  el.addEventListener("ended", onIdle);
  el.addEventListener("emptied", onIdle);
}

function scheduleAutoHangup() {
  if (KNOBS.delayBeforeHangup == null) return;
  if (autoHangupTimer) clearTimeout(autoHangupTimer);
  autoHangupTimer = setTimeout(() => {
    autoHangupTimer = null;
    logTrial("auto-hangup-firing", { delay: KNOBS.delayBeforeHangup });
    hangup();
  }, KNOBS.delayBeforeHangup);
  logTrial("auto-hangup-scheduled", { delay: KNOBS.delayBeforeHangup });
}

function initDebugInstrumentation() {
  if (!KNOBS.debug) return;
  try {
    const obs = new PerformanceObserver((list) => {
      for (const e of list.getEntries()) {
        const att = e.attribution && e.attribution[0];
        console.warn(
          "[longtask]",
          Math.round(e.duration) + "ms",
          att
            ? att.containerName || att.containerSrc || "unknown-script"
            : "unknown-script"
        );
      }
    });
    obs.observe({ entryTypes: ["longtask"] });
  } catch (err) {
    console.warn("[longtask] observer unavailable", err);
  }
}

// Same-origin when served by the app at /demo; otherwise the local
// backend (browsers treat http://localhost as trustworthy from https).
const sameOrigin =
  location.protocol === "http:" &&
  ["localhost", "127.0.0.1"].includes(location.hostname);
const BACKEND_URL = sameOrigin ? "" : "http://localhost:8000";
document.getElementById("backend").textContent =
  BACKEND_URL || location.origin;

let client = null;
let currentCall = null;
let pollTimer = null;
let renderedTurns = 0;
let callSid = null;
let ending = false;
let endTimer = null;

const $ = (id) => document.getElementById(id);

function describeError(err) {
  if (!err) return "unknown error";
  if (err.message) return err.message;
  try {
    const json = JSON.stringify(err);
    return json !== undefined && json !== "{}" ? json : String(err);
  } catch (e) {
    return String(err);
  }
}

function setStatus(text) {
  $("status").textContent = text;
  $("dot").classList.toggle("active", text !== "Idle");
}

async function placeCall() {
  teardown();
  $("callBtn").disabled = true;
  setStatus("Fetching token…");
  try {
    const res = await fetch(BACKEND_URL + "/telnyx-token");
    const data = await res.json();
    if (!data.token) throw new Error(data.error || "no token returned");

    setStatus("Connecting…");
    client = timeIt("sdk-new-telnyxrtc", () =>
      new TelnyxWebRTC.TelnyxRTC({
        login_token: data.token,
        env: "production",
        ...(KNOBS.noReports ? { enableCallReports: false } : {}),
        ...(KNOBS.noReconnect ? { autoReconnect: false } : {}),
        ...(KNOBS.maxReconnect != null
          ? { maxReconnectAttempts: KNOBS.maxReconnect }
          : {}),
      })
    );
    client.remoteElement = "remoteAudio";

    try {
      const mic = timeIt("sdk-enable-microphone", () =>
        client.enableMicrophone()
      );
      if (mic && typeof mic.catch === "function") {
        mic.catch((micErr) => {
          console.error("enableMicrophone", micErr);
          setStatus("Microphone unavailable: " + describeError(micErr));
          teardown();
        });
      }
    } catch (micErr) {
      console.error("enableMicrophone", micErr);
      setStatus("Microphone unavailable: " + describeError(micErr));
      teardown();
      return;
    }

    client.on("telnyx.ready", () => {
      setStatus("Dialing…");
      currentCall = timeIt("sdk-new-call", () =>
        client.newCall({
          destinationNumber: DESTINATION,
          callerNumber: DESTINATION,
          audio: true,
          ...(KNOBS.callDebug ? { debug: true } : {}),
          ...(KNOBS.debugOutput ? { debugOutput: KNOBS.debugOutput } : {}),
        })
      );
    });

    client.on("telnyx.notification", (notification) => {
      if (notification.type === "callUpdate") {
        handleCallUpdate(notification.call);
      }
    });

    client.on("telnyx.error", (error) => {
      console.error("telnyx.error", error);
      setStatus("Error: " + describeError(error));
      teardown();
    });

    client.on("telnyx.socket.close", () => {
      console.warn("telnyx.socket.close");
      endCall();
    });

    timeIt("sdk-connect", () => client.connect());
  } catch (err) {
    console.error("placeCall", err);
    setStatus("Call failed: " + describeError(err));
    $("callBtn").disabled = false;
  }
}

function handleCallUpdate(call) {
  currentCall = call;
  switch (call.state) {
    case "trying":
    case "ringing":
      setStatus("Ringing…");
      break;
    case "active":
      setStatus("In call");
      $("hangupBtn").classList.remove("hidden");
      startPolling();
      scheduleAutoHangup();
      break;
    case "hangup":
    case "destroy":
      endCall();
      break;
  }
}

function hangup() {
  endCall();
}

function endCall() {
  const el = $("remoteAudio");
  const audioActiveAtHangup = !!(el && !el.paused && !el.ended);
  logTrial("hangup", {
    audioActiveAtHangup,
    audioActiveFlag: audioActive,
    hadCall: !!currentCall,
  });
  if (ending) return;
  ending = true;

  setStatus("Ending call…");
  $("hangupBtn").classList.add("hidden");
  $("callBtn").disabled = false;

  if (currentCall) {
    try {
      currentCall.hangup();
    } catch (e) {
      console.error("hangup", e);
    }
  }
  if (client) {
    client.off("telnyx.error");
    client.off("telnyx.ready");
    client.off("telnyx.notification");
    client.off("telnyx.socket.close");
    try {
      client.disconnect();
    } catch (e) {
      console.error("disconnect", e);
    }
    client = null;
  }
  currentCall = null;

  endTimer = setTimeout(() => {
    stopPolling();
    setStatus("Idle");
    ending = false;
    endTimer = null;
  }, 3000);
}

function teardown() {
  stopPolling();
  if (endTimer) {
    clearTimeout(endTimer);
    endTimer = null;
  }
  if (autoHangupTimer) {
    clearTimeout(autoHangupTimer);
    autoHangupTimer = null;
  }
  if (client) {
    client.off("telnyx.error");
    client.off("telnyx.ready");
    client.off("telnyx.notification");
    client.off("telnyx.socket.close");
    client.disconnect();
    client = null;
  }
  currentCall = null;
  ending = false;
  $("hangupBtn").classList.add("hidden");
  $("callBtn").disabled = false;
}

async function startPolling() {
  stopPolling();
  callSid = null;
  try {
    const res = await fetch(BACKEND_URL + "/active-calls");
    const data = await res.json();
    callSid = data.newest || null;
  } catch (e) {
    callSid = null;
  }
  if (!callSid) return;
  renderedTurns = 0;
  pollTimer = setInterval(pollTranscript, 1000);
}

async function pollTranscript() {
  if (!callSid) return;
  try {
    const res = await fetch(BACKEND_URL + "/transcript/" + callSid);
    if (!res.ok) {
      stopPolling();
      return;
    }
    const data = await res.json();
    if (data.scenario) {
      $("scenario").textContent = "Scenario: " + data.scenario;
      $("scenario").classList.remove("hidden");
    }
    const turns = data.turns || [];
    while (renderedTurns < turns.length) {
      appendTurn(turns[renderedTurns]);
      renderedTurns += 1;
    }
  } catch (e) {
    stopPolling();
  }
}

function appendTurn(turn) {
  const row = document.createElement("div");
  row.className = "turn " + (turn.role === "assistant" ? "assistant" : "user");
  const label = document.createElement("div");
  label.className = "label";
  label.textContent = turn.role === "assistant" ? "Najda" : "Caller";
  const body = document.createElement("div");
  body.className = "body";
  body.textContent = turn.content;
  row.appendChild(label);
  row.appendChild(body);
  $("transcript").appendChild(row);
  $("transcript").scrollTop = $("transcript").scrollHeight;
}

function stopPolling() {
  if (pollTimer) {
    clearInterval(pollTimer);
    pollTimer = null;
  }
}

initAudioWatcher();
renderTrialCfg();
initDebugInstrumentation();
logTrial("page-load");
$("callBtn").addEventListener("click", placeCall);
$("hangupBtn").addEventListener("click", hangup);
window.addEventListener("beforeunload", teardown);
