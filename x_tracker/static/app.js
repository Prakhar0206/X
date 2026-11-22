(() => {
  "use strict";

  const VIEW_LABELS = {
    all: "Dashboard",
    overview: "Overview & KPIs",
    analytics: "Charts & Timing",
    posts: "Posts Explorer",
  };

  const DEFAULT_HEATMAP_READOUT = "Hover a cell to inspect slot";
  const BACKUP_STORAGE_KEY = "x_telemetry_backup_v1";

  function readLocalBackup() {
    try {
      const parsed = JSON.parse(window.localStorage.getItem(BACKUP_STORAGE_KEY) || "{}");
      return parsed && typeof parsed === "object" ? parsed : {};
    } catch (_) {
      return {};
    }
  }

  function writeLocalBackup(data) {
    try {
      window.localStorage.setItem(BACKUP_STORAGE_KEY, JSON.stringify(data || {}));
    } catch (_) {}
  }

  async function backupHandleToLocal(handle) {
    const clean = (handle || "").trim().toLowerCase();
    if (!clean) return;
    try {
      const bundle = await apiJson(`/api/export?handle=${encodeURIComponent(clean)}&format=json`);
      const backup = readLocalBackup();
      backup[clean] = bundle;
      writeLocalBackup(backup);
    } catch (_) {}
  }

  function removeHandleFromLocal(handle) {
    const clean = (handle || "").trim().toLowerCase();
    if (!clean) return;
    const backup = readLocalBackup();
    if (Object.prototype.hasOwnProperty.call(backup, clean)) {
      delete backup[clean];
      writeLocalBackup(backup);
    }
  }

  async function restoreFromLocalBackupIfNeeded() {
    const backup = readLocalBackup();
    const entries = Object.entries(backup);
    if (!entries.length) return false;
    let restoredAny = false;
    for (const [handle, bundle] of entries) {
      if (!bundle || typeof bundle !== "object") continue;
      try {
        await apiJson("/api/import", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            format: "json",
            handle,
            content: JSON.stringify(bundle),
          }),
        });
        restoredAny = true;
      } catch (_) {}
    }
    return restoredAny;
  }

  const state = {
    handle: "",
    view: "all",
    timeframe: "30d",
    mode: "cohort",
    granularity: "daily",
    startDate: "",
    endDate: "",
    search: "",
    postType: "all",
    sortBy: "views",
    sortDir: "desc",
    charts: {
      overTime: null,
      followers: null,
      cadence: null,
      engagement: null,
      postSnapshots: null,
    },
  };

  const $ = (id) => document.getElementById(id);

  function fmtInt(n) {
    return Number(n || 0).toLocaleString("en-US");
  }

  function fmtCompact(n) {
    const val = Number(n || 0);
    if (Math.abs(val) >= 1_000_000) return `${(val / 1_000_000).toFixed(2)}M`;
    if (Math.abs(val) >= 10_000) return `${(val / 1_000).toFixed(1)}K`;
    return fmtInt(val);
  }

  function showToast(message, isError = false) {
    const banner = $("toast-banner");
    if (!banner) return;
    banner.textContent = message;
    banner.classList.remove("hidden", "error");
    if (isError) banner.classList.add("error");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      banner.classList.add("hidden");
    }, 5200);
  }

  function applyDeltaBadge(el, value, suffix = "%", invertZeroNeutral = true) {
    if (!el) return;
    const num = Number(value || 0);
    el.classList.remove("pos", "neg", "neutral");
    const sign = num > 0 ? "+" : "";
    el.textContent = `${sign}${num.toFixed(suffix === " pts" ? 2 : 1)}${suffix}`;
    if (num > 0) {
      el.classList.add("pos");
    } else if (num < 0) {
      el.classList.add("neg");
    } else {
      el.classList.add(invertZeroNeutral ? "neutral" : "pos");
    }
  }

  function destroyChart(key) {
    if (state.charts[key]) {
      state.charts[key].destroy();
      state.charts[key] = null;
    }
  }

  function hasChartJs() {
    return typeof window.Chart === "function";
  }

  function getChartTheme() {
    return {
      grid: {
        color: "rgba(148, 163, 184, 0.08)",
        drawTicks: false,
      },
      borderHidden: { display: false },
      tick: {
        color: "#94A3B8",
        padding: 8,
        font: { family: "'JetBrains Mono', monospace", size: 11 },
      },
      compactTick: {
        color: "#94A3B8",
        padding: 6,
        font: { family: "'JetBrains Mono', monospace", size: 10 },
      },
      legend: {
        labels: {
          color: "#CBD5E1",
          boxWidth: 10,
          boxHeight: 10,
          usePointStyle: true,
          pointStyle: "circle",
          padding: 16,
          font: { family: "'Plus Jakarta Sans', sans-serif", size: 12, weight: "500" },
        },
      },
      tooltip: {
        backgroundColor: "#0F172A",
        titleColor: "#F8FAFC",
        bodyColor: "#CBD5E1",
        borderColor: "rgba(148, 163, 184, 0.2)",
        borderWidth: 1,
        padding: 10,
        cornerRadius: 8,
        boxPadding: 5,
        usePointStyle: true,
        titleFont: { family: "'Plus Jakarta Sans', sans-serif", size: 12, weight: "600" },
        bodyFont: { family: "'JetBrains Mono', monospace", size: 11 },
      },
    };
  }

  function setMobileSidebarOpen(isOpen) {
    const sidebar = $("sidebar");
    const backdrop = $("sidebar-backdrop");
    const menuBtn = $("btn-mobile-menu");
    if (sidebar) sidebar.classList.toggle("open", isOpen);
    if (backdrop) backdrop.classList.toggle("hidden", !isOpen);
    if (menuBtn) menuBtn.setAttribute("aria-expanded", isOpen ? "true" : "false");
  }

  function closeMobileSidebar() {
    setMobileSidebarOpen(false);
  }

  function toggleMobileSidebar() {
    const sidebar = $("sidebar");
    if (!sidebar) return;
    setMobileSidebarOpen(!sidebar.classList.contains("open"));
  }

  function applyView(viewName) {
    const validViews = ["all", "overview", "analytics", "posts"];
    const nextView = validViews.includes(viewName) ? viewName : "all";
    state.view = nextView;

    document.querySelectorAll("[data-view]").forEach((btn) => {
      const isActive = btn.dataset.view === nextView;
      btn.classList.toggle("active", isActive);
      if (isActive) {
        btn.setAttribute("aria-current", "page");
      } else {
        btn.removeAttribute("aria-current");
      }
    });

    const sections = {
      overview: $("section-overview"),
      analytics: $("section-analytics"),
      posts: $("section-posts"),
    };

    Object.entries(sections).forEach(([key, el]) => {
      if (!el) return;
      const visible = nextView === "all" || nextView === key;
      el.classList.toggle("hidden", !visible);
    });

    const titleEl = $("active-view-title") || $("view-title") || $("topbar-view-title");
    if (titleEl) {
      const activeBtn = document.querySelector(`[data-view="${nextView}"]`);
      titleEl.textContent = activeBtn?.dataset.viewLabel || VIEW_LABELS[nextView] || "Dashboard";
    }

    requestAnimationFrame(() => {
      Object.values(state.charts).forEach((chart) => {
        if (chart && typeof chart.resize === "function") {
          chart.resize();
        }
      });
    });
  }

  async function apiJson(url, options = {}) {
    const res = await fetch(url, options);
    const contentType = (res.headers.get("content-type") || "").toLowerCase();
    const payload = contentType.includes("application/json") ? await res.json() : await res.text();
    if (!res.ok) {
      const detail = typeof payload === "object" && payload?.detail ? payload.detail : String(payload);
      throw new Error(detail || `HTTP ${res.status}`);
    }
    return payload;
  }

  function renderHandleChips(accounts) {
    const bar = $("handle-chips-bar");
    if (!bar) return;
    bar.innerHTML = "";
    if (!accounts.length) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");

    for (const acc of accounts) {
      const isActive = acc.handle === state.handle;
      const chip = document.createElement("div");
      chip.className = `handle-chip${isActive ? " active" : ""}`;

      const selectBtn = document.createElement("button");
      selectBtn.type = "button";
      selectBtn.className = `chip-select-btn${isActive ? " active" : ""}`;
      selectBtn.title = `Switch to @${acc.handle}`;
      if (isActive) {
        selectBtn.setAttribute("aria-current", "true");
      }

      const handleSpan = document.createElement("span");
      handleSpan.className = "chip-handle";
      handleSpan.textContent = `@${acc.handle}`;

      const metaSpan = document.createElement("span");
      metaSpan.className = "chip-meta mono";
      metaSpan.textContent = fmtCompact(acc.followers_count);

      selectBtn.append(handleSpan, metaSpan);
      selectBtn.addEventListener("click", async () => {
        closeMobileSidebar();
        if (state.handle === acc.handle) return;
        state.handle = acc.handle;
        const selectEl = $("handle-select");
        if (selectEl) selectEl.value = acc.handle;
        renderHandleChips(accounts);
        await refreshDashboard();
      });

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "chip-remove-btn";
      removeBtn.textContent = "×";
      removeBtn.title = `Remove @${acc.handle}`;
      removeBtn.setAttribute("aria-label", `Remove @${acc.handle}`);
      removeBtn.addEventListener("click", (e) => {
        e.stopPropagation();
        removeHandle(acc.handle);
      });

      chip.append(selectBtn, removeBtn);
      bar.appendChild(chip);
    }
  }

  async function loadAccounts(preferredHandle = null) {
    const data = await apiJson("/api/accounts");
    const accounts = data.accounts || [];
    const select = $("handle-select");
    const switchGroup = select ? select.closest(".handle-switch-group") : null;
    if (select) select.innerHTML = "";

    const emptyState = $("empty-state");
    const dashboardContent = $("dashboard-content");

    if (!accounts.length) {
      state.handle = "";
      if (switchGroup) switchGroup.classList.add("hidden");
      renderHandleChips([]);
      if (emptyState) emptyState.classList.remove("hidden");
      if (dashboardContent) dashboardContent.classList.add("hidden");
      return false;
    }

    if (switchGroup) switchGroup.classList.remove("hidden");
    if (emptyState) emptyState.classList.add("hidden");
    if (dashboardContent) dashboardContent.classList.remove("hidden");

    if (select) {
      for (const acc of accounts) {
        const opt = document.createElement("option");
        opt.value = acc.handle;
        opt.textContent = `@${acc.handle} (${fmtCompact(acc.followers_count)})`;
        select.appendChild(opt);
      }
    }

    const exists = (h) => accounts.some((a) => a.handle === h);
    if (preferredHandle && exists(preferredHandle)) {
      state.handle = preferredHandle;
    } else if (!state.handle || !exists(state.handle)) {
      state.handle = accounts[0].handle;
    }
    if (select) select.value = state.handle;
    renderHandleChips(accounts);
    applyView(state.view);
    return true;
  }

  async function pollSyncUntilDone(fill, text, fallbackHandle) {
    const start = Date.now();
    while (Date.now() - start < 180000) {
      await new Promise((r) => setTimeout(r, 450));
      const status = await apiJson("/api/sync/status");
      if (fill) fill.style.width = `${status.progress_pct || 25}%`;
      if (text) text.textContent = status.message || `Syncing @${fallbackHandle}...`;
      if (!status.is_running) {
        return status;
      }
    }
    throw new Error("Sync timed out after 3 minutes.");
  }

  async function addHandle(rawInput, inputEl = null) {
    const val = (rawInput || "").trim();
    if (!val) return;
    const strip = $("sync-progress-strip");
    const fill = $("sync-progress-fill");
    const text = $("sync-progress-text");
    const addBtn = $("btn-add-handle");

    if (addBtn) addBtn.disabled = true;
    if (strip) strip.classList.remove("hidden");
    if (fill) fill.style.width = "15%";
    if (text) text.textContent = `Adding & syncing ${val}...`;

    try {
      const res = await apiJson("/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ handle: val, sync: true, background: true }),
      });
      const createdHandle = res.account?.handle;
      if (inputEl) inputEl.value = "";
      closeMobileSidebar();
      await loadAccounts(createdHandle);
      await refreshDashboard();

      let lastRes = res.sync_result;
      if (!lastRes && res.status && res.status.is_running) {
        const finalStatus = await pollSyncUntilDone(fill, text, createdHandle);
        lastRes = finalStatus.last_result || {};
      } else {
        lastRes = lastRes || res.status?.last_result || {};
      }
      if (fill) fill.style.width = "100%";
      const syncedPosts = lastRes.posts_synced || 0;
      const src = lastRes.source_used || "none";
      showToast(
        syncedPosts > 0
          ? `Added @${createdHandle} and synced ${syncedPosts} post(s) via ${src}.`
          : `Added @${createdHandle} to tracked handles.`
      );
      await loadAccounts(createdHandle);
      await refreshDashboard();
      await backupHandleToLocal(createdHandle);
    } catch (err) {
      showToast(`Could not add handle: ${err.message}`, true);
    } finally {
      if (addBtn) addBtn.disabled = false;
      if (strip) setTimeout(() => strip.classList.add("hidden"), 1200);
    }
  }

  async function removeHandle(handle) {
    const target = (handle || "").trim();
    if (!target) return;
    try {
      await apiJson(`/api/accounts/${encodeURIComponent(target)}`, {
        method: "DELETE",
      });
      removeHandleFromLocal(target);
      showToast(`Removed @${target}.`);
      if (state.handle === target) {
        state.handle = "";
      }
      const hasAccounts = await loadAccounts();
      if (hasAccounts) {
        await refreshDashboard();
      }
    } catch (err) {
      showToast(`Could not remove @${target}: ${err.message}`, true);
    }
  }

  async function refreshDashboard() {
    if (!state.handle) return;
    const params = new URLSearchParams({
      handle: state.handle,
      timeframe: state.timeframe,
      mode: state.mode,
      granularity: state.granularity,
      sort_by: state.sortBy,
      sort_dir: state.sortDir,
    });
    if (state.timeframe === "custom" && state.startDate && state.endDate) {
      params.set("start_date", state.startDate);
      params.set("end_date", state.endDate);
    }
    if (state.search.trim()) params.set("search", state.search.trim());
    if (state.postType !== "all") params.set("post_type", state.postType);

    const data = await apiJson(`/api/analytics?${params.toString()}`);
    renderProfile(data);
    renderKpis(data);
    renderCharts(data);
    renderHeatmap(data.charts?.heatmap);
    renderMediaBreakdown(data.charts?.media_breakdown || []);
    renderPostsTable(data.posts || []);
  }

  function renderProfile(data) {
    const acc = data.account || {};
    const win = data.window || {};
    const kpis = data.kpis || {};
    const avatar = $("profile-avatar");
    if (avatar) {
      if (acc.avatar_url) {
        avatar.src = acc.avatar_url;
        avatar.alt = `${acc.display_name || acc.handle} avatar`;
      } else {
        avatar.removeAttribute("src");
        avatar.alt = "";
      }
    }
    if ($("profile-display-name")) {
      $("profile-display-name").textContent = acc.display_name || `@${acc.handle}`;
    }
    if ($("profile-handle")) {
      $("profile-handle").textContent = `@${acc.handle}`;
    }
    if ($("profile-bio")) {
      $("profile-bio").textContent = acc.bio || "Tracked local handle";
    }
    if ($("mode-badge")) {
      $("mode-badge").textContent =
        win.mode === "velocity"
          ? "Velocity · Snapshot Growth Window"
          : "Cohort · Post-Publish Window";
    }

    const sDate = (win.start_date || "").slice(0, 10);
    const eDate = (win.end_date || "").slice(0, 10);
    if ($("meta-window-range")) {
      $("meta-window-range").textContent = `${sDate} → ${eDate} (${win.days || 0}d)`;
    }
    if ($("meta-cohort-vs-vel")) {
      $("meta-cohort-vs-vel").textContent =
        `Cohort ${fmtCompact(kpis.cohort_impressions)} / Vel ${fmtCompact(kpis.velocity_impressions)}`;
    }
    if ($("meta-last-synced")) {
      $("meta-last-synced").textContent = (acc.last_synced_at || "Never").replace("T", " ").replace("Z", " UTC");
    }
  }

  function renderKpis(data) {
    const k = data.kpis || {};
    const win = data.window || {};
    const heatmap = data.charts?.heatmap || {};

    if ($("kpi-imp-title")) {
      $("kpi-imp-title").textContent =
        win.mode === "velocity" ? "New Impressions (Velocity)" : "Total Impressions (Cohort)";
    }
    if ($("kpi-imp-value")) $("kpi-imp-value").textContent = fmtCompact(k.impressions);
    applyDeltaBadge($("kpi-imp-delta"), k.impressions_pct_change, "%");
    if ($("kpi-imp-sub")) {
      $("kpi-imp-sub").textContent =
        win.mode === "velocity"
          ? `Cohort published in window: ${fmtCompact(k.cohort_impressions)}`
          : `Snapshot velocity gained: ${fmtCompact(k.velocity_impressions)}`;
    }

    if ($("kpi-fol-value")) $("kpi-fol-value").textContent = fmtCompact(k.followers);
    const fDeltaEl = $("kpi-fol-delta");
    if (fDeltaEl) {
      fDeltaEl.classList.remove("pos", "neg", "neutral");
      const gained = Number(k.followers_gained || 0);
      fDeltaEl.textContent = `${gained >= 0 ? "+" : ""}${fmtInt(gained)}`;
      fDeltaEl.classList.add(gained > 0 ? "pos" : gained < 0 ? "neg" : "neutral");
    }
    if ($("kpi-fol-sub")) {
      $("kpi-fol-sub").textContent = `${Number(k.follower_daily_velocity || 0).toFixed(1)} / day net velocity`;
    }

    if ($("kpi-er-value")) $("kpi-er-value").textContent = `${Number(k.engagement_rate || 0).toFixed(2)}%`;
    applyDeltaBadge($("kpi-er-delta"), k.engagement_rate_delta, " pts");
    if ($("kpi-er-sub")) $("kpi-er-sub").textContent = `${fmtInt(k.total_engagements)} total engagements`;

    if ($("kpi-bm-value")) $("kpi-bm-value").textContent = fmtCompact(k.bookmarks);
    applyDeltaBadge($("kpi-bm-delta"), k.bookmarks_pct_change, "%");
    if ($("kpi-bm-sub")) {
      $("kpi-bm-sub").textContent = `High-Signal: ${Number(k.high_signal_score || 0).toFixed(1)} / 1K views`;
    }

    if ($("kpi-bl-value")) $("kpi-bl-value").textContent = `${Number(k.bookmark_to_like_ratio || 0).toFixed(1)}%`;
    const virBadge = $("kpi-vir-badge");
    if (virBadge) {
      virBadge.className = "delta-badge neutral";
      virBadge.textContent = `Virality ${Number(k.virality_ratio || 0).toFixed(2)}%`;
    }
    if ($("kpi-bl-sub")) {
      $("kpi-bl-sub").textContent = `Bookmark-to-like (${fmtInt(k.likes)} likes · ${fmtInt(k.reposts)} reposts)`;
    }

    if ($("kpi-posts-value")) $("kpi-posts-value").textContent = fmtInt(k.total_posts);
    applyDeltaBadge($("kpi-posts-delta"), k.total_posts_pct_change, "%");
    if ($("kpi-posts-sub")) {
      $("kpi-posts-sub").textContent = `Peak slot: ${heatmap.best_day || "—"} ${heatmap.best_hour_label || ""}`;
    }
  }

  function renderCharts(data) {
    if (!hasChartJs()) return;
    const charts = data.charts || {};
    const theme = getChartTheme();

    // 1. Impressions & Engagement Over Time
    destroyChart("overTime");
    const overTimeCtx = $("chart-over-time");
    if (overTimeCtx) {
      const overTime = charts.over_time || [];
      state.charts.overTime = new Chart(overTimeCtx, {
        type: "bar",
        data: {
          labels: overTime.map((d) => d.label),
          datasets: [
            {
              type: "line",
              label: "Impressions",
              data: overTime.map((d) => d.impressions),
              borderColor: "#38BDF8",
              backgroundColor: "rgba(56, 189, 248, 0.14)",
              fill: true,
              tension: 0.34,
              borderWidth: 2,
              pointRadius: overTime.length > 45 ? 0 : 2.5,
              pointHoverRadius: 5,
              pointHoverBorderWidth: 2,
              pointBackgroundColor: "#38BDF8",
              yAxisID: "y",
            },
            {
              type: "bar",
              label: "Engagements",
              data: overTime.map((d) => d.engagements),
              backgroundColor: "rgba(245, 158, 11, 0.72)",
              hoverBackgroundColor: "rgba(245, 158, 11, 0.92)",
              borderRadius: 5,
              maxBarThickness: 28,
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 4, right: 4, bottom: 0, left: 0 } },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: theme.legend,
            tooltip: theme.tooltip,
          },
          scales: {
            x: {
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.tick, maxTicksLimit: 12 },
            },
            y: {
              position: "left",
              border: theme.borderHidden,
              grid: theme.grid,
              ticks: { ...theme.tick, callback: (v) => fmtCompact(v) },
            },
            y1: {
              position: "right",
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.tick, callback: (v) => fmtCompact(v) },
            },
          },
        },
      });
    }

    // 2. Follower Growth Curve
    destroyChart("followers");
    const followersCtx = $("chart-followers");
    if (followersCtx) {
      const fSeries = charts.follower_growth || [];
      state.charts.followers = new Chart(followersCtx, {
        type: "line",
        data: {
          labels: fSeries.map((d) => (d.date || "").slice(5)),
          datasets: [
            {
              label: "Followers",
              data: fSeries.map((d) => d.followers_count),
              borderColor: "#10B981",
              backgroundColor: "rgba(16, 185, 129, 0.14)",
              fill: true,
              tension: 0.3,
              borderWidth: 2,
              pointRadius: fSeries.length > 40 ? 0 : 2.5,
              pointHoverRadius: 5,
              pointHoverBorderWidth: 2,
              pointBackgroundColor: "#10B981",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 8, right: 4, bottom: 0, left: 0 } },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: theme.tooltip,
          },
          scales: {
            x: {
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.tick, maxTicksLimit: 8 },
            },
            y: {
              border: theme.borderHidden,
              grid: theme.grid,
              ticks: { ...theme.tick, callback: (v) => fmtCompact(v) },
            },
          },
        },
      });
    }

    // 3. Posting Cadence vs Impressions
    destroyChart("cadence");
    const cadenceCtx = $("chart-cadence");
    if (cadenceCtx) {
      const cadence = charts.cadence_vs_impressions || [];
      state.charts.cadence = new Chart(cadenceCtx, {
        type: "bar",
        data: {
          labels: cadence.map((d) => d.label),
          datasets: [
            {
              type: "bar",
              label: "Posts Published",
              data: cadence.map((d) => d.posts_count),
              backgroundColor: "rgba(56, 189, 248, 0.32)",
              hoverBackgroundColor: "rgba(56, 189, 248, 0.55)",
              borderColor: "#38BDF8",
              borderWidth: 1,
              borderRadius: 5,
              maxBarThickness: 28,
              yAxisID: "y",
            },
            {
              type: "line",
              label: "Impressions",
              data: cadence.map((d) => d.impressions),
              borderColor: "#F59E0B",
              tension: 0.32,
              borderWidth: 2,
              pointRadius: cadence.length > 45 ? 0 : 2.5,
              pointHoverRadius: 5,
              pointHoverBorderWidth: 2,
              pointBackgroundColor: "#F59E0B",
              yAxisID: "y1",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 4, right: 4, bottom: 0, left: 0 } },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: theme.legend,
            tooltip: theme.tooltip,
          },
          scales: {
            x: {
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.tick, maxTicksLimit: 10 },
            },
            y: {
              position: "left",
              beginAtZero: true,
              border: theme.borderHidden,
              grid: theme.grid,
              ticks: { ...theme.tick, stepSize: 1 },
            },
            y1: {
              position: "right",
              beginAtZero: true,
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.tick, callback: (v) => fmtCompact(v) },
            },
          },
        },
      });
    }

    // 4. Engagement Breakdown
    destroyChart("engagement");
    const engagementCtx = $("chart-engagement-breakdown");
    if (engagementCtx) {
      const totals = charts.engagement_breakdown?.totals || {};
      state.charts.engagement = new Chart(engagementCtx, {
        type: "doughnut",
        data: {
          labels: ["Likes", "Bookmarks", "Reposts", "Replies", "Quotes"],
          datasets: [
            {
              data: [
                totals.likes || 0,
                totals.bookmarks || 0,
                totals.reposts || 0,
                totals.replies || 0,
                totals.quotes || 0,
              ],
              backgroundColor: ["#38BDF8", "#F59E0B", "#10B981", "#A78BFA", "#FB7185"],
              borderColor: "#101622",
              borderWidth: 2,
              hoverOffset: 4,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          cutout: "66%",
          layout: { padding: { top: 4, bottom: 4 } },
          plugins: {
            legend: {
              position: "bottom",
              labels: {
                ...theme.legend.labels,
                padding: 14,
                font: { family: "'Plus Jakarta Sans', sans-serif", size: 11, weight: "500" },
              },
            },
            tooltip: theme.tooltip,
          },
        },
      });
    }
  }

  function renderMediaBreakdown(items) {
    const container = $("media-breakdown-list");
    if (!container) return;
    container.innerHTML = "";
    const maxAvg = Math.max(1, ...items.map((i) => Number(i.avg_views || 0)));

    for (const item of items) {
      const row = document.createElement("div");
      row.className = "media-row";

      const top = document.createElement("div");
      top.className = "media-row-top";
      const lbl = document.createElement("span");
      lbl.textContent = `${item.label} (${item.post_count})`;
      const avg = document.createElement("span");
      avg.className = "mono";
      avg.textContent = `${fmtCompact(item.avg_views)} avg views`;
      top.append(lbl, avg);

      const track = document.createElement("div");
      track.className = "media-bar-track";
      const fill = document.createElement("div");
      fill.className = "media-bar-fill";
      fill.style.width = `${Math.round((Number(item.avg_views || 0) / maxAvg) * 100)}%`;
      track.appendChild(fill);

      const meta = document.createElement("div");
      meta.className = "media-row-meta mono";
      meta.textContent = `ER ${Number(item.engagement_rate || 0).toFixed(2)}% · HS ${Number(item.high_signal_score || 0).toFixed(1)} · Avg BM ${Number(item.avg_bookmarks || 0).toFixed(1)}`;

      row.append(top, track, meta);
      container.appendChild(row);
    }
  }

  function heatColor(avg, maxAvg) {
    if (!avg || maxAvg <= 0) return "#141C2B";
    const ratio = avg / maxAvg;
    if (ratio >= 0.85) return "#F59E0B";
    if (ratio >= 0.58) return "#38BDF8";
    if (ratio >= 0.32) return "#0284C7";
    return "#0C4A6E";
  }

  function renderHeatmap(heatmap) {
    const container = $("heatmap-container");
    if (!container) return;
    container.innerHTML = "";

    const readoutEl = $("heatmap-hover-readout");
    if (readoutEl) {
      readoutEl.textContent = DEFAULT_HEATMAP_READOUT;
    }

    if (!heatmap || !heatmap.matrix) return;

    const bestBadge = $("heatmap-best-badge");
    if (bestBadge) {
      bestBadge.textContent =
        `Peak: ${heatmap.best_day} ${heatmap.best_hour_label} (${fmtCompact(heatmap.best_slot_avg_views)} avg)`;
    }

    const table = document.createElement("table");
    table.className = "heatmap-table";

    const thead = document.createElement("thead");
    const headRow = document.createElement("tr");
    const corner = document.createElement("th");
    corner.textContent = "";
    headRow.appendChild(corner);
    for (let h = 0; h < 24; h++) {
      const th = document.createElement("th");
      th.textContent = h % 2 === 0 ? `${String(h).padStart(2, "0")}` : "";
      headRow.appendChild(th);
    }
    thead.appendChild(headRow);
    table.appendChild(thead);

    const tbody = document.createElement("tbody");
    const maxAvg = Number(heatmap.max_avg_views || 0);
    for (const rowCells of heatmap.matrix) {
      const tr = document.createElement("tr");
      const dayTd = document.createElement("td");
      dayTd.className = "day-label";
      dayTd.textContent = rowCells[0]?.day || "";
      tr.appendChild(dayTd);

      for (const cell of rowCells) {
        const td = document.createElement("td");
        td.className = "heatmap-cell";
        td.tabIndex = 0;
        td.style.backgroundColor = heatColor(cell.avg_views, maxAvg);
        const readoutText = `${cell.day} ${cell.hour_label} UTC · ${cell.post_count} post(s) · ${fmtInt(Math.round(cell.avg_views))} avg views`;
        td.title = readoutText;
        td.setAttribute("aria-label", readoutText);

        const showReadout = () => {
          const el = $("heatmap-hover-readout");
          if (el) el.textContent = readoutText;
        };
        const clearReadout = () => {
          const el = $("heatmap-hover-readout");
          if (el) el.textContent = DEFAULT_HEATMAP_READOUT;
        };

        td.addEventListener("mouseenter", showReadout);
        td.addEventListener("focus", showReadout);
        td.addEventListener("mouseleave", clearReadout);
        td.addEventListener("blur", clearReadout);

        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderPostsTable(posts) {
    const tbody = $("posts-table-body");
    const list = Array.isArray(posts) ? posts : [];
    const countBadge = $("posts-count-badge");
    if (countBadge) {
      countBadge.textContent = `${list.length} post${list.length === 1 ? "" : "s"}`;
    }
    if (!tbody) return;
    tbody.innerHTML = "";

    if (!list.length) {
      const tr = document.createElement("tr");
      const td = document.createElement("td");
      td.colSpan = 9;
      td.style.textAlign = "center";
      td.style.color = "#94A3B8";
      td.style.padding = "28px";
      td.textContent = "No posts match the selected filters in this window.";
      tr.appendChild(td);
      tbody.appendChild(tr);
      return;
    }

    for (const p of list) {
      const tr = document.createElement("tr");
      tr.className = "clickable-row";
      tr.tabIndex = 0;
      tr.addEventListener("click", () => openPostDrawer(p.post_id));
      tr.addEventListener("keydown", (e) => {
        if (e.key === "Enter" || e.key === " ") {
          e.preventDefault();
          openPostDrawer(p.post_id);
        }
      });

      const tdText = document.createElement("td");
      const badgeRow = document.createElement("div");
      badgeRow.className = "badge-row";

      const catBadge = document.createElement("span");
      catBadge.className = `type-badge ${p.post_category === "thread" ? "thread" : ""}`;
      catBadge.textContent = String(p.post_category || "original").toUpperCase();
      badgeRow.appendChild(catBadge);

      if (p.media_type && p.media_type !== "none") {
        const mediaBadge = document.createElement("span");
        mediaBadge.className = "type-badge media";
        mediaBadge.textContent = String(p.media_type).toUpperCase();
        badgeRow.appendChild(mediaBadge);
      }

      const snippet = document.createElement("div");
      snippet.className = "post-snippet";
      snippet.textContent = p.text || "(no text)";
      tdText.append(badgeRow, snippet);

      const tdDate = document.createElement("td");
      tdDate.className = "mono";
      tdDate.style.whiteSpace = "nowrap";
      tdDate.textContent = (p.published_at || "").slice(0, 16).replace("T", " ");

      const tdViews = document.createElement("td");
      tdViews.className = "num";
      tdViews.textContent = fmtInt(p.views);

      const tdVel = document.createElement("td");
      tdVel.className = "num";
      const velSpan = document.createElement("span");
      velSpan.className = "velocity-pill";
      velSpan.textContent = `+${fmtCompact(p.velocity_24h || 0)}`;
      tdVel.appendChild(velSpan);

      const tdEr = document.createElement("td");
      tdEr.className = "num";
      tdEr.textContent = `${Number(p.engagement_rate || 0).toFixed(2)}%`;

      const tdHs = document.createElement("td");
      tdHs.className = "num";
      tdHs.textContent = Number(p.high_signal_score || 0).toFixed(1);

      const tdLikes = document.createElement("td");
      tdLikes.className = "num";
      tdLikes.textContent = fmtInt(p.likes);

      const tdReplies = document.createElement("td");
      tdReplies.className = "num";
      tdReplies.textContent = fmtInt(p.replies);

      const tdBm = document.createElement("td");
      tdBm.className = "num";
      tdBm.textContent = fmtInt(p.bookmarks);

      tr.append(tdText, tdDate, tdViews, tdVel, tdEr, tdHs, tdLikes, tdReplies, tdBm);
      tbody.appendChild(tr);
    }
  }

  async function openPostDrawer(postId) {
    const detail = await apiJson(`/api/posts/${encodeURIComponent(postId)}`);
    if ($("drawer-meta")) {
      $("drawer-meta").textContent =
        `@${detail.handle} · ${(detail.published_at || "").replace("T", " ").replace("Z", " UTC")}`;
    }
    if ($("drawer-x-link")) {
      $("drawer-x-link").href = detail.permalink || `https://x.com/${detail.handle}/status/${detail.post_id}`;
    }
    if ($("drawer-text")) {
      $("drawer-text").textContent = detail.text || "";
    }

    const badges = $("drawer-badges");
    if (badges) {
      badges.innerHTML = "";
      const mediaTag = document.createElement("span");
      mediaTag.className = `type-badge ${detail.media_type !== "none" ? "media" : ""}`;
      mediaTag.textContent = detail.media_type === "none" ? "TEXT-ONLY" : String(detail.media_type).toUpperCase();
      badges.appendChild(mediaTag);
      if (detail.is_thread) {
        const tb = document.createElement("span");
        tb.className = "type-badge thread";
        tb.textContent = "THREAD";
        badges.appendChild(tb);
      }
    }

    if ($("dk-views")) $("dk-views").textContent = fmtInt(detail.views);
    if ($("dk-vel")) $("dk-vel").textContent = `+${fmtInt(detail.velocity_24h || 0)}`;
    if ($("dk-er")) $("dk-er").textContent = `${Number(detail.engagement_rate || 0).toFixed(2)}%`;
    if ($("dk-hs")) $("dk-hs").textContent = Number(detail.high_signal_score || 0).toFixed(1);
    if ($("dk-likes")) $("dk-likes").textContent = fmtInt(detail.likes);
    if ($("dk-bm")) $("dk-bm").textContent = fmtInt(detail.bookmarks);

    const snaps = detail.snapshots || [];
    const snapCtx = $("chart-post-snapshots");
    if (hasChartJs() && snapCtx) {
      const theme = getChartTheme();
      destroyChart("postSnapshots");
      state.charts.postSnapshots = new Chart(snapCtx, {
        type: "line",
        data: {
          labels: snaps.map((s) => (s.captured_at || "").slice(5, 16).replace("T", " ")),
          datasets: [
            {
              label: "Cumulative Views",
              data: snaps.map((s) => s.views),
              borderColor: "#38BDF8",
              backgroundColor: "rgba(56, 189, 248, 0.14)",
              fill: true,
              tension: 0.3,
              borderWidth: 2,
              pointRadius: snaps.length > 30 ? 0 : 3,
              pointHoverRadius: 5,
              pointHoverBorderWidth: 2,
              pointBackgroundColor: "#38BDF8",
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          layout: { padding: { top: 6, right: 4, bottom: 0, left: 0 } },
          interaction: { mode: "index", intersect: false },
          plugins: {
            legend: { display: false },
            tooltip: theme.tooltip,
          },
          scales: {
            x: {
              border: theme.borderHidden,
              grid: { display: false },
              ticks: { ...theme.compactTick, maxTicksLimit: 6 },
            },
            y: {
              border: theme.borderHidden,
              grid: theme.grid,
              ticks: { ...theme.compactTick, callback: (v) => fmtCompact(v) },
            },
          },
        },
      });
    }

    const tbody = $("drawer-snapshots-body");
    if (tbody) {
      tbody.innerHTML = "";
      for (const s of [...snaps].reverse()) {
        const tr = document.createElement("tr");
        const cells = [
          (s.captured_at || "").replace("T", " ").replace("Z", ""),
          fmtInt(s.views),
          `+${fmtInt(s.views_delta)}`,
          fmtInt(s.likes),
          fmtInt(s.replies),
          fmtInt(s.reposts),
          fmtInt(s.bookmarks),
        ];
        cells.forEach((txt, idx) => {
          const td = document.createElement("td");
          td.className = idx > 0 ? "num" : "mono";
          td.textContent = txt;
          tr.appendChild(td);
        });
        tbody.appendChild(tr);
      }
    }

    if ($("drawer-backdrop")) $("drawer-backdrop").classList.remove("hidden");
    if ($("post-drawer")) $("post-drawer").classList.remove("hidden");
  }

  function closePostDrawer() {
    if ($("drawer-backdrop")) $("drawer-backdrop").classList.add("hidden");
    if ($("post-drawer")) $("post-drawer").classList.add("hidden");
  }

  async function triggerSyncNow() {
    if (!state.handle) return;
    const btn = $("btn-sync-now");
    const strip = $("sync-progress-strip");
    const fill = $("sync-progress-fill");
    const text = $("sync-progress-text");
    const dot = btn ? btn.querySelector(".sync-dot") : null;
    const label = $("sync-btn-label");

    if (btn) btn.disabled = true;
    if (dot) dot.classList.add("pulsing");
    if (label) label.textContent = "Syncing...";
    if (strip) strip.classList.remove("hidden");
    if (fill) fill.style.width = "15%";
    if (text) text.textContent = `Fetching @${state.handle} via FxTwitter v2...`;

    try {
      const resp = await apiJson("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ handle: state.handle, background: true }),
      });
      let res = resp.result;
      if (!res && resp.status && resp.status.is_running) {
        const finalStatus = await pollSyncUntilDone(fill, text, state.handle);
        res = Array.isArray(finalStatus.last_result)
          ? finalStatus.last_result[0] || {}
          : finalStatus.last_result || {};
      } else {
        res = res || resp.status?.last_result || {};
      }
      if (fill) fill.style.width = "100%";
      if (res.success) {
        showToast(
          `Synced @${state.handle} via ${res.source_used}: ${res.posts_synced} post(s), ${res.snapshots_recorded} snapshot(s).`
        );
      } else {
        showToast(
          `Sync completed for @${state.handle} with fallback warnings (${(res.errors || []).join(" ")})`,
          true
        );
      }
      await loadAccounts(state.handle);
      await refreshDashboard();
      await backupHandleToLocal(state.handle);
    } catch (err) {
      showToast(`Sync failed: ${err.message}`, true);
    } finally {
      if (btn) btn.disabled = false;
      if (dot) dot.classList.remove("pulsing");
      if (label) label.textContent = "Sync Now";
      if (strip) setTimeout(() => strip.classList.add("hidden"), 1500);
    }
  }

  async function handleFileImport(file) {
    if (!file) return;
    const formData = new FormData();
    formData.append("file", file);
    if (state.handle) formData.append("handle", state.handle);

    try {
      const res = await apiJson("/api/import", {
        method: "POST",
        body: formData,
      });
      showToast(
        `Imported ${res.posts_imported} post(s) and ${res.snapshots_created} snapshot(s) into @${res.handle}.`
      );
      closeMobileSidebar();
      await loadAccounts(res.handle);
      await refreshDashboard();
      await backupHandleToLocal(res.handle);
    } catch (err) {
      showToast(`Import failed: ${err.message}`, true);
    }
  }

  function wireEvents() {
    // View navigation tabs
    document.querySelectorAll("[data-view]").forEach((btn) => {
      btn.addEventListener("click", () => {
        applyView(btn.dataset.view);
        closeMobileSidebar();
      });
    });

    // Mobile sidebar drawer
    const mobileMenuBtn = $("btn-mobile-menu");
    if (mobileMenuBtn) {
      mobileMenuBtn.addEventListener("click", toggleMobileSidebar);
    }
    const sidebarBackdrop = $("sidebar-backdrop");
    if (sidebarBackdrop) {
      sidebarBackdrop.addEventListener("click", closeMobileSidebar);
    }

    const handleSelect = $("handle-select");
    if (handleSelect) {
      handleSelect.addEventListener("change", async (e) => {
        state.handle = e.target.value;
        closeMobileSidebar();
        await loadAccounts(state.handle);
        await refreshDashboard();
      });
    }

    const inlineAddForm = $("inline-add-form");
    if (inlineAddForm) {
      inlineAddForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const input = $("inline-handle-input");
        if (input) await addHandle(input.value, input);
      });
    }

    const emptyAddForm = $("empty-add-form");
    if (emptyAddForm) {
      emptyAddForm.addEventListener("submit", async (e) => {
        e.preventDefault();
        const input = $("empty-handle-input");
        if (input) await addHandle(input.value, input);
      });
    }

    const removeHandleBtn = $("btn-remove-handle");
    if (removeHandleBtn) {
      removeHandleBtn.addEventListener("click", async () => {
        if (state.handle) {
          await removeHandle(state.handle);
        }
      });
    }

    document.querySelectorAll("[data-mode]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll("[data-mode]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.mode = btn.dataset.mode;
        await refreshDashboard();
      });
    });

    document.querySelectorAll("[data-tf]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll("[data-tf]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.timeframe = btn.dataset.tf;
        const customBar = $("custom-date-bar");
        if (state.timeframe === "custom") {
          if (customBar) customBar.classList.remove("hidden");
          const startEl = $("custom-start");
          const endEl = $("custom-end");
          if (startEl && endEl) {
            if (!startEl.value || !endEl.value) {
              const end = new Date();
              const start = new Date(end.getTime() - 14 * 86400 * 1000);
              startEl.value = start.toISOString().slice(0, 10);
              endEl.value = end.toISOString().slice(0, 10);
            }
            state.startDate = startEl.value;
            state.endDate = endEl.value;
          }
        } else if (customBar) {
          customBar.classList.add("hidden");
        }
        await refreshDashboard();
      });
    });

    const applyCustomBtn = $("btn-apply-custom");
    if (applyCustomBtn) {
      applyCustomBtn.addEventListener("click", async () => {
        state.startDate = $("custom-start")?.value || "";
        state.endDate = $("custom-end")?.value || "";
        if (state.startDate && state.endDate) {
          await refreshDashboard();
        }
      });
    }

    document.querySelectorAll("[data-gran]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll("[data-gran]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.granularity = btn.dataset.gran;
        await refreshDashboard();
      });
    });

    document.querySelectorAll("[data-ptype]").forEach((btn) => {
      btn.addEventListener("click", async () => {
        document.querySelectorAll("[data-ptype]").forEach((b) => b.classList.remove("active"));
        btn.classList.add("active");
        state.postType = btn.dataset.ptype;
        await refreshDashboard();
      });
    });

    let searchTimer = null;
    const postSearch = $("post-search");
    if (postSearch) {
      postSearch.addEventListener("input", (e) => {
        state.search = e.target.value;
        clearTimeout(searchTimer);
        searchTimer = setTimeout(refreshDashboard, 180);
      });
    }

    const sortBy = $("sort-by");
    if (sortBy) {
      sortBy.addEventListener("change", async (e) => {
        state.sortBy = e.target.value;
        await refreshDashboard();
      });
    }

    const sortDirBtn = $("btn-sort-dir");
    if (sortDirBtn) {
      sortDirBtn.addEventListener("click", async () => {
        state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
        sortDirBtn.textContent = state.sortDir.toUpperCase();
        await refreshDashboard();
      });
    }

    const syncBtn = $("btn-sync-now");
    if (syncBtn) {
      syncBtn.addEventListener("click", triggerSyncNow);
    }

    const importFileInput = $("import-file-input");
    const importBtn = $("btn-import");
    if (importBtn && importFileInput) {
      importBtn.addEventListener("click", () => importFileInput.click());
    }
    const emptyImportBtn = $("btn-empty-import");
    if (emptyImportBtn && importFileInput) {
      emptyImportBtn.addEventListener("click", () => importFileInput.click());
    }
    if (importFileInput) {
      importFileInput.addEventListener("change", async (e) => {
        const file = e.target.files?.[0];
        e.target.value = "";
        await handleFileImport(file);
      });
    }

    const exportCsvBtn = $("btn-export-csv");
    if (exportCsvBtn) {
      exportCsvBtn.addEventListener("click", () => {
        if (!state.handle) return;
        window.location.href = `/api/export?handle=${encodeURIComponent(state.handle)}&format=csv`;
      });
    }

    const exportJsonBtn = $("btn-export-json");
    if (exportJsonBtn) {
      exportJsonBtn.addEventListener("click", () => {
        if (!state.handle) return;
        window.location.href = `/api/export?handle=${encodeURIComponent(state.handle)}&format=json`;
      });
    }

    const closeDrawerBtn = $("btn-close-drawer");
    if (closeDrawerBtn) {
      closeDrawerBtn.addEventListener("click", closePostDrawer);
    }
    const drawerBackdrop = $("drawer-backdrop");
    if (drawerBackdrop) {
      drawerBackdrop.addEventListener("click", closePostDrawer);
    }
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePostDrawer();
        closeMobileSidebar();
      }
    });
  }

  async function init() {
    wireEvents();
    applyView(state.view);
    try {
      let hasAccounts = await loadAccounts();
      if (!hasAccounts) {
        const restored = await restoreFromLocalBackupIfNeeded();
        if (restored) {
          hasAccounts = await loadAccounts();
          if (hasAccounts) {
            showToast("Restored tracked handles from browser backup.");
          }
        }
      }
      if (hasAccounts) {
        await refreshDashboard();
      }
    } catch (err) {
      showToast(`Failed to load dashboard: ${err.message}`, true);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
