import requests
import time
import base64
import json

# Read a small WAV file (JFK sample)
with open('whisper/samples/jfk.wav', 'rb') as f:
    audio_data = base64.b64encode(f.read()).decode('utf-8')

payload = {
    "audio": {
        "data": audio_data,
        "format": "wav"
    },
    "model": "tiny.en",
    "task": "transcribe",
    "language": "en"
}

start = time.time()
response = requests.post('http://127.0.0.1:8081/inference', json=payload)
end = time.time()

print(f"Response time: {end - start:.3f}s")
print(f"Status code: {response.status_code}")
if response.status_code == 200:
    result = response.json()
    print(f"Transcription: {result.get('text', 'No text')}")
    print(f"Full response keys: {list(result.keys())}")
else:
    print(f"Error: {response.text}")
