const loginPanel = document.querySelector("#login-panel");
const loginStatus = document.querySelector("#login-status");
const dashboard = document.querySelector("#dashboard");
const userSelect = document.querySelector("#user-select");
const openDashboard = document.querySelector("#open-dashboard");
const metricSelect = document.querySelector("#metric-select");
const chartContainer = document.querySelector("#chart-container");
const statusMessage = document.querySelector("#status");

const series = {
  weight_kg: {
    title: "Weight over time",
    subtitle: "Weekly weight readings; missing weigh-ins are left as gaps.",
    unit: "kg",
    color: "#82b5ff",
    digits: 1,
  },
  avg_daily_steps: {
    title: "Steps over time",
    subtitle: "Average steps per day with recorded step buckets.",
    unit: "steps",
    color: "#70d4a0",
    digits: 0,
  },
  avg_sleep_hours: {
    title: "Sleep over time",
    subtitle: "Average recorded sleep per night; awake intervals are excluded.",
    unit: "hours",
    color: "#c5a5ff",
    digits: 1,
  },
  workout_minutes: {
    title: "Workout time over time",
    subtitle: "Total workout minutes recorded in each week.",
    unit: "minutes",
    color: "#f2b56c",
    digits: 0,
  },
  avg_resting_bpm: {
    title: "Resting heart rate over time",
    subtitle: "Average recorded heart-rate samples marked as resting in each week.",
    unit: "bpm",
    color: "#ff8992",
    digits: 1,
  },
  weight_change_kg: {
    title: "Weight change over time",
    subtitle: "Change since the preceding recorded weigh-in, assigned to the later weigh-in week.",
    unit: "kg",
    color: "#82b5ff",
    digits: 2,
  },
};

let weeklyData = [];

function formatNumber(value, digits = 1) {
  if (value === null || value === undefined || !Number.isFinite(Number(value))) return "—";
  return Number(value).toLocaleString(undefined, { maximumFractionDigits: digits });
}

async function requestJson(url) {
  const response = await fetch(url);
  const payload = await response.json().catch(() => ({}));
  if (!response.ok) {
    const detail = payload.detail || `Request failed (${response.status})`;
    throw new Error(typeof detail === "string" ? detail : JSON.stringify(detail));
  }
  return payload;
}

function renderChart() {
  const key = metricSelect.value;
  const metric = series[key];
  const points = weeklyData
    .map((row, index) => ({ index, week: row.week_start, value: row[key] }))
    .filter((point) => point.value !== null && point.value !== undefined);
  document.querySelector("#chart-title").textContent = metric.title;
  document.querySelector("#chart-subtitle").textContent = metric.subtitle;
  chartContainer.replaceChildren();

  if (!points.length) {
    const empty = document.createElement("p");
    empty.className = "chart-empty";
    empty.textContent = "There are no recorded values for this metric in this profile.";
    chartContainer.appendChild(empty);
    return;
  }

  const ns = "http://www.w3.org/2000/svg";
  const svg = document.createElementNS(ns, "svg");
  svg.setAttribute("viewBox", "0 0 960 360");
  svg.setAttribute("role", "img");
  svg.setAttribute("aria-label", `${metric.title} line chart`);
  const title = document.createElementNS(ns, "title");
  title.textContent = metric.title;
  svg.appendChild(title);

  const left = 76;
  const right = 28;
  const top = 24;
  const bottom = 54;
  const width = 960 - left - right;
  const height = 360 - top - bottom;
  let minimum = Math.min(...points.map((point) => Number(point.value)));
  let maximum = Math.max(...points.map((point) => Number(point.value)));
  if (minimum === maximum) {
    const padding = Math.abs(minimum) * 0.05 || 1;
    minimum -= padding;
    maximum += padding;
  } else {
    const padding = (maximum - minimum) * 0.12;
    minimum -= padding;
    maximum += padding;
  }
  const x = (index) => left + (weeklyData.length <= 1 ? width / 2 : (index / (weeklyData.length - 1)) * width);
  const y = (value) => top + ((maximum - Number(value)) / (maximum - minimum)) * height;

  function addSvgElement(name, attributes, text) {
    const element = document.createElementNS(ns, name);
    for (const [attribute, value] of Object.entries(attributes)) {
      element.setAttribute(attribute, String(value));
    }
    if (text !== undefined) element.textContent = text;
    svg.appendChild(element);
    return element;
  }

  for (let tick = 0; tick <= 4; tick += 1) {
    const value = maximum - ((maximum - minimum) * tick) / 4;
    const tickY = top + (height * tick) / 4;
    addSvgElement("line", {
      x1: left, y1: tickY, x2: 960 - right, y2: tickY, class: "chart-grid",
    });
    addSvgElement("text", {
      x: left - 12, y: tickY + 4, class: "chart-axis-label", "text-anchor": "end",
    }, `${formatNumber(value, metric.digits)} ${metric.unit}`);
  }

  addSvgElement("line", {
    x1: left, y1: top + height, x2: 960 - right, y2: top + height, class: "chart-axis",
  });
  const labelCount = Math.min(6, weeklyData.length);
  for (let label = 0; label < labelCount; label += 1) {
    const index = labelCount <= 1
      ? 0
      : Math.round((label * (weeklyData.length - 1)) / (labelCount - 1));
    addSvgElement("text", {
      x: x(index), y: top + height + 27, class: "chart-axis-label", "text-anchor": "middle",
    }, weeklyData[index].week_start);
  }

  let segment = [];
  const drawSegment = () => {
    if (segment.length > 1) {
      addSvgElement("polyline", {
        points: segment.map((point) => `${x(point.index)},${y(point.value)}`).join(" "),
        class: "chart-line",
        stroke: metric.color,
      });
    }
    segment.forEach((point) => {
      const circle = addSvgElement("circle", {
        cx: x(point.index),
        cy: y(point.value),
        r: 4.5,
        class: "chart-point",
        fill: metric.color,
      });
      const tooltip = document.createElementNS(ns, "title");
      tooltip.textContent = `${point.week}: ${formatNumber(point.value, metric.digits)} ${metric.unit}`;
      circle.appendChild(tooltip);
    });
    segment = [];
  };
  weeklyData.forEach((row, index) => {
    if (row[key] === null || row[key] === undefined) {
      drawSegment();
    } else {
      segment.push({ index, week: row.week_start, value: row[key] });
    }
  });
  drawSegment();
  chartContainer.appendChild(svg);
}

function renderSummary(rows) {
  const weights = rows.filter((row) => row.weight_kg !== null);
  const latestWeight = weights.length ? weights[weights.length - 1].weight_kg : null;
  const firstWeight = weights.length ? weights[0].weight_kg : null;
  const weightChange = weights.length > 1 ? latestWeight - firstWeight : null;
  const stepValues = rows.filter((row) => row.avg_daily_steps !== null);
  const sleepValues = rows.filter((row) => row.avg_sleep_hours !== null);
  const average = (values, field) => values.length
    ? values.reduce((total, row) => total + row[field], 0) / values.length
    : null;

  document.querySelector("#latest-weight").textContent =
    latestWeight === null ? "—" : `${formatNumber(latestWeight)} kg`;
  document.querySelector("#weight-change").textContent =
    weightChange === null
      ? "—"
      : `${weightChange > 0 ? "+" : ""}${formatNumber(weightChange, 2)} kg`;
  document.querySelector("#average-steps").textContent =
    formatNumber(average(stepValues, "avg_daily_steps"), 0);
  const avgSleep = average(sleepValues, "avg_sleep_hours");
  document.querySelector("#average-sleep").textContent =
    avgSleep === null ? "—" : `${formatNumber(avgSleep)} h`;
  document.querySelector("#weekly-count").textContent =
    `${rows.length} weeks of computed history · weight change compares recorded measurements and may span multiple days.`;
}

function addAssociationRows(container, entries) {
  container.replaceChildren();
  for (const entry of entries) {
    const row = document.createElement("div");
    row.className = "association-row";
    const label = document.createElement("span");
    label.textContent = entry.label;
    const value = document.createElement("span");
    value.textContent = entry.correlation === null
      ? `Not enough variation · n=${entry.sample_count}`
      : `r = ${formatNumber(entry.correlation, 2)} · n=${entry.sample_count}`;
    row.append(label, value);
    container.appendChild(row);
  }
}

function renderPersonalAnalysis(analysis, prediction) {
  document.querySelector("#personal-guidance").textContent = analysis.guidance;
  addAssociationRows(
    document.querySelector("#personal-associations"),
    analysis.associations,
  );
  const predictionValue = document.querySelector("#prediction-value");
  const predictionDetails = document.querySelector("#prediction-details");
  if (prediction.status !== "ready") {
    predictionValue.textContent = "Not available";
    predictionDetails.textContent = prediction.reason;
    return;
  }
  const change = prediction.predicted_7_day_change_kg;
  predictionValue.textContent =
    `${change > 0 ? "+" : ""}${formatNumber(change, 3)} kg / 7 days`;
  predictionDetails.textContent =
    `Ridge regression trained on ${prediction.training_sample_count} weekly examples ` +
    `from ${prediction.training_profile_count} other profiles (excluding this profile). ` +
    `Latest input week: ${prediction.source_week}. This is a synthetic-data estimate, ` +
    "not a clinical forecast.";
}

function renderCohortAnalysis(data) {
  addAssociationRows(
    document.querySelector("#cohort-associations"),
    data.associations,
  );
  const evaluation = document.querySelector("#model-evaluation");
  if (data.model_evaluation.status !== "ready") {
    evaluation.textContent = data.model_evaluation.reason;
    return;
  }
  const model = data.model_evaluation;
  evaluation.textContent =
    `${model.algorithm}; ${model.validation}. ` +
    `Grouped validation on ${model.profile_count} held-out profiles ` +
    `(${model.sample_count} examples): MAE ${formatNumber(model.mae_kg_per_day, 4)} kg/day, ` +
    `R² ${formatNumber(model.r2, 2)}.`;
}

async function loadUsers() {
  try {
    const { users } = await requestJson("/api/insights/users");
    userSelect.replaceChildren();
    const placeholder = document.createElement("option");
    placeholder.value = "";
    placeholder.textContent = users.length ? "Choose a profile…" : "No profiles available";
    userSelect.appendChild(placeholder);
    for (const id of users) {
      const option = document.createElement("option");
      option.value = id;
      option.textContent = id;
      userSelect.appendChild(option);
    }
    userSelect.disabled = users.length === 0;
    loginStatus.textContent = users.length
      ? `${users.length.toLocaleString()} synthetic profiles are available.`
      : "No profiles are available in the computed dataset.";
    openDashboard.disabled = users.length === 0;
  } catch (error) {
    loginStatus.classList.add("error");
    loginStatus.textContent = error.message;
  }
}

async function showDashboard() {
  const userId = userSelect.value;
  if (!userId) {
    loginStatus.classList.add("error");
    loginStatus.textContent = "Choose a profile to continue.";
    return;
  }
  openDashboard.disabled = true;
  loginStatus.classList.remove("error");
  loginStatus.textContent = "Loading your dashboard…";
  try {
    const [data, cohort] = await Promise.all([
      requestJson(`/api/insights?user_id=${encodeURIComponent(userId)}`),
      requestJson("/api/insights/cohort"),
    ]);
    weeklyData = data.weekly;
    document.querySelector("#profile-title").textContent = `Your trends · ${data.user_id}`;
    renderSummary(weeklyData);
    renderChart();
    renderPersonalAnalysis(data.personal_analysis, data.weight_prediction);
    renderCohortAnalysis(cohort);
    loginPanel.hidden = true;
    dashboard.hidden = false;
    statusMessage.textContent =
      "Personal trends show only the selected profile. Population analysis uses aggregate synthetic data.";
  } catch (error) {
    loginStatus.classList.add("error");
    loginStatus.textContent = error.message;
  } finally {
    openDashboard.disabled = false;
  }
}

openDashboard.addEventListener("click", showDashboard);
userSelect.addEventListener("change", () => {
  openDashboard.disabled = !userSelect.value;
  loginStatus.classList.remove("error");
});
document.querySelector("#change-user").addEventListener("click", () => {
  dashboard.hidden = true;
  loginPanel.hidden = false;
});
metricSelect.addEventListener("change", renderChart);
loadUsers();
