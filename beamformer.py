import numpy as np
import logging
from config import (
    SAMPLE_RATE,
    MVDR_ENABLED,
    MVDR_SOURCE_AZIMUTH_DEG,
    MVDR_SOURCE_ELEVATION_DEG,
    MVDR_NFFT,
    MVDR_MIC_POSITIONS,
)

try:
    from config import MVDR_FALLBACK_CHANNEL
except ImportError:
    MVDR_FALLBACK_CHANNEL = 0

log = logging.getLogger(__name__)

# Mic positions in 3D metres — vertical disc, mics facing +Y (toward user)
# Order matches UDP packet channel indices 0-3
MIC_POSITIONS = np.array(MVDR_MIC_POSITIONS).T  # shape (3, 4)

# Expected source direction — user at ~2m distance, ~18 elevation
SOURCE_AZIMUTH_DEG = MVDR_SOURCE_AZIMUTH_DEG
SOURCE_ELEVATION_DEG = MVDR_SOURCE_ELEVATION_DEG

NFFT = MVDR_NFFT


class MVDRBeamformer:
    def __init__(self, sample_rate: int = SAMPLE_RATE):
        self.sample_rate = sample_rate
        self.enabled = MVDR_ENABLED
        self._ready = False
        self._fallback_ch = MVDR_FALLBACK_CHANNEL

        if not self.enabled:
            log.info("MVDR beamforming disabled in config — using ch%d fallback",
                     self._fallback_ch)
            return

        self._init_beamformer()

    def _init_beamformer(self):
        try:
            from scipy.signal import stft, istft
            self._stft_func = stft
            self._istft_func = istft
        except ImportError:
            log.warning("scipy not available — beamforming disabled")
            return

        az_rad = np.radians(SOURCE_AZIMUTH_DEG)
        el_rad = np.radians(SOURCE_ELEVATION_DEG)

        self._source_direction = np.array([
            np.sin(az_rad) * np.cos(el_rad),   # X
            np.cos(az_rad) * np.cos(el_rad),   # Y (depth toward user)
            np.sin(el_rad),                      # Z (height)
        ])

        self._mic_positions = MIC_POSITIONS
        self._ready = True
        log.info(
            "MVDR beamformer initialised — az=%s° el=%s° nfft=%d",
            SOURCE_AZIMUTH_DEG, SOURCE_ELEVATION_DEG, NFFT,
        )

    def process(self, audio_4ch: np.ndarray) -> np.ndarray:
        if not self._ready or audio_4ch.shape[0] != 4:
            return audio_4ch[self._fallback_ch].astype(np.float32)

        num_samples = audio_4ch.shape[1]

        if num_samples < NFFT * 4:
            return self._delay_and_sum_fallback(audio_4ch)

        try:
            freqs, times, stft_data = self._stft_func(
                audio_4ch,
                fs=self.sample_rate,
                nperseg=NFFT,
                noverlap=NFFT // 2,
                axis=1,
            )
            # stft_data shape: (4, freq_bins, time_frames)

            num_freqs = stft_data.shape[1]
            num_frames = stft_data.shape[2]

            steering = self._compute_steering_vector(freqs)

            beamformed_stft = np.zeros(
                (num_freqs, num_frames), dtype=np.complex128
            )

            for f_idx in range(num_freqs):
                X_f = stft_data[:, f_idx, :]  # shape (4, num_frames)
                R = (X_f @ X_f.conj().T) / num_frames
                R += np.eye(4) * 1e-6 * np.trace(R)

                d = steering[:, f_idx]  # shape (4,)

                try:
                    R_inv = np.linalg.inv(R)
                    w = R_inv @ d
                    w = w / (d.conj() @ w + 1e-10)
                except np.linalg.LinAlgError:
                    w = d / (np.linalg.norm(d) + 1e-10)

                beamformed_stft[f_idx, :] = w.conj() @ X_f

            _, beamformed = self._istft_func(
                beamformed_stft,
                fs=self.sample_rate,
                nperseg=NFFT,
                noverlap=NFFT // 2,
            )

            beamformed = np.real(beamformed[:num_samples]).astype(np.float32)

            peak = np.max(np.abs(beamformed))
            if peak > 1.0:
                beamformed = beamformed / peak

            return beamformed

        except Exception:
            log.error("MVDR failed — falling back to delay-and-sum", exc_info=True)
            return self._delay_and_sum_fallback(audio_4ch)

    def _compute_steering_vector(self, freqs: np.ndarray) -> np.ndarray:
        c = 343.0
        src = self._source_direction  # unit vector (3,)
        tau = (self._mic_positions.T @ src) / c  # shape (4,)
        steering = np.exp(
            -1j * 2 * np.pi * freqs[np.newaxis, :] * tau[:, np.newaxis]
        )  # shape (4, num_freqs)
        return steering

    def _delay_and_sum_fallback(self, audio_4ch: np.ndarray) -> np.ndarray:
        c = 343.0
        src = self._source_direction
        tau = (self._mic_positions.T @ src) / c
        delays = np.round(tau * self.sample_rate).astype(int)
        delays -= delays[0]  # relative to mic 0

        num_samples = audio_4ch.shape[1]
        aligned = np.zeros((4, num_samples), dtype=np.float32)
        for i in range(4):
            d = delays[i]
            if d >= 0:
                aligned[i, d:] = audio_4ch[i, :num_samples - d]
            else:
                aligned[i, :num_samples + d] = audio_4ch[i, -d:]

        return np.mean(aligned, axis=0)

    def snr_estimate_db(self,
                        audio: np.ndarray,
                        noise_floor: float = 1e-4) -> float:
        rms = float(np.sqrt(np.mean(audio.astype(np.float32) ** 2)))
        return 20.0 * np.log10(max(rms, 1e-6) / max(noise_floor, 1e-6))
