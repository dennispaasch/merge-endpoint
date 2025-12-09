from fastapi.responses import FileResponse
from fastapi import FastAPI, HTTPException, Body
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests, json, time

app = FastAPI()
APP_VERSION = "merge-v5-url-2025-12-09"

# Ordner für fertige Dateien (Render erlaubt /tmp)
STORE_DIR = "/tmp/merged_store"
os.makedirs(STORE_DIR, exist_ok=True)

@app.get("/version")
def version():
    return {"version": APP_VERSION}

@app.post("/merge")
def merge(payload=Body(...)):
    if isinstance(payload, str):
        payload = json.loads(payload)

    a_url = payload.get("a_url")
    b_url = payload.get("b_url")
    min_silence_ms = int(payload.get("min_silence_ms", 2000))
    silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))

    if not a_url or not b_url:
        raise HTTPException(400, "a_url and b_url required")

    def download(url, path):
        r = requests.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"Download failed: {url}")
        with open(path, "wb") as f:
            f.write(r.content)

    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.mp3")
        b_path = os.path.join(td, "b.mp3")
        download(a_url, a_path)
        download(b_url, b_path)

        a = AudioSegment.from_file(a_path).set_frame_rate(22050).set_channels(1)
        b = AudioSegment.from_file(b_path).set_frame_rate(22050).set_channels(1)

        def chunks(audio):
            ranges = silence.detect_nonsilent(
                audio,
                min_silence_len=min_silence_ms,
                silence_thresh=silence_thresh_dbfs
            )
            if len(ranges) > 200:
                return [audio]
            return [audio[s:e] for s, e in ranges]

        a_chunks = chunks(a)
        b_chunks = chunks(b)

        out = AudioSegment.silent(0)
        i = 0
        while i < len(a_chunks) or i < len(b_chunks):
            if i < len(a_chunks):
                out += a_chunks[i]
            if i < len(b_chunks):
                out += b_chunks[i]
            i += 1

    # Datei dauerhaft unter ID speichern
    file_id = uuid.uuid4().hex
    out_path = os.path.join(STORE_DIR, f"{file_id}.mp3")
    out.export(out_path, format="mp3")

    # öffentliche Download-URL zurückgeben
    download_url = f"https://merge-endpoint.onrender.com/download/{file_id}"
    return {"download_url": download_url, "filename": "merged.mp3"}


@app.get("/download/{file_id}")
def download_file(file_id: str):
    path = os.path.join(STORE_DIR, f"{file_id}.mp3")
    if not os.path.exists(path):
        raise HTTPException(404, "file not found")
    return FileResponse(path, media_type="audio/mpeg", filename="merged.mp3")
