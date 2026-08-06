# Kokoro TTS Setup Guide

## Overview

Herald uses Kokoro-82M (via `Kokoro-FastAPI` CPU image) for local text-to-speech synthesis on Linux ARM64 CPU.

## Host Model Directories

Models and voice weights are mounted from `/opt/herald/models/kokoro`:

```text
/opt/herald/models/kokoro/
├── kokoro-v0_19.onnx
└── voices/
    ├── af_heart.bin
    └── af_bella.bin
```

## Running the Smoke Test

To verify Kokoro container health and test audio synthesis:

```bash
make smoke-test
```
