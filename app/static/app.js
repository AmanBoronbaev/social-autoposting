const state = { token: sessionStorage.getItem("access_token"), me: null, connections: [] };
let tiktokConnectionId = null;
let instagramAudioConnectionId = null;
const TARGET_PLATFORM_ORDER = {
  telegram: 0,
  whatsapp: 1,
  instagram: 2,
  tiktok: 3,
};
let adminUiLoaded = false;
const appBasePath = new URL("./", document.baseURI).pathname.replace(/\/$/, "");
const MAX_ATTACHMENTS_PER_POST = 35;
const TIKTOK_PHOTO_TITLE_MAX_LENGTH = 90;

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
  let response;
  try {
    response = await fetch(resolvedPath, { ...fetchOptions, headers });
  } catch {
    throw new Error("Не удалось связаться с сервером. Проверьте интернет и не закрывайте страницу во время загрузки.");
  }
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
  [...state.connections]
    .filter((item) => item.status === "connected")
    .sort((left, right) => (
      (TARGET_PLATFORM_ORDER[left.platform] ?? Number.MAX_SAFE_INTEGER)
      - (TARGET_PLATFORM_ORDER[right.platform] ?? Number.MAX_SAFE_INTEGER)
      || left.label.localeCompare(right.label, "ru")
    ))
    .forEach((connection) => {
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

function hasSelectedWhatsAppConnection() {
  const selected = selectedConnectionIds();
  return state.connections.some((connection) => selected.includes(connection.id) && connection.platform === "whatsapp");
}

function hasSelectedTelegramConnection() {
  const selected = selectedConnectionIds();
  return state.connections.some((connection) => selected.includes(connection.id) && connection.platform === "telegram");
}

function isVideoFile(file) {
  return file.type.startsWith("video/") || /\.(3gp|avi|flv|hevc|m2ts|m4v|mkv|mov|mp4|mpeg|mpg|ts|webm|wmv)$/i.test(file.name);
}

function isImageFile(file) {
  return file.type.startsWith("image/") || /\.(avif|gif|heic|heif|jpe?g|png|webp)$/i.test(file.name);
}

function formatFileSize(bytes) {
  if (!Number.isFinite(bytes)) return "";
  const units = ["Б", "КБ", "МБ", "ГБ"];
  let size = bytes;
  let index = 0;
  while (size >= 1024 && index < units.length - 1) { size /= 1024; index += 1; }
  return `${size >= 10 || index === 0 ? Math.round(size) : size.toFixed(1)} ${units[index]}`;
}

function configuredMediaLimit(name, fallback) {
  const configured = Number(state.me?.media_limits?.[name]);
  return Number.isSafeInteger(configured) && configured > 0 ? configured : fallback;
}

function uploadLimitBytes() {
  return configuredMediaLimit("upload_bytes", 500 * 1024 * 1024);
}

function telegramMediaLimitBytes() {
  return configuredMediaLimit("telegram_media_bytes", 50 * 1024 * 1024);
}

function whatsappMediaLimitBytes() {
  return configuredMediaLimit("whatsapp_media_bytes", 100 * 1024 * 1024);
}

function whatsappDocumentLimitBytes() {
  return configuredMediaLimit("whatsapp_document_bytes", 2_000 * 1024 * 1024);
}

function isPdfFile(file) {
  return file.type === "application/pdf" || /\.pdf$/i.test(file.name);
}

function whatsappLimitForFile(file) {
  return isPdfFile(file) ? whatsappDocumentLimitBytes() : whatsappMediaLimitBytes();
}

function instagramVideoLimitBytes() {
  return $("#instagram-content-type").value === "story" ? 100 * 1024 * 1024 : 300 * 1024 * 1024;
}

function updateUploadLimitHelp() {
  $("#upload-limit-help").textContent = `До ${formatFileSize(uploadLimitBytes())} на файл. Порядок в списке — порядок публикации: файл №1 будет первым в карусели и Telegram-альбоме. Видео перед отправкой приводится к MP4 (H.264/AAC), 30 FPS и максимуму 1080p. PDF можно отправлять только в Telegram и WhatsApp.`;
}

function updatePlatformMediaHelp() {
  const help = $("#platform-media-help");
  updateUploadLimitHelp();
  const hasWhatsApp = hasSelectedWhatsAppConnection();
  const hasTelegram = hasSelectedTelegramConnection();
  const hasInstagram = selectedInstagramConnections().length > 0;
  const hasTikTok = Boolean(selectedTikTokConnection());
  if (!hasWhatsApp && !hasTelegram && !hasInstagram && !hasTikTok) {
    help.classList.add("hidden");
    help.textContent = "";
    return;
  }
  const files = [...$("#post-files").files];
  const unchangedTooLarge = hasWhatsApp && files.find(
    (file) => !isVideoFile(file) && file.size > whatsappLimitForFile(file)
  );
  const videoOverLimit = hasWhatsApp && files.some((file) => isVideoFile(file) && file.size > whatsappMediaLimitBytes());
  const parts = [];
  if (hasTelegram) parts.push(`Telegram: до ${formatFileSize(telegramMediaLimitBytes())} на итоговый файл.`);
  if (hasWhatsApp) parts.push(
    `WhatsApp: фото и итоговое видео до ${formatFileSize(whatsappMediaLimitBytes())}, PDF до ${formatFileSize(whatsappDocumentLimitBytes())}.`
  );
  if (hasInstagram) {
    const limit = instagramVideoLimitBytes();
    const type = $("#instagram-content-type").value === "story" ? "Story" : "Reel/публикация";
    parts.push(`Instagram ${type}: итоговое видео до ${formatFileSize(limit)}; при превышении оно автоматически оптимизируется.`);
  }
  if (hasTikTok) {
    const photoPost = files.length > 0 && files.every(isImageFile);
    parts.push(photoPost
      ? "TikTok: фото до 20 МБ каждое."
      : "TikTok: итоговое видео до 287,6 МБ, длительность 3–600 сек.; видео автоматически оптимизируется."
    );
  }
  help.classList.remove("hidden");
  if (unchangedTooLarge) {
    help.textContent = `${parts.join(" ")} «${unchangedTooLarge.name}» больше допустимого объёма и не будет отправлен. Уменьшите файл или снимите WhatsApp с публикации.`;
  } else if (videoOverLimit) {
    help.textContent = `${parts.join(" ")} Видео будет автоматически сжато для WhatsApp; качество может снизиться. Если даже после сжатия файл не поместится, WhatsApp не получит его.`;
  } else {
    help.textContent = `${parts.join(" ")} Видео сжимается перед отправкой; фото и PDF отправляются без уменьшения.`;
  }
}

function uploadQueueItems() {
  const items = [...$("#post-files").files].map((file) => ({
    file, label: isVideoFile(file) ? "Видео" : isImageFile(file) ? "Фото" : "Документ", icon: isVideoFile(file) ? "▶" : isImageFile(file) ? "▣" : "▤",
  }));
  const hasTikTok = Boolean(selectedTikTokConnection());
  const hasInstagramReel = selectedInstagramConnections().length && hasSingleInstagramReelVideo();
  const tiktokCover = hasTikTok ? $("#tiktok-cover-file").files[0] : null;
  const instagramCover = hasInstagramReel ? $("#instagram-cover-file").files[0] : null;
  if (tiktokCover) items.push({ file: tiktokCover, label: "Обложка TikTok", icon: "▣" });
  if (instagramCover) items.push({ file: instagramCover, label: "Обложка Instagram", icon: "▣" });
  return items;
}

function renderUploadQueue({ activeIndex = null, activeStatus, completeThrough = -1, title } = {}) {
  const queue = $("#upload-queue");
  const items = uploadQueueItems();
  queue.classList.toggle("hidden", !items.length);
  if (!items.length) {
    updatePlatformMediaHelp();
    return;
  }
  $("#upload-queue-title").textContent = title || "Выбрано для публикации";
  const list = $("#upload-queue-items");
  clear(list);
  items.forEach((item, index) => {
    const isActive = index === activeIndex;
    const isDone = index <= completeThrough;
    const status = isActive ? activeStatus || "Загружается…" : isDone ? "Готово" : "Ожидает";
    list.append(node("div", { class: `upload-queue-item${isActive ? " active" : ""}${isDone ? " done" : ""}` }, [
      node("span", { class: "upload-queue-icon", text: item.icon }),
      node("span", { class: "upload-queue-meta" }, [
        node("strong", { text: item.label === "Фото" || item.label === "Видео" || item.label === "Документ" ? `${index + 1}. ${item.label}` : item.label }),
        node("span", { class: "upload-queue-name", text: `${item.file.name} · ${formatFileSize(item.file.size)}` }),
      ]),
      node("span", { class: "upload-queue-status", text: status }),
    ]));
  });
  updatePlatformMediaHelp();
}

function updateTikTokPhotoTitleLimit() {
  const notice = $("#tiktok-photo-title-limit");
  const files = [...$("#post-files").files];
  const isPhotoPost = Boolean(selectedTikTokConnection())
    && files.length > 0
    && files.every(isImageFile);
  notice.classList.toggle("hidden", !isPhotoPost);
  if (!isPhotoPost) return;
  const length = $("#post-content").value.length;
  const overLimit = length > TIKTOK_PHOTO_TITLE_MAX_LENGTH;
  notice.classList.toggle("limit-error", overLimit);
  notice.textContent = overLimit
    ? `Текст для фотокарусели TikTok: ${length}/${TIKTOK_PHOTO_TITLE_MAX_LENGTH}. Сократите текст или публикуйте как видео.`
    : `Текст для фотокарусели TikTok: ${length}/${TIKTOK_PHOTO_TITLE_MAX_LENGTH} символов.`;
}

async function updateTikTokOptions() {
  const fieldset = $("#tiktok-options");
  const selected = selectedTikTokConnection();
  fieldset.classList.toggle("hidden", !selected);
  updateTikTokPhotoTitleLimit();
  if (!selected) {
    // A hidden required checkbox is still validated by Safari and Chromium.
    // Disable every TikTok-only control unless TikTok is really selected.
    fieldset.querySelectorAll("input, select").forEach((control) => { control.disabled = true; });
    tiktokConnectionId = null;
    return;
  }
  const selectedFiles = [...$("#post-files").files];
  const hasVideo = selectedFiles.some(isVideoFile);
  const hasPhotosOnly = selectedFiles.length > 0 && !hasVideo;
  $("#tiktok-allow-comment").disabled = false;
  $("#tiktok-allow-duet").disabled = false;
  $("#tiktok-allow-stitch").disabled = false;
  $("#tiktok-ai").disabled = false;
  $("#tiktok-consent").disabled = false;
  $("#tiktok-auto-music").disabled = !hasPhotosOnly;
  $("#tiktok-photo-cover-index").disabled = !hasPhotosOnly;
  $("#tiktok-video-cover-timestamp").disabled = !hasVideo;
  $("#tiktok-cover-file").disabled = !hasVideo;
  if (tiktokConnectionId === selected.id && !$("#tiktok-privacy").disabled) return;
  const privacy = $("#tiktok-privacy");
  const commercial = $("#tiktok-commercial");
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

function refreshPostPlatformOptions() {
  void updatePostPlatformOptions().finally(() => {
    renderUploadQueue();
    updatePlatformMediaHelp();
  });
}

$("#post-targets").addEventListener("change", refreshPostPlatformOptions);
$("#post-content").addEventListener("input", updateTikTokPhotoTitleLimit);
$("#post-files").addEventListener("change", () => {
  tiktokConnectionId = null;
  instagramAudioConnectionId = null;
  refreshPostPlatformOptions();
});
$("#instagram-content-type").addEventListener("change", () => {
  updateInstagramOptions();
  renderUploadQueue();
  updatePlatformMediaHelp();
});
$("#instagram-audio-search").addEventListener("click", () => void loadInstagramAudio());
$("#tiktok-cover-file").addEventListener("change", () => renderUploadQueue());
$("#instagram-cover-file").addEventListener("change", () => renderUploadQueue());

function uploadFile(file, onProgress) {
  const resolvedPath = `${appBasePath}/v1/uploads`.replace(/^\/{2,}/, "/");
  return new Promise((resolve, reject) => {
    const request = new XMLHttpRequest();
    request.open("POST", resolvedPath);
    request.timeout = 900_000;
    if (state.token) request.setRequestHeader("Authorization", `Bearer ${state.token}`);
    request.upload.onprogress = (event) => {
      if (!event.lengthComputable) return;
      onProgress(Math.round((event.loaded / event.total) * 100));
    };
    request.onload = () => {
      let payload = {};
      try { payload = request.responseText ? JSON.parse(request.responseText) : {}; } catch { /* handled below */ }
      if (request.status >= 200 && request.status < 300) {
        resolve(payload);
      } else {
        reject(new Error(errorText(payload)));
      }
    };
    request.onerror = () => reject(new Error(
      `Не удалось загрузить «${file.name}». Проверьте интернет, размер файла и не закрывайте страницу.`
    ));
    request.ontimeout = () => reject(new Error(`Загрузка «${file.name}» заняла слишком много времени. Попробуйте ещё раз.`));
    request.onabort = () => reject(new Error(`Загрузка «${file.name}» была отменена.`));
    const form = new FormData();
    form.append("file", file);
    request.send(form);
  });
}

function validatePostMedia(selected) {
  const files = [...$("#post-files").files];
  const instagramConnections = selectedInstagramConnections();
  const hasTikTok = Boolean(selectedTikTokConnection());
  const sourceTooLarge = files.find((file) => file.size > uploadLimitBytes());
  if (sourceTooLarge) {
    return `Файл «${sourceTooLarge.name}» больше лимита загрузки (${formatFileSize(uploadLimitBytes())}).`;
  }
  if (hasSelectedTelegramConnection()) {
    const tooLarge = files.find((file) => file.size > telegramMediaLimitBytes());
    if (tooLarge) {
      return `Файл «${tooLarge.name}» больше допустимого объёма для Telegram (${formatFileSize(telegramMediaLimitBytes())}).`;
    }
  }
  if (hasSelectedWhatsAppConnection()) {
    const tooLarge = files.find((file) => !isVideoFile(file) && file.size > whatsappLimitForFile(file));
    if (tooLarge) {
      return `Файл «${tooLarge.name}» больше допустимого объёма для WhatsApp (${formatFileSize(whatsappLimitForFile(tooLarge))}). Уменьшите файл или снимите WhatsApp с публикации.`;
    }
  }
  const story = $("#instagram-content-type").value === "story";
  if (files.length > MAX_ATTACHMENTS_PER_POST) {
    return `За одну публикацию можно выбрать не более ${MAX_ATTACHMENTS_PER_POST} файлов.`;
  }
  if (instagramConnections.length && files.length > 10) {
    return "Instagram принимает не более 10 фото или видео в одной карусели. Уберите лишние файлы или снимите Instagram.";
  }
  if (story && (
    files.length !== 1
    || selected.length !== instagramConnections.length
    || (!isImageFile(files[0]) && !isVideoFile(files[0]))
  )) {
    return "Instagram Story отправляется только как один файл и только в Instagram.";
  }
  if (hasTikTok) {
    const videos = files.filter(isVideoFile);
    const photos = files.filter(isImageFile);
    if (!files.length) return "Для TikTok добавьте одно видео или от 1 до 35 фото.";
    if (videos.length && (videos.length !== 1 || files.length !== 1)) {
      return "TikTok принимает одно видео или фотокарусель: видео и фото нельзя смешивать.";
    }
    if (!videos.length && photos.length !== files.length) {
      return "Для TikTok можно выбрать только изображения или одно видео.";
    }
    if (photos.length === files.length && $("#post-content").value.length > TIKTOK_PHOTO_TITLE_MAX_LENGTH) {
      return `Для фотокарусели TikTok текст может содержать до ${TIKTOK_PHOTO_TITLE_MAX_LENGTH} символов. Сократите текст или публикуйте как видео.`;
    }
  }
  return null;
}

function setPostFormBusy(form, isBusy) {
  form.querySelectorAll("input, select, textarea, button").forEach((control) => {
    if (isBusy) {
      control.dataset.wasDisabled = String(control.disabled);
      control.disabled = true;
    } else {
      control.disabled = control.dataset.wasDisabled === "true";
      delete control.dataset.wasDisabled;
    }
  });
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
  const form = event.currentTarget;
  if (form.dataset.submitting === "true") return;
  const selected = selectedConnectionIds();
  if (!selected.length) { setNotice("#post-result", "Выберите хотя бы одну площадку.", true); return; }
  const mediaError = validatePostMedia(selected);
  if (mediaError) { setNotice("#post-result", mediaError, true); return; }
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
  const submitButton = $("#post-submit");
  const submitLabel = submitButton.textContent;
  const queueItems = uploadQueueItems();
  form.dataset.submitting = "true";
  setPostFormBusy(form, true);
  submitButton.textContent = "Загружаем…";
  setNotice("#post-result", "Подготавливаем файлы к загрузке…");
  let createdSuccessfully = false;
  try {
    let queueIndex = 0;
    const uploadQueueFile = async (file) => {
      renderUploadQueue({ activeIndex: queueIndex, completeThrough: queueIndex - 1, title: "Загружаем файлы" });
      const upload = await uploadFile(file, (percent) => {
        renderUploadQueue({
          activeIndex: queueIndex,
          activeStatus: `Загрузка: ${percent}%`,
          completeThrough: queueIndex - 1,
          title: "Загружаем файлы",
        });
      });
      queueIndex += 1;
      return upload;
    };
    for (const file of $("#post-files").files) {
      const upload = await uploadQueueFile(file);
      attachmentIds.push(upload.id);
    }
    const tiktokCoverUpload = tiktokCover ? await uploadQueueFile(tiktokCover) : null;
    const instagramCoverUpload = instagramCover ? await uploadQueueFile(instagramCover) : null;
    renderUploadQueue({ completeThrough: queueItems.length - 1, title: "Создаём публикацию" });
    submitButton.textContent = "Создаём публикацию…";
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
        auto_add_music: $("#tiktok-auto-music").checked,
        photo_cover_index: $("#tiktok-photo-cover-index").value === "" ? null : Number($("#tiktok-photo-cover-index").value),
        video_cover_timestamp_ms: $("#tiktok-video-cover-timestamp").value === "" ? null : Number($("#tiktok-video-cover-timestamp").value),
        content_preview_confirmed: $("#tiktok-consent").checked,
        express_consent_given: $("#tiktok-consent").checked,
      } : null,
    }) });
    createdSuccessfully = true;
    form.reset(); tiktokConnectionId = null; instagramAudioConnectionId = null;
    await refreshPosts();
    setNotice("#post-result", `Пост создан. Назначений: ${created.deliveries.length}.`);
  } catch (error) {
    renderUploadQueue({ title: "Не удалось завершить загрузку" });
    setNotice("#post-result", error.message, true);
    $("#post-result").scrollIntoView({ block: "nearest", behavior: "smooth" });
  } finally {
    delete form.dataset.submitting;
    setPostFormBusy(form, false);
    submitButton.textContent = submitLabel;
    if (createdSuccessfully) await updatePostPlatformOptions();
    renderUploadQueue(createdSuccessfully ? {} : { title: "Файлы готовы к повторной отправке" });
  }
});

if (state.token) {
  refreshDashboard().then(() => showLoggedIn(true)).catch(() => { state.token = null; sessionStorage.removeItem("access_token"); showLoggedIn(false); });
}
