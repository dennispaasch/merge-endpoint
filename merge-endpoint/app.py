from fastapi import FastAPI, File, UploadFile, Form, HTTPException
from fastapi.responses import FileResponse
from pydub import AudioSegment, silence
import tempfile, os, uuid

app = FastAPI()

@app.get("/health")
def health():
    return {"ok": True}

@app.post("/merge")
async def merge(
    a_file: UploadFile = File(...),
    b_file: UploadFile = File(...),
    min_silence_ms: int = Form(2000),
    silence_thresh_dbfs: int = Form(-35),
    keep_silence_ms: int = Form(0),
):
    if not a_file.filename or not b_file.filename:
        raise HTTPException(400, "Both a_file and b_file are required.")

    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, a_file.filename)
        b_path = os.path.join(td, b_file.filename)

        with open(a_path, "wb") as f:
            f.write(await a_file.read())
        with open(b_path, "wb") as f:
            f.write(await b_file.read())

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
            raise HTTPException(400, "No speech chunks detected in either file.")

        out = AudioSegment.silent(duration=0)
        i = 0
        while i < len(a_chunks) or i < len(b_chunks):
            if i < len(a_chunks):
                out += a_chunks[i]
            if i < len(b_chunks):
                out += b_chunks[i]
            i += 1

        out_name = f"merged_{uuid.uuid4().hex}.mp3"
        out_path = os.path.join(td, out_name)
        out.export(out_path, format="mp3")

        return FileResponse(out_path, media_type="audio/mpeg", filename="merged.mp3")
