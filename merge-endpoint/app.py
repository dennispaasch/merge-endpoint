from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests, json, time

app = FastAPI()
APP_VERSION = "merge-v3-urls-tempfix-2025-12-09"

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/version")
def version():
    return {"version": APP_VERSION}

@app.post("/merge")
def merge(payload=Body(...)):
    t0 = time.time()

    # Langdock schickt evtl. Text -> parsen
    if isinstance(payload, str):
        payload = json.loads(payload)

    a_url = payload.get("a_url")
    b_url = payload.get("b_url")
    min_silence_ms = int(payload.get("min_silence_ms", 2000))
    silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))
    keep_silence_ms = int(payload.get("keep_silence_ms", 0))

    if not a_url or not b_url:
        raise HTTPException(400, "a_url and b_url are required")

    print("MERGE start", {"a_url": a_url[:60], "b_url": b_url[:60]})

    def download(url, path):
        print("download", url[:60])
        r = requests.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"Download failed: {url}")
        with open(path, "wb") as f:
            f.write(r.content)
        print("downloaded bytes", os.path.getsize(path))

    # Inputs in Temp-Ordner laden
    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.mp3")
        b_path = os.path.join(td, "b.mp3")

        download(a_url, a_path)
        download(b_url, b_path)

        print("load audio")
        a = AudioSegment.from_file(a_path).set_frame_rate(22050).set_channels(1)
        b = AudioSegment.from_file(b_path).set_frame_rate(22050).set_channels(1)

        # schneller als split_on_silence: detect_nonsilent + slicing
        def chunks_from_nonsilent(audio):
            ranges = silence.detect_nonsilent(
                audio,
                min_silence_len=min_silence_ms,
                silence_thresh=silence_thresh_dbfs
            )
            # Safety: wenn zu viele Mini-Segmente entstehen -> kein Split
            if len(ranges) > 200:
                return [audio]
            return [audio[start:end] for start, end in ranges]

        print("detect chunks")
        a_chunks = chunks_from_nonsilent(a)
        b_chunks = chunks_from_nonsilent(b)
        print("chunks", len(a_chunks), len(b_chunks))

        if not a_chunks and not b_chunks:
            raise HTTPException(400, "No speech chunks detected")

        out = AudioSegment.silent(duration=0)
        i = 0
        while i < len(a_chunks) or i < len(b_chunks):
            if i < len(a_chunks):
                out += a_chunks[i]
            if i < len(b_chunks):
                out += b_chunks[i]
            i += 1

    # Output außerhalb TemporaryDirectory speichern
    out_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    out_path = out_file.name
    out_file.close()

    print("export", out_path)
    out.export(out_path, format="mp3")
    print("export done size", os.path.getsize(out_path), "sec", round(time.time()-t0, 2))

    return FileResponse(out_path, media_type="audio/mpeg", filename="merged.mp3")
