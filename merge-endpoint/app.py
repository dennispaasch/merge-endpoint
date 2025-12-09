from fastapi import FastAPI, HTTPException, Body
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests, json

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/merge")
def merge(payload=Body(...)):
    # Langdock schickt evtl. Text -> falls ja, in dict umwandeln
    if isinstance(payload, str):
        payload = json.loads(payload)

    a_url = payload.get("a_url")
    b_url = payload.get("b_url")
    min_silence_ms = int(payload.get("min_silence_ms", 2000))
    silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))
    keep_silence_ms = int(payload.get("keep_silence_ms", 0))

    if not a_url or not b_url:
        raise HTTPException(400, "a_url and b_url are required")

    # ---- Inputs in Temp-Ordner laden
    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.mp3")
        b_path = os.path.join(td, "b.mp3")

        def download(url, path):
            r = requests.get(url, timeout=30, allow_redirects=True)
            if r.status_code != 200:
                raise HTTPException(400, f"Download failed: {url}")
            with open(path, "wb") as f:
                f.write(r.content)

        download(a_url, a_path)
        download(b_url, b_path)

        a = AudioSegment.from_file(a_path)
        b = AudioSegment.from_file(b_path)

        def split_chunks(audio):
            return silence.split_on_silence(
                audio,
                min_silence_len=min_silence_ms,
                silence_thresh=silence_thresh_dbfs,
                keep_silence=keep_silence_ms
            )

        a_chunks = split_chunks(a)
        b_chunks = split_chunks(b)

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

    # ---- Output außerhalb des TemporaryDirectory speichern!
    out_file = tempfile.NamedTemporaryFile(
        suffix=".mp3", delete=False
    )
    out_path = out_file.name
    out_file.close()

    out.export(out_path, format="mp3")

    return FileResponse(
        out_path,
        media_type="audio/mpeg",
        filename="merged.mp3"
    )
