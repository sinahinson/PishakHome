async function refreshStatus() {
  try {
    const data = await apiJSON("/api/status");
    const sys = data.system, cam = data.camera, rec = data.recording;

    const sysEl = document.getElementById("system-status");
    if (sysEl) {
      sysEl.innerHTML = `
        <div>CPU: <b>${sys.cpu_percent}%</b></div>
        <div>RAM: <b>${sys.ram_used_mb} / ${sys.ram_total_mb} MB</b> (${sys.ram_percent}%)</div>
        <div>Disk free: <b>${sys.disk_free_mb} MB</b> / ${sys.disk_total_mb} MB</div>
        <div>Uptime: <b>${fmtDuration(sys.uptime_seconds)}</b></div>
      `;
    }

    const camEl = document.getElementById("camera-status");
    if (camEl) {
      camEl.innerHTML = `
        <div>Available: <b>${cam.available ? "yes" : "no"}</b></div>
        <div>Source: <b>${cam.source}</b></div>
        <div>Resolution: <b>${cam.width}x${cam.height}@${cam.fps}</b></div>
        <div>Last frame: <b>${cam.last_frame_age_seconds ?? "-"}s ago</b></div>
        ${cam.error ? `<div class="err">Error: ${cam.error}</div>` : ""}
        ${cam.recent_log ? `<div class="log">${cam.recent_log}</div>` : ""}
      `;
    }

    updateRecordingBadge(rec);
  } catch (err) {
    console.error("status refresh failed", err);
  }
}

function updateRecordingBadge(rec) {
  const badge = document.getElementById("recording-badge");
  const text = document.getElementById("recording-badge-text");
  const continuousBtn = document.getElementById("continuous-btn");
  if (!badge || !text) return;

  badge.classList.remove("active", "recording");
  if (rec.active) {
    badge.classList.add("recording");
    text.textContent = `${rec.mode} — ${fmtDuration(rec.elapsed_seconds)}`;
  } else {
    text.textContent = "Off";
  }

  if (continuousBtn) {
    if (rec.mode === "continuous" && rec.active) {
      continuousBtn.textContent = "⏹ Stop 24/7 recording";
      continuousBtn.classList.add("danger");
    } else {
      continuousBtn.textContent = "▶ Start 24/7 recording";
      continuousBtn.classList.remove("danger");
    }
    continuousBtn.disabled = rec.active && rec.mode !== "continuous";
  }

  const manualBtn = document.getElementById("manual-record-btn");
  if (manualBtn) manualBtn.disabled = rec.active;
}

async function refreshNetwork() {
  try {
    const data = await apiJSON("/api/network");
    const el = document.getElementById("network-status");
    if (!el) return;
    const withIp = data.interfaces.filter(i => i.ipv4 || i.is_up).slice(0, 4);
    if (!withIp.length) { el.innerHTML = "<div class='muted'>No active interfaces.</div>"; return; }
    el.innerHTML = withIp.map(i => `
      <div>
        <b>${i.name}</b> ${i.is_up ? "🟢" : "⚪"} ${i.ipv4 || ""}
        <div class="muted" style="font-size:0.82rem;">↓ ${i.rx_kbps} kbps · ↑ ${i.tx_kbps} kbps${i.speed_mbps ? " · " + i.speed_mbps + " Mbps link" : ""}</div>
      </div>
    `).join("<hr style='border-color: var(--border); margin: 6px 0;'>");
  } catch (err) {
    console.error("network refresh failed", err);
  }
}

function renderEventList(el, events) {
  if (!events.length) { el.innerHTML = "<li>No events yet.</li>"; return; }
  el.innerHTML = events.map(e =>
    `<li><span>${e.event_type}${e.zone ? " — " + e.zone : ""}</span><span class="muted">${fmtTime(e.timestamp)}</span></li>`
  ).join("");
}

async function refreshEvents() {
  try {
    const events = await apiJSON("/api/events?limit=8");
    const el = document.getElementById("recent-events");
    if (el) renderEventList(el, events);
  } catch (err) {
    console.error("events refresh failed", err);
  }
}

async function takeSnapshot() {
  const img = document.getElementById("live-feed");
  try {
    await apiFetch("/api/snapshot");
  } catch (e) { /* ignore, still flash the feed */ }
  if (img) {
    const src = img.src;
    img.src = "/api/snapshot?_=" + Date.now();
    setTimeout(() => { img.src = "/video_feed"; }, 1200);
  }
}

async function startManualRecording() {
  try {
    await apiJSON("/api/record/manual", { method: "POST" });
    refreshStatus();
  } catch (err) {
    alert("Could not start recording: " + err.message);
  }
}

async function toggleContinuous() {
  try {
    const status = await apiJSON("/api/record/status");
    if (status.mode === "continuous" && status.active) {
      await apiJSON("/api/record/continuous/stop", { method: "POST" });
    } else {
      await apiJSON("/api/record/continuous/start", { method: "POST" });
    }
    refreshStatus();
  } catch (err) {
    alert("Could not toggle continuous recording: " + err.message);
  }
}

async function restartCamera() {
  const statusEl = document.getElementById("camera-restart-status");
  try {
    await apiJSON("/api/camera/restart", { method: "POST" });
    if (statusEl) {
      statusEl.textContent = "Reconnecting…";
      statusEl.className = "form-status ok";
      setTimeout(() => { statusEl.textContent = ""; }, 4000);
    }
    setTimeout(refreshStatus, 800);
  } catch (err) {
    if (statusEl) {
      statusEl.textContent = err.message;
      statusEl.className = "form-status error";
    }
  }
}

refreshStatus();
refreshEvents();
refreshNetwork();
setInterval(refreshStatus, 4000);
setInterval(refreshEvents, 8000);
setInterval(refreshNetwork, 5000);
