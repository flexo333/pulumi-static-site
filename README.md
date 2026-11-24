# pulumi-static-site

Reusable Pulumi component for deploying static sites on AWS (S3 + CloudFront + ACM + Route53).

## Usage

```python
from pulumi_static_site import StaticSite

site = StaticSite(
    "my-site",
    domain="app.lukeroh.de",
    zone_id=zone_id,            # Route53 hosted zone ID
    bucket_name="my-site-bucket",
    spa_mode=True,              # 403/404 → /index.html for client-side routing
)

# Outputs
pulumi.export("bucket", site.bucket_name)
pulumi.export("distribution_id", site.distribution_id)
pulumi.export("distribution_domain", site.distribution_domain)
```

## Optional API origin

Serve an API same-origin alongside the site by attaching a second, non-S3 origin to the
same distribution:

```python
site = StaticSite(
    "my-site",
    domain="app.lukeroh.de",
    zone_id=zone_id,
    bucket_name="my-site-bucket",
    spa_mode=True,
    api_origin_domain="abc123.lambda-url.us-east-1.on.aws",  # no scheme, no trailing slash
    api_path_pattern="/api/*",                               # default
)
```

- `api_origin_domain` (default `None`): origin hostname for the API. When `None`, nothing
  is added and the resource graph is identical to earlier versions.
- `api_path_pattern` (default `"/api/*"`): path pattern the API cache behavior matches.

The API behavior is `GET`/`HEAD`/`OPTIONS` only, uncached (managed `CachingDisabled`
policy), forwards all viewer headers except `Host` (managed `AllViewerExceptHostHeader`,
which a Lambda function URL origin requires), and has no index.html rewrite. Writes are
deliberately excluded: CloudFront caps origin response time at 30 s by default (60 s
without a quota increase), so long-running write calls should go directly to the API's
own origin.

Caveats:

- **The path prefix is not stripped.** CloudFront forwards `/api/...` to the origin
  as-is. Your origin must ignore the path (or handle the prefix itself).
- **`spa_mode=True` turns API 403/404 into the SPA page.** The 403/404 → `/index.html`
  (status 200) custom error responses apply to the whole distribution, including the API
  behavior; CloudFront cannot scope them to one behavior, and viewer-response functions do
  not run for origin statuses >= 400, so it cannot be undone at the edge. An API behind
  `api_path_pattern` must never return 403 or 404 — signal "not found" another way, e.g.
  200 with a `found: false` body.

## Install

Private git dependency (requires a GitHub PAT with read access):

```
pip install git+https://${GH_PAT}@github.com/lrohde/pulumi-static-site.git@v0.1.0
```

Or in `requirements.txt`:
```
pulumi-static-site @ git+https://github.com/lrohde/pulumi-static-site.git@v0.1.0
```

## What it creates

- ACM certificate (us-east-1, DNS-validated)
- S3 bucket (private, OAC-only access)
- CloudFront distribution (HTTPS, PriceClass_100)
- CloudFront Function for index.html rewrites
- S3 bucket policy scoped to the distribution
- Route53 A + AAAA alias records
