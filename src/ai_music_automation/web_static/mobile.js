const mobileState = { data: null, refreshTimer: null };
const $ = (id) => document.getElementById(id);
const escapeHtml = (value) => String(value ?? "")
  .replaceAll("&", "&amp;")
  .replaceAll("<", "&lt;")
  .replaceAll(">", "&gt;")
  .replaceAll('"', "&quot;")
  .replaceAll("'", "&#039;");

async function requestJson(url, options = {}) {
  const response = await fetch(url, options);
  if (!response.ok) throw new Error((await response.text()) || response.statusText);
  return response.json();
}

function toast(message, type = "success") {
  const element = $("toast");
  element.textContent = message;
  element.className = `toast ${type === "error" ? "error" : ""}`;
  element.hidden = false;
  clearTimeout(mobileState.toastTimer);
  mobileState.toastTimer = setTimeout(() => { element.hidden = true; }, 5000);
}

function refreshIcons() { if (window.lucide) window.lucide.createIcons(); }

function formatDate(value) {
  const date = new Date(value);
  return Number.isNaN(date.getTime()) ? value || "" : date.toLocaleString("en-US", { dateStyle: "short", timeStyle: "short" });
}

function fullAutoAccounts(data) { return data.fullauto?.upload_accounts || {}; }

function readiness(data) {
  const fullauto = data.fullauto || {};
  const normalReady = (fullauto.prompt_count || 0) >= 1 && (fullauto.image_count || 0) >= 1;
  const twentyRequired = Number(fullauto.twenty_min_required_image_count || 5);
  const twentyReady = (fullauto.twenty_min_prompt_count || 0) >= 1 && (fullauto.twenty_min_image_count || 0) >= twentyRequired;
  const longRequired = Number(fullauto.long_required_image_count || 10);
  const longReady = (fullauto.long_prompt_count || 0) >= 1 && (fullauto.long_image_count || 0) >= longRequired;
  return {
    normalReady,
    twentyReady,
    longReady,
    normalMessage: normalReady ? "Ready" : `Needs 1 prompt and 1 image. Found ${fullauto.prompt_count || 0} prompt(s), ${fullauto.image_count || 0} image(s).`,
    twentyMessage: twentyReady ? "Ready" : `Needs 1 prompt and ${twentyRequired} images. Found ${fullauto.twenty_min_prompt_count || 0} prompt(s), ${fullauto.twenty_min_image_count || 0} image(s).`,
    longMessage: longReady ? "Ready" : `Needs 1 prompt and ${longRequired} images. Found ${fullauto.long_prompt_count || 0} prompt(s), ${fullauto.long_image_count || 0} image(s).`,
  };
}

function render(data) {
  mobileState.data = data;
  const accounts = fullAutoAccounts(data);
  const select = $("accountSelect");
  select.innerHTML = Object.entries(accounts).map(([id, account]) => `<option value="${escapeHtml(id)}">${escapeHtml(account.label || id)}</option>`).join("");
  select.value = data.active_account;
  $("connectionState").textContent = "Connected to server";
  $("connectionState").classList.toggle("warn", !(data.credentials_ready && data.token_ready));
  $("credentialState").textContent = data.credentials_ready && data.token_ready ? "YouTube credentials ready" : "YouTube credentials need attention";
  $("modelState").textContent = `${data.fullauto?.provider || ""}/${data.fullauto?.model || ""}`;
  $("assetSummary").textContent = `20-Min: ${data.fullauto?.twenty_min_prompt_count || 0} prompt(s), ${data.fullauto?.twenty_min_image_count || 0}/${data.fullauto?.twenty_min_required_image_count || 5} image(s). Long: ${data.fullauto?.long_prompt_count || 0} prompt(s), ${data.fullauto?.long_image_count || 0}/${data.fullauto?.long_required_image_count || 10} image(s).`;
  const ready = readiness(data);
  const actionStates = {
    start: [ready.normalReady, ready.normalMessage, "startState"],
    "start-20min": [ready.twentyReady, ready.twentyMessage, "start20minState"],
    "start-long": [ready.longReady, ready.longMessage, "startLongState"],
  };
  document.querySelectorAll("[data-action]").forEach((button) => {
    const [isReady, message, labelId] = actionStates[button.dataset.action] || [true, "", ""];
    button.disabled = !isReady;
    button.title = message;
    if ($(labelId)) $(labelId).textContent = message;
  });
  renderJobs(data.jobs || []);
  renderMobileSleepStory(data.story_before_sleep || {}, data.jobs || []);
  $("schedule").innerHTML = (data.schedule_preview || []).map((slot) => `<span>${escapeHtml(formatDate(slot))}</span>`).join("") || '<span>No schedule yet.</span>';
  refreshIcons();
}

function renderMobileSleepStory(sleepStory, jobs) {
  const promptCount = Number(sleepStory.prompt_count || 0);
  const generatedCount = Number(sleepStory.generated_count || 0);
  const fallbackCount = Number(sleepStory.image_count || 0);
  const provider = sleepStory.image_provider || "local image AI";
  const running = jobs.some((job) => job.action === "story-before-sleep-auto" && ["running", "queued"].includes(job.status));
  $("sleepReadyState").textContent = running ? "Running" : promptCount > 0 ? "Ready" : "Needs prompt";
  $("sleepReadyState").classList.toggle("busy", running);
  $("sleepAssetSummary").textContent = `${promptCount} prompt(s) · ${generatedCount} generated image(s) · ${fallbackCount} fallback image(s) · ${provider}`;
  $("mobileSleepRunBtn").disabled = running;
  if (running) $("mobileSleepRunState").textContent = "A Sleepu Stories job is running. Follow its timeline in Jobs below.";

  const drafts = (sleepStory.drafts || []).slice(0, 3);
  $("mobileSleepDraftCount").textContent = `${sleepStory.drafts?.length || 0} draft(s)`;
  $("mobileSleepDrafts").innerHTML = drafts.length ? drafts.map((draft) => {
    const video = draft.video
      ? `<a class="draftAction" href="/api/video/${encodeURIComponent(draft.video)}" target="_blank" rel="noopener"><i data-lucide="circle-play"></i>Video</a>`
      : "";
    const text = draft.markdown
      ? `<a class="draftAction" href="/api/story-before-sleep/markdown/${encodeURIComponent(draft.markdown)}" target="_blank" rel="noopener"><i data-lucide="file-text"></i>Text</a>`
      : "";
    return `<article class="sleepDraft"><div><strong>${escapeHtml(draft.title || draft.id || "Sleep story")}</strong><span>${escapeHtml(formatDate(draft.created_at || ""))}</span></div><div class="draftActions">${video}${text}</div></article>`;
  }).join("") : '<p class="empty">No Sleepu Stories output yet.</p>';
}

async function runMobileSleepStory() {
  const title = $("mobileSleepTitle").value.trim() || "A Gentle Story Before Sleep";
  const prompt = $("mobileSleepPrompt").value.trim();
  const targetMinutes = Math.max(1, Math.min(30, Number($("mobileSleepMinutes").value || 15)));
  const imageCount = Math.max(1, Math.min(32, Number($("mobileSleepImages").value || 12)));
  if (!window.confirm(`Create a new ${targetMinutes}-minute Sleepu Stories video with ${imageCount} fresh images?`)) return;
  const button = $("mobileSleepRunBtn");
  button.disabled = true;
  $("mobileSleepRunState").textContent = "Adding Sleepu Stories Auto Agent to the queue...";
  try {
    const data = await requestJson("/api/story-before-sleep-action", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        action: "auto",
        title,
        prompt,
        target_minutes: targetMinutes,
        voice: "kokoro-en:bm_lewis",
        image_count: imageCount,
        wait_for_images: true,
      }),
    });
    $("mobileSleepRunState").textContent = `Queued: ${data.job_id}`;
    toast("Sleepu Stories job added to the queue.");
    await refresh();
  } catch (error) {
    $("mobileSleepRunState").textContent = error.message;
    toast(error.message, "error");
    button.disabled = false;
  }
}

function renderJobs(jobs) {
  jobs = jobs.filter(isRecentDashboardJob);
  const running = jobs.filter((job) => job.status === "running").length;
  const queued = jobs.filter((job) => job.status === "queued").length;
  const interrupted = jobs.filter((job) => job.status === "interrupted").length;
  $("jobCount").textContent = `${running + queued} active`;
  $("jobOverview").innerHTML = `<span><strong>${running}</strong> Running</span><span><strong>${queued}</strong> Queued</span><span><strong>${interrupted}</strong> Resume</span>`;
  $("jobs").innerHTML = jobs.length ? jobs.map((job) => {
    const progress = Math.max(0, Math.min(100, Number(job.progress || 0)));
    const title = job.label || job.action || "Job";
    const account = job.account_label ? `<span class="jobAccount">${escapeHtml(job.account_label)}</span>` : "";
    const staleQueueDetail = ["running", "interrupted"].includes(job.status) && /waiting in queue/i.test(job.current_step || job.progress_detail || "");
    const detail = staleQueueDetail
      ? (job.stage || job.logs?.at(-1) || "Running")
      : (job.current_step || job.progress_detail || job.stage || job.logs?.at(-1) || "Waiting");
    const recentLogs = (job.recent_logs || job.logs || []).slice(-3);
    const logsHtml = recentLogs.length
      ? `<details class="jobLogs"><summary>Latest activity</summary>${recentLogs.map((line) => `<span>${escapeHtml(line)}</span>`).join("")}</details>`
      : "";
    const pipeline = mobileJobPipeline(job);
    const activeIndex = mobilePipelineIndex(job, pipeline);
    const steps = pipeline.map((step, index) => `<span class="jobStep ${index < activeIndex ? "complete" : index === activeIndex ? "active" : ""}">${index < activeIndex ? "✓ " : ""}${escapeHtml(step)}</span>`).join("");
    const resume = job.status === "interrupted" && ["fullauto-long-start", "story-before-sleep-auto"].includes(job.action)
      ? `<button class="resumeButton" type="button" onclick="resumeMobileJob('${escapeHtml(job.id)}')"><i data-lucide="rotate-cw"></i>Resume</button>`
      : "";
    return `<article class="job"><div class="jobTop"><div><span class="jobTitle">${escapeHtml(title)}</span>${account}</div><span class="jobStatus ${escapeHtml(job.status || "")}">${escapeHtml(job.status || "queued")}</span></div><div class="jobRuntime"><span>${escapeHtml(mobileElapsed(job))}</span><strong>${progress}%</strong></div><progress value="${progress}" max="100"></progress><span class="jobDetail">${escapeHtml(detail)}</span><div class="jobTimeline">${steps}</div>${resume}${logsHtml}</article>`;
  }).join("") : '<p class="empty">No recent jobs.</p>';
}

function mobileJobPipeline(job) {
  if (job.action === "story-before-sleep-auto") return ["Topic", "Plan", "Write", "Review", "Scenes", "Media", "QA", "Render", "Upload"];
  if (job.action === "fullauto-long-start") return ["Outline", "Chapters", "Voice", "Render", "Upload"];
  if (job.action === "fullauto-20min-start") return ["Script", "Voice", "Render", "Upload"];
  if (job.action === "fullauto-start") return ["Script", "Voice", "Images", "Render", "Upload"];
  if ((job.action || "").includes("merge")) return ["Select", "Merge", "Metadata", "Upload"];
  return ["Queued", "Processing", "Complete"];
}

function mobilePipelineIndex(job, steps) {
  if (job.status === "done") return steps.length;
  if (job.status === "queued") return 0;
  const latestLog = job.status === "running" ? ((job.logs || []).at(-1) || "") : "";
  const text = `${job.stage || ""} ${job.current_step || ""} ${latestLog}`.toLowerCase();
  const groups = job.action === "story-before-sleep-auto"
    ? [["topic"], ["planner"], ["writer"], ["review"], ["scene", "prompt_optimizer"], ["voice", "image", "parallel_media"], ["qa"], ["render"], ["upload"]]
    : job.action === "fullauto-long-start"
      ? [["outline"], ["chapter", "writing script"], ["voice", "tts"], ["render"], ["upload"]]
      : steps.map((step) => [step.toLowerCase()]);
  let found = Math.max(0, Math.min(steps.length - 1, Math.floor(Number(job.progress || 0) * steps.length / 100)));
  groups.forEach((keywords, index) => { if (keywords.some((keyword) => text.includes(keyword))) found = index; });
  return found;
}

function mobileElapsed(job) {
  const started = new Date(job.started_at || job.created_at || "").getTime();
  const ended = job.finished_at ? new Date(job.finished_at).getTime() : Date.now();
  if (!Number.isFinite(started) || !Number.isFinite(ended)) return "Time --";
  const total = Math.max(0, Math.floor((ended - started) / 1000));
  const hours = Math.floor(total / 3600);
  const minutes = Math.floor((total % 3600) / 60);
  return hours ? `${hours}h ${minutes}m elapsed` : `${minutes}m elapsed`;
}

async function resumeMobileJob(jobId) {
  try {
    await requestJson(`/api/jobs/${encodeURIComponent(jobId)}/resume`, { method: "POST" });
    toast("Job resumed from checkpoint.");
    await refresh();
  } catch (error) {
    toast(error.message, "error");
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

async function refresh() {
  try { render(await requestJson("/api/status")); }
  catch (error) { $("connectionState").textContent = "Cannot connect"; $("connectionState").classList.add("warn"); toast(error.message, "error"); }
}

async function selectAccount(accountId) {
  await requestJson(`/api/account/${encodeURIComponent(accountId)}`, { method: "POST" });
  await refresh();
}

async function runAction(action) {
  const accountId = $("accountSelect").value;
  const ready = readiness(mobileState.data || {});
  if (action === "start-long" && !ready.longReady) throw new Error(ready.longMessage);
  if (action === "start-20min" && !ready.twentyReady) throw new Error(ready.twentyMessage);
  if (action === "start" && !ready.normalReady) throw new Error(ready.normalMessage);
  const label = action === "start-long" ? "create a long video" : action === "start-20min" ? "create a 20-minute video" : "create shorts";
  if (!window.confirm(`Confirm ${label} for the selected channel?`)) return;
  const data = await requestJson("/api/fullauto-action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ action, target_account: accountId }) });
  toast(`Queued job: ${data.job_id}`);
  await refresh();
}

async function runBulk() {
  const payload = {
    short_count: Number($("bulkShorts").value || 0),
    twenty_min_count: Number($("bulkTwenty").value || 0),
    long_count: Number($("bulkLong").value || 0),
  };
  if (payload.short_count + payload.twenty_min_count + payload.long_count < 1) throw new Error("Choose at least one video.");
  if (!window.confirm("Run the queue for all 3 Buddhist channels?")) return;
  const data = await requestJson("/api/fullauto-bulk-action", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(payload) });
  toast(`Queued 3-channel job: ${data.job_id}`);
  await refresh();
}

$("refreshBtn").addEventListener("click", () => refresh());
$("accountSelect").addEventListener("change", (event) => selectAccount(event.target.value).catch((error) => toast(error.message, "error")));
document.querySelectorAll("[data-action]").forEach((button) => button.addEventListener("click", () => runAction(button.dataset.action).catch((error) => toast(error.message, "error"))));
$("bulkRunBtn").addEventListener("click", () => runBulk().catch((error) => toast(error.message, "error")));
$("mobileSleepRunBtn").addEventListener("click", () => runMobileSleepStory());

if ("serviceWorker" in navigator) navigator.serviceWorker.register("/static/mobile-sw.js").catch(() => {});
refresh();
setInterval(refresh, 15000);
