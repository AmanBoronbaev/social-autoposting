const state = { token: sessionStorage.getItem("access_token"), me: null, connections: [] };
let tiktokConnectionId = null;
let instagramAudioConnectionId = null;
let adminUiLoaded = false;
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
  const { responseType = "json", ...fetchOptions } = options;
  const headers = new Headers(fetchOptions.headers || {});
  if (state.token) headers.set("Authorization", `Bearer ${state.token}`);
  if (fetchOptions.body && !(fetchOptions.body instanceof FormData)) headers.set("Content-Type", "application/json");
  const resolvedPath = `${appBasePath}/${path.replace(/^\//, "")}`.replace(/^\/{2,}/, "/");
  const response = await fetch(resolvedPath, { ...fetchOptions, headers });
  const payload = response.status === 204
    ? null
    : responseType === "text" ? await response.text() : await response.json().catch(() => ({}));
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
  void updatePostPlatformOptions();
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
      if (delivery.error) {
        deliveryList.append(node("small", { class: "delivery-error", text: delivery.error }));
      }
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

async function refreshPosts() { renderPosts(await api("/v1/posts")); }
async function refreshConnections() { state.connections = await api("/v1/connections"); renderConnections(); }

window.Autoposting = { state, $, clear, node, api, setNotice };

async function loadAdminUi() {
  if (adminUiLoaded) return;
  $("#admin").innerHTML = await api("/v1/admin/ui", { responseType: "text" });
  const source = await api("/v1/admin/client.js", { responseType: "text" });
  const moduleUrl = URL.createObjectURL(new Blob([source], { type: "text/javascript" }));
  try {
    await import(moduleUrl);
  } finally {
    URL.revokeObjectURL(moduleUrl);
  }
  adminUiLoaded = true;
}

async function refreshDashboard() {
  state.me = await api("/v1/me");
  $("#identity").textContent = state.me.display_name || state.me.email;
  $("#admin-tab").classList.toggle("hidden", !state.me.is_superuser);
  await Promise.all([refreshConnections(), refreshPosts()]);
  if (state.me.is_superuser) await loadAdminUi();
}

function selectedTikTokConnection() {
  const selected = selectedConnectionIds();
  return state.connections.find((connection) => selected.includes(connection.id) && connection.platform === "tiktok");
}

function selectedConnectionIds() {
  return [...document.querySelectorAll("input[name=connection]:checked")].map((item) => item.value);
}

function selectedInstagramConnections() {
  const selected = selectedConnectionIds();
  return state.connections.filter((connection) => selected.includes(connection.id) && connection.platform === "instagram");
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

function hasSingleInstagramReelVideo() {
  const files = [...$("#post-files").files];
  return $("#instagram-content-type").value === "standard" && files.length === 1 && isVideoFile(files[0]);
}

function resetInstagramAudioSelect(message = "Сначала выполните поиск") {
  const select = $("#instagram-audio-id");
  clear(select);
  select.append(node("option", { value: "", text: message }));
  select.disabled = true;
}

function updateInstagramOptions() {
  const fieldset = $("#instagram-options");
  const selected = selectedInstagramConnections();
  const isReelVideo = hasSingleInstagramReelVideo();
  fieldset.classList.toggle("hidden", !selected.length || !isReelVideo);
  const audioOptions = $("#instagram-audio-options");
  const audioAvailable = selected.length === 1 && isReelVideo;
  audioOptions.classList.toggle("hidden", !audioAvailable);
  if (!audioAvailable) {
    instagramAudioConnectionId = null;
    resetInstagramAudioSelect("Музыка доступна для одного Reel-видео");
  } else if (instagramAudioConnectionId !== selected[0].id) {
    instagramAudioConnectionId = selected[0].id;
    resetInstagramAudioSelect();
  }
}

async function loadInstagramAudio() {
  const [selected] = selectedInstagramConnections();
  if (!selected || !hasSingleInstagramReelVideo()) {
    setNotice("#post-result", "Выберите одну Instagram-площадку и загрузите одно Reel-видео.", true);
    return;
  }
  const button = $("#instagram-audio-search");
  button.disabled = true;
  $("#instagram-audio-help").textContent = "Ищем доступное аудио…";
  try {
    const params = new URLSearchParams({
      audio_type: $("#instagram-audio-type").value,
      q: $("#instagram-audio-query").value.trim(),
    });
    const result = await api(`/v1/connections/${selected.id}/instagram/audio?${params.toString()}`);
    if (selected.id !== selectedInstagramConnections()[0]?.id) return;
    const tracks = Array.isArray(result.tracks) ? result.tracks : [];
    const select = $("#instagram-audio-id");
    clear(select);
    select.append(node("option", { value: "", text: "Без музыки из каталога" }));
    tracks.forEach((track) => {
      const duration = typeof track.duration === "number" ? ` · ${Math.round(track.duration)} сек.` : "";
      const artist = track.artist ? ` — ${track.artist}` : "";
      select.append(node("option", { value: track.id, text: `${track.title}${artist}${duration}` }));
    });
    select.disabled = false;
    $("#instagram-audio-help").textContent = tracks.length
      ? `Найдено вариантов: ${tracks.length}. Выберите один или оставьте «Без музыки».`
      : "Ничего не найдено. Попробуйте другое название или исполнителя.";
  } catch (error) {
    resetInstagramAudioSelect("Аудио недоступно");
    $("#instagram-audio-help").textContent = error.message;
    setNotice("#post-result", error.message, true);
  } finally {
    button.disabled = false;
  }
}

async function updatePostPlatformOptions() {
  await Promise.all([updateTikTokOptions(), Promise.resolve(updateInstagramOptions())]);
}

$("#post-targets").addEventListener("change", () => void updatePostPlatformOptions());
$("#post-files").addEventListener("change", () => {
  tiktokConnectionId = null;
  instagramAudioConnectionId = null;
  void updatePostPlatformOptions();
});
$("#instagram-content-type").addEventListener("change", () => void updateInstagramOptions());
$("#instagram-audio-search").addEventListener("click", () => void loadInstagramAudio());

async function uploadFile(file) {
  const form = new FormData();
  form.append("file", file);
  return api("/v1/uploads", { method: "POST", body: form });
}

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
  state.token = null; state.me = null; sessionStorage.removeItem("access_token");
  adminUiLoaded = false; $("#admin").replaceChildren(); $("#admin-tab").classList.add("hidden");
  showLoggedIn(false); $("#identity").textContent = "";
});

document.querySelectorAll(".tab").forEach((tab) => tab.addEventListener("click", () => showPage(tab.dataset.page)));

$("#post-form").addEventListener("submit", async (event) => {
  event.preventDefault();
  const selected = selectedConnectionIds();
  if (!selected.length) { setNotice("#post-result", "Выберите хотя бы одну площадку.", true); return; }
  const hasTikTok = Boolean(selectedTikTokConnection());
  if (hasTikTok && (!$("#tiktok-privacy").value || !$("#tiktok-consent").checked)) {
    setNotice("#post-result", "Для TikTok выберите видимость и подтвердите согласие на публикацию.", true); return;
  }
  const instagramConnections = selectedInstagramConnections();
  const tiktokCover = hasTikTok ? $("#tiktok-cover-file").files[0] : null;
  const instagramCover = instagramConnections.length && hasSingleInstagramReelVideo()
    ? $("#instagram-cover-file").files[0] : null;
  const instagramAudioId = instagramConnections.length === 1 && hasSingleInstagramReelVideo()
    ? $("#instagram-audio-id").value || null : null;
  const attachmentIds = [];
  try {
    for (const file of $("#post-files").files) {
      const upload = await uploadFile(file); attachmentIds.push(upload.id);
    }
    const tiktokCoverUpload = tiktokCover ? await uploadFile(tiktokCover) : null;
    const instagramCoverUpload = instagramCover ? await uploadFile(instagramCover) : null;
    const localTime = $("#scheduled-at").value;
    const created = await api("/v1/posts", { method: "POST", body: JSON.stringify({
      content: $("#post-content").value, connection_ids: selected, attachment_ids: attachmentIds,
      scheduled_at: localTime ? new Date(localTime).toISOString() : null,
      instagram_content_type: $("#instagram-content-type").value,
      tiktok_cover_attachment_id: tiktokCoverUpload?.id || null,
      instagram_cover_attachment_id: instagramCoverUpload?.id || null,
      instagram_audio_id: instagramAudioId,
      instagram_audio_volume: Number($("#instagram-audio-volume").value),
      instagram_video_volume: Number($("#instagram-video-volume").value),
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
    event.target.reset(); tiktokConnectionId = null; instagramAudioConnectionId = null;
    await refreshPosts();
    void updatePostPlatformOptions();
    setNotice("#post-result", `Пост создан. Назначений: ${created.deliveries.length}.`);
  } catch (error) { setNotice("#post-result", error.message, true); }
});

if (state.token) {
  refreshDashboard().then(() => showLoggedIn(true)).catch(() => { state.token = null; sessionStorage.removeItem("access_token"); showLoggedIn(false); });
}
