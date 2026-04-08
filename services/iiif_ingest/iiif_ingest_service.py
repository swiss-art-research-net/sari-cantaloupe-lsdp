#!/usr/bin/env python3
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Request, Depends, Form
from fastapi.responses import JSONResponse, FileResponse
from fastapi.security import HTTPBearer, HTTPAuthorizationCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.middleware.cors import CORSMiddleware
import tempfile
import zipfile
import shutil
import os
import re
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

CANTALOUPE_IMAGE_DIR = Path(os.environ.get("CANTALOUPE_IMAGE_DIR", "/images")).resolve()
CANTALOUPE_BASE_URL = os.environ.get("CANTALOUPE_BASE_URL")

CANTALOUPE_IMAGE_DIR.mkdir(parents=True, exist_ok=True)

TEMP_OUTPUT_DIR = Path(tempfile.gettempdir()) / "lsdp_upload_outputs"
TEMP_OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

SLASH_SUBSTITUTE = os.environ.get("CANTALOUPE_SLASH_SUBSTITUTE", "!")


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


def save_to_cantaloupe(image_path: str, document_id: str):
    fname = os.path.basename(image_path)
    safe_doc_id = re.sub(r"[^A-Za-z0-9_\-]+", "_", document_id.strip() or "doc")
    doc_dir = CANTALOUPE_IMAGE_DIR.joinpath(safe_doc_id)
    doc_dir.mkdir(parents=True, exist_ok=True)

    dest_path = doc_dir / fname
    shutil.copy2(image_path, dest_path)

    # Use a single identifier path component for Cantaloupe
    identifier_path = f"{safe_doc_id}/{fname}"
    identifier = identifier_path.replace("/", SLASH_SUBSTITUTE)
    iiif_base = f"{CANTALOUPE_BASE_URL.rstrip('/')}/{identifier}"

    return {
        "local_path": str(dest_path),
        "iiif_info_json": f"{iiif_base}/info.json",
        "iiif_base": iiif_base,
        "default_image": f"{iiif_base}/full/max/0/default.jpg",
        "identifier": fname
    }


@app.post("/upload-zip")
async def upload_zip(file: UploadFile = File(...), document_id: str = Form(...), background: BackgroundTasks = None, request: Request = None):
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
        saved_map = {}
        image_infos = []
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

                    
                    saved = save_to_cantaloupe(img, document_id)
                    # open image with PIL to get width/height
                    with Image.open(img) as img_obj:
                        width, height = img_obj.size

                    saved_info = {
                        "identifier": saved["identifier"],
                        "iiif_base": saved["iiif_base"],
                        "width": width,
                        "height": height
                    }
                    image_infos.append(saved_info)

                    saved_map[os.path.basename(img)] = {
                        "iiif_info": saved["iiif_info_json"],
                        "iiif_base": saved["iiif_base"],
                        "default_image": saved["default_image"],
                        "width": width,
                        "height": height,
                    }

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
                if "manifest_url" not in header:
                    header.append("manifest_url")
                if "document_id" not in header:
                    header.append("document_id")
                new_rows = [header]
                for row in reader[1:]:
                    # try to find page filename column (search for a filename pattern)
                    filename = None
                    for val in row:
                        if isinstance(val, str) and val.strip().lower().endswith((".png", ".jpg", ".jpeg", ".tif", ".tiff")):
                            filename = os.path.basename(val.strip())
                            break
                    iiif_url = saved_map.get(filename, {}).get("iiif_info", "")
                    manifest_url = create_document_manifest(document_id, image_infos)
                    new_row = list(row) + [iiif_url, manifest_url, document_id]  # append both IIIF info.json and manifest URL
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
            "download_url": download_url,
            "document_id": document_id,
            "document_manifest_url": create_document_manifest(document_id, image_infos)
        })
    except Exception as e:
        shutil.rmtree(tmp_dir, ignore_errors=True)
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/download/{token}")
def download_result(token: str):
    zpath = TEMP_OUTPUT_DIR.joinpath(f"processed_{token}.zip")
    if not zpath.exists():
        raise HTTPException(status_code=404, detail="file not found")
    return FileResponse(str(zpath), media_type="application/zip", filename=zpath.name)

@app.get("/manifests.json")
def list_manifests():
    """
    Return all manifests in MANIFESTS_DIR (flat, no subfolders) as a list of URLs
    """
    urls = []
    for f in MANIFESTS_DIR.glob("*.json"):
        rel_path = f.relative_to(MANIFESTS_DIR)
        urls.append(f"{MANIFESTS_BASE_URL.rstrip('/')}/{rel_path}")
    return JSONResponse(urls, headers={"Access-Control-Allow-Origin": "*"})


manifests_app = StaticFiles(directory=str(MANIFESTS_DIR))
app.mount("/manifests", manifests_app, name="manifests")

def create_document_manifest(document_id: str, image_infos: list):
    safe = re.sub(r"[^A-Za-z0-9_\-]+", "_", document_id.strip() or "doc")
    
    manifest_path = MANIFESTS_DIR / f"{safe}.json" # e.g. /manifests/doc123.json
    manifest_id = f"{MANIFESTS_BASE_URL.rstrip('/')}/{safe}.json"  # e.g. http://localhost:8000/manifests/doc123.json

    items = []
    for i, info in enumerate(image_infos, start=1):
        canvas_id = f"{manifest_id}/canvas/{i}"
        annotation_page_id = f"{canvas_id}/annotationpage"
        annotation_id = f"{canvas_id}/annotation"
        items.append({
            "id": canvas_id,
            "type": "Canvas",
            "label": {"en": [info["identifier"]]},
            "width": info["width"],
            "height": info["height"],
            "items": [{
                "id": annotation_page_id,
                "type": "AnnotationPage",
                "items": [{
                    "id": annotation_id,
                    "type": "Annotation",
                    "motivation": "painting",
                    "body": {
                        "id": info["iiif_base"] + "/full/max/0/default.jpg",
                        "type": "Image",
                        "format": "image/jpeg",
                        "service": [{
                            "id": info["iiif_base"],
                            "type": "ImageService3",
                            "profile": "level2"
                        }],
                        "width": info["width"],
                        "height": info["height"],
                    },
                    "target": canvas_id
                }]
            }]
        })

    manifest = {
        "@context": "http://iiif.io/api/presentation/3/context.json",
        "id": manifest_id,
        "type": "Manifest",
        "label": {"en": [document_id]},
        "behavior": ["paged"],
        "items": items
    }
    with open(manifest_path, "w", encoding="utf-8") as fh:
        json.dump(manifest, fh, indent=2)
    return manifest_id
