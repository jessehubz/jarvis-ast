#!/usr/bin/env python3
"""
Quick diagnostic — shows the EXACT keys and scores from openWakeWord in real-time.
Run this, say "hey jarvis", and watch the terminal.
Ctrl+C to quit.
"""
import sys
import threading
import time

import numpy as np
import sounddevice as sd
import openwakeword
from openwakeword.model import Model

SAMPLE_RATE = 16000
CHUNK_SIZE  = 1280

print("Loading openWakeWord…", flush=True)
openwakeword.utils.download_models()
oww = Model(wakeword_models=["hey_jarvis"], inference_framework="onnx")
print("Ready — say 'hey jarvis' now. Keys:", list(oww.prediction_buffer.keys()))
print("─" * 50)

peak = {}

def cb(indata, _f, _t, _s):
    chunk = indata.flatten().copy()
    preds = oww.predict(chunk)
    for k, v in preds.items():
        v = float(v)
        if v > peak.get(k, 0.0):
            peak[k] = v
        if v > 0.05:
            print(f"  ▶ {k}: {v:.3f}  (peak so far: {peak[k]:.3f})", flush=True)

with sd.InputStream(samplerate=SAMPLE_RATE, channels=1, dtype="int16",
                    blocksize=CHUNK_SIZE, callback=cb):
    try:
        while True:
            time.sleep(5)
            print(f"[{time.strftime('%H:%M:%S')}] peaks so far: {peak}", flush=True)
    except KeyboardInterrupt:
        print("\nDone. Final peaks:", peak)
