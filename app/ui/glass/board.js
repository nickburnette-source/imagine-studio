/* Timeline board. The server owns the clock. This file only draws and drags. */
(function () {
  const API = "/api/glass";
  const MUTE_KEY = "imagineStudio.videoMuted";
  let state = null;
  let held = null;
  let dragging = 0;
  let editing = 0;
  let selected = null;
  let watchId = null;
  let libraryOpen = false;
  let promptOpen = false;
  let durationOpen = false;
  let aspectOpen = false;
  let pollTimer = 0;
  let modal = null;
  let picked = new Set();
  let viewerEl = null;
  let viewerVideo = null;
  let viewerName = null;
  let viewerUrl = "";
  let viewerWasPlaying = false;
  let panelsWereOpen = false;
  let autoPlayedUrl = "";
  let armEdit = null;
  let resizeTimer = 0;
  let editOpen = false;
  let editVideo = null;
  let editTime = 0;
  let editPlaying = false;
  let editSel = "";
  let soundSel = "";
  let editMuted = true;
  let editAudio = null;
  let editWasPlaying = false;
  let editSeeking = false;
  let speakOpen = false;
  let speakDraft = "";
  let speakVoice = "eve";
  let speakBusy = false;
  let speakFocus = false;
  let speakAt = 0;
  let speakMenu = false;
  let swallowBack = false;
  let ignoreClick = false;
  let desk = null;
  let deskOpenedAt = 0;
  const LINE_VOICES = ["eve", "ara", "leo", "rex", "sal", "gork"];

  function h(tag, attrs, kids) {
    const node = document.createElement(tag);
    Object.keys(attrs || {}).forEach((key) => {
      const value = attrs[key];
      if (value == null || value === false) return;
      if (key === "class") node.className = value;
      else if (key === "text") node.textContent = value;
      else if (key.startsWith("on") && typeof value === "function") {
        node.addEventListener(key.slice(2).toLowerCase(), value);
      } else if (key === "hidden") node.hidden = !!value;
      else node.setAttribute(key, value === true ? "" : String(value));
    });
    (kids || []).forEach((kid) => {
      if (kid == null || kid === false) return;
      node.append(kid instanceof Node ? kid : document.createTextNode(String(kid)));
    });
    return node;
  }

  function svg(html) {
    const wrap = document.createElement("span");
    wrap.innerHTML = html;
    return wrap;
  }

  function boot() {
    if (window.__gb) return;
    const fill = document.querySelector(".gb-fill");
    if (!fill || !window.GB_RALLY) return;
    window.__gb = true;
    modal = h("div", { class: "gb-modal", hidden: true });
    modal.addEventListener("click", (event) => {
      if (event.target === modal) {
        modal.hidden = true;
        syncPanels();
      }
    });
    fill.append(modal);
    mountViewer(fill);
    document.addEventListener("keydown", onKey);
    document.addEventListener("pointerdown", (event) => {
      if (!editOpen) return;
      const back = event.target.closest && event.target.closest(".gb-back");
      if (!back) return;
      swallowBack = true;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
      closeEdit();
    }, true);
    document.addEventListener("click", (event) => {
      if (!swallowBack) return;
      const back = event.target.closest && event.target.closest(".gb-back");
      if (!back) {
        swallowBack = false;
        return;
      }
      swallowBack = false;
      event.preventDefault();
      event.stopPropagation();
      event.stopImmediatePropagation();
    }, true);
    document.addEventListener("click", (event) => {
      const inPop = event.target.closest(".gb-pop, .gb-popbox, .gb-viewer, .gb-modal");
      const inDesk = event.target.closest(".gb-clip, .gb-track-lane, .gb-voicebox, .gb-line-field");
      const inBoard = event.target.closest(".gb-stage");
      let refresh = false;
      if (!inPop && (promptOpen || durationOpen || aspectOpen || (libraryOpen && !inBoard))) {
        libraryOpen = promptOpen = durationOpen = aspectOpen = false;
        syncPanels();
        refresh = true;
      }
      if (speakOpen && !event.target.closest(".gb-speak-inline")) {
        speakOpen = false;
        speakMenu = false;
        refresh = true;
      }
      if (refresh) render();
      if (desk && !inDesk) settleDesk();
    });
    window.addEventListener("resize", () => {
      clearTimeout(resizeTimer);
      resizeTimer = setTimeout(() => {
        if (dragging || editing) {
          drawLinks();
          placeViewer();
          return;
        }
        render();
      }, 80);
    });
    load();
  }

  function mountViewer(fill) {
    viewerVideo = h("video", {
      class: "gb-viewer-video",
      controls: true,
      playsinline: true,
      loop: true,
    });
    const prefer = localStorage.getItem(MUTE_KEY);
    viewerVideo.muted = prefer === null ? true : prefer === "1";
    viewerVideo.addEventListener("volumechange", () => {
      localStorage.setItem(MUTE_KEY, (viewerVideo.muted || viewerVideo.volume === 0) ? "1" : "0");
    });
    viewerName = h("div", { class: "gb-viewer-name", text: "" });
    const bar = h("div", { class: "gb-viewer-bar" }, [
      viewerName,
      h("button", {
        class: "gb-viewer-x",
        type: "button",
        text: "Regenerate",
        onclick: (event) => {
          event.stopPropagation();
          openRedo();
        },
      }),
      h("button", {
        class: "gb-viewer-x",
        type: "button",
        text: "Edit",
        onclick: (event) => {
          event.stopPropagation();
          openEdit(watchedAsset(), true);
        },
      }),
      h("button", {
        class: "gb-viewer-x",
        type: "button",
        text: "Close",
        onclick: (event) => {
          event.stopPropagation();
          closeViewer();
        },
      }),
    ]);
    viewerEl = h("div", { class: "gb-viewer", hidden: true }, [bar, viewerVideo]);
    fill.append(viewerEl);
  }

  function isPanelOpen() {
    return !!(
      (modal && !modal.hidden) ||
      libraryOpen || promptOpen || durationOpen || aspectOpen
    );
  }

  function syncPanels() {
    const open = isPanelOpen();
    if (editVideo && editOpen) {
      if (open && !editVideo.paused) {
        editWasPlaying = true;
        editVideo.pause();
      } else if (!open && panelsWereOpen && editWasPlaying) {
        editWasPlaying = false;
        editPlaying = true;
        editVideo.play().catch(() => {});
      }
    }
    if (!viewerVideo) {
      panelsWereOpen = open;
      return;
    }
    if (open) {
      if (!panelsWereOpen) viewerWasPlaying = !viewerVideo.paused;
      if (!viewerVideo.paused) viewerVideo.pause();
    } else if (panelsWereOpen && viewerWasPlaying && viewerEl && !viewerEl.hidden) {
      viewerWasPlaying = false;
      viewerVideo.play().catch(() => {});
    }
    panelsWereOpen = open;
  }

  function openViewer(url, name, autoplay) {
    if (!viewerEl || !viewerVideo || !url) return;
    viewerEl.hidden = false;
    if (viewerName) viewerName.textContent = name || "Take";
    const changed = viewerUrl !== url;
    if (changed) {
      viewerUrl = url;
      viewerVideo.src = url;
    }
    placeViewer();
    if (!autoplay) return;
    if (isPanelOpen()) viewerWasPlaying = true;
    else if (changed || viewerVideo.paused) viewerVideo.play().catch(() => {});
  }

  function closeViewer() {
    if (!viewerEl) return;
    viewerEl.hidden = true;
    viewerWasPlaying = false;
    watchId = null;
    if (viewerVideo) viewerVideo.pause();
    markSelection();
  }

  function placeViewer() {
    if (!viewerEl || viewerEl.hidden) return;
    const seq = document.querySelector(".gb-seq");
    const fill = document.querySelector(".gb-fill");
    if (!seq || !fill) return;
    const top = seq.getBoundingClientRect().bottom - fill.getBoundingClientRect().top + 8;
    viewerEl.style.top = Math.max(56, top) + "px";
  }

  function noteFreshTake() {
    if (editOpen) return;
    const pending = state && state.pending;
    const url = pending && pending.url;
    if (!url || url === autoPlayedUrl) return;
    autoPlayedUrl = url;
    const playedKey = "gb-played:" + window.GB_RALLY;
    let seen = [];
    try { seen = JSON.parse(sessionStorage.getItem(playedKey) || "[]"); } catch (error) { seen = []; }
    if (seen.indexOf(url) >= 0) return;
    seen.push(url);
    sessionStorage.setItem(playedKey, JSON.stringify(seen.slice(-8)));
    watchId = "pending";
    selected = { kind: "sequence", id: "pending" };
    openViewer(url, pending.name || "New take", true);
    markSelection();
  }

  async function load() {
    const stage = document.getElementById("gb-stage");
    if (stage) stage.textContent = "";
    try {
      const res = await fetch(API + "?rally=" + encodeURIComponent(window.GB_RALLY));
      const data = await res.json();
      if (!res.ok) throw new Error(data.error || "Could not open this project");
      apply(data);
    } catch (error) {
      if (stage) stage.textContent = error.message || "Could not open this project";
    }
  }

  async function call(op, extra) {
    const previousTakes = new Set(((state && state.sequence) || []).map((item) => item.id));
    const res = await fetch(API, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(Object.assign({ rally: window.GB_RALLY, op: op }, extra || {})),
    });
    let data = {};
    try {
      data = await res.json();
    } catch (error) {
      data = { error: "The studio did not answer" };
    }
    if (!res.ok && !data.title) {
      armEdit = null;
      if (state) {
        state.error = data.error || "That edit did not land";
        render();
      }
      return data;
    }
    apply(data);
    if (op === "edit_keep" && state && !state.error) {
      editOpen = false;
      editPlaying = false;
      if (editVideo) editVideo.pause();
      if (editAudio) editAudio.pause();
      const added = (state.sequence || []).filter((item) => !previousTakes.has(item.id));
      const made = (state.sequence || []).find((item) => item.asset_id === state.flash);
      render();
      if (added.length > 1) {
        closeViewer();
        if (made) {
          selected = { kind: "sequence", id: made.id };
          watchId = made.id;
          markSelection();
        }
      } else if (made) {
        selected = { kind: "sequence", id: made.id };
        watchId = made.id;
        markSelection();
        openViewer(made.url, made.name, true);
      }
    }
    return data;
  }

  function apply(data) {
    held = data;
    flush();
  }

  function flush() {
    if (!held || dragging || editing) return;
    const previous = armEdit;
    armEdit = null;
    state = held;
    held = null;
    render();
    if (previous && state) {
      const born = (state.clips || []).find((clip) => !previous.has(clip.id));
      if (born && !desk) {
        selected = { kind: "clip", id: born.id };
        render();
      }
    }
    if (state && state.busy) schedule();
  }

  function schedule() {
    clearTimeout(pollTimer);
    pollTimer = setTimeout(async () => {
      try {
        const res = await fetch(API + "?rally=" + encodeURIComponent(window.GB_RALLY));
        apply(await res.json());
      } catch (error) {
        schedule();
      }
    }, 700);
  }

  function render() {
    const top = document.getElementById("gb-top-slot");
    const stage = document.getElementById("gb-stage");
    if (!top || !stage || !state || state.error && !state.title) {
      if (stage && state && state.error) stage.textContent = state.error;
      return;
    }
    if (desk && desk.id && !(state.clips || []).some((clip) => clip.id === desk.id)) desk = null;
    const fill = document.querySelector(".gb-fill");
    if (fill) fill.style.setProperty("--gb-aspect", (state.aspect || "16:9").replace(":", " / "));
    const lib = document.querySelector(".gb-lib-list");
    const scroller = stage.querySelector(".gb-scroller");
    const libTop = lib ? lib.scrollTop : 0;
    const scrollLeft = scroller ? scroller.scrollLeft : 0;
    top.replaceChildren(renderTop());
    stage.replaceChildren(renderStageBody());
    const nextLib = document.querySelector(".gb-lib-list");
    const nextScroll = stage.querySelector(".gb-scroller");
    if (nextLib) nextLib.scrollTop = libTop;
    if (nextScroll) nextScroll.scrollLeft = scrollLeft;
    noteFreshTake();
    syncPanels();
    requestAnimationFrame(() => {
      drawLinks();
      placeViewer();
      if (editOpen && state.edit) {
        const total = state.edit.seconds || 0;
        if (editTime > total) editTime = total;
        placeEdit(editPlaying && !isPanelOpen());
        paintEditHead();
      }
      if (speakOpen && speakFocus) {
        speakFocus = false;
        const field = document.querySelector(".gb-speak-field");
        if (field) {
          field.focus();
          const end = field.value.length;
          field.setSelectionRange(end, end);
        }
      }
      if (desk && desk.focus && !editOpen) {
        const field = document.querySelector(".gb-line-field");
        if (field) {
          field.focus();
          const end = field.value.length;
          field.setSelectionRange(end, end);
        }
      }
    });
  }

  function togglePop(which) {
    const open = which === "library" ? libraryOpen
      : which === "prompt" ? promptOpen
      : which === "duration" ? durationOpen
      : aspectOpen;
    libraryOpen = promptOpen = durationOpen = aspectOpen = false;
    if (!open) {
      if (which === "library") libraryOpen = true;
      else if (which === "prompt") promptOpen = true;
      else if (which === "duration") durationOpen = true;
      else aspectOpen = true;
    }
    render();
  }

  function renderTop() {
    const title = h("div", { class: "gb-title" }, [
      h("div", { class: "gb-name", text: state.title || "Project" }),
    ]);
    if (state.concept) title.append(h("div", { class: "gb-concept", text: state.concept }));
    const note = state.note || state.error || "";
    return h("div", { class: "gb-top-rest" }, [
      title,
      h("div", { class: "gb-spacer" }),
      h("div", { class: "gb-note" + (state.error ? " is-err" : ""), text: note }),
      h("div", { class: "gb-tools" }, [
        pop("library", "Library", libraryOpen, h("div", { hidden: true })),
        pop("prompt", "Prompt", promptOpen, renderPrompt()),
        pop("duration", state.duration + "s", durationOpen, renderDuration()),
        pop("aspect", state.aspect, aspectOpen, renderAspect()),
        h("button", {
          class: "gb-pop-toggle" + (editOpen ? " is-on" : ""),
          type: "button",
          text: "Edit",
          onclick: (event) => {
            event.stopPropagation();
            if (editOpen) closeEdit();
            else openEdit(watchedAsset(), false);
          },
        }),
      ]),
      h("button", {
        class: "gb-go" + (state.busy ? " is-busy" : ""),
        type: "button",
        text: state.busy ? "Working" : "Generate",
        onclick: onGenerate,
      }),
    ]);
  }

  function pop(which, label, open, panel) {
    const button = h("button", {
      class: "gb-pop-toggle" + (open ? " is-on" : ""),
      type: "button",
      text: label,
      onclick: (event) => {
        event.stopPropagation();
        togglePop(which);
      },
    });
    panel.hidden = !open;
    return h("div", { class: "gb-popbox" }, [button, panel]);
  }

  function renderDuration() {
    const value = h("div", { class: "gb-dur-val", text: state.duration + "s" });
    const slider = h("input", {
      class: "gb-slider",
      type: "range",
      min: "1",
      max: "15",
      step: "1",
      value: String(state.duration),
      "aria-label": "Duration in seconds",
    });
    slider.addEventListener("input", () => {
      value.textContent = slider.value + "s";
    });
    slider.addEventListener("change", () => {
      const next = Number(slider.value);
      if (!state || next === state.duration) return;
      call("set_duration", { duration: next });
    });
    return h("div", { class: "gb-pop" }, [
      h("div", { class: "gb-pop-k", text: "Duration" }),
      h("div", { class: "gb-dur" }, [slider, value]),
    ]);
  }

  function renderAspect() {
    const row = h("div", { class: "gb-aspects" });
    (state.aspects || []).forEach((ratio) => {
      const parts = String(ratio).split(":");
      const aw = Number(parts[0]) || 1;
      const ah = Number(parts[1]) || 1;
      const max = 28;
      const box = h("span", { class: "gb-ratio" });
      box.style.width = (aw >= ah ? max : Math.max(10, Math.round(max * aw / ah))) + "px";
      box.style.height = (ah >= aw ? max : Math.max(10, Math.round(max * ah / aw))) + "px";
      const button = h("button", {
        class: "gb-aspect" + (ratio === state.aspect ? " is-on" : ""),
        type: "button",
        onclick: (event) => {
          event.stopPropagation();
          aspectOpen = false;
          call("set_aspect", { aspect: ratio });
        },
      }, [box, h("span", { text: ratio })]);
      row.append(button);
    });
    return h("div", { class: "gb-pop" }, [
      row,
      h("div", { class: "gb-aspect-hint", text: "16:9 is wide · 9:16 is portrait · 1:1 is square" }),
    ]);
  }

  function renderPrompt() {
    return h("div", { class: "gb-pop gb-prompt-pop" }, [
      h("div", { class: "gb-pop-k", text: "Compiled prompt" }),
      h("pre", { class: "gb-prompt-text", text: state.prompt || "" }),
    ]);
  }

  function onGenerate() {
    if (!state || state.busy) return;
    const line = (state.clips || []).some((clip) => (clip.text || "").trim());
    if (!state.stills.length && !state.pins.length && !line) {
      state.error = "Add a still or a line first";
      render();
      return;
    }
    call("generate_take");
  }

  function renderSequence() {
    const row = h("div", { class: "gb-seq-row" });
    row.addEventListener("dragover", (event) => event.preventDefault());
    row.addEventListener("drop", (event) => {
      const data = readDrag(event);
      if (!data || data.kind !== "video") return;
      event.preventDefault();
      call("add_sequence", { asset_id: data.id });
    });
    (state.sequence || []).forEach((item) => row.append(seqItem(item, false)));
    if (state.pending) {
      row.append(seqItem({
        id: "pending",
        asset_id: state.pending.asset_id,
        name: state.pending.name,
        url: state.pending.url,
      }, true));
    }
    if (!state.sequence.length && !state.pending) {
      row.append(h("div", { class: "gb-hint", text: "Kept takes show up here" }));
    }
    return h("section", { class: "gb-seq" }, [
      h("div", { class: "gb-seq-k", text: "Takes" }),
      row,
    ]);
  }

  function seqItem(item, pending) {
    const on = (selected && selected.kind === "sequence" && selected.id === item.id) || watchId === item.id;
    const card = h("div", {
      class: "gb-seq-item" + (pending ? " is-pending" : "") + (on ? " is-on" : ""),
      title: pending ? "Click to watch. Double-click to keep this take." : "Click to watch",
      "data-sel": "sequence:" + item.id,
    });
    const video = h("video", { src: item.url + "#t=0.1", muted: true, playsinline: true, preload: "metadata" });
    video.draggable = false;
    video.autoplay = false;
    card.append(video, h("div", { class: "gb-seq-name", text: pending ? "New" : item.name }));
    const first = h("button", { class: "gb-frame is-first", type: "button", title: "Pull the first frame. Double-click to pin it as a reference." });
    const last = h("button", { class: "gb-frame is-last", type: "button", title: "Pull the last frame. Double-click to open the next shot on it." });
    bindExtract(first, item.asset_id, "first");
    bindExtract(last, item.asset_id, "last");
    card.append(first, last);
    if (!pending) card.append(xBtn("sequence", item.id, "Remove take"));
    card.addEventListener("click", () => {
      selected = { kind: "sequence", id: item.id };
      watchId = item.id;
      libraryOpen = promptOpen = durationOpen = aspectOpen = false;
      if (modal) modal.hidden = true;
      document.querySelectorAll(".gb-pop").forEach((node) => { node.hidden = true; });
      document.querySelectorAll(".gb-pop-toggle.is-on").forEach((node) => node.classList.remove("is-on"));
      panelsWereOpen = false;
      viewerWasPlaying = false;
      markSelection();
      openViewer(item.url, pending ? "New take" : item.name, true);
    });
    card.addEventListener("dblclick", (event) => {
      if (!pending) return;
      event.preventDefault();
      call("keep");
    });
    return card;
  }

  function bindExtract(node, assetId, which) {
    let timer = 0;
    node.addEventListener("click", (event) => {
      event.stopPropagation();
      clearTimeout(timer);
      timer = setTimeout(() => call("extract", { asset_id: assetId, which: which }), 240);
    });
    node.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      event.preventDefault();
      clearTimeout(timer);
      call("extract", {
        asset_id: assetId,
        which: which,
        place: which === "last" ? "start" : "pin",
      });
    });
  }

  function renderWork() {
    if (editOpen) return h("div", { class: "gb-work" }, [renderEditor()]);
    return h("div", { class: "gb-work" }, [renderMain()]);
  }

  function renderStageBody() {
    const col = h("div", { class: "gb-stage-col" }, [renderSequence(), renderWork()]);
    if (!libraryOpen) return col;
    return h("div", { class: "gb-stage-row" }, [col, renderLibrary()]);
  }

  function watchedAsset() {
    if (!state) return "";
    if (watchId === "pending" && state.pending) return state.pending.asset_id || "";
    const onWatch = (state.sequence || []).find((item) => item.id === watchId);
    if (onWatch) return onWatch.asset_id || "";
    if (selected && selected.kind === "sequence") {
      if (selected.id === "pending" && state.pending) return state.pending.asset_id || "";
      const row = (state.sequence || []).find((item) => item.id === selected.id);
      if (row) return row.asset_id || "";
    }
    return "";
  }

  function openEdit(assetId, fresh) {
    desk = null;
    editOpen = true;
    editTime = 0;
    editPlaying = false;
    editWasPlaying = false;
    editSeeking = false;
    editSel = "";
    if (editVideo) editVideo.pause();
    closeViewer();
    const hasCut = !!(state && state.edit && state.edit.clips && state.edit.clips.length);
    const extra = {};
    if (assetId && (fresh || !hasCut)) {
      extra.asset_id = assetId;
      if (fresh) extra.fresh = true;
    }
    call("edit_open", extra);
  }

  function closeEdit() {
    editOpen = false;
    speakOpen = false;
    speakMenu = false;
    editPlaying = false;
    editWasPlaying = false;
    if (editVideo) editVideo.pause();
    if (editAudio) editAudio.pause();
    render();
  }

  function ensureEditVideo() {
    if (editVideo) return editVideo;
    editVideo = h("video", { class: "gb-edit-video", playsinline: true, preload: "auto" });
    const prefer = localStorage.getItem(MUTE_KEY);
    editMuted = prefer === null ? true : prefer === "1";
    editVideo.muted = editMuted;
    editVideo.addEventListener("timeupdate", onEditTick);
    editVideo.addEventListener("seeked", () => { editSeeking = false; });
    editVideo.addEventListener("ended", () => advanceEdit(true));
    return editVideo;
  }

  function sameSource(current, next) {
    if (!current || !next) return false;
    if (current === next) return true;
    try {
      return new URL(current, location.href).href === new URL(next, location.href).href;
    } catch (error) {
      return false;
    }
  }

  function editParts() {
    const out = [];
    let cursor = 0;
    const clips = (state && state.edit && state.edit.clips) || [];
    clips.forEach((clip) => {
      (clip.parts || []).forEach((part) => {
        const len = Math.max(0, part.end - part.start);
        out.push({
          clipId: clip.id,
          asset_id: part.asset_id,
          url: part.url,
          name: part.name,
          start: part.start,
          end: part.end,
          fps: part.fps || 30,
          mute: !!part.mute,
          audio: !!part.audio,
          media: part.media || part.end,
          t0: cursor,
          t1: cursor + len,
        });
        cursor += len;
      });
    });
    return out;
  }

  function partAt(time) {
    const parts = editParts();
    if (!parts.length) return null;
    const hit = parts.find((part) => time < part.t1 - 0.0001);
    return hit || parts[parts.length - 1];
  }

  function playingPart() {
    if (!editVideo) return null;
    const now = editVideo.currentTime || 0;
    const url = editVideo.currentSrc || editVideo.getAttribute("src") || "";
    const parts = editParts().filter((part) => sameSource(url, part.url));
    if (!parts.length) return null;
    const inside = parts.find((part) => now >= part.start - 0.02 && now < part.end - 0.008);
    if (inside) return inside;
    let best = parts[0];
    parts.forEach((part) => {
      if (part.start <= now + 0.02) best = part;
    });
    return best;
  }

  function onEditTick() {
    if (editSeeking || !editOpen || !editVideo || !state || !state.edit) return;
    const part = playingPart();
    if (!part) return;
    const edge = Math.min(0.04, 1 / (part.fps || 30));
    if (editVideo.currentTime >= part.end - edge) {
      advanceEdit(false);
      return;
    }
    editTime = part.t0 + Math.max(0, editVideo.currentTime - part.start);
    syncEditMute();
    followSound();
    paintEditHead();
  }

  function ensureEditAudio() {
    if (editAudio) return editAudio;
    editAudio = h("audio", { preload: "auto" });
    return editAudio;
  }

  function syncEditMute() {
    const part = partAt(editTime);
    if (editVideo) editVideo.muted = editMuted || !!(part && part.mute);
    if (editAudio) editAudio.muted = editMuted;
  }

  function followSound() {
    const audio = ensureEditAudio();
    if (!editPlaying || editMuted || !state || !state.edit) {
      audio.pause();
      return;
    }
    const sound = (state.edit.sounds || []).find((row) => {
      return editTime >= row.at && editTime < row.at + (row.seconds || 0) - 0.03;
    });
    if (!sound || !sound.url) {
      audio.pause();
      return;
    }
    const local = sound.start + Math.max(0, editTime - sound.at);
    if (!sameSource(audio.getAttribute("src") || "", sound.url)) audio.src = sound.url;
    if (Math.abs((audio.currentTime || 0) - local) > 0.35) audio.currentTime = Math.max(0, local);
    if (audio.paused) audio.play().catch(() => {});
  }

  function advanceEdit(fromEnded) {
    const parts = editParts();
    const part = playingPart();
    const index = part ? parts.findIndex((row) => row.t0 === part.t0 && row.clipId === part.clipId && row.start === part.start) : -1;
    if (index >= 0 && index < parts.length - 1) {
      editTime = parts[index + 1].t0;
      placeEdit(true);
      paintEditHead();
      return;
    }
    editPlaying = false;
    if (parts.length) editTime = parts[parts.length - 1].t1;
    if (editVideo && !fromEnded) editVideo.pause();
    paintEditHead();
  }

  function placeEdit(resume) {
    const video = ensureEditVideo();
    const part = partAt(editTime);
    if (!part || !part.url) return;
    const local = Math.max(part.start, Math.min(part.end - 0.005, part.start + Math.max(0, editTime - part.t0)));
    const apply = () => {
      if (Math.abs((video.currentTime || 0) - local) > 0.08) {
        editSeeking = true;
        video.currentTime = Math.max(0, local);
      } else {
        editSeeking = false;
      }
      syncEditMute();
      if (resume && editOpen && !isPanelOpen()) video.play().catch(() => {});
    };
    const shown = video.getAttribute("src") || "";
    if (!sameSource(shown, part.url)) {
      editSeeking = true;
      video.src = part.url;
      video.addEventListener("loadeddata", apply, { once: true });
    } else {
      apply();
    }
  }

  function paintEditHead() {
    const head = document.querySelector(".gb-edit-head");
    const stamp = document.querySelector(".gb-edit-tc");
    const stage = document.querySelector(".gb-edit-stage");
    const total = (state && state.edit && state.edit.seconds) || 0;
    const width = total > 0 ? Math.max(0, Math.min(100, (editTime / total) * 100)) : 0;
    if (head) {
      head.style.left = width + "%";
      head.classList.toggle("is-armed", canCutNow());
    }
    if (stamp) stamp.textContent = editStamp(editTime);
    if (stage) stage.classList.toggle("is-playing", !!editPlaying);
  }

  function editStamp(time) {
    const safe = Math.max(0, time || 0);
    const mins = Math.floor(safe / 60);
    const secs = safe - mins * 60;
    const part = partAt(safe);
    const fps = (part && part.fps) || 30;
    const frame = Math.max(0, Math.round(safe * fps));
    const whole = secs.toFixed(2).padStart(5, "0");
    return mins + ":" + whole + " · frame " + frame;
  }

  function canCutNow() {
    const part = partAt(editTime);
    if (!part || !editParts().length) return false;
    const local = editTime - part.t0;
    const length = part.t1 - part.t0;
    const room = Math.max(1 / (part.fps || 30), 0.04);
    return local > room && length - local > room;
  }

  function cutHere() {
    if (!canCutNow()) return;
    editPlaying = false;
    if (editVideo) editVideo.pause();
    call("edit_cut", { at: editTime });
  }

  function clipUnderPlayhead() {
    const edit = state && state.edit;
    if (!edit) return null;
    let cursor = 0;
    for (let i = 0; i < edit.clips.length; i += 1) {
      const clip = edit.clips[i];
      const next = cursor + (clip.seconds || 0);
      if (editTime <= next + 0.000001 || i === edit.clips.length - 1) return clip;
      cursor = next;
    }
    return null;
  }

  function timeOn(node, event, total) {
    const rect = node.getBoundingClientRect();
    const ratio = (event.clientX - rect.left) / Math.max(1, rect.width);
    return Math.max(0, Math.min(total, ratio * total));
  }

  function snapTime(time, total) {
    const marks = [0];
    if (total) marks.push(total);
    let cursor = 0;
    ((state && state.edit && state.edit.clips) || []).forEach((clip) => {
      cursor += clip.seconds || 0;
      marks.push(cursor);
    });
    ((state && state.edit && state.edit.sounds) || []).forEach((sound) => {
      marks.push(sound.at || 0);
      marks.push((sound.at || 0) + (sound.seconds || 0));
    });
    let best = time;
    let dist = 0.08;
    marks.forEach((mark) => {
      const gap = Math.abs(mark - time);
      if (gap < dist) {
        dist = gap;
        best = mark;
      }
    });
    return Math.max(0, Math.min(total || 0, best));
  }

  function bindScrub(node, total) {
    node.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      if (event.target.closest(".gb-edit-edge, .gb-edit-lift, .gb-edit-x, .gb-edit-blade, .gb-edit-join, .gb-speak-inline")) return;
      if (event.target.closest(".gb-edit-sound")) return;
      if (event.target.closest(".gb-edit-clip") && !event.target.closest(".gb-edit-head")) return;
      const scrub = (ev) => seekEdit(snapTime(timeOn(node, ev, total), total));
      scrub(event);
      function move(ev) { scrub(ev); }
      function up() {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
      }
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
  }

  function selectedEditClip() {
    const edit = state && state.edit;
    if (!edit) return null;
    return (edit.clips || []).find((clip) => clip.id === editSel) || clipUnderPlayhead();
  }

  function stepEdit(dir) {
    const part = partAt(editTime);
    const fps = (part && part.fps) || 30;
    const total = (state && state.edit && state.edit.seconds) || 0;
    editPlaying = false;
    if (editVideo) editVideo.pause();
    editTime = Math.max(0, Math.min(total, editTime + dir / fps));
    placeEdit(false);
    paintEditHead();
  }

  function toggleEditPlay() {
    if (!state || !state.edit || !state.edit.clips.length) return;
    const video = ensureEditVideo();
    if (editPlaying && !video.paused) {
      editPlaying = false;
      video.pause();
      paintEditHead();
      return;
    }
    const total = state.edit.seconds || 0;
    if (total && editTime >= total - 0.05) editTime = 0;
    editPlaying = true;
    placeEdit(true);
    paintEditHead();
  }

  function seekEdit(time) {
    const total = (state && state.edit && state.edit.seconds) || 0;
    editTime = Math.max(0, Math.min(total, time));
    placeEdit(editPlaying);
    paintEditHead();
  }

  const SPEAK_VOICES = ["eve", "ara", "leo", "rex", "sal", "gork"];

  async function placeSpeech() {
    const text = (speakDraft || "").trim();
    if (!text || speakBusy || !editOpen) return;
    speakBusy = true;
    speakFocus = true;
    render();
    let data = {};
    try {
      data = await call("edit_speak", { text: text, voice: speakVoice, at: speakAt });
    } catch (error) {
      data = { error: "Speech did not answer" };
      if (state) state.error = data.error;
    }
    speakBusy = false;
    const failed = !data || data.error || (state && state.error);
    if (!failed) {
      speakOpen = false;
      speakDraft = "";
      const sounds = (state && state.edit && state.edit.sounds) || [];
      const born = sounds[sounds.length - 1];
      if (born) {
        soundSel = born.id;
        editSel = "";
      }
    } else {
      speakFocus = true;
    }
    render();
  }

  function icon(markup) {
    const wrap = document.createElement("span");
    wrap.innerHTML = markup;
    return wrap.firstElementChild || wrap;
  }

  function speakerIcon(muted) {
    if (muted) {
      return icon('<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M4 9h3.2L11 5.6v12.8L7.2 15H4V9z"/><path d="M15.2 9.4l5 5.2M20.2 9.4l-5 5.2" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>');
    }
    return icon('<svg viewBox="0 0 24 24" width="16" height="16"><path fill="currentColor" d="M4 9h3.2L11 5.6v12.8L7.2 15H4V9z"/><path d="M15 9.2a4 4 0 010 5.6M17.6 7a7 7 0 010 10" stroke="currentColor" stroke-width="1.6" fill="none"/></svg>');
  }

  function edgeHandle(edge) {
    return h("div", {
      class: "gb-edit-edge is-" + edge,
      title: edge === "in" ? "Trim in" : "Trim out",
    });
  }

  function editX(title, action) {
    const button = h("button", {
      class: "gb-edit-x",
      type: "button",
      text: "×",
      title: title,
    });
    button.addEventListener("pointerdown", (event) => event.stopPropagation());
    button.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      action();
    });
    return button;
  }

  function bindPictureTrim(handle, spec) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      event.preventDefault();
      editPlaying = false;
      if (editVideo) editVideo.pause();
      paintEditHead();
      const edge = spec.edge;
      const part = spec.part;
      const origin = edge === "in" ? Number(part.start) || 0 : Number(part.end) || 0;
      let sourceAt = origin;
      let moved = false;
      editing += 1;
      spec.node.classList.add("is-trimming");
      handle.classList.add("is-hot");
      const clipSeconds = Math.max(spec.clip.seconds || 0.05, 0.05);
      function move(ev) {
        moved = true;
        const time = timeOn(spec.lane, ev, spec.total);
        const room = Math.max(1 / (part.fps || 30), 0.04);
        const media = Math.max(Number(part.media) || Number(part.end) || 0, Number(part.end) || 0);
        const px = spec.node.clientWidth / clipSeconds;
        let delta = 0;
        if (edge === "in") {
          sourceAt = Math.min((Number(part.end) || 0) - room, Math.max(0, (Number(part.start) || 0) + (time - spec.t0)));
          delta = sourceAt - (Number(part.start) || 0);
          handle.style.left = (delta * px) + "px";
          handle.style.right = "auto";
        } else {
          sourceAt = Math.max((Number(part.start) || 0) + room, Math.min(media, (Number(part.end) || 0) + (time - spec.t1)));
          delta = (Number(part.end) || 0) - sourceAt;
          handle.style.right = (delta * px) + "px";
          handle.style.left = "auto";
        }
        if (delta > 0.01) {
          spec.shade.style.display = "block";
          spec.shade.className = "gb-edit-shade is-" + edge;
          spec.shade.style.width = Math.min(100, (delta / clipSeconds) * 100) + "%";
        } else {
          spec.shade.style.display = "none";
        }
        const next = Math.max(room, clipSeconds - delta);
        spec.duration.textContent = spec.name + " · " + next.toFixed(1) + "s" + (spec.lifted ? " · sound lifted" : "");
      }
      function up() {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        editing = Math.max(0, editing - 1);
        if (moved) {
          ignoreClick = true;
          setTimeout(() => { ignoreClick = false; }, 0);
        }
        if (moved && Math.abs(sourceAt - origin) >= 0.01) {
          call("edit_trim", { id: spec.clip.id, edge: edge, at: sourceAt });
        } else if (moved) {
          render();
        }
      }
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    handle.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
    handle.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
  }

  function bindSoundTrim(handle, sound, edge, block, lane, total) {
    handle.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      event.preventDefault();
      editPlaying = false;
      if (editVideo) editVideo.pause();
      paintEditHead();
      const origin = edge === "in" ? Number(sound.start) || 0 : Number(sound.end) || 0;
      let sourceAt = origin;
      let moved = false;
      editing += 1;
      handle.classList.add("is-hot");
      function move(ev) {
        moved = true;
        const time = timeOn(lane, ev, total);
        const room = 0.04;
        const media = Math.max(Number(sound.media) || Number(sound.end) || 0, Number(sound.end) || 0);
        if (edge === "in") {
          sourceAt = Math.min((Number(sound.end) || 0) - room, Math.max(0, (Number(sound.start) || 0) + (time - (Number(sound.at) || 0))));
          const at = (Number(sound.at) || 0) + (sourceAt - (Number(sound.start) || 0));
          const seconds = Math.max(room, (Number(sound.end) || 0) - sourceAt);
          block.style.left = (total ? (at / total) * 100 : 0) + "%";
          block.style.width = Math.max(total ? (seconds / total) * 100 : 8, 2) + "%";
        } else {
          const endAt = (Number(sound.at) || 0) + (Number(sound.seconds) || 0);
          sourceAt = Math.max((Number(sound.start) || 0) + room, Math.min(media, (Number(sound.end) || 0) + (time - endAt)));
          const seconds = Math.max(room, sourceAt - (Number(sound.start) || 0));
          block.style.width = Math.max(total ? (seconds / total) * 100 : 8, 2) + "%";
        }
      }
      function up() {
        window.removeEventListener("pointermove", move);
        window.removeEventListener("pointerup", up);
        editing = Math.max(0, editing - 1);
        if (moved) {
          ignoreClick = true;
          setTimeout(() => { ignoreClick = false; }, 0);
        }
        if (moved && Math.abs(sourceAt - origin) >= 0.01) {
          call("edit_trim_sound", { id: sound.id, edge: edge, at: sourceAt });
        } else if (moved) {
          render();
        }
      }
      window.addEventListener("pointermove", move);
      window.addEventListener("pointerup", up);
    });
    handle.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
    handle.addEventListener("dblclick", (event) => {
      event.stopPropagation();
      event.preventDefault();
    });
  }

  function speakInline(total) {
    const at = Math.max(0, Math.min(total || 0, speakAt));
    const wrap = h("div", { class: "gb-speak-inline" });
    const ratio = total ? at / total : 0;
    if (ratio > 0.58) wrap.style.right = ((1 - ratio) * 100) + "%";
    else wrap.style.left = (ratio * 100) + "%";
    const field = h("input", {
      class: "gb-speak-field",
      type: "text",
      placeholder: speakBusy ? "Speaking…" : "What they say",
    });
    field.value = speakDraft;
    if (speakBusy) field.disabled = true;
    field.addEventListener("input", () => { speakDraft = field.value; });
    field.addEventListener("keydown", (event) => {
      if (event.key !== "Enter") return;
      event.preventDefault();
      event.stopPropagation();
      speakDraft = field.value;
      placeSpeech();
    });
    const voice = h("button", {
      class: "gb-speak-voice" + (speakMenu ? " is-on" : ""),
      type: "button",
      text: speakVoice,
      title: "Voice",
    });
    voice.addEventListener("pointerdown", (event) => event.stopPropagation());
    voice.addEventListener("click", (event) => {
      event.stopPropagation();
      speakDraft = field.value;
      speakMenu = !speakMenu;
      speakFocus = true;
      render();
    });
    wrap.addEventListener("pointerdown", (event) => event.stopPropagation());
    wrap.addEventListener("click", (event) => event.stopPropagation());
    wrap.append(field, voice);
    if (speakMenu) {
      const menu = h("div", { class: "gb-speak-menu" });
      SPEAK_VOICES.forEach((name) => {
        const choice = h("button", {
          class: "gb-speak-name" + (name === speakVoice ? " is-on" : ""),
          type: "button",
          text: name,
        });
        choice.addEventListener("click", (event) => {
          event.stopPropagation();
          speakVoice = name;
          speakMenu = false;
          speakDraft = field.value;
          speakFocus = true;
          render();
        });
        menu.append(choice);
      });
      wrap.append(menu);
    }
    return wrap;
  }

  function renderEditor() {
    const edit = (state && state.edit) || { clips: [], seconds: 0, undo: 0, sounds: [] };
    const clips = edit.clips || [];
    const sounds = edit.sounds || [];
    const total = edit.seconds || 0;
    const video = ensureEditVideo();
    const stage = h("div", { class: "gb-edit-stage" + (editPlaying ? " is-playing" : "") });
    stage.append(video);
    const playMark = h("div", { class: "gb-edit-playmark" });
    playMark.append(icon('<svg viewBox="0 0 24 24" width="22" height="22"><path fill="currentColor" d="M9 6.5v11l10-5.5z"/></svg>'));
    const speaker = h("button", {
      class: "gb-edit-speaker" + (editMuted ? " is-muted" : ""),
      type: "button",
      title: editMuted ? "Muted" : "Sound",
    });
    speaker.append(speakerIcon(editMuted));
    speaker.addEventListener("pointerdown", (event) => event.stopPropagation());
    speaker.addEventListener("click", (event) => {
      event.stopPropagation();
      editMuted = !editMuted;
      localStorage.setItem(MUTE_KEY, editMuted ? "1" : "0");
      if (viewerVideo) viewerVideo.muted = editMuted;
      syncEditMute();
      if (editMuted && editAudio) editAudio.pause();
      render();
    });
    stage.addEventListener("click", (event) => {
      if (event.target.closest(".gb-edit-speaker")) return;
      toggleEditPlay();
    });
    stage.append(playMark, speaker);
    const picked = selectedEditClip();
    const pickedIndex = picked ? clips.findIndex((clip) => clip.id === picked.id) : -1;
    const ruler = h("div", { class: "gb-edit-ruler" });
    const step = total > 20 ? 5 : total > 8 ? 2 : 1;
    for (let s = 0; s <= Math.ceil(total); s += step) {
      if (s > total + 0.001) break;
      const tick = h("span", { text: s + "s" });
      tick.style.left = (total ? (s / total) * 100 : 0) + "%";
      ruler.append(tick);
    }
    const lane = h("div", { class: "gb-edit-lane" });
    const head = h("div", { class: "gb-edit-head" + (canCutNow() ? " is-armed" : "") });
    const blade = h("button", { class: "gb-edit-blade", type: "button", title: "Cut" });
    blade.append(icon('<svg viewBox="0 0 16 16" width="11" height="11"><path fill="currentColor" d="M2.2 1.4h2.1L13.8 14h-2.2z"/></svg>'));
    blade.addEventListener("pointerdown", (event) => event.stopPropagation());
    blade.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      cutHere();
    });
    head.append(blade);
    let cursor = 0;
    clips.forEach((clip, index) => {
      const t0 = cursor;
      const seconds = clip.seconds || 0;
      cursor += seconds;
      const t1 = cursor;
      const node = h("div", {
        class: "gb-edit-clip" + (picked && picked.id === clip.id ? " is-on" : ""),
        draggable: "true",
      });
      node.style.left = (total ? (t0 / total) * 100 : 0) + "%";
      node.style.width = (total ? (Math.max(seconds, 0.05) / total) * 100 : 100) + "%";
      const parts = clip.parts || [];
      const part = parts[0] || {};
      const last = parts[parts.length - 1] || part;
      const name = part.name || "Clip";
      const lifted = parts.some((row) => row.mute);
      const liftable = parts.some((row) => row.audio && !row.mute);
      const shade = h("div", { class: "gb-edit-shade" });
      shade.style.display = "none";
      node.append(shade);
      if (part.url) {
        const thumb = h("video", {
          class: "gb-edit-thumb",
          muted: true,
          playsinline: true,
          preload: "metadata",
        });
        thumb.src = part.url + "#t=0.4";
        thumb.draggable = false;
        node.classList.add("has-thumb");
        node.append(thumb);
      }
      const duration = h("span", {
        class: "gb-edit-clip-d",
        text: name + " · " + seconds.toFixed(1) + "s" + (lifted ? " · sound lifted" : ""),
      });
      if (liftable) duration.style.right = "72px";
      node.append(h("span", { class: "gb-edit-clip-t", text: "Clip " + (index + 1) }), duration);
      const edgeIn = edgeHandle("in");
      const edgeOut = edgeHandle("out");
      bindPictureTrim(edgeIn, { edge: "in", clip: clip, part: part, t0: t0, t1: t1, node: node, shade: shade, duration: duration, name: name, lifted: lifted, lane: lane, total: total });
      bindPictureTrim(edgeOut, { edge: "out", clip: clip, part: last, t0: t0, t1: t1, node: node, shade: shade, duration: duration, name: name, lifted: lifted, lane: lane, total: total });
      node.append(edgeIn, edgeOut);
      if (liftable) {
        const lift = h("button", {
          class: "gb-edit-lift",
          type: "button",
          text: "Lift",
          title: "Lift sound onto the lane",
        });
        lift.addEventListener("pointerdown", (event) => event.stopPropagation());
        lift.addEventListener("dblclick", (event) => {
          event.stopPropagation();
          event.preventDefault();
        });
        lift.addEventListener("click", (event) => {
          event.stopPropagation();
          call("edit_yank", { id: clip.id });
        });
        node.append(lift);
      }
      node.append(editX("Remove clip", () => call("edit_delete", { id: clip.id })));
      node.addEventListener("dragstart", (event) => {
        if (event.target.closest(".gb-edit-edge, .gb-edit-lift, .gb-edit-x")) {
          event.preventDefault();
          return;
        }
        setDrag(event, { from: "edit", id: clip.id });
      });
      node.addEventListener("click", (event) => {
        event.stopPropagation();
        if (ignoreClick) return;
        editSel = clip.id;
        soundSel = "";
        seekEdit(snapTime(timeOn(lane, event, total), total));
        markEditSelection();
      });
      node.addEventListener("dblclick", (event) => {
        event.preventDefault();
        event.stopPropagation();
        editSel = clip.id;
        editTime = timeOn(lane, event, total);
        cutHere();
      });
      lane.append(node);
      if (index < clips.length - 1) {
        const join = h("button", { class: "gb-edit-join", type: "button", title: "Join" });
        join.style.left = (total ? (t1 / total) * 100 : 0) + "%";
        join.append(icon('<svg viewBox="0 0 16 16" width="12" height="12"><path d="M6 3H3v10h3M10 3h3v10h-3" fill="none" stroke="currentColor" stroke-width="1.6"/></svg>'));
        join.addEventListener("pointerdown", (event) => event.stopPropagation());
        join.addEventListener("click", (event) => {
          event.stopPropagation();
          call("edit_join", { id: clip.id });
        });
        ruler.append(join);
      }
    });
    lane.addEventListener("dragover", (event) => event.preventDefault());
    lane.addEventListener("drop", (event) => {
      const data = readDrag(event);
      if (!data || data.from !== "edit") return;
      event.preventDefault();
      event.stopPropagation();
      const ids = clips.map((clip) => clip.id);
      const from = ids.indexOf(data.id);
      if (from < 0) return;
      ids.splice(from, 1);
      let dest = 0;
      lane.querySelectorAll(".gb-edit-clip").forEach((node) => {
        const rect = node.getBoundingClientRect();
        if (event.clientX > rect.left + rect.width / 2) dest += 1;
      });
      if (dest > from) dest -= 1;
      ids.splice(Math.max(0, dest), 0, data.id);
      call("edit_reorder", { ids: ids });
    });
    const alane = h("div", { class: "gb-edit-alane" });
    sounds.forEach((sound) => {
      const block = h("div", {
        class: "gb-edit-sound" + (sound.id === soundSel ? " is-on" : ""),
        title: sound.name || "Sound",
      });
      const left = total ? (sound.at / total) * 100 : 0;
      const width = total ? ((sound.seconds || 0.2) / total) * 100 : 8;
      block.style.left = left + "%";
      block.style.width = Math.max(width, 2) + "%";
      block.append(h("span", { text: sound.name || "Sound" }));
      const inEdge = edgeHandle("in");
      const outEdge = edgeHandle("out");
      bindSoundTrim(inEdge, sound, "in", block, alane, total);
      bindSoundTrim(outEdge, sound, "out", block, alane, total);
      block.append(
        inEdge,
        outEdge,
        editX("Remove sound", () => {
          soundSel = "";
          call("edit_delete_sound", { id: sound.id });
        }),
      );
      block.addEventListener("pointerdown", (event) => {
        if (event.target.closest(".gb-edit-edge, .gb-edit-x")) return;
        dragSound(event, sound, total, alane);
      });
      block.addEventListener("click", (event) => {
        if (event.target.closest(".gb-edit-edge, .gb-edit-x")) return;
        event.stopPropagation();
        if (ignoreClick) return;
        soundSel = sound.id;
        editSel = "";
        render();
      });
      alane.append(block);
    });
    if (!sounds.length && !speakOpen) {
      alane.append(h("div", { class: "gb-edit-ahint", text: "Double-click to speak. Drop a sound." }));
    }
    if (speakOpen) alane.append(speakInline(total));
    alane.addEventListener("dragenter", (event) => {
      if ([].slice.call(event.dataTransfer.types).indexOf("Files") >= 0) alane.classList.add("is-hot");
    });
    alane.addEventListener("dragleave", (event) => {
      if (!alane.contains(event.relatedTarget)) alane.classList.remove("is-hot");
    });
    alane.addEventListener("dragover", (event) => {
      const types = [].slice.call(event.dataTransfer.types);
      if (types.indexOf("Files") >= 0 || types.indexOf("application/x-glass") >= 0 || types.indexOf("text/plain") >= 0) {
        event.preventDefault();
      }
    });
    alane.addEventListener("drop", (event) => {
      alane.classList.remove("is-hot");
      if (event.dataTransfer.files && event.dataTransfer.files.length) {
        event.preventDefault();
        event.stopPropagation();
        const file = event.dataTransfer.files[0];
        const named = (file && file.name || "").toLowerCase();
        const audio = named.endsWith(".m4a") || named.endsWith(".mp3") || named.endsWith(".wav") || named.endsWith(".aac") || named.endsWith(".ogg") || (file.type || "").indexOf("audio/") === 0;
        if (audio) addSoundFile(event.dataTransfer.files);
        return;
      }
      const data = readDrag(event);
      if (!data || data.kind !== "audio") return;
      event.preventDefault();
      event.stopPropagation();
      call("edit_sound", { asset_id: data.id, at: timeOn(alane, event, total) });
    });
    alane.addEventListener("dblclick", (event) => {
      if (event.target.closest(".gb-edit-sound, .gb-speak-inline, .gb-edit-edge, .gb-edit-x")) return;
      event.preventDefault();
      event.stopPropagation();
      editPlaying = false;
      if (editVideo) editVideo.pause();
      speakAt = timeOn(alane, event, total);
      editTime = speakAt;
      speakOpen = true;
      speakMenu = false;
      speakFocus = true;
      seekEdit(speakAt);
      render();
    });
    const stack = h("div", { class: "gb-edit-stack" }, [
      ruler,
      lane,
      h("div", { class: "gb-edit-lane-k", text: "Sound" }),
      alane,
      head,
    ]);
    bindScrub(stack, total);
    const sub = picked
      ? "Clip " + (pickedIndex + 1) + " · " + (picked.seconds || 0).toFixed(1) + "s"
      : (clips.length ? clips.length + " clips" : "No take on the cut");
    return h("div", { class: "gb-edit" }, [
      h("div", { class: "gb-edit-headrow" }, [
        h("div", { class: "gb-kicker", text: "Edit" }),
        h("div", { class: "gb-edit-sub", text: sub }),
        h("span", { class: "gb-edit-tc", text: editStamp(editTime) }),
        h("span", { class: "gb-edit-spacer" }),
        h("button", {
          class: "gb-go",
          type: "button",
          text: clips.length > 1 ? "Save pieces" : "Save take",
          onclick: () => call("edit_keep", editSel ? { id: editSel } : {}),
        }),
      ]),
      stage,
      h("div", { class: "gb-edit-tl" }, [
        h("div", { class: "gb-edit-lane-k", text: "Picture" }),
        stack,
      ]),
      ensureEditAudio(),
    ]);
  }

  function addSoundFile(files) {
    const file = files && files[0];
    if (!file || !state) return;
    const before = new Set((state.library || []).map((asset) => asset.id));
    const at = editTime;
    const body = new FormData();
    body.append("rally", window.GB_RALLY);
    body.append("file", file);
    fetch("/api/glass/upload", { method: "POST", body: body })
      .then((res) => res.json())
      .then((data) => {
        if (!data || !data.title) {
          if (state) {
            state.error = (data && data.error) || "Upload failed";
            render();
          }
          return;
        }
        apply(data);
        const added = (data.library || []).find((asset) => asset.kind === "audio" && !before.has(asset.id));
        if (added) call("edit_sound", { asset_id: added.id, at: at });
      })
      .catch(() => {});
  }

  function dragSound(event, sound, total, lane) {
    if (event.button !== 0) return;
    if (event.target.closest(".gb-edit-edge, .gb-edit-x")) return;
    event.stopPropagation();
    soundSel = sound.id;
    const origin = event.clientX;
    const at0 = sound.at;
    let moved = false;
    function move(ev) {
      if (Math.abs(ev.clientX - origin) < 3) return;
      moved = true;
      const rect = lane.getBoundingClientRect();
      const at = Math.max(0, at0 + ((ev.clientX - origin) / Math.max(1, rect.width)) * total);
      const block = lane.querySelector(".gb-edit-sound.is-on") || event.currentTarget;
      if (block && block.style) block.style.left = (total ? (at / total) * 100 : 0) + "%";
      sound.at = at;
    }
    function up() {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (moved) call("edit_move_sound", { id: sound.id, at: sound.at });
    }
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }

  function openRedo() {
    if (!modal || !state) return;
    const assetId = watchedAsset();
    if (!assetId) return;
    const video = document.querySelector(".gb-viewer-video");
    const raw = video && isFinite(video.duration) ? Math.round(video.duration) : (state.duration || 6);
    let length = Math.max(1, Math.min(15, raw || 6));
    let hold = "both";
    const choices = h("div", { class: "gb-hold" });
    ["first", "last", "both", "none"].forEach((name) => {
      const button = h("button", {
        class: "gb-hold-btn" + (name === hold ? " is-on" : ""),
        type: "button",
        text: name === "none" ? "None" : name[0].toUpperCase() + name.slice(1),
      });
      button.addEventListener("click", () => {
        hold = name;
        choices.querySelectorAll(".gb-hold-btn").forEach((node) => node.classList.remove("is-on"));
        button.classList.add("is-on");
      });
      choices.append(button);
    });
    const val = h("span", { class: "gb-dur-val", text: length + "s" });
    const slider = h("input", { class: "gb-slider", type: "range", min: "1", max: "15", step: "1", value: String(length) });
    slider.addEventListener("input", () => {
      length = Number(slider.value);
      val.textContent = length + "s";
    });
    const field = h("textarea", {
      class: "gb-still-prompt",
      placeholder: "What should this clip become?",
    });
    const sheet = h("div", { class: "gb-sheet" }, [
      h("div", { class: "gb-sheet-k", text: "Regenerate" }),
      h("div", {
        class: "gb-redo-note",
        text: "This makes a new take from the one you are watching. Cut a piece and save it first when you only want the middle.",
      }),
      h("div", { class: "gb-sheet-sub", text: "Hold frames" }),
      choices,
      h("div", { class: "gb-dur" }, [slider, val]),
      field,
      h("button", {
        class: "gb-go",
        type: "button",
        text: "Make",
        onclick: () => {
          const prompt = field.value.trim();
          if (!prompt) { field.focus(); return; }
          modal.hidden = true;
          syncPanels();
          call("generate_redo", { asset_id: assetId, prompt: prompt, hold: hold, duration: length });
        },
      }),
    ]);
    sheet.addEventListener("click", (event) => event.stopPropagation());
    modal.replaceChildren(sheet);
    modal.hidden = false;
    syncPanels();
    field.focus();
  }

  function markEditSelection() {
    document.querySelectorAll(".gb-edit-clip").forEach((node) => node.classList.remove("is-on"));
    const clips = (state && state.edit && state.edit.clips) || [];
    const index = clips.findIndex((clip) => clip.id === editSel);
    const nodes = document.querySelectorAll(".gb-edit-clip");
    if (index >= 0 && nodes[index]) nodes[index].classList.add("is-on");
  }

  function renderLibrary() {
    const input = h("input", { type: "file", accept: "image/*,video/*,audio/*", multiple: true, hidden: true });
    input.addEventListener("change", () => upload(input.files));
    const list = h("div", { class: "gb-lib-list" });
    (state.library || []).forEach((asset) => {
      const item = h("div", {
        class: "gb-asset" + (asset.id === state.flash ? " is-flash" : ""),
        draggable: "true",
        title: asset.name,
      });
      item.addEventListener("dragstart", (event) => {
        item.dataset.dragged = "1";
        setDrag(event, { from: "library", id: asset.id, kind: asset.kind });
      });
      item.addEventListener("dragend", () => {
        setTimeout(() => { delete item.dataset.dragged; }, 0);
      });
      item.addEventListener("click", (event) => {
        if (item.dataset.dragged) return;
        if (asset.kind === "audio") {
        event.stopPropagation();
        if (!editOpen) {
          state.error = "Open the cut, then click the sound";
          render();
          return;
        }
        libraryOpen = false;
        call("edit_sound", { asset_id: asset.id, at: editTime });
        return;
      }
      if (asset.kind !== "still") return;
        event.stopPropagation();
        const line = selected && selected.kind === "clip"
          ? (state.clips || []).find((clip) => clip.id === selected.id)
          : null;
        if (!line) {
          state.error = "Select a line, then click the still";
          render();
          return;
        }
        libraryOpen = false;
        call("add_pin", {
          asset_id: asset.id,
          at: (line.start + line.end) / 2,
          clip_id: line.id,
        });
      });
      if (asset.kind === "audio") {
        item.append(h("div", { class: "gb-audio" }));
      } else {
        const media = asset.kind === "video"
          ? h("video", { src: asset.url + "#t=0.1", muted: true, playsinline: true, preload: "metadata" })
          : h("img", { src: asset.url, alt: asset.name });
        media.draggable = false;
        item.append(media);
      }
      item.append(h("div", { class: "gb-asset-name", text: asset.name }));
      list.append(item);
    });
    if (!state.library.length) list.append(h("div", { class: "gb-hint", text: "Drop a file here" }));
    const panel = h("div", { class: "gb-pop gb-lib-pop gb-lib-dock" }, [
      h("div", { class: "gb-pop-head" }, [
        h("div", { class: "gb-pop-k", text: "Library" }),
        h("button", { class: "gb-folder", type: "button", text: "Add files", onclick: (event) => {
          event.stopPropagation();
          input.click();
        } }),
        h("button", {
          class: "gb-viewer-x",
          type: "button",
          text: "Close",
          onclick: (event) => {
            event.stopPropagation();
            libraryOpen = false;
            render();
          },
        }),
      ]),
      h("div", {
        class: "gb-lib-note",
        text: editOpen
          ? "Click a sound to place it at the playhead. Click a still to pin it to the selected line."
          : "Click a line, then a still. Or drop the still on the line.",
      }),
      input,
      list,
    ]);
    panel.addEventListener("dragover", (event) => {
      if ([].slice.call(event.dataTransfer.types).indexOf("Files") >= 0) event.preventDefault();
    });
    panel.addEventListener("drop", (event) => {
      if (!event.dataTransfer.files || !event.dataTransfer.files.length) return;
      event.preventDefault();
      upload(event.dataTransfer.files);
    });
    return panel;
  }

  function upload(files) {
    const body = new FormData();
    body.append("rally", window.GB_RALLY);
    [].slice.call(files || []).forEach((file) => body.append("file", file));
    fetch("/api/glass/upload", { method: "POST", body: body })
      .then((res) => res.json())
      .then((data) => {
        if (data && data.title) apply(data);
        else if (state) {
          state.error = (data && data.error) || "Upload failed";
          render();
        }
      })
      .catch(() => {});
  }

  function clockWidth() {
    const stage = document.getElementById("gb-stage");
    const stageW = stage && stage.clientWidth ? stage.clientWidth : 1440;
    const dock = libraryOpen ? 460 : 0;
    return Math.max(640, stageW - 120 - dock);
  }

  function stillMetrics() {
    const parts = String(state.aspect || "16:9").split(":");
    const aw = Number(parts[0]) || 16;
    const ah = Number(parts[1]) || 9;
    const count = Math.max(state.stills.length, 1);
    const slot = clockWidth() / count;
    let cardW = Math.max(96, slot - 28);
    let cardH = cardW * ah / aw;
    const cap = ah > aw ? 440 : ah === aw ? 400 : 360;
    if (cardH > cap) {
      cardH = cap;
      cardW = cardH * aw / ah;
    }
    if (cardW > slot - 16) {
      cardW = Math.max(96, slot - 16);
      cardH = cardW * ah / aw;
    }
    return { cardW: Math.round(cardW), cardH: Math.round(cardH) };
  }

  function renderMain() {
    const n = state.stills.length;
    const metrics = stillMetrics();
    const cardW = metrics.cardW;
    const cardH = metrics.cardH;
    const rulerH = 28;
    const hasPins = (state.pins || []).length > 0;
    const refH = 68;
    const duration = state.duration || 6;
    const clock = h("div", { class: "gb-clock" });
    clock.style.setProperty("--card-h", cardH + "px");
    clock.style.setProperty("--secs", String(duration));
    const ruler = renderRuler(duration, clockWidth());
    const span = desk || (selected && selected.kind === "clip"
      ? (state.clips || []).find((clip) => clip.id === selected.id)
      : null);
    if (span && span.end > span.start) {
      const mark = h("div", { class: "gb-range" });
      mark.style.left = (span.start * 100) + "%";
      mark.style.width = Math.max(0.8, (span.end - span.start) * 100) + "%";
      ruler.append(mark);
    }
    const refs = h("div", { class: "gb-refband" });
    refs.append(h("div", {
      class: "gb-ref-hint" + (hasPins ? " is-quiet" : ""),
      text: hasPins
        ? "Drop a still on a line"
        : "Drop a still on a line. Or select a line and click one in the library.",
    }));
    const stills = h("div", { class: "gb-stillband" });
    const lanes = h("div", { class: "gb-lanes" });
    const links = document.createElementNS("http://www.w3.org/2000/svg", "svg");
    links.setAttribute("class", "gb-links");
    clock.append(ruler, refs, stills, lanes, links);
    (state.pins || []).forEach((pin) => refs.append(renderPin(pin)));
    if (!n) {
      const ghost = h("div", { class: "gb-ghost" }, [h("span", { text: "Still" })]);
      ghost.style.width = cardW + "px";
      ghost.style.height = cardH + "px";
      ghost.style.left = "50%";
      ghost.style.transform = "translateX(-50%)";
      stills.append(ghost);
    }
    state.stills.forEach((still, index) => stills.append(renderStill(still, index, n, cardW)));
    for (let i = 0; i < n - 1; i += 1) {
      const marker = svg('<svg viewBox="0 0 28 14" width="28" height="14"><line x1="1" y1="7" x2="18" y2="7" stroke="currentColor" stroke-width="1.4" stroke-dasharray="2 3"/><path d="M16 2.5 L24 7 L16 11.5" fill="none" stroke="currentColor" stroke-width="1.4"/></svg>');
      marker.className = "gb-arrow";
      marker.style.left = ((i + 1) / n * 100) + "%";
      stills.append(marker);
    }
    bindStillDrop(stills);
    bindRefDrop(refs);
    lanes.append(renderLane(duration));
    const plus = h("button", {
      class: "gb-plus",
      type: "button",
      title: "New still",
      onclick: openModal,
    }, [
      h("span", { class: "gb-plus-mark", text: "+" }),
      h("span", { class: "gb-plus-k", text: "Still" }),
    ]);
    plus.style.height = cardH + "px";
    plus.style.marginTop = (rulerH + refH + 6) + "px";
    const scale = h("div", { class: "gb-scale" }, [clock, plus]);
    const scroller = h("div", { class: "gb-scroller" }, [scale]);
    return h("div", { class: "gb-main" }, [
      h("div", { class: "gb-kicker", text: "Timeline" }),
      scroller,
    ]);
  }

  function renderRuler(duration, width) {
    const ruler = h("div", { class: "gb-ruler" });
    const px = duration > 0 && width ? width / duration : 48;
    const step = px >= 36 ? 1 : px >= 18 ? 2 : 5;
    for (let s = 0; s <= duration; s += 1) {
      const tick = h("div", { class: "gb-tick" + (s === duration ? " is-end" : "") });
      tick.style.left = (duration ? (s / duration) * 100 : 0) + "%";
      if (s % step === 0 || s === duration) tick.append(h("span", { text: s + "s" }));
      ruler.append(tick);
    }
    return ruler;
  }

  function renderStill(still, index, n, cardW) {
    const slot = h("div", {
      class: "gb-slot" + (selected && selected.kind === "still" && selected.id === still.id ? " is-on" : ""),
      "data-sel": "still:" + still.id,
    });
    slot.style.left = ((index + 0.5) / n * 100) + "%";
    slot.style.width = cardW + "px";
    slot.style.transform = "translateX(-50%)";
    const card = h("div", { class: "gb-still", draggable: "true" });
    card.addEventListener("dragstart", (event) => setDrag(event, { from: "still", id: still.id, kind: "still" }));
    if (still.url) {
      const img = h("img", { src: still.url, alt: still.name });
      img.draggable = false;
      card.append(img);
    } else card.append(h("div", { class: "gb-still-k", text: "Still" }));
    card.append(xBtn("still", still.id, "Remove still"));
    card.addEventListener("click", (event) => {
      if (event.target.closest(".gb-x")) return;
      selected = { kind: "still", id: still.id };
      markSelection();
    });
    slot.append(card, h("div", { class: "gb-still-time", text: fmt(still.t0) + "–" + fmt(still.t1) + "s" }));
    return slot;
  }

  function bindStillDrop(band) {
    band.addEventListener("dragover", (event) => event.preventDefault());
    band.addEventListener("drop", (event) => {
      const data = readDrag(event);
      if (!data) return;
      event.preventDefault();
      event.stopPropagation();
      const index = dropIndex(band, event.clientX);
      if (data.from === "still") {
        const ids = state.stills.map((still) => still.id);
        const from = ids.indexOf(data.id);
        if (from < 0) return;
        ids.splice(from, 1);
        const dest = index > from ? index - 1 : index;
        ids.splice(Math.max(0, dest), 0, data.id);
        call("reorder_stills", { ids: ids });
        return;
      }
      if (data.kind === "video" || data.kind === "audio") return;
      call("add_still", { asset_id: data.id, index: index });
    });
  }

  function dropIndex(band, clientX) {
    let index = 0;
    state.stills.forEach((still, i) => {
      const card = band.querySelector('[data-sel="still:' + still.id + '"]');
      if (!card) return;
      const rect = card.getBoundingClientRect();
      if (clientX > rect.left + rect.width / 2) index = i + 1;
    });
    return index;
  }

  function clipCovering(at) {
    const clips = (state && state.clips) || [];
    const hits = clips.filter((clip) => at >= clip.start - 0.001 && at <= clip.end + 0.001);
    const starting = hits.find((clip) => Math.abs(clip.start - at) <= 0.02);
    if (starting) return starting;
    if (hits.length) return hits[0];
    let best = null;
    let dist = 2;
    clips.forEach((clip) => {
      const mid = (clip.start + clip.end) / 2;
      const gap = Math.abs(mid - at);
      if (gap < dist) {
        dist = gap;
        best = clip;
      }
    });
    return best;
  }

  function attachStill(assetId, at) {
    const clip = clipCovering(at);
    if (!clip) {
      if (state) {
        state.error = "Add a line, then drop the still on it";
        render();
      }
      return;
    }
    libraryOpen = false;
    call("add_pin", { asset_id: assetId, at: at, clip_id: clip.id });
  }

  function bindRefDrop(band) {
    band.addEventListener("dragover", (event) => event.preventDefault());
    band.addEventListener("drop", (event) => {
      const data = readDrag(event);
      if (!data || data.kind === "video" || data.kind === "audio" || data.from === "still") return;
      event.preventDefault();
      const rect = band.getBoundingClientRect();
      const at = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
      attachStill(data.id, at);
    });
  }

  function renderPin(pin) {
    const node = h("div", {
      class: "gb-pin" + (selected && selected.kind === "pin" && selected.id === pin.id ? " is-on" : ""),
      "data-pin": pin.id,
      "data-sel": "pin:" + pin.id,
      title: pin.name,
    });
    node.style.left = (pin.at * 100) + "%";
    if (pin.url) {
      const img = h("img", { src: pin.url, alt: pin.name });
      img.draggable = false;
      node.append(img);
    }
    node.append(xBtn("pin", pin.id, "Remove reference"));
    node.addEventListener("pointerdown", (event) => {
      if (event.button !== 0 || event.target.closest(".gb-x")) return;
      const band = node.parentElement;
      const origin = event.clientX;
      const at0 = pin.at;
      let moved = false;
      node.setPointerCapture(event.pointerId);
      function move(ev) {
        if (Math.abs(ev.clientX - origin) < 3) return;
        if (!moved) { moved = true; dragging += 1; }
        const rect = band.getBoundingClientRect();
        pin.at = Math.max(0, Math.min(1, at0 + (ev.clientX - origin) / rect.width));
        node.style.left = (pin.at * 100) + "%";
        drawLinks();
      }
      let ended = false;
      function up() {
        if (ended) return;
        ended = true;
        node.removeEventListener("pointermove", move);
        node.removeEventListener("pointerup", up);
        node.removeEventListener("pointercancel", up);
        if (moved) {
          dragging -= 1;
          call("patch_pin", { id: pin.id, at: pin.at });
        } else {
          selected = { kind: "pin", id: pin.id };
          markSelection();
        }
      }
      node.addEventListener("pointermove", move);
      node.addEventListener("pointerup", up);
      node.addEventListener("pointercancel", up);
    });
    return node;
  }

  function splitClient(text) {
    const voice = /^\s*voice(?:\s+([A-Za-z]+))?\s*:\s*([\s\S]*)$/i.exec(text || "");
    if (voice) {
      const token = (voice[1] || "").toLowerCase();
      if (token && LINE_VOICES.indexOf(token) < 0) return null;
      return { kind: "voice", voice: token || "eve", body: voice[2].trim() };
    }
    const sound = /^\s*sound\s*:\s*([\s\S]*)$/i.exec(text || "");
    if (sound) return { kind: "sound", voice: "eve", body: sound[1].trim() };
    return null;
  }

  function lineView(clip) {
    const parsed = splitClient(clip.text || "");
    if (parsed) return { kind: parsed.kind, voice: parsed.voice, text: parsed.body };
    return {
      kind: clip.kind || "scene",
      voice: LINE_VOICES.indexOf(clip.voice) >= 0 ? clip.voice : "eve",
      text: String(clip.text || ""),
    };
  }

  function soundExists(exceptId) {
    return (state.clips || []).some((clip) => clip.id !== exceptId && lineView(clip).kind === "sound");
  }

  function spanFor(kind, anchor) {
    if (kind === "sound" && !soundExists(desk && desk.id)) return { start: 0, end: 1 };
    if (kind === "voice") {
      const width = 0.16;
      const mid = (anchor.start + anchor.end) / 2;
      const start = Math.max(0, Math.min(1 - width, mid - width / 2));
      return { start: start, end: Math.min(1, start + width) };
    }
    return { start: anchor.start, end: anchor.end };
  }

  function payloadFromDesk(kindOverride) {
    let kind = desk.kind;
    let voice = desk.voice;
    let text = desk.text || "";
    const parsed = splitClient(text);
    if (parsed) {
      kind = parsed.kind;
      voice = parsed.voice;
      text = parsed.body;
    }
    if (kindOverride) kind = kindOverride;
    if (LINE_VOICES.indexOf(voice) < 0) voice = "eve";
    return { kind: kind, voice: voice, text: String(text).trim(), start: desk.start, end: desk.end };
  }

  const LINE_PLACE = {
    scene: "What happens",
    voice: "What they say",
    sound: "What you hear",
  };

  function openDesk(clip, focus) {
    const view = lineView(clip);
    deskOpenedAt = Date.now();
    selected = { kind: "clip", id: clip.id };
    desk = {
      id: clip.id,
      kind: view.kind,
      voice: view.voice,
      text: view.text,
      dirty: false,
      focus: !!focus,
      menu: false,
      start: clip.start,
      end: clip.end,
      anchor: { start: clip.start, end: clip.end },
    };
    render();
  }

  function openNew(x, kind) {
    if (desk && desk.id && desk.dirty) commitEdit(true);
    else if (desk && !desk.id && (desk.text || "").trim()) commitNew();
    const width = 0.28;
    const start = Math.max(0, Math.min(1 - width, x - width / 2));
    const anchor = { start: start, end: Math.min(1, start + width) };
    const span = spanFor(kind || "scene", anchor);
    deskOpenedAt = Date.now();
    selected = null;
    desk = {
      id: null,
      kind: kind || "scene",
      voice: "eve",
      text: "",
      dirty: false,
      focus: true,
      menu: false,
      start: span.start,
      end: span.end,
      anchor: anchor,
    };
    render();
  }

  function commitNew() {
    const payload = payloadFromDesk();
    if (!payload.text) {
      desk = null;
      render();
      return;
    }
    armEdit = new Set((state.clips || []).map((clip) => clip.id));
    desk = null;
    call("add_clip", payload);
  }

  function commitEdit(close) {
    if (!desk || !desk.id) return;
    const id = desk.id;
    const payload = payloadFromDesk();
    if (close) desk = null;
    else {
      desk.dirty = false;
      desk.text = payload.text;
      desk.kind = payload.kind;
      desk.voice = payload.voice;
      desk.focus = false;
    }
    call("patch_clip", { id: id, kind: payload.kind, voice: payload.voice, text: payload.text });
  }

  function settleDesk() {
    if (!desk) return;
    if (!desk.id) {
      if ((desk.text || "").trim()) commitNew();
      else {
        desk = null;
        render();
      }
      return;
    }
    if (desk.dirty) commitEdit(true);
    else {
      desk = null;
      render();
    }
  }

  function retarget(clip, kind) {
    const view = lineView(clip);
    let start = clip.start;
    let end = clip.end;
    if (kind === "sound" && !soundExists(clip.id)) {
      start = 0;
      end = 1;
    }
    call("patch_clip", {
      id: clip.id,
      kind: kind,
      voice: view.voice,
      text: view.text,
      start: start,
      end: end,
    });
  }

  function chooseVoice(name) {
    if (!desk) return;
    const keep = !!(document.activeElement && document.activeElement.classList.contains("gb-line-field"));
    desk.kind = "voice";
    desk.voice = name;
    desk.focus = keep || !desk.id;
    desk.menu = false;
    if (!desk.id) {
      const span = spanFor("voice", desk.anchor);
      desk.start = span.start;
      desk.end = span.end;
      render();
      return;
    }
    const id = desk.id;
    const payload = payloadFromDesk("voice");
    payload.voice = name;
    desk.text = payload.text;
    const clip = (state.clips || []).find((row) => row.id === id);
    if (clip) {
      clip.kind = "voice";
      clip.voice = name;
      clip.text = payload.text;
    }
    render();
    call("patch_clip", { id: id, kind: "voice", voice: name, text: payload.text });
  }

  function lineField() {
    const field = h("input", {
      class: "gb-line-field",
      type: "text",
      placeholder: LINE_PLACE[desk.kind] || LINE_PLACE.scene,
      spellcheck: "true",
    });
    field.value = desk.text || "";
    field.addEventListener("pointerdown", (event) => event.stopPropagation());
    field.addEventListener("click", (event) => event.stopPropagation());
    field.addEventListener("input", () => {
      const parsed = splitClient(field.value);
      if (parsed) {
        desk.kind = parsed.kind;
        desk.voice = parsed.voice;
        desk.text = parsed.body;
        desk.dirty = true;
        desk.focus = true;
        if (!desk.id) {
          const span = spanFor(parsed.kind, desk.anchor);
          desk.start = span.start;
          desk.end = span.end;
        }
        render();
        return;
      }
      desk.text = field.value;
      desk.dirty = true;
    });
    field.addEventListener("keydown", (event) => {
      if (event.key === "Escape") {
        event.preventDefault();
        event.stopPropagation();
        desk = null;
        render();
        return;
      }
      if (event.key === "Enter") {
        event.preventDefault();
        event.stopPropagation();
        desk.text = field.value;
        if (desk.id) commitEdit(true);
        else commitNew();
      }
    });
    return field;
  }

  function renderVoiceMenu(row) {
    const box = h("div", { class: "gb-voicebox" });
    box.style.top = (4 + row * 46) + "px";
    if (desk.end > 0.72) {
      box.style.right = ((1 - desk.start) * 100) + "%";
      box.style.marginRight = "8px";
    } else {
      box.style.left = (desk.end * 100) + "%";
      box.style.marginLeft = "8px";
    }
    LINE_VOICES.forEach((name) => {
      box.append(h("button", {
        class: "gb-kind is-voice" + (desk.voice === name ? " is-on" : ""),
        type: "button",
        text: name,
        onclick: (event) => {
          event.stopPropagation();
          chooseVoice(name);
        },
      }));
    });
    box.addEventListener("mousedown", (event) => event.preventDefault());
    box.addEventListener("click", (event) => event.stopPropagation());
    return box;
  }

  function renderDraft(row) {
    const node = h("div", { class: "gb-clip is-draft is-" + desk.kind });
    placeClip(node, desk, row);
    if (desk.kind === "voice") node.append(voiceChip(null));
    node.append(lineField());
    node.addEventListener("mousedown", (event) => {
      if (event.target.closest("input, button")) return;
      event.preventDefault();
    });
    node.addEventListener("click", (event) => event.stopPropagation());
    return node;
  }

  function renderLane(duration) {
    const clips = state.clips || [];
    const wrap = h("div", { class: "gb-tracks" });
    ["scene", "voice", "sound"].forEach((kind) => {
      const mine = clips.filter((clip) => lineView(clip).kind === kind);
      const writing = !!(desk && !desk.id && desk.kind === kind);
      const packed = pack(writing
        ? mine.concat([{ id: "__draft__", start: desk.start, end: desk.end }])
        : mine);
      const ghostRow = writing ? packed.map.__draft__ : -1;
      const rows = Math.max(1, packed.count);
      const lane = h("div", { class: "gb-track-lane", "data-kind": kind });
      lane.style.minHeight = Math.max(48, rows * 46 + 8) + "px";
      lane.style.setProperty("--secs", String(duration || 6));
      if (!mine.length && !writing) {
        lane.append(h("div", {
          class: "gb-lane-hint",
          text: "Click to write " + (LINE_PLACE[kind] || "a line").toLowerCase(),
        }));
      }
      mine.forEach((clip) => lane.append(renderClip(clip, packed.map[clip.id] || 0)));
      if (writing) lane.append(renderDraft(ghostRow));
      if (desk && desk.menu && desk.kind === "voice" && kind === "voice") {
        const row = desk.id ? (packed.map[desk.id] || 0) : Math.max(0, ghostRow);
        lane.append(renderVoiceMenu(row));
      }
      lane.addEventListener("click", (event) => {
        if (Date.now() - deskOpenedAt < 280) return;
        if (event.target.closest(".gb-clip, .gb-line-field, .gb-voicebox")) return;
        const rect = lane.getBoundingClientRect();
        const x = (event.clientX - rect.left) / Math.max(1, rect.width);
        openNew(x, kind);
      });
      lane.addEventListener("dragover", (event) => {
        event.preventDefault();
        lane.classList.add("is-hot");
      });
      lane.addEventListener("dragleave", () => lane.classList.remove("is-hot"));
      lane.addEventListener("drop", (event) => {
        lane.classList.remove("is-hot");
        if (event.target.closest(".gb-clip")) return;
        const data = readDrag(event);
        if (!data || data.kind === "video" || data.kind === "audio" || data.from === "still") return;
        event.preventDefault();
        event.stopPropagation();
        const rect = lane.getBoundingClientRect();
        const at = Math.max(0, Math.min(1, (event.clientX - rect.left) / Math.max(1, rect.width)));
        attachStill(data.id, at);
      });
      const label = h("div", {
        class: "gb-track-k" + (desk && desk.kind === kind ? " is-on" : ""),
        text: kind,
      });
      wrap.append(h("div", { class: "gb-track is-" + kind }, [label, lane]));
    });
    return wrap;
  }

  function pack(clips) {
    const rows = [];
    const map = {};
    clips.forEach((clip) => {
      let row = rows.findIndex((list) => list.every((other) => other.end <= clip.start + 0.001 || other.start >= clip.end - 0.001));
      if (row < 0) {
        row = rows.length;
        rows.push([]);
      }
      rows[row].push(clip);
      map[clip.id] = row;
    });
    return { count: Math.max(1, rows.length), map: map };
  }

  function voiceChip(clip) {
    const name = clip ? lineView(clip).voice : (desk && desk.voice) || "eve";
    const chip = h("button", { class: "gb-vchip", type: "button", text: name, title: "Voice" });
    chip.addEventListener("pointerdown", (event) => event.stopPropagation());
    chip.addEventListener("click", (event) => {
      event.stopPropagation();
      if (clip) {
        const same = desk && desk.id === clip.id;
        if (!same) openDesk(clip, false);
        desk.menu = !(same && desk.menu);
        desk.focus = !!(document.activeElement && document.activeElement.classList.contains("gb-line-field"));
        render();
        return;
      }
      if (!desk) return;
      desk.menu = !desk.menu;
      desk.focus = true;
      render();
    });
    return chip;
  }

  function renderClip(clip, row) {
    const on = selected && selected.kind === "clip" && selected.id === clip.id;
    const view = lineView(clip);
    const shown = view.text.trim();
    const editingLine = !!(desk && desk.id === clip.id && desk.focus);
    const node = h("div", {
      class: "gb-clip is-" + view.kind + (on ? " is-on" : ""),
      "data-sel": "clip:" + clip.id,
      title: shown,
    });
    placeClip(node, clip, row);
    node.append(h("span", { class: "gb-edge is-l" }), h("span", { class: "gb-edge is-r" }));
    const thumbs = h("div", { class: "gb-clip-refs" });
    (clip.refs || []).forEach((ref) => thumbs.append(refChip(clip, ref)));
    const kids = [thumbs];
    if (view.kind === "voice") kids.push(voiceChip(clip));
    if (editingLine) kids.push(lineField());
    else kids.push(h("div", { class: "gb-clip-text" + (shown ? "" : " is-empty"), text: shown || "Write a line" }));
    kids.push(
      h("div", { class: "gb-clip-time", text: fmt(clip.t0) + "–" + fmt(clip.t1) }),
      xBtn("clip", clip.id, "Remove line"),
    );
    node.append.apply(node, kids);
    node.querySelectorAll(".gb-edge").forEach((edge) => bindEdge(edge, node, clip, row));
    node.addEventListener("pointerdown", (event) => bindMove(event, node, clip, row));
    node.addEventListener("click", (event) => {
      if (event.target.closest(".gb-edge, .gb-x, .gb-refx, .gb-refchip, .gb-vchip, .gb-line-field")) return;
      event.stopPropagation();
    });
    node.addEventListener("dblclick", (event) => {
      if (event.target.closest(".gb-edge, .gb-x, .gb-refx")) return;
      event.stopPropagation();
      openDesk(clip, true);
    });
    node.addEventListener("dragover", (event) => {
      event.preventDefault();
      node.classList.add("is-drop");
    });
    node.addEventListener("dragleave", () => node.classList.remove("is-drop"));
    node.addEventListener("drop", (event) => {
      node.classList.remove("is-drop");
      const data = readDrag(event);
      if (!data || data.kind === "video" || data.kind === "audio") return;
      event.preventDefault();
      event.stopPropagation();
      libraryOpen = false;
      call("add_pin", {
        asset_id: data.id,
        at: (clip.start + clip.end) / 2,
        clip_id: clip.id,
        also_ref: true,
      });
    });
    return node;
  }

  function bindMove(event, node, clip, row) {
    if (event.button !== 0) return;
    if (event.target.closest(".gb-edge, .gb-x, .gb-refx, .gb-refchip, .gb-vchip, .gb-line-field")) return;
    const origin = event.clientX;
    const originY = event.clientY;
    const start0 = clip.start;
    const width = clip.end - clip.start;
    let moved = false;
    node.setPointerCapture(event.pointerId);
    function move(ev) {
      if (Math.abs(ev.clientX - origin) < 4 && Math.abs(ev.clientY - originY) < 4) return;
      if (!moved) { moved = true; dragging += 1; }
      const rect = node.parentElement.getBoundingClientRect();
      const next = Math.max(0, Math.min(1 - width, start0 + (ev.clientX - origin) / rect.width));
      clip.start = next;
      clip.end = next + width;
      placeClip(node, clip, row);
      const time = node.querySelector(".gb-clip-time");
      if (time) time.textContent = liveRange(clip.start, clip.end);
      drawLinks();
      document.querySelectorAll(".gb-track-lane").forEach((lane) => lane.classList.remove("is-hot"));
      if (Math.abs(ev.clientY - originY) > 28) {
        const under = document.elementFromPoint(ev.clientX, ev.clientY);
        const track = under && under.closest(".gb-track-lane");
        if (track && track.dataset.kind !== lineView(clip).kind) track.classList.add("is-hot");
      }
    }
    let ended = false;
    function up(ev) {
      if (ended) return;
      ended = true;
      node.removeEventListener("pointermove", move);
      node.removeEventListener("pointerup", up);
      node.removeEventListener("pointercancel", up);
      document.querySelectorAll(".gb-track-lane").forEach((lane) => lane.classList.remove("is-hot"));
      if (moved) {
        dragging -= 1;
        const point = ev || event;
        const dy = Math.abs(point.clientY - originY);
        const under = document.elementFromPoint(point.clientX, point.clientY);
        const track = under && under.closest(".gb-track-lane");
        const nextKind = track && track.dataset.kind;
        if (dy > 28 && nextKind && nextKind !== lineView(clip).kind) retarget(clip, nextKind);
        else call("patch_clip", { id: clip.id, start: clip.start, end: clip.end });
      } else {
        openDesk(clip, true);
      }
    }
    node.addEventListener("pointermove", move);
    node.addEventListener("pointerup", up);
    node.addEventListener("pointercancel", up);
  }

  function bindEdge(edge, node, clip, row) {
    edge.addEventListener("pointerdown", (event) => {
      if (event.button !== 0) return;
      event.stopPropagation();
      const right = edge.classList.contains("is-r");
      const origin = event.clientX;
      const start0 = clip.start;
      const end0 = clip.end;
      dragging += 1;
      edge.setPointerCapture(event.pointerId);
      function move(ev) {
        const rect = node.parentElement.getBoundingClientRect();
        const dx = (ev.clientX - origin) / rect.width;
        if (right) clip.end = Math.max(clip.start + 0.04, Math.min(1, end0 + dx));
        else clip.start = Math.min(clip.end - 0.04, Math.max(0, start0 + dx));
        placeClip(node, clip, row);
        const time = node.querySelector(".gb-clip-time");
        if (time) time.textContent = liveRange(clip.start, clip.end);
        drawLinks();
      }
      let ended = false;
      function up() {
        if (ended) return;
        ended = true;
        edge.removeEventListener("pointermove", move);
        edge.removeEventListener("pointerup", up);
        edge.removeEventListener("pointercancel", up);
        dragging -= 1;
        call("patch_clip", { id: clip.id, start: clip.start, end: clip.end });
      }
      edge.addEventListener("pointermove", move);
      edge.addEventListener("pointerup", up);
      edge.addEventListener("pointercancel", up);
    });
  }

  function placeClip(node, clip, row) {
    node.style.left = (clip.start * 100) + "%";
    node.style.width = ((clip.end - clip.start) * 100) + "%";
    node.style.top = (4 + row * 46) + "px";
    node.style.height = "38px";
  }

  function drawLinks() {
    const track = document.querySelector(".gb-clock");
    const svgEl = document.querySelector(".gb-links");
    if (!track || !svgEl || !state) return;
    const rect = track.getBoundingClientRect();
    svgEl.setAttribute("viewBox", "0 0 " + rect.width + " " + rect.height);
    svgEl.setAttribute("width", String(rect.width));
    svgEl.setAttribute("height", String(rect.height));
    while (svgEl.firstChild) svgEl.removeChild(svgEl.firstChild);
    (state.pins || []).forEach((pin) => {
      const pinEl = track.querySelector('[data-pin="' + pin.id + '"]');
      if (!pinEl) return;
      const pinRect = pinEl.getBoundingClientRect();
      const x1 = pinRect.left + pinRect.width / 2 - rect.left;
      const y1 = pinRect.bottom - rect.top + 3;
      let x2 = x1;
      let y2 = y1 + 40;
      if (pin.clip_id) {
        const clipEl = track.querySelector('[data-sel="clip:' + pin.clip_id + '"]');
        if (clipEl) {
          const clipRect = clipEl.getBoundingClientRect();
          const localLeft = clipRect.left - rect.left;
          const localRight = clipRect.right - rect.left;
          x2 = Math.min(localRight - 12, Math.max(localLeft + 12, x1));
          y2 = clipRect.top - rect.top - 3;
        }
      } else {
        const band = track.querySelector(".gb-stillband");
        if (band) y2 = band.getBoundingClientRect().top - rect.top + 8;
      }
      const path = document.createElementNS("http://www.w3.org/2000/svg", "path");
      const mid = (y1 + y2) / 2;
      path.setAttribute("d", "M " + x1 + " " + y1 + " C " + x1 + " " + mid + ", " + x2 + " " + mid + ", " + x2 + " " + y2);
      path.setAttribute("fill", "none");
      path.setAttribute("stroke", "#e6e5e3");
      path.setAttribute("stroke-width", "1.5");
      path.setAttribute("stroke-linecap", "round");
      path.setAttribute("stroke-dasharray", "0.5 6");
      svgEl.appendChild(path);
    });
  }

  function openModal() {
    if (!modal || !state) return;
    picked = new Set();
    const images = (state.library || []).filter((asset) => asset.kind === "still");
    const grid = h("div", { class: "gb-refpicks" });
    if (!images.length) grid.append(h("div", { class: "gb-hint", text: "Add references from the library" }));
    images.forEach((asset) => {
      const cell = h("button", { class: "gb-refpick", type: "button", title: asset.name });
      const img = h("img", { src: asset.url, alt: asset.name });
      img.draggable = false;
      cell.append(img);
      cell.addEventListener("click", () => {
        if (picked.has(asset.id)) picked.delete(asset.id);
        else picked.add(asset.id);
        cell.classList.toggle("is-on", picked.has(asset.id));
      });
      grid.append(cell);
    });
    const field = h("textarea", { class: "gb-still-prompt", placeholder: "Describe the still" });
    const sheet = h("div", { class: "gb-sheet" }, [
      h("div", { class: "gb-sheet-k", text: "New still" }),
      field,
      h("div", { class: "gb-sheet-sub", text: "References" }),
      grid,
      h("button", {
        class: "gb-go",
        type: "button",
        text: "Generate still",
        onclick: () => {
          const prompt = field.value.trim();
          if (!prompt) { field.focus(); return; }
          modal.hidden = true;
          syncPanels();
          call("generate_still", { prompt: prompt, refs: Array.from(picked) });
        },
      }),
    ]);
    sheet.addEventListener("click", (event) => event.stopPropagation());
    modal.replaceChildren(sheet);
    modal.hidden = false;
    syncPanels();
    field.focus();
  }

  function refChip(clip, ref) {
    const chip = h("span", { class: "gb-refchip", title: ref.name });
    const img = h("img", { src: ref.url, alt: ref.name });
    img.draggable = false;
    const remove = h("button", {
      class: "gb-refx",
      type: "button",
      text: "×",
      title: "Remove " + (ref.name || "reference"),
    });
    remove.addEventListener("pointerdown", (event) => event.stopPropagation());
    remove.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      call("remove", { kind: "ref", id: ref.id, clip_id: clip.id });
    });
    chip.addEventListener("pointerdown", (event) => event.stopPropagation());
    chip.append(img, remove);
    return chip;
  }

  function xBtn(kind, id, title) {
    const button = h("button", {
      class: "gb-x" + (kind === "clip" ? " is-line" : ""),
      type: "button",
      text: "×",
      title: title || "Remove",
    });
    button.addEventListener("pointerdown", (event) => event.stopPropagation());
    button.addEventListener("click", (event) => {
      event.stopPropagation();
      event.preventDefault();
      call("remove", { kind: kind, id: id });
    });
    return button;
  }

  function markSelection() {
    document.querySelectorAll(".gb-seq-item.is-on, .gb-clip.is-on, .gb-slot.is-on, .gb-pin.is-on").forEach((el) => {
      el.classList.remove("is-on");
    });
    if (!selected) return;
    const el = document.querySelector('[data-sel="' + selected.kind + ":" + selected.id + '"]');
    if (el) el.classList.add("is-on");
  }

  function onKey(event) {
    const tag = document.activeElement && document.activeElement.tagName;
    const typing = tag === "INPUT" || tag === "TEXTAREA";
    if (event.key === "Escape") {
      if (modal && !modal.hidden) {
        modal.hidden = true;
        syncPanels();
        return;
      }
      if (libraryOpen || promptOpen || durationOpen || aspectOpen) {
        libraryOpen = promptOpen = durationOpen = aspectOpen = false;
        render();
        return;
      }
      if (desk && !editOpen) {
        desk = null;
        render();
        return;
      }
      if (speakOpen) {
        speakOpen = false;
        speakMenu = false;
        render();
        return;
      }
      if (editOpen) {
        closeEdit();
        return;
      }
      if (viewerEl && !viewerEl.hidden) {
        closeViewer();
        return;
      }
      selected = null;
      markSelection();
      return;
    }
    if (editOpen && !typing) {
      if (event.key === " ") {
        event.preventDefault();
        toggleEditPlay();
        return;
      }
      if (event.key === "ArrowLeft") {
        event.preventDefault();
        stepEdit(-1);
        return;
      }
      if (event.key === "ArrowRight") {
        event.preventDefault();
        stepEdit(1);
        return;
      }
      if ((event.key === "z" || event.key === "Z") && (event.metaKey || event.ctrlKey)) {
        event.preventDefault();
        call("edit_undo");
        return;
      }
      if ((event.key === "c" || event.key === "C") && !event.metaKey && !event.ctrlKey && !event.altKey) {
        event.preventDefault();
        cutHere();
        return;
      }
      if (event.key === "Delete" || event.key === "Backspace") {
        if (soundSel) {
          const id = soundSel;
          soundSel = "";
          event.preventDefault();
          call("edit_delete_sound", { id: id });
          return;
        }
        const clip = selectedEditClip();
        if (!clip) return;
        event.preventDefault();
        call("edit_delete", { id: clip.id });
        return;
      }
      return;
    }
    if (typing || !selected) return;
    if (selected.kind === "sequence" && selected.id === "pending") return;
    event.preventDefault();
    const current = selected;
    selected = null;
    call("remove", { kind: current.kind, id: current.id });
  }

  function setDrag(event, data) {
    const raw = JSON.stringify(data);
    event.dataTransfer.setData("application/x-glass", raw);
    event.dataTransfer.setData("text/plain", raw);
    event.dataTransfer.effectAllowed = data.from === "edit" ? "move" : "copy";
  }

  function readDrag(event) {
    const raw = event.dataTransfer.getData("application/x-glass") || event.dataTransfer.getData("text/plain");
    if (!raw) return null;
    try { return JSON.parse(raw); } catch (error) { return null; }
  }

  function fmt(value) {
    const n = Math.round(Number(value) * 10) / 10;
    return n.toFixed(1);
  }

  function liveRange(start, end) {
    const duration = (state && state.duration) || 6;
    return fmt(start * duration) + "–" + fmt(end * duration);
  }

  window.gbBoot = boot;
  if (window.GB_RALLY) boot();
})();
