/* Twitch Drops Miner - modern UI
 * State arrives from Python via TDM.batch([...events]).
 * User actions go back through window.pywebview.api.* (fire-and-forget).
 */
(function () {
  "use strict";

  const $ = (sel) => document.querySelector(sel);
  const $$ = (sel) => Array.from(document.querySelectorAll(sel));

  const S = {
    status: "",
    state: "pickaxe",
    ws: [],
    login: null,
    progress: null,
    channels: { rows: [], watching: null, selected: null },
    campaigns: new Map(),
    settings: null,
    help: { invalidate_enabled: false },
    filters: { not_linked: false, upcoming: true, expired: false, excluded: false, finished: false },
    filtersTouched: false,
    lastClaimed: null,
  };

  const api = () => (window.pywebview && window.pywebview.api) || null;
  const call = (name, ...args) => {
    const a = api();
    if (a && typeof a[name] === "function") a[name](...args);
  };

  const esc = (s) => String(s ?? "").replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
  const pct = (v) => `${(Math.max(0, Math.min(1, v || 0)) * 100).toFixed(1)}%`;
  const initial = (s) => (String(s || "?").trim()[0] || "?").toUpperCase();
  const num = (n) => (n == null ? "" : Number(n).toLocaleString("pt-BR"));

  /* ---------- Navigation ---------- */
  const VIEW_TITLES = {
    dashboard: "Painel", campaigns: "Campanhas", channels: "Canais",
    settings: "Configurações", about: "Sobre",
  };
  function showView(name) {
    $$(".nav-item").forEach((b) => b.classList.toggle("active", b.dataset.view === name));
    $$(".view").forEach((v) => v.classList.toggle("active", v.id === `view-${name}`));
    $("#view-title").textContent = VIEW_TITLES[name] || "";
    if (name === "campaigns") call("inv_refresh");
  }
  $$(".nav-item").forEach((b) => b.addEventListener("click", () => showView(b.dataset.view)));

  /* ---------- Theme ---------- */
  function setTheme(theme) {
    document.documentElement.dataset.theme = theme === "light" ? "light" : "dark";
  }

  /* ---------- Status / state ---------- */
  const STATE_TEXT = {
    pickaxe: "Iniciando", active: "Minerando", idle: "Ocioso",
    maint: "Atualizando", error: "Erro",
  };
  function renderStatus() {
    $("#status-text").textContent = S.status || "—";
    const busy = S.state !== "active" && S.state !== "error" && S.state !== "idle";
    $("#status-spinner").classList.toggle("off", !busy);
    const pill = $("#state-pill");
    pill.className = `state-pill ${S.state}`;
    $("#state-text").textContent = STATE_TEXT[S.state] || S.state;
    $("#sum-state").className = `sum-state ${S.state}`;
    $("#sum-state-text").textContent = (STATE_TEXT[S.state] || S.state).toUpperCase();
    $("#sb-engine").textContent = (STATE_TEXT[S.state] || S.state).toUpperCase();
  }

  /* ---------- Session ---------- */
  let sessionStart = Date.now();
  let sessionClaimed = 0;
  function tickSession() {
    const sec = Math.max(0, Math.floor((Date.now() - sessionStart) / 1000));
    const h = Math.floor(sec / 3600), m = Math.floor(sec / 60) % 60, s2 = sec % 60;
    const p2 = (n) => String(n).padStart(2, "0");
    $("#sum-session").textContent = `${p2(h)}h ${p2(m)}m ${p2(s2)}s`;
    $("#sb-session").textContent = `${p2(h)}:${p2(m)}:${p2(s2)}`;
  }
  setInterval(tickSession, 1000);
  function renderClaimed() {
    $("#sum-claimed").textContent = sessionClaimed;
    $("#sb-claimed").textContent = sessionClaimed;
  }

  /* ---------- Login ---------- */
  function renderLogin() {
    const l = S.login;
    if (!l) return;
    const card = $("#login-card");
    const needs = !l.logged_in && (l.awaiting_press || l.mode);
    card.classList.toggle("hidden", !needs);
    $("#account-name").textContent = l.logged_in ? `ID ${l.user_id}` : "Desconectado";
    const av = $("#account-avatar");
    av.classList.toggle("on", !!l.logged_in);
    av.textContent = l.logged_in ? "✓" : "?";

    const btn = $("#btn-login");
    const winBtn = $("#btn-login-window");
    const codeWrap = $("#login-code-wrap");
    codeWrap.classList.toggle("hidden", l.mode !== "code");
    if (l.mode === "code") {
      $("#login-code").textContent = l.code || "--------";
      $("#login-desc").textContent = "Digite este código na página de ativação da Twitch (pode ser até pelo celular, em twitch.tv/activate).";
      btn.classList.add("hidden");
      winBtn.classList.remove("hidden");
      winBtn.textContent = "Abrir página de ativação";
    } else if (l.mode === "browser") {
      $("#login-desc").textContent = "A janela de login da Twitch está aberta. Entre com a sua conta nela — o programa continua sozinho quando terminar.";
      btn.classList.add("hidden");
      winBtn.classList.remove("hidden");
      winBtn.textContent = "Mostrar janela de login";
    } else {
      $("#login-desc").textContent = "O login acontece numa janela do próprio programa. Sua senha não passa pelo app — ela vai direto para a Twitch.";
      btn.classList.remove("hidden");
      btn.disabled = !l.awaiting_press;
      btn.textContent = "Entrar com a Twitch";
      winBtn.classList.add("hidden");
    }
    $("#login-status").textContent = l.status || "";
  }
  $("#btn-login").addEventListener("click", () => call("login_press"));
  $("#btn-login-window").addEventListener("click", () => call("login_open_window"));

  /* ---------- Now mining ---------- */
  function renderProgress() {
    const p = S.progress;
    $("#hero-empty").classList.toggle("hidden", !!p);
    $("#hero-content").classList.toggle("hidden", !p);
    if (!p) return;
    const art = $("#hero-art");
    if (art.dataset.src !== p.campaign_image) {
      art.src = p.campaign_image;
      art.dataset.src = p.campaign_image;
      $("#hero-bg").style.backgroundImage = `url("${p.campaign_image}")`;
    }
    $("#hero-game").textContent = p.game;
    $("#hero-campaign").textContent = p.campaign;
    const rewardsKey = JSON.stringify(p.benefits);
    const rewards = $("#hero-rewards");
    if (rewards.dataset.key !== rewardsKey) {
      rewards.dataset.key = rewardsKey;
      rewards.innerHTML = p.benefits.map((b) => `
        <div class="reward"><img src="${esc(b.image)}" alt="" referrerpolicy="no-referrer"><span title="${esc(b.name)}">${esc(b.name)}</span></div>
      `).join("");
    }
    $("#drop-pct").textContent = pct(p.drop_progress);
    $("#drop-minutes").textContent = `${p.drop_minutes}/${p.drop_required} min`;
    $("#drop-bar").style.width = pct(p.drop_progress);
    $("#drop-remaining").textContent = p.drop_remaining;
    $("#camp-pct").textContent = `${pct(p.campaign_progress)} concluído`;
    $("#camp-claimed").textContent = `${p.claimed} de ${p.total}`;
    $("#camp-segments").innerHTML = (p.segments || []).map((g) => `
      <div class="seg ${g.state}" title="${esc(g.name)}">${g.state === "current" ? `<div style="width:${pct(g.progress)}"></div>` : ""}</div>
    `).join("");
    $("#camp-remaining").textContent = p.campaign_remaining;
    $("#camp-ends").textContent = p.ends;
  }

  /* ---------- Watching + stats ---------- */
  function renderWatching() {
    const { rows, watching } = S.channels;
    const row = rows.find((r) => r.iid === watching);
    $("#watch").classList.toggle("hidden", !row);
    $("#watch-empty").classList.toggle("hidden", !!row);
    if (row) {
      $("#watch-name").textContent = row.name;
      $("#watch-avatar").textContent = initial(row.name);
      $("#watch-viewers").textContent = row.viewers != null ? num(row.viewers) : "—";
      $("#watch-game").textContent = row.game || "";
      $("#watch-drops").textContent = row.drops ? "ATIVOS" : "NÃO";
      $("#btn-open-channel").dataset.url = `https://www.twitch.tv/${row.name}`;
    }
    const online = rows.filter((r) => r.status_key === "online").length;
    $("#stat-online").textContent = online;
    $("#watch-pool").textContent = Math.max(0, online - (row ? 1 : 0));
    $("#sb-channels").textContent = `${online}/${rows.length}`;
    $("#nav-channels-count").textContent = rows.length || "";
  }
  $("#btn-goto-channels").addEventListener("click", () => showView("channels"));
  $("#btn-sync").addEventListener("click", () => {
    call("reload");
    toast("Atualizando", "Buscando campanhas e inventário de novo.");
  });
  $("#btn-open-channel").addEventListener("click", (e) => {
    const url = e.currentTarget.dataset.url;
    if (url) call("open_url", url);
  });

  function renderStats() {
    let active = 0, claimed = 0;
    for (const c of S.campaigns.values()) {
      if (c.status_key === "active" && c.eligible && !c.sub_only) active++;
      claimed += c.claimed || 0;
    }
    $("#stat-campaigns").textContent = active;
  }

  /* ---------- Websockets ---------- */
  function renderWs() {
    const list = $("#ws-list");
    const okCount = S.ws.filter((w) => /connected|conectad/i.test(w.status) && !/dis|des/i.test(w.status)).length;
    const sock = $("#sb-socket");
    sock.textContent = S.ws.length ? (okCount ? "CONECTADO" : "RECONECTANDO") : "—";
    sock.className = okCount ? "green" : "";
    if (!S.ws.length) {
      list.innerHTML = '<span class="muted">Sem conexões</span>';
      return;
    }
    list.innerHTML = S.ws.map((w) => {
      const ok = /connected|conectad/i.test(w.status) && !/dis|des/i.test(w.status);
      return `<div class="ws-item ${ok ? "ok" : "warn"}">
        <span class="dot"></span>
        <span class="ws-status">Conexão ${w.id} · ${esc(w.status)}</span>
        <span class="ws-topics">${w.topics}/${w.limit}</span>
      </div>`;
    }).join("");
  }

  /* ---------- Log ---------- */
  const LOG_MAX = 400;
  function addLog(line, scroll = true) {
    const log = $("#log");
    const atBottom = log.scrollHeight - log.scrollTop - log.clientHeight < 40;
    const div = document.createElement("div");
    div.className = "log-line";
    const m = /^(\d{1,2}:\d{2}:\d{2})\s{2}([\s\S]*)$/.exec(line);
    const time = m ? m[1] : "";
    const msg = (m ? m[2] : line).replace(/^(INFO|DEBUG|CALL|WARNING):\s*/, "").trim();
    if (!msg) return;
    let kind = "info", label = "INFO";
    if (/claim|resgat/i.test(msg)) { kind = "claim"; label = "RESGATE"; }
    else if (/error|erro|fatal|traceback|exception/i.test(msg)) { kind = "err"; label = "ERRO"; }
    else if (/watching|assistindo|switch|troc|offline|online/i.test(msg)) { kind = "switch"; label = "CANAL"; }
    else if (/drop|campa|progress|minut/i.test(msg)) { kind = "drop"; label = "DROP"; }
    div.classList.add(`lk-${kind}`);
    div.innerHTML = `<span class="t">${time ? `[${esc(time)}]` : ""}</span><span class="k k-${kind}">${label}</span><span class="m">${esc(msg)}</span>`;
    log.appendChild(div);
    while (log.childElementCount > LOG_MAX) log.firstElementChild.remove();
    if (scroll && atBottom && $("#log-autoscroll").checked) log.scrollTop = log.scrollHeight;
  }
  $("#btn-log-clear").addEventListener("click", () => { $("#log").innerHTML = ""; });

  /* ---------- Campaigns ---------- */
  function campaignVisible(c) {
    const st = S.settings;
    const f = S.filters;
    if (c.sub_only) return false;
    if (!(f.not_linked || c.eligible)) return false;
    const statusOk = c.status_key === "active"
      || (f.upcoming && c.status_key === "upcoming")
      || (f.expired && c.status_key === "expired");
    if (!statusOk) return false;
    if (st && !f.excluded) {
      const priorityOnly = st.priority_mode === "PRIORITY_ONLY";
      const okGame = (!st.exclude.includes(c.game) && !priorityOnly) || st.priority.includes(c.game);
      if (!okGame) return false;
    }
    if (!f.finished && c.finished) return false;
    const q = $("#campaign-search").value.trim().toLowerCase();
    if (q && !(`${c.game} ${c.name}`.toLowerCase().includes(q))) return false;
    return true;
  }

  function dropHtml(d) {
    const b = d.benefits[0] || { name: "", image: "" };
    const names = d.benefits.map((x) => x.name).join(", ");
    const mini = d.state === "progress"
      ? `<div class="mini"><div style="width:${pct(d.progress)}"></div></div>` : "";
    return `<div class="drop ${d.state}" title="${esc(names)}${d.extra ? "\n" + esc(d.extra) : ""}">
      <img src="${esc(b.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="drop-name">${esc(b.name)}</div>
      <div class="drop-text">${esc(d.text)}</div>${mini}
    </div>`;
  }

  function campaignHtml(c) {
    const statusClass = { active: "green", upcoming: "yellow", expired: "red" }[c.status_key] || "";
    const linkTag = c.eligible
      ? '<span class="tag green">Vinculada</span>'
      : `<span class="tag red link" data-url="${esc(c.link_url)}" title="Clique para vincular">Não vinculada ↗</span>`;
    const channels = c.channels.length
      ? (c.channels.length <= 3 ? c.channels.join(", ") : `${c.channels.slice(0, 3).join(", ")} +${c.channels.length - 3}`)
      : "Todos os canais";
    const when = c.status_key === "upcoming" ? `Começa ${esc(c.starts)}` : `Termina ${esc(c.ends)}`;
    return `<div class="card campaign">
      <img class="campaign-art" src="${esc(c.image)}" alt="" loading="lazy" referrerpolicy="no-referrer">
      <div class="campaign-body">
        <div class="campaign-game" title="${esc(c.game)}">${esc(c.game)}</div>
        <div class="campaign-name" title="${esc(c.name)}">${esc(c.name)}</div>
        <div class="tags">
          <span class="tag ${statusClass}">${esc(c.status)}</span>
          ${linkTag}
          <span class="tag">${c.claimed}/${c.total} drops</span>
        </div>
        <div class="campaign-meta">${when} · ${esc(channels)}</div>
        <div class="drops-row">${c.drops.map(dropHtml).join("")}</div>
      </div>
    </div>`;
  }

  let campaignsTimer = null;
  function scheduleCampaigns() {
    if (campaignsTimer) return;
    campaignsTimer = setTimeout(() => { campaignsTimer = null; renderCampaigns(); }, 150);
  }
  function renderCampaigns() {
    const list = Array.from(S.campaigns.values()).filter(campaignVisible);
    const order = { active: 0, upcoming: 1, expired: 2 };
    list.sort((a, b) => (order[a.status_key] - order[b.status_key])
      || (Number(b.eligible) - Number(a.eligible)) || a.game.localeCompare(b.game));
    $("#campaign-grid").innerHTML = list.map(campaignHtml).join("");
    $("#campaigns-empty").classList.toggle("hidden", list.length > 0);
    renderStats();
    $("#nav-campaigns-count").textContent = list.length || "";
  }
  $("#campaign-grid").addEventListener("click", (e) => {
    const t = e.target.closest("[data-url]");
    if (t) call("open_url", t.dataset.url);
  });
  $$("#filters .chip").forEach((chip) => {
    chip.addEventListener("click", () => {
      const k = chip.dataset.filter;
      S.filters[k] = !S.filters[k];
      S.filtersTouched = true;
      chip.classList.toggle("active", S.filters[k]);
      renderCampaigns();
    });
  });
  $("#campaign-search").addEventListener("input", scheduleCampaigns);

  /* ---------- Channels ---------- */
  function renderChannels() {
    const { rows, watching, selected } = S.channels;
    const q = $("#channel-search").value.trim().toLowerCase();
    const filtered = q ? rows.filter((r) => `${r.name} ${r.game}`.toLowerCase().includes(q)) : rows;
    $("#channel-rows").innerHTML = filtered.map((r) => `
      <tr data-iid="${esc(r.iid)}" class="${r.iid === selected ? "selected" : ""} ${r.iid === watching ? "watching" : ""}">
        <td><div class="ch-name">${esc(r.name)}${r.iid === watching ? '<span class="ch-watching">ASSISTINDO</span>' : ""}</div></td>
        <td><span class="status-dot ${r.status_key}">${esc(r.status)}</span></td>
        <td>${esc(r.game) || '<span class="no">—</span>'}</td>
        <td class="center">${r.drops ? '<span class="yes">✓</span>' : '<span class="no">✕</span>'}</td>
        <td class="right">${num(r.viewers)}</td>
      </tr>`).join("");
    $("#channels-empty").classList.toggle("hidden", rows.length > 0);
    $("#btn-switch").disabled = !selected;
    renderWatching();
  }
  $("#channel-rows").addEventListener("click", (e) => {
    const tr = e.target.closest("tr[data-iid]");
    if (!tr) return;
    const iid = tr.dataset.iid;
    const next = S.channels.selected === iid ? null : iid;
    S.channels.selected = next;
    renderChannels();
    call("select_channel", next);
  });
  $("#btn-switch").addEventListener("click", () => call("switch_channel"));
  $("#channel-search").addEventListener("input", renderChannels);

  /* ---------- Settings ---------- */
  const ICON_UP = '<svg viewBox="0 0 24 24"><path d="M7.4 15.4L12 10.8l4.6 4.6L18 14l-6-6-6 6z"/></svg>';
  const ICON_DOWN = '<svg viewBox="0 0 24 24"><path d="M7.4 8.6L12 13.2l4.6-4.6L18 10l-6 6-6-6z"/></svg>';
  const ICON_DEL = '<svg viewBox="0 0 24 24"><path d="M19 6.4L17.6 5 12 10.6 6.4 5 5 6.4l5.6 5.6L5 17.6 6.4 19l5.6-5.6 5.6 5.6 1.4-1.4-5.6-5.6z"/></svg>';

  function renderSettings() {
    const st = S.settings;
    if (!st) return;
    $$("[data-setting]").forEach((el) => { el.checked = !!st[el.dataset.setting]; });
    const pm = $("#priority-mode");
    pm.innerHTML = st.priority_modes.map((m) => `<option value="${esc(m.value)}">${esc(m.label)}</option>`).join("");
    pm.value = st.priority_mode;
    const lang = $("#language");
    lang.innerHTML = st.languages.map((l) => `<option>${esc(l)}</option>`).join("");
    lang.value = st.language;
    const proxy = $("#proxy");
    if (document.activeElement !== proxy) proxy.value = st.proxy || "";

    $("#priority-list").innerHTML = st.priority.length
      ? st.priority.map((g, i) => `<li data-i="${i}">
          <span class="pos">${i + 1}</span><span class="name" title="${esc(g)}">${esc(g)}</span>
          <span class="acts">
            <button data-act="up" title="Subir">${ICON_UP}</button>
            <button data-act="down" title="Descer">${ICON_DOWN}</button>
            <button data-act="del" class="del" title="Remover">${ICON_DEL}</button>
          </span></li>`).join("")
      : '<li class="empty">Nenhum jogo. Sem prioridade, o modo “Somente a lista” não minera nada.</li>';

    $("#exclude-list").innerHTML = st.exclude.length
      ? st.exclude.map((g) => `<li data-name="${esc(g)}">
          <span class="name" title="${esc(g)}">${esc(g)}</span>
          <span class="acts"><button data-act="del" class="del" title="Remover">${ICON_DEL}</button></span></li>`).join("")
      : '<li class="empty">Nenhum jogo excluído.</li>';

    const mob = st.mobile || {};
    $("#mobile-body").classList.toggle("hidden", !mob.running);
    $("#mobile-qr").innerHTML = mob.qr || "";
    $("#mobile-url").textContent = mob.url || "";
    const mErr = $("#mobile-error");
    mErr.classList.toggle("hidden", !mob.error);
    mErr.textContent = mob.error || "";
    st.mobile_access = !!mob.enabled;
    const ts = mob.tailscale || {};
    $("#remote").classList.toggle("hidden", !mob.running);
    $("#ts-missing").classList.toggle("hidden", !!(ts.installed || ts.url));
    $("#ts-stopped").classList.toggle("hidden", !(ts.installed && !ts.running));
    $("#ts-ready").classList.toggle("hidden", !ts.url);
    $("#ts-qr").innerHTML = ts.qr || "";
    $("#ts-url").textContent = ts.url || "";
    $("#ts-https-on").classList.toggle("hidden", !ts.https);
    const httpsBtn = $("#btn-ts-https");
    httpsBtn.classList.toggle("hidden", !ts.installed);
    httpsBtn.textContent = ts.https ? "Desativar HTTPS" : "Ativar HTTPS";
    httpsBtn.dataset.on = ts.https ? "1" : "";
    const tsErr = $("#ts-error");
    tsErr.classList.toggle("hidden", !ts.error);
    tsErr.textContent = ts.error || "";
    const mChk = document.querySelector('[data-setting="mobile_access"]');
    if (mChk) mChk.checked = !!mob.enabled;

    $("#games-list").innerHTML = st.games.map((g) => `<option value="${esc(g)}">`).join("");

    if (!S.filtersTouched) {
      S.filters.not_linked = st.priority_mode === "PRIORITY_ONLY";
      $$("#filters .chip").forEach((c) => c.classList.toggle("active", !!S.filters[c.dataset.filter]));
    }
    scheduleCampaigns();
  }

  function markDirty() { $("#apply-bar").classList.add("show"); }

  $$("[data-setting]").forEach((el) => {
    el.addEventListener("change", () => {
      call("set_setting", el.dataset.setting, el.checked);
      if (el.dataset.setting === "dark_mode") setTheme(el.checked ? "dark" : "light");
    });
  });
  $("#priority-mode").addEventListener("change", (e) => { call("set_setting", "priority_mode", e.target.value); markDirty(); });
  $("#language").addEventListener("change", (e) => {
    call("set_setting", "language", e.target.value);
    toast("Idioma alterado", "Reinicie o programa para aplicar.");
  });
  $("#proxy").addEventListener("change", (e) => call("set_proxy", e.target.value));

  function addFrom(inputSel, apiName) {
    const input = $(inputSel);
    const v = input.value.trim();
    if (!v) return;
    call(apiName, v);
    input.value = "";
    markDirty();
  }
  $("#btn-priority-add").addEventListener("click", () => addFrom("#priority-input", "priority_add"));
  $("#priority-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addFrom("#priority-input", "priority_add"); });
  $("#btn-exclude-add").addEventListener("click", () => addFrom("#exclude-input", "exclude_add"));
  $("#exclude-input").addEventListener("keydown", (e) => { if (e.key === "Enter") addFrom("#exclude-input", "exclude_add"); });

  $("#priority-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    const li = e.target.closest("li[data-i]");
    if (!btn || !li) return;
    const i = Number(li.dataset.i);
    if (btn.dataset.act === "up") call("priority_move", i, 1);
    else if (btn.dataset.act === "down") call("priority_move", i, -1);
    else if (btn.dataset.act === "del") call("priority_delete", i);
    markDirty();
  });
  $("#exclude-list").addEventListener("click", (e) => {
    const btn = e.target.closest("button[data-act]");
    const li = e.target.closest("li[data-name]");
    if (!btn || !li) return;
    call("exclude_delete", li.dataset.name);
    markDirty();
  });
  $("#btn-reload").addEventListener("click", () => {
    call("reload");
    $("#apply-bar").classList.remove("show");
    toast("Recarregando", "Buscando campanhas com as novas configurações.");
  });
  $("#btn-ts-refresh").addEventListener("click", () => { call("tailscale", "refresh"); toast("Verificando", "Procurando o Tailscale…"); });
  $("#btn-ts-https").addEventListener("click", (e) => {
    const on = !!e.currentTarget.dataset.on;
    call("tailscale", on ? "https_off" : "https_on");
    toast(on ? "Desativando HTTPS" : "Ativando HTTPS", on ? "" : "Pode levar alguns segundos na primeira vez.");
  });
  $("#remote").addEventListener("click", (e) => {
    const b = e.target.closest("[data-url]");
    if (b) call("open_url", b.dataset.url);
  });
  $("#btn-mobile-token").addEventListener("click", () => {
    if (confirm("Gerar um link novo? O celular vai precisar escanear o QR code de novo.")) call("mobile_new_token");
  });
  $("#btn-test-notify").addEventListener("click", () => call("test_notification"));

  /* ---------- About / misc ---------- */
  $$("[data-url].link-btn").forEach((b) => b.addEventListener("click", () => call("open_url", b.dataset.url)));
  $("#btn-invalidate").addEventListener("click", () => {
    if (confirm("Sair da conta da Twitch neste PC?")) call("invalidate");
  });
  $("#btn-tray").addEventListener("click", () => call("minimize_to_tray"));

  function renderHelp() {
    $("#btn-invalidate").disabled = !S.help.invalidate_enabled;
  }

  /* ---------- Unlinked campaigns ---------- */
  function renderAlerts() {
    const list = S.alerts || [];
    $("#link-alert").classList.toggle("hidden", !list.length);
    $("#link-alert-list").innerHTML = list.map((a) => `
      <div class="link-row">
        <img src="${esc(a.image)}" alt="" referrerpolicy="no-referrer">
        <div class="grow">
          <div class="lr-game">${esc(a.game)}</div>
          <div class="lr-name">${esc(a.name)} · termina ${esc(a.ends)}</div>
        </div>
        <div class="lr-acts">
          <button class="btn btn-primary btn-sm" data-link="${esc(a.link_url)}">Vincular conta ↗</button>
          <button class="btn btn-ghost btn-sm" data-relinked="1" title="Busca as campanhas de novo">Já vinculei</button>
        </div>
      </div>`).join("");
  }
  $("#link-alert-list").addEventListener("click", (e) => {
    const link = e.target.closest("[data-link]");
    if (link) { call("open_url", link.dataset.link); return; }
    if (e.target.closest("[data-relinked]")) {
      call("reload");
      toast("Atualizando", "Conferindo o vínculo e buscando as campanhas de novo.");
    }
  });

  /* ---------- Toasts ---------- */
  function toast(title, text) {
    const el = document.createElement("div");
    el.className = "toast";
    el.innerHTML = `<b>${esc(title)}</b><span>${esc(text)}</span>`;
    $("#toasts").appendChild(el);
    setTimeout(() => el.remove(), 4500);
  }

  /* ---------- Event intake ---------- */
  function applyFull(d) {
    if (d.title) {
      const m = /v([\w.]+)/.exec(d.title);
      $("#version").textContent = m ? `v${m[1]}` : "Twitch";
      $("#footnote").textContent = `${d.title} · interface redesenhada`;
    }
    if (d.started_at) sessionStart = d.started_at * 1000;
    sessionClaimed = d.session_claimed || 0;
    tickSession(); renderClaimed();
    S.status = d.status; S.state = d.state; S.ws = d.ws; S.login = d.login;
    S.progress = d.progress; S.channels = d.channels; S.settings = d.settings; S.help = d.help;
    S.campaigns = new Map((d.inventory || []).map((c) => [c.id, c]));
    S.alerts = d.alerts || [];
    renderAlerts();
    $("#log").innerHTML = "";
    (d.log || []).forEach((l) => addLog(l, false));
    $("#log").scrollTop = $("#log").scrollHeight;
    renderStatus(); renderLogin(); renderProgress(); renderWs();
    renderChannels(); renderSettings(); renderHelp(); renderCampaigns();
  }

  const handlers = {
    full: applyFull,
    status: (d) => { S.status = d; renderStatus(); },
    state: (d) => { S.state = d; renderStatus(); },
    theme: setTheme,
    ws: (d) => { S.ws = d; renderWs(); },
    login: (d) => { S.login = d; renderLogin(); },
    progress: (d) => { S.progress = d; renderProgress(); },
    log: (d) => addLog(d),
    channels: (d) => { S.channels = d; renderChannels(); },
    inventory: (d) => { S.campaigns = new Map(d.map((c) => [c.id, c])); scheduleCampaigns(); },
    campaign: (d) => {
      const prev = S.campaigns.get(d.id);
      if (prev && d.claimed > prev.claimed) {
        toast("Drop resgatado! 🎉", `${d.game}: ${d.claimed}/${d.total}`);
      }
      S.campaigns.set(d.id, d);
      scheduleCampaigns();
    },
    settings: (d) => { S.settings = d; renderSettings(); },
    help: (d) => { S.help = d; renderHelp(); },
    alerts: (d) => { S.alerts = d; renderAlerts(); },
    session: (d) => { sessionClaimed = d.claimed || 0; renderClaimed(); },
  };

  window.TDM = {
    batch(events) {
      for (const ev of events) {
        const h = handlers[ev.type];
        if (h) {
          try { h(ev.data); } catch (err) { console.error(ev.type, err); }
        }
      }
    },
  };

  function announceReady() { call("ready"); }
  if (api()) announceReady();
  else window.addEventListener("pywebviewready", announceReady);

  // keyboard: Esc clears selection
  document.addEventListener("keydown", (e) => {
    if (e.key === "Escape" && S.channels.selected) {
      S.channels.selected = null;
      renderChannels();
      call("select_channel", null);
    }
  });
})();
