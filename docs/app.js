// Destination = the app's DID (config.telnyx_phone_number).
const DESTINATION = "+13464720939";

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
    client = new TelnyxWebRTC.TelnyxRTC({
      login_token: data.token,
      env: "production",
    });
    client.remoteElement = "remoteAudio";

    try {
      const mic = client.enableMicrophone();
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
      currentCall = client.newCall({
        destinationNumber: DESTINATION,
        callerNumber: DESTINATION,
        audio: true,
      });
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

    client.connect();
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

$("callBtn").addEventListener("click", placeCall);
$("hangupBtn").addEventListener("click", hangup);
window.addEventListener("beforeunload", teardown);
