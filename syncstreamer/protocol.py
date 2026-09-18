import struct

MAGIC             = 0xEE15A3D1
FRAMES_PER_PACKET = 256
SAMPLE_RATE       = 48000
PCM_BYTES_MONO    = FRAMES_PER_PACKET * 2       # 512 bytes
PCM_BYTES_STEREO  = FRAMES_PER_PACKET * 4       # 1024 bytes
PACKET_SIZE       = 16 + PCM_BYTES_STEREO       # 1040 bytes

HEADER_FMT  = "<IIQ"           # little-endian: magic, seq, present_us
HEADER_SIZE = struct.calcsize(HEADER_FMT)  # 16 bytes

def pack(sequence: int, present_us: int, pcm: bytes) -> bytes:
    header = struct.pack(HEADER_FMT, MAGIC, sequence, present_us)
    return header + pcm
