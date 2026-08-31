const state = { token: sessionStorage.getItem("access_token"), me: null, connections: [], users: [] };
let tiktokConnectionId = null;
const appBasePath = new URL("./", document.baseURI).pathname.replace(/\/$/, "");

const $ = (selector) => document.querySelector(selector);
const clear = (node) => node.replaceChildren();
const text = (value) => document.createTextNode(String(value ?? ""));

function node(tag, options = {}, children = []) {
  const element = document.createElement(tag);
  Object.entries(options).forEach(([key, value]) => {
    if (key === "class") element.className = value;
    else if (key === "text") element.textContent = value;
    else if (key.startsWith("data-")) element.dataset[key.slice(5)] = value;
    else element[key] = value;
  });
  children.forEach((child) => element.append(child));
  return element;
}

function errorText(payload) {
  if (typeof payload?.detail === "string") return payload.detail;
  return "Не удалось выполнить действие.";
}

async function api(path, options = {}) {
  const headers = new Headers(options.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (options.body && !(options.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const resolvedPath = `${appBasePath}/${path.replace(/^\//, "")}`.replace(/^\/{2,}/, "/");
  const response = await fetch(resolvedPath, { ...options, headers });
  const payload = response.status === 204 ? null : await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(errorText(payload));
  return payload;
}

function setNotice(selector, message, isError = false) {
  const target = $(selector);
  target.className = isError ? "error" : "notice";
  target.textContent = message;
}

function showLoggedIn(loggedIn) {
  $("#login-card").classList.toggle("hidden", loggedIn);
  $("#app").classList.toggle("hidden", !loggedIn);
  $("#logout").classList.toggle("hidden", !loggedIn);
}

function showPage(name) {
  document.querySelectorAll(".page").forEach((page) => page.classList.toggle("hidden", page.id !== name));
  document.querySelectorAll(".tab").forEach((tab) => tab.classList.toggle("active", tab.dataset.page === name));
}

function renderConnections() {
  const list = $("#connections-list");
  clear(list);
  if (!state.connections.length) {
    list.append(node("span", { class: "muted", text: "Площадки пока не добавлены." }));
  }
  state.connections.forEach((connection) => {
    const head = node("div", { class: "item-head" }, [
      node("strong", { text: connection.label }),
      node("span", { class: "pill", text: `${connection.platform} · ${connection.status}` }),
    ]);
    list.append(node("article", { class: "item" }, [head, node("span", { class: "muted", text: connection.external_id })]));
  });
  const targets = $("#post-targets");
  clear(targets);
  state.connections.filter((item) => item.status === "connected").forEach((connection) => {
    const checkbox = node("input", { type: "checkbox", value: connection.id, name: "connection" });
    targets.append(node("label", { class: "target" }, [checkbox, text(connection.label), node("small", { text: connection.platform })]));
  });
  if (!targets.childElementCount) targets.append(node("span", { class: "muted", text: "Сначала добавьте площадку." }));
  void updateTikTokOptions();
}

function formatDate(value) {
  return new Date(value).toLocaleString("ru-RU", { dateStyle: "medium", timeStyle: "short" });
}

function renderPosts(posts) {
  const list = $("#posts-list");
  clear(list);
  if (!posts.length) {
    list.append(node("span", { class: "muted", text: "Публикаций пока нет." }));
    return;
  }
  posts.forEach((post) => {
    const deliveryList = node("div", { class: "delivery" });
    post.deliveries.forEach((delivery) => {
      const statusNames = {
        queued: "в очереди", processing: "отправка", provider_processing: "обрабатывает площадка",
        published: "опубликовано", failed: "ошибка", unknown: "нужно проверить",
      };
      const destination = delivery.destination
        ? `${delivery.destination.label} · ${delivery.destination.platform}` : "Площадка";
      const label = node("span", {
        class: "pill",
        text: `${destination} · ${statusNames[delivery.status] || delivery.status} · попыток: ${delivery.attempts}`,
      });
      deliveryList.append(label);
      if (delivery.status === "failed" || delivery.status === "unknown") {
        const retry = node("button", { class: "secondary", type: "button", text: "Повторить" });
        retry.addEventListener("click", async () => {
          try { await api(`/v1/deliveries/${delivery.id}/retry`, { method: "POST" }); await refreshPosts(); }
          catch (error) { setNotice("#post-result", error.message, true); }
        });
        deliveryList.append(retry);
      }
    });
    const title = post.content ? post.content.slice(0, 160) : "Пост с медиа";
    const attachments = node("div", { class: "attachment-previews" });
    post.attachments.forEach((attachment) => {
      if (!attachment.media_url) return;
      if (attachment.content_type.startsWith("image/")) {
        attachments.append(node("img", { class: "attachment-preview", src: attachment.media_url, alt: attachment.name }));
      } else if (attachment.content_type.startsWith("video/")) {
        attachments.append(node("video", { class: "attachment-preview", src: attachment.media_url, controls: true, preload: "metadata" }));
      } else {
        attachments.append(node("a", { class: "attachment-file", href: attachment.media_url, target: "_blank", text: attachment.name }));
      }
    });
    const item = node("article", { class: "item" }, [
      node("div", { class: "item-head" }, [node("strong", { text: title }), node("span", { class: "muted", text: formatDate(post.scheduled_at) })]),
      node("p", { class: "post-content", text: post.content || "Без текста" }),
      attachments,
      node("small", { text: `Вложений: ${post.attachments.length}` }),
      deliveryList,
    ]);
    list.append(item);
  });
}

function renderUsers() {
  const list = $("#users-list");
  const connectionSelect = $("#admin-connection-user");
  const credentialSelect = $("#admin-credential-user");
  clear(list); clear(connectionSelect); clear(credentialSelect);
  state.users.forEach((user) => {
    const option = { value: user.id, text: `${user.display_name || user.email} — ${user.email}` };
    connectionSelect.append(node("option", option));
    credentialSelect.append(node("option", option));
    list.append(node("article", { class: "item" }, [
      node("div", { class: "item-head" }, [node("strong", { text: user.display_name || user.email }), node("span", { class: "pill", text: user.is_superuser ? "суперпользователь" : "пользователь" })]),
      node("span", { class: "muted", text: user.email }),
      node("small", { text: user.is_active ? "Активен" : "Отключён" }),
    ]));
  });
  updateAdminConnectionFields();
}

async function refreshPosts() { renderPosts(await api("/v1/posts")); }
async function refreshConnections() { state.connections = await api("/v1/connections"); renderConnections(); }
async function refreshUsers() { state.users = await api("/v1/admin/users"); renderUsers(); }

async function refreshDashboard() {
  state.me = await api("/v1/me");
  $("#identity").textContent = state.me.display_name || state.me.email;
  $("#admin-tab").classList.toggle("hidden", !state.me.is_superuser);
  await Promise.all([refreshConnections(), refreshPosts()]);
  if (state.me.is_superuser) await refreshUsers();
}

function selectedTikTokConnection() {
  const selected = [...document.querySelectorAll("input[name=connection]:checked")].map((item) => item.value);
  return state.connections.find((connection) => selected.includes(connection.id) && connection.platform === "tiktok");
}

function isVideoFile(file) {
  return file.type.startsWith("video/") || /\.(3gp|avi|flv|hevc|m2ts|m4v|mkv|mov|mp4|mpeg|mpg|ts|webm|wmv)$/i.test(file.name);
}

async function updateTikTokOptions() {
  const fieldset = $("#tiktok-options");
  const selected = selectedTikTokConnection();
  fieldset.classList.toggle("hidden", !selected);
  if (!selected) { tiktokConnectionId = null; return; }
  if (tiktokConnectionId === selected.id && !$("#tiktok-privacy").disabled) return;
  const privacy = $("#tiktok-privacy");
  const commercial = $("#tiktok-commercial");
  const selectedFiles = [...$("#post-files").files];
  const hasVideo = selectedFiles.some(isVideoFile);
  const hasPhotosOnly = selectedFiles.length > 0 && !hasVideo;
  document.querySelectorAll(".tiktok-video-option").forEach((element) => element.classList.toggle("hidden", !hasVideo));
  document.querySelectorAll(".tiktok-photo-option").forEach((element) => element.classList.toggle("hidden", !hasPhotosOnly));
  clear(privacy); clear(commercial); privacy.disabled = true; commercial.disabled = true;
  privacy.append(node("option", { text: "Загрузка настроек…" }));
  $("#tiktok-help").textContent = "Загружаем разрешённые настройки этого TikTok-аккаунта…";
  try {
    const info = await api(`/v1/connections/${selected.id}/tiktok/creator-info?media_type=${hasVideo ? "video" : "photo"}`);
    if (selected.id !== selectedTikTokConnection()?.id) return;
    clear(privacy); clear(commercial);
    info.privacy_levels.forEach((level) => privacy.append(node("option", { value: level.value, text: level.label })));
    (info.commercial_content_types.length ? info.commercial_content_types : [{ value: "none", label: "Нет" }])
      .forEach((type) => commercial.append(node("option", { value: type.value, text: type.label })));
    $("#tiktok-allow-comment").checked = Boolean(info.interaction_defaults.allow_comment);
    $("#tiktok-allow-duet").checked = Boolean(info.interaction_defaults.allow_duet);
    $("#tiktok-allow-stitch").checked = Boolean(info.interaction_defaults.allow_stitch);
    $("#tiktok-help").textContent = "Настройки и уровень видимости получены напрямую из подключённого TikTok-аккаунта.";
    tiktokConnectionId = selected.id;
  } catch (error) {
    clear(privacy); privacy.append(node("option", { text: "Не удалось загрузить настройки" }));
    $("#tiktok-help").textContent = error.message;
    setNotice("#post-result", error.message, true);
  } finally {
    privacy.disabled = !privacy.querySelector("option[value]");
    commercial.disabled = !commercial.querySelector("option[value]");
  }
}

$("#post-targets").addEventListener("change", () => void updateTikTokOptions());
$("#post-files").addEventListener("change", () => { tiktokConnectionId = null; void updateTikTokOptions(); });

$("#login-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  setNotice("#login-error", "");
  try {
    const answer = await api("/v1/auth/login", { method: "POST", body: JSON.stringify({ email: $("#login-email").value, password: $("#login-password").value }) });
    state.token = answer.access_token;
    sessionStorage.setItem("access_token", state.token);
    await refreshDashboard();
    showLoggedIn(true);
  } catch (error) { setNotice("#login-error", error.message, true); }
});

$("#logout").addEventListener("click", () => {
  state.token = null; state.me = null; sessionStorage.removeItem("access_token"); showLoggedIn(false); $("#identity").textContent = "";
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => showPage(tab.dataset.page)));

function updateAdminConnectionFields() {
  const provider = $("#admin-connection-provider").value;
  const platform = $("#admin-connection-platform");
  const options = provider === "zernio"
    ? [["instagram", "Instagram"], ["tiktok", "TikTok"]]
    : provider === "telegram" ? [["telegram", "Telegram"]] : [["whatsapp", "WhatsApp"]];
  clear(platform);
  options.forEach(([value, label]) => platform.append(node("option", { value, text: label })));
  const isZernio = provider === "zernio";
  const usesAutomaticTargets = provider === "telegram" || provider === "whapi";
  $("#admin-zernio-account-field").classList.toggle("hidden", !isZernio);
  $("#admin-automatic-target-field").classList.toggle("hidden", !usesAutomaticTargets);
  $("#admin-connection-target-field").classList.toggle("hidden", isZernio || usesAutomaticTargets);
  $("#admin-zernio-account").required = isZernio;
  $("#admin-automatic-target").required = usesAutomaticTargets;
  $("#admin-connection-target").required = !isZernio && !usesAutomaticTargets;
  $("#admin-connection-target").placeholder = provider === "whapi" ? "123…@g.us или …@newsletter" : "@channel или -100…";
  if (isZernio) void loadZernioAccounts();
  if (usesAutomaticTargets) void loadAutomaticTargets();
}

$("#admin-connection-provider").addEventListener("change", updateAdminConnectionFields);
$("#admin-connection-user").addEventListener("change", () => {
  const provider = $("#admin-connection-provider").value;
  if (provider === "zernio") void loadZernioAccounts();
  if (provider === "telegram" || provider === "whapi") void loadAutomaticTargets();
});
$("#admin-connection-platform").addEventListener("change", () => { if ($("#admin-connection-provider").value === "zernio") void loadZernioAccounts(); });
$("#admin-refresh-targets").addEventListener("click", () => void loadAutomaticTargets());
$("#admin-automatic-target").addEventListener("change", (event) => {
  if (!$("#admin-connection-label").value) $("#admin-connection-label").value = event.target.selectedOptions[0]?.dataset.label || "";
});
updateAdminConnectionFields();

async function loadZernioAccounts() {
  const select = $("#admin-zernio-account");
  const userId = $("#admin-connection-user").value;
  const platform = $("#admin-connection-platform").value;
  clear(select);
  select.disabled = true;
  if (!userId || !platform) return;
  select.append(node("option", { text: "Загрузка аккаунтов…" }));
  try {
    const accounts = await api(`/v1/admin/users/${userId}/zernio/accounts`);
    const matching = accounts.filter((account) => account.platform === platform && account.status === "connected");
    clear(select);
    if (!matching.length) {
      select.append(node("option", { text: "Нет подключённых аккаунтов" }));
      setNotice("#admin-result", "Сначала сохраните токен клиента и подключите его аккаунт в Zernio.", true);
      return;
    }
    matching.forEach((account) => {
      const detail = [account.display_name, account.username, account.profile_name].filter(Boolean).join(" · ");
      select.append(node("option", { value: account.id, text: detail }));
    });
    if (!$("#admin-connection-label").value) $("#admin-connection-label").value = matching[0].display_name;
  } catch (error) {
    clear(select);
    select.append(node("option", { text: "Не удалось загрузить аккаунты" }));
    setNotice("#admin-result", error.message, true);
  } finally {
    select.disabled = !select.querySelector("option[value]");
  }
}

function automaticTargetInfo(provider) {
  if (provider === "whapi") return {
    path: "whapi/targets",
    loading: "Загрузка групп и каналов WhatsApp…",
    empty: "Нет доступных групп или каналов WhatsApp.",
    help: "ID не нужен: отображаются группы и каналы, доступные по токену Whapi.",
  };
  return {
    path: "telegram/targets",
    loading: "Поиск чатов Telegram…",
    empty: "Приём включён, но новых чатов пока нет.",
    help: "Сначала нажмите «Обновить список» — это включает приём постов. Затем опубликуйте НОВЫЙ тестовый пост и нажмите кнопку ещё раз. Если канал не появился, перешлите любой его пост боту в личку после Start и обновите список.",
  };
}

async function loadAutomaticTargets() {
  const provider = $("#admin-connection-provider").value;
  if (provider !== "telegram" && provider !== "whapi") return;
  const userId = $("#admin-connection-user").value;
  const select = $("#admin-automatic-target");
  const help = $("#admin-automatic-target-help");
  const info = automaticTargetInfo(provider);
  clear(select); select.disabled = true; help.textContent = info.help;
  if (!userId) return;
  select.append(node("option", { text: info.loading }));
  try {
    const targets = await api(`/v1/admin/users/${userId}/${info.path}`);
    if (provider !== $("#admin-connection-provider").value || userId !== $("#admin-connection-user").value) return;
    clear(select);
    if (!targets.length) {
      select.append(node("option", { text: info.empty }));
      return;
    }
    targets.forEach((target) => {
      const kind = target.kind === "channel" ? "канал" : "группа";
      select.append(node("option", { value: target.id, text: `${target.label} · ${kind}`, "data-label": target.label }));
    });
    if (!$("#admin-connection-label").value) $("#admin-connection-label").value = targets[0].label;
  } catch (error) {
    clear(select); select.append(node("option", { text: "Не удалось загрузить список" }));
    setNotice("#admin-result", error.message, true);
  } finally {
    select.disabled = !select.querySelector("option[value]");
  }
}

function updateAdminCredentialFields() {
  const provider = $("#admin-credential-provider").value;
  $("#admin-credential-token").placeholder = provider === "zernio"
    ? "Zernio API key" : provider === "telegram" ? "Telegram Bot token" : "Whapi token";
}

$("#admin-credential-provider").addEventListener("change", updateAdminCredentialFields);
updateAdminCredentialFields();

$("#post-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selected = [...document.querySelectorAll("input[name=connection]:checked")].map((item) => item.value);
  if (!selected.length) { setNotice("#post-result", "Выберите хотя бы одну площадку.", true); return; }
  const hasTikTok = Boolean(selectedTikTokConnection());
  if (hasTikTok && (!$("#tiktok-privacy").value || !$("#tiktok-consent").checked)) {
    setNotice("#post-result", "Для TikTok выберите видимость и подтвердите согласие на публикацию.", true); return;
  }
  const attachmentIds = [];
  try {
    for (const file of $("#post-files").files) {
      const form = new FormData(); form.append("file", file);
      const upload = await api("/v1/uploads", { method: "POST", body: form }); attachmentIds.push(upload.id);
    }
    const localTime = $("#scheduled-at").value;
    const created = await api("/v1/posts", { method: "POST", body: JSON.stringify({
      content: $("#post-content").value, connection_ids: selected, attachment_ids: attachmentIds,
      scheduled_at: localTime ? new Date(localTime).toISOString() : null,
      instagram_content_type: $("#instagram-content-type").value,
      tiktok_settings: hasTikTok ? {
        privacy_level: $("#tiktok-privacy").value,
        allow_comment: $("#tiktok-allow-comment").checked,
        allow_duet: $("#tiktok-allow-duet").checked,
        allow_stitch: $("#tiktok-allow-stitch").checked,
        video_made_with_ai: $("#tiktok-ai").checked,
        commercial_content_type: $("#tiktok-commercial").value,
        draft: $("#tiktok-draft").checked,
        auto_add_music: $("#tiktok-auto-music").checked,
        photo_cover_index: $("#tiktok-photo-cover-index").value === "" ? null : Number($("#tiktok-photo-cover-index").value),
        video_cover_timestamp_ms: $("#tiktok-video-cover-timestamp").value === "" ? null : Number($("#tiktok-video-cover-timestamp").value),
        content_preview_confirmed: $("#tiktok-consent").checked,
        express_consent_given: $("#tiktok-consent").checked,
      } : null,
    }) });
    event.target.reset(); tiktokConnectionId = null; await refreshPosts();
    setNotice("#post-result", `Пост создан. Назначений: ${created.deliveries.length}.`);
  } catch (error) { setNotice("#post-result", error.message, true); }
});

$("#user-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api("/v1/admin/users", { method: "POST", body: JSON.stringify({
      email: $("#user-email").value, password: $("#user-password").value,
      display_name: $("#user-name").value, is_superuser: $("#user-super").checked,
    }) });
    event.target.reset(); await refreshUsers(); setNotice("#admin-result", "Кабинет создан.");
  } catch (error) { setNotice("#admin-result", error.message, true); }
});

$("#admin-credential-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  try {
    await api(`/v1/admin/users/${$("#admin-credential-user").value}/credentials`, { method: "POST", body: JSON.stringify({
      provider: $("#admin-credential-provider").value,
      api_token: $("#admin-credential-token").value,
    }) });
    event.target.reset(); updateAdminCredentialFields();
    const connectionProvider = $("#admin-connection-provider").value;
    if (connectionProvider === "zernio") void loadZernioAccounts();
    if (connectionProvider === "telegram" || connectionProvider === "whapi") void loadAutomaticTargets();
    setNotice("#admin-result", "Токен клиента сохранён и скрыт.");
  } catch (error) { setNotice("#admin-result", error.message, true); }
});

$("#admin-connection-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const provider = $("#admin-connection-provider").value;
  const externalId = provider === "zernio" ? $("#admin-zernio-account").value
    : provider === "telegram" || provider === "whapi" ? $("#admin-automatic-target").value
      : $("#admin-connection-target").value;
  if (!externalId) { setNotice("#admin-result", "Сначала выберите площадку из списка.", true); return; }
  try {
    await api(`/v1/admin/users/${$("#admin-connection-user").value}/connections`, { method: "POST", body: JSON.stringify({
      provider, platform: $("#admin-connection-platform").value,
      label: $("#admin-connection-label").value,
      external_id: externalId,
    }) });
    event.target.reset(); updateAdminConnectionFields(); setNotice("#admin-result", "Площадка пользователя сохранена.");
  } catch (error) { setNotice("#admin-result", error.message, true); }
});

if (state.token) {
  refreshDashboard().then(() => showLoggedIn(true)).catch(() => { state.token = null; sessionStorage.removeItem("access_token"); showLoggedIn(false); });
}
