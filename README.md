# SARI Cantaloupe IIIF Image Server & Ingest Pipeline

This repository implements a Dockerised IIIF image server and ingest pipeline for uploading a zipped LCP corpus, processing its images and manifests with Cantaloupe, and viewing them in a basic Mirador image viewer.

## How to use

Copy and edit .env.example file:
`cp .env.example .env`

(optional) Edit configuration stored in `config/cantaloupe.properties`

Start with `docker compose up -d`.

Place images in `images` directory. Or you can leave the folder empty and use the upload service described below to upload images to the running server.

## Upload Images Service

Once the service is running, a zipped LCP corpus can be uploaded. To test this service, you can run the code at https://github.com/liri-uzh/lcpimport_erara44085 and use the zip package it produces with our Upload Image Service. Otherwise, a sample zip file is provided in `sample_lcp_data`.
Expected zip contents:

```
.
├── book_name.csv
├── book.csv
├── config.json
├── fts_vector.csv
├── line.csv
├── media
│   ├── p10709677.png
│   ├── p10709678.png
│   ├── p12550757.png
│   └── p12550758.png
│   └── ...
├── page.csv
├── segment.csv
├── token_form.csv
└── token.csv
```

Use the following command to upload zip (replace `localhost:8000` with host name if applicable)
```
curl -F "file=@path/to/lcp_erara_output.zip" http://localhost:8000/upload-zip
```

The response will look like the following:

```json
{
  "uploaded": 4,
  "details": [
    {
      "image": "media/p10709677.png",
      "result": {
        "local_path": "/images/p10709677.png",
        "iiif_info_json": "http://localhost:8182/iiif/2/p10709677.png/info.json",
        "iiif_base": "http://localhost:8182/iiif/2/p10709677.png",
        "default_image": "http://localhost:8182/iiif/2/p10709677.png/full/full/0/default.jpg",
        "identifier": "p10709677.png",
        "manifest_url": "http://localhost:8000/manifests/p10709677.png.json"
      },
      "destination": "cantaloupe_fs"
    },
    {
      "image": "media/p10709678.png",
      "result": {
        "local_path": "/images/p10709678.png",
        "iiif_info_json": "http://localhost:8182/iiif/2/p10709678.png/info.json",
        "iiif_base": "http://localhost:8182/iiif/2/p10709678.png",
        "default_image": "http://localhost:8182/iiif/2/p10709678.png/full/full/0/default.jpg",
        "identifier": "p10709678.png",
        "manifest_url": "http://localhost:8000/manifests/p10709678.png.json"
      },
      "destination": "cantaloupe_fs"
    },
    {
      "image": "media/p12550757.png",
      "result": {
        "local_path": "/images/p12550757.png",
        "iiif_info_json": "http://localhost:8182/iiif/2/p12550757.png/info.json",
        "iiif_base": "http://localhost:8182/iiif/2/p12550757.png",
        "default_image": "http://localhost:8182/iiif/2/p12550757.png/full/full/0/default.jpg",
        "identifier": "p12550757.png",
        "manifest_url": "http://localhost:8000/manifests/p12550757.png.json"
      },
      "destination": "cantaloupe_fs"
    },
    {
      "image": "media/p12550758.png",
      "result": {
        "local_path": "/images/p12550758.png",
        "iiif_info_json": "http://localhost:8182/iiif/2/p12550758.png/info.json",
        "iiif_base": "http://localhost:8182/iiif/2/p12550758.png",
        "default_image": "http://localhost:8182/iiif/2/p12550758.png/full/full/0/default.jpg",
        "identifier": "p12550758.png",
        "manifest_url": "http://localhost:8000/manifests/p12550758.png.json"
      },
      "destination": "cantaloupe_fs"
    }
  ],
  "download_id": "ae306569544741988a71cac49d4b7cdb",
  "download_url": "http://localhost:8000/download/ae306569544741988a71cac49d4b7cdb"
}
```

The response will provide a download url (`download_url`) where a slightly modified version of the uploaded zip can be downloaded.
The modifications concern exclusively the file `page.csv` in the zip package, where the column `iiif_url` was added. This columns maps names of the original image files to their corresponding `info.json` file on the IIIF image server.
Here is an excerpt of the modified `page.csv` file:

```csv
page_id,char_range,xy_box,page,iiif_url,manifest_url
1,"[0,32)","(0,0),(1492,2299)",p12550757.png,http://localhost:8182/iiif/2/p12550757.png/info.json,http://localhost:8000/manifests/p12550757.png.json
2,"[32,38)","(1493,0),(2895,2303)",p12550758.png,http://localhost:8182/iiif/2/p12550758.png/info.json,http://localhost:8000/manifests/p12550758.png.json
```

## IIIF Viewer
Once the images are uploaded, they can be examined in a simple viewer available at http://localhost:8088/.


## Configure Proxy

If Cantaloupe is behind a reverse proxy, CORS settings need to be set in order for it to function correctly with IIIF image viewers. For [our Nginx](https://github.com/swiss-art-research-net/sari-nginx) configuration, create a _location_ overwrite by creating a file in the `vhost.d` directory with the name of the virtual host followed by `_location`. e.g. for https://iiif.swissartresearch.net the file should be called `iiif.swissartresearch.net_location`. Specify the CORS settings in this file, for example as follows:

```
add_header      "Access-Control-Allow-Methods" "GET, OPTIONS" always;
add_header      "Access-Control-Allow-Headers" "Accept,DNT,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Range" always;
add_header      "Access-Control-Max-Age" 1728000;
```

The `rs-iiif-mirador` component in Metaphacts/ResearchSpace tends to introduce additional slashes in the URL to an image. To redirect URLs with double slashes, insert the following in the _location_ overwrite:

```
if ($request_uri ~ "^[^?]*?//") {
   add_header   "Access-Control-Allow-Origin" "*" always;
   add_header      "Access-Control-Allow-Methods" "GET, OPTIONS" always;
   add_header      "Access-Control-Allow-Headers" "Accept,DNT,User-Agent,X-Requested-With,If-Modified-Since,Cache-Control,Content-Type,Range" always;
   add_header      "Access-Control-Max-Age" 1728000;
   rewrite "^" $scheme://$host$uri permanent;
}
```

This will rewrite the URL to single slashes and insert a CORS header so that the 301 redirect is followed.
