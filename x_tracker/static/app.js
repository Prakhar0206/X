(() => {
  "use strict";

  const state = {
    handle: "",
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
    banner.textContent = message;
    banner.classList.remove("hidden", "error");
    if (isError) banner.classList.add("error");
    clearTimeout(showToast._timer);
    showToast._timer = setTimeout(() => {
      banner.classList.add("hidden");
    }, 5200);
  }

  function applyDeltaBadge(el, value, suffix = "%", invertZeroNeutral = true) {
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
    bar.innerHTML = "";
    if (!accounts.length) {
      bar.classList.add("hidden");
      return;
    }
    bar.classList.remove("hidden");

    for (const acc of accounts) {
      const chip = document.createElement("div");
      chip.className = `handle-chip ${acc.handle === state.handle ? "active" : ""}`;

      const selectBtn = document.createElement("button");
      selectBtn.type = "button";
      selectBtn.className = "chip-select-btn";
      selectBtn.textContent = `@${acc.handle}`;
      selectBtn.title = `Switch to @${acc.handle}`;
      selectBtn.addEventListener("click", async () => {
        if (state.handle === acc.handle) return;
        state.handle = acc.handle;
        $("handle-select").value = acc.handle;
        renderHandleChips(accounts);
        await refreshDashboard();
      });

      const removeBtn = document.createElement("button");
      removeBtn.type = "button";
      removeBtn.className = "chip-remove-btn";
      removeBtn.textContent = "×";
      removeBtn.title = `Remove @${acc.handle}`;
      removeBtn.setAttribute("aria-label", `Remove @${acc.handle}`);
      removeBtn.addEventListener("click", () => removeHandle(acc.handle));

      chip.append(selectBtn, removeBtn);
      bar.appendChild(chip);
    }
  }

  async function loadAccounts(preferredHandle = null) {
    const data = await apiJson("/api/accounts");
    const accounts = data.accounts || [];
    const select = $("handle-select");
    const switchGroup = select.closest(".handle-switch-group");
    select.innerHTML = "";

    if (!accounts.length) {
      state.handle = "";
      if (switchGroup) switchGroup.classList.add("hidden");
      renderHandleChips([]);
      $("empty-state").classList.remove("hidden");
      $("dashboard-content").classList.add("hidden");
      return false;
    }

    if (switchGroup) switchGroup.classList.remove("hidden");
    $("empty-state").classList.add("hidden");
    $("dashboard-content").classList.remove("hidden");

    for (const acc of accounts) {
      const opt = document.createElement("option");
      opt.value = acc.handle;
      opt.textContent = `@${acc.handle} (${fmtCompact(acc.followers_count)})`;
      select.appendChild(opt);
    }

    const exists = (h) => accounts.some((a) => a.handle === h);
    if (preferredHandle && exists(preferredHandle)) {
      state.handle = preferredHandle;
    } else if (!state.handle || !exists(state.handle)) {
      state.handle = accounts[0].handle;
    }
    select.value = state.handle;
    renderHandleChips(accounts);
    return true;
  }

  async function pollSyncUntilDone(fill, text, fallbackHandle) {
    const start = Date.now();
    while (Date.now() - start < 180000) {
      await new Promise((r) => setTimeout(r, 450));
      const status = await apiJson("/api/sync/status");
      fill.style.width = `${status.progress_pct || 25}%`;
      text.textContent = status.message || `Syncing @${fallbackHandle}...`;
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
    strip.classList.remove("hidden");
    fill.style.width = "15%";
    text.textContent = `Adding & syncing ${val}...`;

    try {
      const res = await apiJson("/api/accounts", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ handle: val, sync: true, background: true }),
      });
      const createdHandle = res.account?.handle;
      if (inputEl) inputEl.value = "";
      await loadAccounts(createdHandle);
      await refreshDashboard();

      const finalStatus = await pollSyncUntilDone(fill, text, createdHandle);
      fill.style.width = "100%";
      const lastRes = finalStatus.last_result || {};
      const syncedPosts = lastRes.posts_synced || 0;
      const src = lastRes.source_used || "none";
      showToast(
        syncedPosts > 0
          ? `Added @${createdHandle} and synced ${syncedPosts} post(s) via ${src}.`
          : `Added @${createdHandle} to tracked handles.`
      );
      await loadAccounts(createdHandle);
      await refreshDashboard();
    } catch (err) {
      showToast(`Could not add handle: ${err.message}`, true);
    } finally {
      if (addBtn) addBtn.disabled = false;
      setTimeout(() => strip.classList.add("hidden"), 1200);
    }
  }

  async function removeHandle(handle) {
    const target = (handle || "").trim();
    if (!target) return;
    try {
      await apiJson(`/api/accounts/${encodeURIComponent(target)}`, {
        method: "DELETE",
      });
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
    renderHeatmap(data.charts.heatmap);
    renderMediaBreakdown(data.charts.media_breakdown);
    renderPostsTable(data.posts);
  }

  function renderProfile(data) {
    const acc = data.account || {};
    const win = data.window || {};
    const kpis = data.kpis || {};
    const avatar = $("profile-avatar");
    if (acc.avatar_url) {
      avatar.src = acc.avatar_url;
      avatar.alt = `${acc.display_name || acc.handle} avatar`;
    } else {
      avatar.removeAttribute("src");
      avatar.alt = "";
    }
    $("profile-display-name").textContent = acc.display_name || `@${acc.handle}`;
    $("profile-handle").textContent = `@${acc.handle}`;
    $("profile-bio").textContent = acc.bio || "Tracked local handle";
    $("mode-badge").textContent =
      win.mode === "velocity"
        ? "Velocity · Snapshot Growth Window"
        : "Cohort · Post-Publish Window";

    const sDate = (win.start_date || "").slice(0, 10);
    const eDate = (win.end_date || "").slice(0, 10);
    $("meta-window-range").textContent = `${sDate} → ${eDate} (${win.days || 0}d)`;
    $("meta-cohort-vs-vel").textContent =
      `Cohort ${fmtCompact(kpis.cohort_impressions)} / Vel ${fmtCompact(kpis.velocity_impressions)}`;
    $("meta-last-synced").textContent = (acc.last_synced_at || "Never").replace("T", " ").replace("Z", " UTC");
  }

  function renderKpis(data) {
    const k = data.kpis || {};
    const win = data.window || {};
    const heatmap = data.charts?.heatmap || {};

    $("kpi-imp-title").textContent =
      win.mode === "velocity" ? "New Impressions (Velocity)" : "Total Impressions (Cohort)";
    $("kpi-imp-value").textContent = fmtCompact(k.impressions);
    applyDeltaBadge($("kpi-imp-delta"), k.impressions_pct_change, "%");
    $("kpi-imp-sub").textContent =
      win.mode === "velocity"
        ? `Cohort published in window: ${fmtCompact(k.cohort_impressions)}`
        : `Snapshot velocity gained: ${fmtCompact(k.velocity_impressions)}`;

    $("kpi-fol-value").textContent = fmtCompact(k.followers);
    const fDeltaEl = $("kpi-fol-delta");
    fDeltaEl.classList.remove("pos", "neg", "neutral");
    const gained = Number(k.followers_gained || 0);
    fDeltaEl.textContent = `${gained >= 0 ? "+" : ""}${fmtInt(gained)}`;
    fDeltaEl.classList.add(gained > 0 ? "pos" : gained < 0 ? "neg" : "neutral");
    $("kpi-fol-sub").textContent = `${Number(k.follower_daily_velocity || 0).toFixed(1)} / day net velocity`;

    $("kpi-er-value").textContent = `${Number(k.engagement_rate || 0).toFixed(2)}%`;
    applyDeltaBadge($("kpi-er-delta"), k.engagement_rate_delta, " pts");
    $("kpi-er-sub").textContent = `${fmtInt(k.total_engagements)} total engagements`;

    $("kpi-bm-value").textContent = fmtCompact(k.bookmarks);
    applyDeltaBadge($("kpi-bm-delta"), k.bookmarks_pct_change, "%");
    $("kpi-bm-sub").textContent = `High-Signal: ${Number(k.high_signal_score || 0).toFixed(1)} / 1K views`;

    $("kpi-bl-value").textContent = `${Number(k.bookmark_to_like_ratio || 0).toFixed(1)}%`;
    const virBadge = $("kpi-vir-badge");
    virBadge.className = "delta-badge neutral";
    virBadge.textContent = `Virality ${Number(k.virality_ratio || 0).toFixed(2)}%`;
    $("kpi-bl-sub").textContent = `Bookmark-to-like (${fmtInt(k.likes)} likes · ${fmtInt(k.reposts)} reposts)`;

    $("kpi-posts-value").textContent = fmtInt(k.total_posts);
    applyDeltaBadge($("kpi-posts-delta"), k.total_posts_pct_change, "%");
    $("kpi-posts-sub").textContent = `Peak slot: ${heatmap.best_day || "—"} ${heatmap.best_hour_label || ""}`;
  }

  function renderCharts(data) {
    if (!hasChartJs()) return;
    const charts = data.charts || {};
    const commonGrid = { color: "rgba(36, 48, 68, 0.55)" };
    const commonTick = { color: "#94A3B8", font: { family: "JetBrains Mono", size: 11 } };

    // 1. Impressions & Engagement Over Time
    destroyChart("overTime");
    const overTime = charts.over_time || [];
    state.charts.overTime = new Chart($("chart-over-time"), {
      type: "bar",
      data: {
        labels: overTime.map((d) => d.label),
        datasets: [
          {
            type: "line",
            label: "Impressions",
            data: overTime.map((d) => d.impressions),
            borderColor: "#38BDF8",
            backgroundColor: "rgba(56, 189, 248, 0.16)",
            fill: true,
            tension: 0.32,
            borderWidth: 2,
            pointRadius: overTime.length > 45 ? 0 : 2.5,
            yAxisID: "y",
          },
          {
            type: "bar",
            label: "Engagements",
            data: overTime.map((d) => d.engagements),
            backgroundColor: "rgba(245, 158, 11, 0.72)",
            borderRadius: 4,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: {
          legend: { labels: { color: "#E2E8F0", boxWidth: 12 } },
        },
        scales: {
          x: { grid: { display: false }, ticks: { ...commonTick, maxTicksLimit: 12 } },
          y: {
            position: "left",
            grid: commonGrid,
            ticks: { ...commonTick, callback: (v) => fmtCompact(v) },
          },
          y1: {
            position: "right",
            grid: { display: false },
            ticks: { ...commonTick, callback: (v) => fmtCompact(v) },
          },
        },
      },
    });

    // 2. Follower Growth Curve
    destroyChart("followers");
    const fSeries = charts.follower_growth || [];
    state.charts.followers = new Chart($("chart-followers"), {
      type: "line",
      data: {
        labels: fSeries.map((d) => d.date.slice(5)),
        datasets: [
          {
            label: "Followers",
            data: fSeries.map((d) => d.followers_count),
            borderColor: "#10B981",
            backgroundColor: "rgba(16, 185, 129, 0.14)",
            fill: true,
            tension: 0.28,
            borderWidth: 2,
            pointRadius: fSeries.length > 40 ? 0 : 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { display: false } },
        scales: {
          x: { grid: { display: false }, ticks: { ...commonTick, maxTicksLimit: 8 } },
          y: {
            grid: commonGrid,
            ticks: { ...commonTick, callback: (v) => fmtCompact(v) },
          },
        },
      },
    });

    // 3. Posting Cadence vs Impressions
    destroyChart("cadence");
    const cadence = charts.cadence_vs_impressions || [];
    state.charts.cadence = new Chart($("chart-cadence"), {
      type: "bar",
      data: {
        labels: cadence.map((d) => d.label),
        datasets: [
          {
            type: "bar",
            label: "Posts Published",
            data: cadence.map((d) => d.posts_count),
            backgroundColor: "rgba(56, 189, 248, 0.35)",
            borderColor: "#38BDF8",
            borderWidth: 1,
            borderRadius: 4,
            yAxisID: "y",
          },
          {
            type: "line",
            label: "Impressions",
            data: cadence.map((d) => d.impressions),
            borderColor: "#F59E0B",
            tension: 0.3,
            borderWidth: 2,
            pointRadius: cadence.length > 45 ? 0 : 2.5,
            yAxisID: "y1",
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        interaction: { mode: "index", intersect: false },
        plugins: { legend: { labels: { color: "#E2E8F0", boxWidth: 12 } } },
        scales: {
          x: { grid: { display: false }, ticks: { ...commonTick, maxTicksLimit: 10 } },
          y: {
            position: "left",
            beginAtZero: true,
            grid: commonGrid,
            ticks: { ...commonTick, stepSize: 1 },
          },
          y1: {
            position: "right",
            beginAtZero: true,
            grid: { display: false },
            ticks: { ...commonTick, callback: (v) => fmtCompact(v) },
          },
        },
      },
    });

    // 4. Engagement Breakdown
    destroyChart("engagement");
    const totals = charts.engagement_breakdown?.totals || {};
    state.charts.engagement = new Chart($("chart-engagement-breakdown"), {
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
            borderColor: "#111827",
            borderWidth: 2,
          },
        ],
      },
      options: {
        responsive: true,
        maintainAspectRatio: false,
        cutout: "62%",
        plugins: {
          legend: {
            position: "bottom",
            labels: { color: "#CBD5E1", boxWidth: 10, font: { size: 11 } },
          },
        },
      },
    });
  }

  function renderMediaBreakdown(items) {
    const container = $("media-breakdown-list");
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
    container.innerHTML = "";
    if (!heatmap || !heatmap.matrix) return;

    $("heatmap-best-badge").textContent =
      `Peak: ${heatmap.best_day} ${heatmap.best_hour_label} (${fmtCompact(heatmap.best_slot_avg_views)} avg)`;

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
        td.style.backgroundColor = heatColor(cell.avg_views, maxAvg);
        td.title = `${cell.day} ${cell.hour_label} UTC — ${cell.post_count} post(s), avg ${fmtInt(Math.round(cell.avg_views))} views`;
        tr.appendChild(td);
      }
      tbody.appendChild(tr);
    }
    table.appendChild(tbody);
    container.appendChild(table);
  }

  function renderPostsTable(posts) {
    const tbody = $("posts-table-body");
    tbody.innerHTML = "";

    if (!posts || !posts.length) {
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

    for (const p of posts) {
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
    $("drawer-meta").textContent =
      `@${detail.handle} · ${(detail.published_at || "").replace("T", " ").replace("Z", " UTC")}`;
    $("drawer-x-link").href = detail.permalink || `https://x.com/${detail.handle}/status/${detail.post_id}`;
    $("drawer-text").textContent = detail.text || "";

    const badges = $("drawer-badges");
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

    $("dk-views").textContent = fmtInt(detail.views);
    $("dk-vel").textContent = `+${fmtInt(detail.velocity_24h || 0)}`;
    $("dk-er").textContent = `${Number(detail.engagement_rate || 0).toFixed(2)}%`;
    $("dk-hs").textContent = Number(detail.high_signal_score || 0).toFixed(1);
    $("dk-likes").textContent = fmtInt(detail.likes);
    $("dk-bm").textContent = fmtInt(detail.bookmarks);

    const snaps = detail.snapshots || [];
    if (hasChartJs()) {
      destroyChart("postSnapshots");
      state.charts.postSnapshots = new Chart($("chart-post-snapshots"), {
        type: "line",
        data: {
          labels: snaps.map((s) => s.captured_at.slice(5, 16).replace("T", " ")),
          datasets: [
            {
              label: "Cumulative Views",
              data: snaps.map((s) => s.views),
              borderColor: "#38BDF8",
              backgroundColor: "rgba(56, 189, 248, 0.16)",
              fill: true,
              tension: 0.28,
              borderWidth: 2,
            },
          ],
        },
        options: {
          responsive: true,
          maintainAspectRatio: false,
          plugins: { legend: { display: false } },
          scales: {
            x: {
              grid: { display: false },
              ticks: { color: "#94A3B8", font: { family: "JetBrains Mono", size: 10 }, maxTicksLimit: 6 },
            },
            y: {
              grid: { color: "rgba(36, 48, 68, 0.55)" },
              ticks: { color: "#94A3B8", font: { family: "JetBrains Mono", size: 10 }, callback: (v) => fmtCompact(v) },
            },
          },
        },
      });
    }

    const tbody = $("drawer-snapshots-body");
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

    $("drawer-backdrop").classList.remove("hidden");
    $("post-drawer").classList.remove("hidden");
  }

  function closePostDrawer() {
    $("drawer-backdrop").classList.add("hidden");
    $("post-drawer").classList.add("hidden");
  }

  async function triggerSyncNow() {
    if (!state.handle) return;
    const btn = $("btn-sync-now");
    const strip = $("sync-progress-strip");
    const fill = $("sync-progress-fill");
    const text = $("sync-progress-text");
    const dot = btn.querySelector(".sync-dot");

    btn.disabled = true;
    dot.classList.add("pulsing");
    $("sync-btn-label").textContent = "Syncing...";
    strip.classList.remove("hidden");
    fill.style.width = "15%";
    text.textContent = `Fetching @${state.handle} via FxTwitter v2...`;

    try {
      await apiJson("/api/sync", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ handle: state.handle, background: true }),
      });
      const finalStatus = await pollSyncUntilDone(fill, text, state.handle);
      fill.style.width = "100%";
      const res = Array.isArray(finalStatus.last_result)
        ? finalStatus.last_result[0] || {}
        : finalStatus.last_result || {};
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
    } catch (err) {
      showToast(`Sync failed: ${err.message}`, true);
    } finally {
      btn.disabled = false;
      dot.classList.remove("pulsing");
      $("sync-btn-label").textContent = "Sync Now";
      setTimeout(() => strip.classList.add("hidden"), 1500);
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
      await loadAccounts(res.handle);
      await refreshDashboard();
    } catch (err) {
      showToast(`Import failed: ${err.message}`, true);
    }
  }

  function wireEvents() {
    $("handle-select").addEventListener("change", async (e) => {
      state.handle = e.target.value;
      await loadAccounts(state.handle);
      await refreshDashboard();
    });

    $("inline-add-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("inline-handle-input");
      await addHandle(input.value, input);
    });

    $("empty-add-form").addEventListener("submit", async (e) => {
      e.preventDefault();
      const input = $("empty-handle-input");
      await addHandle(input.value, input);
    });

    $("btn-remove-handle").addEventListener("click", async () => {
      if (state.handle) {
        await removeHandle(state.handle);
      }
    });

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
          customBar.classList.remove("hidden");
          if (!$("custom-start").value || !$("custom-end").value) {
            const end = new Date();
            const start = new Date(end.getTime() - 14 * 86400 * 1000);
            $("custom-start").value = start.toISOString().slice(0, 10);
            $("custom-end").value = end.toISOString().slice(0, 10);
          }
          state.startDate = $("custom-start").value;
          state.endDate = $("custom-end").value;
        } else {
          customBar.classList.add("hidden");
        }
        await refreshDashboard();
      });
    });

    $("btn-apply-custom").addEventListener("click", async () => {
      state.startDate = $("custom-start").value;
      state.endDate = $("custom-end").value;
      if (state.startDate && state.endDate) {
        await refreshDashboard();
      }
    });

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
    $("post-search").addEventListener("input", (e) => {
      state.search = e.target.value;
      clearTimeout(searchTimer);
      searchTimer = setTimeout(refreshDashboard, 180);
    });

    $("sort-by").addEventListener("change", async (e) => {
      state.sortBy = e.target.value;
      await refreshDashboard();
    });

    $("btn-sort-dir").addEventListener("click", async () => {
      state.sortDir = state.sortDir === "desc" ? "asc" : "desc";
      $("btn-sort-dir").textContent = state.sortDir.toUpperCase();
      await refreshDashboard();
    });

    $("btn-sync-now").addEventListener("click", triggerSyncNow);

    $("btn-import").addEventListener("click", () => $("import-file-input").click());
    $("btn-empty-import").addEventListener("click", () => $("import-file-input").click());
    $("import-file-input").addEventListener("change", async (e) => {
      const file = e.target.files?.[0];
      e.target.value = "";
      await handleFileImport(file);
    });

    $("btn-export-csv").addEventListener("click", () => {
      if (!state.handle) return;
      window.location.href = `/api/export?handle=${encodeURIComponent(state.handle)}&format=csv`;
    });

    $("btn-export-json").addEventListener("click", () => {
      if (!state.handle) return;
      window.location.href = `/api/export?handle=${encodeURIComponent(state.handle)}&format=json`;
    });

    $("btn-close-drawer").addEventListener("click", closePostDrawer);
    $("drawer-backdrop").addEventListener("click", closePostDrawer);
    document.addEventListener("keydown", (e) => {
      if (e.key === "Escape") {
        closePostDrawer();
      }
    });
  }

  async function init() {
    wireEvents();
    try {
      const hasAccounts = await loadAccounts();
      if (hasAccounts) {
        await refreshDashboard();
      }
    } catch (err) {
      showToast(`Failed to load dashboard: ${err.message}`, true);
    }
  }

  document.addEventListener("DOMContentLoaded", init);
})();
