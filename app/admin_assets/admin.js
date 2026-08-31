const { state, $, clear, node, api, setNotice } = window.Autoposting;

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

async function refreshUsers() {
  state.users = await api("/v1/admin/users");
  renderUsers();
}

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

async function loadZernioAccounts() {
  const select = $("#admin-zernio-account");
  const userId = $("#admin-connection-user").value;
  const platform = $("#admin-connection-platform").value;
  clear(select); select.disabled = true;
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
    clear(select); select.append(node("option", { text: "Не удалось загрузить аккаунты" }));
    setNotice("#admin-result", error.message, true);
  } finally {
    select.disabled = !select.querySelector("option[value]");
  }
}

function automaticTargetInfo(provider) {
  if (provider === "whapi") return {
    path: "whapi/targets", loading: "Загрузка групп и каналов WhatsApp…",
    empty: "Нет доступных групп или каналов WhatsApp.",
    help: "ID не нужен: отображаются группы и каналы, доступные по токену Whapi.",
  };
  return {
    path: "telegram/targets", loading: "Поиск чатов Telegram…", empty: "Приём включён, но новых чатов пока нет.",
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
$("#admin-credential-provider").addEventListener("change", updateAdminCredentialFields);

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
      provider: $("#admin-credential-provider").value, api_token: $("#admin-credential-token").value,
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
      label: $("#admin-connection-label").value, external_id: externalId,
    }) });
    event.target.reset(); updateAdminConnectionFields(); setNotice("#admin-result", "Площадка пользователя сохранена.");
  } catch (error) { setNotice("#admin-result", error.message, true); }
});

updateAdminCredentialFields();
await refreshUsers();
