/* Shared clip card + favourite + media player. Exposes window.Clips.
   Card URLs are carried on data-* attributes so this works for any URL scheme
   (authenticated pages here; the public share page has its own inline player). */
(function () {
  function esc(s) {
    return (s || "").replace(/[&<>"]/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" }[c]));
  }

  function metaHtml(c) {
    const bits = [`<span>${esc(c.display_camera)}</span>`];
    if (c.duration_secs) { const s = Math.round(c.duration_secs); bits.push(`<span>${Math.floor(s / 60)}m ${s % 60}s</span>`); }
    if (c.recorded_at)   bits.push(`<span>${esc(String(c.recorded_at).slice(0, 10))}</span>`);
    if (c.size_bytes)    bits.push(`<span>${(c.size_bytes / 1073741824).toFixed(1)} GB</span>`);
    return bits.join("");
  }

  function createCard(c) {
    const d = document.createElement("div");
    d.className = "clip";
    d.dataset.id = c.id;
    d.dataset.mediaType = c.media_type || "video";
    d.dataset.video = c.video_url || "";
    d.dataset.image = c.image_url || "";
    d.dataset.download = c.download_url || "";
    if (c.raw_url) d.dataset.raw = c.raw_url;
    const photo = c.media_type === "photo";
    d.innerHTML = `
      <div class="thumb-wrap">
        ${c.thumb_url ? `<img src="${c.thumb_url}" loading="lazy" alt="">` : `<div class="no-thumb">${photo ? "🖼" : "▶"}</div>`}
        <button class="play-btn" title="${photo ? "View" : "Play"}">${photo ? "⤢" : "▶"}</button>
        <div class="checkbox" title="Select"></div>
        <button class="heart" title="Favourite">♥</button>
      </div>
      <div class="clip-info">
        <div class="clip-name" title="${esc(c.filename)}">${esc(c.filename)}</div>
        <div class="clip-meta">${metaHtml(c)}</div>
      </div>`;
    updateCard(d, c);
    return d;
  }

  function updateCard(node, c) {
    node.querySelector(".heart").classList.toggle("active", !!c.is_favourite);
  }

  let overlay;
  function ensurePlayer() {
    if (overlay) return;
    overlay = document.createElement("div");
    overlay.id = "player";
    overlay.innerHTML = `
      <div id="player-box">
        <div id="player-bar">
          <span id="player-title"></span><span style="flex:1"></span>
          <a id="player-raw" href="#" download style="display:none">Download RAW</a>
          <a id="player-download" href="#" download>Download</a>
          <button id="player-close" type="button">✕</button>
        </div>
        <video id="player-video" controls playsinline></video>
        <img id="player-image" alt="" style="display:none">
        <div id="player-error">This clip can’t be played in the browser — most likely HEVC/H.265.<br>
          <a id="player-error-dl" href="#" download>Download the file</a> to view it.</div>
      </div>`;
    document.body.appendChild(overlay);
    overlay.addEventListener("click", e => { if (e.target.id === "player") closePlayer(); });
    overlay.querySelector("#player-close").addEventListener("click", closePlayer);
    const v = overlay.querySelector("#player-video");
    v.addEventListener("error", () => {
      if (v.style.display === "none") return;   // showing a photo, not a video error
      v.style.display = "none";
      overlay.querySelector("#player-error").style.display = "block";
    });
    document.addEventListener("keydown", e => { if (e.key === "Escape") closePlayer(); });
  }

  function openPlayer(card) {
    ensurePlayer();
    const v = overlay.querySelector("#player-video"), img = overlay.querySelector("#player-image");
    const photo = card.dataset.mediaType === "photo";
    const nameEl = card.querySelector(".clip-name");
    overlay.querySelector("#player-title").textContent = (nameEl && nameEl.textContent) || card.dataset.name || "";
    overlay.querySelector("#player-download").href = card.dataset.download;
    const raw = overlay.querySelector("#player-raw");
    if (card.dataset.raw) { raw.href = card.dataset.raw; raw.style.display = "inline"; } else raw.style.display = "none";
    overlay.querySelector("#player-error").style.display = "none";
    if (photo) {
      v.pause(); v.removeAttribute("src"); v.load(); v.style.display = "none";
      img.src = card.dataset.image; img.style.display = "block";
    } else {
      img.removeAttribute("src"); img.style.display = "none";
      overlay.querySelector("#player-error-dl").href = card.dataset.download;
      v.style.display = "block"; v.src = card.dataset.video; v.play().catch(() => {});
    }
    overlay.classList.add("visible");
  }

  function closePlayer() {
    if (!overlay) return;
    const v = overlay.querySelector("#player-video");
    v.pause(); v.removeAttribute("src"); v.load();
    overlay.querySelector("#player-image").removeAttribute("src");
    overlay.classList.remove("visible");
  }

  async function heartToggle(card) {
    const r = await fetch(`/clips/${card.dataset.id}/favourite`, { method: "POST" });
    if (r.ok) card.querySelector(".heart").classList.toggle("active");
    return r.ok;
  }

  // --- media-type segmented toggle (#media-toggle, shared by all clip views) ---
  function toggleMedia(type) {
    const btn = document.querySelector(`#media-toggle button[data-media="${type}"]`);
    if (btn) btn.classList.toggle("active");
  }
  function mediaAllows(type) {
    const btn = document.querySelector(`#media-toggle button[data-media="${type === "photo" ? "photo" : "video"}"]`);
    return !btn || btn.classList.contains("active");
  }

  window.Clips = { esc, metaHtml, createCard, updateCard, openPlayer, closePlayer, heartToggle, toggleMedia, mediaAllows };
})();
