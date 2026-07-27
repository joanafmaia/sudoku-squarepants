/**
 * Royalty-free procedural background music (Web Audio API).
 * Tropical / cartoon loop — no audio files, no copyrighted themes.
 */

const BPM = 104;
const BEAT_SEC = 60 / BPM;
const LOOP_BEATS = 8;
const LOOKAHEAD_SEC = 0.18;
const SCHEDULER_MS = 28;

/** @type {number} midi note */
const PATTERN = [
  { m: 60, b: 0, d: 0.42 },
  { m: 64, b: 0.5, d: 0.42 },
  { m: 67, b: 1, d: 0.42 },
  { m: 72, b: 1.5, d: 0.55 },
  { m: 67, b: 2.2, d: 0.38 },
  { m: 64, b: 2.65, d: 0.38 },
  { m: 62, b: 3.1, d: 0.38 },
  { m: 60, b: 3.55, d: 0.5 },
  { m: 55, b: 4.1, d: 0.42 },
  { m: 59, b: 4.6, d: 0.42 },
  { m: 62, b: 5.1, d: 0.42 },
  { m: 67, b: 5.6, d: 0.55 },
  { m: 64, b: 6.3, d: 0.38 },
  { m: 60, b: 6.75, d: 0.7 },
];

function midiToFreq(midi) {
  return 440 * 2 ** ((midi - 69) / 12);
}

let getCtx = null;
let isEnabled = () => true;
let masterFilter = null;
let masterGain = null;
let schedulerTimer = null;
let nextLoopAt = 0;
let running = false;
let armed = false;
let paused = false;

function playMelodyNote(ctx, freq, t0, durSec) {
  const osc = ctx.createOscillator();
  const env = ctx.createGain();
  osc.type = "triangle";
  osc.frequency.setValueAtTime(freq, t0);
  env.gain.setValueAtTime(0.0001, t0);
  env.gain.exponentialRampToValueAtTime(0.11, t0 + 0.035);
  env.gain.exponentialRampToValueAtTime(0.0001, t0 + durSec);
  osc.connect(env);
  env.connect(masterFilter);
  osc.start(t0);
  osc.stop(t0 + durSec + 0.06);
}

function playBassNote(ctx, freq, t0, durSec) {
  const osc = ctx.createOscillator();
  const env = ctx.createGain();
  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, t0);
  env.gain.setValueAtTime(0.0001, t0);
  env.gain.exponentialRampToValueAtTime(0.06, t0 + 0.05);
  env.gain.exponentialRampToValueAtTime(0.0001, t0 + durSec);
  osc.connect(env);
  env.connect(masterFilter);
  osc.start(t0);
  osc.stop(t0 + durSec + 0.08);
}

function ensureGraph(ctx) {
  if (masterGain) return;
  masterFilter = ctx.createBiquadFilter();
  masterFilter.type = "lowpass";
  masterFilter.frequency.value = 2400;
  masterFilter.Q.value = 0.6;
  masterGain = ctx.createGain();
  masterGain.gain.value = 0.38;
  masterFilter.connect(masterGain);
  masterGain.connect(ctx.destination);
}

function scheduleLoop(ctx, loopStart) {
  for (const { m, b, d } of PATTERN) {
    playMelodyNote(ctx, midiToFreq(m), loopStart + b * BEAT_SEC, d * BEAT_SEC);
  }
  playBassNote(ctx, midiToFreq(48), loopStart, BEAT_SEC * 3.6);
  playBassNote(ctx, midiToFreq(55), loopStart + BEAT_SEC * 4, BEAT_SEC * 3.6);
}

function schedulerTick() {
  if (!running || paused || !isEnabled()) return;
  const ctx = getCtx?.();
  if (!ctx) return;
  ensureGraph(ctx);
  const horizon = ctx.currentTime + LOOKAHEAD_SEC;
  while (nextLoopAt < horizon) {
    scheduleLoop(ctx, nextLoopAt);
    nextLoopAt += LOOP_BEATS * BEAT_SEC;
  }
}

function startScheduler() {
  if (running || paused || !isEnabled()) return;
  const ctx = getCtx?.();
  if (!ctx) return;
  ensureGraph(ctx);
  running = true;
  nextLoopAt = ctx.currentTime + 0.06;
  schedulerTick();
  if (!schedulerTimer) {
    schedulerTimer = setInterval(schedulerTick, SCHEDULER_MS);
  }
}

function stopScheduler() {
  running = false;
  if (schedulerTimer) {
    clearInterval(schedulerTimer);
    schedulerTimer = null;
  }
}

export function configureProceduralBgm(options) {
  getCtx = options.getCtx;
  isEnabled = options.isEnabled || (() => true);
}

/** Call after the first user gesture (browser autoplay policy). */
export function armProceduralBgm() {
  armed = true;
  if (isEnabled() && !paused) startScheduler();
}

export function syncProceduralBgmEnabled(enabled) {
  if (enabled && armed && !paused) startScheduler();
  else stopScheduler();
}

export function pauseProceduralBgm() {
  paused = true;
  stopScheduler();
}

export function resumeProceduralBgm() {
  if (!armed || !isEnabled()) return;
  paused = false;
  startScheduler();
}

export function stopProceduralBgm() {
  armed = false;
  paused = false;
  stopScheduler();
  masterFilter = null;
  masterGain = null;
}
