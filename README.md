<!-- Copyright (c) 2025, AgriTheory and contributors
For license information, please see license.txt-->

## Cloud Storage

Frappe app that stores ERPNext / Frappe file attachments in S3-compatible object storage (DigitalOcean Spaces, AWS S3, Backblaze B2, MinIO) instead of the server's disk.

### Features

- Every uploaded file, private or public, is stored in the bucket; no object is publicly readable. Links go through `/api/method/retrieve`, which checks permissions for private files and redirects to a short-lived signed URL.
- Working files that apps rewrite in place stay on local disk: Data Import, Bank Statement Import, Repost Item Valuation checkpoint, india_banking_connector H2H / CITI files (extendable via `local_only_attachments`).
- Code that opens files by path (importers, *Unzip*, xlsx readers, bank keys) gets a local cached copy, deleted after 24 h unused.
- Uploads are integrity-checked (Content-MD5) with retries and timeouts; a failed upload leaves no File record.
- Shareable links, DOCX and presentation (PDF) previews.
- Deleting a File removes the ERPNext record but keeps the object in the bucket (retention).

### Installation

```bash
cd ~/frappe-bench
bench get-app --branch version-16 git@github.com:digidyke-com/cloud_storage.git
bench --site <your-site> install-app cloud_storage
```

For previews, the server also needs LibreOffice and GNU `timeout` (coreutils):

```bash
sudo apt install libreoffice-core libreoffice-writer libreoffice-impress
```

### Configuration (DigitalOcean Spaces)

1. In DigitalOcean, create a **Space** (e.g. `my-erp-files` in `sgp1`) with *File Listing: Restricted*.
2. Create a **Spaces access key** limited to that Space (read/write/delete).
3. Add the settings to `sites/<your-site>/site_config.json`:

```json
{
  "require_cloud_storage": 1,
  "cloud_storage_settings": {
    "endpoint_url": "https://sgp1.digitaloceanspaces.com",
    "region": "sgp1",
    "bucket": "my-erp-files",
    "access_key": "DO00XXXXXXXXXXXXXXXX",
    "secret": "your-spaces-secret-key",
    "folder": "erp.example.com",
    "expiration": 120,
    "use_legacy_paths": 1
  }
}
```

Or set them from the command line:

```bash
bench --site <your-site> set-config -p cloud_storage_settings '{"endpoint_url": "https://sgp1.digitaloceanspaces.com", "region": "sgp1", "bucket": "my-erp-files", "access_key": "DO00XXXXXXXXXXXXXXXX", "secret": "your-spaces-secret-key", "folder": "erp.example.com", "expiration": 120, "use_legacy_paths": 1}'
bench --site <your-site> set-config -p require_cloud_storage 1
```

4. Apply and restart:

```bash
bench --site <your-site> migrate        # registers the hourly cache-cleanup job
bench --site <your-site> clear-cache
bench restart                            # production (supervisor); restart `bench start` in development
```

#### Settings

| Key | Required | Default | Description |
|---|---|---|---|
| `endpoint_url` | yes (non-AWS) | — | Provider endpoint, e.g. `https://<region>.digitaloceanspaces.com`. Omit for AWS S3. |
| `region` | yes | — | Region slug, e.g. `sgp1`, `blr1`, `nyc3`. |
| `bucket` | yes | — | Space / bucket name. |
| `access_key`, `secret` | yes | — | Spaces access key pair. |
| `folder` | no | none | Prefix for every object key; use one per site when sites share a bucket. |
| `expiration` | no | `120` | Lifetime (seconds) of signed links for private files. |
| `use_legacy_paths` | no | `1` | Key layout `folder/doctype/docname/hash/file`. `0` = `folder/hash/file`. |
| `use_local` | no | `0` | `1` stores files on local disk (disables cloud storage). |
| `local_only_attachments` | no | built-in list | Extra attachments kept on local disk: `[["DocType", "fieldname or null"]]`. |
| `max_attempts` | no | `5` | Retry attempts for S3 calls. |
| `connect_timeout` / `read_timeout` | no | `5` / `60` | S3 timeouts in seconds. |
| `max_pool_connections` | no | `10` | S3 connections per worker process. |
| `cache_ttl_hours` | no | `24` | Unused local cached copies are deleted after this. |
| `preview_timeout` | no | `120` | Max seconds for a LibreOffice preview conversion. |

`require_cloud_storage` (top level, not inside `cloud_storage_settings`): when `1`, uploads are refused if cloud storage is not configured, instead of silently falling back to local disk. Recommended for production.

### Verify

1. Attach a file to any document and open it from the sidebar: the link is `/api/method/retrieve?key=...` and the file opens.
2. The object appears in the Space under `<folder>/<DocType>/<docname>/...`.
3. **Error Log** has no `Cloud Storage upload failed` entries.

### Recommended

- To stop non-System Managers from making files public (anyone with a public link can open it): **System Settings → App tab → Only allow System Managers to upload public files**.
- Keep `secret` out of version control and limit the access key to the one Space.

More: [production](docs/production.md) · [development](docs/development.md) · [configuration](docs/configuration.md) · [DigitalOcean Spaces](docs/digitalocean-spaces.md)

#### License

MIT
