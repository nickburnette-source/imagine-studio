"""Step timeline bar and the frame editor overlay."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path

from fastapi import File, Form, Request, UploadFile
from fastapi.responses import JSONResponse
from nicegui import app, ui

from app.config import OUTPUT_DIR
from app.models.rally import RallyPackage, StepKind, clip_label
from app.pipeline.assets import media_url
from app.pipeline.editor_jobs import detail_for_scene
from app.pipeline.scenes import (
    SceneError,
    apply_attempt_edit,
    apply_edit,
    attempt_view,
    bar_items,
    editor_bin,
    editor_snapshot,
    editor_source,
    extract_audio,
    import_timeline_clip,
    join_with_next,
    reorder_scenes,
    select_scene,
    selected_id,
    splice_clip_audio,
    store_audio_upload,
    undo_edit,
)

_REV: dict[str, int] = {}
_REV_NOTE: dict[str, str] = {}


def revision_of(name: str) -> int:
    return _REV.get(name, 0)


def revision_note(name: str) -> str:
    return _REV_NOTE.get(name, "")


def _bump(name: str, note: str = "Scenes saved") -> None:
    _REV[name] = _REV.get(name, 0) + 1
    _REV_NOTE[name] = note


_SAVED_HOOK = None
_NOTICE = None


def set_saved_hook(fn) -> None:
    """Studio reloads the open rally after a cut is written."""
    global _SAVED_HOOK
    _SAVED_HOOK = fn


def set_notice_hook(fn) -> None:
    """Studio activity line for an editor job."""
    global _NOTICE
    _NOTICE = fn


def _notify(kind: str, title: str, **kwargs) -> None:
    fn = _NOTICE
    if fn is None:
        return
    try:
        fn(kind, title, **kwargs)
    except Exception:  # noqa: BLE001
        pass


def _saved(name: str) -> None:
    hook = _SAVED_HOOK
    if hook is None:
        return
    try:
        hook(name)
    except Exception:  # noqa: BLE001
        pass

_ROUTES = False
_CLICK = {"key": "", "at": 0.0}

_JS = r"""
(() => {
  const payload = __PAYLOAD__;
  const cfg = payload.cfg;
  document.getElementById("rb-scene-shade")?.remove();
  document.body.insertAdjacentHTML("beforeend", payload.html);
  const shade = document.getElementById("rb-scene-shade");
  if (shade) shade.dataset.key = cfg.key || "";
  let video = document.getElementById("rb-scene-video");
  let standby = document.getElementById("rb-scene-video-next");
  const lanes = document.getElementById("rb-scene-lanes");
  const lane = document.getElementById("rb-scene-lane");
  const box = document.getElementById("rb-scene-clips");
  const alane = document.getElementById("rb-scene-alane");
  const ruler = document.getElementById("rb-scene-ruler");
  const head = document.getElementById("rb-scene-head");
  if (!shade || !video || !lanes || !lane || !box || !alane || !ruler || !head) return;
  if (standby) {
    standby.classList.add("standby");
    standby.muted = true;
  }

  function silenceBehind() {
    document.querySelectorAll("video, audio").forEach((el) => {
      if (shade.contains(el)) return;
      try {
        el.pause();
        el.autoplay = false;
      } catch (err) {}
    });
  }
  silenceBehind();
  shade.addEventListener("pointerdown", () => silenceBehind(), true);
  shade.addEventListener("click", (e) => e.stopPropagation());

  let FPS = cfg.fps || 30;
  const hintEl = document.getElementById("rb-scene-hint");
  const HINT = hintEl ? hintEl.textContent : "";
  let clips = [];
  let seq = 1;
  let playhead = 0;
  let clipSel = null;
  let dragId = null;
  let seqPlay = false;
  let playClip = null;
  let playWatch = 0;
  let crossTimer = 0;
  let crossWaits = 0;
  let sounds = [];
  let soundSeq = 1;
  let soundSel = null;
  let bin = (cfg.bin || []).slice();
  let copying = false;
  let scrubbing = false;
  let seekToken = 0;
  let ignoreTime = 0;
  let undoStack = [];
  let previewing = "";
  let previewFile = "";
  let highlightFile = "";
  let editingFile = cfg.editing || "";
  let busy = false;
  let saving = false;
  let acting = false;
  let attemptRows = [];
  let seenJob = "";
  let appliedJob = "";
  let knownJobs = {};
  let pollTimer = 0;
  let sheetKind = "";
  let lastSynced = cfg.sceneId || "";
  let selectTimer = 0;
  let selectFlight = Promise.resolve();
  const players = new Map();

  function total() {
    return clips.reduce((n, c) => n + (c.out - c.in), 0);
  }
  function locate(frame) {
    let acc = 0;
    for (let i = 0; i < clips.length; i++) {
      const len = clips[i].out - clips[i].in;
      if (frame < acc + len) {
        return { index: i, clip: clips[i], local: frame - acc, acc, len };
      }
      acc += len;
    }
    return null;
  }
  function clipFps(clip) {
    return (clip && clip.fps) || FPS || 30;
  }
  function totalSeconds() {
    return clips.reduce((n, c) => n + (c.out - c.in) / clipFps(c), 0);
  }
  function secondsAt(frame) {
    let left = Math.max(0, frame);
    let sec = 0;
    for (const c of clips) {
      const len = c.out - c.in;
      const fps = clipFps(c);
      if (left <= len) return sec + left / fps;
      sec += len / fps;
      left -= len;
    }
    return sec;
  }
  function fmt(frame) {
    const whole = secondsAt(frame);
    const sec = Math.floor(whole);
    const hund = Math.min(99, Math.floor((whole - sec) * 100));
    return "0:" + String(sec).padStart(2, "0") + "." + String(hund).padStart(2, "0");
  }
  function clipsFromScenes(rows) {
    let n = 0;
    return (rows || []).map((row) => {
      const frames = Math.max(0, row.frames | 0);
      return {
        id: "c" + (++n),
        sceneId: row.id,
        file: row.file || "",
        url: row.url || "",
        fps: row.fps || cfg.fps || 30,
        frames: frames,
        in: 0,
        out: frames,
        mute: false,
        audio: !!row.audio,
        fresh: !!row.fresh,
        virtual: !!row.virtual,
        pending: !!row.pending,
        detail: row.detail || "",
      };
    });
  }
  function bootTimeline(anchor) {
    if (editingFile) {
      clips = [{
        id: "c1",
        sceneId: cfg.sceneId,
        file: cfg.file,
        url: cfg.src,
        fps: cfg.fps || 30,
        frames: cfg.frames,
        in: 0,
        out: cfg.frames,
        mute: false,
        audio: !!cfg.hasAudio,
        fresh: false,
      }];
      seq = 1;
    } else if ((cfg.scenes || []).length) {
      clips = clipsFromScenes(cfg.scenes);
      seq = Math.max(clips.length, 1);
    } else {
      clips = [{
        id: "c1",
        sceneId: cfg.sceneId,
        file: cfg.file,
        url: cfg.src,
        fps: cfg.fps || 30,
        frames: cfg.frames,
        in: 0,
        out: cfg.frames,
        mute: false,
        audio: !!cfg.hasAudio,
        fresh: false,
      }];
      seq = 1;
    }
    if (clips[0]) FPS = clipFps(clips[0]);
    const wanted = (anchor && anchor.sceneId) || cfg.sceneId;
    const clip = clips.find((c) => c.sceneId === wanted) || clips[0] || null;
    clipSel = clip ? clip.id : null;
    let local = 0;
    if (clip && anchor && anchor.sceneId === clip.sceneId) {
      local = Math.max(0, Math.min((clip.out - clip.in) - 1, anchor.local || 0));
    }
    playhead = clip ? sequenceOf(clip) + local : 0;
    soundSel = null;
    sounds.forEach((sound) => dropPlayer(sound.id));
    sounds = [];
    lastSynced = cfg.sceneId || "";
  }
  function noteSelection() {
    const clip = clips.find((c) => c.id === clipSel);
    if (!clip || editingFile || !clip.sceneId || clip.sceneId === lastSynced) {
      if (!clip) clearTimeout(selectTimer);
      return;
    }
    cfg.sceneId = clip.sceneId;
    (cfg.scenes || []).forEach((row) => { row.on = row.id === clip.sceneId; });
    clearTimeout(selectTimer);
    selectTimer = setTimeout(() => { syncSelection(); }, 120);
  }
  function syncSelection() {
    clearTimeout(selectTimer);
    const clip = clips.find((c) => c.id === clipSel);
    if (!clip || editingFile) return Promise.resolve(true);
    const id = clip.sceneId || "";
    if (!id || id === lastSynced) return selectFlight.then(() => true);
    cfg.sceneId = id;
    (cfg.scenes || []).forEach((row) => { row.on = row.id === id; });
    selectFlight = selectFlight.catch(() => {}).then(async () => {
      const data = await postJson("/api/scene-select", {
        rally: cfg.rally,
        step: cfg.step,
        scene_id: id,
      });
      if (data.sceneId && data.sceneId !== id) throw new Error("That clip is not on the timeline");
      if (cfg.sceneId === id) lastSynced = id;
    });
    return selectFlight.then(() => true).catch((err) => {
      hint((err && err.message) || "Could not select that clip");
      return false;
    });
  }
  function sceneRuns() {
    const runs = [];
    for (const clip of clips) {
      const last = runs[runs.length - 1];
      if (last && last.sceneId === clip.sceneId) last.clips.push(clip);
      else runs.push({ sceneId: clip.sceneId, clips: [clip] });
    }
    return runs;
  }
  function runLength(run) {
    return run.clips.reduce((n, c) => n + (c.out - c.in), 0);
  }
  function runDirty(run) {
    if (run.clips.length !== 1) return true;
    const clip = run.clips[0];
    if (clip.in !== 0 || clip.out !== clip.frames || clip.mute) return true;
    const origin = sequenceOf(clip);
    const end = origin + (clip.out - clip.in);
    return sounds.some((sound) => sound.at < end && sound.at + sound.frames > origin);
  }
  function clearCross() {
    if (!crossTimer) return;
    clearTimeout(crossTimer);
    cancelAnimationFrame(crossTimer);
    crossTimer = 0;
  }
  function stopPlay() {
    seqPlay = false;
    playClip = null;
    playWatch += 1;
    crossWaits = 0;
    clearCross();
    video.pause();
    if (standby) standby.pause();
    const btn = document.getElementById("rb-scene-play");
    if (btn) btn.textContent = "Play";
    syncAudio();
  }
  function hint(text) {
    if (hintEl) hintEl.textContent = text || HINT;
  }
  function playerFor(sound) {
    let el = players.get(sound.id);
    if (!el) {
      el = new Audio(sound.url);
      el.preload = "auto";
      players.set(sound.id, el);
    }
    return el;
  }
  function dropPlayer(id) {
    const el = players.get(id);
    if (!el) return;
    el.pause();
    players.delete(id);
  }
  function soundOffset(sound) {
    return (sound && sound.offset) || 0;
  }
  function soundFps(sound) {
    return (sound && sound.fps) || FPS || 30;
  }
  function soundFilePos(sound, frame) {
    return soundOffset(sound) + (frame - sound.at);
  }
  function splitSoundsAt(frame) {
    const next = [];
    sounds.forEach((sound) => {
      const end = sound.at + sound.frames;
      if (sound.at < frame && end > frame) {
        const leftFrames = frame - sound.at;
        next.push(Object.assign({}, sound, { frames: leftFrames }));
        next.push(Object.assign({}, sound, {
          id: "a" + (++soundSeq),
          at: frame,
          frames: end - frame,
          offset: soundOffset(sound) + leftFrames,
        }));
        return;
      }
      next.push(sound);
    });
    sounds = next;
  }
  function laneMatchesClip(sound, clip, frame) {
    if (!sound.fromFile || sound.fromFile !== clip.file) return false;
    if (frame < sound.at || frame >= sound.at + sound.frames) return false;
    const sourceFrame = clip.in + (frame - sequenceOf(clip));
    const heard = (sound.fromIn || 0) + soundFilePos(sound, frame);
    return Math.abs(heard - sourceFrame) <= 1;
  }
  function videoShouldMute(frame) {
    const loc = locate(frame);
    if (!seqPlay || !loc) return true;
    if (loc.clip.mute) return true;
    return sounds.some((sound) => laneMatchesClip(sound, loc.clip, frame));
  }
  function yankHeld(clip) {
    return sounds.some((sound) => sound.fromFile && sound.fromFile === clip.file);
  }
  function pictureDirty(run) {
    if (run.clips.length !== 1) return true;
    const clip = run.clips[0];
    return clip.in !== 0 || clip.out !== clip.frames;
  }
  function layerIsDuplicate(sound, run) {
    let overlapped = false;
    for (const clip of run.clips) {
      const origin = sequenceOf(clip);
      const length = clip.out - clip.in;
      const start = Math.max(sound.at, origin);
      const end = Math.min(sound.at + sound.frames, origin + length);
      if (end <= start) continue;
      overlapped = true;
      if (clip.mute || !laneMatchesClip(sound, clip, start)) return false;
    }
    return overlapped;
  }
  function sameWave(a, b, frame) {
    if (!a.file || a.file !== b.file) return false;
    const fpsA = soundFps(a);
    const posA = soundFilePos(a, frame) / fpsA;
    const posB = soundFilePos(b, frame) / soundFps(b);
    return Math.abs(posA - posB) < (1.5 / Math.max(fpsA, 1));
  }
  function syncAudio(force) {
    const frame = playhead;
    video.muted = videoShouldMute(frame);
    const heard = [];
    sounds.forEach((sound) => {
      const el = playerFor(sound);
      const inside = seqPlay && frame >= sound.at && frame < sound.at + sound.frames;
      const duplicate = inside && heard.some((other) => sameWave(other, sound, frame));
      if (!inside || duplicate) {
        if (!el.paused) el.pause();
        return;
      }
      heard.push(sound);
      const t = soundFilePos(sound, frame) / soundFps(sound);
      if (el.paused) {
        try { el.currentTime = t; } catch (err) {}
        el.play().catch(() => {});
      } else if (force || Math.abs(el.currentTime - t) > 0.08) {
        try { el.currentTime = t; } catch (err) {}
      }
    });
  }
  function showFrame(frame) {
    const loc = locate(frame);
    if (!loc) return;
    const clip = loc.clip;
    video.muted = videoShouldMute(frame);
    const url = clip.url || "";
    if (!url) {
      hint("That clip is missing");
      return;
    }
    const t = (clip.in + loc.local + 0.15) / clipFps(clip);
    const token = ++seekToken;
    ignoreTime = token;
    const clear = () => {
      if (ignoreTime === token) ignoreTime = 0;
    };
    const seek = () => {
      if (ignoreTime !== token) return;
      try { video.currentTime = t; } catch (err) {}
      video.addEventListener("seeked", clear, { once: true });
      setTimeout(clear, 500);
      if (seqPlay && video.paused) video.play().catch(() => stopPlay());
    };
    if (video.dataset.rbSrc !== url) {
      video.dataset.rbSrc = url;
      video.addEventListener("loadeddata", seek, { once: true });
      video.src = url;
      video.load();
      return;
    }
    if (Math.abs(video.currentTime - t) <= 0.02) {
      ignoreTime = 0;
      if (seqPlay && video.paused) video.play().catch(() => stopPlay());
      return;
    }
    video.addEventListener("seeked", clear, { once: true });
    setTimeout(clear, 500);
    video.currentTime = t;
  }
  function canCut() {
    const loc = locate(playhead);
    if (!loc || loc.clip.pending) return false;
    return loc.local > 0 && loc.local < loc.len;
  }
  function blockGeom(el) {
    const base = lanes.getBoundingClientRect().left;
    const rect = el.getBoundingClientRect();
    return { left: rect.left - base, width: rect.width };
  }
  function frameToPx(frame) {
    const n = total();
    const blocks = box.querySelectorAll(".eclip");
    if (!n || !blocks.length) return 0;
    if (frame >= n) {
      const last = blocks[blocks.length - 1];
      const geom = blockGeom(last);
      const len = Math.max(1, clips[clips.length - 1].out - clips[clips.length - 1].in);
      return geom.left + geom.width + ((frame - n) * geom.width) / len;
    }
    const loc = locate(Math.max(0, frame));
    if (!loc || !blocks[loc.index]) return 0;
    const geom = blockGeom(blocks[loc.index]);
    const len = Math.max(1, loc.len);
    return geom.left + (loc.local / len) * geom.width;
  }
  function frameAtX(clientX) {
    const blocks = [...box.querySelectorAll(".eclip")];
    if (!blocks.length) return 0;
    const x = clientX - lanes.getBoundingClientRect().left;
    let acc = 0;
    for (let i = 0; i < blocks.length; i++) {
      const geom = blockGeom(blocks[i]);
      const len = clips[i].out - clips[i].in;
      if (x < geom.left + geom.width || i === blocks.length - 1) {
        const local = Math.min(
          len - 1,
          Math.max(0, Math.floor(((x - geom.left) / Math.max(geom.width, 1)) * len))
        );
        return acc + local;
      }
      acc += len;
    }
    return 0;
  }
  function stackSounds(items) {
    const rowEnds = [];
    const rows = new Map();
    const ordered = items.slice().sort((a, b) => (
      a.at - b.at || String(a.id).localeCompare(String(b.id))
    ));
    ordered.forEach((sound) => {
      const end = sound.at + sound.frames;
      let row = 0;
      for (; row < rowEnds.length; row++) {
        if (rowEnds[row] <= sound.at) break;
      }
      if (row === rowEnds.length) rowEnds.push(end);
      else rowEnds[row] = end;
      rows.set(sound.id, row);
    });
    return { rows: rows, count: rowEnds.length };
  }
  function placeSounds() {
    const items = allSounds();
    if (!items.length) {
      alane.style.height = "";
      return;
    }
    const pack = stackSounds(items);
    const pitch = 28;
    const pad = 4;
    const endFrame = items.reduce((max, sound) => Math.max(max, sound.at + sound.frames), total());
    alane.style.minWidth = Math.ceil(frameToPx(endFrame)) + "px";
    alane.style.height = (pad + Math.max(pack.count, 1) * pitch + 4) + "px";
    alane.querySelectorAll(".ablk").forEach((el) => {
      const sound = items.find((item) => item.id === el.dataset.id);
      if (!sound) return;
      const left = frameToPx(sound.at);
      const right = frameToPx(sound.at + sound.frames);
      const row = pack.rows.get(sound.id) || 0;
      el.style.left = left + "px";
      el.style.width = Math.max(10, right - left) + "px";
      el.style.top = (pad + row * pitch) + "px";
      el.classList.toggle("on", el.dataset.id === soundSel);
    });
  }
  function paintHead() {
    const n = total();
    const loc = locate(Math.min(Math.max(playhead, 0), Math.max(n - 1, 0)));
    const blocks = box.querySelectorAll(".eclip");
    let x = 0;
    if (loc && blocks[loc.index]) {
      const geom = blockGeom(blocks[loc.index]);
      const len = Math.max(1, loc.len);
      x = geom.left + (loc.local / len) * geom.width;
    }
    head.style.left = x + "px";
    const tc = document.getElementById("rb-scene-tc");
    if (tc) tc.textContent = (n ? fmt(playhead) : "0:00.00") + " · frame " + playhead;
    const num = document.getElementById("rb-scene-frame");
    if (num) num.textContent = String(playhead);
    const cut = document.getElementById("rb-scene-cut");
    if (cut) cut.disabled = !!previewing || busy || saving || acting || !canCut();
    const sound = allSounds().find((item) => item.id === soundSel);
    const picked = sound
      ? null
      : (clips.find((c) => c.id === clipSel) || (loc && loc.clip) || null);
    const pickedAt = picked ? clips.indexOf(picked) : -1;
    const del = document.getElementById("rb-scene-del");
    if (del) {
      del.disabled = !!previewing || busy || saving || acting || (!sound && !clipSel);
      del.textContent = sound ? "Delete audio" : "Delete";
      del.title = sound ? "Delete this audio" : "Delete this clip";
    }
    const yank = document.getElementById("rb-scene-yank");
    if (yank && yank.dataset.busy !== "1") {
      yank.disabled = !!previewing || busy || saving || acting || !picked || !picked.audio || !!picked.mute;
    }
    const genClip = document.getElementById("rb-gen-clip");
    if (genClip) {
      genClip.textContent = sound
        ? (sound.name || "Audio")
        : (pickedAt >= 0 ? ("Clip " + (pickedAt + 1)) : "Clip");
    }
    const sub = document.getElementById("rb-scene-sub");
    if (sub && pickedAt >= 0) {
      if (picked.pending) {
        sub.textContent = "Clip " + (pickedAt + 1) + " · " + (picked.detail || "Generating…");
      } else {
        const sec = ((picked.out - picked.in) / clipFps(picked)).toFixed(1);
        sub.textContent = "Clip " + (pickedAt + 1) + " · " + sec + "s";
      }
    }
    const backCut = document.getElementById("rb-scene-back-cut");
    if (backCut) backCut.hidden = !editingFile;
    const back = document.getElementById("rb-scene-back");
    const fwd = document.getElementById("rb-scene-fwd");
    if (back) back.disabled = !!previewing || busy;
    if (fwd) fwd.disabled = !!previewing || busy;
    const join = document.getElementById("rb-scene-join");
    if (join) {
      const idx = clips.findIndex((c) => c.id === clipSel);
      const noNext = idx < 0 || idx >= clips.length - 1;
      const pendingJoin = !noNext && (!!(clips[idx] && clips[idx].pending) || !!(clips[idx + 1] && clips[idx + 1].pending));
      join.disabled = busy || saving || acting || !!editingFile || noNext || pendingJoin;
      join.title = editingFile
        ? "Back to the clip in the cut before joining."
        : pendingJoin
          ? "That clip is still generating"
          : noNext
            ? "No clip after this one."
            : "Merge this clip with the next one.";
    }
    const undoBtn = document.getElementById("rb-scene-undo");
    const pickedPending = !!(picked && picked.pending);
    const genWhy = editingFile
      ? "Back to the clip in the cut before generating."
      : pickedPending
        ? "That clip is still generating"
        : "";
    const upload = document.getElementById("rb-scene-upload");
    if (upload) {
      if (!upload.dataset.tip) upload.dataset.tip = upload.title || "";
      upload.disabled = busy || saving || acting;
      upload.title = upload.dataset.tip || upload.title;
    }
    ["rb-gen-redo", "rb-gen-nudge", "rb-gen-new"].forEach((id) => {
      const el = document.getElementById(id);
      if (!el) return;
      if (!el.dataset.tip) el.dataset.tip = el.title || "";
      el.disabled = busy || saving || !!editingFile || !!previewing || pickedPending;
      el.title = (el.disabled && genWhy) ? genWhy : el.dataset.tip;
    });
    document.querySelectorAll("#rb-scene-results .acts button").forEach((el) => {
      el.disabled = busy || saving;
      el.title = (busy || saving) ? "Wait for this to finish." : (el.dataset.tip || el.title);
    });
    if (undoBtn) undoBtn.disabled = saving || busy || (undoStack.length === 0 && !cfg.undo);
    box.querySelectorAll(".eclip").forEach((el) => {
      el.classList.toggle("on", !soundSel && el.dataset.id === clipSel);
    });
    placeSounds();
  }
  function paintRuler() {
    const span = Math.max(totalSeconds(), 0.001);
    const seconds = Math.floor(span);
    const width = ruler.clientWidth || 300;
    const step = Math.max(1, Math.ceil((seconds * 36) / Math.max(width, 1)));
    ruler.innerHTML = "";
    const marks = [];
    for (let s = 0; s <= seconds; s += step) marks.push(s);
    if (marks[marks.length - 1] !== seconds) marks.push(seconds);
    marks.forEach((s, i) => {
      const mark = document.createElement("span");
      mark.style.left = (Math.min(s, span) / span * 100) + "%";
      if (i === 0) mark.className = "first";
      if (i === marks.length - 1) mark.className = ((mark.className || "") + " last").trim();
      mark.textContent = "0:" + String(s).padStart(2, "0");
      ruler.appendChild(mark);
    });
  }
  function renderClips() {
    box.innerHTML = "";
    clips.forEach((c, i) => {
      const el = document.createElement("div");
      el.className = "eclip";
      el.draggable = true;
      el.dataset.id = c.id;
      el.style.flex = (c.out - c.in) + " 1 0";
      if (c.mute) el.classList.add("is-mute");
      if (c.fresh && !c.pending) el.classList.add("is-new");
      if (c.pending) el.classList.add("is-pending");
      if (!c.url && !c.pending) el.classList.add("is-miss");
      const sec = ((c.out - c.in) / clipFps(c)).toFixed(1);
      const title = document.createElement("span");
      title.className = "t";
      if (c.pending) {
        const spin = document.createElement("span");
        spin.className = "spin";
        title.append(spin);
      }
      title.append(document.createTextNode("Clip " + (i + 1)));
      if (c.fresh && !c.pending) {
        const badge = document.createElement("span");
        badge.className = "new";
        badge.textContent = "New";
        title.append(badge);
      }
      const sub = document.createElement("span");
      sub.className = "d";
      sub.textContent = !c.url && !c.pending
        ? "missing"
        : c.pending
          ? (c.detail || "Generating…")
          : (c.mute ? sec + "s · silent" : sec + "s");
      el.append(title, sub);
      el.addEventListener("click", (e) => {
        soundSel = null;
        clipSel = c.id;
        noteSelection();
        seekAt(e.clientX);
      });
      el.addEventListener("dragstart", (e) => {
        dragId = c.id;
        e.dataTransfer.effectAllowed = "move";
        e.dataTransfer.setData("text/plain", c.id);
      });
      el.addEventListener("dragover", (e) => e.preventDefault());
      el.addEventListener("drop", (e) => {
        e.preventDefault();
        e.stopPropagation();
        reorder(dragId, c.id);
      });
      box.appendChild(el);
    });
    paintRuler();
    paintHead();
  }
  function seekAt(clientX) {
    if (previewing) {
      hint("Back to the clip in the cut before editing.");
      return;
    }
    const keep = seqPlay;
    if (!keep) stopPlay();
    if (!box.querySelector(".eclip")) return;
    playhead = frameAtX(clientX);
    const loc = locate(playhead);
    if (loc) {
      clipSel = loc.clip.id;
      if (keep) playClip = loc.clip;
    }
    soundSel = null;
    noteSelection();
    showFrame(playhead);
    if (keep) {
      syncAudio(true);
      if (video.paused) video.play().catch(() => stopPlay());
      const btn = document.getElementById("rb-scene-play");
      if (btn) btn.textContent = "Pause";
      startPlayWatch();
    }
    paintHead();
  }
  function snapshot() {
    undoStack.push({
      clips: clips.map((c) => ({
        id: c.id, in: c.in, out: c.out, mute: !!c.mute,
        sceneId: c.sceneId, file: c.file, url: c.url, fps: c.fps,
        frames: c.frames, audio: !!c.audio, fresh: !!c.fresh, virtual: !!c.virtual,
        pending: !!c.pending, detail: c.detail || "",
      })),
      sounds: sounds.map((s) => ({
        id: s.id, file: s.file, url: s.url, name: s.name, at: s.at, frames: s.frames,
        offset: soundOffset(s), fps: soundFps(s),
        fromFile: s.fromFile || "", fromIn: s.fromIn || 0,
      })),
      playhead: playhead,
      clipSel: clipSel,
      soundSel: soundSel,
      seq: seq,
      soundSeq: soundSeq,
    });
    if (undoStack.length > 40) undoStack.shift();
    paintHead();
  }
  function editPayload() {
    return {
      rally: cfg.rally,
      step: cfg.step,
      scene_id: cfg.sceneId,
      file: editingFile,
      clips: clips.map((c) => ({ in: c.in, out: c.out, mute: !!c.mute })),
      audio: sounds.map((sound) => ({
        file: sound.file,
        at: sound.at,
        frames: sound.frames,
        offset: soundOffset(sound),
      })),
    };
  }
  function contiguousScenes(list) {
    const seen = new Set();
    let prev = null;
    for (const clip of list) {
      if (clip.sceneId === prev) continue;
      if (seen.has(clip.sceneId)) return false;
      seen.add(clip.sceneId);
      prev = clip.sceneId;
    }
    return true;
  }
  function stageSnapshot() {
    const selected = clips.find((c) => c.id === clipSel) || null;
    let piece = 0;
    let local = 0;
    if (selected) {
      const run = sceneRuns().find((item) => item.sceneId === selected.sceneId);
      if (run) piece = Math.max(0, run.clips.indexOf(selected));
      local = playhead - sequenceOf(selected);
    }
    return {
      runs: sceneRuns().map((run) => ({
        sceneId: run.sceneId,
        mutes: run.clips.map((c) => !!c.mute),
      })),
      sounds: sounds.map((s) => ({
        id: s.id, file: s.file, url: s.url, name: s.name, at: s.at, frames: s.frames,
        offset: soundOffset(s), fps: soundFps(s),
        fromFile: s.fromFile || "", fromIn: s.fromIn || 0,
      })),
      playhead: playhead,
      soundSel: soundSel,
      sel: selected ? { sceneId: selected.sceneId, piece: piece, local: local } : null,
    };
  }
  function restoreStage(saved) {
    if (!saved) return;
    let si = 0;
    let picked = -1;
    saved.runs.forEach((run) => {
      const start = si;
      const same = si < clips.length
        && clips[si].sceneId === run.sceneId
        && run.mutes.length === 1;
      if (same) {
        clips[si].mute = !!run.mutes[0];
        si += 1;
      } else {
        run.mutes.forEach((mute) => {
          if (si >= clips.length) return;
          clips[si].mute = !!mute;
          si += 1;
        });
      }
      if (saved.sel && run.sceneId === saved.sel.sceneId) {
        const count = Math.max(1, si - start);
        const piece = Math.max(0, Math.min(count - 1, saved.sel.piece));
        picked = start + piece;
      }
    });
    sounds = saved.sounds.map((s) => Object.assign({}, s));
    if (picked >= 0 && clips[picked]) {
      const clip = clips[picked];
      clipSel = clip.id;
      const span = Math.max(1, clip.out - clip.in);
      const local = Math.max(0, Math.min(span - 1, saved.sel.local || 0));
      playhead = sequenceOf(clip) + local;
    }
    soundSel = sounds.some((s) => s.id === saved.soundSel) ? saved.soundSel : null;
    noteSelection();
  }
  async function commit(opts) {
    const keepLane = !!(opts && opts.keepLane);
    const persistMute = !!(opts && opts.persistMute);
    if (saving || busy || previewing || !isDirty()) return false;
    if (!editingFile && !(await syncSelection())) return false;
    const editing = editingFile;
    saving = true;
    paintHead();
    stopPlay();
    try {
      if (editing) {
        const data = await postJson("/api/scene-attempt-edit", editPayload());
        undoStack = [];
        if (data.highlight) highlightFile = data.highlight;
        applyState(data, true);
        setStatus("Saved as a new take. The clip in the cut stays.");
        return true;
      }
      if (!contiguousScenes(clips)) {
        hint("Keep a clip's pieces together.");
        return false;
      }
      const kept = keepLane ? stageSnapshot() : null;
      const runs = sceneRuns();
      const serverIds = (cfg.scenes || []).map((row) => row.id);
      const localIds = runs.map((run) => run.sceneId);
      const dirty = runs.filter((run) => {
        if (!keepLane) return runDirty(run);
        if (pictureDirty(run)) return true;
        return persistMute && run.clips.some((clip) => clip.mute);
      });
      const removed = serverIds.filter((id) => !localIds.includes(id));
      const orderOnly = !dirty.length && !removed.length
        && localIds.join("|") !== serverIds.join("|");
      const selectId = (clips.find((c) => c.id === clipSel) || {}).sceneId || cfg.sceneId || "";
      let data = null;
      if (orderOnly) {
        data = await postJson("/api/scene-order", {
          rally: cfg.rally,
          step: cfg.step,
          ids: localIds,
        });
      } else {
        for (const id of removed) {
          data = await postJson("/api/scene-edit", {
            rally: cfg.rally,
            step: cfg.step,
            scene_id: id,
            clips: [],
            audio: [],
            select: selectId,
          });
        }
        for (const run of dirty) {
          const origin = sequenceOf(run.clips[0]);
          const length = runLength(run);
          const fps = clipFps(run.clips[0]);
          const audio = [];
          if (!keepLane) {
            for (const sound of sounds) {
              const start = Math.max(sound.at, origin);
              const end = Math.min(sound.at + sound.frames, origin + length);
              if (end <= start || layerIsDuplicate(sound, run)) continue;
              const into = (start - sound.at) + soundOffset(sound);
              const from = soundFps(sound);
              const offset = Math.abs(from - fps) < 0.01
                ? into
                : Math.round(into * fps / from);
              audio.push({
                file: sound.file,
                at: start - origin,
                frames: end - start,
                offset: offset,
              });
            }
          }
          data = await postJson("/api/scene-edit", {
            rally: cfg.rally,
            step: cfg.step,
            scene_id: run.sceneId,
            clips: run.clips.map((c) => ({
              in: c.in,
              out: c.out,
              mute: keepLane ? (!!c.mute && !yankHeld(c)) : !!c.mute,
            })),
            audio: audio,
            select: selectId,
          });
        }
      }
      if (!data) return false;
      undoStack = [];
      if (data.highlight) highlightFile = data.highlight;
      applyState(data, true);
      if (kept) {
        restoreStage(kept);
        renderClips();
        renderSounds();
        if (total()) showFrame(playhead);
      }
      setStatus("");
      return true;
    } catch (err) {
      hint((err && err.message) || "Could not save");
      return false;
    } finally {
      saving = false;
      paintHead();
    }
  }
  async function undo() {
    if (previewing) {
      hint("Back to the clip in the cut before editing.");
      return;
    }
    if (saving || busy) return;
    if (undoStack.length) {
      const snap = undoStack.pop();
      stopPlay();
      clips = snap.clips;
      sounds = snap.sounds;
      playhead = snap.playhead;
      clipSel = snap.clipSel;
      soundSel = snap.soundSel;
      seq = snap.seq;
      soundSeq = snap.soundSeq;
      const live = new Set(sounds.map((s) => s.id));
      [...players.keys()].forEach((id) => {
        if (!live.has(id)) dropPlayer(id);
      });
      renderClips();
      renderSounds();
      if (total()) showFrame(playhead);
      paintHead();
      return;
    }
    if (!cfg.undo) return;
    stopPlay();
    saving = true;
    paintHead();
    try {
      const data = await postJson("/api/scene-undo", {
        rally: cfg.rally,
        step: cfg.step,
      });
      highlightFile = "";
      applyState(data, true);
      setStatus("Undone");
    } catch (err) {
      hint((err && err.message) || "Could not undo");
    } finally {
      saving = false;
      paintHead();
    }
  }
  async function joinNext() {
    if (busy || saving || acting || editingFile) return;
    if (!(await syncSelection())) return;
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    const idx = clips.findIndex((c) => c.id === clipSel);
    if (idx < 0 || idx >= clips.length - 1) return;
    if ((clips[idx] && clips[idx].pending) || (clips[idx + 1] && clips[idx + 1].pending)) {
      hint("That clip is still generating");
      return;
    }
    stopPlay();
    saving = true;
    paintHead();
    try {
      const data = await postJson("/api/scene-join", {
        rally: cfg.rally,
        step: cfg.step,
      });
      undoStack = [];
      highlightFile = "";
      applyState(data, true);
      setStatus("Joined with the next clip.");
    } catch (err) {
      hint((err && err.message) || "Could not join");
    } finally {
      saving = false;
      paintHead();
    }
  }
  function step(delta) {
    if (previewing) {
      hint("Back to the clip in the cut before editing.");
      return;
    }
    stopPlay();
    const n = total();
    if (n <= 0) return;
    playhead = Math.min(n - 1, Math.max(0, playhead + delta));
    const loc = locate(playhead);
    if (loc) clipSel = loc.clip.id;
    noteSelection();
    showFrame(playhead);
    paintHead();
  }
  function cut() {
    if (previewing || busy || saving || acting) return;
    stopPlay();
    const loc = locate(playhead);
    if (!loc || loc.local <= 0 || loc.local >= loc.len) return;
    if (loc.clip.pending) {
      hint("That clip is still generating");
      return;
    }
    snapshot();
    const at = loc.clip.in + loc.local;
    const cutFrame = loc.acc + loc.local;
    const right = { id: "c" + (++seq), in: at, out: loc.clip.out, mute: !!loc.clip.mute };
    loc.clip.out = at;
    right.sceneId = loc.clip.sceneId;
    right.file = loc.clip.file;
    right.url = loc.clip.url;
    right.fps = loc.clip.fps;
    right.frames = loc.clip.frames;
    right.audio = !!loc.clip.audio;
    right.fresh = false;
    clips.splice(loc.index + 1, 0, right);
    splitSoundsAt(cutFrame);
    clipSel = right.id;
    noteSelection();
    renderClips();
    renderSounds();
    commit({ keepLane: true });
  }
  function shiftSoundsAfter(frame, by) {
    sounds.forEach((sound) => {
      if (sound.at >= frame) sound.at += by;
    });
  }
  async function deleteSelected() {
    if (previewing) {
      hint("Back to the clip in the cut before editing.");
      return;
    }
    if (busy || saving || acting) return;
    stopPlay();
    if (soundSel) {
      const sound = allSounds().find((item) => item.id === soundSel);
      if (!sound) {
        soundSel = null;
        paintHead();
        return;
      }
      if (sound.embedded) {
        const clip = clips.find((item) => item.id === sound.clipId);
        if (!clip) return;
        snapshot();
        clip.mute = true;
        soundSel = null;
        renderClips();
        renderSounds();
        await commit({ keepLane: true, persistMute: true });
        return;
      }
      snapshot();
      dropPlayer(soundSel);
      sounds = sounds.filter((item) => item.id !== soundSel);
      soundSel = null;
      renderSounds();
      paintHead();
      return;
    }
    const idx = clips.findIndex((c) => c.id === clipSel);
    if (idx < 0) return;
    if (clips.length <= 1) {
      hint("Keep at least one clip");
      return;
    }
    const clip = clips[idx];
    const start = sequenceOf(clip);
    const length = Math.max(1, clip.out - clip.in);
    const end = start + length;
    const nextClip = clips[idx + 1] || clips[idx - 1] || null;
    let lifted = null;
    if (clip.audio && !clip.mute && clip.file) {
      acting = true;
      paintHead();
      try {
        const data = await postAudio({
          rally: cfg.rally,
          step: cfg.step,
          file: clip.file,
          in: clip.in,
          out: clip.out,
          fps: clipFps(clip),
          name: "Clip " + (idx + 1),
        });
        data.fps = clipFps(clip);
        data.fromFile = clip.file;
        data.fromIn = clip.in;
        data.offset = 0;
        lifted = data;
      } catch (err) {
        hint((err && err.message) || "Could not keep that audio");
        acting = false;
        paintHead();
        return;
      }
    }
    snapshot();
    if (lifted) {
      rememberSound(lifted);
      addSound(lifted, start, length, lifted.name || ("Clip " + (idx + 1)), false);
    }
    splitSoundsAt(end);
    shiftSoundsAfter(end, -length);
    clips.splice(idx, 1);
    if (nextClip && nextClip.sceneId) cfg.sceneId = nextClip.sceneId;
    if (lifted) clipSel = null;
    else clipSel = nextClip ? nextClip.id : null;
    noteSelection();
    const n = total();
    playhead = n ? Math.min(playhead, Math.max(0, n - 1)) : 0;
    if (n) showFrame(playhead);
    renderClips();
    renderSounds();
    let saved = false;
    try {
      saved = await commit({ keepLane: true });
    } finally {
      acting = false;
      paintHead();
    }
    if (!saved) return;
    if (lifted) {
      clipSel = null;
      renderClips();
      renderSounds();
      setStatus("Removed the clip. Its audio stays on the lane.");
    } else {
      setStatus("Removed the clip.");
    }
  }
  function reorder(fromId, toId) {
    if (!fromId || fromId === toId) return;
    const moving = clips.find((c) => c.id === fromId);
    if (!moving) return;
    const rest = clips.filter((c) => c.id !== fromId);
    const idx = rest.findIndex((c) => c.id === toId);
    if (idx < 0) return;
    rest.splice(idx, 0, moving);
    if (!contiguousScenes(rest)) {
      hint("Keep a clip's pieces together.");
      return;
    }
    snapshot();
    clips = rest;
    dragId = null;
    const loc = locate(playhead);
    if (loc) clipSel = loc.clip.id;
    noteSelection();
    renderClips();
    renderSounds();
    if (total()) showFrame(playhead);
    commit();
  }
  function togglePlay() {
    document.querySelectorAll("#rb-scene-results video").forEach((el) => {
      try { el.pause(); } catch (err) {}
    });
    if (previewing) {
      const btn = document.getElementById("rb-scene-play");
      if (video.paused) {
        video.play().catch(() => {});
        if (btn) btn.textContent = "Pause";
      } else {
        video.pause();
        if (btn) btn.textContent = "Play";
      }
      return;
    }
    if (seqPlay) { stopPlay(); return; }
    const n = total();
    if (n <= 1) return;
    const loc = locate(playhead) || locate(0);
    if (!loc) return;
    seqPlay = true;
    playClip = loc.clip;
    const btn = document.getElementById("rb-scene-play");
    if (btn) btn.textContent = "Pause";
    video.muted = videoShouldMute(playhead);
    syncAudio(true);
    showFrame(playhead);
    armStandby(playClip);
    video.play().catch(() => stopPlay());
    startPlayWatch();
  }
  function standbyAt(clip) {
    return (clip.in + 0.15) / clipFps(clip);
  }
  function armStandby(clip) {
    if (!standby || !seqPlay || !clip) return;
    const next = clips[clips.indexOf(clip) + 1];
    if (!next || !next.url || next.url === clip.url) return;
    const t = standbyAt(next);
    const parked = standby.dataset.rbSrc === next.url
      && standby.dataset.rbReady === "1"
      && standby.readyState >= 2
      && Math.abs((standby.currentTime || 0) - t) < 0.08;
    if (parked || standby.dataset.rbArming === "1") return;
    const mark = () => {
      standby.dataset.rbArming = "0";
      if (standby.dataset.rbSrc !== next.url) return;
      if (Math.abs((standby.currentTime || 0) - t) > 0.08) return;
      standby.dataset.rbReady = "1";
    };
    const seek = () => {
      if (standby.dataset.rbSrc !== next.url || standby.dataset.rbArming === "1") return;
      standby.dataset.rbArming = "1";
      standby.dataset.rbReady = "0";
      standby.addEventListener("seeked", mark, { once: true });
      try { standby.currentTime = t; } catch (err) {
        standby.dataset.rbArming = "0";
        return;
      }
      if (!standby.seeking) mark();
    };
    if (standby.dataset.rbSrc !== next.url) {
      standby.dataset.rbSrc = next.url;
      standby.dataset.rbReady = "0";
      standby.dataset.rbArming = "1";
      standby.muted = true;
      standby.addEventListener("loadeddata", () => {
        standby.dataset.rbArming = "0";
        seek();
      }, { once: true });
      standby.src = next.url;
      standby.load();
      return;
    }
    seek();
  }
  function swapStandby(next) {
    const t = standbyAt(next);
    if (!standby || standby.dataset.rbSrc !== (next.url || "")
      || standby.dataset.rbReady !== "1" || standby.readyState < 2
      || Math.abs((standby.currentTime || 0) - t) > 0.08) {
      return false;
    }
    const show = standby;
    const hide = video;
    hide.pause();
    hide.classList.add("standby");
    show.classList.remove("standby");
    video = show;
    standby = hide;
    standby.pause();
    standby.muted = true;
    standby.dataset.rbReady = "0";
    video.muted = videoShouldMute(sequenceOf(next));
    if (video.paused) video.play().catch(() => stopPlay());
    return true;
  }
  function advancePlay(from) {
    if (!seqPlay || playClip !== from) return;
    const next = clips[clips.indexOf(from) + 1];
    if (!next) {
      playhead = Math.max(0, total() - 1);
      stopPlay();
      showFrame(playhead);
      paintHead();
      return;
    }
    const src = video.currentTime * clipFps(from);
    // A fresh cut still shares one file. Playing on into it drops no frames.
    const intoNext = next.url === from.url
      && src + 0.5 >= next.in && src < next.out - 0.25;
    if (!intoNext && !swapStandby(next)) {
      if (crossWaits < 10) {
        crossWaits += 1;
        crossTimer = setTimeout(() => {
          crossTimer = 0;
          advancePlay(from);
        }, 30);
        return;
      }
    }
    crossWaits = 0;
    playClip = next;
    if (!intoNext) {
      playhead = sequenceOf(next);
      if (!soundSel) clipSel = next.id;
      noteSelection();
      if (video.dataset.rbSrc !== (next.url || "")) showFrame(playhead);
    }
    paintHead();
    syncAudio();
    armStandby(playClip);
    bindPlayWatch(video, playWatch);
  }
  function bindPlayWatch(el, token) {
    if (!el || !seqPlay) return;
    const tick = (_now, meta) => {
      if (token !== playWatch || el !== video || !seqPlay || !playClip) return;
      if (ignoreTime || el.seeking) {
        if (el.requestVideoFrameCallback) el.requestVideoFrameCallback(tick);
        return;
      }
      const clip = playClip;
      const fps = clipFps(clip);
      const end = clip.out / fps;
      const presented = meta && isFinite(meta.mediaTime) ? meta.mediaTime : el.currentTime;
      const frame = 1 / Math.max(fps, 1);
      if (presented + frame < end - 0.0005) {
        if (el.requestVideoFrameCallback) el.requestVideoFrameCallback(tick);
        else crossTimer = setTimeout(() => tick(0, null), 30);
        return;
      }
      // rAF stays on the display clock. setTimeout here was late enough to hold the last frame.
      const fireAt = end - (0.35 / Math.max(fps, 1));
      const waitFrame = () => {
        crossTimer = 0;
        if (token !== playWatch || el !== video || playClip !== clip) return;
        if (!el.ended && el.currentTime < fireAt) {
          crossTimer = requestAnimationFrame(waitFrame);
          return;
        }
        advancePlay(clip);
      };
      crossTimer = requestAnimationFrame(waitFrame);
    };
    if (el.requestVideoFrameCallback) el.requestVideoFrameCallback(tick);
    else crossTimer = setTimeout(() => tick(0, null), 30);
  }
  function startPlayWatch() {
    playWatch += 1;
    crossWaits = 0;
    clearCross();
    bindPlayWatch(video, playWatch);
  }
  function followPlayback() {
    if (this !== video) return;
    if (previewing) return;
    if (!seqPlay || !playClip || scrubbing || ignoreTime || video.seeking) return;
    const src = video.currentTime * clipFps(playClip);
    if (src >= playClip.out - 0.05) return;
    const local = Math.max(0, Math.min(playClip.out - playClip.in - 1, Math.floor(src - playClip.in)));
    let acc = 0;
    for (const c of clips) {
      if (c === playClip) break;
      acc += c.out - c.in;
    }
    playhead = acc + local;
    if (!soundSel) clipSel = playClip.id;
    noteSelection();
    paintHead();
    syncAudio();
    armStandby(playClip);
  }
  function onClipEnded() {
    if (this !== video || !seqPlay || !playClip || previewing) return;
    clearCross();
    advancePlay(playClip);
  }
  video.addEventListener("timeupdate", followPlayback);
  video.addEventListener("ended", onClipEnded);
  if (standby) {
    standby.addEventListener("timeupdate", followPlayback);
    standby.addEventListener("ended", onClipEnded);
  }

  function sequenceOf(clip) {
    let acc = 0;
    for (const item of clips) {
      if (item === clip) return acc;
      acc += item.out - item.in;
    }
    return acc;
  }
  function embeddedSounds() {
    const rows = [];
    clips.forEach((clip, i) => {
      if (!clip.audio || clip.mute) return;
      rows.push({
        id: "emb-" + clip.id,
        embedded: true,
        clipId: clip.id,
        name: "Clip " + (i + 1),
        at: sequenceOf(clip),
        frames: Math.max(1, clip.out - clip.in),
      });
    });
    return rows;
  }
  function allSounds() {
    return embeddedSounds().concat(sounds);
  }
  function renderSounds() {
    alane.innerHTML = "";
    allSounds().forEach((sound) => {
      const el = document.createElement("div");
      el.className = "ablk" + (sound.embedded ? " is-emb" : "");
      el.dataset.id = sound.id;
      el.textContent = sound.name || "Audio";
      el.addEventListener("pointerdown", (e) => {
        if (!sound.embedded) {
          startSoundDrag(e, sound);
          return;
        }
        if (e.button !== 0) return;
        e.preventDefault();
        e.stopPropagation();
        stopPlay();
        clipSel = null;
        soundSel = sound.id;
        const n = total();
        playhead = Math.max(0, Math.min(Math.max(n - 1, 0), sound.at | 0));
        if (n) showFrame(playhead);
        paintHead();
      });
      alane.appendChild(el);
    });
    placeSounds();
  }
  function startSoundDrag(e, sound) {
    if (e.button !== 0) return;
    e.preventDefault();
    e.stopPropagation();
    stopPlay();
    clipSel = null;
    soundSel = sound.id;
    placeSounds();
    paintHead();
    const grab = frameAtX(e.clientX) - sound.at;
    let moved = false;
    const move = (ev) => {
      if (!moved) {
        snapshot();
        moved = true;
      }
      const n = total();
      let at = frameAtX(ev.clientX) - grab;
      at = Math.max(0, Math.min(Math.max(n - 1, 0), Math.round(at)));
      sound.at = at;
      placeSounds();
      paintHead();
    };
    const up = () => {
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  }
  function rememberSound(data) {
    if (!data || !data.file) return;
    const existing = bin.find((item) => item.file === data.file);
    if (existing) {
      if (data.fromFile) {
        existing.fromFile = data.fromFile;
        existing.fromIn = data.fromIn || 0;
      }
      if (data.fps) existing.fps = data.fps;
      return;
    }
    bin.push({
      kind: "file",
      file: data.file,
      name: data.name || "Audio",
      frames: data.frames,
      url: data.url,
      fps: data.fps || FPS,
      fromFile: data.fromFile || "",
      fromIn: data.fromIn || 0,
    });
    renderBin();
  }
  function addSound(data, at, frames, name, record) {
    if (record !== false) snapshot();
    const n = total();
    const block = {
      id: "a" + (++soundSeq),
      file: data.file,
      url: data.url,
      name: name || data.name || "Audio",
      at: at == null ? Math.min(playhead, Math.max(n - 1, 0)) : at,
      frames: Math.max(1, frames || data.frames || 1),
      offset: data.offset || 0,
      fps: data.fps || FPS,
      fromFile: data.fromFile || "",
      fromIn: data.fromIn || 0,
    };
    sounds.push(block);
    soundSel = block.id;
    renderSounds();
    paintHead();
  }
  async function postAudio(body) {
    const res = await fetch("/api/scene-audio", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not take audio");
    return data;
  }
  async function yank() {
    if (previewing || busy || saving) return;
    const clip = clips.find((item) => item.id === clipSel);
    const btn = document.getElementById("rb-scene-yank");
    if (!clip || clip.mute || !clip.audio || !clip.file || !btn || btn.dataset.busy === "1") return;
    btn.dataset.busy = "1";
    btn.disabled = true;
    stopPlay();
    const index = clips.indexOf(clip);
    try {
      const data = await postAudio({
        rally: cfg.rally,
        step: cfg.step,
        file: clip.file,
        in: clip.in,
        out: clip.out,
        fps: clipFps(clip),
        name: "Clip " + (index + 1),
      });
      snapshot();
      clip.mute = true;
      data.fps = clipFps(clip);
      data.fromFile = clip.file;
      data.fromIn = clip.in;
      data.offset = 0;
      rememberSound(data);
      addSound(data, sequenceOf(clip), clip.out - clip.in, "Clip " + (index + 1), false);
      renderClips();
      hint("Lifted onto the lane.");
    } catch (err) {
      hint((err && err.message) || "Could not take audio");
    } finally {
      btn.dataset.busy = "";
      paintHead();
    }
  }
  async function useBin(item, chip) {
    if (copying) return;
    stopPlay();
    if (item.kind === "file") {
      addSound(item, null, item.frames, item.name);
      return;
    }
    copying = true;
    if (chip) chip.disabled = true;
    hint("Copying audio…");
    try {
      const data = await postAudio({
        rally: cfg.rally,
        step: cfg.step,
        file: item.file,
        in: item.in || 0,
        out: item.out,
        fps: FPS,
        name: item.name,
      });
      data.fps = FPS;
      data.fromFile = item.file;
      data.fromIn = item.in || 0;
      data.offset = 0;
      rememberSound(data);
      addSound(data, null, data.frames, item.name);
      hint("");
    } catch (err) {
      hint((err && err.message) || "Could not copy audio");
    } finally {
      copying = false;
      if (chip) chip.disabled = false;
    }
  }
  function renderBin() {
    const host = document.getElementById("rb-scene-bin");
    if (!host) return;
    host.innerHTML = "";
    const label = document.createElement("span");
    label.className = "bin-label";
    label.textContent = "Sounds";
    host.appendChild(label);
    if (!bin.length) {
      const empty = document.createElement("span");
      empty.className = "bin-empty";
      empty.textContent = "Yank a clip or add a file";
      host.appendChild(empty);
      return;
    }
    bin.forEach((item) => {
      const chip = document.createElement("button");
      chip.type = "button";
      chip.className = "chip";
      chip.textContent = item.name || "Audio";
      chip.title = "Place at the playhead";
      chip.addEventListener("click", () => useBin(item, chip));
      host.appendChild(chip);
    });
  }
  async function onSoundFile(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    stopPlay();
    const add = document.getElementById("rb-scene-add");
    if (add) add.disabled = true;
    hint("Adding sound…");
    try {
      const body = new FormData();
      body.append("rally", cfg.rally);
      body.append("step", cfg.step);
      body.append("fps", String(FPS));
      body.append("file", file, file.name);
      const res = await fetch("/api/scene-audio-upload", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Could not add that sound");
      rememberSound(data);
      addSound(data, null, data.frames, data.name);
      hint("");
    } catch (err) {
      hint((err && err.message) || "Could not add that sound");
    } finally {
      if (add) add.disabled = false;
    }
  }

  document.getElementById("rb-scene-back").onclick = () => step(-1);
  document.getElementById("rb-scene-fwd").onclick = () => step(1);
  document.getElementById("rb-scene-play").onclick = togglePlay;
  document.getElementById("rb-scene-cut").onclick = cut;
  const undoBtn = document.getElementById("rb-scene-undo");
  if (undoBtn) undoBtn.onclick = undo;
  document.getElementById("rb-scene-yank").onclick = yank;
  document.getElementById("rb-scene-add").onclick = () => {
    document.getElementById("rb-scene-file").click();
  };
  document.getElementById("rb-scene-file").addEventListener("change", onSoundFile);
  async function onClipFile(e) {
    const file = e.target.files && e.target.files[0];
    e.target.value = "";
    if (!file) return;
    const name = (file.name || "").toLowerCase();
    if (!/\.(mp4|mov|m4v|webm)$/.test(name)) {
      hint("Use an mp4, mov, or webm file");
      return;
    }
    if (file.size > 80 * 1024 * 1024) {
      hint("That clip is over 80 MB");
      return;
    }
    stopPlay();
    if (busy || saving || acting) return;
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    setBusy(true);
    setStatus("Adding the clip…");
    try {
      const body = new FormData();
      body.append("rally", cfg.rally);
      body.append("step", cfg.step);
      body.append("file", file, file.name);
      const res = await fetch("/api/scene-clip-upload", { method: "POST", body });
      const data = await res.json().catch(() => ({}));
      if (!res.ok) throw new Error(data.error || "Could not add that clip");
      undoStack = [];
      highlightFile = "";
      applyState(data, true);
      setStatus("Added the clip.");
    } catch (err) {
      const message = (err && err.message) || "Could not add that clip";
      hint(message);
      setStatus(message);
    } finally {
      setBusy(false);
    }
  }
  const uploadBtn = document.getElementById("rb-scene-upload");
  if (uploadBtn) {
    uploadBtn.onclick = () => {
      const input = document.getElementById("rb-scene-clip-file");
      if (input && !uploadBtn.disabled) input.click();
    };
  }
  const clipFile = document.getElementById("rb-scene-clip-file");
  if (clipFile) clipFile.addEventListener("change", onClipFile);
  document.getElementById("rb-scene-del").onclick = deleteSelected;
  document.getElementById("rb-scene-close").onclick = async () => {
    stopPlay();
    if (saving) return;
    if (isDirty() && !busy && !previewing) {
      await commit();
      if (isDirty()) return;
    }
    try {
      await postJson("/api/scene-select", {
        rally: cfg.rally,
        step: cfg.step,
        scene_id: cfg.sceneId,
        bump: true,
      });
    } catch (err) {}
    stopPoll();
    shade.remove();
  };
  const backCut = document.getElementById("rb-scene-back-cut");
  if (backCut) backCut.onclick = () => focusScene(cfg.sceneId);
  const joinBtn = document.getElementById("rb-scene-join");
  if (joinBtn) joinBtn.onclick = () => joinNext();
  const helpBtn = document.getElementById("rb-gen-help");
  const helpSheet = document.getElementById("rb-help-sheet");
  if (helpBtn && helpSheet) {
    helpBtn.onclick = () => { helpSheet.hidden = false; };
    helpSheet.addEventListener("click", (e) => {
      if (e.target === helpSheet) helpSheet.hidden = true;
    });
    const helpClose = document.getElementById("rb-help-close");
    if (helpClose) helpClose.onclick = () => { helpSheet.hidden = true; };
  }
  ruler.addEventListener("pointerdown", (e) => {
    if (e.button !== 0) return;
    e.preventDefault();
    scrubbing = true;
    seekAt(e.clientX);
    const move = (ev) => seekAt(ev.clientX);
    const up = () => {
      scrubbing = false;
      window.removeEventListener("pointermove", move);
      window.removeEventListener("pointerup", up);
      if (seqPlay && video.paused) video.play().catch(() => stopPlay());
    };
    window.addEventListener("pointermove", move);
    window.addEventListener("pointerup", up);
  });
  if (window.__rbSceneKey) {
    document.removeEventListener("keydown", window.__rbSceneKey);
  }
  window.__rbSceneKey = (e) => {
    if (!document.getElementById("rb-scene-shade")) return;
    const sheet = document.querySelector("#rb-scene-shade .sheet:not([hidden])");
    if (sheet) {
      if (e.key === "Escape") {
        e.preventDefault();
        if (sheet.id === "rb-scene-sheet") hideSheet();
        else sheet.hidden = true;
      }
      return;
    }
    const tag = (e.target && e.target.tagName) || "";
    if (tag === "INPUT" || tag === "TEXTAREA") return;
    if (e.metaKey || e.ctrlKey) {
      if ((e.key === "z" || e.key === "Z") && !e.shiftKey) {
        e.preventDefault();
        undo();
      }
      return;
    }
    if (e.altKey) return;
    if (e.key === "ArrowLeft") { e.preventDefault(); step(-1); }
    else if (e.key === "ArrowRight") { e.preventDefault(); step(1); }
    else if (e.key === "c" || e.key === "C") { e.preventDefault(); cut(); }
    else if (e.key === "Delete" || e.key === "Backspace") { e.preventDefault(); deleteSelected(); }
    else if (e.key === " ") { e.preventDefault(); togglePlay(); }
  };
  document.addEventListener("keydown", window.__rbSceneKey);
  video.muted = true;
  bootTimeline();
  renderBin();
  renderClips();
  renderSounds();
  if (total()) showFrame(playhead);
  if (window.__rbSceneResize) {
    window.removeEventListener("resize", window.__rbSceneResize);
  }
  window.__rbSceneResize = () => {
    if (!document.getElementById("rb-scene-shade")) return;
    paintRuler();
    paintHead();
  };
  window.addEventListener("resize", window.__rbSceneResize);
  requestAnimationFrame(() => {
    paintRuler();
    paintHead();
  });
__GEN__
})();
"""


def _rally_root(name: str) -> tuple[Path | None, JSONResponse | None]:
    rally = str(name or "").strip()
    if not rally or "/" in rally or "\\" in rally or rally.startswith("."):
        return None, JSONResponse({"error": "Unknown rally"}, status_code=400)
    root = (OUTPUT_DIR / rally).resolve()
    base = OUTPUT_DIR.resolve()
    if root != base and base not in root.parents:
        return None, JSONResponse({"error": "Unknown rally"}, status_code=400)
    if not (root / "meta.json").is_file():
        return None, JSONResponse({"error": "Unknown rally"}, status_code=404)
    return root, None


def _audio_payload(pkg: RallyPackage, saved: dict) -> dict:
    return {
        "ok": True,
        "file": saved["file"],
        "url": media_url(pkg, saved["file"]),
        "name": saved["name"],
        "frames": saved["frames"],
    }


def register_routes() -> None:
    global _ROUTES
    if _ROUTES:
        return
    _ROUTES = True

    @app.post("/api/scene-edit")
    async def scene_edit(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Bad request"}, status_code=400)
        rally = str((body or {}).get("rally") or "").strip()
        step_id = str((body or {}).get("step") or "").strip()
        scene_id = str((body or {}).get("scene_id") or "").strip()
        select = str((body or {}).get("select") or "").strip()
        clips = (body or {}).get("clips")
        lane = (body or {}).get("audio") or []
        root, err = _rally_root(rally)
        if err is not None or root is None:
            return err
        if not isinstance(clips, list) or not isinstance(lane, list):
            return JSONResponse({"error": "Bad clips"}, status_code=400)

        def _run() -> dict:
            pkg = RallyPackage.load(root)
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            changed = apply_edit(pkg, step_id, scene_id, clips, lane)
            if select:
                select_scene(pkg, step_id, select)
            return _editor_state(pkg, step_id, changed=changed)

        try:
            state = await asyncio.to_thread(_run)
        except KeyError:
            return JSONResponse({"error": "Unknown step"}, status_code=400)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        if state.get("changed"):
            _bump(rally, "")
            _saved(rally)
        return state

    @app.post("/api/scene-order")
    async def scene_order(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        ids = body.get("ids")
        if not isinstance(ids, list):
            return JSONResponse({"error": "Bad order"}, status_code=400)

        def _run() -> dict:
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            changed = reorder_scenes(pkg, step_id, ids)
            return _editor_state(pkg, step_id, changed=changed)

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        if state.get("changed"):
            _bump(pkg.name, "")
            _saved(pkg.name)
        return state

    @app.post("/api/scene-select")
    async def scene_select(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        scene_id = str(body.get("scene_id") or "").strip()
        bump = bool(body.get("bump"))

        def _run() -> dict:
            select_scene(pkg, step_id, scene_id)
            current = selected_id(pkg, step_id)
            if scene_id and current != scene_id:
                raise SceneError("That clip is not on the timeline")
            return {"ok": True, "sceneId": current}

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        if bump:
            _bump(pkg.name, "")
            _saved(pkg.name)
        return state

    def _quiet(changed: bool, rally: str) -> None:
        if not changed:
            return
        _bump(rally, "")
        _saved(rally)

    @app.post("/api/scene-undo")
    async def scene_undo(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err

        def _run() -> dict:
            undo_edit(pkg, step_id)
            return _editor_state(pkg, step_id, changed=True)

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _quiet(True, pkg.name)
        return state

    @app.post("/api/scene-join")
    async def scene_join(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err

        def _run() -> dict:
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            join_with_next(pkg, step_id)
            return _editor_state(pkg, step_id, changed=True)

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _quiet(True, pkg.name)
        return state

    @app.post("/api/scene-attempt-open")
    async def scene_attempt_open(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        rel = str(body.get("file") or "")

        def _run() -> dict:
            return _editor_state(pkg, step_id, editing=rel)

        try:
            return await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    @app.post("/api/scene-attempt-edit")
    async def scene_attempt_edit(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        rel = str(body.get("file") or "")
        clips = body.get("clips")
        lane = body.get("audio") or []
        if not isinstance(clips, list) or not isinstance(lane, list):
            return JSONResponse({"error": "Bad clips"}, status_code=400)

        def _run() -> dict:
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            made = apply_attempt_edit(pkg, step_id, rel, clips, lane)
            return _editor_state(
                pkg, step_id, editing=rel, highlight=made, changed=bool(made)
            )

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        return state

    @app.post("/api/scene-attempt-splice")
    async def scene_attempt_splice(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        rel = str(body.get("file") or "")

        def _run() -> dict:
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            made = splice_clip_audio(pkg, step_id, rel)
            return _editor_state(pkg, step_id, highlight=made, changed=True)

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        return state

    @app.post("/api/scene-audio")
    async def scene_audio(request: Request):
        try:
            body = await request.json()
        except Exception:  # noqa: BLE001
            return JSONResponse({"error": "Bad request"}, status_code=400)
        body = body or {}
        root, err = _rally_root(str(body.get("rally") or ""))
        if err is not None or root is None:
            return err
        step_id = str(body.get("step") or "").strip()

        def _run() -> dict:
            pkg = RallyPackage.load(root)
            if pkg.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            try:
                fps = float(body.get("fps") or 0)
            except (TypeError, ValueError):
                fps = 0.0
            saved = extract_audio(
                pkg,
                str(body.get("file") or ""),
                body.get("in"),
                body.get("out"),
                fps,
                str(body.get("name") or ""),
            )
            return _audio_payload(pkg, saved)

        try:
            return await asyncio.to_thread(_run)
        except KeyError:
            return JSONResponse({"error": "Unknown step"}, status_code=400)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    @app.post("/api/scene-audio-upload")
    async def scene_audio_upload(
        rally: str = Form(""),
        step: str = Form(""),
        fps: float = Form(30),
        file: UploadFile = File(),
    ):
        root, err = _rally_root(rally)
        if err is not None or root is None:
            return err
        raw = await file.read(26 * 1024 * 1024)
        if len(raw) > 25 * 1024 * 1024:
            return JSONResponse(
                {"error": "Sound file is over 25 MB"}, status_code=400
            )
        original = file.filename or "sound.wav"

        def _run() -> dict:
            pkg = RallyPackage.load(root)
            if pkg.step(step).status.value == "running":
                raise SceneError("Wait for generation to finish")
            saved = store_audio_upload(pkg, raw, original, fps)
            return _audio_payload(pkg, saved)

        try:
            return await asyncio.to_thread(_run)
        except KeyError:
            return JSONResponse({"error": "Unknown step"}, status_code=400)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    @app.post("/api/scene-clip-upload")
    async def scene_clip_upload(
        rally: str = Form(""),
        step: str = Form(""),
        file: UploadFile = File(),
    ):
        root, err = _rally_root(rally)
        if err is not None or root is None:
            return err
        raw = await file.read(81 * 1024 * 1024)
        if len(raw) > 80 * 1024 * 1024:
            return JSONResponse(
                {"error": "That clip is over 80 MB"}, status_code=400
            )
        if not raw:
            return JSONResponse({"error": "That clip is empty"}, status_code=400)
        original = file.filename or "clip.mp4"

        def _run() -> dict:
            pkg = RallyPackage.load(root)
            if pkg.step(step).status.value == "running":
                raise SceneError("Wait for generation to finish")
            import_timeline_clip(pkg, step, raw, original)
            return _editor_state(pkg, step, changed=True)

        try:
            state = await asyncio.to_thread(_run)
        except KeyError:
            return JSONResponse({"error": "Unknown step"}, status_code=400)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _bump(rally, "")
        _saved(rally)
        return state

    @app.get("/api/scene-state")
    async def scene_state(rally: str = "", step: str = ""):
        pkg, step_id, err = _load_step({"rally": rally, "step": step})
        if err is not None or pkg is None:
            return err
        try:
            snap = await asyncio.to_thread(editor_snapshot, pkg, step_id)
        except KeyError:
            return JSONResponse({"error": "Unknown step"}, status_code=400)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        return _client_state(pkg, snap)

    @app.get("/api/scene-job")
    async def scene_job_status(rally: str = "", step: str = ""):
        from app.pipeline.editor_jobs import public_state

        _root, err = _rally_root(rally)
        if err is not None:
            return err
        return public_state(rally, step)

    @app.post("/api/scene-info")
    async def scene_info(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        kind = str(body.get("kind") or "")
        if kind not in {"regenerate", "redo-frames", "redo-plain", "nudge", "new-scene"}:
            return JSONResponse({"error": "Unknown edit"}, status_code=400)

        def _run() -> dict:
            from app.pipeline.generate import (
                clip_duration_choice,
                clip_duration_choices,
                clip_length_note,
            )
            from app.pipeline.scenes import capture_editor_pins, editor_target

            target = editor_target(pkg, step_id)
            if any(
                item["id"] == target["scene_id"] and item.get("pending")
                for item in bar_items(pkg, step_id)
            ):
                raise SceneError("That clip is still generating")
            seconds = float(target["seconds"])
            choose = kind != "nudge"
            notes = {
                key: clip_length_note(seconds, kind=kind, pins=key, choose=choose)
                for key in ("first", "last", "both")
            } if kind in {"redo-frames", "regenerate"} else {}
            if kind == "regenerate":
                notes = {
                    key: clip_length_note(
                        seconds, kind="redo-frames", pins=key, choose=True
                    )
                    for key in ("first", "last", "both")
                }
                notes["none"] = clip_length_note(
                    seconds, kind="redo-plain", choose=True
                )
            note = notes.get("both") or clip_length_note(
                seconds, kind=kind, choose=choose
            )
            pins: list[dict] = []
            if kind in {"redo-frames", "regenerate"}:
                info = capture_editor_pins(pkg, step_id, which="both")
                pins = [
                    {"id": "first", "label": "First frame", "url": _bust(pkg, info["first"])},
                    {"id": "last", "label": "Last frame", "url": _bust(pkg, info["last"])},
                ]
            elif kind == "new-scene":
                info = capture_editor_pins(pkg, step_id, which="last")
                pins = [
                    {"id": "last", "label": "Last frame", "url": _bust(pkg, info["last"])}
                ]
            return {
                "ok": True,
                "note": note,
                "notes": notes,
                "pins": pins,
                "durations": clip_duration_choices() if choose else [],
                "duration": clip_duration_choice(seconds) if choose else None,
            }

        try:
            return await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

    @app.post("/api/scene-job")
    async def scene_job(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        kind = str(body.get("kind") or "")
        prompt = str(body.get("prompt") or "").strip()
        scene_id = str(body.get("scene_id") or "").strip()
        raw_start = body.get("start_frame", True)
        start_frame = raw_start is True or raw_start == 1
        raw_pins = str(body.get("pins") or "both").strip().lower()
        pins = raw_pins if raw_pins in {"first", "last", "both"} else "both"
        duration = None
        if kind != "nudge":
            try:
                duration = int(body.get("duration"))
            except (TypeError, ValueError):
                return JSONResponse(
                    {"error": "Pick a length from 1 to 15 seconds"},
                    status_code=400,
                )
            if duration < 1 or duration > 15:
                return JSONResponse(
                    {"error": "Pick a length from 1 to 15 seconds"},
                    status_code=400,
                )
        if kind not in {"redo-frames", "redo-plain", "nudge", "new-scene"}:
            return JSONResponse({"error": "Unknown edit"}, status_code=400)
        if not prompt:
            return JSONResponse(
                {
                    "error": (
                        "Say the one thing to change"
                        if kind == "nudge"
                        else "Say what this clip should do"
                    )
                },
                status_code=400,
            )
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        from app.pipeline.editor_jobs import abandon, begin
        from app.pipeline.scenes import drop_pending_scene, prepare_pending_job

        rally = pkg.name
        root = pkg.dir()

        def _prepare() -> dict:
            live = RallyPackage.load(root)
            if live.step(step_id).status.value == "running":
                raise SceneError("Wait for generation to finish")
            return prepare_pending_job(
                live,
                step_id,
                scene_id,
                kind,
                seconds=duration,
                pins=pins,
                start_frame=start_frame,
            )

        try:
            prepared = await asyncio.to_thread(_prepare)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

        dest_id = str(prepared["dest_id"])

        def _drop() -> None:
            drop_pending_scene(RallyPackage.load(root), step_id, dest_id)

        try:
            job = begin(rally, step_id, dest_id)
        except RuntimeError as exc:
            await asyncio.to_thread(_drop)
            return JSONResponse({"error": str(exc)}, status_code=409)

        def _state() -> dict:
            live = RallyPackage.load(root)
            state = _editor_state(live, step_id)
            state["id"] = job.id
            state["jobId"] = job.id
            state["placeholderId"] = dest_id
            return state

        try:
            state = await asyncio.to_thread(_state)
        except Exception as exc:  # noqa: BLE001
            abandon(job.id)
            try:
                await asyncio.to_thread(_drop)
            except Exception:  # noqa: BLE001
                pass
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)

        async def _run() -> None:
            from app.pipeline.editor_jobs import finish, set_detail
            from app.pipeline.generate import editor_generate

            async def on_progress(msg: str) -> None:
                set_detail(job.id, msg)

            try:
                live = await asyncio.to_thread(RallyPackage.load, root)
                result = await editor_generate(
                    live,
                    step_id,
                    kind,
                    prompt,
                    start_frame=start_frame,
                    pins=pins,
                    duration=duration,
                    progress=on_progress,
                    dest_scene_id=dest_id,
                    pinned=prepared["target"],
                )
                finish(job.id, result=result)
                _saved(rally)
                _bump(rally, "")
                _notify(
                    "success",
                    f"{rally} · {clip_label(step_id)}",
                    detail=str(result.get("note") or ""),
                    rally=rally,
                    step=step_id,
                )
            except Exception as exc:  # noqa: BLE001
                finish(job.id, error=str(exc)[:400])
                try:
                    await asyncio.to_thread(_drop)
                except Exception:  # noqa: BLE001
                    pass
                _saved(rally)
                _bump(rally, "")
                _notify(
                    "error",
                    f"{rally} · {clip_label(step_id)} failed",
                    detail=str(exc)[:200],
                    rally=rally,
                    step=step_id,
                )

        verb = {
            "redo-frames": "Regenerate",
            "redo-plain": "Regenerate",
            "nudge": "Nudge",
            "new-scene": "New scene",
        }.get(kind, "Edit")
        _notify("info", f"{verb} · {rally} · {clip_label(step_id)}", rally=rally, step=step_id)
        job.task = asyncio.create_task(_run())
        return state

    @app.post("/api/scene-use")
    async def scene_use(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        rel = str(body.get("file") or "")

        def _run() -> dict:
            from app.pipeline.scenes import _safe, adopt_editor_attempt

            adopt_editor_attempt(pkg, step_id, _safe(pkg, rel))
            return _client_state(pkg, editor_snapshot(pkg, step_id))

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _bump(pkg.name, "Clip updated")
        _saved(pkg.name)
        return state

    @app.post("/api/scene-attempt-delete")
    async def scene_attempt_delete(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        rel = str(body.get("file") or "")

        def _run() -> dict:
            from app.pipeline.scenes import _safe, delete_editor_attempt

            delete_editor_attempt(pkg, step_id, _safe(pkg, rel))
            fresh = RallyPackage.load(pkg.dir())
            return _client_state(fresh, editor_snapshot(fresh, step_id))

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _bump(pkg.name, "")
        _saved(pkg.name)
        return state

    @app.post("/api/scene-focus")
    async def scene_focus(request: Request):
        body, err = await _body(request)
        if err is not None:
            return err
        pkg, step_id, err = _load_step(body)
        if err is not None or pkg is None:
            return err
        scene_id = str(body.get("scene_id") or "")

        def _run() -> dict:
            from app.pipeline.scenes import focus_editor_scene

            snap = focus_editor_scene(pkg, step_id, scene_id)
            return _client_state(pkg, snap)

        try:
            state = await asyncio.to_thread(_run)
        except SceneError as exc:
            return JSONResponse({"error": str(exc)}, status_code=400)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc)[:240]}, status_code=400)
        _bump(pkg.name, "")
        _saved(pkg.name)
        return state


def render_scene_bar(pkg: RallyPackage, step_id: str, *, refresh, open_editor) -> None:
    """Timeline under the step row. Edit opens the frame cutter."""
    seen = {"n": revision_of(pkg.name)}

    def poll() -> None:
        n = revision_of(pkg.name)
        if n == seen["n"]:
            return
        seen["n"] = n
        refresh(saved=True, note=revision_note(pkg.name))

    ui.timer(0.4, poll).style("display:none")

    st = pkg.step(step_id)
    bar_error = ""
    try:
        items = bar_items(pkg, step_id) if st.kind != StepKind.IMAGE else []
    except SceneError as exc:
        items = []
        bar_error = str(exc)
    sel = selected_id(pkg, step_id) if items else ""

    with ui.element("div").classes("rb-scenes"):
        with ui.element("div").classes("rb-scenes-track"):
            if st.kind == StepKind.IMAGE:
                with ui.element("div").classes("rb-scene is-still"):
                    ui.label("Preview").classes("t")
                    ui.label("still").classes("d")
            elif bar_error:
                with ui.element("div").classes("rb-scene is-still"):
                    ui.label(st.label or step_id).classes("t")
                    ui.label(bar_error).classes("d")
            elif not items:
                with ui.element("div").classes("rb-scene is-still"):
                    ui.label(st.label or step_id).classes("t")
                    ui.label("no take").classes("d")
            else:
                for i, item in enumerate(items):
                    cls = "rb-scene"
                    if item["id"] == sel:
                        cls += " is-on"
                    if item.get("missing"):
                        cls += " is-miss"
                    if item.get("fresh"):
                        cls += " is-new"
                    if item.get("pending"):
                        cls += " is-pending"
                    el = ui.element("div").classes(cls)
                    el.style(f"flex: {max(item['frames'], 1)} 1 0")
                    with el:
                        with ui.row().classes("items-center no-wrap gap-1"):
                            ui.label(f"Clip {i + 1}").classes("t")
                            if item.get("fresh"):
                                ui.label("New").classes("new")
                        if item.get("pending"):
                            sub = detail_for_scene(
                                pkg.name, step_id, str(item["id"])
                            ) or "Generating…"
                        elif item.get("missing"):
                            sub = "missing"
                        else:
                            sub = str(item.get("label") or "")
                        ui.label(sub).classes("d")
                    el.tooltip(
                        "Selects this clip. The player is all of these, "
                        "combined. Double-click to cut."
                    )

                    def make_arm(sid: str):
                        def arm(_e=None) -> None:
                            now = time.time()
                            key = f"{pkg.name}:{step_id}:{sid}"
                            if _CLICK["key"] == key and now - _CLICK["at"] < 0.4:
                                select_scene(pkg, step_id, sid)
                                open_editor()
                                return
                            before = selected_id(pkg, step_id)
                            select_scene(pkg, step_id, sid)
                            changed = selected_id(pkg, step_id) != before
                            _CLICK["key"] = key
                            _CLICK["at"] = now

                            def later(at: float = now, changed_: bool = changed) -> None:
                                if _CLICK["at"] != at:
                                    return
                                if changed_:
                                    refresh()

                            if changed:
                                ui.timer(0.45, later, once=True).style("display:none")

                        return arm

                    el.on("click", make_arm(item["id"]))

        missing_n = sum(1 for item in items if item.get("missing"))
        if len(items) > 1 and not missing_n:
            ui.label(f"{len(items)} clips, combined").classes(
                "rb-scenes-note"
            ).tooltip(
                "The player is these clips in order. "
                "You don't combine them again."
            )
        elif missing_n:
            ui.label("Clip file missing").classes("rb-scenes-note")

        edit = ui.button("Edit", on_click=open_editor).props("no-caps unelevated")
        edit.classes("rb-scene-edit")
        edit.set_enabled(bool(items))
        edit.tooltip(
            "Cut these clips. They are already combined into the take."
        )


def _bust(pkg: RallyPackage, path: Path) -> str:
    try:
        if not path.is_file():
            return ""
    except OSError:
        return ""
    rel = path.resolve().relative_to(pkg.dir().resolve()).as_posix()
    st = path.stat()
    bust = f"{int(st.st_mtime * 1000)}-{st.st_size}"
    return f"{media_url(pkg, rel)}?t={bust}"


def _scene_media(pkg: RallyPackage, rel: str) -> str:
    rel = str(rel or "").replace("\\", "/").lstrip("/")
    if not rel or rel.startswith("../") or "/../" in f"/{rel}":
        return ""
    path = (pkg.dir() / rel).resolve()
    root = pkg.dir().resolve()
    if path != root and root not in path.parents:
        return ""
    if not path.is_file():
        return ""
    return _bust(pkg, path)


def _client_state(pkg: RallyPackage, snap: dict) -> dict:
    path = snap["path"]
    scenes = []
    for row in snap.get("scenes") or []:
        item = dict(row)
        item["url"] = _scene_media(pkg, str(item.get("file") or ""))
        scenes.append(item)
    state = {
        "sceneId": snap["scene_id"],
        "src": _bust(pkg, path),
        "fps": snap["fps"],
        "frames": snap["frames"],
        "file": snap["file"],
        "hasAudio": bool(snap["audio"]),
        "onlyScene": bool(snap["only_scene"]),
        "canGenerate": bool(snap["can_generate"]),
        "generateReason": snap["generate_reason"],
        "label": snap["label"],
        "seconds": snap["seconds"],
        "scenes": scenes,
        "attempts": [
            {
                "file": row["file"],
                "url": _bust(pkg, row["path"]),
                "kind": row.get("kind") or "",
            }
            for row in snap["attempts"]
            if row["path"].is_file()
        ],
        "cutUrl": _bust(pkg, path),
        "lastFresh": snap["last_fresh"],
        "lastNudge": snap["last_nudge"],
        "undo": int(snap.get("undo") or 0),
        "editing": snap.get("editing") or "",
    }
    raw_bin = snap.get("bin")
    if raw_bin is not None:
        rows = []
        for item in raw_bin:
            row = dict(item)
            if row.get("kind") == "file" and row.get("file"):
                row["url"] = media_url(pkg, row["file"])
            rows.append(row)
        state["bin"] = rows
    return state


def _editor_state(
    pkg: RallyPackage,
    step_id: str,
    *,
    editing: str = "",
    highlight: str = "",
    changed: bool = False,
) -> dict:
    fresh = RallyPackage.load(pkg.dir())
    if editing:
        snap = attempt_view(fresh, step_id, editing)
    else:
        snap = editor_snapshot(fresh, step_id)
    state = _client_state(fresh, snap)
    state["ok"] = True
    state["changed"] = changed
    state["highlight"] = highlight
    return state


async def _body(request: Request) -> tuple[dict, JSONResponse | None]:
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return {}, JSONResponse({"error": "Bad request"}, status_code=400)
    if not isinstance(body, dict):
        return {}, JSONResponse({"error": "Bad request"}, status_code=400)
    return body, None


def _load_step(body: dict) -> tuple[RallyPackage | None, str, JSONResponse | None]:
    root, err = _rally_root(str(body.get("rally") or ""))
    if err is not None or root is None:
        return None, "", err
    step_id = str(body.get("step") or "").strip()
    if not step_id:
        return None, "", JSONResponse({"error": "Unknown step"}, status_code=400)
    try:
        pkg = RallyPackage.load(root)
        pkg.step(step_id)
    except KeyError:
        return None, "", JSONResponse({"error": "Unknown step"}, status_code=400)
    except Exception as exc:  # noqa: BLE001
        return None, "", JSONResponse({"error": str(exc)[:240]}, status_code=400)
    return pkg, step_id, None


_GEN_JS = r"""
  let pinChoice = "both";
  let pinNotes = {};
  let pinRows = [];
  let chosenDur = null;
  let durationChoices = [];
  let baseNote = "";
  function paintPinChoice() {
    document.querySelectorAll("#rb-scene-pin-choice button").forEach((btn) => {
      btn.classList.toggle("on", btn.dataset.pins === pinChoice);
    });
  }
  function renderPins() {
    const pins = document.getElementById("rb-scene-pins");
    if (!pins) return;
    pins.innerHTML = "";
    const start = document.getElementById("rb-scene-start");
    if (sheetKind === "new-scene" && start && !start.checked) {
      pins.hidden = true;
      return;
    }
    if (sheetKind === "regenerate" && pinChoice === "none") {
      pins.hidden = true;
      return;
    }
    const rows = (pinRows || []).filter((pin) => {
      if (sheetKind !== "regenerate" && sheetKind !== "redo-frames") return true;
      return pinChoice === "both" || pin.id === pinChoice;
    });
    if (!rows.length) {
      pins.hidden = true;
      return;
    }
    pins.hidden = false;
    rows.forEach((pin) => {
      const fig = document.createElement("figure");
      const img = document.createElement("img");
      img.src = pin.url;
      img.alt = pin.label || "";
      const cap = document.createElement("figcaption");
      cap.textContent = pin.label || "";
      fig.append(img, cap);
      pins.appendChild(fig);
    });
  }
  function nearestDuration(n) {
    const choices = durationChoices.map(Number).filter((sec) => sec >= 1 && sec <= 15);
    if (!choices.length) return null;
    let best = choices[0];
    let dist = Infinity;
    choices.forEach((sec) => {
      const gap = Math.abs(sec - n);
      if (gap < dist) {
        dist = gap;
        best = sec;
      }
    });
    return best;
  }
  function paintDuration() {
    const row = document.getElementById("rb-scene-dur-row");
    const slider = document.getElementById("rb-scene-dur");
    const scale = document.getElementById("rb-scene-dur-scale");
    const val = document.getElementById("rb-scene-dur-val");
    if (!row || !slider || !scale) return;
    const choices = durationChoices.map(Number).filter((sec) => sec >= 1 && sec <= 15);
    const show = sheetKind !== "nudge" && choices.length > 0;
    row.hidden = !show;
    if (!show) return;
    const picked = nearestDuration(Number(chosenDur));
    if (picked == null) return;
    chosenDur = picked;
    const min = choices[0];
    const max = choices[choices.length - 1];
    const span = Math.max(1, max - min);
    slider.min = String(min);
    slider.max = String(max);
    slider.step = "1";
    if (Number(slider.value) !== picked) slider.value = String(picked);
    slider.setAttribute("aria-valuemin", String(min));
    slider.setAttribute("aria-valuemax", String(max));
    slider.setAttribute("aria-valuenow", String(picked));
    slider.setAttribute("aria-valuetext", picked + " seconds");
    slider.style.setProperty("--p", String((picked - min) / span));
    if (val) val.textContent = picked + "s";
    const key = choices.join(",");
    if (scale.dataset.choices !== key) {
      scale.dataset.choices = key;
      scale.innerHTML = "";
      choices.forEach((sec) => {
        const mark = document.createElement("button");
        mark.type = "button";
        mark.textContent = String(sec);
        mark.dataset.dur = String(sec);
        mark.style.setProperty("--p", String((sec - min) / span));
        scale.appendChild(mark);
      });
    }
    scale.querySelectorAll("button").forEach((mark) => {
      const on = Number(mark.dataset.dur) === picked;
      mark.classList.toggle("on", on);
      mark.setAttribute("aria-pressed", on ? "true" : "false");
    });
  }
  function paintNote() {
    const note = document.getElementById("rb-scene-sheet-note");
    if (!note) return;
    let text = baseNote || "";
    if ((sheetKind === "regenerate" || sheetKind === "redo-frames") && pinNotes[pinChoice]) {
      text = pinNotes[pinChoice];
    }
    const row = document.getElementById("rb-scene-dur-row");
    if (row && !row.hidden && chosenDur != null) {
      const extra = "Asked for " + chosenDur + "s.";
      text = text ? text.replace(/\s+$/, "") + " " + extra : extra;
    }
    note.textContent = text;
  }
  function applyPinChoice() {
    paintPinChoice();
    const prompt = document.getElementById("rb-scene-prompt");
    if (sheetKind === "regenerate" || sheetKind === "redo-frames") {
      if (prompt) {
        prompt.placeholder = pinChoice === "first"
          ? "What this clip should do, starting on the first frame"
          : pinChoice === "last"
            ? "What this clip should do, ending on the last frame"
            : pinChoice === "none"
              ? "What this clip should do"
              : "What this clip should do between the first frame and the last";
      }
    }
    renderPins();
    paintNote();
  }
  function isDirty() {
    if (editingFile) {
      if (sounds.length) return true;
      if (clips.length !== 1) return true;
      const clip = clips[0];
      if (!clip) return true;
      if (clip.in !== 0 || clip.out !== cfg.frames || clip.mute) return true;
      return false;
    }
    const serverIds = (cfg.scenes || []).map((row) => row.id);
    const localIds = [];
    for (const run of sceneRuns()) {
      if (run.clips.length !== 1) return true;
      const clip = run.clips[0];
      if (clip.in !== 0 || clip.out !== clip.frames || clip.mute) return true;
      localIds.push(run.sceneId);
    }
    if (sounds.length) return true;
    return localIds.join("|") !== serverIds.join("|");
  }
  function paintWhole() {
    const el = document.getElementById("rb-scene-whole");
    if (!el) return;
    const rows = (cfg.scenes || []).filter((row) => !row.virtual);
    if (editingFile || rows.length < 2) {
      el.hidden = true;
      el.textContent = "";
      return;
    }
    const sec = rows.reduce((sum, row) => sum + (Number(row.seconds) || 0), 0);
    el.hidden = false;
    el.textContent = "These clips are already this take (" + sec.toFixed(1) + "s).";
  }
  function setStatus(text) {
    const el = document.getElementById("rb-scene-status");
    if (el) el.textContent = text || "";
  }
  function setBusy(on) {
    busy = !!on;
    paintHead();
  }
  function stopPoll() {
    if (pollTimer) {
      clearInterval(pollTimer);
      pollTimer = 0;
    }
  }
  function hideSheet() {
    const sheet = document.getElementById("rb-scene-sheet");
    if (sheet) sheet.hidden = true;
  }
  function jobUrl() {
    return "/api/scene-job?rally=" + encodeURIComponent(cfg.rally) + "&step=" + encodeURIComponent(cfg.step);
  }
  function stateUrl() {
    return "/api/scene-state?rally=" + encodeURIComponent(cfg.rally) + "&step=" + encodeURIComponent(cfg.step);
  }
  async function postJson(url, body) {
    const res = await fetch(url, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Request failed");
    return data;
  }
  async function guardGenerate() {
    if (busy || saving) return false;
    if (previewing || editingFile) {
      hint("Back to the clip in the cut before generating.");
      return false;
    }
    if (!(await syncSelection())) return false;
    if (isDirty()) {
      await commit();
      if (isDirty()) return false;
    }
    return true;
  }
  function attemptLabel(kind) {
    if (kind === "redo") return "Regenerate";
    if (kind === "nudge") return "Nudge";
    return "Take";
  }
  function renderAttempts(rows) {
    attemptRows = rows || [];
    const host = document.getElementById("rb-scene-results");
    const arrow = document.getElementById("rb-scene-arrow");
    const stage = document.querySelector("#rb-scene-shade .stage");
    if (!host) return;
    host.innerHTML = "";
    const rank = (kind) => (kind === "redo" ? 0 : kind === "nudge" ? 1 : 2);
    const list = attemptRows.slice().sort((a, b) => rank(a.kind) - rank(b.kind));
    if (arrow) arrow.hidden = list.length === 0;
    if (stage) stage.classList.toggle("has-results", list.length > 0);
    list.forEach((row) => {
      const card = document.createElement("div");
      card.className = "result" + (row.file && row.file === highlightFile ? " is-new" : "");
      const cap = document.createElement("div");
      cap.className = "cap";
      cap.textContent = attemptLabel(row.kind);
      const mon = document.createElement("div");
      mon.className = "monitor";
      const vid = document.createElement("video");
      vid.controls = true;
      vid.playsInline = true;
      vid.preload = "metadata";
      vid.muted = true;
      vid.src = row.url;
      vid.addEventListener("play", () => {
        silenceBehind();
        stopPlay();
        host.querySelectorAll("video").forEach((other) => {
          if (other !== vid) other.pause();
        });
      });
      mon.appendChild(vid);
      const acts = document.createElement("div");
      acts.className = "acts";
      const use = document.createElement("button");
      use.type = "button";
      use.textContent = "Use";
      use.dataset.tip = "Replaces this scene only and stitches the cut";
      use.title = use.dataset.tip;
      use.disabled = !!busy || saving;
      use.addEventListener("click", () => useAttempt(row.file));
      const edit = document.createElement("button");
      edit.type = "button";
      edit.textContent = "Edit";
      edit.dataset.tip = "Cut this take in the lane. Back to the cut returns to the timeline.";
      edit.title = edit.dataset.tip;
      edit.disabled = !!busy || saving;
      edit.addEventListener("click", () => editAttempt(row.file));
      const add = document.createElement("button");
      add.type = "button";
      add.textContent = "Add audio";
      add.dataset.tip = "Mix this clip's audio onto that take from the start, and keep the take's own audio.";
      add.title = add.dataset.tip;
      add.disabled = !!busy || saving;
      add.addEventListener("click", () => spliceAttempt(row.file));
      const del = document.createElement("button");
      del.type = "button";
      del.className = "danger";
      del.textContent = "Delete";
      del.dataset.tip = "Deletes this take. The clip in the cut stays.";
      del.title = del.dataset.tip;
      del.disabled = !!busy || saving;
      del.addEventListener("click", () => deleteAttempt(row.file));
      acts.append(use, edit, add, del);
      card.append(cap, mon, acts);
      host.appendChild(card);
    });
  }
  function clearPreview() {
    previewing = "";
    previewFile = "";
  }
  function applyState(state, reset) {
    if (!state || !state.src) return;
    const prevScene = (clips.find((c) => c.id === clipSel) || {}).sceneId || "";
    const prevLoc = locate(playhead);
    const anchor = {
      sceneId: state.sceneId || prevScene,
      local: (prevScene && state.sceneId === prevScene && prevLoc) ? prevLoc.local : 0,
    };
    cfg.sceneId = state.sceneId;
    cfg.src = state.src;
    cfg.fps = state.fps || cfg.fps;
    FPS = cfg.fps || 30;
    cfg.frames = state.frames;
    cfg.file = state.file;
    cfg.hasAudio = !!state.hasAudio;
    cfg.onlyScene = !!state.onlyScene;
    cfg.canGenerate = !!state.canGenerate;
    cfg.generateReason = state.generateReason || "";
    cfg.label = state.label || cfg.label;
    cfg.cutUrl = state.cutUrl || state.src;
    cfg.scenes = state.scenes || [];
    cfg.undo = state.undo || 0;
    editingFile = state.editing || "";
    if (state.highlight) highlightFile = state.highlight;
    if (state.bin) {
      bin = state.bin;
      renderBin();
    }
    if (state.lastFresh) cfg.lastFresh = state.lastFresh;
    if (state.lastNudge) cfg.lastNudge = state.lastNudge;
    const sub = document.getElementById("rb-scene-sub");
    if (sub) {
      const sec = Number(state.seconds);
      const shown = isFinite(sec) ? sec.toFixed(1) : "0.0";
      sub.textContent = (state.label || "Scene") + " · " + shown + "s";
    }
    renderAttempts(state.attempts || []);
    paintWhole();
    if (!reset) {
      lastSynced = state.sceneId || lastSynced;
      paintHead();
      return;
    }
    stopPlay();
    previewing = "";
    previewFile = "";
    const mon = document.querySelector("#rb-scene-shade .monitor");
    if (mon) mon.classList.remove("is-preview");
    const tl = document.querySelector("#rb-scene-shade .tl");
    if (tl) tl.classList.remove("is-locked");
    const note = document.getElementById("rb-scene-preview-note");
    if (note) note.textContent = "";
    const play = document.getElementById("rb-scene-play");
    if (play) play.textContent = "Play";
    undoStack = [];
    video.muted = true;
    delete video.dataset.rbSrc;
    if (standby) {
      standby.pause();
      standby.classList.add("standby");
      video.classList.remove("standby");
      standby.muted = true;
      delete standby.dataset.rbSrc;
      standby.removeAttribute("src");
      standby.load();
    }
    bootTimeline(anchor);
    renderClips();
    renderSounds();
    if (total()) showFrame(playhead);
  }
  async function fetchState() {
    const res = await fetch(stateUrl());
    const data = await res.json().catch(() => ({}));
    if (!res.ok) throw new Error(data.error || "Could not reload");
    return data;
  }
  async function applyResult(result) {
    const state = await fetchState();
    if (result.timeline_changed) {
      highlightFile = "";
      applyState(state, true);
      setStatus(result.note || "Added on the timeline.");
      return;
    }
    highlightFile = (result && result.file) || "";
    applyState(state, false);
    setStatus(result.note || "Cut kept.");
  }
  function paintPending(sceneId, detail) {
    if (!sceneId) return;
    const text = detail || "Generating…";
    let changed = false;
    clips.forEach((clip) => {
      if (clip.sceneId !== sceneId || !clip.pending) return;
      if (clip.detail === text) return;
      clip.detail = text;
      changed = true;
    });
    if (!changed) return;
    box.querySelectorAll(".eclip.is-pending").forEach((el) => {
      const clip = clips.find((item) => item.id === el.dataset.id);
      if (!clip || clip.sceneId !== sceneId) return;
      const sub = el.querySelector(".d");
      if (sub) sub.textContent = text;
    });
    paintHead();
  }
  function insertPendingClip(row, serverIds) {
    const frames = Math.max(0, row.frames | 0);
    const clip = {
      id: "c" + (++seq),
      sceneId: row.id,
      file: row.file || "",
      url: row.url || "",
      fps: row.fps || cfg.fps || 30,
      frames: frames,
      in: 0,
      out: frames,
      mute: false,
      audio: !!row.audio,
      fresh: false,
      virtual: !!row.virtual,
      pending: true,
      detail: row.detail || "Queued…",
    };
    let insertAt = clips.length;
    const pos = (serverIds || []).indexOf(row.id);
    if (pos > 0) {
      const before = new Set(serverIds.slice(0, pos));
      for (let i = clips.length - 1; i >= 0; i--) {
        if (before.has(clips[i].sceneId)) {
          insertAt = i + 1;
          break;
        }
      }
    }
    clips.splice(insertAt, 0, clip);
  }
  function adoptJobState(state, placeholderId) {
    const rows = (state && state.scenes) || [];
    const serverIds = rows.map((row) => row.id);
    const localIds = [];
    clips.forEach((clip) => {
      if (clip.sceneId && localIds[localIds.length - 1] !== clip.sceneId) {
        localIds.push(clip.sceneId);
      }
    });
    const stale = localIds.some((id) => serverIds.indexOf(id) < 0);
    if (!stale && !isDirty() && !editingFile) {
      applyState(state, true);
      return;
    }
    const row = rows.find((item) => item.id === placeholderId);
    if (row && !clips.some((clip) => clip.sceneId === row.id)) {
      insertPendingClip(row, serverIds);
      const cfgRows = (cfg.scenes || []).slice();
      if (!cfgRows.some((item) => item.id === row.id)) {
        let at = cfgRows.length;
        const pos = serverIds.indexOf(row.id);
        if (pos > 0) {
          const prev = serverIds[pos - 1];
          const idx = cfgRows.findIndex((item) => item.id === prev);
          if (idx >= 0) at = idx + 1;
        }
        cfgRows.splice(at, 0, row);
        cfg.scenes = cfgRows;
      }
      renderClips();
      renderSounds();
    }
    if (state && state.undo) cfg.undo = state.undo;
  }
  function absorbFinished(state, sceneIds) {
    const rows = (state && state.scenes) || [];
    const wanted = new Set((sceneIds || []).filter(Boolean));
    const byId = new Map(rows.map((row) => [row.id, row]));
    const under = locate(playhead);
    if (under && wanted.has(under.clip.sceneId)) stopPlay();
    clips.forEach((clip) => {
      if (!wanted.has(clip.sceneId)) return;
      const row = byId.get(clip.sceneId);
      if (!row) {
        clip._drop = true;
        return;
      }
      clip.file = row.file || "";
      clip.url = row.url || "";
      clip.frames = Math.max(0, row.frames | 0);
      clip.fps = row.fps || clip.fps;
      clip.audio = !!row.audio;
      clip.fresh = !!row.fresh;
      clip.pending = !!row.pending;
      clip.detail = row.detail || "";
      clip.virtual = !!row.virtual;
      if (!clip.pending) {
        clip.in = 0;
        clip.out = clip.frames;
      }
    });
    clips = clips.filter((clip) => !clip._drop);
    cfg.scenes = (cfg.scenes || []).map((row) => byId.get(row.id) || row).filter((row) => {
      if (!wanted.has(row.id)) return true;
      return byId.has(row.id);
    });
    cfg.undo = (state && state.undo) || cfg.undo || 0;
    if (state && state.attempts) renderAttempts(state.attempts);
    if (state && state.lastFresh) cfg.lastFresh = state.lastFresh;
    if (state && state.lastNudge) cfg.lastNudge = state.lastNudge;
    if (!clips.some((clip) => clip.id === clipSel)) {
      const stay = clips.find((clip) => clip.sceneId === cfg.sceneId) || clips[0] || null;
      clipSel = stay ? stay.id : null;
      if (stay) cfg.sceneId = stay.sceneId;
    }
    const n = total();
    if (playhead >= n) playhead = Math.max(0, n - 1);
    renderClips();
    renderSounds();
    const sel = clips.find((clip) => clip.id === clipSel);
    if (sel && wanted.has(sel.sceneId)) {
      delete video.dataset.rbSrc;
      if (standby) delete standby.dataset.rbSrc;
    }
    if (n) showFrame(playhead);
    else paintHead();
  }
  async function pollOnce() {
    if (!document.getElementById("rb-scene-shade")) {
      stopPoll();
      return;
    }
    let data = {};
    try {
      const res = await fetch(jobUrl());
      data = await res.json();
    } catch (err) {
      return;
    }
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    let running = false;
    const done = [];
    jobs.forEach((job) => {
      if (!job || !job.id) return;
      if (!knownJobs[job.id]) {
        knownJobs[job.id] = { sceneId: job.sceneId || "", applied: !job.running };
      }
      const known = knownJobs[job.id];
      if (job.sceneId) known.sceneId = job.sceneId;
      if (job.running) {
        running = true;
        known.applied = false;
        paintPending(known.sceneId, job.detail || "Generating…");
        return;
      }
      if (!known.applied) done.push(job);
    });
    if (done.length) {
      try {
        const state = await fetchState();
        const ids = done.map((job) => job.sceneId).filter(Boolean);
        if (!editingFile && !previewing) {
          if (isDirty()) absorbFinished(state, ids);
          else applyState(state, true);
        }
        done.forEach((job) => {
          if (knownJobs[job.id]) knownJobs[job.id].applied = true;
          if (job.error) hint(job.error);
        });
      } catch (err) {
        hint((err && err.message) || "Could not show that clip");
      }
    }
    const waiting = running || jobs.some((job) => {
      const known = job && job.id && knownJobs[job.id];
      return known && !known.applied;
    });
    if (!waiting) stopPoll();
  }
  function startPoll() {
    stopPoll();
    pollTimer = setInterval(pollOnce, 800);
    pollOnce();
  }
  async function focusScene(id) {
    if (!id || busy || saving) return;
    if (id === cfg.sceneId && !editingFile) {
      if (previewing) clearPreview();
      return;
    }
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    try {
      const data = await postJson("/api/scene-focus", {
        rally: cfg.rally,
        step: cfg.step,
        scene_id: id,
      });
      highlightFile = "";
      applyState(data, true);
      setStatus("");
    } catch (err) {
      hint((err && err.message) || "Could not open that scene");
    }
  }
  async function editAttempt(file) {
    if (!file || busy || saving) return;
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    setBusy(true);
    try {
      const data = await postJson("/api/scene-attempt-open", {
        rally: cfg.rally,
        step: cfg.step,
        file: file,
      });
      highlightFile = "";
      applyState(data, true);
      setStatus("This take is open in the cutter. Back to the cut returns to the timeline. Regenerate stays off until you do.");
    } catch (err) {
      hint((err && err.message) || "Could not open that take");
    } finally {
      setBusy(false);
    }
  }
  async function spliceAttempt(file) {
    if (!file || busy || saving) return;
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    setBusy(true);
    try {
      const data = await postJson("/api/scene-attempt-splice", {
        rally: cfg.rally,
        step: cfg.step,
        file: file,
      });
      applyState(data, true);
      setStatus("Added this clip's audio onto that take.");
    } catch (err) {
      hint((err && err.message) || "Could not add that audio");
    } finally {
      setBusy(false);
    }
  }
  async function useAttempt(file) {
    if (!file || busy || saving) return;
    if (isDirty()) {
      await commit();
      if (isDirty()) return;
    }
    setBusy(true);
    try {
      const data = await postJson("/api/scene-use", {
        rally: cfg.rally,
        step: cfg.step,
        file: file,
      });
      highlightFile = "";
      applyState(data, true);
      setStatus("This clip is now that take.");
    } catch (err) {
      hint((err && err.message) || "Could not use that take");
    } finally {
      setBusy(false);
    }
  }
  async function deleteAttempt(file) {
    if (!file || busy) return;
    setBusy(true);
    try {
      const data = await postJson("/api/scene-attempt-delete", {
        rally: cfg.rally,
        step: cfg.step,
        file: file,
      });
      if (highlightFile === file) highlightFile = "";
      applyState(data, false);
      setStatus("Deleted that take. The clip in the cut stays.");
    } catch (err) {
      hint((err && err.message) || "Could not delete that take");
    } finally {
      setBusy(false);
    }
  }
  async function openSheet(kind) {
    const pickedNow = clips.find((c) => c.id === clipSel);
    if (pickedNow && pickedNow.pending) {
      hint("That clip is still generating");
      return;
    }
    if (!(await guardGenerate())) return;
    sheetKind = kind;
    const title = document.getElementById("rb-scene-sheet-title");
    const note = document.getElementById("rb-scene-sheet-note");
    const pins = document.getElementById("rb-scene-pins");
    const choice = document.getElementById("rb-scene-pin-choice");
    const startRow = document.getElementById("rb-scene-start-row");
    const prompt = document.getElementById("rb-scene-prompt");
    const go = document.getElementById("rb-scene-sheet-go");
    const start = document.getElementById("rb-scene-start");
    pinRows = [];
    pinNotes = {};
    chosenDur = null;
    durationChoices = [];
    baseNote = "";
    pins.hidden = true;
    pins.innerHTML = "";
    const durRow = document.getElementById("rb-scene-dur-row");
    if (durRow) durRow.hidden = true;
    choice.hidden = kind !== "regenerate";
    startRow.hidden = kind !== "new-scene";
    start.checked = true;
    if (kind === "regenerate") paintPinChoice();
    if (kind === "nudge") {
      title.textContent = "Nudge";
      go.textContent = "Nudge";
      prompt.placeholder = "The one thing to change";
      prompt.value = cfg.lastNudge || "";
    } else if (kind === "new-scene") {
      title.textContent = "New scene";
      go.textContent = "Add scene";
      prompt.placeholder = "What the new scene should do";
      prompt.value = "";
    } else {
      title.textContent = "Regenerate";
      go.textContent = "Regenerate";
      prompt.placeholder = pinChoice === "first"
        ? "What this clip should do, starting on the first frame"
        : pinChoice === "last"
          ? "What this clip should do, ending on the last frame"
          : pinChoice === "none"
            ? "What this clip should do"
            : "What this clip should do between the first frame and the last";
      prompt.value = cfg.lastFresh || "";
    }
    note.textContent = "Reading this clip…";
    document.getElementById("rb-scene-sheet").hidden = false;
    prompt.focus();
    try {
      const info = await postJson("/api/scene-info", {
        rally: cfg.rally,
        step: cfg.step,
        scene_id: cfg.sceneId,
        kind: kind,
      });
      baseNote = info.note || "";
      pinRows = info.pins || [];
      pinNotes = info.notes || {};
      durationChoices = Array.isArray(info.durations) ? info.durations : [];
      chosenDur = info.duration == null ? null : Number(info.duration);
      paintDuration();
      applyPinChoice();
    } catch (err) {
      note.textContent = (err && err.message) || "Could not read this clip";
    }
  }
  async function submitSheet() {
    const promptEl = document.getElementById("rb-scene-prompt");
    const text = (promptEl.value || "").trim();
    if (!text) {
      hint(sheetKind === "nudge" ? "Say the one thing to change" : "Say what this clip should do");
      return;
    }
    if (sheetKind !== "nudge" && !(chosenDur >= 1 && chosenDur <= 15)) {
      hint("Pick a length from 1 to 15 seconds");
      return;
    }
    const go = document.getElementById("rb-scene-sheet-go");
    go.disabled = true;
    try {
      const sendKind = sheetKind === "regenerate"
        ? (pinChoice === "none" ? "redo-plain" : "redo-frames")
        : sheetKind;
      const payload = {
        rally: cfg.rally,
        step: cfg.step,
        scene_id: cfg.sceneId,
        kind: sendKind,
        prompt: text,
        start_frame: sheetKind === "new-scene"
          ? document.getElementById("rb-scene-start").checked
          : sendKind === "redo-frames",
        pins: sendKind === "redo-frames" ? pinChoice : "both",
      };
      if (sheetKind !== "nudge") payload.duration = chosenDur;
      const data = await postJson("/api/scene-job", payload);
      if (sheetKind === "nudge") cfg.lastNudge = text;
      else if (sheetKind !== "new-scene") cfg.lastFresh = text;
      hideSheet();
      const jobId = data.jobId || data.id;
      if (jobId) {
        knownJobs[jobId] = { sceneId: data.placeholderId || "", applied: false };
      }
      adoptJobState(data, data.placeholderId || "");
      startPoll();
    } catch (err) {
      hint((err && err.message) || "Could not start");
    } finally {
      go.disabled = false;
    }
  }
  document.getElementById("rb-gen-redo").onclick = () => openSheet("regenerate");
  document.getElementById("rb-gen-nudge").onclick = () => openSheet("nudge");
  document.getElementById("rb-gen-new").onclick = () => openSheet("new-scene");
  document.getElementById("rb-scene-sheet-cancel").onclick = () => hideSheet();
  document.getElementById("rb-scene-sheet-go").onclick = () => submitSheet();
  document.getElementById("rb-scene-form").addEventListener("submit", (e) => {
    e.preventDefault();
    submitSheet();
  });
  document.getElementById("rb-scene-sheet").addEventListener("click", (e) => {
    if (e.target && e.target.id === "rb-scene-sheet") hideSheet();
  });
  document.getElementById("rb-scene-start").addEventListener("change", () => {
    if (sheetKind !== "new-scene") return;
    renderPins();
  });
  document.querySelectorAll("#rb-scene-pin-choice button").forEach((btn) => {
    btn.addEventListener("click", () => {
      pinChoice = btn.dataset.pins || "both";
      applyPinChoice();
    });
  });
  const durSlider = document.getElementById("rb-scene-dur");
  if (durSlider) {
    durSlider.addEventListener("input", () => {
      chosenDur = nearestDuration(Number(durSlider.value));
      paintDuration();
      paintNote();
    });
  }
  const durScale = document.getElementById("rb-scene-dur-scale");
  if (durScale) {
    durScale.addEventListener("click", (e) => {
      const mark = e.target.closest("button");
      if (!mark || !mark.dataset.dur) return;
      chosenDur = Number(mark.dataset.dur);
      paintDuration();
      paintNote();
    });
  }
  renderAttempts(cfg.attempts || []);
  paintWhole();
  if (!cfg.canGenerate && cfg.generateReason) setStatus(cfg.generateReason);
  fetch(jobUrl()).then((res) => res.json()).then((data) => {
    if (!data || !document.getElementById("rb-scene-shade")) return;
    const jobs = Array.isArray(data.jobs) ? data.jobs : [];
    let running = false;
    jobs.forEach((job) => {
      if (!job || !job.id) return;
      knownJobs[job.id] = { sceneId: job.sceneId || "", applied: !job.running };
      if (job.running) {
        running = true;
        paintPending(job.sceneId, job.detail || "Generating…");
      }
    });
    if (running) startPoll();
  }).catch(() => {});
"""


def open_scene_editor(pkg: RallyPackage, step_id: str, media_url_for) -> None:
    """Full-screen editor. Cuts, joins, and audio save on their own."""
    try:
        snap = editor_snapshot(pkg, step_id)
    except SceneError as exc:
        ui.notify(str(exc), type="warning")
        return
    state = _client_state(pkg, snap)
    bin_items = []
    for item in editor_bin(pkg, step_id, snap["file"], float(snap["fps"])):
        row = dict(item)
        if row.get("kind") == "file":
            row["url"] = media_url_for(pkg.dir() / row["file"])
        bin_items.append(row)
    label = state["label"]
    seconds = float(state["seconds"])
    html = f"""
        <div id="rb-scene-shade" class="rb-scene-shade">
          <div class="rb-scene-ed" role="dialog" aria-label="Edit scene">
            <div class="head">
              <div class="head-left">
                <div class="head-titles">
                  <h2>Edit</h2>
                  <span class="sub" id="rb-scene-sub">{label} · {seconds:.1f}s</span>
                </div>
                <span class="whole" id="rb-scene-whole" hidden></span>
              </div>
              <div class="head-right">
                <button type="button" id="rb-scene-back-cut" hidden>Back to the cut</button>
                <button type="button" class="close" id="rb-scene-close">Close</button>
              </div>
            </div>
            <div class="stage">
              <div class="stage-gap"></div>
              <div class="monitor">
                <video id="rb-scene-video" playsinline preload="auto"></video>
                <video id="rb-scene-video-next" class="standby" playsinline preload="auto" muted></video>
                <div class="f" id="rb-scene-frame">0</div>
              </div>
              <div class="beside" id="rb-scene-beside">
                <div class="arrow" id="rb-scene-arrow" hidden>
                  <svg viewBox="0 0 48 24" width="48" height="24" aria-hidden="true">
                    <path d="M2 12h36M30 4l10 8-10 8" fill="none" stroke="currentColor" stroke-width="2.5" stroke-linecap="round" stroke-linejoin="round"/>
                  </svg>
                </div>
                <div class="results" id="rb-scene-results"></div>
              </div>
            </div>
            <div class="work">
              <div class="gen">
                <span class="gen-clip" id="rb-gen-clip">{label}</span>
                <button type="button" id="rb-gen-redo">Regenerate</button>
                <button type="button" id="rb-gen-nudge">Nudge</button>
                <button type="button" id="rb-gen-new">New scene</button>
                <button type="button" class="help" id="rb-gen-help" title="What these do" aria-label="What Regenerate, Nudge, and New scene do">?</button>
                <span class="status" id="rb-scene-status"></span>
              </div>
            </div>
            <div class="transport">
              <button type="button" id="rb-scene-back">◀ 1 frame</button>
              <button type="button" id="rb-scene-play">Play</button>
              <button type="button" id="rb-scene-fwd">1 frame ▶</button>
              <span class="tc" id="rb-scene-tc">0:00.00 · frame 0</span>
              <span class="spacer"></span>
              <button type="button" class="yank" id="rb-scene-yank" title="Lift this clip's audio onto the track">Yank audio</button>
              <button type="button" id="rb-scene-add" title="Drop a sound file at the playhead">Add sound</button>
              <input id="rb-scene-file" type="file" accept=".m4a,.mp3,.wav,.aac,.ogg,audio/*">
              <button type="button" id="rb-scene-upload" title="Add this video after the selected clip">Upload clip</button>
              <input id="rb-scene-clip-file" type="file" accept="video/mp4,video/quicktime,video/webm,.mp4,.mov,.m4v,.webm">
              <button type="button" id="rb-scene-undo" disabled title="Undo the last edit">Undo</button>
              <button type="button" class="cut" id="rb-scene-cut">Cut</button>
              <button type="button" id="rb-scene-join" title="Merge this clip with the next one. The whole cut is already the take.">Join next</button>
              <button type="button" id="rb-scene-del">Delete</button>
            </div>
            <div class="tl">
              <div class="ruler" id="rb-scene-ruler"></div>
              <div class="lanes" id="rb-scene-lanes">
                <div class="lane" id="rb-scene-lane">
                  <div class="eclips" id="rb-scene-clips"></div>
                </div>
                <div class="alane" id="rb-scene-alane" aria-label="Audio track"></div>
                <div class="playhead" id="rb-scene-head"></div>
              </div>
              <div class="abin" id="rb-scene-bin"></div>
              <div class="hint" id="rb-scene-hint"></div>
            </div>
            <div class="sheet" id="rb-help-sheet" hidden>
              <div class="sheet-card">
                <h3>New clips</h3>
                <p><b>Regenerate</b> makes another take of this clip. You choose which frames to hold. It is inserted right after this clip and marked New.</p>
                <p><b>Nudge</b> keeps this clip and changes the one thing you describe. It lands in the same place.</p>
                <p><b>New scene</b> adds a different clip after this one. It can start on this clip’s last frame.</p>
                <p><b>Upload clip</b> puts a video you already have on the timeline, right after the selected clip.</p>
                <p>On Regenerate and New scene, Length is any second from 1 to 15. Nudge keeps this clip’s length.</p>
                <div class="sheet-acts">
                  <button type="button" id="rb-help-close">Close</button>
                </div>
              </div>
            </div>
            <div class="sheet" id="rb-scene-sheet" hidden>
              <form class="sheet-card" id="rb-scene-form">
                <h3 id="rb-scene-sheet-title">Regenerate</h3>
                <div class="pin-choice" id="rb-scene-pin-choice" hidden>
                  <button type="button" data-pins="first">First</button>
                  <button type="button" data-pins="last">Last</button>
                  <button type="button" data-pins="both" class="on">Both</button>
                  <button type="button" data-pins="none">None</button>
                </div>
                <div class="dur-choice" id="rb-scene-dur-row" hidden>
                  <div class="dur-head">
                    <span class="dur-k">Length</span>
                    <span class="dur-v" id="rb-scene-dur-val"></span>
                  </div>
                  <input type="range" id="rb-scene-dur" min="1" max="15" step="1" value="6" aria-label="Length in seconds">
                  <div class="dur-scale" id="rb-scene-dur-scale"></div>
                </div>
                <div class="pins" id="rb-scene-pins" hidden></div>
                <p class="note" id="rb-scene-sheet-note"></p>
                <textarea id="rb-scene-prompt"></textarea>
                <label class="check" id="rb-scene-start-row" hidden>
                  <input type="checkbox" id="rb-scene-start" checked>
                  Start on this clip’s last frame
                </label>
                <div class="sheet-acts">
                  <button type="button" id="rb-scene-sheet-cancel">Cancel</button>
                  <button type="button" class="primary" id="rb-scene-sheet-go">Make</button>
                </div>
              </form>
            </div>
          </div>
        </div>
    """
    payload = json.dumps(
        {
            "html": html,
            "cfg": {
                "rally": pkg.name,
                "step": step_id,
                "bin": bin_items,
                "key": f"{pkg.name}:{step_id}",
                **state,
            },
        }
    ).replace("<", "\\u003c")
    script = _JS.replace("__PAYLOAD__", payload).replace("__GEN__", _GEN_JS)
    ui.run_javascript(script)


