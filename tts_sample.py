import argparse
import logging

from config import KOKORO_VOICE
from tts_kokoro import get_tts

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s %(name)s %(levelname)s %(message)s',
)


def main():
    parser = argparse.ArgumentParser(description="Generate a Kokoro TTS WAV sample")
    parser.add_argument(
        "--text",
        default="Hello. This is bf lily speaking from your local assistant.",
        help="Text to synthesize",
    )
    parser.add_argument(
        "--out",
        default=None,
        help="Output WAV path (default: auto in TTS_OUTPUT_DIR)",
    )
    args = parser.parse_args()

    tts = get_tts()
    wav = tts.synth_to_wav(args.text, out_path=args.out)
    print(f"voice={KOKORO_VOICE}")
    print(f"wav={wav}")


if __name__ == "__main__":
    main()
