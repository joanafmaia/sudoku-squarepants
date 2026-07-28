/**
 * Looping background track for the Activity (Clownfish Capers).
 */

const TRACK_SRC = "/audio/clownfish-capers.mp3";
const MASTER_VOLUME = 0.42;

export const TRACK_META = {
  emoji: "🎵",
  label: "Clownfish Capers",
};

let audio = null;
let armed = false;
let paused = false;
let isEnabled = () => true;

function ensureAudio() {
  if (!audio) {
    audio = new Audio(TRACK_SRC);
    audio.loop = true;
    audio.preload = "auto";
    audio.volume = MASTER_VOLUME;
  }
  return audio;
}

async function playTrack() {
  if (!armed || !isEnabled() || paused) return;
  const el = ensureAudio();
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
  return TRACK_META;
}

export function armTrackBgm() {
  armed = true;
  if (isEnabled() && !paused) void playTrack();
}

export function syncTrackBgmEnabled(enabled) {
  const el = ensureAudio();
  el.volume = enabled ? MASTER_VOLUME : 0;
  if (enabled && armed && !paused) void playTrack();
  else el.pause();
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
