const userSelect = document.querySelector("#user-select");
const streamStatus = document.querySelector("#stream-status");
const settingsStatus = document.querySelector("#settings-status");
const alarmBanner = document.querySelector("#alarm-banner");
const chartContainer = document.querySelector("#chart");
const alarmEventsContainer = document.querySelector("#alarm-events");
const CHART_WINDOW_POINTS = 60;
let eventSource;
let lastSampleReceivedAt = null;

async function requestJson(url, options) {
  const response = await fetch(url, options);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `Request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function formatTime(value) {
  if (!value) return "No reading yet";
  return new Date(value).toLocaleTimeString();
}

function setStreamStatus(text, state = "") {
  streamStatus.textContent = text;
  streamStatus.className = `stream-status ${state}`;
}

function renderAlarm(snapshot) {
  const alarm = snapshot.alarm;
  if (!alarm) {
    alarmBanner.hidden = true;
    return;
  }
  const setting = alarm.direction === "low"
    ? `below ${snapshot.settings.low_bpm} bpm`
    : `above ${snapshot.settings.high_bpm} bpm`;
  alarmBanner.hidden = false;
  alarmBanner.className = `alarm-banner${alarm.triggered ? "" : " pending"}`;
  document.querySelector("#alarm-title").textContent = alarm.triggered
    ? `Sustained ${alarm.direction === "low" ? "low" : "high"} heart-rate alert`
    : `Heart rate ${setting}`;
  document.querySelector("#alarm-message").textContent = alarm.triggered
    ? `${alarm.consecutive_samples} consecutive readings were ${setting}.`
    : `${alarm.consecutive_samples} of ${snapshot.settings.alert_after_samples} consecutive readings ${setting}; alert in ${alarm.samples_until_trigger} readings if the threshold remains crossed.`;
}

function renderChart(snapshot) {
  chartContainer.replaceChildren();
  const readings = (snapshot.recent || []).slice(-CHART_WINDOW_POINTS);
  if (!readings.length) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = "Waiting for live readings…";
    chartContainer.appendChild(empty);
    return;
  }
  const namespace = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(namespace, "svg");
  svg.setAttribute("viewBox", "0 0 800 220");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", "Recent heart-rate line chart");
  const left = 44;
  const right = 18;
  const top = 14;
  const bottom = 25;
  const plotWidth = 800 - left - right;
  const plotHeight = 220 - top - bottom;
  const settings = snapshot.settings;
  const values = readings.map((reading) => reading.bpm);
  const minimum = Math.max(30, Math.min(...values, settings.low_bpm) - 8);
  const maximum = Math.min(220, Math.max(...values, settings.high_bpm) + 8);
  const y = (value) => top + ((maximum - value) / (maximum - minimum)) * plotHeight;
  const x = (index) => left + (readings.length <= 1
    ? plotWidth
    : (index / (readings.length - 1)) * plotWidth);
  const add = (name, attributes, text) => {
    const element = document.createElementNS(namespace, name);
    for (const [key, value] of Object.entries(attributes)) {
      element.setAttribute(key, String(value));
    }
    if (text !== undefined) element.textContent = text;
    svg.appendChild(element);
    return element;
  };
  for (let tick = 0; tick <= 4; tick += 1) {
    const value = maximum - ((maximum - minimum) * tick) / 4;
    const tickY = top + (plotHeight * tick) / 4;
    add("line", { x1: left, y1: tickY, x2: 800 - right, y2: tickY, class: "chart-grid" });
    add("text", { x: left - 8, y: tickY + 4, class: "chart-label", "text-anchor": "end" }, `${Math.round(value)}`);
  }
  add("line", {
    x1: left, y1: y(settings.low_bpm), x2: 800 - right, y2: y(settings.low_bpm),
    class: "chart-limit", stroke: "#f2b56c",
  });
  add("line", {
    x1: left, y1: y(settings.high_bpm), x2: 800 - right, y2: y(settings.high_bpm),
    class: "chart-limit", stroke: "#ff777f",
  });
  add("polyline", {
    points: readings.map((reading, index) => `${x(index)},${y(reading.bpm)}`).join(" "),
    class: "chart-line",
  });
  for (const [index, reading] of [
    [0, readings[0]],
    [readings.length - 1, readings[readings.length - 1]],
  ]) {
    const circle = add("circle", {
      cx: x(index), cy: y(reading.bpm), r: 4, fill: "#82b5ff",
    });
    const title = document.createElementNS(namespace, "title");
    title.textContent = `${formatTime(reading.observed_at)} · ${reading.bpm} bpm`;
    circle.appendChild(title);
  }
  chartContainer.appendChild(svg);
}

function renderBaseline(baseline, latest) {
  const summary = document.querySelector("#baseline-summary");
  const context = document.querySelector("#personal-context");
  if (!baseline) {
    summary.textContent = "Not enough resting history yet to calculate a personal baseline.";
    context.textContent = "Keep the synthetic monitor running to build a descriptive seven-day range.";
    return;
  }
  summary.textContent =
    `Average ${baseline.average_bpm.toFixed(1)} bpm · median ${baseline.median_bpm.toFixed(1)} bpm · ` +
    `${baseline.sample_minutes.toLocaleString()} minute averages`;
  context.textContent =
    `Typical observed resting range: ${baseline.typical_low_bpm.toFixed(1)}–` +
    `${baseline.typical_high_bpm.toFixed(1)} bpm (5th–95th percentile).`;
  if (latest && latest.activity === "rest" &&
      (latest.bpm < baseline.typical_low_bpm || latest.bpm > baseline.typical_high_bpm)) {
    context.textContent += " The latest resting reading is outside this profile’s recent typical band.";
  }
}

function renderAlarmEvents(events) {
  alarmEventsContainer.replaceChildren();
  if (!events.length) {
    const empty = document.createElement("p");
    empty.className = "muted";
    empty.textContent = "No alarm events recorded.";
    alarmEventsContainer.appendChild(empty);
    return;
  }
  for (const event of events) {
    const row = document.createElement("div");
    row.className = `alarm-event ${event.event_type}`;
    const time = document.createElement("time");
    time.dateTime = event.observed_at;
    time.textContent = formatTime(event.observed_at);
    const message = document.createElement("span");
    message.textContent =
      `${event.event_type === "triggered" ? "ALERT" : "RECOVERED"} · ${event.message}`;
    row.append(time, message);
    alarmEventsContainer.appendChild(row);
  }
}

function renderSnapshot(snapshot) {
  const latest = snapshot.latest;
  lastSampleReceivedAt = latest ? new Date(latest.observed_at).getTime() : null;
  document.querySelector("#current-bpm").textContent = latest ? latest.bpm : "—";
  const latestAge = latest ? Date.now() - new Date(latest.observed_at).getTime() : Infinity;
  setStreamStatus(
    latestAge <= 3000 ? "Live · 1 Hz" : latest ? "No recent sample" : "Waiting for stream",
    latestAge <= 3000 ? "connected" : "stale",
  );
  const settingsForm = document.querySelector("#settings-form");
  if (!settingsForm.contains(document.activeElement)) {
    document.querySelector("#low-bpm").value = snapshot.settings.low_bpm;
    document.querySelector("#high-bpm").value = snapshot.settings.high_bpm;
    document.querySelector("#alert-after").value = snapshot.settings.alert_after_samples;
  }
  renderAlarm(snapshot);
  renderChart(snapshot);
  renderBaseline(snapshot.personal_baseline, latest);
  renderAlarmEvents(snapshot.alarm_events);
}

async function openMonitor() {
  if (eventSource) eventSource.close();
  const userId = userSelect.value;
  if (!userId) return;
  try {
    const initial = await requestJson(`/api/monitor?user_id=${encodeURIComponent(userId)}`);
    renderSnapshot(initial);
    eventSource = new EventSource(`/api/monitor/stream?user_id=${encodeURIComponent(userId)}`);
    eventSource.onopen = () => setStreamStatus("Connected · waiting for update", "connected");
    eventSource.onmessage = (event) => {
      try {
        renderSnapshot(JSON.parse(event.data));
      } catch (error) {
        setStreamStatus(`Invalid stream update: ${error.message}`, "disconnected");
      }
    };
    eventSource.onerror = () => setStreamStatus("Reconnecting to stream…", "disconnected");
  } catch (error) {
    setStreamStatus(error.message, "disconnected");
  }
}

async function loadUsers() {
  try {
    const { users } = await requestJson("/api/monitor/users");
    userSelect.replaceChildren();
    for (const user of users) {
      const option = document.createElement("option");
      option.value = user;
      option.textContent = user;
      userSelect.appendChild(option);
    }
    userSelect.disabled = users.length === 0;
    if (!users.length) {
      setStreamStatus("No monitor profiles configured", "disconnected");
      return;
    }
    userSelect.value = users[0];
    await openMonitor();
  } catch (error) {
    setStreamStatus(error.message, "disconnected");
  }
}

document.querySelector("#settings-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  settingsStatus.classList.remove("error");
  settingsStatus.textContent = "Saving…";
  const body = {
    user_id: userSelect.value,
    low_bpm: Number(document.querySelector("#low-bpm").value),
    high_bpm: Number(document.querySelector("#high-bpm").value),
    alert_after_samples: Number(document.querySelector("#alert-after").value),
  };
  try {
    await requestJson("/api/monitor/settings", {
      method: "PUT",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    settingsStatus.textContent = "Settings saved.";
    renderSnapshot(await requestJson(`/api/monitor?user_id=${encodeURIComponent(userSelect.value)}`));
  } catch (error) {
    settingsStatus.classList.add("error");
    settingsStatus.textContent = error.message;
  }
});

userSelect.addEventListener("change", openMonitor);
window.setInterval(() => {
  if (lastSampleReceivedAt && Date.now() - lastSampleReceivedAt > 3000) {
    setStreamStatus("No recent sample", "stale");
  }
}, 1000);
loadUsers();
