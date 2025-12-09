from fastapi import FastAPI, HTTPException, Body
from pydub import AudioSegment, silence
import tempfile, os, uuid, requests, json, time, base64

app = FastAPI()

APP_VERSION = "merge-v4-base64-2025-12-09"

@app.get("/health")
def health():
    return {"ok": True}

@app.get("/version")
def version():
    return {"version": APP_VERSION}


@app.post("/merge")
def merge(payload=Body(...)):
    t0 = time.time()

    # Langdock kann den Body manchmal als String schicken → robust parsen
    if isinstance(payload, str):
        try:
            payload = json.loads(payload)
        except Exception:
            raise HTTPException(400, "Body must be JSON or JSON-string")

    a_url = payload.get("a_url")
    b_url = payload.get("b_url")
    min_silence_ms = int(payload.get("min_silence_ms", 2000))
    silence_thresh_dbfs = int(payload.get("silence_thresh_dbfs", -35))
    keep_silence_ms = int(payload.get("keep_silence_ms", 0))

    if not a_url or not b_url:
        raise HTTPException(400, "a_url and b_url are required")

    print("MERGE start", {
        "a_url": a_url[:80],
        "b_url": b_url[:80],
        "min_silence_ms": min_silence_ms,
        "silence_thresh_dbfs": silence_thresh_dbfs
    })

    def download(url, path):
        print("download", url[:80])
        r = requests.get(url, timeout=25, allow_redirects=True)
        if r.status_code != 200:
            raise HTTPException(400, f"Download failed: {url}")

        # (optional, aber hilfreich) grobe Plausibilitätsprüfung:
        ctype = r.headers.get("content-type", "")
        if ("audio" not in ctype) and ("octet-stream" not in ctype):
            # Drive liefert evtl HTML (Virus-Scan-Page) → lieber sauber abbrechen
            print("WARN content-type:", ctype)

        with open(path, "wb") as f:
            f.write(r.content)

        size = os.path.getsize(path)
        print("downloaded bytes", size)
        if size < 1000:
            raise HTTPException(400, "Downloaded file too small, likely not audio")

    # Inputs in temp-Ordner laden
    with tempfile.TemporaryDirectory() as td:
        a_path = os.path.join(td, "a.mp3")
        b_path = os.path.join(td, "b.mp3")

        download(a_url, a_path)
        download(b_url, b_path)

        print("load audio")
        # normalisieren: mono + niedrigere Samplerate → schneller/stabiler
        a = AudioSegment.from_file(a_path).set_frame_rate(22050).set_channels(1)
        b = AudioSegment.from_file(b_path).set_frame_rate(22050).set_channels(1)

        # schneller als split_on_silence: nonsilent-ranges finden und schneiden
        def chunks_from_nonsilent(audio):
            ranges = silence.detect_nonsilent(
                audio,
                min_silence_len=min_silence_ms,
                silence_thresh=silence_thresh_dbfs
            )
            # Falls extrem viele Mini-Segmente entstehen → nicht splitten
            if len(ranges) > 200:
                return [audio]
            return [audio[start:end] for start, end in ranges]

        print("detect chunks")
        a_chunks = chunks_from_nonsilent(a)
        b_chunks = chunks_from_nonsilent(b)
        print("chunks", len(a_chunks), len(b_chunks))

        if not a_chunks and not b_chunks:
            raise HTTPException(400, "No speech chunks detected")

        # abwechselnd A, B, A, B …
        out = AudioSegment.silent(duration=0)
        i = 0
        while i < len(a_chunks) or i < len(b_chunks):
            if i < len(a_chunks):
                out += a_chunks[i]
            if i < len(b_chunks):
                out += b_chunks[i]
            i += 1

    # Output außerhalb des TemporaryDirectory speichern!
    out_file = tempfile.NamedTemporaryFile(suffix=".mp3", delete=False)
    out_path = out_file.name
    out_file.close()

    print("export", out_path)
    out.export(out_path, format="mp3")

    out_size = os.path.getsize(out_path)
    elapsed = round(time.time() - t0, 2)
    print("export done size", out_size, "sec", elapsed)

    # Base64 zurückgeben (Langdock kann JSON lesen)
    with open(out_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode("utf-8")

    # optional: Datei nach Response löschen
    try:
        os.remove(out_path)
    except Exception:
        pass

    return {
        "filename": "merged.mp3",
        "mimeType": "audio/mpeg",
        "base64": b64,
        "bytes": out_size,
        "took_seconds": elapsed
    }
