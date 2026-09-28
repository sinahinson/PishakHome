let CURRENT_SETTINGS = null;

function showSection(name) {
  document.querySelectorAll(".settings-nav button").forEach(b => b.classList.toggle("active", b.dataset.section === name));
  document.querySelectorAll(".settings-section").forEach(s => s.classList.toggle("active", s.id === "section-" + name));
}

function setStatus(elId, message, ok) {
  const el = document.getElementById(elId);
  if (!el) return;
  el.textContent = message;
  el.className = "form-status " + (ok ? "ok" : "error");
  setTimeout(() => { el.textContent = ""; }, 4000);
}

async function loadSettings() {
  try {
    CURRENT_SETTINGS = await apiJSON("/api/settings");
    populateForms(CURRENT_SETTINGS);
  } catch (err) {
    console.error("Failed to load settings", err);
  }
}

function populateForms(s) {
  document.getElementById("sec-username").value = s.security.username || "";

  document.getElementById("cam-enabled").checked = !!s.camera.enabled;
  document.getElementById("cam-device").value = s.camera.device;
  document.getElementById("cam-width").value = s.camera.width;
  document.getElementById("cam-height").value = s.camera.height;
  document.getElementById("cam-fps").value = s.camera.fps;

  document.getElementById("mo-enabled").checked = !!s.motion.enabled;
  document.getElementById("mo-snapshot").checked = !!s.motion.snapshot_on_motion;
  document.getElementById("mo-interval").value = s.motion.poll_interval_seconds;
  document.getElementById("mo-pixel").value = s.motion.pixel_threshold;
  document.getElementById("mo-area").value = s.motion.area_threshold;
  document.getElementById("mo-cooldown").value = s.motion.event_cooldown_seconds;

  document.getElementById("rec-mode").value = s.recording.mode;
  document.getElementById("rec-codec").value = s.recording.codec;
  document.getElementById("rec-width").value = s.recording.width;
  document.getElementById("rec-height").value = s.recording.height;
  document.getElementById("rec-fps").value = s.recording.fps;
  document.getElementById("rec-bitrate").value = s.recording.bitrate_kbps;
  document.getElementById("rec-segment").value = s.recording.segment_minutes;
  document.getElementById("rec-retention").value = s.recording.retention_days;
  document.getElementById("rec-manual").value = s.recording.manual_clip_seconds;
  document.getElementById("rec-post-event").value = s.recording.post_event_seconds;
  document.getElementById("rec-max-event").value = s.recording.max_event_seconds;

  document.getElementById("tg-enabled").checked = !!s.telegram.enabled;
  document.getElementById("tg-send-photo").checked = !!s.telegram.send_snapshot_on_motion;
  document.getElementById("tg-chatids").value = (s.telegram.authorized_chat_ids || []).join(", ");
  document.getElementById("tg-proxy-enabled").checked = !!s.telegram.proxy_enabled;
  document.getElementById("tg-proxy-url").value = s.telegram.proxy_url || "";

  document.getElementById("gen-title").value = s.general.site_title || "";
  document.getElementById("gen-low-space").value = s.storage.low_space_warning_mb;
}

async function saveGeneral() {
  try {
    const general = await apiJSON("/api/settings/general", {
      method: "POST",
      body: { site_title: document.getElementById("gen-title").value.trim() },
    });
    const storage = await apiJSON("/api/settings/storage", {
      method: "POST",
      body: { low_space_warning_mb: Number(document.getElementById("gen-low-space").value) },
    });
    CURRENT_SETTINGS.general = general;
    CURRENT_SETTINGS.storage = storage;
    setStatus("general-status-msg", "Saved", true);
  } catch (err) {
    setStatus("general-status-msg", err.message, false);
  }
}

async function saveUsername() {
  try {
    await apiJSON("/api/settings/security/username", {
      method: "POST",
      body: { username: document.getElementById("sec-username").value.trim() },
    });
    setStatus("sec-username-status", "Saved", true);
  } catch (err) {
    setStatus("sec-username-status", err.message, false);
  }
}

async function savePassword() {
  const current = document.getElementById("pw-current").value;
  const next = document.getElementById("pw-new").value;
  const confirm = document.getElementById("pw-confirm").value;
  if (next !== confirm) { setStatus("pw-status", "Passwords do not match", false); return; }
  try {
    await apiJSON("/api/settings/security/password", {
      method: "POST",
      body: { current_password: current, new_password: next },
    });
    setStatus("pw-status", "Password changed", true);
    document.getElementById("pw-current").value = "";
    document.getElementById("pw-new").value = "";
    document.getElementById("pw-confirm").value = "";
  } catch (err) {
    setStatus("pw-status", err.message, false);
  }
}

const FIELD_IDS = {
  camera: { enabled: "cam-enabled", device: "cam-device", width: "cam-width", height: "cam-height", fps: "cam-fps" },
  motion: {
    enabled: "mo-enabled", snapshot_on_motion: "mo-snapshot",
    poll_interval_seconds: "mo-interval", pixel_threshold: "mo-pixel", area_threshold: "mo-area",
  },
};

function readField(id, isNumber, isCheckbox) {
  const el = document.getElementById(id);
  if (isCheckbox) return el.checked;
  if (isNumber) return Number(el.value);
  return el.value;
}

async function saveSection(section, keys) {
  const map = FIELD_IDS[section];
  const payload = {};
  for (const key of keys) {
    const id = map[key];
    const el = document.getElementById(id);
    if (el.type === "checkbox") payload[key] = el.checked;
    else if (el.type === "number") payload[key] = Number(el.value);
    else payload[key] = el.value;
  }
  try {
    const updated = await apiJSON(`/api/settings/${section}`, { method: "POST", body: payload });
    CURRENT_SETTINGS[section] = updated;
    setStatus(`${section}-status-msg`, "Saved", true);
  } catch (err) {
    setStatus(`${section}-status-msg`, err.message, false);
  }
}

async function saveCooldown() {
  try {
    const updated = await apiJSON("/api/settings/motion", {
      method: "POST",
      body: { event_cooldown_seconds: Number(document.getElementById("mo-cooldown").value) },
    });
    CURRENT_SETTINGS.motion = updated;
    setStatus("motion-status-msg", "Saved", true);
  } catch (err) {
    setStatus("motion-status-msg", err.message, false);
  }
}

async function saveRecording() {
  const payload = {
    mode: document.getElementById("rec-mode").value,
    codec: document.getElementById("rec-codec").value,
    width: Number(document.getElementById("rec-width").value),
    height: Number(document.getElementById("rec-height").value),
    fps: Number(document.getElementById("rec-fps").value),
    bitrate_kbps: Number(document.getElementById("rec-bitrate").value),
    segment_minutes: Number(document.getElementById("rec-segment").value),
    retention_days: Number(document.getElementById("rec-retention").value),
    manual_clip_seconds: Number(document.getElementById("rec-manual").value),
    post_event_seconds: Number(document.getElementById("rec-post-event").value),
    max_event_seconds: Number(document.getElementById("rec-max-event").value),
  };
  try {
    const updated = await apiJSON("/api/settings/recording", { method: "POST", body: payload });
    CURRENT_SETTINGS.recording = updated;
    setStatus("recording-status-msg", "Saved", true);
  } catch (err) {
    setStatus("recording-status-msg", err.message, false);
  }
}

async function saveTelegram() {
  const payload = {
    enabled: document.getElementById("tg-enabled").checked,
    send_snapshot_on_motion: document.getElementById("tg-send-photo").checked,
    authorized_chat_ids: document.getElementById("tg-chatids").value
      .split(",").map(s => s.trim()).filter(Boolean).map(Number),
    proxy_enabled: document.getElementById("tg-proxy-enabled").checked,
    proxy_url: document.getElementById("tg-proxy-url").value.trim(),
  };
  const token = document.getElementById("tg-token").value.trim();
  if (token) payload.bot_token = token;
  try {
    const updated = await apiJSON("/api/settings/telegram", { method: "POST", body: payload });
    CURRENT_SETTINGS.telegram = updated;
    document.getElementById("tg-token").value = "";
    setStatus("telegram-status-msg", "Saved", true);
  } catch (err) {
    setStatus("telegram-status-msg", err.message, false);
  }
}

async function testTelegram() {
  try {
    const result = await apiJSON("/api/telegram/test", { method: "POST" });
    setStatus("telegram-status-msg", result.message, result.ok);
  } catch (err) {
    setStatus("telegram-status-msg", err.message, false);
  }
}

async function resetAllSettings() {
  const password = document.getElementById("reset-password").value;
  if (!password) { setStatus("reset-status-msg", "Enter your current password first", false); return; }
  if (!confirm("Reset ALL settings and your login to factory defaults?")) return;
  try {
    await apiJSON("/api/settings/reset", {
      method: "POST",
      body: { current_password: password },
    });
    setStatus("reset-status-msg", "Everything reset — reloading…", true);
    setTimeout(() => window.location.reload(), 1200);
  } catch (err) {
    setStatus("reset-status-msg", err.message, false);
  }
}

loadSettings();
