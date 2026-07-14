const state = {
  activePage: location.hash.replace("#", "") || "dashboard",
  currentJob: null,
  latestData: null,
  lastStoryVoiceDefault: "",
  selectedCollections: new Set(),
  selectedLongMergeVideos: new Set(),
  lyricsRows: [],
  toastTimer: null,
  refreshing: false,
  conversationAudio: "",
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
  }, type === "error" ? 9000 : 5200);
}

function refreshIcons() {
  if (window.lucide) window.lucide.createIcons();
}

function escapeHtml(value) {
  return String(value ?? "")
    .replaceAll("&", "&amp;")
    .replaceAll("<", "&lt;")
    .replaceAll(">", "&gt;")
    .replaceAll('"', "&quot;")
    .replaceAll("'", "&#039;");
}

function formatSlot(value) {
  const date = new Date(value);
  if (Number.isNaN(date.getTime())) return value || "";
  return date.toLocaleString("en-US", {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "numeric",
    minute: "2-digit",
  });
}

function channelName(data = state.latestData) {
  if (!data) return "";
  return data.accounts?.[data.active_account]?.label || data.active_account;
}

function setPage(page) {
  state.activePage = page || "dashboard";
  document.querySelectorAll(".page").forEach((item) => item.classList.remove("active"));
  $(`page-${state.activePage}`)?.classList.add("active");
  document.querySelectorAll(".navItem").forEach((item) => {
    item.classList.toggle("active", item.dataset.page === state.activePage);
  });
  if (location.hash !== `#${state.activePage}`) history.replaceState(null, "", `#${state.activePage}`);
}

async function refresh(options = {}) {
  if (state.refreshing) return state.latestData;
  state.refreshing = true;
  try {
    const data = await requestJson("/api/status");
    state.latestData = data;

    $("connection").textContent = options.auto ? "Connected" : "Connected";
    $("audioCount").textContent = data.counts.audio;
    $("imageCount").textContent = data.counts.images;
    $("pendingCount").textContent = data.counts.pending;
    $("outputCount").textContent = data.counts.output;
    $("uploadCount").textContent = data.counts.uploads;

    $("dashboardSub").textContent = `Overview for ${channelName(data)}`;
    $("trackSummary").textContent = `Manage all tracks for ${channelName(data)}`;
    $("uploadsSub").textContent = `Upload audio, images, and thumbnails for ${channelName(data)}`;
    $("collectionsSub").textContent = `Create and manage video collections for ${channelName(data)}`;
    $("uploadPolicy").textContent = data.upload_policy?.upload_limit_warning || data.upload_policy?.warning || "Auto render and upload when files are ready.";

    if (document.activeElement !== $("configEditor")) {
      $("configEditor").value = JSON.stringify(data.config, null, 2);
    }

    renderAccounts(data);
    renderStatus(data);
    renderSchedule(data.schedule_preview || []);
    renderQueue(data);
    renderTracks(data.tracks || [], data.mode);
    renderManualUploads(data);
    renderUploads(data);
    renderCollection(data.collection || {});
    renderStorageCleanup(data.storage || {});
    renderFullAuto(data.fullauto || {});
    renderStoryBeforeSleep(data.story_before_sleep || {});
    renderJobs(data.jobs || []);
    renderVoices(data.tts_voices || []);
    renderStoryAudio(data.files?.audio || []);
    renderLyricsAudio(data.lyrics_files?.audio || []);
    renderConversationAudio(data.conversation_files?.audio || []);
    renderTikTokDemo(data);
    refreshIcons();
    return data;
  } finally {
    state.refreshing = false;
  }
}

function renderAccounts(data) {
  const select = $("accountSelect");
  if (!select) return;
  select.innerHTML = Object.entries(data.accounts || {})
    .map(([id, account]) => `<option value="${escapeHtml(id)}" ${id === data.active_account ? "selected" : ""}>${escapeHtml(account.label || id)}</option>`)
    .join("");
}

function renderStatus(data) {
  $("credentialBadge").textContent = data.credentials_ready && data.token_ready ? "Credentials OK" : "Needs Credentials";
  $("credentialBadge").classList.toggle("warn", !(data.credentials_ready && data.token_ready));
  $("youtubeApiState").textContent = data.credentials_ready ? "Connected" : "Missing";
  $("youtubeTokenState").textContent = data.token_ready ? "Valid" : "Missing";
}

function renderFullAuto(fullauto) {
  const startBtn = $("fullAutoStartBtn");
  const longStartBtn = $("fullAutoLongStartBtn");
  const merge1HourBtn = $("fullAutoMerge1HourBtn");
  const mergeUpload1HourBtn = $("fullAutoMergeUpload1HourBtn");
  const bulkBtn = $("fullAutoBulkBtn");
  if (!startBtn) return;
  const modelSelect = $("fullAutoModelSelect");
  const promptCount = fullauto.prompt_count || 0;
  const imageCount = fullauto.image_count || 0;
  const activeAccount = state.latestData?.active_account || "";
  const activeChannel = state.latestData?.accounts?.[activeAccount];
  const fullAutoSupported = Boolean(fullauto.upload_accounts?.[activeAccount]);
  const selectedChannelLabel =
    activeChannel?.label ||
    fullauto.upload_accounts?.[activeAccount]?.label ||
    activeAccount ||
    "No channel selected";
  const englishFullAuto = activeAccount === "account4";
  const selectedValue = `${fullauto.provider || "gemini"}|${fullauto.model || "gemini-2.5-flash"}`;
  const options = [
    ["gemini|gemini-2.5-flash", "Gemini 2.5 Flash"],
    ...(fullauto.ollama_models || []).map((model) => [`ollama|${model}`, `Ollama - ${model}`]),
  ];
  if (!options.some(([value]) => value === selectedValue)) {
    options.push([selectedValue, `${fullauto.provider === "ollama" ? "Ollama" : "Gemini"} - ${fullauto.model}`]);
  }
  modelSelect.innerHTML = options
    .map(([value, label]) => `<option value="${escapeHtml(value)}" ${value === selectedValue ? "selected" : ""}>${escapeHtml(label)}</option>`)
    .join("");
  $("fullAutoStats").textContent =
    `${promptCount} prompt(s), ${imageCount} image(s) in the pool. ` +
    `Short Images: ${fullauto.paths?.images || "not configured"}.`;
  $("fullAutoPolicy").textContent = fullauto.enabled
    ? `Uses ${fullauto.provider === "ollama" ? `local Ollama ${fullauto.model}` : `Gemini ${fullauto.model}`}, ${englishFullAuto ? "English" : "Vietnamese"} voices, pooled images, then schedules Story Shorts only.`
    : "Full Auto is unavailable.";
  $("fullAutoSub").textContent = "Creates Buddhist content and uploads it to the globally selected channel";
  $("fullAutoChannelName").textContent = selectedChannelLabel;
  $("fullAutoChannelHint").textContent = fullAutoSupported
    ? "This section follows the global channel selector in the header."
    : "This channel does not support Full Auto. Pick a supported story channel in the global selector.";
  startBtn.disabled = !fullauto.enabled || !fullAutoSupported || promptCount < 1 || imageCount < 5;
  if (bulkBtn) {
    bulkBtn.disabled = !fullauto.enabled || promptCount < 1;
  }

  if (longStartBtn) {
    const longPromptCount = fullauto.long_prompt_count || 0;
    const longImageCount = fullauto.long_image_count || 0;
    const longRequiredImageCount = fullauto.long_required_image_count || 10;
    longStartBtn.disabled = !fullauto.enabled || !fullAutoSupported || longPromptCount < 1 || longImageCount < longRequiredImageCount;
    $("fullAutoLongStats").textContent =
      `${fullauto.long_target_minutes || 60}-minute mode: ${longPromptCount} prompt(s), ${longImageCount}/${longRequiredImageCount} horizontal image(s), ` +
      `${fullauto.long_effect_count || 0} effect(s), ${fullauto.long_wave_count || 0} wave asset(s), ${fullauto.long_sticker_count || 0} sticker(s). ` +
      `One click creates one ${fullauto.long_target_minutes || 60}-minute video and uploads it as a normal video.`;
  }

  const twentyMinStartBtn = $("fullAutoTwentyMinStartBtn");
  if (twentyMinStartBtn) {
    const tmPromptCount = fullauto.twenty_min_prompt_count || 0;
    const tmImageCount = fullauto.twenty_min_image_count || 0;
    const tmRequiredImageCount = fullauto.twenty_min_required_image_count || 5;
    const twentyMinStats = $("fullAutoTwentyMinStats");
    const missingPrompts = tmPromptCount < 1;
    const missingImages = tmImageCount < tmRequiredImageCount;
    twentyMinStartBtn.disabled = !fullauto.enabled || !fullAutoSupported || missingPrompts || missingImages;
    twentyMinStats.classList.toggle("warning", missingPrompts || missingImages);
    if (missingImages) {
      const missingCount = tmRequiredImageCount - tmImageCount;
      twentyMinStats.textContent =
        `Missing ${missingCount} horizontal image(s) for the 20-minute mode. ` +
        `Add images to 20-Min Images; both Vietnamese and English 20-minute videos use this shared image pool.`;
    } else if (missingPrompts) {
      twentyMinStats.textContent =
        `Missing 20-minute prompts. Add prompt files to ${fullauto.paths?.twenty_min_prompts || "20-Min Prompts"}.`;
    } else {
      twentyMinStats.textContent =
        `${fullauto.twenty_min_target_minutes || 25}-minute mode: ${tmPromptCount} prompt(s), ${tmImageCount}/${tmRequiredImageCount} horizontal image(s). ` +
        `One click triggers topic-to-upload pipeline.`;
    }
  }

  const autoMergeStats = $("fullAutoAutoMergeStats");
  if (autoMergeStats) {
    const am = fullauto.auto_merge;
    if (am && am.enabled) {
      autoMergeStats.style.display = "";
      const s1Ready = am.stage1_candidates_count >= am.stage1_required_count;
      let statusHtml = `<strong><i data-lucide="git-merge" style="width:16px;height:16px;vertical-align:middle;margin-right:4px;"></i>Trạng thái gộp 1 giờ:</strong><br/>`;
      statusHtml += `• Video 20 phút cùng cụm chủ đề sẵn sàng để gộp: <strong>${am.stage1_candidates_count}/${am.stage1_required_count}</strong>`;
      if (am.stage1_cluster) {
        statusHtml += ` - cụm <strong>${escapeHtml(am.stage1_cluster)}</strong>`;
      }
      if (am.stage1_total_available) {
        statusHtml += ` (tổng video 20 phút chưa dùng: ${am.stage1_total_available})`;
      }
      statusHtml += `. `;
      statusHtml += s1Ready
        ? `<span style="color:#2ec4b6; font-weight:bold;">[ĐỦ ĐIỀU KIỆN GỘP]</span>`
        : `<span style="color:#ff9f1c;">[Đang chờ thêm ${am.stage1_required_count - am.stage1_candidates_count} video nữa]</span>`;
      if (am.preferred_cluster && (am.stage1_remaining_count || 0) > 0) {
        statusHtml += `<br/>• Hệ thống đang ưu tiên tạo tiếp cụm <strong>${escapeHtml(am.preferred_cluster)}</strong> cho đến khi đủ 5 video để gộp 1 giờ.`;
      }
      statusHtml += `<br/>• Bản gộp 1 giờ chưa upload: <strong>${am.stage2_candidates_count || 0}</strong>.`;
      if (am.latest_stage1_output) {
        statusHtml += `<br/>• Bản mới nhất: <strong>${escapeHtml(am.latest_stage1_output)}</strong>`;
        if (am.latest_stage1_publish_at) {
          statusHtml += ` - ${escapeHtml(formatSlot(am.latest_stage1_publish_at))}`;
        }
        if (am.latest_stage1_youtube_url) {
          statusHtml += ` - <a href="${escapeHtml(am.latest_stage1_youtube_url)}" target="_blank" rel="noopener">YouTube</a>`;
        }
      }
      const clusters = am.twenty_min_clusters || [];
      if (clusters.length) {
        statusHtml += `<div class="clusterSummary"><strong>Cụm video 20 phút:</strong>`;
        statusHtml += clusters
          .map((cluster) => {
            const latest = (cluster.latest || [])
              .map((item) => `<span>${escapeHtml(item.title || item.video_name || "")}</span>`)
              .join("");
            return `<section class="clusterRow">
              <div><strong>${escapeHtml(cluster.cluster)}</strong><span>${cluster.available}/${cluster.total} chưa dùng, ${cluster.used || 0} đã gộp</span></div>
              <div class="clusterLatest">${latest}</div>
            </section>`;
          })
          .join("");
        statusHtml += `</div>`;
      }
      autoMergeStats.innerHTML = `<div>${statusHtml}</div>`;
      if (window.lucide) {
        window.lucide.createIcons({ attrs: { class: "lucide-icon" }, nameAttr: "data-lucide" });
      }
    } else {
      autoMergeStats.style.display = "none";
    }
  }

  if (merge1HourBtn) {
    const am = fullauto.auto_merge || {};
    merge1HourBtn.disabled = !fullauto.enabled || !fullAutoSupported || !am.enabled || (am.stage1_candidates_count || 0) < (am.stage1_required_count || 5);
  }
  if (mergeUpload1HourBtn) {
    const am = fullauto.auto_merge || {};
    mergeUpload1HourBtn.disabled = !fullauto.enabled || !fullAutoSupported || !am.enabled || (am.stage1_candidates_count || 0) < (am.stage1_required_count || 5);
  }

  renderLongMergeCandidates(fullauto, fullAutoSupported);

  const drafts = fullauto.drafts || [];
  $("fullAutoDrafts").innerHTML = drafts.length
    ? drafts
        .map(
          (draft) => `<article class="fullAutoDraft">
            <div>
              <strong>${escapeHtml(draft.title || draft.id)}</strong>
              <span>${escapeHtml(draft.status || "draft")}${draft.upload_channel ? ` - ${escapeHtml(draft.upload_channel)}` : ""} ${draft.publish_at ? `- ${formatSlot(draft.publish_at)}` : ""}</span>
            </div>
            <div class="actionsRow compactActions">
              ${draft.markdown ? `<button type="button" data-fullauto-md="${escapeHtml(draft.markdown)}"><i data-lucide="file-text"></i>Text</button>` : ""}
              ${draft.audio ? `<button type="button" data-fullauto-audio="${escapeHtml(draft.audio)}"><i data-lucide="music"></i>Sound</button>` : ""}
              ${draft.short_video ? `<button type="button" data-fullauto-video="${escapeHtml(draft.short_video)}"><i data-lucide="circle-play"></i>MP4</button>` : ""}
              ${draft.normal_video ? `<button type="button" data-fullauto-video="${escapeHtml(draft.normal_video)}"><i data-lucide="circle-play"></i>Long MP4</button>` : ""}
              ${draft.youtube_url ? `<label class="buttonLink"><i data-lucide="image-up"></i>Thumbnail<input type="file" accept=".jpg,.jpeg,.png,.webp,image/jpeg,image/png,image/webp" data-fullauto-thumbnail="${escapeHtml(draft.id)}" hidden /></label>` : ""}
              ${draft.youtube_url ? `<a class="buttonLink" href="${escapeHtml(draft.youtube_url)}" target="_blank" rel="noopener">YouTube</a>` : ""}
            </div>
          </article>`
        )
        .join("")
    : '<p class="muted">No Full Auto draft yet.</p>';
}

function renderLongMergeCandidates(fullauto, fullAutoSupported) {
  const list = $("fullAutoLongMergeList");
  const count = $("fullAutoLongMergeCount");
  const mergeBtn = $("fullAutoLongMergeBtn");
  const uploadBtn = $("fullAutoLongMergeUploadBtn");
  if (!list || !count || !mergeBtn || !uploadBtn) return;
  const candidates = fullauto.long_merge_candidates || [];
  const available = new Set(candidates.map((item) => item.name));
  state.selectedLongMergeVideos.forEach((name) => {
    if (!available.has(name)) state.selectedLongMergeVideos.delete(name);
  });
  const selected = state.selectedLongMergeVideos.size;
  count.textContent = `${selected} selected`;
  list.innerHTML = candidates.length
    ? candidates.map((item) => `<label class="sourceItem"><input type="checkbox" data-long-merge-video="${escapeHtml(item.name)}" ${state.selectedLongMergeVideos.has(item.name) ? "checked" : ""} /><span>${escapeHtml(item.label || item.name)}</span><small>${Number(item.size_gb || 0).toFixed(2)} GB · ${escapeHtml(formatSlot(item.created_at))}</small></label>`).join("")
    : '<p class="muted">No eligible local long videos. Long videos must be rendered locally before they can be merged.</p>';
  const enabled = fullauto.enabled && fullAutoSupported && selected >= 2;
  mergeBtn.disabled = !enabled;
  uploadBtn.disabled = !enabled;
}

function renderStoryBeforeSleep(sleepStory) {
  const status = $("sleepStoryStatus");
  if (!$("sleepStoryCreateBtn")) return;
  const promptCount = sleepStory.prompt_count || 0;
  const referenceCount = sleepStory.reference_count || 0;
  const imageCount = sleepStory.image_count || 0;
  const generatedCount = sleepStory.generated_count || 0;
  const imageProvider = sleepStory.image_provider || "sd_webui";
  const localImageUrl = sleepStory.local_image_url || "http://127.0.0.1:7860";
  if ($("sleepPromptCount")) $("sleepPromptCount").textContent = promptCount;
  if ($("sleepReferenceCount")) $("sleepReferenceCount").textContent = referenceCount;
  if ($("sleepGeneratedCount")) $("sleepGeneratedCount").textContent = generatedCount;
  $("sleepStoryPolicy").textContent =
    `Image AI: ${imageProvider} at ${localImageUrl}. Fallback image pool: ${imageCount}.`;

  const drafts = sleepStory.drafts || [];
  $("sleepStoryDrafts").innerHTML = drafts.length
    ? drafts
        .map(
          (draft) => `<article class="fullAutoDraft">
            <div>
              <strong>${escapeHtml(draft.title || draft.id)}</strong>
              <span>${escapeHtml(draft.created_at || "")}</span>
            </div>
            <div class="actionsRow compactActions">
              ${draft.markdown ? `<button type="button" data-sleep-md="${escapeHtml(draft.markdown)}"><i data-lucide="file-text"></i>Text</button>` : ""}
              ${draft.video ? `<button type="button" data-sleep-video="${escapeHtml(draft.video)}"><i data-lucide="circle-play"></i>MP4</button>` : ""}
            </div>
          </article>`
        )
        .join("")
    : '<p class="muted">No Story Before Sleep draft yet.</p>';

  if (drafts.length && !$("sleepStoryReviewPlayer").getAttribute("src")) {
    const latest = drafts[0];
    if (latest.video) {
      $("sleepStoryReviewInfo").textContent = latest.video;
    }
  }
  if (status && !status.textContent) {
    status.textContent = "Ready. Use Auto Agent Video for the full Sleepu Stories workflow.";
  }
}
function renderSchedule(items) {
  $("schedulePreview").innerHTML = items.length
    ? items.map((item, index) => `<div class="scheduleItem"><i data-lucide="clock"></i><div class="scheduleMain"><strong>${formatSlot(item).split(",")[0] || `Slot ${index + 1}`}</strong><span>${formatSlot(item).split(",").slice(1).join(",").trim()}</span></div><span class="status ${index < 2 ? "ready" : "pending"}">${index < 2 ? "Scheduled" : "Available"}</span></div>`).join("")
    : '<p class="muted">No scheduled slots.</p>';
}

function renderQueue(data) {
  const jobs = data.jobs || [];
  const queue = jobs.length ? jobs.slice(0, 3) : [];
  $("queueList").innerHTML = queue.length
    ? queue.map((job) => queueItem(job)).join("")
    : '<p class="muted">No active queue. New render or upload jobs will appear here.</p>';
}

function queueItem(job) {
  const status = job.status || "queued";
  const progress = Number.isFinite(Number(job.progress)) ? Math.max(0, Math.min(100, Number(job.progress))) : null;
  const pct = status === "done" ? "100%" : status === "running" ? `${progress ?? 65}%` : `${progress ?? 0}%`;
  const title = jobTitle(job);
  const detail = job.current_step || job.progress_detail || job.stage || (job.logs || []).at(-1) || status;
  return `<div class="queueItem"><div class="jobMain"><strong>${escapeHtml(title)}</strong><span>${escapeHtml(detail)}</span><progress max="100" value="${parseInt(pct, 10)}"></progress></div><span>${pct}</span></div>`;
}

function trackStatus(track) {
  if (track.normal_uploaded && track.short_uploaded) return ["Uploaded", "done"];
  if (track.normal_uploaded && !track.short_uploaded) return ["Missing Short", "waiting"];
  if (!track.normal_uploaded && track.short_uploaded) return ["Short Uploaded", "ready"];
  if (!track.has_thumbnail) return ["Need Thumbnail", "waiting"];
  if (!track.image) return ["Need Image", "waiting"];
  if (track.video_exists && track.short_exists) return ["Rendered", "ready"];
  if (track.video_exists) return ["Need Short", "waiting"];
  return ["Pending", "pending"];
}

function boolIcon(ok) {
  return ok ? '<span class="check"><i data-lucide="circle-check"></i></span>' : '<span class="xmark"><i data-lucide="circle-x"></i></span>';
}

function renderTracks(tracks, mode = "") {
  if (!tracks.length) {
    $("trackRows").innerHTML = '<tr><td colspan="7" class="muted">No tracks for this account.</td></tr>';
    return;
  }

  $("trackRows").innerHTML = tracks.map((track) => {
    const [label, className] = trackStatus(track);
    return `<tr>
      <td>${escapeHtml(track.title)}</td>
      <td>${boolIcon(Boolean(track.audio))}</td>
      <td><div class="assetStack"><span>Img ${boolIcon(Boolean(track.image))}</span><span>Thumb ${boolIcon(track.has_thumbnail)}</span></div></td>
      <td>${boolIcon(track.video_exists)}</td>
      <td>${boolIcon(track.short_exists)}</td>
      <td><span class="status ${className}">${escapeHtml(label)}</span></td>
      <td>
        <div class="rowActions">
          <button type="button" title="Edit metadata" data-save-metadata data-audio="${escapeHtml(track.audio)}"><i data-lucide="pencil"></i></button>
          <button type="button" title="Re-render" data-track-action="rerender" data-audio="${escapeHtml(track.audio)}"><i data-lucide="refresh-cw"></i></button>
          ${mode === "football"
            ? (track.short_exists && !track.short_uploaded
              ? `<button type="button" class="textAction primaryAction" title="Upload this Short to YouTube" data-track-action="upload-short" data-audio="${escapeHtml(track.audio)}"><i data-lucide="upload"></i>Upload Short</button>`
              : "")
            : `<button type="button" title="Upload both" data-track-action="upload" data-audio="${escapeHtml(track.audio)}" ${track.normal_uploaded && track.short_uploaded ? "disabled" : ""}><i data-lucide="upload"></i></button>`}
          ${track.short_exists && !track.short_uploaded ? `<button type="button" title="Mark short uploaded manually" data-track-action="mark-short-uploaded" data-audio="${escapeHtml(track.audio)}"><i data-lucide="badge-check"></i></button>` : ""}
          ${track.video_url ? `<button type="button" title="Preview" data-preview-url="${escapeHtml(track.video_url)}" data-preview-name="${escapeHtml(track.video)}"><i data-lucide="external-link"></i></button>` : ""}
          ${track.video_exists ? `<button type="button" title="Open MP4 file" data-open-video="${escapeHtml(track.video)}"><i data-lucide="folder-open"></i></button>` : ""}
          ${track.short_exists ? `<button type="button" title="Open Short file" data-open-video="${escapeHtml(track.short_video)}"><i data-lucide="file-video"></i></button>` : ""}
          ${!track.normal_uploaded && !track.short_uploaded ? `<button type="button" class="danger" title="Delete" data-track-action="delete" data-audio="${escapeHtml(track.audio)}"><i data-lucide="trash-2"></i></button>` : ""}
        </div>
        ${renderYoutubeLinks(track.youtube_urls)}
      </td>
    </tr>`;
  }).join("");
}

function renderManualUploads(data) {
  const tracks = data.tracks || [];
  const outputDir = data.paths?.output_dir || "data/output";
  const thumbDir = data.paths?.thumbnail_dir || "data/input/thumbnails";
  const preferredTypes = new Set(data.upload_policy?.upload_types || []);
  const entries = [];

  for (const track of tracks) {
    if (track.short_exists && !track.short_uploaded) {
      entries.push({ track, type: "short", preferred: preferredTypes.has("short") });
    }
    if (track.video_exists && !track.normal_uploaded) {
      entries.push({ track, type: "normal", preferred: preferredTypes.has("normal") });
    }
  }

  entries.sort((a, b) => Number(b.preferred) - Number(a.preferred));
  $("manualUploadSummary").textContent = entries.length
    ? `${entries.length} rendered video(s) ready for manual upload.`
    : "No rendered videos waiting for manual upload.";
  $("manualUploadList").innerHTML = entries.length
    ? entries.map(({ track, type, preferred }) => manualUploadItem(track, type, outputDir, thumbDir, preferred)).join("")
    : '<p class="muted">Nothing to copy right now. Render a track first, then its title and description will appear here.</p>';
}

function manualUploadItem(track, type, outputDir, thumbDir, preferred) {
  const isShort = type === "short";
  const title = isShort ? track.short_title || track.title : track.title;
  const description = isShort ? track.short_description || track.description : track.description;
  const tags = (isShort ? track.short_tags || track.tags : track.tags) || [];
  const categoryId = isShort ? track.short_category_id || track.category_id : track.category_id;
  const videoName = isShort ? track.short_video : track.video;
  const videoPath = `${outputDir}\\${videoName}`;
  const thumbPath = track.thumbnail ? `${thumbDir}\\${track.thumbnail}` : "";
  return `<article class="manualUploadItem">
    <div class="manualUploadTop">
      <div>
        <strong>${escapeHtml(videoName)}</strong>
        <span class="muted">${escapeHtml(isShort ? "Shorts" : "Long video")}${preferred ? " - scheduled type" : ""}</span>
      </div>
      <div class="rowActions">
        ${track[isShort ? "short_video_url" : "video_url"] ? `<button type="button" title="Preview" data-preview-url="${escapeHtml(track[isShort ? "short_video_url" : "video_url"])}" data-preview-name="${escapeHtml(videoName)}"><i data-lucide="circle-play"></i></button>` : ""}
        <button type="button" title="Open MP4 file" data-open-video="${escapeHtml(videoName)}"><i data-lucide="folder-open"></i></button>
        <button type="button" title="Copy all" data-copy-manual="${escapeHtml(track.audio)}" data-copy-type="${type}" data-copy-field="all"><i data-lucide="copy"></i></button>
      </div>
    </div>
    <div class="manualUploadGrid">
      <label><span>Video File</span><input readonly value="${escapeHtml(videoPath)}" /></label>
      <label><span>Thumbnail</span><input readonly value="${escapeHtml(thumbPath || "No thumbnail")}" /></label>
      <label><span>Category</span><input readonly value="${escapeHtml(categoryId || "")}" /></label>
      <label><span>Tags</span><input readonly value="${escapeHtml(tags.join(", "))}" /></label>
    </div>
    <label class="manualCopyField">
      <span>Title <button type="button" data-copy-manual="${escapeHtml(track.audio)}" data-copy-type="${type}" data-copy-field="title">Copy</button></span>
      <input readonly value="${escapeHtml(title)}" />
    </label>
    <label class="manualCopyField">
      <span>Description <button type="button" data-copy-manual="${escapeHtml(track.audio)}" data-copy-type="${type}" data-copy-field="description">Copy</button></span>
      <textarea readonly>${escapeHtml(description)}</textarea>
    </label>
  </article>`;
}

async function copyManualField(audio, type, field) {
  const track = (state.latestData?.tracks || []).find((item) => item.audio === audio);
  if (!track) return;
  const isShort = type === "short";
  const title = isShort ? track.short_title || track.title : track.title;
  const description = isShort ? track.short_description || track.description : track.description;
  const tags = ((isShort ? track.short_tags || track.tags : track.tags) || []).join(", ");
  const categoryId = isShort ? track.short_category_id || track.category_id : track.category_id;
  const outputDir = state.latestData?.paths?.output_dir || "data/output";
  const thumbDir = state.latestData?.paths?.thumbnail_dir || "data/input/thumbnails";
  const videoName = isShort ? track.short_video : track.video;
  const videoPath = `${outputDir}\\${videoName}`;
  const thumbPath = track.thumbnail ? `${thumbDir}\\${track.thumbnail}` : "";
  const values = {
    title,
    description,
    tags,
    category: categoryId || "",
    all: [
      `Video: ${videoPath}`,
      thumbPath ? `Thumbnail: ${thumbPath}` : "Thumbnail: No thumbnail",
      `Title: ${title}`,
      "",
      "Description:",
      description,
      "",
      `Tags: ${tags}`,
      `Category: ${categoryId || ""}`,
    ].join("\n"),
  };
  await copyText(values[field] || "");
  showToast(field === "all" ? "Manual upload info copied." : `${field[0].toUpperCase() + field.slice(1)} copied.`);
}

async function copyText(value) {
  if (navigator.clipboard?.writeText) {
    await navigator.clipboard.writeText(value);
    return;
  }
  const textarea = document.createElement("textarea");
  textarea.value = value;
  textarea.style.position = "fixed";
  textarea.style.opacity = "0";
  document.body.appendChild(textarea);
  textarea.select();
  document.execCommand("copy");
  textarea.remove();
}

function renderUploads(data) {
  const files = [
    ...(data.files?.audio || []).map((name) => ({ name, icon: "music", meta: "Audio file" })),
    ...(data.files?.images || []).slice(0, 5).map((name) => ({ name, icon: "image", meta: "Background image" })),
    ...(data.files?.thumbnails || []).slice(0, 5).map((name) => ({ name, icon: "image-plus", meta: "Thumbnail" })),
  ].slice(0, 10);
  $("recentUploads").innerHTML = files.length
    ? files.map((file) => `<div class="recentItem"><span class="metricIcon blue"><i data-lucide="${file.icon}"></i></span><div class="recentMain"><strong>${escapeHtml(file.name)}</strong><span>${escapeHtml(file.meta)}</span></div><span class="muted">Recent</span></div>`).join("")
    : '<p class="muted">No recent files.</p>';
}

function renderCollection(collection) {
  const outputs = collection.outputs || [];
  const available = new Set(outputs.map((video) => video.name));
  for (const selected of Array.from(state.selectedCollections)) {
    if (!available.has(selected)) state.selectedCollections.delete(selected);
  }

  $("collectionBtn").disabled = !collection.ready;
  $("collectionText").innerHTML = collection.ready
    ? `<h3><span class="check">${icon("circle-check")}</span> Fresh Videos Available</h3><p class="muted">${collection.rendered_count} long videos ready. Create one collection from all of them.</p><strong class="bigNumber">${collection.rendered_count}</strong>`
    : `<h3><span class="xmark">${icon("circle-alert")}</span> Waiting For Videos</h3><p class="muted">Need ${collection.needed_count || 0} more long video(s) before creating a collection.</p><strong class="bigNumber">${collection.rendered_count || 0}</strong>`;

  $("collectionSources").innerHTML = (collection.videos || []).length
    ? collection.videos.map((name) => `<div class="sourceItem"><i data-lucide="circle-play"></i>${escapeHtml(name)}</div>`).join("")
    : '<p class="muted">No source videos ready.</p>';

  $("collectionCount").textContent = `${outputs.length} collections`;
  $("collectionOutputs").innerHTML = outputs.length
    ? outputs.map((video) => collectionOutput(video)).join("")
    : '<p class="muted">No collections created yet.</p>';
  updateMegaCollectionButton();
}

function icon(name) {
  return `<i data-lucide="${name}"></i>`;
}

function collectionOutput(video) {
  const status = video.uploaded ? '<span class="status done">Uploaded</span>' : '<span class="status pending">Local</span>';
  const metadata = video.metadata || {};
  const localSize = Number(video.local_mb || 0);
  return `<div class="collectionItem">
    <label class="collectionMain">
      <span><input type="checkbox" data-collection-select="${escapeHtml(video.name)}" ${state.selectedCollections.has(video.name) ? "checked" : ""} /> <strong>${escapeHtml(video.name)}</strong> ${status}</span>
      <small>${video.is_mega ? "Big collection" : "Collection"} ${localSize ? `${localSize.toFixed(2)} MB` : ""} ${video.publish_at ? `Published ${escapeHtml(formatSlot(video.publish_at))}` : ""}</small>
      <div class="collectionMeta">
        <label><span>Title</span><input readonly value="${escapeHtml(metadata.title || "")}" /></label>
        <label><span>Description</span><textarea readonly>${escapeHtml(metadata.description || "")}</textarea></label>
      </div>
    </label>
    <div class="rowActions">
      <button type="button" title="Preview" data-preview-url="${escapeHtml(video.url)}" data-preview-name="${escapeHtml(video.name)}"><i data-lucide="circle-play"></i></button>
      <button type="button" title="Open MP4 file" data-open-video="${escapeHtml(video.name)}"><i data-lucide="folder-open"></i></button>
      <button type="button" title="Upload" data-collection-action="upload" data-filename="${escapeHtml(video.name)}" ${video.uploaded ? "disabled" : ""}><i data-lucide="upload"></i></button>
      ${!video.uploaded ? `<button type="button" title="Mark uploaded manually" data-collection-action="mark-uploaded" data-filename="${escapeHtml(video.name)}"><i data-lucide="badge-check"></i></button>` : ""}
      ${video.uploaded ? `<button type="button" class="danger" title="Delete local file" data-collection-action="delete-local" data-filename="${escapeHtml(video.name)}"><i data-lucide="hard-drive-x"></i></button>` : `<button type="button" class="danger" title="Delete" data-collection-action="delete" data-filename="${escapeHtml(video.name)}"><i data-lucide="trash-2"></i></button>`}
    </div>
    ${renderYoutubeLinks(video.youtube_urls)}
  </div>`;
}

function renderStorageCleanup(storage) {
  const groups = storage.groups || [];
  $("storageCleanupList").innerHTML = groups.length
    ? `${storage.busy ? '<div class="policyBox slim"><i data-lucide="loader-circle"></i>Rendering or merging is running.</div>' : ""}${groups.map((group) => storageCleanupItem(group, Boolean(storage.busy))).join("")}`
    : '<p class="muted">No cleanup candidates.</p>';
  const safeMb = Number(storage.safe_total_mb || 0);
  $("cleanupAllSafeBtn").disabled = safeMb <= 0 || Boolean(storage.busy);
}

function storageCleanupItem(group, busy = false) {
  const key = group.key || "";
  const canClean = ["source-videos", "merged-collections", "logs"].includes(key) && Number(group.count || 0) > 0 && !busy;
  const uploaded = key === "uploaded-collections";
  return `<div class="storageCleanupItem">
    <span class="metricIcon green"><i data-lucide="${uploaded ? "cloud-check" : "hard-drive"}"></i></span>
    <div class="storageCleanupMain">
      <strong>${escapeHtml(group.label || key)}</strong>
      <span>${Number(group.count || 0)} file(s) - ${Number(group.mb || 0).toFixed(2)} MB</span>
    </div>
    ${canClean ? `<button type="button" data-cleanup-kind="${escapeHtml(key)}"><i data-lucide="trash-2"></i>Clean</button>` : ""}
  </div>`;
}

function updateMegaCollectionButton() {
  const count = state.selectedCollections.size;
  const outputs = state.latestData?.collection?.outputs || [];
  const selectedOutputs = outputs.filter((video) => state.selectedCollections.has(video.name));
  const selectedMega = selectedOutputs.filter((video) => video.is_mega);
  $("megaCollectionBtn").disabled = count < 2 || count > 4;
  $("megaCollectionBtn").innerHTML = `<i data-lucide="folder-plus"></i>Create Longer Collection (${count} selected)`;
  $("deleteMegaCollectionBtn").disabled = !selectedMega.length || selectedMega.length !== selectedOutputs.length;
  $("deleteMegaCollectionBtn").innerHTML = `<i data-lucide="trash-2"></i>Delete Big Collection (${selectedMega.length} selected)`;
  refreshIcons();
}

function renderJobs(jobs) {
  jobs = jobs.filter(isRecentDashboardJob);
  renderJobsOverview(jobs);
  const list = jobs.length ? jobs.map((job) => jobItem(job)).join("") : '<p class="muted">No jobs yet.</p>';
  $("activeJobsList").innerHTML = list;
  $("jobsPageList").innerHTML = list;
  const job = jobs.find((item) => item.id === state.currentJob) || jobs[0];
  $("jobStatus").textContent = job ? `${jobTitle(job)} - ${job.status}${job.account_label ? ` - ${job.account_label}` : ""}` : "";
  $("jobLog").textContent = job ? (job.logs || []).join("\n") : "";
  if (job && (job.status === "running" || job.status === "queued")) {
    $("jobsPanel").classList.remove("collapsed");
  }
}

function renderJobsOverview(jobs) {
  const counts = {
    running: jobs.filter((job) => job.status === "running").length,
    queued: jobs.filter((job) => job.status === "queued").length,
    interrupted: jobs.filter((job) => job.status === "interrupted").length,
  };
  const overviewHtml = [
      ["loader-circle", counts.running, "Running", "running"],
      ["list-ordered", counts.queued, "Queued", "queued"],
      ["circle-pause", counts.interrupted, "Can resume", "interrupted"],
    ].map(([iconName, count, label, kind]) => `<div class="jobOverviewMetric ${kind}">${icon(iconName)}<strong>${count}</strong><span>${label}</span></div>`).join("");
  if ($("jobsOverview")) $("jobsOverview").innerHTML = overviewHtml;
  if ($("jobsPageOverview")) $("jobsPageOverview").innerHTML = overviewHtml;
  if ($("jobsUpdatedAt")) {
    $("jobsUpdatedAt").textContent = `Updated ${new Date().toLocaleTimeString([], { hour: "2-digit", minute: "2-digit", second: "2-digit" })}`;
  }
}

function isRecentDashboardJob(job) {
  if (!job?.finished_at) return true;
  const finishedAt = new Date(job.finished_at).getTime();
  if (!Number.isFinite(finishedAt)) return true;
  const ageMs = Math.max(0, Date.now() - finishedAt);
  if (job.status === "failed") return ageMs <= 15 * 60 * 1000;
  if (job.status === "done") return ageMs <= 2 * 60 * 60 * 1000;
  return true;
}

function jobItem(job) {
  const className = job.status === "done" ? "done" : job.status === "running" ? "running" : job.status === "failed" ? "waiting" : "pending";
  const staleQueueDetail = ["running", "interrupted"].includes(job.status) && /waiting in queue/i.test(job.current_step || job.progress_detail || "");
  const detail = staleQueueDetail
    ? (job.stage || (job.logs || []).at(-1) || "Running")
    : (job.current_step || job.progress_detail || job.stage || (job.logs || []).at(-1) || job.id);
  const progress = Number.isFinite(Number(job.progress)) ? Math.max(0, Math.min(100, Number(job.progress))) : 0;
  const progressHtml = job.status === "running" || job.status === "queued" || progress > 0
    ? `<div class="jobProgressHead"><span>${escapeHtml(job.stage || "Processing")}</span><strong>${progress}%</strong></div><progress value="${progress}" max="100"></progress>`
    : "";
  const recentLogs = (job.recent_logs || job.logs || []).slice(-3);
  const logsHtml = recentLogs.length ? `<details class="jobMiniLogs"><summary>Latest activity</summary>${recentLogs.map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</details>` : "";
  const accountHtml = job.account_label ? `<em>${escapeHtml(job.account_label)}</em>` : "";
  const resumeHtml = job.status === "interrupted" && ["fullauto-long-start", "story-before-sleep-auto"].includes(job.action)
    ? `<button type="button" class="iconTextButton" onclick="resumeInterruptedJob('${escapeHtml(job.id)}')">${icon("rotate-cw")}Resume</button>`
    : "";
  const timeline = jobPipeline(job).map((step, index) => {
    const activeIndex = jobPipelineIndex(job);
    const stateName = index < activeIndex ? "complete" : index === activeIndex ? "active" : "pending";
    return `<span class="jobStep ${stateName}">${index < activeIndex ? icon("check") : ""}${escapeHtml(step)}</span>`;
  }).join("");
  const elapsed = formatJobElapsed(job);
  return `<article class="jobItem jobItemDetailed"><span class="metricIcon ${className === "done" ? "green" : "blue"}">${icon(job.status === "done" ? "circle-check" : job.status === "running" ? "loader-circle" : job.status === "failed" ? "circle-alert" : "clock")}</span><div class="jobMain"><div class="jobTitleRow"><div><strong>${escapeHtml(jobTitle(job))}</strong>${accountHtml}</div><div class="jobMeta"><span>${icon("timer")}${escapeHtml(elapsed)}</span><span class="status ${className}">${escapeHtml(job.status)}</span></div></div><span class="jobCurrentStep">${escapeHtml(detail)}</span>${progressHtml}<div class="jobTimeline">${timeline}</div>${logsHtml}</div>${resumeHtml}</article>`;
}

function jobPipeline(job) {
  const action = job?.action || "";
  if (action === "story-before-sleep-auto") return ["Topic", "Plan", "Write", "Review", "Scenes", "Media", "QA", "Render", "Upload"];
  if (action === "fullauto-long-start") return ["Outline", "Chapters", "Review", "Rewrite", "Voice", "Render", "Upload"];
  if (action === "fullauto-20min-start") return ["Script", "Voice", "Render", "Upload"];
  if (action === "fullauto-start") return ["Script", "Voice", "Images", "Render", "Upload"];
  if (action.includes("merge")) return ["Select", "Merge", "Metadata", "Upload"];
  return ["Queued", "Processing", "Complete"];
}

function jobPipelineIndex(job) {
  const steps = jobPipeline(job);
  if (job.status === "done") return steps.length;
  if (job.status === "queued") return 0;
  const latestLog = job.status === "running" ? ((job.logs || []).at(-1) || "") : "";
  const text = `${job.stage || ""} ${job.current_step || ""} ${latestLog}`.toLowerCase();
  const keywordSets = job.action === "story-before-sleep-auto"
    ? [["topic"], ["planner"], ["writer"], ["review"], ["scene", "prompt_optimizer"], ["voice", "image", "parallel_media"], ["qa"], ["render"], ["upload"]]
    : job.action === "fullauto-long-start"
      ? [["outline"], ["chapter", "writing script", "viet chuong"], ["qa", "review", "trung chuong", "duplicate"], ["rewrite", "viet lai"], ["voice", "tts"], ["render"], ["upload"]]
      : steps.map((step) => [step.toLowerCase()]);
  let found = Math.max(0, Math.min(steps.length - 1, Math.floor(Number(job.progress || 0) * steps.length / 100)));
  keywordSets.forEach((keywords, index) => { if (keywords.some((keyword) => text.includes(keyword))) found = index; });
  return found;
}

function formatJobElapsed(job) {
  const started = new Date(job.started_at || job.created_at || "").getTime();
  const ended = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return "--";
  const total = Math.max(0, Math.floor((ended - started) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  const seconds = total % 60;
  return hours ? `${hours}h ${minutes}m` : minutes ? `${minutes}m ${seconds}s` : `${seconds}s`;
}

async function resumeInterruptedJob(jobId) {
  try {
    const result = await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
    state.currentJob = result.job_id;
    showToast("Job resumed from its saved checkpoint.");
    await refreshStatus();
  } catch (error) {
    showToast(`Resume failed: ${error.message}`, "error");
  }
}

function jobTitle(job) {
  return job?.label || actionLabel(job?.action || "");
}

function actionLabel(action = "") {
  if (action === "fullauto-start") return "Shorts Job";
  if (action === "fullauto-long-start") return "Long Video Job";
  if (action === "fullauto-20min-start") return "20-Min Video Job";
  if (action === "fullauto-long-resume") return "Long Resume Upload Job";
  if (action === "fullauto-merge-1hour") return "Full Auto Merge 1 Hour";
  if (action === "fullauto-merge-upload-1hour") return "Full Auto Merge + Upload 1 Hour";
  if (action === "fullauto-merge-long-selected") return "Merge Selected Long Videos";
  if (action === "fullauto-merge-upload-long-selected") return "Merge + Upload Selected Long Videos";
  if (action === "story-before-sleep-test") return "Story Before Sleep Test";
  if (action === "story-before-sleep-auto") return "Sleepu Stories Auto Agent";
  return action.split("-").map((word) => word ? word[0].toUpperCase() + word.slice(1) : "").join(" ");
}

function renderVoices(voices) {
  const storyFallback = state.latestData?.active_account === "account4" ? "en-US-BrianNeural" : "vi-VN-HoaiMyNeural";
  renderVoiceSelect($("ttsVoice"), voices, storyFallback, state.lastStoryVoiceDefault);
  state.lastStoryVoiceDefault = storyFallback;
  renderVoiceSelect($("footballVoice"), voices, "en-US-BrianNeural");
  renderVoiceSelect($("conversationVoice1"), voices, "en-US-JennyNeural");
  renderVoiceSelect($("conversationVoice2"), voices, "en-US-GuyNeural");
  renderVoiceSelect($("conversationVoice3"), voices, "en-US-AvaNeural");
  renderVoiceSelect($("sleepStoryVoice"), voices, "kokoro-en:bm_lewis");
}

function renderVoiceSelect(select, voices, fallback, previousDefault = "") {
  if (!select) return;
  const current = !select.value || select.value === previousDefault ? fallback : select.value;
  select.innerHTML = voices.map((voice) => `<option value="${escapeHtml(voice.id)}" ${voice.id === current ? "selected" : ""}>${escapeHtml(voice.label)}</option>`).join("");
}

function renderStoryAudio(audioFiles) {
  const select = $("storyAudioSelect");
  const current = select.value;
  select.innerHTML = audioFiles.length
    ? audioFiles.map((audio) => `<option value="${escapeHtml(audio)}" ${audio === current ? "selected" : ""}>${escapeHtml(audio)}</option>`).join("")
    : '<option value="">No story MP3 yet</option>';
  if (current && audioFiles.includes(current)) select.value = current;
  updateVoicePreview();
}

function renderLyricsAudio(audioFiles) {
  const select = $("lyricsAudioSelect");
  const current = select.value;
  select.innerHTML = audioFiles.length
    ? audioFiles.map((audio) => `<option value="${escapeHtml(audio)}" ${audio === current ? "selected" : ""}>${escapeHtml(audio)}</option>`).join("")
    : '<option value="">No audio file found</option>';
  if (current && audioFiles.includes(current)) select.value = current;
  updateLyricsAudioSource();
}

function renderConversationAudio(audioFiles) {
  const list = $("conversationAudioList");
  $("conversationAudioCount").textContent = `${audioFiles.length} file${audioFiles.length === 1 ? "" : "s"}`;
  if (!state.conversationAudio && audioFiles.length) {
    setConversationReviewAudio(audioFiles[0]);
  } else if (state.conversationAudio && !audioFiles.includes(state.conversationAudio)) {
    setConversationReviewAudio("");
  }
  list.innerHTML = audioFiles.length
    ? audioFiles.map((audio) => conversationAudioItem(audio)).join("")
    : '<p class="muted">No conversation MP3 yet.</p>';
  refreshIcons();
}

function conversationAudioItem(audio) {
  const selected = audio === state.conversationAudio;
  return `<div class="conversationAudioItem ${selected ? "selected" : ""}">
    <div class="recentMain">
      <strong>${escapeHtml(audio)}</strong>
      <span>${selected ? "Selected for review" : "Conversation MP3"}</span>
    </div>
    <div class="rowActions">
      <button type="button" title="Review" data-conversation-audio="${escapeHtml(audio)}"><i data-lucide="circle-play"></i></button>
      <button type="button" title="Open MP3 file" data-open-podcast-audio="${escapeHtml(audio)}"><i data-lucide="folder-open"></i></button>
    </div>
  </div>`;
}

function updateLyricsAudioSource() {
  const audioName = $("lyricsAudioSelect").value;
  const player = $("lyricsAudioPlayer");
  if (!audioName) {
    player.removeAttribute("src");
    player.load();
    $("lyricsPreviewInfo").textContent = "No audio file selected.";
    return;
  }
  const url = `/api/lyrics/audio/${encodeURIComponent(audioName)}`;
  if (player.getAttribute("src") !== url) {
    player.setAttribute("src", url);
    player.load();
  }
  $("lyricsPreviewInfo").textContent = audioName;
}

async function uploadLyricsAudio(input) {
  if (!input.files.length) return;
  const form = new FormData();
  for (const file of input.files) form.append("files", file);
  const data = await requestJson("/api/lyrics/upload-files", { method: "POST", body: form });
  input.value = "";
  await refresh();
  if (data.saved?.length) {
    $("lyricsAudioSelect").value = data.saved[0];
    updateLyricsAudioSource();
  }
  showToast("Lyrics audio uploaded.");
}

function buildLyricsReview() {
  updateLyricsAudioSource();
  const player = $("lyricsAudioPlayer");
  const lines = $("lyricsText").value
    .split(/\r?\n/)
    .map((line) => line.trim())
    .filter(Boolean);
  if (!lines.length) {
    showToast("Paste lyrics first.", "error");
    return;
  }
  if (!player.src) {
    showToast("Choose an audio file first.", "error");
    return;
  }
  if (!Number.isFinite(player.duration) || player.duration <= 0) {
    player.addEventListener("loadedmetadata", buildLyricsReview, { once: true });
    player.load();
    return;
  }

  const offset = Number.parseFloat($("lyricsOffset").value || "0");
  const endPadding = Math.max(0, Number.parseFloat($("lyricsEndPadding").value || "0"));
  const start = Math.max(0, offset);
  const usableDuration = Math.max(lines.length, player.duration - start - endPadding);
  const perLine = usableDuration / lines.length;

  state.lyricsRows = lines.map((text, index) => ({
    text,
    start: start + index * perLine,
    end: index === lines.length - 1 ? player.duration : start + (index + 1) * perLine,
  }));
  renderLyricsTimeline();
  updateLyricsDisplay();
  $("lyricsPreviewInfo").textContent = `${lines.length} lyric lines - review only`;
  showToast("Lyrics review ready.");
}

function renderLyricsTimeline() {
  $("lyricsTimeline").innerHTML = state.lyricsRows.length
    ? state.lyricsRows.map((row, index) => `<div class="lyricRow" data-lyric-index="${index}"><time>${formatSeconds(row.start)}</time><span>${escapeHtml(row.text)}</span></div>`).join("")
    : "";
}

function updateLyricsDisplay() {
  const player = $("lyricsAudioPlayer");
  const current = player.currentTime || 0;
  const index = state.lyricsRows.findIndex((row) => current >= row.start && current < row.end);
  const row = index >= 0 ? state.lyricsRows[index] : null;
  const next = index >= 0 ? state.lyricsRows[index + 1] : state.lyricsRows[0];
  $("lyricsDisplay").innerHTML = row
    ? `<div><span>${escapeHtml(row.text)}</span>${next ? `<div class="nextLine">${escapeHtml(next.text)}</div>` : ""}</div>`
    : `<span>${state.lyricsRows.length ? "Waiting for lyrics..." : "No lyrics loaded"}</span>`;
  document.querySelectorAll(".lyricRow").forEach((item) => {
    item.classList.toggle("active", Number(item.dataset.lyricIndex) === index);
  });
  const active = document.querySelector(".lyricRow.active");
  active?.scrollIntoView({ block: "nearest" });
}

function formatSeconds(value) {
  const total = Math.max(0, Math.floor(value));
  const minutes = Math.floor(total / 60);
  const seconds = String(total % 60).padStart(2, "0");
  return `${minutes}:${seconds}`;
}

function renderedVideoOptions(data) {
  const videos = [];
  for (const track of data.tracks || []) {
    if (track.video_url) videos.push({ label: `${track.audio} - Long`, value: track.video });
    if (track.short_video_url) videos.push({ label: `${track.audio} - Short`, value: track.short_video });
  }
  for (const video of data.rendered_videos || []) {
    videos.push({ label: video.name, value: video.name });
  }
  for (const video of data.collection?.outputs || []) {
    videos.push({ label: `${video.name} - Collection`, value: video.name });
  }
  return videos.filter((video, index, list) => list.findIndex((item) => item.value === video.value) === index);
}

function renderTikTokDemo(data) {
  const select = $("tiktokVideoSelect");
  const current = select.value;
  const videos = renderedVideoOptions(data);
  select.innerHTML = videos.length
    ? videos.map((video) => `<option value="${escapeHtml(video.value)}" ${video.value === current ? "selected" : ""}>${escapeHtml(video.label)}</option>`).join("")
    : '<option value="">No rendered video yet</option>';
  if (!$("tiktokCaption").value && videos.length) {
    $("tiktokCaption").value = `${videos[0].value.replace(/\.mp4$/i, "").replaceAll("-", " ")}\n\n#tiktok #video #autovid`;
  }
}

function renderYoutubeLinks(urls) {
  const links = Object.entries(urls || {});
  if (!links.length) return "";
  return `<div class="youtubeLinks">${links.map(([type, url]) => `<a href="${escapeHtml(url)}" target="_blank" rel="noreferrer">${escapeHtml(type)}</a>`).join("")}</div>`;
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

async function runFullAuto() {
  const targetAccount = state.latestData?.active_account;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "start", target_account: targetAccount }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runFullAutoLong() {
  const proceed = window.confirm("Create and upload one long video of about 60 minutes? This job can take a long time.");
  if (!proceed) return null;
  const targetAccount = state.latestData?.active_account;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "start-long", target_account: targetAccount }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runFullAutoTwentyMin() {
  const proceed = window.confirm("Create and upload one 20-30 minute video? This job can take some time.");
  if (!proceed) return null;
  const targetAccount = state.latestData?.active_account;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "start-20min", target_account: targetAccount }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

function boundedNumberInput(id, fallback, min, max) {
  const value = Number.parseInt($(id)?.value ?? "", 10);
  if (!Number.isFinite(value)) return fallback;
  return Math.max(min, Math.min(max, value));
}

async function runFullAutoBulk() {
  const shortCount = boundedNumberInput("fullAutoBulkShortCount", 0, 0, 10);
  const twentyMinCount = boundedNumberInput("fullAutoBulkTwentyMinCount", 0, 0, 5);
  const longCount = boundedNumberInput("fullAutoBulkLongCount", 0, 0, 3);
  if (shortCount + twentyMinCount + longCount < 1) {
    showToast("Choose at least one video to create.", "error");
    return null;
  }
  const proceed = window.confirm(
    `Run Full Auto for 3 Buddhist channels?\n\n` +
    `Each channel: ${shortCount} Short(s), ${twentyMinCount} 20-min, ${longCount} long video(s).`
  );
  if (!proceed) return null;
  const data = await requestJson("/api/fullauto-bulk-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      short_count: shortCount,
      twenty_min_count: twentyMinCount,
      long_count: longCount,
    }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runYouTubeResearch() {
  const channelUrl = $("youtubeResearchUrl")?.value.trim();
  if (!channelUrl) {
    showToast("Paste a YouTube channel link first.", "error");
    return null;
  }
  const status = $("youtubeResearchStatus");
  if (status) status.textContent = "Crawling channel metadata...";
  const data = await requestJson("/api/youtube-research", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      channel_url: channelUrl,
      tab: $("youtubeResearchTab")?.value || "shorts",
      limit: boundedNumberInput("youtubeResearchLimit", 24, 1, 80),
      transcript_limit: boundedNumberInput("youtubeResearchTranscriptLimit", 8, 0, 30),
    }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  const ok = job.status === "done";
  const lastLog = (job.logs || []).slice(-1)[0] || "";
  if (status) status.textContent = ok ? lastLog : jobSummary(job);
  showToast(ok ? "Channel research finished. Open Research Folder to view report." : jobSummary(job), ok ? "success" : "error");
  return job;
}

async function runViewOptimizer() {
  const status = $("youtubeResearchStatus");
  if (status) status.textContent = "Building view optimizer report from research and local drafts...";
  const data = await requestJson("/api/view-optimizer", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ limit: boundedNumberInput("youtubeResearchLimit", 80, 10, 200) }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  const ok = job.status === "done";
  const lastLog = (job.logs || []).slice(-1)[0] || "";
  if (status) status.textContent = ok ? lastLog : jobSummary(job);
  showToast(ok ? "View optimizer report finished. Open Research Folder to view it." : jobSummary(job), ok ? "success" : "error");
  return job;
}

async function runYouTubeAnalyticsSync() {
  const status = $("youtubeResearchStatus");
  if (status) status.textContent = "Syncing YouTube Analytics for uploaded drafts...";
  const data = await requestJson("/api/youtube-analytics-sync", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ days: 90, limit: 120 }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  const ok = job.status === "done";
  const lastLog = (job.logs || []).slice(-1)[0] || "";
  if (status) status.textContent = ok ? lastLog : jobSummary(job);
  showToast(ok ? "YouTube Analytics sync finished. Open Research Folder to view it." : jobSummary(job), ok ? "success" : "error");
  return job;
}

async function runStoryBeforeSleepTest(mode = "test") {
  const title = $("sleepStoryTitle")?.value.trim() || "A Gentle Story Before Sleep";
  const prompt = $("sleepStoryPrompt")?.value.trim() || "";
  const targetMinutes = boundedNumberInput("sleepStoryMinutes", 10, 1, 30);
  const imageCount = boundedNumberInput("sleepStoryImageCount", 8, 1, 32);
  const waitForImages = Boolean($("sleepStoryWaitImages")?.checked);
  const voice = $("sleepStoryVoice")?.value || "en-US-BrianNeural";
  const status = $("sleepStoryStatus");
  const button = $("sleepStoryCreateBtn");
  const autoButton = $("sleepStoryAutoBtn");
  if (button) button.disabled = true;
  if (autoButton) autoButton.disabled = true;
  if (status) {
    status.textContent = mode === "auto"
      ? "Running full Sleepu Stories workflow: story, images, voice, metadata, QA, render..."
      : "Creating a quick local test render with script, voice, scenes, and MP4...";
  }
  try {
    const data = await requestJson("/api/story-before-sleep-action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: mode,
        title,
        prompt,
        target_minutes: targetMinutes,
        voice,
        image_count: imageCount,
        wait_for_images: waitForImages,
      }),
    });
    state.currentJob = data.job_id;
    await refresh();
    const job = await pollJob(data.job_id);
    await refresh();
    if (job.status === "done" && job.output_video) {
      const player = $("sleepStoryReviewPlayer");
      player.setAttribute("src", `/api/video/${encodeURIComponent(job.output_video)}`);
      player.load();
      $("sleepStoryReviewInfo").textContent = job.output_video;
      if (job.output_markdown) {
        const response = await fetch(`/api/story-before-sleep/markdown/${encodeURIComponent(job.output_markdown)}`);
        $("sleepStoryMarkdownReview").value = await response.text();
      }
      if (status) status.textContent = mode === "auto" ? "Sleepu Stories Auto Agent video created." : "Quick test video created.";
    } else if (status) {
      status.textContent = "Sleepu Stories render failed.";
    }
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return job;
  } finally {
    if (button) button.disabled = false;
    if (autoButton) autoButton.disabled = false;
  }
}

async function uploadSleepStoryReferences(input) {
  if (!input.files?.length) return;
  const form = new FormData();
  Array.from(input.files).forEach((file) => form.append("files", file));
  $("sleepStoryStatus").textContent = "Uploading sleep-story reference art...";
  const response = await fetch("/api/story-before-sleep/reference", {
    method: "POST",
    body: form,
  });
  const data = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(data.detail || "Upload failed");
  }
  input.value = "";
  $("sleepStoryStatus").textContent = `Uploaded ${data.saved?.length || 0} reference image(s).`;
  await refresh();
  showToast("Sleep-story reference art uploaded.", "success");
}

async function runFullAutoMerge1Hour() {
  const proceed = window.confirm("Gộp 5 video 20 phút thành 1 video 1 giờ?");
  if (!proceed) return null;
  const targetAccount = state.latestData?.active_account;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "merge-1hour", target_account: targetAccount }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runFullAutoMergeUpload1Hour() {
  const proceed = window.confirm("Gộp 5 video 20 phút và upload luôn video 1 giờ?");
  if (!proceed) return null;
  const targetAccount = state.latestData?.active_account;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ action: "merge-upload-1hour", target_account: targetAccount }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runSelectedLongMerge(upload = false) {
  const filenames = Array.from(state.selectedLongMergeVideos);
  if (filenames.length < 2) throw new Error("Choose at least 2 long videos to merge.");
  const actionLabel = upload ? "Gộp và upload" : "Gộp";
  const cleanupNotice = upload
    ? "Video nguồn chỉ bị xóa sau khi gộp thành công. File gộp chỉ bị xóa sau khi YouTube upload thành công."
    : "Video nguồn chỉ bị xóa sau khi gộp thành công.";
  if (!window.confirm(`${actionLabel} ${filenames.length} video long đã chọn?\n\n${cleanupNotice}`)) return null;
  const data = await requestJson("/api/fullauto-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      action: upload ? "merge-upload-long-selected" : "merge-long-selected",
      target_account: state.latestData?.active_account,
      filenames,
    }),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  if (job.status === "done") state.selectedLongMergeVideos.clear();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function saveFullAutoProvider() {
  const [provider, model] = $("fullAutoModelSelect").value.split("|", 2);
  await requestJson("/api/fullauto-provider", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({ provider, model }),
  });
  await refresh();
  showToast(`Full Auto model: ${provider}/${model}`, "success");
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

async function runCollectionAction(action, filename) {
  const payload = action === "merge" ? { action, filenames: Array.from(state.selectedCollections) } : { action, filename };
  if (action === "merge" && (payload.filenames.length < 2 || payload.filenames.length > 4)) {
    showToast("Choose 2 to 4 collection files first.", "error");
    return null;
  }
  const data = await requestJson("/api/collection-action", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function runStorageCleanup(kind) {
  const data = await requestJson(`/api/storage-cleanup/${encodeURIComponent(kind)}`, { method: "POST" });
  state.currentJob = data.job_id;
  await refresh();
  const job = await pollJob(data.job_id);
  await refresh();
  showToast(jobSummary(job), job.status === "done" ? "success" : "error");
  return job;
}

async function deleteSelectedMegaCollections() {
  const outputs = state.latestData?.collection?.outputs || [];
  const selectedMega = outputs.filter((video) => state.selectedCollections.has(video.name) && video.is_mega);
  if (!selectedMega.length) {
    showToast("Select at least one big collection first.", "error");
    return null;
  }
  for (const video of selectedMega) {
    await runCollectionAction(video.uploaded ? "delete-local" : "delete", video.name);
    state.selectedCollections.delete(video.name);
  }
  await refresh();
  return selectedMega.length;
}

async function pollJob(jobId) {
  for (;;) {
    const job = await requestJson(`/api/jobs/${jobId}`);
    $("jobStatus").textContent = `${jobTitle(job)} - ${job.status}${job.account_label ? ` - ${job.account_label}` : ""}`;
    $("jobLog").textContent = (job.logs || []).join("\n");
    if (job.status !== "running" && job.status !== "queued") return job;
    await new Promise((resolve) => setTimeout(resolve, 1200));
  }
}

function jobSummary(job) {
  if (!job) return "Job finished.";
  const lastLog = (job.logs || []).at(-1) || "";
  if (job.status === "done") return lastLog || `${actionLabel(job.action)} finished.`;
  return lastLog || `${actionLabel(job.action)} failed.`;
}

async function uploadFiles(kind, input) {
  if (!input.files.length) return;
  const form = new FormData();
  form.append("kind", kind);
  for (const file of input.files) form.append("files", file);
  const data = await requestJson("/api/upload-files", { method: "POST", body: form });
  input.value = "";
  await refresh();
  if (data.job_id) {
    state.currentJob = data.job_id;
    const job = await pollJob(data.job_id);
    await refresh();
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return;
  }
  showToast("Files uploaded.");
}

async function openFolder(folder) {
  await requestJson(`/api/open-folder/${folder}`, { method: "POST" });
}

async function openVideoFile(filename) {
  if (!filename) {
    showToast("Choose a rendered video first.", "error");
    return;
  }
  await requestJson(`/api/open-video/${encodeURIComponent(filename)}`, { method: "POST" });
  showToast("Opened MP4 file.");
}

async function openAudioFile(filename) {
  if (!filename) {
    showToast("Choose an MP3 first.", "error");
    return;
  }
  await requestJson(`/api/open-audio/${encodeURIComponent(filename)}`, { method: "POST" });
  showToast("Opened MP3 file.");
}

async function openPodcastAudioFile(filename) {
  if (!filename) {
    showToast("Choose a podcast MP3 first.", "error");
    return;
  }
  await requestJson(`/api/podcast/open-audio/${encodeURIComponent(filename)}`, { method: "POST" });
  showToast("Opened podcast MP3 file.");
}

async function setAccount(accountId) {
  await requestJson(`/api/account/${accountId}`, { method: "POST" });
  if (state.latestData?.fullauto?.upload_accounts?.[accountId]) {
    await requestJson("/api/fullauto-channel", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ target_account: accountId }),
    });
  }
  await refresh();
}

async function saveConfig() {
  const payload = JSON.parse($("configEditor").value);
  await requestJson("/api/config", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(payload),
  });
  await refresh();
  showToast("Config saved.");
}

async function saveMetadataOverride(audio) {
  const track = (state.latestData?.tracks || []).find((item) => item.audio === audio);
  if (!track) return;
  await requestJson("/api/metadata-override", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      audio,
      title: track.title,
      description: track.description,
      tags: track.tags,
      category_id: track.category_id,
    }),
  });
  showToast("Metadata saved from current generated values.");
}

async function generateVoice() {
  const title = $("ttsTitle").value.trim();
  const text = $("ttsText").value.trim();
  const voice = $("ttsVoice").value;
  if (!title || !text) {
    $("ttsStatus").textContent = "Enter title and story text first.";
    return null;
  }
  $("ttsBtn").disabled = true;
  $("ttsStatus").textContent = "Creating voice MP3...";
  try {
    const data = await requestJson("/api/tts", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, text, voice }),
    });
    state.currentJob = data.job_id;
    const job = await pollJob(data.job_id);
    await refresh();
    $("ttsStatus").textContent = job.status === "done" ? "Voice MP3 created." : "Voice generation failed.";
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return job;
  } finally {
    $("ttsBtn").disabled = false;
  }
}

async function generateFootballShort() {
  const title = $("footballTitle").value.trim();
  const text = $("footballText").value.trim();
  const articleUrl = $("footballArticleUrl").value.trim();
  const language = $("footballLanguage").value;
  const voice = $("footballVoice").value;
  if ((!title || !text) && !articleUrl) {
    $("footballStatus").textContent = "Enter title/script or paste a football article URL first.";
    return null;
  }
  if (state.latestData?.active_account !== "football") {
    $("footballStatus").textContent = "Switching to the Football account...";
    await setAccount("football");
  }

  const button = $("footballCreateBtn");
  button.disabled = true;
  $("footballStatus").textContent = articleUrl
    ? "Reading article, asking Gemma for a script, then rendering the Short..."
    : "Creating MP3, finding player images, and rendering the Short...";
  try {
    const data = await requestJson("/api/football-short", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, text, voice, article_url: articleUrl, language }),
    });
    state.currentJob = data.job_id;
    const job = await pollJob(data.job_id);
    await refresh();
    if (job.generated_title) $("footballTitle").value = job.generated_title;
    if (job.generated_text) $("footballText").value = job.generated_text;
    if (job.status === "done" && job.output_video) {
      const player = $("footballReviewPlayer");
      const url = `/api/video/${encodeURIComponent(job.output_video)}`;
      player.setAttribute("src", url);
      player.load();
      $("footballReviewInfo").textContent = job.output_video;
      $("footballStatus").textContent = "Football Short created.";
      player.play().catch(() => {});
    } else {
      $("footballStatus").textContent = "Football Short generation failed.";
    }
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return job;
  } finally {
    button.disabled = false;
  }
}

async function generateFootballVoiceOnly() {
  const title = $("footballTitle").value.trim();
  const text = $("footballText").value.trim();
  const articleUrl = $("footballArticleUrl").value.trim();
  const language = $("footballLanguage").value;
  const voice = $("footballVoice").value;
  if ((!title || !text) && !articleUrl) {
    $("footballStatus").textContent = "Enter title/script or paste a football article URL first.";
    return null;
  }
  if (state.latestData?.active_account !== "football") {
    $("footballStatus").textContent = "Switching to the Football account...";
    await setAccount("football");
  }

  const button = $("footballVoiceOnlyBtn");
  button.disabled = true;
  $("footballStatus").textContent = articleUrl ? "Reading article and creating football voice MP3..." : "Creating football voice MP3 only...";
  try {
    const data = await requestJson("/api/football-voice", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title, text, voice, article_url: articleUrl, language }),
    });
    state.currentJob = data.job_id;
    const job = await pollJob(data.job_id);
    await refresh();
    if (job.generated_title) $("footballTitle").value = job.generated_title;
    if (job.generated_text) $("footballText").value = job.generated_text;
    $("footballStatus").textContent = job.status === "done" ? `Football voice created: ${job.output_audio || "MP3"}` : "Football voice generation failed.";
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return job;
  } finally {
    button.disabled = false;
  }
}

async function generateConversationVoice() {
  const title = $("conversationTitle").value.trim();
  const script = $("conversationScript").value.trim();
  const speaker1Label = $("conversationLabel1").value.trim() || "A";
  const speaker2Label = $("conversationLabel2").value.trim() || "B";
  const speaker3Label = $("conversationLabel3").value.trim();
  const speaker1Voice = $("conversationVoice1").value;
  const speaker2Voice = $("conversationVoice2").value;
  const speaker3Voice = $("conversationVoice3").value;
  if (!title || !script) {
    $("conversationStatus").textContent = "Enter title and conversation script first.";
    return null;
  }
  $("conversationBtn").disabled = true;
  $("conversationStatus").textContent = "Creating conversation MP3...";
  try {
    const data = await requestJson("/api/tts-conversation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        title,
        script,
        speaker1_label: speaker1Label,
        speaker2_label: speaker2Label,
        speaker3_label: speaker3Label,
        speaker1_voice: speaker1Voice,
        speaker2_voice: speaker2Voice,
        speaker3_voice: speaker3Label ? speaker3Voice : "",
      }),
    });
    state.currentJob = data.job_id;
    const job = await pollJob(data.job_id);
    await refresh();
    $("conversationStatus").textContent = job.status === "done" ? "Conversation MP3 created." : "Conversation generation failed.";
    if (job.status === "done" && job.output_audio) {
      setConversationReviewAudio(job.output_audio);
    }
    showToast(jobSummary(job), job.status === "done" ? "success" : "error");
    return job;
  } finally {
    $("conversationBtn").disabled = false;
  }
}

function setConversationReviewAudio(audioName) {
  state.conversationAudio = audioName || "";
  $("conversationReviewBtn").disabled = !state.conversationAudio;
  $("conversationReview").hidden = !state.conversationAudio;
  $("conversationReviewInfo").textContent = state.conversationAudio || "No conversation MP3 selected";
  if (!state.conversationAudio) return;
  const player = $("conversationAudioPlayer");
  const url = `/api/podcast/audio/${encodeURIComponent(state.conversationAudio)}`;
  if (player.getAttribute("src") !== url) {
    player.setAttribute("src", url);
    player.load();
  }
  renderConversationAudio(state.latestData?.conversation_files?.audio || []);
}

function reviewConversationAudio() {
  if (!state.conversationAudio) {
    showToast("Generate a conversation MP3 first.", "error");
    return;
  }
  setConversationReviewAudio(state.conversationAudio);
  $("conversationAudioPlayer").play().catch(() => {});
}

async function attachStoryAssets() {
  const audioName = $("storyAudioSelect").value;
  const imagesInput = $("storyImagesInput");
  const thumbInput = $("storyThumbInput");
  if (!audioName || !imagesInput.files.length) {
    $("ttsStatus").textContent = "Choose an MP3 and 1 to 5 images first.";
    return false;
  }
  const form = new FormData();
  form.append("audio_name", audioName);
  for (const file of imagesInput.files) form.append("images", file);
  if (thumbInput.files.length) form.append("thumbnail", thumbInput.files[0]);
  $("storyAssetsBtn").disabled = true;
  try {
    const data = await requestJson("/api/story-assets", { method: "POST", body: form });
    imagesInput.value = "";
    thumbInput.value = "";
    await refresh();
    if (data.job_id) {
      state.currentJob = data.job_id;
      const job = await pollJob(data.job_id);
      await refresh();
      showToast(jobSummary(job), job.status === "done" ? "success" : "error");
      return job.status === "done";
    }
    showToast("Assets attached.");
    return true;
  } finally {
    $("storyAssetsBtn").disabled = false;
  }
}

async function storyOneClick() {
  const voiceJob = await generateVoice();
  if (!voiceJob || voiceJob.status !== "done") return;
  await attachStoryAssets();
}

function updateVoicePreview() {
  const audioName = $("storyAudioSelect").value;
  const player = $("voicePreview");
  if (!audioName) {
    player.removeAttribute("src");
    player.load();
    $("voicePreviewInfo").textContent = "No MP3 selected";
    return;
  }
  const url = `/api/audio/${encodeURIComponent(audioName)}`;
  if (player.getAttribute("src") !== url) {
    player.setAttribute("src", url);
    player.load();
  }
  $("voicePreviewInfo").textContent = audioName;
}

function connectTikTokDemo() {
  const clientKey = $("tiktokClientKey").value.trim();
  const redirectUri = $("tiktokRedirectUri").value.trim();
  if (!clientKey || !redirectUri) {
    showToast("Enter TikTok client key and redirect URI first.", "error");
    return;
  }
  const params = new URLSearchParams({
    client_key: clientKey,
    response_type: "code",
    scope: "user.info.basic,video.upload,video.publish",
    redirect_uri: redirectUri,
    state: `autovid_${Date.now()}`,
  });
  window.open(`https://www.tiktok.com/v2/auth/authorize/?${params.toString()}`, "_blank", "noopener,noreferrer");
  $("tiktokDemoLog").textContent = "Opened TikTok Login Kit authorization page.\nPaste the callback code here for the token exchange step.";
}

function runTikTokUploadDemo() {
  const video = $("tiktokVideoSelect").value;
  const caption = $("tiktokCaption").value.trim();
  const privacy = $("tiktokPrivacy").value;
  if (!video || !caption) {
    showToast("Choose a rendered video and caption first.", "error");
    return;
  }
  $("tiktokDemoLog").textContent = [
    "TikTok Content Posting API review demo",
    `Video file: data/output/${video}`,
    `Caption: ${caption.split("\n")[0]}`,
    `Privacy: ${privacy}`,
    "Transfer method: FILE_UPLOAD",
    "Ready for review recording. Direct upload requires approved scopes.",
  ].join("\n");
}

function isUserEditing() {
  const element = document.activeElement;
  return Boolean(element && element !== document.body && (element.matches("input, textarea, select") || element.isContentEditable));
}

function startAutoRefresh() {
  window.setInterval(() => {
    if (document.hidden || isUserEditing()) return;
    refresh({ auto: true }).catch((error) => {
      $("connection").textContent = error.message;
    });
  }, 4000);
}

document.querySelectorAll("[data-page]").forEach((item) => {
  item.addEventListener("click", (event) => {
    event.preventDefault();
    setPage(item.dataset.page);
  });
});

$("refreshBtn").addEventListener("click", () => refresh().catch((error) => showToast(error.message, "error")));
$("accountSelect").addEventListener("change", (event) => setAccount(event.target.value).catch((error) => showToast(error.message, "error")));

$("collectionBtn").addEventListener("click", () => runAction("create-collection").catch((error) => showToast(error.message, "error")));
$("megaCollectionBtn").addEventListener("click", () => runCollectionAction("merge").catch((error) => showToast(error.message, "error")));
$("deleteMegaCollectionBtn").addEventListener("click", () => {
  const count = (state.latestData?.collection?.outputs || []).filter((video) => state.selectedCollections.has(video.name) && video.is_mega).length;
  if (!count) return;
  if (!window.confirm(`Delete ${count} selected big collection local file(s)?`)) return;
  deleteSelectedMegaCollections().catch((error) => showToast(error.message, "error"));
});
$("cleanupAllSafeBtn").addEventListener("click", () => runStorageCleanup("all-safe").catch((error) => showToast(error.message, "error")));
$("renderBtn").addEventListener("click", () => runAction("render").catch((error) => showToast(error.message, "error")));
$("dryBtn").addEventListener("click", () => runAction("daily-dry-run").catch((error) => showToast(error.message, "error")));
$("uploadBtn").addEventListener("click", () => runAction("daily-upload").catch((error) => showToast(error.message, "error")));
$("syncBtn").addEventListener("click", () => runAction("sync-state").catch((error) => showToast(error.message, "error")));
$("saveConfigBtn").addEventListener("click", () => saveConfig().catch((error) => showToast(error.message, "error")));
$("ttsBtn").addEventListener("click", () => generateVoice().catch((error) => showToast(error.message, "error")));
$("footballCreateBtn").addEventListener("click", () => generateFootballShort().catch((error) => {
  $("footballStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("footballVoiceOnlyBtn").addEventListener("click", () => generateFootballVoiceOnly().catch((error) => {
  $("footballStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("conversationBtn").addEventListener("click", () => generateConversationVoice().catch((error) => showToast(error.message, "error")));
$("conversationReviewBtn").addEventListener("click", reviewConversationAudio);
$("storyOneClickBtn").addEventListener("click", () => storyOneClick().catch((error) => showToast(error.message, "error")));
$("storyAssetsBtn").addEventListener("click", () => attachStoryAssets().catch((error) => showToast(error.message, "error")));
$("fullAutoStartBtn").addEventListener("click", () => runFullAuto().catch((error) => showToast(error.message, "error")));
$("fullAutoLongStartBtn").addEventListener("click", () => runFullAutoLong().catch((error) => showToast(error.message, "error")));
$("fullAutoTwentyMinStartBtn").addEventListener("click", () => runFullAutoTwentyMin().catch((error) => showToast(error.message, "error")));
$("fullAutoBulkBtn").addEventListener("click", () => runFullAutoBulk().catch((error) => showToast(error.message, "error")));
$("youtubeResearchBtn").addEventListener("click", () => runYouTubeResearch().catch((error) => {
  $("youtubeResearchStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("youtubeAnalyticsSyncBtn").addEventListener("click", () => runYouTubeAnalyticsSync().catch((error) => {
  $("youtubeResearchStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("viewOptimizerBtn").addEventListener("click", () => runViewOptimizer().catch((error) => {
  $("youtubeResearchStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("sleepStoryCreateBtn").addEventListener("click", () => runStoryBeforeSleepTest().catch((error) => {
  $("sleepStoryStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("sleepStoryAutoBtn").addEventListener("click", () => runStoryBeforeSleepTest("auto").catch((error) => {
  $("sleepStoryStatus").textContent = error.message;
  showToast(error.message, "error");
}));
$("fullAutoMerge1HourBtn").addEventListener("click", () => runFullAutoMerge1Hour().catch((error) => showToast(error.message, "error")));
$("fullAutoMergeUpload1HourBtn").addEventListener("click", () => runFullAutoMergeUpload1Hour().catch((error) => showToast(error.message, "error")));
$("fullAutoLongMergeBtn").addEventListener("click", () => runSelectedLongMerge(false).catch((error) => showToast(error.message, "error")));
$("fullAutoLongMergeUploadBtn").addEventListener("click", () => runSelectedLongMerge(true).catch((error) => showToast(error.message, "error")));
$("fullAutoModelSelect").addEventListener("change", () => saveFullAutoProvider().catch((error) => showToast(error.message, "error")));
$("storyAudioSelect").addEventListener("change", updateVoicePreview);
$("lyricsAudioSelect").addEventListener("change", updateLyricsAudioSource);
$("lyricsBuildBtn").addEventListener("click", buildLyricsReview);
$("lyricsAudioPlayer").addEventListener("timeupdate", updateLyricsDisplay);
$("lyricsAudioPlayer").addEventListener("seeked", updateLyricsDisplay);
$("lyricsAudioUpload").addEventListener("change", (event) => uploadLyricsAudio(event.target).catch((error) => showToast(error.message, "error")));
$("tiktokConnectBtn").addEventListener("click", connectTikTokDemo);
$("tiktokDemoBtn").addEventListener("click", runTikTokUploadDemo);
$("openRenderedVideoBtn").addEventListener("click", () => openVideoFile($("tiktokVideoSelect").value).catch((error) => showToast(error.message, "error")));
$("toggleJobsBtn").addEventListener("click", () => {
  $("jobsPanel").classList.toggle("collapsed");
  $("toggleJobsBtn").textContent = $("jobsPanel").classList.contains("collapsed") ? "Expand" : "Collapse";
});

$("audioUpload").addEventListener("change", (event) => uploadFiles("audio", event.target).catch((error) => showToast(error.message, "error")));
$("imageUpload").addEventListener("change", (event) => uploadFiles("image", event.target).catch((error) => showToast(error.message, "error")));
$("shortImageUpload").addEventListener("change", (event) => uploadFiles("short-image", event.target).catch((error) => showToast(error.message, "error")));
$("thumbUpload").addEventListener("change", (event) => uploadFiles("thumbnail", event.target).catch((error) => showToast(error.message, "error")));
const sleepStoryReferenceUpload = $("sleepStoryReferenceUpload");
if (sleepStoryReferenceUpload) {
  sleepStoryReferenceUpload.addEventListener("change", (event) => uploadSleepStoryReferences(event.target).catch((error) => {
    $("sleepStoryStatus").textContent = error.message;
    showToast(error.message, "error");
  }));
}

document.addEventListener("change", async (event) => {
  const longMergeInput = event.target.closest("[data-long-merge-video]");
  if (longMergeInput) {
    const filename = longMergeInput.dataset.longMergeVideo;
    if (longMergeInput.checked) state.selectedLongMergeVideos.add(filename);
    else state.selectedLongMergeVideos.delete(filename);
    renderLongMergeCandidates(state.latestData?.fullauto || {}, Boolean(state.latestData?.capabilities?.fullauto));
    return;
  }

  const input = event.target.closest("[data-fullauto-thumbnail]");
  if (!input || !input.files?.length) return;
  const form = new FormData();
  form.append("draft_id", input.dataset.fullautoThumbnail);
  form.append("thumbnail", input.files[0]);
  try {
    const result = await requestJson("/api/fullauto/thumbnail", { method: "POST", body: form });
    await refresh();
    showToast(`Thumbnail updated for ${result.video_id}`, "success");
  } catch (error) {
    showToast(error.message, "error");
  } finally {
    input.value = "";
  }
});

document.querySelectorAll("[data-folder]").forEach((button) => {
  button.addEventListener("click", () => openFolder(button.dataset.folder).catch((error) => showToast(error.message, "error")));
});

document.addEventListener("click", async (event) => {
  const copyButton = event.target.closest("[data-copy-manual]");
  if (copyButton) {
    copyManualField(copyButton.dataset.copyManual, copyButton.dataset.copyType, copyButton.dataset.copyField).catch((error) => showToast(error.message, "error"));
    return;
  }

  const actionButton = event.target.closest("[data-track-action]");
  if (actionButton) {
    if (actionButton.dataset.trackAction === "delete" && !window.confirm(`Delete local files for "${actionButton.dataset.audio}"?`)) return;
    actionButton.disabled = true;
    try {
      await runTrackAction(actionButton.dataset.trackAction, actionButton.dataset.audio);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      actionButton.disabled = false;
    }
    return;
  }

  const metadataButton = event.target.closest("[data-save-metadata]");
  if (metadataButton) {
    saveMetadataOverride(metadataButton.dataset.audio).catch((error) => showToast(error.message, "error"));
    return;
  }

  const previewButton = event.target.closest("[data-preview-url]");
  if (previewButton) {
    showInlineVideo(previewButton.dataset.previewUrl, previewButton.dataset.previewName || "Selected video");
    return;
  }

  const openVideoButton = event.target.closest("[data-open-video]");
  if (openVideoButton) {
    openVideoFile(openVideoButton.dataset.openVideo).catch((error) => showToast(error.message, "error"));
    return;
  }

  const openAudioButton = event.target.closest("[data-open-audio]");
  if (openAudioButton) {
    openAudioFile(openAudioButton.dataset.openAudio).catch((error) => showToast(error.message, "error"));
    return;
  }

  const openPodcastAudioButton = event.target.closest("[data-open-podcast-audio]");
  if (openPodcastAudioButton) {
    openPodcastAudioFile(openPodcastAudioButton.dataset.openPodcastAudio).catch((error) => showToast(error.message, "error"));
    return;
  }

  const collectionButton = event.target.closest("[data-collection-action]");
  if (collectionButton) {
    if (collectionButton.dataset.collectionAction === "delete" && !window.confirm(`Delete ${collectionButton.dataset.filename}?`)) return;
    if (collectionButton.dataset.collectionAction === "delete-local" && !window.confirm(`Delete only the local file for ${collectionButton.dataset.filename}?`)) return;
    collectionButton.disabled = true;
    try {
      await runCollectionAction(collectionButton.dataset.collectionAction, collectionButton.dataset.filename);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      collectionButton.disabled = false;
    }
    return;
  }

  const cleanupButton = event.target.closest("[data-cleanup-kind]");
  if (cleanupButton) {
    if (!window.confirm("Clean these local files now?")) return;
    cleanupButton.disabled = true;
    try {
      await runStorageCleanup(cleanupButton.dataset.cleanupKind);
    } catch (error) {
      showToast(error.message, "error");
    } finally {
      cleanupButton.disabled = false;
    }
    return;
  }

  const collectionSelect = event.target.closest("[data-collection-select]");
  if (collectionSelect) {
    if (collectionSelect.checked) state.selectedCollections.add(collectionSelect.dataset.collectionSelect);
    else state.selectedCollections.delete(collectionSelect.dataset.collectionSelect);
    updateMegaCollectionButton();
    return;
  }

  const conversationButton = event.target.closest("[data-conversation-audio]");
  if (conversationButton) {
    setConversationReviewAudio(conversationButton.dataset.conversationAudio);
    $("conversationAudioPlayer").play().catch(() => {});
    return;
  }

  const sleepMd = event.target.closest("[data-sleep-md]");
  if (sleepMd) {
    const filename = sleepMd.dataset.sleepMd;
    const response = await fetch(`/api/story-before-sleep/markdown/${encodeURIComponent(filename)}`);
    if (!response.ok) {
      showToast(await response.text(), "error");
      return;
    }
    $("sleepStoryMarkdownReview").value = await response.text();
    $("sleepStoryReviewInfo").textContent = filename;
    return;
  }

  const sleepVideo = event.target.closest("[data-sleep-video]");
  if (sleepVideo) {
    const filename = sleepVideo.dataset.sleepVideo;
    const player = $("sleepStoryReviewPlayer");
    player.setAttribute("src", `/api/video/${encodeURIComponent(filename)}`);
    player.load();
    $("sleepStoryReviewInfo").textContent = filename;
    player.play().catch(() => {});
    return;
  }

  const fullAutoMd = event.target.closest("[data-fullauto-md]");
  if (fullAutoMd) {
    const filename = fullAutoMd.dataset.fullautoMd;
    const response = await fetch(`/api/fullauto/markdown/${encodeURIComponent(filename)}`);
    if (!response.ok) {
      showToast(await response.text(), "error");
      return;
    }
    $("fullAutoMarkdownReview").value = await response.text();
    $("fullAutoReviewInfo").textContent = filename;
    return;
  }

  const fullAutoAudio = event.target.closest("[data-fullauto-audio]");
  if (fullAutoAudio) {
    const filename = fullAutoAudio.dataset.fullautoAudio;
    const player = $("fullAutoAudioReview");
    player.setAttribute("src", `/api/audio/${encodeURIComponent(filename)}`);
    player.load();
    $("fullAutoReviewInfo").textContent = filename;
    player.play().catch(() => {});
    return;
  }

  const fullAutoVideo = event.target.closest("[data-fullauto-video]");
  if (fullAutoVideo) {
    const filename = fullAutoVideo.dataset.fullautoVideo;
    const player = $("fullAutoVideoReview");
    player.setAttribute("src", `/api/video/${encodeURIComponent(filename)}`);
    player.load();
    $("fullAutoReviewInfo").textContent = filename;
    player.play().catch(() => {});
  }
});

window.addEventListener("hashchange", () => setPage(location.hash.replace("#", "") || "dashboard"));

setPage(state.activePage);
refresh().catch((error) => {
  $("connection").textContent = error.message;
  showToast(error.message, "error");
});
startAutoRefresh();
refreshIcons();

function showInlineVideo(url, label) {
  const isCollection = state.activePage === "collections";
  const player = $(isCollection ? "collectionReviewPlayer" : "videoReviewPlayer");
  const info = $(isCollection ? "collectionReviewInfo" : "videoReviewInfo");
  if (!player || !info) return;
  if (player.getAttribute("src") !== url) {
    player.setAttribute("src", url);
    player.load();
  }
  info.textContent = label;
  player.closest(".videoReviewPanel")?.scrollIntoView({ behavior: "smooth", block: "start" });
  player.play().catch(() => {});
}

