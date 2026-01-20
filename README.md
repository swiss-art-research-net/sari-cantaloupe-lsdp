# SARI Cantaloupe

A Docker configuration of the [Cantaloupe](https://cantaloupe-project.github.io/) IIIF Image Server

## How to use

Copy and edit .env.example file:
`cp .env.example .env`

(optional) Edit configuration stored in `config/cantaloupe.properties`

Start with `docker compose up -d`.

Place images in `images` directory.

## Upload Images Service

Once the service is running, a zipped export of https://github.com/liri-uzh/lcpimport_erara44085 can be uploaded. Expected zip contents:

```
.
├── book_name.csv
├── book.csv
├── config.json
├── fts_vector.csv
├── line.csv
├── media
│   ├── p10709677.png
│   ├── p10709678.png
│   ├── p12550757.png
│   └── p12550758.png
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

The response will provide a download url of the original zip with the modified `page.csv`, as well as the iiif `info.json` urls.


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
