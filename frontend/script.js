const API_BASE = window.location.origin;
const STORAGE_KEY = "allergy_app_selected";
const CUSTOM_KEY = "allergy_app_custom";

const allergyList = document.getElementById("allergyList");
const customInput = document.getElementById("customInput");
const customChips = document.getElementById("customChips");
const addCustomBtn = document.getElementById("addCustomBtn");
const cameraInput = document.getElementById("cameraInput");
const fileInput = document.getElementById("fileInput");
const textInput = document.getElementById("textInput");
const previewWrap = document.getElementById("previewWrap");
const preview = document.getElementById("preview");
const clearBtn = document.getElementById("clearBtn");
const sizeInfo = document.getElementById("sizeInfo");
const analyzeBtn = document.getElementById("analyzeBtn");
const resultSection = document.getElementById("resultSection");
const resultContent = document.getElementById("resultContent");
const loading = document.getElementById("loading");

let selectedAllergies = new Set(JSON.parse(localStorage.getItem(STORAGE_KEY) || "[]"));
let customAllergies = JSON.parse(localStorage.getItem(CUSTOM_KEY) || "[]");
let allergensList = [];
let currentImageFile = null;
let lastResultData = null;

// ----- language toggle -----
applyTranslations();
document.querySelectorAll(".lang-btn").forEach((btn) => {
  btn.addEventListener("click", () => setLang(btn.dataset.lang));
});
document.addEventListener("langChanged", () => {
  renderAllergyChips(allergensList);
  renderCustomChips();
  if (lastResultData) renderResult(lastResultData);
});

// ----- allergens -----
async function loadAllergens() {
  try {
    const r = await fetch(`${API_BASE}/api/allergens`);
    allergensList = await r.json();
    renderAllergyChips(allergensList);
  } catch (err) {
    allergyList.innerHTML = `<p class="error">${err.message}</p>`;
  }
}

function renderAllergyChips(list) {
  allergyList.innerHTML = "";
  list.forEach((a) => {
    const chip = document.createElement("div");
    const label = allergenLabel(a);
    chip.className = "chip" + (selectedAllergies.has(a.key) ? " active" : "");
    chip.innerHTML = `<span class="icon">${a.icon}</span><span>${escapeHtml(label)}</span>`;
    chip.addEventListener("click", () => {
      if (selectedAllergies.has(a.key)) {
        selectedAllergies.delete(a.key);
        chip.classList.remove("active");
      } else {
        selectedAllergies.add(a.key);
        chip.classList.add("active");
      }
      localStorage.setItem(STORAGE_KEY, JSON.stringify([...selectedAllergies]));
    });
    allergyList.appendChild(chip);
  });
}

function renderCustomChips() {
  customChips.innerHTML = "";
  customAllergies.forEach((term, idx) => {
    const chip = document.createElement("div");
    chip.className = "chip custom-chip active";
    chip.innerHTML = `<span class="icon">⚠️</span><span>${escapeHtml(term)}</span><span class="remove-x">×</span>`;
    chip.querySelector(".remove-x").addEventListener("click", (e) => {
      e.stopPropagation();
      customAllergies.splice(idx, 1);
      localStorage.setItem(CUSTOM_KEY, JSON.stringify(customAllergies));
      renderCustomChips();
    });
    customChips.appendChild(chip);
  });
}

function addCustomAllergy() {
  const v = customInput.value.trim();
  if (!v) return;
  if (!customAllergies.includes(v)) {
    customAllergies.push(v);
    localStorage.setItem(CUSTOM_KEY, JSON.stringify(customAllergies));
    renderCustomChips();
  }
  customInput.value = "";
}

addCustomBtn.addEventListener("click", addCustomAllergy);
customInput.addEventListener("keydown", (e) => {
  if (e.key === "Enter") {
    e.preventDefault();
    addCustomAllergy();
  }
});

// ----- image upload -----
const RESIZE_MAX_LONG_EDGE = 1600;
const RESIZE_QUALITY = 0.8;

function formatSize(bytes) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(0)} KB`;
  return `${(bytes / 1024 / 1024).toFixed(1)} MB`;
}

async function resizeImage(file, maxLongEdge = RESIZE_MAX_LONG_EDGE, quality = RESIZE_QUALITY) {
  // Skip resize for tiny files (already small) or non-image
  if (!file.type.startsWith("image/") || file.size < 200 * 1024) {
    return file;
  }
  let bitmap;
  try {
    bitmap = await createImageBitmap(file);
  } catch (e) {
    console.warn("createImageBitmap failed, sending original:", e);
    return file;
  }
  const { width, height } = bitmap;
  const scale = Math.min(1, maxLongEdge / Math.max(width, height));
  if (scale === 1 && file.size < 1.5 * 1024 * 1024) {
    bitmap.close?.();
    return file;
  }
  const w = Math.round(width * scale);
  const h = Math.round(height * scale);
  const canvas = document.createElement("canvas");
  canvas.width = w;
  canvas.height = h;
  const ctx = canvas.getContext("2d");
  ctx.drawImage(bitmap, 0, 0, w, h);
  bitmap.close?.();
  return new Promise((resolve) => {
    canvas.toBlob(
      (blob) => {
        if (!blob) return resolve(file);
        const renamed = file.name.replace(/\.\w+$/, ".jpg");
        resolve(new File([blob], renamed || "menu.jpg", { type: "image/jpeg" }));
      },
      "image/jpeg",
      quality,
    );
  });
}

async function setImageFile(file) {
  if (!file) return;
  textInput.value = "";
  preview.src = URL.createObjectURL(file);
  previewWrap.classList.remove("hidden");
  sizeInfo.textContent = "⏳";

  const origSize = file.size;
  const resized = await resizeImage(file);
  currentImageFile = resized;

  if (resized !== file && resized.size < origSize) {
    sizeInfo.textContent = t("size_resized", {
      orig: formatSize(origSize),
      new: formatSize(resized.size),
    });
    sizeInfo.classList.add("resized");
    // Update preview to the resized image so the user sees what's being sent
    preview.src = URL.createObjectURL(resized);
  } else {
    sizeInfo.textContent = t("size_original", { size: formatSize(origSize) });
    sizeInfo.classList.remove("resized");
  }
  updateButtonState();
}

function clearImage() {
  currentImageFile = null;
  cameraInput.value = "";
  fileInput.value = "";
  preview.src = "";
  sizeInfo.textContent = "";
  sizeInfo.classList.remove("resized");
  previewWrap.classList.add("hidden");
  updateButtonState();
}

function updateButtonState() {
  const hasInput = currentImageFile !== null || textInput.value.trim().length > 0;
  analyzeBtn.disabled = !hasInput;
}

cameraInput.addEventListener("change", (e) => setImageFile(e.target.files[0]));
fileInput.addEventListener("change", (e) => setImageFile(e.target.files[0]));
clearBtn.addEventListener("click", clearImage);
textInput.addEventListener("input", updateButtonState);

// ----- analyze -----
analyzeBtn.addEventListener("click", async () => {
  resultSection.classList.remove("hidden");
  resultContent.innerHTML = "";
  loading.classList.remove("hidden");

  const fd = new FormData();
  fd.append("allergies", JSON.stringify([...selectedAllergies]));
  fd.append("custom_allergies", JSON.stringify(customAllergies));

  const typed = textInput.value.trim();
  if (typed) {
    fd.append("text", typed);
  } else if (currentImageFile) {
    fd.append("image", currentImageFile);
  } else {
    return;
  }

  try {
    const r = await fetch(`${API_BASE}/api/analyze`, { method: "POST", body: fd });
    if (!r.ok) {
      const err = await r.json().catch(() => ({ detail: "Error" }));
      throw new Error(err.detail || `HTTP ${r.status}`);
    }
    lastResultData = await r.json();
    renderResult(lastResultData);
  } catch (err) {
    resultContent.innerHTML = `<div class="error">${t("error_prefix")}${escapeHtml(err.message)}</div>`;
  } finally {
    loading.classList.add("hidden");
  }
});

// ----- result rendering -----
function renderDishCard(dish) {
  const isUncertain = !dish.has_alert && dish.source !== "local_db";
  const cls = dish.has_alert
    ? "dish-card danger"
    : isUncertain
    ? "dish-card uncertain"
    : "dish-card safe";
  const headerIcon = dish.has_alert ? "⚠️" : isUncertain ? "❓" : "✅";

  const alertList = dish.alerts?.length
    ? `<ul class="alert-list">${dish.alerts
        .map((a) => `<li class="badge">${a.icon} ${escapeHtml(allergenLabel(a))}</li>`)
        .join("")}</ul>`
    : "";

  // Cross-contamination chips (only present on non-alerted dishes)
  const contaminationHtml = dish.contamination_warnings?.length
    ? `<div class="contamination-warnings">${dish.contamination_warnings
        .map((w) => {
          const reason = (w.reason && (w.reason[currentLang] || w.reason.th)) || "";
          const allergens = (w.allergens_info || [])
            .map((a) => `${a.icon} ${escapeHtml(allergenLabel(a))}`)
            .join(", ");
          return `<div class="contamination-chip">
            <strong>⚠️ ${t("contamination_label")}: ${allergens}</strong>
            <span class="contamination-reason">${escapeHtml(reason)}</span>
          </div>`;
        })
        .join("")}</div>`
    : "";

  const ingredients = dish.ingredients?.length
    ? `<ul class="ingredient-list">${dish.ingredients
        .map((i) => `<li>${escapeHtml(i)}</li>`)
        .join("")}</ul>`
    : `<p class="hint inline">${t("no_ingredients")}</p>`;

  const allergens = dish.allergens_info?.length
    ? `<div class="allergen-summary">${dish.allergens_info
        .map((a) => `<span class="allergen-tag">${a.icon} ${escapeHtml(allergenLabel(a))}</span>`)
        .join("")}</div>`
    : `<p class="hint inline">${t("no_allergens")}</p>`;

  let sourceLabel = t("source_ai");
  if (dish.source === "local_db") sourceLabel = t("source_db");
  else if (dish.source === "local_db_fuzzy") {
    const ratio = dish.match_ratio ? ` (${Math.round(dish.match_ratio * 100)}%)` : "";
    sourceLabel = t("source_db_fuzzy") + ratio;
  } else if (dish.source === "typhoon_llm") {
    sourceLabel = dish.web_search_used ? t("source_web") : t("source_ai");
  }

  const confidenceLabel = {
    high: t("conf_high"),
    medium: t("conf_medium"),
    low: t("conf_low"),
  }[dish.confidence] || dish.confidence;

  // Primary dish name by current language; secondary is whichever is different
  let dishName, dishSecondary;
  if (currentLang === "en" && dish.dish_name_en) {
    dishName = dish.dish_name_en;
    dishSecondary = dish.dish_name_th;
  } else {
    // TH and ZH both default to Thai name (DB has no Chinese names yet)
    dishName = dish.dish_name_th || dish.query;
    dishSecondary = dish.dish_name_en;
  }
  // For fuzzy matches, show what we matched it to as the secondary line
  if (dish.source === "local_db_fuzzy" && dish.matched_to) {
    dishSecondary = `≈ ${dish.matched_to}`;
  }

  return `
    <div class="${cls}">
      <div class="dish-header">
        <span class="dish-icon">${headerIcon}</span>
        <div>
          <h3 class="dish-name">${escapeHtml(dishName)}</h3>
          ${dishSecondary ? `<p class="dish-en">${escapeHtml(dishSecondary)}</p>` : ""}
        </div>
      </div>
      ${alertList}
      ${contaminationHtml}
      <details class="dish-details">
        <summary>${t("see_details")}</summary>
        <p class="section-title">${t("ingredients_label")}</p>
        ${ingredients}
        <p class="section-title">${t("allergens_label")}</p>
        ${allergens}
        <div class="meta">
          <span>${sourceLabel}</span>
          <span>${confidenceLabel}</span>
        </div>
      </details>
    </div>
  `;
}

// Build a comma-separated allergen list for the vendor phrase, in the given language
function buildAllergenListLabel(lang) {
  const items = [];
  for (const a of allergensList) {
    if (!selectedAllergies.has(a.key)) continue;
    let label;
    if (lang === "th") label = a.th;
    else if (lang === "zh") label = ALLERGEN_LABELS_ZH[a.key] || a.en || a.th;
    else label = a.en || a.th;
    items.push(label);
  }
  for (const c of customAllergies) items.push(c);
  return items.join(", ");
}

function vendorPhrase(lang, includeCleanup) {
  const list = buildAllergenListLabel(lang) || "—";
  const tpl = (I18N[lang] && I18N[lang].vendor_phrase) || "";
  let msg = tpl.replace("{list}", list);
  if (includeCleanup) {
    const cleanup = (I18N[lang] && I18N[lang].vendor_cleanup) || "";
    if (cleanup) msg += " " + cleanup;
  }
  return msg;
}

function renderVendorPanel(hasUserAllergies, hasContamination) {
  if (!hasUserAllergies) return "";
  const thMsg = vendorPhrase("th", hasContamination);
  const showSecond = currentLang !== "th";
  const userMsg = showSecond ? vendorPhrase(currentLang, hasContamination) : "";
  const flagSecond = currentLang === "en" ? "🇬🇧 EN" : "🇨🇳 中文";

  return `
    <div class="vendor-panel">
      <div class="vendor-header">
        <h3>${t("vendor_title")}</h3>
        <button class="vendor-copy-btn" data-msg="${escapeAttr(thMsg + (showSecond ? "\n\n" + userMsg : ""))}">
          ${t("vendor_copy")}
        </button>
      </div>
      <div class="vendor-msg">
        <span class="vendor-lang-tag">🇹🇭 TH</span>
        <p>${escapeHtml(thMsg)}</p>
      </div>
      ${showSecond ? `
        <div class="vendor-msg">
          <span class="vendor-lang-tag">${flagSecond}</span>
          <p>${escapeHtml(userMsg)}</p>
        </div>
      ` : ""}
    </div>
  `;
}

function escapeAttr(s) {
  return String(s ?? "").replace(/&/g, "&amp;").replace(/"/g, "&quot;").replace(/\n/g, "&#10;");
}

function renderResult(data) {
  const dishes = data.dishes || [];
  // 3-way split:
  //   alerted   = has_alert (any source)
  //   safe      = no alert + dish came from exact DB match (high-confidence safe)
  //   uncertain = no alert + dish came from fuzzy/LLM (we couldn't fully confirm)
  const alerted = dishes.filter((d) => d.has_alert);
  const safe = dishes.filter((d) => !d.has_alert && d.source === "local_db");
  const uncertain = dishes.filter((d) => !d.has_alert && d.source !== "local_db");
  const hasUserAllergies = selectedAllergies.size > 0 || customAllergies.length > 0;
  const hasContamination = dishes.some((d) => d.contamination_warnings?.length);

  let bannerHtml = "";
  let bannerStyle = "info"; // info | danger | safe — controls layout
  if (data.is_menu) {
    if (alerted.length > 0) {
      bannerStyle = "danger";
      bannerHtml = `
        <span class="summary-icon">⚠️</span>
        <div>
          <h2>${t("alert_title_multi_some", { alerted: alerted.length, total: dishes.length })}</h2>
          <p>${t("alert_desc_multi_some")}</p>
        </div>`;
    } else if (hasUserAllergies) {
      bannerStyle = "safe";
      bannerHtml = `
        <span class="summary-icon">✅</span>
        <div>
          <h2>${t("alert_title_multi_safe")}</h2>
          <p>${t("alert_desc_multi_safe", { total: dishes.length })}</p>
        </div>`;
    } else {
      bannerStyle = "info";
      bannerHtml = `
        <span class="summary-icon">ℹ️</span>
        <div>
          <h2>${t("alert_title_multi_info", { total: dishes.length })}</h2>
          <p>${t("alert_desc_multi_info")}</p>
        </div>`;
    }
  } else if (dishes.length === 1) {
    const d = dishes[0];
    if (d.has_alert) {
      bannerStyle = "danger";
      bannerHtml = `
        <span class="summary-icon">⚠️</span>
        <div>
          <h2>${t("alert_title_single_warn")}</h2>
          <p>${t("alert_desc_single_warn", { n: d.alerts.length })}</p>
        </div>`;
    } else if (hasUserAllergies) {
      bannerStyle = "safe";
      bannerHtml = `
        <span class="summary-icon">✅</span>
        <div>
          <h2>${t("alert_title_single_safe")}</h2>
          <p>${t("alert_desc_single_safe")}</p>
        </div>`;
    }
  }

  // Compose: split layout when there's both a banner AND a vendor panel
  const vendorHtml = renderVendorPanel(hasUserAllergies, hasContamination);
  let summary = "";
  if (bannerHtml && vendorHtml) {
    summary = `
      <div class="alert-row">
        <div class="summary-banner ${bannerStyle}">${bannerHtml}</div>
        ${vendorHtml}
      </div>`;
  } else if (bannerHtml) {
    summary = `<div class="summary-banner ${bannerStyle}">${bannerHtml}</div>`;
  } else if (vendorHtml) {
    summary = vendorHtml;
  }

  const alertedHtml = alerted.length
    ? `<p class="section-title danger-title">${t("section_alerted", { n: alerted.length })}</p>
       ${alerted.map(renderDishCard).join("")}`
    : "";
  const safeHtml = safe.length
    ? `<p class="section-title safe-title">${t("section_safe", { n: safe.length })}</p>
       ${safe.map(renderDishCard).join("")}`
    : "";
  const uncertainHtml = uncertain.length
    ? `<p class="section-title uncertain-title">${t("section_uncertain", { n: uncertain.length })}</p>
       <p class="hint inline uncertain-hint">${t("uncertain_hint")}</p>
       ${uncertain.map(renderDishCard).join("")}`
    : "";

  const debugHtml = data.is_menu
    ? `<details class="debug">
         <summary>${t("debug_summary")}</summary>
         <p class="hint inline">${t("debug_db_match", { db: data.db_matched_count || 0, llm: data.extracted_names?.length || 0 })}</p>
         ${data.extracted_names?.length
           ? `<p class="hint inline">${t("debug_llm_names")}</p>
              <ul class="ingredient-list">${data.extracted_names.map((n) => `<li>${escapeHtml(n)}</li>`).join("")}</ul>`
           : `<p class="hint inline">${t("debug_no_llm")}</p>`}
         <p class="hint inline" style="margin-top:0.7rem">${t("debug_ocr")}</p>
         <pre class="ocr-raw">${escapeHtml(data.ocr_text || "")}</pre>
       </details>`
    : "";

  resultContent.innerHTML = `${summary}${alertedHtml}${safeHtml}${uncertainHtml}${debugHtml}`;

  // Hook up copy buttons on vendor panels
  resultContent.querySelectorAll(".vendor-copy-btn").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const msg = btn.dataset.msg || "";
      try {
        await navigator.clipboard.writeText(msg);
        const original = btn.textContent;
        btn.textContent = t("vendor_copied");
        btn.classList.add("copied");
        setTimeout(() => {
          btn.textContent = original;
          btn.classList.remove("copied");
        }, 1500);
      } catch (e) {
        console.warn("Copy failed:", e);
      }
    });
  });
}

function escapeHtml(s) {
  if (s == null) return "";
  return String(s)
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;")
    .replace(/"/g, "&quot;");
}

renderCustomChips();
loadAllergens();
