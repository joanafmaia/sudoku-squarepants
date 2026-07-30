/**
 * Looping background tracks for the Activity.
 * Drop MP3s in public/audio/ — each selection plays on loop until changed.
 */

const MASTER_VOLUME = 0.42;
const TRACK_INDEX_KEY = "thcoku_music_track";

/** @type {readonly { id: string; src: string; emoji: string; label: string }[]} */
export const TRACKS = [
  {
    id: "clownfish-capers",
    src: "/audio/clownfish-capers.mp3",
    emoji: "🐠",
    label: "Clownfish Capers",
  },
  {
    id: "rake-hornpipe",
    src: "/audio/rake-hornpipe.mp3",
    emoji: "⚓",
    label: "The Rake Hornpipe",
  },
  {
    id: "grass-skirt-chase",
    src: "/audio/grass-skirt-chase.mp3",
    emoji: "🌴",
    label: "Grass Skirt Chase",
  },
  {
    id: "hula-dancers",
    src: "/audio/hula-dancers.mp3",
    emoji: "🌺",
    label: "Hula Dancers",
  },
];

let audio = null;
/** @type {string | null} */
let loadedSrc = null;
let armed = false;
let paused = false;
let trackIndex = readStoredTrackIndex();
let isEnabled = () => true;

function readStoredTrackIndex() {
  try {
    const raw = localStorage.getItem(TRACK_INDEX_KEY);
    const n = Number(raw);
    if (Number.isInteger(n) && n >= 0 && n < TRACKS.length) return n;
  } catch {
    /* localStorage disabled */
  }
  return 0;
}

function persistTrackIndex() {
  try {
    localStorage.setItem(TRACK_INDEX_KEY, String(trackIndex));
  } catch {
    /* localStorage disabled */
  }
}

function currentTrack() {
  return TRACKS[trackIndex] || TRACKS[0];
}

function shouldPlay() {
  return armed && isEnabled() && !paused;
}

function ensureAudio() {
  const track = currentTrack();
  if (!audio) {
    audio = new Audio(track.src);
    audio.loop = true;
    audio.preload = "auto";
    audio.volume = MASTER_VOLUME;
    loadedSrc = track.src;
    audio.addEventListener("error", () => {
      console.warn("[Thcoku] music track failed to load", currentTrack().src);
    });
    return audio;
  }
  if (loadedSrc !== track.src) {
    audio.pause();
    audio.src = track.src;
    audio.loop = true;
    audio.load();
    audio.currentTime = 0;
    loadedSrc = track.src;
  }
  return audio;
}

async function playTrack() {
  if (!shouldPlay()) return;
  const el = ensureAudio();
  el.loop = true;
  el.volume = MASTER_VOLUME;
  if (!el.paused && !el.ended) return;
  try {
    await el.play();
  } catch {
    /* autoplay blocked until user gesture */
  }
}

export function configureTrackBgm(options) {
  isEnabled = options.isEnabled || (() => true);
}

export function getTrackMeta() {
  const t = currentTrack();
  return {
    id: t.id,
    emoji: t.emoji,
    label: t.label,
    index: trackIndex,
    total: TRACKS.length,
  };
}

export function getTrackIndex() {
  return trackIndex;
}

export function listTracks() {
  return TRACKS.map((t, i) => ({ ...t, index: i }));
}

/** Switch to an absolute track index and start looping it (if music is on). */
export function setTrackIndex(index) {
  const n = Number(index);
  if (!Number.isFinite(n)) return getTrackMeta();
  const next = ((Math.trunc(n) % TRACKS.length) + TRACKS.length) % TRACKS.length;
  if (next !== trackIndex) {
    trackIndex = next;
    persistTrackIndex();
  }
  ensureAudio();
  if (shouldPlay()) void playTrack();
  return getTrackMeta();
}

/** Cycle to the next track; keeps looping the new choice. */
export function cycleTrack(delta = 1) {
  const step = Number(delta);
  return setTrackIndex(trackIndex + (Number.isFinite(step) && step !== 0 ? step : 1));
}

export function armTrackBgm() {
  armed = true;
  ensureAudio();
  if (shouldPlay()) void playTrack();
}

export function syncTrackBgmEnabled(enabled) {
  const el = ensureAudio();
  if (!enabled) {
    el.volume = 0;
    el.pause();
    return;
  }
  el.volume = MASTER_VOLUME;
  if (shouldPlay()) void playTrack();
}

export function pauseTrackBgm() {
  paused = true;
  audio?.pause();
}

export function resumeTrackBgm() {
  if (!armed || !isEnabled()) return;
  paused = false;
  void playTrack();
}

export function stopTrackBgm() {
  armed = false;
  paused = false;
  if (!audio) return;
  audio.pause();
  audio.currentTime = 0;
}
