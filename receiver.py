# ~/assistant/receiver.py
import socket
import struct
import numpy as np
import threading
import queue
import time
import json
import logging
import wave
import os
from collections import deque
from config import *

log = logging.getLogger(__name__)
log.setLevel(logging.INFO)

MAGIC              = 0x53504D31
HEADER_SIZE        = 16
FRAMES_PER_PKT     = 128
CHANNELS           = 5
# BEAM_CH removed — lama now does its own MVDR beamforming using raw ch0-3
SAMPLE_RATE        = 16000

# Host-side VAD parameters (int16 scale — RMS computed on int16-reconstituted ch0)
SILENCE_THRESHOLD    = 350       # trailing silence RMS threshold
SILENCE_DURATION_S   = 1.0       # trailing silence to declare end-of-speech
SILENCE_FRAMES       = int(SILENCE_DURATION_S * SAMPLE_RATE / FRAMES_PER_PKT)
MAX_UTTERANCE_S      = 15.0
MIN_UTTERANCE_S      = 0.5       # discard shorter utterances
SILENCE_TOLERANCE    = 5         # consecutive noisy frames before silence-count penalty

# Speech onset parameters
SPEECH_ONSET_FRAMES  = 5         # consecutive above-threshold frames needed to confirm onset
SPEECH_ONSET_RMS     = 200       # onset threshold: ~4x beam noise floor (51 RMS)
PRE_SPEECH_TIMEOUT_S = 4.0       # abort silent utterance if no onset within this window
PRE_ROLL_FRAMES      = 10        # ~80ms pre-roll kept before onset to avoid clipping attack

# Utterance quality gate (reduces noise-only hallucinations)
MIN_SPEECH_FRAMES    = 8         # minimum speech-like packets after onset (~64ms)
MIN_VOICED_RATIO     = 0.12      # speech packets / total packets in buffered utterance
MIN_ONSET_ELAPSED_S  = 0.08      # onset earlier than this is suspicious
MIN_EARLY_ONSET_RMS  = 2200.0    # early onset below this RMS is treated as probable noise
MAX_NOISE_BURST_S    = 1.6       # only short utterances are eligible for early-onset noise reject
MAX_NOISE_SPEECH_FRAMES = 60     # and only if speech evidence is limited


class AudioReceiver:
    def __init__(self, audio_queue: queue.Queue):
        self.audio_queue      = audio_queue
        self.streaming        = False
        self.buffer           = []
        self.last_seq         = -1
        self._seq_per_ip       = {}   # per-source IP sequence tracking
        self.silence_count    = 0
        self.utterance_start  = None
        self.processing_sent  = False
        self.active_uid       = None   # current utterance_id
        self.noise_count      = 0      # noisy frames within current silence window
        self.speech_detected  = False  # speech onset confirmed
        self.speech_onset_count = 0    # consecutive above-threshold frames for onset
        self.pre_roll         = deque(maxlen=PRE_ROLL_FRAMES)  # pre-onset audio ring buffer
        self.phase2_total_frames = 0
        self.phase2_speech_frames = 0
        self.phase2_speech_rms_sum = 0.0
        self.speech_onset_elapsed_s = None

        # Discard window after wakeword (in packets)
        self._discard_packets_after_wake = 0
        self._active_front_end = None  # IP of front-end that sent last WAKE_DETECTED
        self._wake_cooldown_until = 0   # reject WAKE_DETECTED until this monotonic time

        # 4-channel raw mic recording for diagnostics
        self.recording_active = False
        self._rec_writers = [None] * 5  # WAV writers for ch0-ch3 raw + ch4 beamformed
        self._rec_dir = ''

        # Audio RX
        self.audio_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.audio_sock.bind(('0.0.0.0', UDP_AUDIO_PORT))
        self.audio_sock.settimeout(1.0)

        # Control RX
        self.ctrl_rx_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.ctrl_rx_sock.bind(('0.0.0.0', UDP_CONTROL_RX_PORT))
        self.ctrl_rx_sock.settimeout(1.0)

        # Control TX
        self.ctrl_tx_sock  = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.esp_ctrl_addr = (ESP32_IP, UDP_CONTROL_TX_PORT)

    def start_recording(self, output_dir: str, base_name: str = ''):
        if self.recording_active:
            return False
        os.makedirs(output_dir, exist_ok=True)
        if not base_name:
            base_name = f"rec_{int(time.time())}"
        channel_names = ['ch0_raw', 'ch1_raw', 'ch2_raw', 'ch3_raw', 'ch4_beam']
        self._rec_dir = output_dir
        for i, cname in enumerate(channel_names):
            path = os.path.join(output_dir, f"{base_name}_{cname}.wav")
            wf = wave.open(path, 'wb')
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(SAMPLE_RATE)
            self._rec_writers[i] = wf
        self.recording_active = True
        log.info("4ch recording started: %s/%s_*.wav", output_dir, base_name)
        return True

    def stop_recording(self):
        if not self.recording_active:
            return None
        paths = []
        for i, wf in enumerate(self._rec_writers):
            if wf is not None:
                wf.close()
                paths.append(os.path.join(self._rec_dir,
                    f"rec_{int(time.time())}_{['ch0','ch1','ch2','ch3','ch4'][i]}.wav"))
        self._rec_writers = [None] * 5
        self.recording_active = False
        log.info("4ch recording stopped — %d channels written", len([p for p in paths if p]))
        return [p for p in paths if os.path.exists(p)]

    def is_recording(self) -> bool:
        return self.recording_active

    def send_control(self, msg: str):
        log.info(f"Control TX: {msg}")
        self.ctrl_tx_sock.sendto(msg.encode('utf-8'), self.esp_ctrl_addr)

    def _rms(self, frame: np.ndarray) -> float:
        return float(np.sqrt(np.mean(frame.astype(np.float32) ** 2)))

    def _is_active_uid(self, pkt: dict) -> bool:
        """
        Returns True if packet belongs to current active utterance.
        If ESP doesn't send utterance_id, always returns True (backward compat).
        """
        uid = pkt.get('utterance_id', None)
        if uid is None:
            return True
        if self.active_uid is None:
            return True
        if uid != self.active_uid:
            log.debug(f"Stale event ignored: uid={uid} active={self.active_uid}")
            return False
        return True

    def parse_audio_packet(self, data, source_ip=None):
        if len(data) < HEADER_SIZE:
            log.warning(f"Packet too small: {len(data)} bytes")
            return None, None, None
        magic, seq, sr, frames, channels = struct.unpack('<IIIHH', data[:HEADER_SIZE])
        if magic != MAGIC:
            log.warning(f"Bad magic: {magic:#010x}")
            return None, None, None
        expected_sample_bytes = frames * channels * 2
        if len(data) - HEADER_SIZE < expected_sample_bytes:
            log.warning(f"Packet too small for declared frames: {len(data)} bytes, header says {frames}x{channels}={expected_sample_bytes} sample bytes")
            return None, None, None
        if len(data) != 1296:
            log.debug(f"Variable-length packet: {len(data)} bytes, {frames} frames")
        prev = self._seq_per_ip.get(source_ip, -1) if source_ip else self.last_seq
        if prev >= 0 and seq != prev + 1:
            gap = seq - prev - 1
            log.warning("Dropped %d packets (seq %d→%d from %s)",
                        gap, prev, seq, source_ip or 'unknown')
        if source_ip:
            self._seq_per_ip[source_ip] = seq
        self.last_seq = seq

        raw = np.frombuffer(data, dtype=np.int16, count=frames * channels, offset=HEADER_SIZE)
        raw = raw.reshape(-1, channels).T  # shape (channels, 128)

        # Extract available mic channels (up to 4) for MVDR, normalise to float32
        n_mics = min(channels, 4)
        audio_4ch = raw[:n_mics].astype(np.float32) / 32768.0
        # Pad to 4 channels if fewer (MVDR expects 4)
        if audio_4ch.shape[0] < 4:
            pad = np.zeros((4 - audio_4ch.shape[0], audio_4ch.shape[1]), dtype=np.float32)
            audio_4ch = np.vstack([audio_4ch, pad])
        # VAD channel: last channel = beamformed (5ch) or HP-filtered mono (3ch)
        ch4 = raw[channels - 1]

        return seq, audio_4ch, ch4

    def _end_of_speech_detected(self, reason: str = 'VAD'):
        """
        Host is primary authority for end of utterance.
        Send PROCESSING immediately — do not wait for AUDIO_END.
        Flush buffer directly here.
        """
        if not self.streaming or self.processing_sent:
            return

        log.info(f"End of speech detected ({reason}) — sending PROCESSING")
        self.processing_sent = True
        self.send_control('PROCESSING')

        # Host is authoritative — flush immediately, don't wait for AUDIO_END
        self._flush_utterance()

    def _abort_utterance(self):
        """
        Silently discard the current utterance (no speech detected, or too short).
        Signals ESP READY to re-arm wakeword detection.
        """
        log.info("Aborting silent/short utterance — sending READY")
        # Resume music if interrupted and signal mic close
        try:
            from syncstreamer import get_syncstreamer
            get_syncstreamer().on_mic_close()
        except Exception:
            pass
        try:
            from jellyfin_music import get_music_controller
            music = get_music_controller()
            music.resume_after_interrupt()
        except Exception:
            pass
        self.streaming          = False
        self.buffer             = []
        self.pre_roll           = deque(maxlen=PRE_ROLL_FRAMES)
        self.silence_count      = 0
        self.noise_count        = 0
        self.speech_detected    = False
        self.speech_onset_count = 0
        self.phase2_total_frames = 0
        self.phase2_speech_frames = 0
        self.phase2_speech_rms_sum = 0.0
        self.speech_onset_elapsed_s = None
        self.utterance_start    = None
        self.processing_sent    = False
        self.send_control('READY')

    def _flush_utterance(self):
        """
        Flush buffered audio to STT queue.
        Called by host VAD (primary) or watchdog AUDIO_END (fallback).
        """
        # Snapshot metrics before resetting state so quality gating uses
        # real values from the just-finished utterance.
        phase2_total_frames = self.phase2_total_frames
        phase2_speech_frames = self.phase2_speech_frames
        phase2_speech_rms_sum = self.phase2_speech_rms_sum
        speech_onset_elapsed_s = self.speech_onset_elapsed_s

        self.streaming          = False
        self.silence_count      = 0
        self.noise_count        = 0
        self.speech_detected    = False
        self.speech_onset_count = 0
        self.pre_roll           = deque(maxlen=PRE_ROLL_FRAMES)
        self.utterance_start    = None

        duration = np.concatenate(self.buffer, axis=1).shape[1] / SAMPLE_RATE if self.buffer else 0

        voiced_ratio = (
            phase2_speech_frames / phase2_total_frames
            if phase2_total_frames > 0 else 0.0
        )
        mean_speech_rms = (
            phase2_speech_rms_sum / phase2_speech_frames
            if phase2_speech_frames > 0 else 0.0
        )
        quality_ok = (
            phase2_speech_frames >= MIN_SPEECH_FRAMES
            and voiced_ratio >= MIN_VOICED_RATIO
        )
        early_onset_suspect = (
            speech_onset_elapsed_s is not None
            and speech_onset_elapsed_s < MIN_ONSET_ELAPSED_S
            and mean_speech_rms < MIN_EARLY_ONSET_RMS
            and duration <= MAX_NOISE_BURST_S
            and phase2_speech_frames <= MAX_NOISE_SPEECH_FRAMES
        )

        if self.buffer and duration >= MIN_UTTERANCE_S and quality_ok and not early_onset_suspect:
            audio_4ch = np.concatenate(self.buffer, axis=1)  # (4, total_samples)
            log.info(
                "Flushing %.2fs audio to STT queue (speech_frames=%d total_frames=%d voiced_ratio=%.2f mean_speech_rms=%.0f onset=%.3fs)",
                duration,
                phase2_speech_frames,
                phase2_total_frames,
                voiced_ratio,
                mean_speech_rms,
                speech_onset_elapsed_s if speech_onset_elapsed_s is not None else -1.0,
            )
            meta = {
                'speech_frames': int(phase2_speech_frames),
                'total_frames': int(phase2_total_frames),
                'voiced_ratio': float(voiced_ratio),
                'mean_speech_rms': float(mean_speech_rms),
                'speech_onset_elapsed_s': float(speech_onset_elapsed_s or 0.0),
            }
            self.audio_queue.put((audio_4ch, duration, meta))
        elif self.buffer:
            log.warning(
                "Discarding low-quality utterance (duration=%.2fs speech_frames=%d total_frames=%d voiced_ratio=%.2f mean_speech_rms=%.0f onset=%.3fs early_onset_suspect=%s)",
                duration,
                phase2_speech_frames,
                phase2_total_frames,
                voiced_ratio,
                mean_speech_rms,
                speech_onset_elapsed_s if speech_onset_elapsed_s is not None else -1.0,
                early_onset_suspect,
            )
            # Ensure music unmutes even for low-quality utterances
            try:
                from syncstreamer import get_syncstreamer
                get_syncstreamer().on_mic_close()
            except Exception:
                pass
            self.send_control('READY')
            self.streaming = False
            self.processing_sent = False
            self.buffer = []
        else:
            log.warning("Buffer empty — discarding")

        self.phase2_total_frames = 0
        self.phase2_speech_frames = 0
        self.phase2_speech_rms_sum = 0.0
        self.speech_onset_elapsed_s = None
        self.buffer           = []
        self.processing_sent  = False

    def control_listener(self):
        log.info("Control listener started")
        while True:
            try:
                data, addr = self.ctrl_rx_sock.recvfrom(256)
                try:
                    pkt = json.loads(data.decode('utf-8'))
                    msg_type = pkt.get('type', '')
                except json.JSONDecodeError:
                    log.warning(f"Malformed control packet: {data}")
                    continue

                if msg_type != 'HEARTBEAT':
                    log.info(f"Control RX: {msg_type} state={pkt.get('state','')} "
                             f"uid={pkt.get('utterance_id','?')} from {addr}")

                allowed = (addr[0] == ESP32_IP)
                if not allowed:
                    try:
                        from syncstreamer import get_syncstreamer
                        ss = get_syncstreamer()
                        if addr[0] in ss._registry.active_clients():
                            allowed = True
                    except Exception:
                        pass
                if not allowed:
                    log.warning(f"Ignoring control packet from unexpected host {addr[0]} msg={msg_type}")
                    continue

                if msg_type == 'WAKE_DETECTED':
                    # Reject wakewords while pipeline is busy or TTS is playing
                    tts_active = False
                    try:
                        from syncstreamer import get_syncstreamer
                        tts_active = get_syncstreamer()._tts_active
                    except Exception:
                        pass
                    if self.processing_sent or self.streaming or tts_active:
                        log.info("Ignoring WAKE_DETECTED — pipeline busy (processing=%s streaming=%s)",
                                 self.processing_sent, self.streaming)
                        continue
                    try:
                        from syncstreamer import get_syncstreamer
                        get_syncstreamer().on_mic_open()
                    except Exception:
                        pass

                    self.active_uid         = pkt.get('utterance_id', None)
                    self.streaming          = True
                    self.buffer             = []
                    self.last_seq           = -1
                    self._seq_per_ip = {}
                    self._active_front_end = addr[0]
                    self.silence_count      = 0
                    self.noise_count        = 0
                    self.speech_detected    = False
                    self.speech_onset_count = 0
                    self.phase2_total_frames = 0
                    self.phase2_speech_frames = 0
                    self.phase2_speech_rms_sum = 0.0
                    self.speech_onset_elapsed_s = None
                    self.pre_roll           = deque(maxlen=PRE_ROLL_FRAMES)
                    self.processing_sent    = False
                    self.utterance_start    = time.time()
                    # Discard first ~300ms of audio after wake
                    self._discard_packets_after_wake = 38
                    log.info(f"Wake detected uid={self.active_uid} — "
                             f"buffer cleared, listening armed")

                elif msg_type == 'AUDIO_START':
                    if self._is_active_uid(pkt) and self.streaming:
                        log.info("First audio packet confirmed")
                    # else stale/reordered UDP — ignore


                # ESP32 cut its listen window — use as fallback flush trigger.
                # If host VAD already fired, processing_sent=True and _end_of_speech
                # is a no-op. If speech was detected but host VAD hasn't fired yet
                # (ESP32 cut off first), flush now so the utterance isn't lost.
                elif msg_type in ('AUDIO_END', 'VAD_END', 'LISTEN_TIMEOUT'):
                    if self._is_active_uid(pkt) and self.streaming:
                        if self.speech_detected:
                            log.info(f"ESP32 {msg_type} — flushing buffered speech (fallback)")
                            self._end_of_speech_detected(f"ESP32 {msg_type}")
                        else:
                            log.info(f"ESP32 {msg_type} — no speech detected, aborting")
                            self._abort_utterance()

                elif msg_type == 'HEARTBEAT':
                    pass  # intentionally silent

            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"Control RX error: {e}")

    def audio_listener(self):
        log.info(f"Audio listener on UDP port {UDP_AUDIO_PORT}")
        while True:
            try:
                data, addr = self.audio_sock.recvfrom(2048)

                if not self.streaming:
                    continue

                seq, audio_4ch, ch4 = self.parse_audio_packet(data, addr[0])
                if audio_4ch is None:
                    continue

                # 4-channel diagnostic recording — write raw int16 per channel
                if self.recording_active:
                    for idx in range(5):
                        wf = self._rec_writers[idx]
                        if wf is None:
                            continue
                        if idx < 4 and idx < audio_4ch.shape[0]:
                            arr = (audio_4ch[idx] * 32768.0).clip(-32768, 32767).astype(np.int16)
                            wf.writeframes(arr.tobytes())
                        elif idx == 4:
                            wf.writeframes(ch4.tobytes())
                        else:
                            # Unavailable channel: write silence
                            wf.writeframes(np.zeros(FPP, dtype=np.int16).tobytes())

                # Discard the first N packets after wakeword
                # Only accept audio from the front-end that sent WAKE_DETECTED
                if self._active_front_end is not None and addr[0] != self._active_front_end:
                    continue
                if self._discard_packets_after_wake > 0:
                    self._discard_packets_after_wake -= 1
                    if self._discard_packets_after_wake == 0:
                        log.info("Discard window after wakeword complete; now buffering audio for STT.")
                    continue

                elapsed = time.time() - self.utterance_start

                rms = self._rms(ch4)
                is_speech = rms >= SILENCE_THRESHOLD
                is_onset_speech = rms >= SPEECH_ONSET_RMS

                if not self.speech_detected:
                    # ── Phase 1: waiting for speech onset ──────────────────────
                    # Do NOT append to self.buffer yet — accumulate only into
                    # the pre-roll ring so pre-command silence is not sent to
                    # Whisper.  Once onset is confirmed, we prepend the last
                    # PRE_ROLL_FRAMES frames to capture the phoneme attack.
                    self.pre_roll.append(audio_4ch)

                    prev_count = self.speech_onset_count
                    if is_onset_speech:
                        self.speech_onset_count += 1
                        if self.speech_onset_count != prev_count:
                            log.debug(f"Phase1 onset_count={self.speech_onset_count} "
                                      f"t={elapsed:.3f}s rms={rms:.0f} onset_thr={SPEECH_ONSET_RMS}")
                        if self.speech_onset_count >= SPEECH_ONSET_FRAMES:
                            self.speech_detected = True
                            self.silence_count   = 0
                            self.noise_count     = 0
                            self.speech_onset_elapsed_s = elapsed
                            # Flush pre-roll into main buffer
                            self.buffer.extend(list(self.pre_roll))
                            self.pre_roll.clear()
                            log.info(f"Speech onset at {elapsed:.2f}s post-wake "
                                     f"(rms={rms:.0f})")
                    else:
                        if self.speech_onset_count > 0:
                            log.debug(f"Phase1 onset reset t={elapsed:.3f}s rms={rms:.0f}")
                        self.speech_onset_count = max(0, self.speech_onset_count - 1)

                    # Max utterance still applies
                    if elapsed > MAX_UTTERANCE_S:
                        log.warning("Max utterance duration reached (Phase 1) — aborting")
                        self._abort_utterance()
                    elif elapsed > PRE_SPEECH_TIMEOUT_S:
                        log.warning("Pre-speech timeout — no command detected")
                        self._abort_utterance()
                else:
                    # ── Phase 2: buffering in-speech audio ───────────────────
                    self.buffer.append(audio_4ch)
                    self.phase2_total_frames += 1

                    if elapsed > MAX_UTTERANCE_S:
                        log.warning("Max utterance duration reached — flushing")
                        self._end_of_speech_detected('max duration')
                        continue

                    if not is_speech:
                        self.silence_count += 1
                        self.noise_count    = 0
                    else:
                        self.phase2_speech_frames += 1
                        self.phase2_speech_rms_sum += rms
                        self.noise_count += 1
                        if self.noise_count > SILENCE_TOLERANCE:
                            # Soft reset: penalise but don't zero — prevents
                            # background noise from continuously blocking VAD
                            self.silence_count = max(
                                0, self.silence_count - self.noise_count
                            )
                            self.noise_count = 0

                    if self.silence_count >= SILENCE_FRAMES:
                        log.info(f"VAD: {self.silence_count} silent frames "
                                 f"({SILENCE_DURATION_S:.2f}s) — end of speech")
                        self._end_of_speech_detected('VAD silence')

            except socket.timeout:
                continue
            except Exception as e:
                log.error(f"Audio RX error: {e}")

    def start(self):
        threading.Thread(target=self.control_listener, daemon=True).start()
        threading.Thread(target=self.audio_listener,   daemon=True).start()
        log.info("AudioReceiver started")
