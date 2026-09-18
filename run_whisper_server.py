"""Launch whisper-server using the same config as the CLI fallback."""
import json
import os
import sys
import config


def command():
    args = [config.WHISPER_SERVER_BIN, '-m', config.WHISPER_MODEL,
            '--host', config.WHISPER_SERVER_HOST, '--port', str(config.WHISPER_SERVER_PORT),
            '-t', str(config.WHISPER_THREADS), '--language', 'en',
            '-bo', str(config.WHISPER_BEST_OF), '-bs', str(config.WHISPER_BEAM_SIZE),
            '-ac', str(config.WHISPER_AUDIO_CTX)]
    if config.WHISPER_INITIAL_PROMPT:
        args += ['--prompt', config.WHISPER_INITIAL_PROMPT]
    return args


if __name__ == '__main__':
    args = command()
    if '--print-command' in sys.argv:
        print(json.dumps(args))
    else:
        os.execv(args[0], args)
