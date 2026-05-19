const state = {
  currentJob: null,
  latestData: null,
  toastTimer: null,
};

const $ = (id) => document.getElementById(id);

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) {
    const text = await response.text();
    throw new Error(text || response.statusText);
  }
  return response.json();
}

function showToast(message, type = "success") {
  const toast = $("toast");
  toast.textContent = message;
  toast.className = `toast ${type}`;
  toast.hidden = false;
  clearTimeout(state.toastTimer);
  state.toastTimer = setTimeout(() => {
    toast.hidden = true;
  }, type === "error" ? 9000 : 5500);
}

function jobSummary(job) {
  if (!job) return "Job finished.";
  const lastLog = job.logs?.[job.logs.length - 1] || "";
  if (job.status === "done") {
    if (job.action === "tts") return "Tạo MP3 thành công.";
    if (job.action === "render" || job.action === "track-render" || job.action === "track-rerender") return "Render video thành công.";
    if (job.action === "daily-dry-run" || job.action === "track-dry-run") return "Dry run thành công.";
    if (
      job.action === "daily-upload" ||
      job.action === "track-upload" ||
      job.action === "track-upload-normal" ||
      job.action === "track-upload-short"
    ) return "Upload hoàn tất.";
    if (job.action === "track-delete") return "Đã xóa track local.";
    return `${job.action} thành công.`;
  }
  return lastLog || `${job.action} lỗi.`;
}

async function refresh() {
  const data = await requestJson("/api/status");
  state.latestData = data;

  $("connection").textContent = "Ready";
  $("audioCount").textContent = data.counts.audio;
  $("imageCount").textContent = data.counts.images;
  $("pendingCount").textContent = data.counts.pending;
  $("outputCount").textContent = data.counts.output;
  $("uploadCount").textContent = data.counts.uploads;
  $("trackSummary").textContent = `${data.counts.tracks} tracks · ${data.counts.pending} pending`;
  $("configEditor").value = JSON.stringify(data.config, null, 2);

  renderAccounts(data.accounts, data.active_account);
  renderVoices(data.tts_voices);
  renderStoryAudio(data.files.audio);
  renderMode(data);
  renderMetadataPreview(data.tracks.filter((track) => track.upload_needed), data.upload_policy);
  renderVideoPreview(data.tracks);
  renderCollection(data.collection);
  renderTracks(data.tracks);
  renderJobs(data.jobs);

  const badge = $("credentialBadge");
  badge.textContent = data.credentials_ready ? "Credentials ready" : "No credentials";
  badge.classList.toggle("warn", !data.credentials_ready);

  $("schedulePreview").innerHTML = data.schedule_preview
    .map((item, index) => `<div><strong>Slot ${index + 1}</strong><span>${formatSlot(item)}</span></div>`)
    .join("");
}

function renderTracks(tracks) {
  if (!tracks.length) {
    $("trackRows").innerHTML = '<tr><td colspan="7" class="muted">No tracks</td></tr>';
    return;
  }

  $("trackRows").innerHTML = tracks
    .map((track) => {
      const bothUploaded = track.normal_uploaded && track.short_uploaded;
      const bothRendered = track.video_exists && track.short_exists;
      const status = bothUploaded
        ? '<span class="status done">Uploaded</span>'
        : track.normal_uploaded && !track.short_uploaded
          ? '<span class="status waiting">Missing short</span>'
          : bothRendered
            ? '<span class="status ready">Rendered</span>'
            : '<span class="status waiting">Pending</span>';

      const normal = track.video_exists ? track.video : "Not rendered";
      const short = track.short_exists ? track.short_video : "Not rendered";
      const assets = track.image || (track.has_thumbnail ? "Thumbnail ready" : "No image");

      return `<tr>
        <td>${escapeHtml(track.title)}</td>
        <td>${escapeHtml(track.audio)}</td>
        <td>${escapeHtml(assets)}</td>
        <td>${escapeHtml(normal)}</td>
        <td>${escapeHtml(short)}</td>
        <td>${status}</td>
        <td class="rowActions">
          <button type="button" data-track-action="rerender" data-audio="${escapeHtml(track.audio)}">Re-render</button>
          <button type="button" data-track-action="dry-run" data-audio="${escapeHtml(track.audio)}">Dry Run</button>
          <button type="button" data-track-action="upload-normal" data-audio="${escapeHtml(track.audio)}" ${track.normal_uploaded ? "disabled" : ""}>Upload Long</button>
          <button type="button" data-track-action="upload-short" data-audio="${escapeHtml(track.audio)}" ${track.short_uploaded ? "disabled" : ""}>Upload Short</button>
          <button type="button" data-track-action="upload" data-audio="${escapeHtml(track.audio)}" ${track.normal_uploaded && track.short_uploaded ? "disabled" : ""}>Upload Both</button>
          <button type="button" data-track-action="skip" data-audio="${escapeHtml(track.audio)}">Skip</button>
          ${!track.normal_uploaded && !track.short_uploaded ? `<button type="button" class="danger" data-track-action="delete" data-audio="${escapeHtml(track.audio)}">Delete</button>` : ""}
          ${renderYoutubeLinks(track.youtube_urls)}
        </td>
      </tr>`;
    })
    .join("");
}

function renderJobs(jobs) {
  if (!jobs.length && !state.currentJob) {
    $("jobStatus").textContent = "";
    $("jobLog").textContent = "";
    return;
  }
  const job = jobs.find((item) => item.id === state.currentJob) || jobs[0];
  $("jobStatus").textContent = job ? `${job.action} · ${job.status}` : "";
  $("jobLog").textContent = job ? job.logs.join("\n") : "";
  if (job && (job.status === "running" || job.status === "queued")) {
    setJobsCollapsed(false);
  }
}

async function runAction(action) {
  const data = await requestJson(`/api/action/${action}`, { method: "POST" });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runTrackAction(action, audio) {
  const data = await requestJson("/api/track-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action, audio }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function pollJob(jobId) {
  for (;;) {
    const job = await requestJson(`/api/jobs/${jobId}`);
    $("jobStatus").textContent = `${job.action} · ${job.status}`;
    $("jobLog").textContent = job.logs.join("\n");
    if (job.status !== "running" && job.status !== "queued") {
      if (job.status === "failed") showToast(jobSummary(job), "error");
      return job;
    }
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

function setJobsCollapsed(collapsed) {
  $("jobsPanel").classList.toggle("collapsed", collapsed);
  $("toggleJobsBtn").textContent = collapsed ? "Show" : "Hide";
}

async function uploadFiles(kind, input) {
  if (!input.files.length) return;
  const form = new FormData();
  form.append("kind", kind);
  for (const file of input.files) {
    form.append("files", file);
  }
  await requestJson("/api/upload-files", { method: "POST", body: form });
  input.value = "";
  await refresh();
  showToast("Upload file thành công.");
}

async function saveConfig() {
  const payload = JSON.parse($("configEditor").value);
  await requestJson("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refresh();
  showToast("Đã lưu config.");
}

async function openFolder(folder) {
  await requestJson(`/api/open-folder/${folder}`, { method: "POST" });
}

async function setAccount(accountId) {
  await requestJson(`/api/account/${accountId}`, { method: "POST" });
  await refresh();
}

async function generateVoice() {
  const title = $("ttsTitle").value.trim();
  const text = $("ttsText").value.trim();
  const voice = $("ttsVoice").value;
  const button = $("ttsBtn");
  const status = $("ttsStatus");

  if (!title || !text) {
    status.textContent = "Please enter both title and story text.";
    status.classList.add("warn");
    return null;
  }

  button.disabled = true;
  button.textContent = "Generating...";
  status.textContent = "Creating voice MP3...";
  status.classList.remove("warn");

  try {
    const data = await requestJson("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, text, voice }),
    });
    state.currentJob = data.job_id;
    await refresh();
    const job = await pollJob(data.job_id);
    status.textContent = job.status === "done" ? "Voice MP3 created." : "Voice generation failed. Check Jobs.";
    status.classList.toggle("warn", job.status !== "done");
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    await refresh();
    if (job.status === "done" && state.latestData.files.audio.length) {
      $("storyAudioSelect").value = state.latestData.files.audio[0];
      updateVoicePreview();
    }
    return job;
  } catch (error) {
    status.textContent = error.message || "Voice generation failed.";
    status.classList.add("warn");
    showToast(status.textContent, "error");
    return null;
  } finally {
    button.disabled = false;
    button.textContent = "Generate Voice";
  }
}

async function attachStoryAssets() {
  const status = $("ttsStatus");
  const audioName = $("storyAudioSelect").value;
  const imagesInput = $("storyImagesInput");
  const thumbInput = $("storyThumbInput");

  if (!audioName) {
    status.textContent = "Please select a story MP3.";
    status.classList.add("warn");
    return false;
  }
  if (!imagesInput.files.length || imagesInput.files.length > 5) {
    status.textContent = "Please select 1 to 5 images.";
    status.classList.add("warn");
    return false;
  }

  const form = new FormData();
  form.append("audio_name", audioName);
  for (const file of imagesInput.files) {
    form.append("images", file);
  }
  if (thumbInput.files.length) {
    form.append("thumbnail", thumbInput.files[0]);
  }

  $("storyAssetsBtn").disabled = true;
  status.textContent = "Attaching images and thumbnail...";
  status.classList.remove("warn");

  try {
    await requestJson("/api/story-assets", { method: "POST", body: form });
    imagesInput.value = "";
    thumbInput.value = "";
    status.textContent = "Story assets attached.";
    showToast("Gắn ảnh/thumbnail thành công.");
    await refresh();
    return true;
  } catch (error) {
    status.textContent = error.message || "Could not attach story assets.";
    status.classList.add("warn");
    showToast(status.textContent, "error");
    return false;
  } finally {
    $("storyAssetsBtn").disabled = false;
  }
}

async function storyOneClick() {
  const data = state.latestData;
  const status = $("ttsStatus");
  if (!data || data.mode !== "story") {
    status.textContent = "Switch to Story mode first.";
    status.classList.add("warn");
    return;
  }

  $("storyOneClickBtn").disabled = true;
  status.classList.remove("warn");
  try {
    status.textContent = "Step 1/4: creating voice MP3...";
    const voiceJob = await generateVoice();
    if (!voiceJob || voiceJob.status !== "done") return;

    await refresh();
    const audioFiles = state.latestData.files.audio;
    if (audioFiles.length) $("storyAudioSelect").value = audioFiles[0];

    status.textContent = "Step 2/4: attaching images and thumbnail...";
    const attached = await attachStoryAssets();
    if (!attached) return;

    status.textContent = "Step 3/4: rendering video...";
    const renderJob = await runAction("render");
    if (renderJob.status !== "done") {
      status.textContent = "Render failed. Check Jobs.";
      status.classList.add("warn");
      return;
    }

    status.textContent = "Step 4/4: running upload dry-run...";
    const dryJob = await runAction("daily-dry-run");
    status.textContent = dryJob.status === "done" ? "Story is ready. Dry-run passed." : "Dry-run failed. Check Jobs.";
    status.classList.toggle("warn", dryJob.status !== "done");
  } finally {
    $("storyOneClickBtn").disabled = false;
  }
}

function escapeHtml(value) {
  return String(value)
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatSlot(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value;
  return date.toLocaleString("vi-VN", {
    dateStyle: "short",
    timeStyle: "short",
  });
}

function renderCollection(collection) {
  const panel = $("collectionPanel");
  const button = $("collectionBtn");
  panel.classList.toggle("ready", collection.ready);
  button.disabled = !collection.ready;

  if (!collection.enabled) {
    $("collectionText").textContent = "Collection is disabled in config.";
    $("collectionVideos").innerHTML = "";
    return;
  }

  $("collectionText").textContent = collection.ready
    ? `Đã đủ ${collection.size} video thường. Có thể tạo tuyển tập ngay.`
    : `Cần thêm ${collection.needed_count} video thường nữa để tạo tuyển tập.`;

  $("collectionVideos").innerHTML = collection.videos
    .map((video, index) => `<span>${index + 1}. ${escapeHtml(video)}</span>`)
    .join("");
}

function renderAccounts(accounts, activeAccount) {
  const select = $("accountSelect");
  const previous = select.value;
  select.innerHTML = Object.entries(accounts)
    .map(([id, account]) => {
      const selected = id === activeAccount ? "selected" : "";
      return `<option value="${escapeHtml(id)}" ${selected}>${escapeHtml(account.label || id)}</option>`;
    })
    .join("");
  if (previous && previous !== activeAccount) select.value = activeAccount;
}

function renderVoices(voices) {
  const select = $("ttsVoice");
  const current = select.value || "vi-VN-HoaiMyNeural";
  select.innerHTML = voices
    .map((voice) => {
      const selected = voice.id === current ? "selected" : "";
      return `<option value="${escapeHtml(voice.id)}" ${selected}>${escapeHtml(voice.label)}</option>`;
    })
    .join("");
}

function renderStoryAudio(audioFiles) {
  const select = $("storyAudioSelect");
  const current = select.value;
  if (!audioFiles.length) {
    select.innerHTML = '<option value="">No story MP3 yet</option>';
    updateVoicePreview();
    return;
  }
  select.innerHTML = audioFiles
    .map((audio) => {
      const selected = audio === current ? "selected" : "";
      return `<option value="${escapeHtml(audio)}" ${selected}>${escapeHtml(audio)}</option>`;
    })
    .join("");
  if (current && audioFiles.includes(current)) select.value = current;
  updateVoicePreview();
}

function updateVoicePreview() {
  const select = $("storyAudioSelect");
  const audioName = select.value;
  const player = $("voicePreview");
  const info = $("voicePreviewInfo");
  if (!audioName) {
    player.removeAttribute("src");
    player.load();
    info.textContent = "No MP3 selected";
    return;
  }
  const url = `/api/audio/${encodeURIComponent(audioName)}`;
  if (player.getAttribute("src") !== url) {
    player.setAttribute("src", url);
    player.load();
  }
  info.textContent = audioName;
}

function renderMode(data) {
  const isStory = data.active_account === "account1";
  const isBobo = data.active_account === "account2";
  const isNhahn = data.active_account === "account3";

  $("storyModeBtn").classList.toggle("active", isStory);
  $("boboModeBtn").classList.toggle("active", isBobo);
  $("nhahnModeBtn").classList.toggle("active", isNhahn);
  $("modeTitle").textContent = isStory ? "Story" : isBobo ? "Bobo Remix" : "Nhahn Bolero Buồn";
  $("modeInfo").textContent = `${data.accounts[data.active_account]?.label || data.active_account} · ${data.paths.audio_dir} · ${data.upload_policy.warning}`;
  $("storyPanel").hidden = !isStory;
  $("storyOneClickBtn").disabled = !isStory;
}

function renderMetadataPreview(tracks, policy) {
  $("uploadPolicy").textContent = `${policy.videos_per_day} video/day`;
  const hasLimitWarning = Boolean(policy.upload_limit_warning);
  $("uploadPolicy").classList.toggle("warn", policy.videos_per_day > 1 || hasLimitWarning);
  if (hasLimitWarning) $("uploadPolicy").textContent = "Upload limit hit";

  if (!tracks.length) {
    $("metadataPreview").innerHTML = `<p class="muted">${escapeHtml(policy.upload_limit_warning || "No pending upload preview.")}</p>`;
    return;
  }

  $("metadataPreview").innerHTML = tracks
    .slice(0, 3)
    .map((track, index) => `<article>
      <div><strong>${escapeHtml(track.title)}</strong><span>${track.has_thumbnail ? "Thumbnail ready" : "No thumbnail"}</span></div>
      ${track.thumbnail_url ? `<img class="thumbPreview" src="${escapeHtml(track.thumbnail_url)}" alt="Thumbnail preview" />` : ""}
      <label>
        <span>Title</span>
        <input id="metaTitle${index}" value="${escapeHtml(track.title)}" />
      </label>
      <label>
        <span>Description</span>
        <textarea id="metaDescription${index}" class="metaText">${escapeHtml(track.description)}</textarea>
      </label>
      <label>
        <span>Tags</span>
        <input id="metaTags${index}" value="${escapeHtml(track.tags.join(", "))}" />
      </label>
      <input id="metaCategory${index}" type="hidden" value="${escapeHtml(track.category_id)}" />
      <div class="previewActions">
        <small>${escapeHtml(track.tags.join(", "))} · Category ${escapeHtml(track.category_id)}</small>
        <button type="button" data-save-metadata="true" data-index="${index}" data-audio="${escapeHtml(track.audio)}">Save Metadata</button>
      </div>
      ${renderYoutubeLinks(track.youtube_urls)}
    </article>`)
    .join("");
}

function renderVideoPreview(tracks) {
  const select = $("videoPreviewSelect");
  const current = select.value;
  const videos = [];
  for (const track of tracks) {
    if (track.video_url) {
      videos.push({ label: `${track.audio} · Normal`, url: track.video_url, info: track.video });
    }
    if (track.short_video_url) {
      videos.push({ label: `${track.audio} · Short`, url: track.short_video_url, info: track.short_video });
    }
  }

  if (!videos.length) {
    select.innerHTML = '<option value="">No rendered video yet</option>';
    $("videoPreview").removeAttribute("src");
    $("videoPreview").load();
    $("videoPreviewInfo").textContent = "Render a track first, then preview it here before uploading.";
    return;
  }

  select.innerHTML = videos
    .map((video) => {
      const selected = video.url === current ? "selected" : "";
      return `<option value="${escapeHtml(video.url)}" data-info="${escapeHtml(video.info)}" ${selected}>${escapeHtml(video.label)}</option>`;
    })
    .join("");
  if (current && videos.some((video) => video.url === current)) select.value = current;
  updateVideoPreview();
}

function updateVideoPreview() {
  const select = $("videoPreviewSelect");
  const url = select.value;
  if (!url) return;
  if ($("videoPreview").getAttribute("src") !== url) {
    $("videoPreview").setAttribute("src", url);
    $("videoPreview").load();
  }
  const selected = select.options[select.selectedIndex];
  $("videoPreviewInfo").textContent = selected?.dataset.info || "";
}

function renderYoutubeLinks(urls) {
  const links = Object.entries(urls || {});
  if (!links.length) return "";
  return `<div class="youtubeLinks">${links
    .map(([type, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(type)} on YouTube</a>`)
    .join("")}</div>`;
}

async function saveMetadataOverride(audio, index) {
  await requestJson("/api/metadata-override", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      audio,
      title: $(`metaTitle${index}`).value,
      description: $(`metaDescription${index}`).value,
      tags: $(`metaTags${index}`).value,
      category_id: $(`metaCategory${index}`).value,
    }),
  });
  await refresh();
  showToast("Đã lưu metadata.");
}

$("refreshBtn").addEventListener("click", refresh);
$("renderBtn").addEventListener("click", () => runAction("render"));
$("dryBtn").addEventListener("click", () => runAction("daily-dry-run"));
$("syncBtn").addEventListener("click", () => runAction("sync-state"));
$("uploadBtn").addEventListener("click", () => runAction("daily-upload"));
$("collectionBtn").addEventListener("click", () => runAction("create-collection"));
$("toggleJobsBtn").addEventListener("click", () => setJobsCollapsed(!$("jobsPanel").classList.contains("collapsed")));
$("saveConfigBtn").addEventListener("click", (event) => {
  event.preventDefault();
  saveConfig().catch((error) => showToast(error.message, "error"));
});
$("accountSelect").addEventListener("change", (event) => setAccount(event.target.value));
$("storyModeBtn").addEventListener("click", () => setAccount("account1"));
$("boboModeBtn").addEventListener("click", () => setAccount("account2"));
$("nhahnModeBtn").addEventListener("click", () => setAccount("account3"));
$("ttsBtn").addEventListener("click", generateVoice);
$("storyOneClickBtn").addEventListener("click", storyOneClick);
$("storyAssetsBtn").addEventListener("click", attachStoryAssets);
$("storyAudioSelect").addEventListener("change", updateVoicePreview);
$("videoPreviewSelect").addEventListener("change", updateVideoPreview);
$("audioUpload").addEventListener("change", (event) => uploadFiles("audio", event.target));
$("imageUpload").addEventListener("change", (event) => uploadFiles("image", event.target));
$("thumbUpload").addEventListener("change", (event) => uploadFiles("thumbnail", event.target));

for (const button of document.querySelectorAll("[data-folder]")) {
  button.addEventListener("click", () => openFolder(button.dataset.folder));
}

document.addEventListener("click", async (event) => {
  const actionButton = event.target.closest("[data-track-action]");
  if (actionButton) {
    if (
      actionButton.dataset.trackAction === "delete" &&
      !window.confirm(`Delete local files for "${actionButton.dataset.audio}"?`)
    ) {
      return;
    }
    actionButton.disabled = true;
    try {
      await runTrackAction(actionButton.dataset.trackAction, actionButton.dataset.audio);
    } finally {
      actionButton.disabled = false;
    }
    return;
  }

  const saveButton = event.target.closest("[data-save-metadata]");
  if (saveButton) {
    saveButton.disabled = true;
    try {
      await saveMetadataOverride(saveButton.dataset.audio, saveButton.dataset.index);
    } finally {
      saveButton.disabled = false;
    }
  }
});

refresh().catch((error) => {
  $("connection").textContent = error.message;
  showToast(error.message, "error");
});
