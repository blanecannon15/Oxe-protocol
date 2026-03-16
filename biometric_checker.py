"""
biometric_checker.py — Nativeness scoring for the Oxe Protocol.

Compares user pronunciation against a native Baiano model using:
- Syllable-duration DTW for isochrony detection (syllable-timed vs stress-timed)
- Pitch contour DTW
- Open-mid vowel (ɛ, ɔ) formant analysis
- Nasalized 'ão' and palatal 'lh' pattern checks

Scoring weights:
  50% isochrony (syllable-duration DTW)
  25% pitch contour DTW
  10% speech rate similarity
  15% rhythm metrics (nPVI / rPVI)

If speech is stress-timed (English pattern) instead of syllable-timed (Baiano),
the score is forced below 65 and open-mid vowel re-drill is triggered.

Usage:
    python3 biometric_checker.py score <user_audio> <native_audio>
    python3 biometric_checker.py check <chunk_id> <user_audio>
    python3 biometric_checker.py rhythm <audio>   Show rhythm metrics
"""

import sys
import numpy as np
from pathlib import Path

import parselmouth
from parselmouth.praat import call
from tslearn.metrics import dtw as tslearn_dtw

VAULT_AUDIOS = Path(__file__).parent / "voca_vault" / "audios"

CHORUSING_THRESHOLD = 85
ISOCHRONY_FAIL_CEILING = 65  # Max score when stress-timed pattern detected

# nPVI thresholds — syllable-timed languages have LOW nPVI (~30-50),
# stress-timed languages have HIGH nPVI (~60-80).
# Baiano Portuguese is strongly syllable-timed.
NPVI_SYLLABLE_TIMED_MAX = 45   # Above this → stress-timed warning
NPVI_STRESS_TIMED_MIN = 50     # Above this → definite English rhythm pattern

# Open-mid vowels that need re-drill when isochrony fails
OPEN_MID_VOWEL_WORDS = [
    "ferro", "porta", "pedra", "certo", "aberto", "festa", "belo",
    "força", "morte", "sete", "terra", "hotel", "papel", "pode",
    "nova", "bola", "fora", "cola", "modo", "jogo", "corpo",
    "café", "você", "sofá", "avó", "avô", "pé", "nó",
]


# ---------------------------------------------------------------------------
# Syllable segmentation via intensity envelope
# ---------------------------------------------------------------------------

def extract_intensity_contour(audio_path):
    """Extract intensity contour for syllable segmentation."""
    sound = parselmouth.Sound(str(audio_path))
    intensity = sound.to_intensity(minimum_pitch=75, time_step=0.005)
    times = intensity.xs()
    values = intensity.values.flatten()
    return times, values


def find_syllable_nuclei(audio_path, threshold_db=2.0, min_gap_s=0.04):
    """
    Detect syllable nuclei as intensity peaks.
    Returns array of peak times and corresponding durations between peaks.
    """
    times, values = extract_intensity_contour(audio_path)
    if len(values) < 3:
        return np.array([]), np.array([])

    mean_intensity = values.mean()
    cutoff = mean_intensity + threshold_db

    # Find rising edges (transitions from below to above threshold)
    above = values > cutoff
    transitions = np.diff(above.astype(int))
    rise_indices = np.where(transitions == 1)[0]

    if len(rise_indices) < 2:
        return np.array([]), np.array([])

    # For each rising edge, find the peak within the above-threshold region
    nuclei_times = []
    for idx in rise_indices:
        # Find the end of this above-threshold region
        end = idx + 1
        while end < len(above) and above[end]:
            end += 1
        # Peak is the max intensity in this region
        region = values[idx:end]
        peak_offset = np.argmax(region)
        peak_time = times[idx + peak_offset]

        # Enforce minimum gap between nuclei
        if nuclei_times and (peak_time - nuclei_times[-1]) < min_gap_s:
            continue
        nuclei_times.append(peak_time)

    nuclei_times = np.array(nuclei_times)

    # Syllable durations = intervals between consecutive nuclei
    durations = np.diff(nuclei_times)

    return nuclei_times, durations


def extract_syllable_durations(audio_path):
    """Return array of syllable durations (seconds) via intensity peaks."""
    _, durations = find_syllable_nuclei(audio_path)
    return durations


# ---------------------------------------------------------------------------
# Rhythm metrics: nPVI and rPVI
# ---------------------------------------------------------------------------

def compute_npvi(durations):
    """
    Normalized Pairwise Variability Index (nPVI).

    Measures rhythmic variability normalized for speech rate.
    Low nPVI (~30-50) → syllable-timed (Portuguese, Spanish, French)
    High nPVI (~60-80) → stress-timed (English, German, Dutch)

    Formula: nPVI = 100 * mean(|d_k - d_{k+1}| / ((d_k + d_{k+1})/2))
    """
    if len(durations) < 2:
        return 0.0
    pairs = []
    for k in range(len(durations) - 1):
        dk = durations[k]
        dk1 = durations[k + 1]
        avg = (dk + dk1) / 2.0
        if avg > 0:
            pairs.append(abs(dk - dk1) / avg)
    if not pairs:
        return 0.0
    return 100.0 * np.mean(pairs)


def compute_rpvi(durations):
    """
    Raw Pairwise Variability Index (rPVI).

    Non-normalized version — measures absolute variability in ms.
    rPVI = mean(|d_k - d_{k+1}|)
    """
    if len(durations) < 2:
        return 0.0
    diffs = [abs(durations[k] - durations[k + 1]) for k in range(len(durations) - 1)]
    return np.mean(diffs) * 1000  # Convert to ms


def compute_varco_v(durations):
    """
    VarcoV — coefficient of variation of vowel/syllable durations.

    Another rhythm metric: (std / mean) * 100
    Lower → more isochronous (syllable-timed)
    """
    if len(durations) < 2:
        return 0.0
    mean_d = np.mean(durations)
    if mean_d == 0:
        return 0.0
    return (np.std(durations) / mean_d) * 100.0


def is_stress_timed(durations):
    """
    Determine if the rhythm pattern is stress-timed (English-like)
    rather than syllable-timed (Baiano Portuguese).

    Returns (is_stress_timed: bool, npvi: float, details: str)
    """
    if len(durations) < 3:
        return False, 0.0, "Insufficient syllables for rhythm analysis."

    npvi = compute_npvi(durations)
    varco = compute_varco_v(durations)

    varco = compute_varco_v(durations)

    if npvi >= NPVI_STRESS_TIMED_MIN or (npvi >= NPVI_SYLLABLE_TIMED_MAX and varco > 50):
        return True, npvi, (
            f"nPVI={npvi:.1f} (>{NPVI_STRESS_TIMED_MIN}), VarcoV={varco:.1f} — English stress-timed rhythm detected. "
            f"Baiano Portuguese is syllable-timed (target nPVI < {NPVI_SYLLABLE_TIMED_MAX}). "
            f"Your syllables have uneven duration — some are rushed, others stretched. "
            f"Focus on making every syllable roughly equal length."
        )
    elif npvi >= NPVI_SYLLABLE_TIMED_MAX:
        return False, npvi, (
            f"nPVI={npvi:.1f} — borderline. Slightly stress-timed tendency. "
            f"Work on evening out syllable durations."
        )
    else:
        return False, npvi, (
            f"nPVI={npvi:.1f} — good syllable-timed rhythm. "
            f"Matching Baiano isochrony pattern."
        )


# ---------------------------------------------------------------------------
# Pitch contour (F0) analysis
# ---------------------------------------------------------------------------

def extract_f0(audio_path, time_step=0.01, f0_min=75, f0_max=600):
    """Extract normalized F0 pitch contour from audio file."""
    sound = parselmouth.Sound(str(audio_path))
    pitch = sound.to_pitch(time_step=time_step, pitch_floor=f0_min, pitch_ceiling=f0_max)
    f0 = pitch.selected_array["frequency"].flatten()

    nonzero = np.where(f0 > 0)[0]
    if len(nonzero) < 2:
        return f0

    f0_interp = np.interp(np.arange(len(f0)), nonzero, f0[nonzero])

    mean, std = f0_interp.mean(), f0_interp.std()
    if std > 0:
        f0_interp = (f0_interp - mean) / std

    return f0_interp


def compute_dtw_distance(seq_a, seq_b):
    """DTW distance between two 1D sequences via tslearn."""
    a_2d = np.asarray(seq_a, dtype=np.float64).reshape(-1, 1)
    b_2d = np.asarray(seq_b, dtype=np.float64).reshape(-1, 1)
    return tslearn_dtw(a_2d, b_2d)


# ---------------------------------------------------------------------------
# Isochrony DTW — the core syllable-timing comparison
# ---------------------------------------------------------------------------

def isochrony_dtw(user_audio, native_audio):
    """
    Compare syllable-duration sequences via DTW.

    In perfectly syllable-timed speech, all syllable durations are ~equal,
    so the duration sequence is nearly flat. Stress-timed speech has
    alternating long/short durations.

    Returns (dtw_distance, user_npvi, native_npvi, user_durations, native_durations).
    """
    user_durs = extract_syllable_durations(user_audio)
    native_durs = extract_syllable_durations(native_audio)

    if len(user_durs) < 2 or len(native_durs) < 2:
        return 0.0, 0.0, 0.0, user_durs, native_durs

    # Normalize durations by their mean (rate-independent comparison)
    user_norm = user_durs / np.mean(user_durs) if np.mean(user_durs) > 0 else user_durs
    native_norm = native_durs / np.mean(native_durs) if np.mean(native_durs) > 0 else native_durs

    dtw_dist = compute_dtw_distance(user_norm, native_norm)
    user_npvi = compute_npvi(user_durs)
    native_npvi = compute_npvi(native_durs)

    return dtw_dist, user_npvi, native_npvi, user_durs, native_durs


# ---------------------------------------------------------------------------
# Open-mid vowel detection
# ---------------------------------------------------------------------------

def detect_open_mid_vowel_issues(user_audio, native_audio):
    """
    Detect issues with open-mid vowels ɛ (as in 'ferro') and ɔ (as in 'porta').

    Uses F1/F2 formant analysis:
    - ɛ: high F1 (~550-700 Hz), mid F2 (~1700-2000 Hz)
    - ɔ: high F1 (~550-700 Hz), low F2 (~900-1100 Hz)

    If user's F1 is too low, they're likely producing closed-mid (e/o) instead
    of open-mid (ɛ/ɔ) — a common English speaker error in Portuguese.
    """
    issues = []

    sound_user = parselmouth.Sound(str(user_audio))
    sound_native = parselmouth.Sound(str(native_audio))

    # Extract formants
    formant_user = call(sound_user, "To Formant (burg)", 0.0, 5, 5500, 0.025, 50)
    formant_native = call(sound_native, "To Formant (burg)", 0.0, 5, 5500, 0.025, 50)

    # Sample F1 at midpoint
    dur_user = sound_user.get_total_duration()
    dur_native = sound_native.get_total_duration()

    f1_user = call(formant_user, "Get mean", 1, 0, dur_user, "hertz")
    f1_native = call(formant_native, "Get mean", 1, 0, dur_native, "hertz")

    if f1_native > 0 and f1_user > 0:
        f1_ratio = f1_user / f1_native
        if f1_ratio < 0.80:
            issues.append(
                f"F1 too low ({f1_user:.0f} Hz vs native {f1_native:.0f} Hz) — "
                f"producing closed-mid vowels (e/o) instead of open-mid (ɛ/ɔ). "
                f"Open your mouth wider on words like 'ferro', 'porta', 'café', 'avó'."
            )

    return issues


# ---------------------------------------------------------------------------
# Extended prosody measurements (10-dimension scoring)
# ---------------------------------------------------------------------------

def measure_vowel_length(user_audio, native_audio):
    """Compare stressed vowel durations. Baiano has longer open vowels.

    Extract vowel regions via intensity peaks, compare duration ratios.
    Score: 100 * (1 - abs(user_ratio - native_ratio))
    Returns float 0-100.
    """
    try:
        user_durs = extract_syllable_durations(user_audio)
        native_durs = extract_syllable_durations(native_audio)
        if not user_durs or not native_durs:
            return 50.0  # neutral score

        # Ratio of longest syllable to mean — proxy for stressed vowel prominence
        user_ratio = max(user_durs) / (np.mean(user_durs) + 1e-6)
        native_ratio = max(native_durs) / (np.mean(native_durs) + 1e-6)

        similarity = 1.0 - min(abs(user_ratio - native_ratio) / max(native_ratio, 1e-6), 1.0)
        return round(similarity * 100, 1)
    except Exception:
        return 50.0


def measure_airflow(user_audio, native_audio):
    """Estimate laryngeal relaxation via spectral tilt.

    Baiano speech has relaxed airflow — more energy in lower harmonics.
    Spectral tilt = difference between first and second harmonic amplitudes.
    Higher tilt = more relaxed = more Baiano-like.
    Returns float 0-100.
    """
    try:
        def _spectral_tilt(audio_path):
            snd = parselmouth.Sound(str(audio_path))
            spectrum = snd.to_spectrum()
            freqs = spectrum.xs()
            power = np.array([spectrum.get_power_in_band(f, f + 50) for f in range(50, 2000, 50)])
            if len(power) < 10:
                return 0.0
            # Fit linear slope to log power vs frequency
            log_power = np.log10(power + 1e-10)
            x = np.arange(len(log_power))
            slope = np.polyfit(x, log_power, 1)[0]
            return slope  # more negative = steeper rolloff = more relaxed

        user_tilt = _spectral_tilt(user_audio)
        native_tilt = _spectral_tilt(native_audio)

        if native_tilt == 0:
            return 50.0

        # Score based on how similar the tilts are
        diff = abs(user_tilt - native_tilt) / (abs(native_tilt) + 1e-6)
        score = max(0, 1.0 - diff) * 100
        return round(score, 1)
    except Exception:
        return 50.0


def measure_sentence_final_contour(user_audio, native_audio):
    """Compare F0 contour in the last 500ms of the utterance.

    Baiano declaratives have gradual fall. Questions have sharp rise.
    Uses DTW on the final pitch segment.
    Returns float 0-100.
    """
    try:
        from tslearn.metrics import dtw as ts_dtw

        def _final_f0(audio_path, duration_ms=500):
            f0 = extract_f0(audio_path)
            if len(f0) == 0:
                return np.array([])
            # Take last portion proportional to 500ms
            n_frames = max(1, int(len(f0) * 0.3))  # approx last 30% ~ 500ms
            segment = f0[-n_frames:]
            # Remove zeros (unvoiced)
            voiced = segment[segment > 0]
            if len(voiced) < 3:
                return np.array([])
            # Normalize
            mean_f0 = np.mean(voiced)
            if mean_f0 == 0:
                return voiced
            return (voiced - mean_f0) / (np.std(voiced) + 1e-6)

        user_final = _final_f0(user_audio)
        native_final = _final_f0(native_audio)

        if len(user_final) < 3 or len(native_final) < 3:
            return 50.0

        dist = ts_dtw(user_final.reshape(-1, 1), native_final.reshape(-1, 1))
        score = 100.0 / (1.0 + dist * 2.0)
        return round(min(score, 100.0), 1)
    except Exception:
        return 50.0


def measure_nasalization(user_audio, native_audio):
    """Detect nasalization depth on ão/ã segments.

    Nasalization shows as wider F1 bandwidth and anti-formant presence.
    Compare spectral characteristics in the 800-1200 Hz range.
    Returns float 0-100.
    """
    try:
        def _nasal_energy_ratio(audio_path):
            snd = parselmouth.Sound(str(audio_path))
            # Get power in nasal frequency band (800-1200 Hz) vs total
            spectrum = snd.to_spectrum()
            total_power = spectrum.get_band_energy(50, 4000)
            nasal_power = spectrum.get_band_energy(800, 1200)
            if total_power == 0:
                return 0.0
            return nasal_power / total_power

        user_ratio = _nasal_energy_ratio(user_audio)
        native_ratio = _nasal_energy_ratio(native_audio)

        if native_ratio == 0:
            return 50.0

        similarity = 1.0 - min(abs(user_ratio - native_ratio) / native_ratio, 1.0)
        return round(similarity * 100, 1)
    except Exception:
        return 50.0


def measure_syllable_reduction(user_audio, native_audio):
    """Compare unstressed vowel duration ratios.

    Baiano reduces unstressed vowels moderately (less than Paulista).
    Compare the ratio of shortest to longest syllable durations.
    Returns float 0-100.
    """
    try:
        user_durs = extract_syllable_durations(user_audio)
        native_durs = extract_syllable_durations(native_audio)

        if len(user_durs) < 3 or len(native_durs) < 3:
            return 50.0

        def _reduction_ratio(durs):
            sorted_d = sorted(durs)
            # Ratio of bottom quartile mean to top quartile mean
            q = max(1, len(sorted_d) // 4)
            short_mean = np.mean(sorted_d[:q])
            long_mean = np.mean(sorted_d[-q:])
            if long_mean == 0:
                return 1.0
            return short_mean / long_mean

        user_ratio = _reduction_ratio(user_durs)
        native_ratio = _reduction_ratio(native_durs)

        diff = abs(user_ratio - native_ratio)
        score = max(0, 1.0 - diff / max(native_ratio, 0.1)) * 100
        return round(score, 1)
    except Exception:
        return 50.0


def measure_cadence(user_audio, native_audio):
    """Detect Baiano melodic 'swing' via F0 autocorrelation.

    Baiano has a distinctive musical cadence — rhythmic pitch modulation.
    Compare F0 autocorrelation patterns between user and native.
    Returns float 0-100.
    """
    try:
        def _f0_autocorr(audio_path):
            f0 = extract_f0(audio_path)
            voiced = f0[f0 > 0]
            if len(voiced) < 10:
                return np.array([])
            # Normalize
            v = voiced - np.mean(voiced)
            v = v / (np.std(v) + 1e-6)
            # Autocorrelation via FFT
            n = len(v)
            fft = np.fft.fft(v, n=2*n)
            acf = np.fft.ifft(fft * np.conj(fft))[:n].real
            acf = acf / acf[0] if acf[0] != 0 else acf
            return acf[:min(n, 20)]  # first 20 lags

        user_acf = _f0_autocorr(user_audio)
        native_acf = _f0_autocorr(native_audio)

        if len(user_acf) < 5 or len(native_acf) < 5:
            return 50.0

        # Truncate to same length
        min_len = min(len(user_acf), len(native_acf))
        user_acf = user_acf[:min_len]
        native_acf = native_acf[:min_len]

        # Correlation between autocorrelation patterns
        corr = np.corrcoef(user_acf, native_acf)[0, 1]
        if np.isnan(corr):
            return 50.0

        score = (corr + 1.0) / 2.0 * 100  # map [-1,1] to [0,100]
        return round(score, 1)
    except Exception:
        return 50.0


def enhanced_nativeness_score(user_audio, native_audio):
    """10-dimension Baiano prosody scoring.

    Weights:
        isochrony: 0.35, pitch: 0.15, rate: 0.10, rhythm: 0.10,
        vowel_length: 0.08, airflow: 0.05, sentence_final: 0.07,
        nasalization: 0.08, syllable_reduction: 0.07, cadence: 0.05

    Returns dict with total_score, per-dimension scores, and diagnostics.
    """
    # Existing dimensions — isochrony DTW → convert distance to 0-100 score
    iso_dtw, _, _, _, _ = isochrony_dtw(user_audio, native_audio)
    iso_score = 100.0 / (1.0 + iso_dtw * 2.0)

    # Pitch DTW
    try:
        from tslearn.metrics import dtw as ts_dtw
        user_f0 = extract_f0(user_audio)
        native_f0 = extract_f0(native_audio)
        user_voiced = user_f0[user_f0 > 0]
        native_voiced = native_f0[native_f0 > 0]
        if len(user_voiced) > 3 and len(native_voiced) > 3:
            u_norm = (user_voiced - np.mean(user_voiced)) / (np.std(user_voiced) + 1e-6)
            n_norm = (native_voiced - np.mean(native_voiced)) / (np.std(native_voiced) + 1e-6)
            dist = ts_dtw(u_norm.reshape(-1, 1), n_norm.reshape(-1, 1))
            pitch_score = 100.0 / (1.0 + dist * 0.5)
        else:
            pitch_score = 50.0
    except Exception:
        pitch_score = 50.0

    # Speech rate
    try:
        user_durs = extract_syllable_durations(user_audio)
        native_durs = extract_syllable_durations(native_audio)
        if user_durs and native_durs:
            user_rate = len(user_durs) / (sum(user_durs) + 1e-6)
            native_rate = len(native_durs) / (sum(native_durs) + 1e-6)
            rate_diff = abs(user_rate - native_rate) / (native_rate + 1e-6)
            rate_score = max(0, (1.0 - rate_diff)) * 100
        else:
            rate_score = 50.0
    except Exception:
        rate_score = 50.0

    # Rhythm (nPVI)
    try:
        user_durs_r = extract_syllable_durations(user_audio)
        native_durs_r = extract_syllable_durations(native_audio)
        if len(user_durs_r) >= 2 and len(native_durs_r) >= 2:
            user_npvi = compute_npvi(user_durs_r)
            native_npvi = compute_npvi(native_durs_r)
            npvi_diff = abs(user_npvi - native_npvi)
            npvi_score = max(0, (1.0 - npvi_diff / 50)) * 100
        else:
            npvi_score = 50.0
    except Exception:
        npvi_score = 50.0

    # New dimensions
    vowel_score = measure_vowel_length(user_audio, native_audio)
    airflow_score = measure_airflow(user_audio, native_audio)
    final_score = measure_sentence_final_contour(user_audio, native_audio)
    nasal_score = measure_nasalization(user_audio, native_audio)
    reduction_score = measure_syllable_reduction(user_audio, native_audio)
    cadence_score = measure_cadence(user_audio, native_audio)

    # Weighted total
    total = (
        0.35 * iso_score
      + 0.15 * pitch_score
      + 0.10 * rate_score
      + 0.10 * npvi_score
      + 0.08 * vowel_score
      + 0.05 * airflow_score
      + 0.07 * final_score
      + 0.08 * nasal_score
      + 0.07 * reduction_score
      + 0.05 * cadence_score
    )

    # Stress-timed penalty
    stress_timed = False
    try:
        user_durs_st = extract_syllable_durations(user_audio)
        if user_durs_st:
            st, npvi_val, _ = is_stress_timed(user_durs_st)
            stress_timed = st
            if stress_timed:
                total = min(total, 65.0)
    except Exception:
        pass

    total = round(min(max(total, 0), 100), 1)

    dimensions = {
        "isochrony": round(iso_score, 1),
        "pitch_contour": round(pitch_score, 1),
        "speech_rate": round(rate_score, 1),
        "rhythm_npvi": round(npvi_score, 1),
        "vowel_length": round(vowel_score, 1),
        "airflow": round(airflow_score, 1),
        "sentence_final": round(final_score, 1),
        "nasalization": round(nasal_score, 1),
        "syllable_reduction": round(reduction_score, 1),
        "cadence": round(cadence_score, 1),
    }

    # Generate feedback for dimensions below 60
    feedback = []
    feedback_map = {
        "isochrony": "Ritmo tá stress-timed — precisa ser mais silábico",
        "pitch_contour": "Melodia tá diferente do baiano — ouve mais o nativo",
        "speech_rate": "Velocidade tá diferente — tenta igualar o ritmo",
        "rhythm_npvi": "Variação rítmica tá alta — mantém sílabas mais iguais",
        "vowel_length": "Vogais tônicas tão curtas — abre mais, como baiano",
        "airflow": "Voz tá tensa — relaxa mais a garganta, deixa o ar fluir",
        "sentence_final": "Final da frase tá errado — presta atenção na descida/subida",
        "nasalization": "Nasalização fraca no ão/ã — puxa mais pelo nariz",
        "syllable_reduction": "Redução de sílaba tá diferente — ouve como o nativo reduz",
        "cadence": "Falta a ginga baiana — ouve a melodia e imita o swing",
    }
    for dim, score in dimensions.items():
        if score < 60:
            feedback.append(feedback_map.get(dim, f"{dim} precisa melhorar"))

    return {
        "total_score": total,
        "dimensions": dimensions,
        "stress_timed": stress_timed,
        "needs_chorusing": total < 85,
        "force_redrill": stress_timed,
        "dimension_feedback": feedback,
    }


# ---------------------------------------------------------------------------
# Main scoring function
# ---------------------------------------------------------------------------

def nativeness_score(user_audio, native_audio):
    """
    Compute nativeness score (0-100) with isochrony as primary metric.

    Weights:
      50% — Syllable-duration DTW (isochrony match)
      25% — Pitch contour DTW
      10% — Speech rate similarity
      15% — Rhythm regularity (nPVI comparison)

    If stress-timed pattern detected → score capped at 65, open-mid vowel
    re-drill flagged.
    """
    # --- Isochrony (syllable duration DTW) ---
    iso_dtw, user_npvi, native_npvi, user_durs, native_durs = isochrony_dtw(
        user_audio, native_audio
    )
    iso_score = 100.0 / (1.0 + iso_dtw * 2.0)  # Scale DTW → 0-100

    # --- Pitch contour DTW ---
    f0_user = extract_f0(user_audio)
    f0_native = extract_f0(native_audio)
    if len(f0_user) > 1 and len(f0_native) > 1:
        pitch_dtw = compute_dtw_distance(f0_user, f0_native)
        pitch_score = 100.0 / (1.0 + pitch_dtw)
    else:
        pitch_score = 50.0

    # --- Speech rate ---
    sound_user = parselmouth.Sound(str(user_audio))
    sound_native = parselmouth.Sound(str(native_audio))
    syl_user = max(len(user_durs) + 1, 1)
    syl_native = max(len(native_durs) + 1, 1)
    rate_user = syl_user / sound_user.get_total_duration()
    rate_native = syl_native / sound_native.get_total_duration()
    rate_diff = abs(rate_user - rate_native)
    rate_score = max(0, 100 - rate_diff * 20)

    # --- Rhythm regularity (nPVI comparison) ---
    npvi_diff = abs(user_npvi - native_npvi)
    npvi_score = max(0, 100 - npvi_diff * 2.0)

    # --- Weighted combination ---
    combined = (
        iso_score * 0.50
        + pitch_score * 0.25
        + rate_score * 0.10
        + npvi_score * 0.15
    )

    # --- Stress-timed penalty ---
    stress_timed, user_npvi_val, _ = is_stress_timed(user_durs)
    if stress_timed:
        combined = min(combined, ISOCHRONY_FAIL_CEILING)

    return round(max(0, min(100, combined)), 1)


def full_analysis(user_audio, native_audio):
    """
    Full diagnostic analysis. Returns dict with score, metrics, issues, and flags.
    """
    result = {
        "score": 0.0,
        "stress_timed": False,
        "needs_chorusing": False,
        "force_redrill": False,
        "needs_open_mid_drill": False,
        "open_mid_drill_words": [],
        "issues": [],
        "metrics": {},
    }

    # Isochrony
    iso_dtw, user_npvi, native_npvi, user_durs, native_durs = isochrony_dtw(
        user_audio, native_audio
    )
    stress_timed, npvi_val, rhythm_detail = is_stress_timed(user_durs)

    result["metrics"]["isochrony_dtw"] = round(iso_dtw, 3)
    result["metrics"]["user_npvi"] = round(user_npvi, 1)
    result["metrics"]["native_npvi"] = round(native_npvi, 1)
    result["metrics"]["user_rpvi_ms"] = round(compute_rpvi(user_durs), 1)
    result["metrics"]["user_varco_v"] = round(compute_varco_v(user_durs), 1)
    result["metrics"]["user_syllable_count"] = len(user_durs) + 1
    result["metrics"]["native_syllable_count"] = len(native_durs) + 1

    # rPVI absolute check
    user_rpvi = compute_rpvi(user_durs)
    if user_rpvi > 60:
        result["issues"].append(
            f"rPVI={user_rpvi:.1f}ms — syllable durations vary too much in absolute terms. "
            f"Target < 60ms. Even out every syllable."
        )

    # Stress-timed detection
    result["stress_timed"] = stress_timed
    if stress_timed:
        result["issues"].append(rhythm_detail)
        result["needs_open_mid_drill"] = True
        result["open_mid_drill_words"] = OPEN_MID_VOWEL_WORDS
    elif npvi_val >= NPVI_SYLLABLE_TIMED_MAX:
        result["issues"].append(rhythm_detail)

    # Pitch
    f0_user = extract_f0(user_audio)
    f0_native = extract_f0(native_audio)
    if len(f0_user) > 1 and len(f0_native) > 1:
        pitch_dtw = compute_dtw_distance(f0_user, f0_native)
        result["metrics"]["pitch_dtw"] = round(pitch_dtw, 3)

        # Pitch range check (nasalization)
        user_range = f0_user.max() - f0_user.min()
        native_range = f0_native.max() - f0_native.min()
        if native_range > 0 and user_range / native_range < 0.7:
            result["issues"].append(
                "Pitch range too narrow — likely missing nasalization on 'ão'. "
                "Open the nasal passage and let the pitch rise."
            )

    # Open-mid vowel formant check
    try:
        vowel_issues = detect_open_mid_vowel_issues(user_audio, native_audio)
        result["issues"].extend(vowel_issues)
        if vowel_issues:
            result["needs_open_mid_drill"] = True
            result["open_mid_drill_words"] = OPEN_MID_VOWEL_WORDS
    except Exception:
        pass  # Formant extraction can fail on short/noisy audio

    # Final score
    score = nativeness_score(user_audio, native_audio)
    result["score"] = score
    result["needs_chorusing"] = score < CHORUSING_THRESHOLD

    # Force re-drill if score < 65 due to isochrony failure
    if score < ISOCHRONY_FAIL_CEILING and stress_timed:
        result["needs_open_mid_drill"] = True
        result["force_redrill"] = True

    return result


def check_pronunciation(chunk_id, user_audio_path, native_audio_path=None):
    """Full pipeline for a specific chunk."""
    if native_audio_path is None:
        native_audio_path = VAULT_AUDIOS / f"{chunk_id}_native.wav"
        if not Path(native_audio_path).exists():
            native_audio_path = VAULT_AUDIOS / f"{chunk_id}_native.mp3"

    if not Path(native_audio_path).exists():
        raise FileNotFoundError(
            f"Native reference audio not found for chunk {chunk_id}. "
            f"Expected at {native_audio_path}"
        )

    return full_analysis(user_audio_path, native_audio_path)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _print_analysis(result):
    """Pretty-print full analysis result."""
    score = result["score"]
    if score >= CHORUSING_THRESHOLD:
        status = "PASS"
    elif score < ISOCHRONY_FAIL_CEILING and result["stress_timed"]:
        status = "FAIL — STRESS-TIMED RHYTHM"
    elif score < CHORUSING_THRESHOLD:
        status = "CHORUS NEEDED"
    else:
        status = "REVIEW"

    print(f"\n  Nativeness Score: {score}/100  [{status}]")
    print()

    m = result["metrics"]
    print("  Rhythm Metrics:")
    print(f"    nPVI (user):    {m.get('user_npvi', 0):.1f}  (target: <{NPVI_SYLLABLE_TIMED_MAX})")
    print(f"    nPVI (native):  {m.get('native_npvi', 0):.1f}")
    print(f"    rPVI (user):    {m.get('user_rpvi_ms', 0):.1f} ms")
    print(f"    VarcoV (user):  {m.get('user_varco_v', 0):.1f}")
    print(f"    Isochrony DTW:  {m.get('isochrony_dtw', 0):.3f}")
    if "pitch_dtw" in m:
        print(f"    Pitch DTW:      {m['pitch_dtw']:.3f}")
    print(f"    Syllables:      {m.get('user_syllable_count', 0)} (native: {m.get('native_syllable_count', 0)})")

    if result["issues"]:
        print("\n  Issues:")
        for issue in result["issues"]:
            print(f"    ⚠ {issue}")

    if result["needs_open_mid_drill"]:
        print(f"\n  🔴 OPEN-MID VOWEL RE-DRILL REQUIRED (ɛ, ɔ)")
        print(f"     Practice words: {', '.join(result['open_mid_drill_words'][:10])}...")

    if result["needs_chorusing"]:
        print(f"\n  🔄 CHORUSING REPETITION REQUIRED (score < {CHORUSING_THRESHOLD})")

    print()


def main():
    if len(sys.argv) < 2:
        print(__doc__.strip())
        sys.exit(1)

    cmd = sys.argv[1]

    if cmd == "score" and len(sys.argv) >= 4:
        user_audio = sys.argv[2]
        native_audio = sys.argv[3]
        result = full_analysis(user_audio, native_audio)
        _print_analysis(result)

    elif cmd == "check" and len(sys.argv) >= 4:
        chunk_id = int(sys.argv[2])
        user_audio = sys.argv[3]
        result = check_pronunciation(chunk_id, user_audio)
        _print_analysis(result)

    elif cmd == "rhythm" and len(sys.argv) >= 3:
        audio = sys.argv[2]
        durs = extract_syllable_durations(audio)
        if len(durs) < 2:
            print("Not enough syllables detected.")
            sys.exit(1)
        npvi = compute_npvi(durs)
        rpvi = compute_rpvi(durs)
        varco = compute_varco_v(durs)
        stress, _, detail = is_stress_timed(durs)
        print(f"\n  Rhythm Analysis: {audio}")
        print(f"    Syllables detected: {len(durs) + 1}")
        print(f"    Durations (ms): {[round(d*1000, 1) for d in durs]}")
        print(f"    nPVI:   {npvi:.1f}  (syllable-timed < {NPVI_SYLLABLE_TIMED_MAX})")
        print(f"    rPVI:   {rpvi:.1f} ms")
        print(f"    VarcoV: {varco:.1f}")
        print(f"    {detail}")
        print()

    else:
        print(__doc__.strip())
        sys.exit(1)


if __name__ == "__main__":
    main()
