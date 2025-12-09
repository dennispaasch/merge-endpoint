from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/merge")
def merge(payload: dict):
    a_url = payload.get("a_url")
    b_url = payload.get("b_url")
    min_silence_ms = int(payload.get("min_silence_ms", 2000))
    silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))
    keep_silence_ms = int(payload.get("keep_silence_ms", 0))

    if not a_url or not b_url:
        raise HTTPException(400, "a_url and b_url are required")

    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.mp3")
        b_path = os.path.join(td, "b.mp3")

        ra = requests.get(a_url)
        rb = requests.get(b_url)
        if ra.status_code != 200 or rb.status_code != 200:
            raise HTTPException(400, "Could not download one of the files")

        with open(a_path, "wb") as f:
            f.write(ra.content)
        with open(b_path, "wb") as f:
            f.write(rb.content)

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

        out_path = os.path.join(td, f"merged_{uuid.uuid4().hex}.mp3")
        out.export(out_path, format="mp3")

        return FileResponse(out_path, media_type="audio/mpeg", filename="merged.mp3")
