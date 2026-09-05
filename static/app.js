async function refreshData() {
  const btn = document.getElementById("btn-refresh");
  const msg = document.getElementById("refresh-msg");
  if (!btn) return;
  btn.disabled = true;
  msg.textContent = "Starting collection…";
  try {
    const res = await fetch("/api/refresh", { method: "POST" });
    const data = await res.json();
    msg.textContent = data.message || "Running…";
    pollStatus();
  } catch (e) {
    msg.textContent = "Refresh failed: " + e;
    btn.disabled = false;
  }
}

async function pollStatus() {
  const btn = document.getElementById("btn-refresh");
  const msg = document.getElementById("refresh-msg");
  let tries = 0;
  const tick = async () => {
    tries += 1;
    try {
      const res = await fetch("/api/refresh/status");
      const st = await res.json();
      msg.textContent = st.message || (st.running ? "Collecting…" : "");
      if (st.running && tries < 120) {
        setTimeout(tick, 2000);
        return;
      }
      btn.disabled = false;
      if (!st.running && st.last) {
        msg.textContent = (st.message || "Done") + " — reloading…";
        setTimeout(() => location.reload(), 800);
      }
    } catch (e) {
      btn.disabled = false;
      msg.textContent = String(e);
    }
  };
  setTimeout(tick, 1500);
}

document.getElementById("btn-refresh")?.addEventListener("click", refreshData);
