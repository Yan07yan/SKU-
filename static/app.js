const state = {
  status: "all",
  q: "",
  tasks: [],
  selectedSkus: new Set(),
  statuses: {},
  codex: { authenticated: false, message: "正在检测 Codex 本地服务配置状态..." },
  apiKeyInfo: { authenticated: false, has_api_base_url: false },
  generation: { state: "idle", completed: 0, total: 0 },
  detailGeneration: { state: "idle", completed: 0, total: 0 },
  preview: { sku: null, images: [], index: 0 },
};
const labels = {
  all: "全部",
  pending: "待处理",
  generating: "生成中",
  collaging: "拼图中",
  collaging_failed: "拼图失败",
  quality_failed: "质检失败",
  waiting_confirm: "待确认",
  awaiting_confirm: "待确认",
  detail_generating: "详情页生成中",
  detail_done: "详情页完成",
  detail_failed: "详情页失败",
  approved: "已通过",
  rejected: "已驳回",
  failed: "真失败",
  quota_exhausted: "额度耗尽",
};

async function api(path, options = {}) {
  const res = await fetch(path, options);
  if (!res.ok) {
    const data = await res.json().catch(() => ({}));
    throw new Error(data.error || `请求失败：${res.status}`);
  }
  const contentType = res.headers.get("content-type") || "";
  return contentType.includes("json") ? res.json() : res.blob();
}

function imageUrl(taskOrPath) {
  const path = typeof taskOrPath === "string"
    ? taskOrPath
    : (taskOrPath?.image_path || (taskOrPath?.image_folder ? `${taskOrPath.image_folder}/主图.png` : ""));
  if (!path) return "";
  const normalized = path.replaceAll("\\", "/");
  const marker = "/output/";
  const idx = normalized.toLowerCase().indexOf(marker);
  let outputPath = "";
  if (idx >= 0) {
    outputPath = normalized.slice(idx);
  } else if (normalized.toLowerCase().startsWith("output/")) {
    outputPath = `/${normalized}`;
  } else if (normalized.toLowerCase().startsWith("/output/")) {
    outputPath = normalized;
  }
  if (!outputPath) return "";
  const encoded = outputPath
    .split("/")
    .map((part, index) => (index === 0 ? part : encodeURIComponent(part)))
    .join("/");
  console.log("[imageUrl]", { source: path, url: encoded });
  return encoded;
}

function expectedImageUrls(task) {
  if (!task?.image_folder) {
    const main = imageUrl(task);
    return main ? [{ name: "主图.png", url: main }] : [];
  }
  const folder = task.image_folder.replaceAll("\\", "/").replace(/\/+$/, "");
  return [
    ...Array.from({ length: 9 }, (_, index) => {
      const name = `${index + 1}.png`;
      return { name, url: imageUrl(`${folder}/${name}`) };
    }),
    { name: "主图.png", url: imageUrl(task.image_path || `${folder}/主图.png`) },
    ...Array.from({ length: 6 }, (_, index) => {
      const name = `detail_${index + 1}.png`;
      return { name, url: imageUrl(`${folder}/${name}`) };
    }),
  ].filter((item) => item.url);
}

async function loadImagesForTask(task) {
  if (!task?.sku) return [];
  try {
    const data = await api(`/api/images/${encodeURIComponent(task.sku)}`);
    if (Array.isArray(data.images) && data.images.length) {
      console.log("[previewImages]", { sku: task.sku, images: data.images });
      return data.images;
    }
  } catch (error) {
    console.warn("[previewImages] fallback to local path rules", error);
  }
  return expectedImageUrls(task);
}

async function refresh() {
  const params = new URLSearchParams({ status: state.status, q: state.q });
  const [tasks, appState] = await Promise.all([
    api(`/api/tasks?${params}`),
    api("/api/state"),
  ]);
  state.tasks = tasks;
  state.statuses = appState.statuses;
  state.codex = appState.codex || state.codex;
  state.generation = appState.generation || state.generation;
  state.detailGeneration = appState.detail_generation || state.detailGeneration;
  renderState(appState);
  renderCodexAuth(state.codex);
  renderFilters(appState.counts);
  renderTasks(tasks);
}

async function refreshCodexAuth(useRefreshEndpoint = false) {
  const auth = await api(useRefreshEndpoint ? "/api/codex/refresh" : "/api/codex/status", {
    method: useRefreshEndpoint ? "POST" : "GET",
  });
  state.codex = auth;
  renderCodexAuth(auth);
  if (auth.authenticated) {
    refreshApiKeyInfo().catch(() => {});
  } else {
    renderApiKeyInfo({ authenticated: false, has_api_base_url: false });
  }
  return auth;
}

function renderCodexAuth(auth) {
  const banner = document.querySelector("#codexAuthBanner");
  const text = document.querySelector("#codexAuthText");
  if (!banner || !text) return;
  const ready = Boolean(auth && auth.authenticated);
  banner.className = `authBanner ${ready ? "ready" : "missing"}`;
  text.textContent = ready
    ? "✅ Codex 本地服务已就绪，可以生成图片"
    : `⚠️ 请先配置 Codex 本地服务地址才能生成图片。${auth?.message || ""}`;
}

async function refreshApiKeyInfo() {
  const info = await api("/api/codex/user-info");
  state.apiKeyInfo = info;
  renderApiKeyInfo(info);
  return info;
}

function renderApiKeyInfo(info) {
  const button = document.querySelector("#apiKeyOpenBtn");
  const status = document.querySelector("#apiKeyStatus");
  const membership = document.querySelector("#membershipInfo");
  if (!button || !status || !membership) return;
  if (info && info.has_api_base_url) {
    const tokenText = info.has_access_token ? " · 已认证" : " · 未填 Token";
    status.textContent = `${info.api_base_url_display || info.api_base_url}${tokenText}`;
    status.classList.remove("hidden");
    button.textContent = "更换";
    button.classList.add("signedIn");
    membership.textContent = `会员状态：${info.membership_status || "订阅有效"}`;
    membership.classList.remove("hidden");
  } else {
    status.classList.add("hidden");
    button.textContent = "配置本地服务";
    button.classList.remove("signedIn");
    membership.classList.add("hidden");
  }
}

function showApiKeyDialog() {
  const message = document.querySelector("#apiKeyMessage");
  if (message) {
    message.textContent = "";
    message.className = "formMessage";
  }
  document.querySelector("#apiKeyDialog").showModal();
}

async function ensureCodexReady() {
  if (state.codex && state.codex.authenticated) return true;
  alert("请先配置 Codex 本地服务地址");
  showApiKeyDialog();
  return false;
}

function renderState(appState) {
  const stats = document.querySelector("#stats");
  const counts = appState.counts || {};
  stats.innerHTML = Object.entries(labels)
    .filter(([key]) => key !== "all")
    .map(([key, label]) => `<div class="stat">${label}：${counts[key] || 0}</div>`)
    .join("");

  const badge = document.querySelector("#quotaBadge");
  const quota = appState.quota || { status: "unknown", message: "尚未检测" };
  badge.className = `quota ${quota.status || "unknown"}`;
  const text = quota.status === "available" ? "绿色充足" : quota.status === "exhausted" ? "红色耗尽" : "未知";
  badge.textContent = `额度：${text} · ${quota.message || ""}`;
  renderGenerationControls(appState.generation || state.generation);
  renderDetailControls(appState.detail_generation || state.detailGeneration, counts);
}

function renderGenerationControls(generation) {
  const startBtn = document.querySelector("#startBtn");
  const pauseBtn = document.querySelector("#pauseBtn");
  const stopBtn = document.querySelector("#stopBtn");
  const progress = document.querySelector("#queueProgress");
  if (!startBtn || !pauseBtn || !stopBtn || !progress) return;
  const stateName = generation?.state || "idle";
  const running = stateName === "running";
  const paused = stateName === "paused";
  startBtn.disabled = running;
  pauseBtn.disabled = stateName === "idle" || stateName === "stopped";
  pauseBtn.textContent = paused ? "继续生成" : "暂停生成";
  stopBtn.disabled = stateName === "idle" || stateName === "stopped";
  const completed = generation?.completed || 0;
  const total = generation?.total || 0;
  const labelMap = { running: "运行中", paused: "已暂停", stopped: "已停止", idle: "空闲" };
  progress.textContent = `队列：${labelMap[stateName] || stateName} · 已完成 ${completed}/${total}`;
}

function renderDetailControls(detailGeneration, counts = {}) {
  const button = document.querySelector("#detailGenerateBtn");
  if (!button) return;
  const running = detailGeneration?.state === "running" || Boolean(detailGeneration?.running);
  const available = (counts.waiting_confirm || 0) + (counts.awaiting_confirm || 0) + (counts.detail_failed || 0);
  button.disabled = running || available === 0;
  if (running) {
    button.textContent = detailGeneration?.message || `生成中... ${detailGeneration?.completed || 0}/${detailGeneration?.total || 0}`;
  } else {
    button.textContent = available > 0 ? `生成详情页（${available}）` : "生成详情页";
  }
}

function renderFilters(counts) {
  const wrap = document.querySelector("#filters");
  const allCount = Object.values(counts || {}).reduce((sum, value) => sum + value, 0);
  wrap.innerHTML = Object.entries(labels)
    .map(([key, label]) => {
      const count = key === "all" ? allCount : counts[key] || 0;
      return `<button class="${state.status === key ? "active" : ""}" data-status="${key}">${label} ${count}</button>`;
    })
    .join("");
  wrap.querySelectorAll("button").forEach((button) => {
    button.addEventListener("click", () => {
      state.status = button.dataset.status;
      refresh();
    });
  });
}

function renderTasks(tasks) {
  const body = document.querySelector("#taskRows");
  body.innerHTML = tasks
    .map((task) => {
      const img = imageUrl(task);
      const color = [task.color_code, task.color_name].filter(Boolean).join("_");
      return `
        <tr data-id="${task.id}">
          <td><input class="rowSelect" type="checkbox" data-sku="${escapeHtml(task.sku)}" ${state.selectedSkus.has(task.sku) ? "checked" : ""}></td>
          <td>${escapeHtml(task.sku)}</td>
          <td>${escapeHtml(task.spu || "")}</td>
          <td>${escapeHtml(color)}</td>
          <td>${escapeHtml(task.product_name || "")}</td>
          <td><span class="status ${task.status}">${escapeHtml(task.status_label)}</span></td>
          <td>${img ? `<img class="thumb" src="${img}" alt="">` : ""}</td>
          <td title="${escapeHtml(task.error_message || "")}">${escapeHtml(shorten(task.error_message || "", 38))}</td>
          <td><div class="actions">${actionsFor(task)}</div></td>
        </tr>
      `;
    })
    .join("");

  body.querySelectorAll("tr").forEach((row) => {
    row.addEventListener("click", (event) => {
      if (event.target.closest("button")) return;
      const task = state.tasks.find((item) => String(item.id) === row.dataset.id);
      renderPreview(task);
    });
  });

  body.querySelectorAll(".rowSelect").forEach((checkbox) => {
    checkbox.addEventListener("click", (event) => event.stopPropagation());
    checkbox.addEventListener("change", () => {
      if (checkbox.checked) state.selectedSkus.add(checkbox.dataset.sku);
      else state.selectedSkus.delete(checkbox.dataset.sku);
      updateSelectAllState();
    });
  });

  body.querySelectorAll("button[data-action]").forEach((button) => {
    button.addEventListener("click", async () => {
      await taskAction(button.dataset.id, button.dataset.action);
    });
  });
  updateSelectAllState();
}

function updateSelectAllState() {
  const selectAll = document.querySelector("#selectAllRows");
  if (!selectAll) return;
  const visibleSkus = state.tasks.map((task) => task.sku);
  const selectedVisible = visibleSkus.filter((sku) => state.selectedSkus.has(sku));
  selectAll.checked = visibleSkus.length > 0 && selectedVisible.length === visibleSkus.length;
  selectAll.indeterminate = selectedVisible.length > 0 && selectedVisible.length < visibleSkus.length;
}

function selectedSkuList() {
  return Array.from(state.selectedSkus);
}

async function downloadSelectedImages() {
  const skus = selectedSkuList();
  if (!skus.length) {
    alert("请先勾选 SKU");
    return;
  }
  const blob = await api("/api/download/batch", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skus }),
  });
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = `sku_images_${Date.now()}.zip`;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

function filenameFromResponse(response, fallback) {
  const disposition = response.headers.get("content-disposition") || "";
  const utf8Match = disposition.match(/filename\*=UTF-8''([^;]+)/i);
  if (utf8Match) return decodeURIComponent(utf8Match[1]);
  const asciiMatch = disposition.match(/filename="?([^";]+)"?/i);
  if (asciiMatch) return asciiMatch[1];
  return fallback;
}

function downloadBlob(blob, filename) {
  const url = URL.createObjectURL(blob);
  const link = document.createElement("a");
  link.href = url;
  link.download = filename;
  document.body.appendChild(link);
  link.click();
  link.remove();
  URL.revokeObjectURL(url);
}

async function downloadExport(url = "/api/export") {
  const response = await fetch(url);
  if (!response.ok) {
    const data = await response.json().catch(() => ({}));
    throw new Error(data.error || `导出失败：${response.status}`);
  }
  const blob = await response.blob();
  downloadBlob(blob, filenameFromResponse(response, `erp_export_${Date.now()}.xlsx`));
}

function pickExcelFile() {
  return new Promise((resolve) => {
    const input = document.createElement("input");
    let settled = false;
    const finish = (file) => {
      if (settled) return;
      settled = true;
      input.remove();
      resolve(file || null);
    };
    input.type = "file";
    input.accept = ".xlsx,.xlsm";
    input.style.display = "none";
    input.addEventListener("change", () => finish(input.files?.[0] || null), { once: true });
    window.addEventListener("focus", () => {
      setTimeout(() => {
        if (!input.files?.length) finish(null);
      }, 500);
    }, { once: true });
    document.body.appendChild(input);
    input.click();
  });
}

async function deleteSelectedTasks() {
  const skus = selectedSkuList();
  if (!skus.length) {
    alert("请先勾选 SKU");
    return;
  }
  if (!confirm(`确定要删除选中的 ${skus.length} 个 SKU 吗？`)) return;
  await api("/api/tasks", {
    method: "DELETE",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ skus }),
  });
  skus.forEach((sku) => state.selectedSkus.delete(sku));
  await refresh();
}

async function generateDetailImages() {
  if (!(await ensureCodexReady())) return;
  const selected = selectedSkuList();
  const waitingCount = state.tasks.filter((task) =>
    ["waiting_confirm", "awaiting_confirm", "detail_failed"].includes(task.status)
  ).length;
  if (!waitingCount) {
    alert("当前没有可生成详情页的待确认 SKU");
    return;
  }
  const useSelected = selected.length && confirm(`点击“确定”为已勾选的 ${selected.length} 个 SKU 生成详情页；点击“取消”为所有待确认 SKU 生成详情页。`);
  const result = await api("/api/generate/detail", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ mode: useSelected ? "selected" : "all", skus: selected }),
  });
  if (result && result.ok === false) {
    alert(result.error || "没有可生成详情页的 SKU");
  }
  await refresh();
}

function actionsFor(task) {
  const id = task.id;
  const recollage = canRecollage(task) ? `<button data-id="${id}" data-action="recollage">重新拼图</button>` : "";
  const download = task.image_path
    ? `
      <button data-id="${id}" data-action="download-white">下载白底图</button>
      <button data-id="${id}" data-action="download-detail">下载详情页</button>
      <button data-id="${id}" data-action="download-all">下载全部</button>
      <span class="image-count" title="包含 1~9.png、主图.png 和 detail_1~6.png">最多16张图</span>
    `
    : `<button disabled title="图片未生成">下载白底图</button><button disabled title="图片未生成">下载详情页</button><button disabled title="图片未生成">下载全部</button>`;
  if (task.status === "awaiting_confirm" || task.status === "waiting_confirm") {
    return `
      <button class="ok" data-id="${id}" data-action="approve">通过</button>
      <button class="danger" data-id="${id}" data-action="reject">驳回</button>
      <button data-id="${id}" data-action="quality-fail">质检失败</button>
      ${recollage}
      ${download}
    `;
  }
  if (task.status === "failed" || task.status === "quality_failed" || task.status === "rejected") {
    return `<button data-id="${id}" data-action="retry">重试</button>${recollage}${download}`;
  }
  if (task.status === "collaging_failed") {
    return `${recollage}<button data-id="${id}" data-action="retry">重新生成</button>${download}`;
  }
  if (task.status === "quota_exhausted") {
    return `<button class="pause" data-id="${id}" data-action="queue">加入待续跑队列</button>${download}`;
  }
  if (task.status === "pending") return `<button data-id="${id}" data-action="retry">立即生成</button>${download}`;
  return download;
}

async function taskAction(id, action) {
  if (action === "retry" && !(await ensureCodexReady())) return;
  const map = {
    approve: "approve",
    reject: "reject",
    retry: "retry",
    queue: "queue",
    "quality-fail": "quality-fail",
  };
  if (action === "download-main" || action === "download-all" || action === "download-white" || action === "download-detail") {
    const task = state.tasks.find((item) => String(item.id) === String(id));
    if (!task || !task.image_path) {
      alert("图片未生成");
      return;
    }
    const encodedSku = encodeURIComponent(task.sku);
    const downloadMap = {
      "download-main": "main",
      "download-all": "all",
      "download-white": "white",
      "download-detail": "detail",
    };
    window.location.href = `/api/download/${downloadMap[action]}/${encodedSku}`;
    return;
  }
  if (action === "recollage") {
    const task = state.tasks.find((item) => String(item.id) === String(id));
    await reCollage(task);
    return;
  }
  await api(`/api/tasks/${id}/${map[action]}`, {
    method: "POST",
    body: action === "quality-fail" ? JSON.stringify({ message: "人工标记质检失败" }) : undefined,
    headers: action === "quality-fail" ? { "Content-Type": "application/json" } : undefined,
  });
  await refresh();
}

async function renderPreview(task) {
  const preview = document.querySelector("#preview");
  if (!task) {
    preview.innerHTML = `<div class="empty">选择一行预览素材</div>`;
    state.preview = { sku: null, images: [], index: 0 };
    return;
  }
  state.preview = { sku: task.sku, images: [], index: 0 };
  const recollageButton = canRecollage(task)
    ? `<button class="preview-recollage" id="previewRecollage" type="button">重新拼图</button>`
    : "";
  preview.innerHTML = `
    <h2>${escapeHtml(task.sku)}</h2>
    <p>${escapeHtml(task.product_name || "")}</p>
    <div class="preview-image-wrap" id="previewImageWrap">
      <div class="empty">正在加载图片...</div>
    </div>
    <div class="preview-pager">
      <button class="preview-nav" id="previewPrev" type="button">◀</button>
      <span class="preview-meta" id="previewMeta">0/0</span>
      <button class="preview-nav" id="previewNext" type="button">▶</button>
      ${recollageButton}
    </div>
    <h3>提示词</h3>
    <pre>${escapeHtml(task.prompt || "")}</pre>
    <h3>原始资料</h3>
    <pre>${escapeHtml(JSON.stringify(task.raw || {}, null, 2))}</pre>
  `;
  const images = await loadImagesForTask(task);
  if (state.preview.sku !== task.sku) return;
  state.preview.images = images;
  state.preview.index = 0;
  renderPreviewImage();
  preview.querySelector("#previewPrev")?.addEventListener("click", () => movePreview(-1));
  preview.querySelector("#previewNext")?.addEventListener("click", () => movePreview(1));
  preview.querySelector("#previewRecollage")?.addEventListener("click", () => reCollage(task));
}

function canRecollage(task) {
  return Boolean(
    task?.image_folder &&
      (task.status === "collaging_failed" ||
        task.status === "failed" ||
        task.status === "waiting_confirm" ||
        task.status === "awaiting_confirm")
  );
}

async function reCollage(task) {
  if (!task) return;
  try {
    const url = `/api/collage/${encodeURIComponent(task.sku)}`;
    console.log("[reCollage]", { sku: task.sku, url });
    await api(url, { method: "POST" });
    await refresh();
    const updated =
      state.tasks.find((item) => String(item.id) === String(task.id)) ||
      state.tasks.find((item) => String(item.sku) === String(task.sku));
    if (updated) await renderPreview(updated);
  } catch (error) {
    alert(error.message || "重新拼图失败");
    await refresh();
  }
}

function renderPreviewImage() {
  const wrap = document.querySelector("#previewImageWrap");
  const meta = document.querySelector("#previewMeta");
  const prev = document.querySelector("#previewPrev");
  const next = document.querySelector("#previewNext");
  if (!wrap || !meta) return;

  const images = state.preview.images || [];
  if (!images.length) {
    wrap.innerHTML = `<div class="empty">图片未生成</div>`;
    meta.textContent = "0/0";
    if (prev) prev.disabled = true;
    if (next) next.disabled = true;
    return;
  }

  const index = Math.max(0, Math.min(state.preview.index, images.length - 1));
  state.preview.index = index;
  const image = images[index];
  console.log("[previewImage]", { index: index + 1, total: images.length, image });
  wrap.innerHTML = `<img class="preview-main fade-in" src="${image.url}" alt="${escapeHtml(image.name || "")}" onerror="this.replaceWith(Object.assign(document.createElement('div'), {className: 'empty', textContent: '图片未生成'}))">`;
  meta.textContent = `${index + 1}/${images.length} ${image.name ? `· ${image.name}` : ""}`;
  if (prev) prev.disabled = images.length <= 1;
  if (next) next.disabled = images.length <= 1;
}

function movePreview(delta) {
  const images = state.preview.images || [];
  if (!images.length) return;
  state.preview.index = (state.preview.index + delta + images.length) % images.length;
  renderPreviewImage();
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;");
}

function shorten(value, max) {
  return value.length > max ? `${value.slice(0, max)}...` : value;
}

document.querySelector("#excelInput").addEventListener("change", async (event) => {
  const file = event.target.files[0];
  if (!file) return;
  await api("/api/import", {
    method: "POST",
    body: await file.arrayBuffer(),
    headers: { "X-Filename": encodeURIComponent(file.name) },
  });
  event.target.value = "";
  await refresh();
});

document.querySelector("#startBtn").addEventListener("click", async () => {
  if (!(await ensureCodexReady())) return;
  await api("/api/generate/start", { method: "POST" });
  await refresh();
});

document.querySelector("#pauseBtn").addEventListener("click", async () => {
  if (state.generation?.state === "paused") {
    if (!(await ensureCodexReady())) return;
    await api("/api/generate/start", { method: "POST" });
  } else {
    await api("/api/generate/pause", { method: "POST" });
  }
  await refresh();
});

document.querySelector("#stopBtn").addEventListener("click", async () => {
  await api("/api/generate/stop", { method: "POST" });
  await refresh();
});

document.querySelector("#selectAllRows").addEventListener("change", (event) => {
  if (event.target.checked) {
    state.tasks.forEach((task) => state.selectedSkus.add(task.sku));
  } else {
    state.tasks.forEach((task) => state.selectedSkus.delete(task.sku));
  }
  renderTasks(state.tasks);
});

document.querySelector("#batchDownloadBtn").addEventListener("click", downloadSelectedImages);
document.querySelector("#deleteSelectedBtn").addEventListener("click", deleteSelectedTasks);
document.querySelector("#detailGenerateBtn").addEventListener("click", generateDetailImages);
document.querySelector("#exportExcelBtn").addEventListener("click", exportExcel);

document.querySelector("#apiKeyOpenBtn").addEventListener("click", showApiKeyDialog);
document.querySelector("#apiKeyCancel").addEventListener("click", () => {
  document.querySelector("#apiKeyDialog").close();
});
document.querySelector("#accessTokenToggle").addEventListener("click", () => {
  const input = document.querySelector("#accessTokenInput");
  const button = document.querySelector("#accessTokenToggle");
  const hidden = input.type === "password";
  input.type = hidden ? "text" : "password";
  button.textContent = hidden ? "隐藏" : "显示";
});
document.querySelector("#apiKeyForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const input = document.querySelector("#apiKeyInput");
  const tokenInput = document.querySelector("#accessTokenInput");
  const message = document.querySelector("#apiKeyMessage");
  try {
    const result = await api("/api/codex/set-key", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        api_base_url: input.value.trim(),
        access_token: tokenInput.value.trim(),
      }),
    });
    message.textContent = result.message || "本地服务地址已保存";
    message.className = "formMessage success";
    input.value = "";
    tokenInput.value = "";
    await refreshCodexAuth(true);
    await refreshApiKeyInfo();
    document.querySelector("#apiKeyDialog").close();
  } catch (error) {
    message.textContent = error.message;
    message.className = "formMessage error";
  }
});

document.querySelector("#searchInput").addEventListener("input", (event) => {
  state.q = event.target.value.trim();
  clearTimeout(window.searchTimer);
  window.searchTimer = setTimeout(refresh, 200);
});

const dialog = document.querySelector("#manualDialog");
document.querySelector("#manualOpen").addEventListener("click", () => dialog.showModal());
document.querySelector("#manualCancel").addEventListener("click", () => dialog.close());
document.querySelector("#manualForm").addEventListener("submit", async (event) => {
  event.preventDefault();
  const form = new FormData(event.currentTarget);
  await api("/api/manual", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(Object.fromEntries(form.entries())),
  });
  event.currentTarget.reset();
  dialog.close();
  await refresh();
});

refreshCodexAuth().catch((error) => {
  state.codex = { authenticated: false, message: error.message };
  renderCodexAuth(state.codex);
});
refreshApiKeyInfo().catch(() => {
  renderApiKeyInfo({ authenticated: false, has_api_base_url: false });
});
refresh();
setInterval(refresh, 5000);

function findTaskBySkuOrId(skuOrId) {
  return state.tasks.find((task) => String(task.sku) === String(skuOrId) || String(task.id) === String(skuOrId));
}

async function generateImage(skuOrId) {
  const task = findTaskBySkuOrId(skuOrId);
  if (!task) throw new Error(`未找到 SKU：${skuOrId}`);
  await taskAction(task.id, "retry");
}

async function approveSku(skuOrId) {
  const task = findTaskBySkuOrId(skuOrId);
  if (!task) throw new Error(`未找到 SKU：${skuOrId}`);
  await taskAction(task.id, "approve");
}

async function retrySku(skuOrId) {
  const task = findTaskBySkuOrId(skuOrId);
  if (!task) throw new Error(`未找到 SKU：${skuOrId}`);
  await taskAction(task.id, "retry");
}

async function requeueSku(skuOrId) {
  const task = findTaskBySkuOrId(skuOrId);
  if (!task) throw new Error(`未找到 SKU：${skuOrId}`);
  await taskAction(task.id, "queue");
}

async function exportExcel() {
  try {
    const source = await api("/api/export/source-status");
    if (source.exists) {
      await downloadExport("/api/export");
      return;
    }

    alert("找不到原始 Excel 文件，请手动选择。若取消选择，将导出一份新建表格。");
    const file = await pickExcelFile();
    if (!file) {
      await downloadExport("/api/export");
      return;
    }

    const response = await fetch("/api/export/source", {
      method: "POST",
      headers: { "X-Filename": encodeURIComponent(file.name) },
      body: await file.arrayBuffer(),
    });
    if (!response.ok) {
      const data = await response.json().catch(() => ({}));
      throw new Error(data.error || `导出失败：${response.status}`);
    }
    const blob = await response.blob();
    downloadBlob(blob, filenameFromResponse(response, `erp_export_${Date.now()}.xlsx`));
  } catch (error) {
    alert(error.message || error);
  }
}

window.generateImage = generateImage;
window.approveSku = approveSku;
window.retrySku = retrySku;
window.requeueSku = requeueSku;
window.exportExcel = exportExcel;
