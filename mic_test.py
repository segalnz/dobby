#!/home/ron/venvs/assistant/bin/python3
"""Mic capture + playback — r=record s=save p=play q=quit (deletes temp)."""
import socket, struct, numpy as np, sys, os, wave, select, termios, tty, time

AUDIO_PORT = 4000; SAMPLE_RATE = 16000; CH = 5; FPP = 128; HDR = 16; MAGIC = 0x53504D31

def get_key():
    if select.select([sys.stdin], [], [], 0.1)[0]: return sys.stdin.read(1)
    return ''

def _pr(s="", end="\r\n"):
    sys.stdout.write(str(s) + end); sys.stdout.flush()

def raw_mode():
    fd = sys.stdin.fileno(); old = termios.tcgetattr(fd); tty.setraw(fd); return old

class MicCapture:
    def __init__(self):
        self.s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.s.bind(('', AUDIO_PORT)); self.s.settimeout(0.1)
        self.buf = []; self.ch = 1; self.rec = False; self.pkts = 0

    def parse(self, data):
        if len(data) < HDR: return None
        m, _, _, f, c = struct.unpack('<IIIHH', data[:HDR])
        if m != MAGIC: return None
        return np.frombuffer(data, dtype=np.int16, count=f*c, offset=HDR).reshape(-1,c).T

    def start(self):
        self.buf = []; self.rec = True; self.pkts = 0
        _pr(f"[REC] ch{self.ch} (s=stop+save):")

    def stop(self):
        self.rec = False
        n = len(self.buf) * FPP
        if n > 0: _pr(f"[REC] {n} samples ({n/SAMPLE_RATE:.1f}s, {self.pkts} pkts)")
        else: _pr("[REC] no audio")

    def audio(self, ch=None):
        c = ch if ch is not None else self.ch
        if not self.buf: return None
        return np.concatenate([p[c] for p in self.buf])

    def poll(self):
        if not self.rec: return
        try:
            d, _ = self.s.recvfrom(4096)
            p = self.parse(d)
            if p is not None:
                self.buf.append(p); self.pkts += 1
                if self.pkts % 50 == 0:
                    sys.stdout.write(f".{self.pkts}\r"); sys.stdout.flush()
        except socket.timeout: pass

def play_wav(fname):
    try:
        sys.path.insert(0, os.path.dirname(__file__))
        from syncstreamer import get_syncstreamer, TtsSource, SyncStreamerConfig, init_syncstreamer
        s = get_syncstreamer() or init_syncstreamer(SyncStreamerConfig())
        with wave.open(fname, 'rb') as w:
            pcm = np.frombuffer(w.readframes(w.getnframes()), dtype=np.int16)
            sr = w.getframerate()
        f32 = pcm.astype(np.float32) / 32768.0
        src = TtsSource(f32, sr)
        _pr(f"[PLAY] {fname} ({len(pcm)/sr:.1f}s)...")
        s.play_tts(src)
        _pr("[PLAY] done")
    except Exception as e: _pr(f"[PLAY] error: {e}")

def main():
    old = raw_mode()
    cap = MicCapture(); ch = 1; tmp = []; last_save = None
    _pr("=== Mic Test - No Wakeword ===")
    _pr("r=record  s=stop+save  p=play  c0-c4=ch  q=quit")
    _pr(f"ch={ch}")
    try:
        while True:
            k = get_key()
            if k == 'r':
                cap.start()
            elif k == 's':
                if not cap.rec:
                    _pr("[SAVE] not recording")
                    continue
                cap.stop()
                pcm = cap.audio(ch)
                if pcm is not None and len(pcm) > 0:
                    fn = f"/tmp/mic_{int(time.time())}.wav"
                    with wave.open(fn, 'wb') as w:
                        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
                        w.writeframes(pcm.tobytes())
                    tmp.append(fn); last_save = fn
                    _pr(f"[SAVE] {fn}")
                else: _pr("[SAVE] no audio")
            elif k == 'p':
                if cap.rec:
                    cap.stop()
                    pcm = cap.audio(ch)
                    if pcm is not None and len(pcm) > 0:
                        fn = f"/tmp/mic_{int(time.time())}.wav"
                        with wave.open(fn, 'wb') as w:
                            w.setnchannels(1); w.setsampwidth(2); w.setframerate(SAMPLE_RATE)
                            w.writeframes(pcm.tobytes())
                        tmp.append(fn); last_save = fn
                if last_save and os.path.exists(last_save):
                    play_wav(last_save)
                else: _pr("[PLAY] nothing saved")
            elif k == 'c':
                d = sys.stdin.read(1)
                if d.isdigit() and 0 <= int(d) <= 4:
                    ch = int(d); cap.ch = ch
                    _pr(f"[CH] channel={ch}")
                else:
                    _pr(f"[CH] invalid: {d!r}")
            elif k in ('q', '\x03'):
                if cap.rec: cap.stop()
                break
            if cap.rec: cap.poll()
    finally:
        termios.tcsetattr(sys.stdin.fileno(), termios.TCSADRAIN, old)
        for f in tmp:
            try: os.unlink(f); print(f"[CLEAN] {f}")
            except OSError: pass
        print("Done.")

if __name__ == '__main__': main()
