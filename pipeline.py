# ~/assistant/pipeline.py
import queue
import logging
import time
import re
import os
import numpy as np
from collections import deque
from receiver import AudioReceiver
from stt import transcribe
# from tts_kokoro import get_tts
from tts_dispatcher import get_tts
from command_router import get_command_router
from conversation import ConversationMemory
from syncstreamer import init_syncstreamer, get_syncstreamer, TtsSource, JellyfinSource, SyncStreamerConfig
from cloud_provider import get_cloud_provider
from beamformer import MVDRBeamformer
from config import *

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s'
)
logging.getLogger('receiver').setLevel(logging.INFO)
logging.getLogger('werkzeug').setLevel(logging.WARNING)
log = logging.getLogger(__name__)

STT_TEST_MODE = STT_TEST_MODE_FILE.exists() or os.environ.get('ASSISTANT_STT_TEST_MODE', '0').strip() in ('1', 'true', 'TRUE', 'yes', 'YES')

# Frequent low-information hallucinations from noise bursts.
_SPURIOUS_SINGLE_WORDS = {
    'the', 'you', 'yeah', 'yes', 'no', 'okay', 'ok',
    'sorry', 'hello', 'hi', 'um', 'uh', "i'm",
}
_SPURIOUS_SINGLE_MAX_DURATION_S = 2.6
_SPURIOUS_SINGLE_MAX_MEAN_RMS = 430.0


def _percentile(values, p: float) -> float:
    if not values:
        return 0.0
    if len(values) == 1:
        return float(values[0])
    ordered = sorted(values)
    idx = int(round((p / 100.0) * (len(ordered) - 1)))
    idx = max(0, min(idx, len(ordered) - 1))
    return float(ordered[idx])


def _is_probably_spurious(text: str, flush_duration: float, meta: dict | None) -> bool:
    cleaned = (text or '').strip().lower()
    if not cleaned:
        return True

    # Strip punctuation and split to words for quick heuristics.
    words = [w for w in re.sub(r"[^a-z0-9' ]+", ' ', cleaned).split() if w]
    if not words:
        return True

    mean_speech_rms = 9999.0
    if meta:
        voiced_ratio = float(meta.get('voiced_ratio', 0.0) or 0.0)
        speech_frames = int(meta.get('speech_frames', 0) or 0)
        mean_speech_rms = float(meta.get('mean_speech_rms', 0.0) or 0.0)

        # Conservative reject for extremely weak/brief voice evidence.
        if speech_frames < 10 and voiced_ratio < 0.16 and mean_speech_rms < 1400:
            return True

    # Single filler tokens should never drive cloud/TTS responses.
    # Keep this strict enough to block noise captures while allowing real commands.
    if len(words) == 1 and words[0] in _SPURIOUS_SINGLE_WORDS:
        if flush_duration <= _SPURIOUS_SINGLE_MAX_DURATION_S:
            return True
        if mean_speech_rms <= _SPURIOUS_SINGLE_MAX_MEAN_RMS:
            return True

    # Whisper hallucination: repetitive single-token output on pure noise
    # e.g. "F F F F F..." or "Fffffffffffff..." from preamble tones
    if len(words) >= 8:
        unique_words = set(words)
        if len(unique_words) == 1:
            return True
        if len(unique_words) <= 2 and len(words) >= 16:
            return True
    # Single long token made of one repeated character (e.g. "Fffffffff")
    if len(words) == 1 and len(words[0]) >= 8:
        if len(set(words[0])) == 1:
            return True

    return False

def main():
    audio_queue = queue.Queue()
    receiver = AudioReceiver(audio_queue)
    receiver.start()

    beamformer = MVDRBeamformer(sample_rate=SAMPLE_RATE)

    tts = get_tts() if ENABLE_TTS and TTS_ECHO_TRANSCRIPT else None
    syncstreamer = init_syncstreamer(SyncStreamerConfig())
    command_router = get_command_router()
    # Cloud-generated replies are cached locally; failed refresh keeps known-good data.
    import threading as _thr
    def _refresh_loop():
        import random
        while True:
            time.sleep(random.uniform(RESPONSE_REFRESH_MIN_SECONDS, RESPONSE_REFRESH_MAX_SECONDS))
            try:
                command_router.refresh_dobby_responses()
            except Exception:
                log.exception("Response refresh failed; keeping local cache")
    if RESPONSE_REFRESH_ENABLED:
        if not 0 < RESPONSE_REFRESH_MIN_SECONDS <= RESPONSE_REFRESH_MAX_SECONDS:
            raise ValueError("Invalid response refresh interval")
        _thr.Thread(target=_refresh_loop, daemon=True).start()
    memory = ConversationMemory(max_messages=12)
    cloud = get_cloud_provider()

    from jellyfin_music import init_music_controller
    tts_for_music = get_tts() if ENABLE_TTS and TTS_ECHO_TRANSCRIPT else None
    music = init_music_controller(syncstreamer, tts_for_music)
    from music_ipc import start_music_ipc
    music_ipc = start_music_ipc(music, MUSIC_IPC_PATH)

    if ENABLE_MQTT_NOTIFICATIONS and tts is not None:
        from mqtt_notifier import MQTTNotifier
        notifier = MQTTNotifier(tts, syncstreamer, receiver)
        notifier.start()
        log.info("MQTT notification listener started on topic: %s", MQTT_NOTIFICATION_TOPIC)

    flush_latencies = deque(maxlen=LATENCY_STATS_WINDOW)
    stt_latencies = deque(maxlen=LATENCY_STATS_WINDOW)
    samples = 0

    if ENABLE_WEB_SERVER:
        from web_server import start_web_server
        start_web_server(receiver, syncstreamer, tts)
        log.info("Web server started on http://%s:%d", HOST_IP, WEB_SERVER_PORT)

    if STT_TEST_MODE:
        log.info("Pipeline ready — waiting for wake word (STT test mode: transcribe only)")
    else:
        log.info("Pipeline ready — waiting for wake word")

    while True:
        try:
            # Wait for a flushed utterance from receiver.
            # Timeout is 10s — long enough to cover:
            #   - PROCESSING sent by host VAD
            #   - ESP processing and emitting AUDIO_END
            #   - Any network jitter
            # If AUDIO_END never arrives (lost UDP packet), we recover
            # by sending READY to unblock the ESP from any stuck state.
            item = audio_queue.get(timeout=10.0)
            meta = None
            if isinstance(item, tuple) and len(item) == 3:
                audio_4ch, flush_duration, meta = item
            elif isinstance(item, tuple) and len(item) == 2:
                audio, flush_duration = item
                audio_4ch = None
            else:
                audio = item
                flush_duration = len(audio) / SAMPLE_RATE
                audio_4ch = None

            if audio_4ch is not None:
                audio_beamformed = beamformer.process(audio_4ch)
                snr = beamformer.snr_estimate_db(audio_beamformed)
                log.info("Utterance received — %d samples, ch%d SNR: %.1f dB",
                         audio_4ch.shape[1], beamformer._fallback_ch, snr)

                audio = (audio_beamformed * 32767.0).clip(-32768, 32767).astype(np.int16)
            else:
                log.info("Utterance received — running STT (no MVDR)")

            # Barge-in: duck speakers only — music continues in background
            # DUCK_START already sent on WAKE_DETECTED — music cancellation handled by MusicController


            t0 = time.time()

            text = transcribe(audio)

            elapsed = time.time() - t0
            log.info(f"STT completed in {elapsed:.2f}s")

            flush_latencies.append(float(flush_duration))
            stt_latencies.append(float(elapsed))
            samples += 1

            if samples % LATENCY_STATS_REPORT_EVERY == 0:
                log.info(
                    "Latency stats n=%d window=%d flush_s[p50=%.2f p95=%.2f] "
                    "stt_s[p50=%.2f p95=%.2f]",
                    samples,
                    len(stt_latencies),
                    _percentile(flush_latencies, 50),
                    _percentile(flush_latencies, 95),
                    _percentile(stt_latencies, 50),
                    _percentile(stt_latencies, 95),
                )

            if text:
                if _is_probably_spurious(text, float(flush_duration), meta):
                    log.warning(
                        "Discarding likely spurious STT: text=%r duration=%.2fs meta=%s",
                        text,
                        float(flush_duration),
                        meta,
                    )
                    syncstreamer.on_mic_close()
                    receiver.send_control('READY')
                    continue

                if STT_TEST_MODE:
                    log.info("STT test mode active — skipping routing/cloud/TTS")
                    receiver.send_control('READY')
                    continue

                log.info(f"Transcribed: '{text}'")
                # Drop blank/empty STT output silently
                if not text or text.strip() in ('[BLANK_AUDIO]', '[ Silence ]', ''):
                    log.info("Blank STT — discarding")
                    receiver.send_control('READY')
                    continue
                decision = command_router.handle_text(text)
                log.info(
                    "Command routing handled_locally=%s forward_to_cloud=%s parsed=%s",
                    decision.handled_locally,
                    decision.forward_to_cloud,
                    decision.parsed,
                )

                if decision.forward_to_cloud:
                    cloud_query = text
                    if isinstance(decision.parsed, dict):
                        parsed_query = decision.parsed.get('cloud_query')
                        if isinstance(parsed_query, str) and parsed_query.strip():
                            cloud_query = parsed_query.strip()

                    if tts is not None and hasattr(cloud, 'stream_reply'):
                        # Speak an immediate acknowledgment so the user knows we're
                        # working — OpenClaw buffers during tool execution so the
                        # first real token may arrive 30-60s later.
                        receiver.send_control('TTS_START')
                        try:
                            ack_audio, ack_sr = tts.synth("Right away sir.")
                            if syncstreamer is not None:
                                src = TtsSource(ack_audio, ack_sr)
                                syncstreamer.play_tts(src)
                        except Exception as ack_err:
                            log.warning("Acknowledgment TTS failed: %s", ack_err)

                        spoken_sentences = []

                        def _speak_sentence(sentence: str):
                            try:
                                audio_s, sr_s = tts.synth(sentence)
                                if syncstreamer is not None:
                                    src = TtsSource(audio_s, sr_s)
                                    syncstreamer.play_tts(src)
                                spoken_sentences.append(sentence)
                            except Exception as tts_err:
                                log.error("TTS streaming error: %s", tts_err)
                                raise

                        response_text = cloud.stream_reply(cloud_query, memory.get_messages(), _speak_sentence)
                        # TTS already sent — skip the normal TTS block below.
                        memory.add_user(text)
                        memory.add_assistant(response_text)
                        if UDP_TTS_END_GRACE_S > 0:
                            time.sleep(UDP_TTS_END_GRACE_S)
                        receiver.send_control('TTS_END')
                        syncstreamer.on_mic_close()
                        receiver.send_control('READY')
                        continue
                    else:
                        response_text = cloud.reply(cloud_query, memory.get_messages())
                else:
                    response_text = decision.response_text

                memory.add_user(text)
                memory.add_assistant(response_text)

                if tts is not None and decision.parsed.get('intent') != 'music_control':
                    try:
                        audio_tts, sr_tts = tts.synth(response_text)
                        source = TtsSource(audio_tts, sr_tts, gain=SNAPCAST_GAIN)
                        syncstreamer.play_tts(source)
                        log.info(f"TTS streamed via syncstreamer")
                    except Exception as tts_err:
                        log.error(f"TTS error: {tts_err}")
            else:
                log.warning("Empty transcription — discarding")

            # Barge-in: restore speaker volume after response
            syncstreamer.on_mic_close()

            # In "before" mode, always attempt execution and speak publication failure.
            failure = command_router.execute_deferred_action()
            if failure:
                memory.add_assistant(failure)
                log.error(failure)
                if tts is not None:
                    audio_failed, sr_failed = tts.synth(failure)
                    syncstreamer.play_tts(TtsSource(audio_failed, sr_failed, gain=SNAPCAST_GAIN))
            # Signal ESP ready for next utterance
            receiver.send_control('READY')



        except queue.Empty:
            # Reports persist until spoken. Do not interrupt an active microphone window.
            if not receiver.processing_sent and not getattr(receiver, 'streaming', False):
                for report_id, report in command_router.automations.reports():
                    if tts is not None and syncstreamer._registry.count() == 0:
                        break
                    log.info("Automation report: %s", report)
                    try:
                        if tts is not None:
                            receiver.send_control('TTS_START')
                            report_audio, report_sr = tts.synth(report)
                            syncstreamer.play_tts(TtsSource(report_audio, report_sr, gain=SNAPCAST_GAIN))
                        command_router.automations.acknowledge_report(report_id)
                    except Exception:
                        log.exception("Could not speak automation report; retained for retry")
                        break
                    finally:
                        receiver.send_control('TTS_END')
                        receiver.send_control('READY')
            # No utterance arrived within 10s of PROCESSING being sent.
            # This means AUDIO_END was lost (dropped UDP packet).
            # Send READY to recover ESP from any stuck state.
            # Note: this fires every 10s during normal idle too — that is
            # harmless as ESP ignores READY when already idle.
            if receiver.processing_sent:
                log.warning("Timeout waiting for AUDIO_END — sending READY to recover")
                receiver.processing_sent = False
                receiver.send_control('READY')

        except KeyboardInterrupt:
            log.info("Shutdown requested")
            break
        except Exception as e:
            log.error(f"Pipeline error: {e}", exc_info=True)
            # Attempt recovery
            receiver.send_control('READY')
            try:
                syncstreamer.send_ready_ping()
            except Exception:
                pass

if __name__ == '__main__':
    main()
