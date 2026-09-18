#!/usr/bin/env python3
"""Control the live assistant music player via a private Unix socket."""
import argparse
import json
import sys
from config import MUSIC_IPC_PATH, MUSIC_IPC_TIMEOUT_SECONDS
from music_ipc import request_music


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--json', action='store_true', help='Print the structured result')
    sub = parser.add_subparsers(dest='command', required=True)
    for name in ('play', 'search'):
        p = sub.add_parser(name)
        p.add_argument('--query', required=True)
        if name == 'play':
            p.add_argument('--type', choices=['artist', 'album', 'track', 'playlist'])
    p = sub.add_parser('control')
    p.add_argument('--action', required=True, choices=['pause', 'stop', 'resume', 'next', 'previous', 'now'])
    sub.add_parser('status')
    args = vars(parser.parse_args())
    as_json = args.pop('json')
    try:
        result = request_music(MUSIC_IPC_PATH, args, MUSIC_IPC_TIMEOUT_SECONDS)
    except (OSError, ValueError) as exc:
        print('Assistant music service is unavailable: ' + str(exc), file=sys.stderr)
        return 1
    if as_json:
        print(json.dumps(result))
    elif not result.get('ok'):
        print(result.get('error', 'Music command failed'), file=sys.stderr)
    elif 'items' in result:
        print('\n'.join(f"[{i['type']}] {i['name']}" for i in result['items']) or 'No matching music found.')
    else:
        print(result.get('response', 'Done.'))
    return 0 if result.get('ok') else 1


if __name__ == '__main__':
    raise SystemExit(main())
