#!/usr/bin/env python3
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Depends
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import zipfile
import shutil
import os
import glob
import logging
from pathlib import Path
import httpx
from dotenv import load_dotenv, find_dotenv
import uuid
import csv
from PIL import Image   
import json

security = HTTPBearer(auto_error=False)

# load env
_dotenv = find_dotenv()
if _dotenv:
    load_dotenv(_dotenv)

log = logging.getLogger("ingest_service")
logging.basicConfig(level=logging.INFO)

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)


MANIFESTS_DIR = Path(os.environ.get("MANIFESTS_DIR", "/manifests")).resolve()
MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)

# fallback to localhost:8000
MANIFESTS_BASE_URL = os.environ.get("MANIFESTS_BASE_URL", "http://localhost:8000/manifests")

IIIF_UPLOAD_URL = os.environ.get("IIIF_UPLOAD_URL")  # optional remote ingest endpoint
IIIF_API_KEY = os.environ.get("IIIF_API_KEY") # none atm
CANTALOUPE_IMAGE_DIR = Path(os.environ.get("CANTALOUPE_IMAGE_DIR", "/images")).resolve()
CANTALOUPE_BASE_URL = os.environ.get("CANTALOUPE_BASE_URL")

CANTALOUPE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

TEMP_OUTPUT_DIR = Path(tempfile.gettempdir()) / "lsdp_upload_outputs"
TEMP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)


def create_manifest(image_info: dict):
    """
    image_info: {"iiif": iiif_info_json, "width": w, "height": h, "identifier": fname}
    """
    identifier = image_info["identifier"]
    width = image_info.get("width", 1000)
    height = image_info.get("height", 1000)
    iiif_url = image_info["iiif"]

    # Build manifest id using configured base URL
    manifest_id = f"{MANIFESTS_BASE_URL.rstrip('/')}/{identifier}.json"
    canvas_id = f"{manifest_id}/canvas"
    annotation_page_id = f"{manifest_id}/annotationpage"
    annotation_id = f"{manifest_id}/annotation"

    manifest = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": manifest_id,
        "type": "Manifest",
        "label": {"en": [identifier]},
        "items": [
            {
                "id": canvas_id,
                "type": "Canvas",
                "width": width,
                "height": height,
                "items": [
                    {
                        "id": annotation_page_id,
                        "type": "AnnotationPage",
                        "items": [
                            {
                                "id": annotation_id,
                                "type": "Annotation",
                                "motivation": "painting",
                                "body": {
                                    "id": iiif_url,
                                    "type": "Image",
                                    "format": "image/png",
                                    "service": [
                                        {
                                            "id": iiif_url.replace("/info.json", ""),
                                            "type": "ImageService3",
                                            "profile": "level2"
                                        }
                                    ]
                                },
                                "target": canvas_id
                            }
                        ]
                    }
                ]
            }
        ]
    }

    # save manifest JSON
    manifest_path = MANIFESTS_DIR / f"{identifier}.json"
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)

    return manifest_id

# disabled for now
async def require_upload_token(credentials: HTTPAuthorizationCredentials = Depends(security)):
    """
    If UPLOAD_API_TOKEN env var is set, require a Bearer token that matches it.
    If UPLOAD_API_TOKEN is not set, allow.
    """
    expected = os.environ.get("UPLOAD_API_TOKEN")
    if not expected:
        return True
    if not credentials or credentials.scheme.lower() != "bearer" or credentials.credentials != expected:
        raise HTTPException(status_code=401, detail="Unauthorized")
    return True

def find_images(extract_dir: str):
    candidates = []
    media_dir = os.path.join(extract_dir, "media")
    if os.path.isdir(media_dir):
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.tif", "*.tiff", "*.bmp", "*.gif"):
            candidates.extend(glob.glob(os.path.join(media_dir, "**", ext), recursive=True))
    if not candidates:
        for ext in ("*.jpg", "*.jpeg", "*.png", "*.tif", "*.tiff"):
            candidates.extend(glob.glob(os.path.join(extract_dir, "**", ext), recursive=True))
    return sorted(set(candidates))

async def upload_image_to_iiif(client: httpx.AsyncClient, image_path: str):
    if not IIIF_UPLOAD_URL:
        raise RuntimeError("IIIF_UPLOAD_URL not configured")
    filename = os.path.basename(image_path)
    headers = {}
    if IIIF_API_KEY:
        if IIIF_API_KEY.lower().startswith(("bearer ", "token ")):
            headers["Authorization"] = IIIF_API_KEY
        else:
            headers["Authorization"] = f"Bearer {IIIF_API_KEY}"
    with open(image_path, "rb") as fh:
        files = {"file": (filename, fh, "application/octet-stream")}
        resp = await client.post(IIIF_UPLOAD_URL, files=files, headers=headers, timeout=120.0)
    resp.raise_for_status()
    ctype = resp.headers.get("content-type", "")
    if "application/json" in ctype:
        return resp.json()
    return {"status": "ok", "code": resp.status_code, "text": resp.text}

def save_to_cantaloupe(image_path: str, extract_root: str):
    """
    Copy image into Cantaloupe image dir using the filename only (no directory prefixes).
    Return identifier (including extension) and iiif info.json URL.
    """
    fname = os.path.basename(image_path)
    dest_path = CANTALOUPE_IMAGE_DIR.joinpath(fname)
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(image_path, dest_path)
    identifier = fname  # include extension
    info_json = f"{CANTALOUPE_BASE_URL}/{identifier}/info.json"
    return {"local_path": str(dest_path), "iiif_info_json": info_json, "identifier": identifier}

@app.post("/upload-zip")
# @app.post("/upload-zip", dependencies=[Depends(require_upload_token)])
async def upload_zip(file: UploadFile = File(...), background: BackgroundTasks = None, request: Request = None):
    """
    Accept zip, extract, process images, update page.csv, re-zip output and return download id.
    """
    if not file.filename.lower().endswith(".zip"):
        raise HTTPException(status_code=400, detail="Expected a .zip file")
    tmp_dir = tempfile.mkdtemp(prefix="zip_extract_")
    tmp_zip = os.path.join(tmp_dir, "upload.zip")
    try:
        with open(tmp_zip, "wb") as out:
            content = await file.read()
            out.write(content)
        with zipfile.ZipFile(tmp_zip, "r") as z:
            z.extractall(tmp_dir)

        # use top-level directory as base_dir
        IGNORE_NAMES = {"__MACOSX", ".DS_Store"}
        upload_zip_name = os.path.basename(tmp_zip)
        all_entries = [
            e for e in os.listdir(tmp_dir)
            if e not in IGNORE_NAMES and not e.startswith(".") and e != upload_zip_name
        ]
        dir_entries = [e for e in all_entries if os.path.isdir(os.path.join(tmp_dir, e))]
        non_dir_entries = [e for e in all_entries if not os.path.isdir(os.path.join(tmp_dir, e))]
        if len(dir_entries) == 1 and len(non_dir_entries) == 0:
            base_dir = os.path.join(tmp_dir, dir_entries[0])
        else:
            base_dir = tmp_dir

        images = find_images(base_dir)
        if not images:
            raise HTTPException(status_code=400, detail="No images found in the archive")

        results = []
        saved_map = {}  # filename -> {"iiif": url, "width": w, "height": h}
        async with httpx.AsyncClient() as client:
            for img in images:
                rel_img = os.path.relpath(img, base_dir)
                try:
                    # extract dimensions from the extracted image
                    try:
                        with Image.open(img) as im:
                            width, height = im.size
                    except Exception:
                        width, height = (1000, 1000)

                    # not going to be used for now, but keep for future
                    if IIIF_UPLOAD_URL:
                        try:
                            res = await upload_image_to_iiif(client, img)
                            # attempt to extract identifier/iiif url from response
                            iiif_url = ""
                            if isinstance(res, dict) and res.get("iiif_info_json"):
                                iiif_url = res["iiif_info_json"]
                            results.append({"image": rel_img, "result": res, "destination": "iiif_remote"})
                            saved_map[os.path.basename(img)] = {"iiif": iiif_url, "width": width, "height": height}
                            continue
                        except Exception as e_remote:
                            results.append({"image": rel_img, "warning": "remote upload failed", "error": str(e_remote)})
                    # falls back to: copy into Cantaloupe FS
                    saved = save_to_cantaloupe(img, base_dir)
                    iiif_url = saved.get("iiif_info_json", "")
                    saved_map[os.path.basename(img)] = {"iiif": iiif_url, "width": width, "height": height}
                    
                    # generate manifest for this image
                    manifest_url = create_manifest({
                        "iiif": iiif_url,
                        "width": width,
                        "height": height,
                        "identifier": os.path.basename(img)
                    })
                    saved_map[os.path.basename(img)]["manifest"] = manifest_url

                    results.append({"image": rel_img, "result": saved, "destination": "cantaloupe_fs"})
                except Exception as e:
                    results.append({"image": rel_img, "error": str(e)})

        # generate token
        token = uuid.uuid4().hex

        # modify page.csv by adding iiif_url column
        page_csv_path = os.path.join(base_dir, "page.csv")
        if os.path.exists(page_csv_path):
            # read rows, preserve header and order
            with open(page_csv_path, newline="", encoding="utf-8") as fh:
                reader = list(csv.reader(fh))
            if reader:
                header = reader[0]
                # append new column if not present
                if "iiif_url" not in header:
                    header.append("iiif_url")
                new_rows = [header]
                for row in reader[1:]:
                    # try to find page filename column (search for a filename pattern)
                    filename = None
                    for val in row:
                        if isinstance(val, str) and val.strip().lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
                            filename = os.path.basename(val.strip())
                            break
                    iiif_url = saved_map.get(filename, {}).get("iiif", "")
                    manifest_url = saved_map.get(filename, {}).get("manifest", "")
                    new_row = list(row) + [iiif_url, manifest_url]  # append both IIIF info.json and manifest URL
                    new_rows.append(new_row)
            # write back
            with open(page_csv_path, "w", newline="", encoding="utf-8") as fh:
                writer = csv.writer(fh)
                writer.writerows(new_rows)

        # create output zip
        out_zip = TEMP_OUTPUT_DIR.joinpath(f"processed_{token}.zip")

        # remove the original uploaded zip if it's present in the extracted tree
        try:
            if os.path.exists(tmp_zip):
                os.remove(tmp_zip)
        except Exception:
            pass

        # create zip, skip any .zip files to avoid embedding the upload again
        with zipfile.ZipFile(out_zip, "w", zipfile.ZIP_DEFLATED) as z:
            for root, _, files in os.walk(base_dir):
                for f in files:
                    if f.lower().endswith(".zip"):
                        continue
                    full = os.path.join(root, f)
                    arcname = os.path.relpath(full, base_dir)
                    z.write(full, arcname=arcname)

        # cleanup tmp_dir and optionally old outputs
        def _cleanup(path):
            try:
                shutil.rmtree(path)
            except Exception:
                pass
        if background:
            background.add_task(_cleanup, tmp_dir)
        else:
            _cleanup(tmp_dir)

        # build full download URL using request.base_url when available
        base = str(request.base_url).rstrip("/") if request else ""
        download_url = f"{base}/download/{token}" if base else f"/download/{token}"

        return JSONResponse({
            "uploaded": len([r for r in results if "error" not in r]),
            "details": results,
            "download_id": token,
            "download_url": download_url
        })
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{token}")
# @app.get("/download/{token}", dependencies=[Depends(require_upload_token)])
def download_result(token: str):
    zpath = TEMP_OUTPUT_DIR.joinpath(f"processed_{token}.zip")
    if not zpath.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(zpath), media_type="application/zip", filename=zpath.name)

@app.get("/manifests.json")
def list_manifests():
    """
    Return all manifests in services/iiif_manifests/ as a list of URLs
    """
    urls = []
    for f in MANIFESTS_DIR.glob("*.json"):
        urls.append(f"{MANIFESTS_BASE_URL.rstrip('/')}/{f.name}")
    # Ensure CORS header is present in the response
    return JSONResponse(urls, headers={"Access-Control-Allow-Origin": "*"})


app.mount("/manifests", StaticFiles(directory=str(MANIFESTS_DIR)), name="manifests")
