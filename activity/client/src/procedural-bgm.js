/**
 * Royalty-free procedural ocean ambience (Web Audio API).
 * Gentle waves, soft surf, and occasional bubbles — no audio files.
 */

const LOOKAHEAD_SEC = 0.35;
const SCHEDULER_MS = 45;
const MASTER_LEVEL = 0.4;

let getCtx = null;
let isEnabled = () => true;
let masterFilter = null;
let masterGain = null;
let schedulerTimer = null;
let nextEventAt = 0;
let running = false;
let armed = false;
let paused = false;
let swellPhase = 0;

/** @type {AudioNode[]} */
let ambientNodes = [];
let waveBedGain = null;
let deepBedGain = null;

function ensureGraph(ctx) {
  if (masterGain) return;
  masterFilter = ctx.createBiquadFilter();
  masterFilter.type = "lowpass";
  masterFilter.frequency.value = 2800;
  masterFilter.Q.value = 0.35;
  masterGain = ctx.createGain();
  masterGain.gain.value = MASTER_LEVEL;
  masterFilter.connect(masterGain);
  masterGain.connect(ctx.destination);
}

function createNoiseBuffer(ctx, seconds, color = "brown") {
  const n = Math.ceil(ctx.sampleRate * seconds);
  const buffer = ctx.createBuffer(1, n, ctx.sampleRate);
  const data = buffer.getChannelData(0);
  let last = 0;
  for (let i = 0; i < n; i++) {
    const white = Math.random() * 2 - 1;
    if (color === "pink") {
      last = 0.98 * last + 0.02 * white;
      data[i] = last * 2.2;
    } else {
      last = (last + 0.02 * white) / 1.02;
      data[i] = last * 3.4;
    }
  }
  return buffer;
}

function track(node) {
  ambientNodes.push(node);
  return node;
}

function stopAmbientLayers() {
  for (const node of ambientNodes) {
    try {
      if (typeof node.stop === "function") node.stop();
      node.disconnect?.();
    } catch {
      /* already stopped */
    }
  }
  ambientNodes = [];
  waveBedGain = null;
  deepBedGain = null;
}

function startContinuousOcean(ctx, t0) {
  stopAmbientLayers();

  const wave = track(ctx.createBufferSource());
  wave.buffer = createNoiseBuffer(ctx, 4, "brown");
  wave.loop = true;

  const waveLp = track(ctx.createBiquadFilter());
  waveLp.type = "lowpass";
  waveLp.frequency.value = 420;
  waveLp.Q.value = 0.5;

  waveBedGain = track(ctx.createGain());
  waveBedGain.gain.value = 0.14;

  wave.connect(waveLp);
  waveLp.connect(waveBedGain);
  waveBedGain.connect(masterFilter);
  wave.start(t0);

  const deep = track(ctx.createBufferSource());
  deep.buffer = createNoiseBuffer(ctx, 5, "pink");
  deep.loop = true;

  const deepLp = track(ctx.createBiquadFilter());
  deepLp.type = "lowpass";
  deepLp.frequency.value = 180;
  deepLp.Q.value = 0.4;

  deepBedGain = track(ctx.createGain());
  deepBedGain.gain.value = 0.05;

  deep.connect(deepLp);
  deepLp.connect(deepBedGain);
  deepBedGain.connect(masterFilter);
  deep.start(t0);

  const shimmer = track(ctx.createBufferSource());
  shimmer.buffer = createNoiseBuffer(ctx, 2, "pink");
  shimmer.loop = true;

  const shimmerLp = track(ctx.createBiquadFilter());
  shimmerLp.type = "bandpass";
  shimmerLp.frequency.value = 1400;
  shimmerLp.Q.value = 0.8;

  const shimmerGain = track(ctx.createGain());
  shimmerGain.gain.value = 0.018;

  shimmer.connect(shimmerLp);
  shimmerLp.connect(shimmerGain);
  shimmerGain.connect(masterFilter);
  shimmer.start(t0);
}

function updateSwell(ctx) {
  swellPhase += 0.0028;
  const swell = 0.11 + 0.09 * Math.sin(swellPhase);
  const deep = 0.04 + 0.02 * Math.sin(swellPhase * 0.6 + 1.2);
  if (waveBedGain) {
    waveBedGain.gain.setTargetAtTime(swell, ctx.currentTime, 0.12);
  }
  if (deepBedGain) {
    deepBedGain.gain.setTargetAtTime(deep, ctx.currentTime, 0.18);
  }
}

function playSurfWash(ctx, t0) {
  const dur = 1.6 + Math.random() * 1.4;
  const noise = ctx.createBufferSource();
  noise.buffer = createNoiseBuffer(ctx, dur, "brown");

  const bp = ctx.createBiquadFilter();
  bp.type = "bandpass";
  bp.frequency.setValueAtTime(280, t0);
  bp.frequency.exponentialRampToValueAtTime(720, t0 + dur * 0.35);
  bp.frequency.exponentialRampToValueAtTime(220, t0 + dur);
  bp.Q.value = 0.9;

  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, t0);
  env.gain.exponentialRampToValueAtTime(0.09, t0 + 0.25);
  env.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

  noise.connect(bp);
  bp.connect(env);
  env.connect(masterFilter);
  noise.start(t0);
  noise.stop(t0 + dur + 0.05);
}

function playBubble(ctx, t0) {
  const freq = 520 + Math.random() * 900;
  const dur = 0.06 + Math.random() * 0.08;

  const osc = ctx.createOscillator();
  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, t0);
  osc.frequency.exponentialRampToValueAtTime(freq * 1.35, t0 + dur);

  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, t0);
  env.gain.exponentialRampToValueAtTime(0.028, t0 + 0.012);
  env.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

  osc.connect(env);
  env.connect(masterFilter);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

function scheduleOceanEvents(ctx, horizon) {
  while (nextEventAt < horizon) {
    const roll = Math.random();
    if (roll < 0.22) {
      playSurfWash(ctx, nextEventAt);
      nextEventAt += 5.5 + Math.random() * 7;
    } else {
      playBubble(ctx, nextEventAt);
      nextEventAt += 0.35 + Math.random() * 1.6;
    }
  }
}

function schedulerTick() {
  if (!running || paused || !isEnabled()) return;
  const ctx = getCtx?.();
  if (!ctx) return;
  ensureGraph(ctx);
  updateSwell(ctx);
  const horizon = ctx.currentTime + LOOKAHEAD_SEC;
  scheduleOceanEvents(ctx, horizon);
}

function startScheduler() {
  if (running || paused || !isEnabled()) return;
  const ctx = getCtx?.();
  if (!ctx) return;
  ensureGraph(ctx);
  if (!waveBedGain) {
    startContinuousOcean(ctx, ctx.currentTime + 0.02);
    nextEventAt = ctx.currentTime + 0.15;
    swellPhase = Math.random() * Math.PI * 2;
  }
  running = true;
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
  stopAmbientLayers();
  nextEventAt = 0;
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
  if (masterGain) {
    masterGain.gain.value = enabled ? MASTER_LEVEL : 0;
  }
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
