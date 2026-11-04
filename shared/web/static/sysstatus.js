// Header status badges, shared by every service's pages. Wifi, CPU and
// power come from the manager's /ws/system (see shared/sysstats.py); the
// mode badge comes from base's /ws/status. Each is a separate,
// independently-retrying websocket (see ws-utils.js) - if the owning
// service isn't running, its badge just stays grey.

const MODE_LABELS = {locate: "Locate", survey: "Survey-in", base: "Base"};

function modeBadgeClass(mode) {
  return mode === "base" ? "text-bg-success"
    : mode === "locate" ? "text-bg-info"
    : mode === "survey" ? "text-bg-warning"
    : "text-bg-secondary";
}

(function () {
  connectWsUrl(MANAGER_URL.replace(/^http/, "ws") + "ws/system", (msg) => {
    // Wifi by dBm: the Zero sat at about -58 dBm on the bench.
    const wifi = document.getElementById("sys-wifi");
    const dbm = msg.wifi ? msg.wifi.level_dbm : null;
    wifi.textContent = dbm === null ? "Wifi" : `Wifi ${Math.round(dbm)}`;
    wifi.className = "badge " + (
      dbm === null ? "text-bg-secondary"
      : dbm >= -67 ? "text-bg-success"
      : dbm >= -75 ? "text-bg-warning"
      : "text-bg-danger"
    );

    const cpu = document.getElementById("sys-cpu");
    if (msg.cpu_percent === null) {
      cpu.textContent = "CPU —";
      cpu.className = "badge text-bg-secondary";
    } else {
      cpu.textContent = `CPU ${Math.round(msg.cpu_percent)}%`;
      cpu.className = "badge " + (
        msg.cpu_percent >= 90 ? "text-bg-danger"
        : msg.cpu_percent >= 70 ? "text-bg-warning"
        : "text-bg-success"
      );
    }

    // get_throttled: bit 0 under-voltage now (red), anything else set
    // since boot (amber) - on the Zero that's usually the micro-USB cable.
    const power = document.getElementById("sys-power");
    const t = msg.throttled;
    power.className = "badge " + (
      t === null || t === undefined ? "text-bg-secondary"
      : (t & 0x1) ? "text-bg-danger"
      : t !== 0 ? "text-bg-warning"
      : "text-bg-success"
    );
  });

  if (BASE_WS_URL) {
    connectWsUrl(BASE_WS_URL, (msg) => {
      const badge = document.getElementById("sys-mode");
      badge.textContent = MODE_LABELS[msg.mode] || "—";
      badge.className = "badge " + modeBadgeClass(msg.mode);
    });
  }
})();
