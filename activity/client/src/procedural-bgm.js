/**
 * Royalty-free procedural ocean ambience (Web Audio API).
 * Multiple presets — gentle waves, surf, reef bubbles; no audio files.
 */

const LOOKAHEAD_SEC = 0.35;
const SCHEDULER_MS = 45;
const MASTER_LEVEL = 0.4;

/** @type {readonly { emoji: string; label: string; waveLp: number; deepLp: number; shimmerLp: number; waveGain: number; deepGain: number; shimmerGain: number; swellSpeed: number; surfChance: number; bubbleGap: [number, number]; surfGap: [number, number]; filterHz: number }[]} */
export const OCEAN_PRESETS = [
  {
    emoji: "🌊",
    label: "Calm lagoon",
    waveLp: 420,
    deepLp: 180,
    shimmerLp: 1400,
    waveGain: 0.14,
    deepGain: 0.05,
    shimmerGain: 0.018,
    swellSpeed: 0.0028,
    surfChance: 0.22,
    bubbleGap: [0.35, 1.6],
    surfGap: [5.5, 7],
    filterHz: 2800,
  },
  {
    emoji: "🐚",
    label: "Shallow reef",
    waveLp: 640,
    deepLp: 260,
    shimmerLp: 1800,
    waveGain: 0.11,
    deepGain: 0.04,
    shimmerGain: 0.03,
    swellSpeed: 0.0034,
    surfChance: 0.1,
    bubbleGap: [0.25, 0.9],
    surfGap: [7, 10],
    filterHz: 3400,
  },
  {
    emoji: "🌙",
    label: "Deep ocean",
    waveLp: 260,
    deepLp: 110,
    shimmerLp: 900,
    waveGain: 0.16,
    deepGain: 0.07,
    shimmerGain: 0.01,
    swellSpeed: 0.002,
    surfChance: 0.3,
    bubbleGap: [0.5, 2.2],
    surfGap: [4, 6],
    filterHz: 2200,
  },
  {
    emoji: "🏖️",
    label: "Gentle surf",
    waveLp: 520,
    deepLp: 210,
    shimmerLp: 1200,
    waveGain: 0.15,
    deepGain: 0.055,
    shimmerGain: 0.014,
    swellSpeed: 0.0042,
    surfChance: 0.38,
    bubbleGap: [0.45, 1.8],
    surfGap: [3.2, 5],
    filterHz: 3000,
  },
];

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
let presetIndex = 0;

/** @type {AudioNode[]} */
let ambientNodes = [];
let waveBedGain = null;
let deepBedGain = null;

function currentPreset() {
  return OCEAN_PRESETS[presetIndex] || OCEAN_PRESETS[0];
}

function ensureGraph(ctx) {
  if (!masterGain) {
    masterFilter = ctx.createBiquadFilter();
    masterGain = ctx.createGain();
    masterGain.gain.value = MASTER_LEVEL;
    masterFilter.connect(masterGain);
    masterGain.connect(ctx.destination);
  }
  const p = currentPreset();
  masterFilter.type = "lowpass";
  masterFilter.frequency.setTargetAtTime(p.filterHz, ctx.currentTime, 0.08);
  masterFilter.Q.value = 0.35;
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
  const p = currentPreset();

  const wave = track(ctx.createBufferSource());
  wave.buffer = createNoiseBuffer(ctx, 4, "brown");
  wave.loop = true;

  const waveLp = track(ctx.createBiquadFilter());
  waveLp.type = "lowpass";
  waveLp.frequency.value = p.waveLp;
  waveLp.Q.value = 0.5;

  waveBedGain = track(ctx.createGain());
  waveBedGain.gain.value = p.waveGain;

  wave.connect(waveLp);
  waveLp.connect(waveBedGain);
  waveBedGain.connect(masterFilter);
  wave.start(t0);

  const deep = track(ctx.createBufferSource());
  deep.buffer = createNoiseBuffer(ctx, 5, "pink");
  deep.loop = true;

  const deepLp = track(ctx.createBiquadFilter());
  deepLp.type = "lowpass";
  deepLp.frequency.value = p.deepLp;
  deepLp.Q.value = 0.4;

  deepBedGain = track(ctx.createGain());
  deepBedGain.gain.value = p.deepGain;

  deep.connect(deepLp);
  deepLp.connect(deepBedGain);
  deepBedGain.connect(masterFilter);
  deep.start(t0);

  const shimmer = track(ctx.createBufferSource());
  shimmer.buffer = createNoiseBuffer(ctx, 2, "pink");
  shimmer.loop = true;

  const shimmerLp = track(ctx.createBiquadFilter());
  shimmerLp.type = "bandpass";
  shimmerLp.frequency.value = p.shimmerLp;
  shimmerLp.Q.value = 0.8;

  const shimmerGain = track(ctx.createGain());
  shimmerGain.gain.value = p.shimmerGain;

  shimmer.connect(shimmerLp);
  shimmerLp.connect(shimmerGain);
  shimmerGain.connect(masterFilter);
  shimmer.start(t0);
}

function updateSwell(ctx) {
  const p = currentPreset();
  swellPhase += p.swellSpeed;
  const swell = p.waveGain * (0.78 + 0.22 * Math.sin(swellPhase));
  const deep = p.deepGain * (0.75 + 0.25 * Math.sin(swellPhase * 0.6 + 1.2));
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
  const p = currentPreset();
  bp.frequency.setValueAtTime(p.waveLp * 0.7, t0);
  bp.frequency.exponentialRampToValueAtTime(p.waveLp * 1.6, t0 + dur * 0.35);
  bp.frequency.exponentialRampToValueAtTime(p.waveLp * 0.55, t0 + dur);
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
  const p = currentPreset();
  const freq = 420 + Math.random() * (p.shimmerLp * 0.6);
  const dur = 0.06 + Math.random() * 0.08;

  const osc = ctx.createOscillator();
  osc.type = "sine";
  osc.frequency.setValueAtTime(freq, t0);
  osc.frequency.exponentialRampToValueAtTime(freq * 1.35, t0 + dur);

  const env = ctx.createGain();
  env.gain.setValueAtTime(0.0001, t0);
  env.gain.exponentialRampToValueAtTime(0.02 + p.shimmerGain, t0 + 0.012);
  env.gain.exponentialRampToValueAtTime(0.0001, t0 + dur);

  osc.connect(env);
  env.connect(masterFilter);
  osc.start(t0);
  osc.stop(t0 + dur + 0.02);
}

function scheduleOceanEvents(ctx, horizon) {
  const p = currentPreset();
  while (nextEventAt < horizon) {
    const roll = Math.random();
    if (roll < p.surfChance) {
      playSurfWash(ctx, nextEventAt);
      nextEventAt += p.surfGap[0] + Math.random() * p.surfGap[1];
    } else {
      playBubble(ctx, nextEventAt);
      nextEventAt += p.bubbleGap[0] + Math.random() * p.bubbleGap[1];
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

export function getOceanPresetCount() {
  return OCEAN_PRESETS.length;
}

export function getOceanPresetIndex() {
  return presetIndex;
}

export function getOceanPresetMeta(index = presetIndex) {
  return OCEAN_PRESETS[index] || OCEAN_PRESETS[0];
}

/** Switch ambient style; restarts layers if already playing. */
export function setOceanPreset(index) {
  const n = OCEAN_PRESETS.length;
  presetIndex = ((Number(index) % n) + n) % n;
  const wasRunning = running;
  stopScheduler();
  if (wasRunning && armed && isEnabled() && !paused) {
    startScheduler();
  }
}

export function configureProceduralBgm(options) {
  getCtx = options.getCtx;
  isEnabled = options.isEnabled || (() => true);
  if (options.initialPreset != null) {
    presetIndex = Math.max(0, Math.min(OCEAN_PRESETS.length - 1, Number(options.initialPreset) || 0));
  }
}

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
