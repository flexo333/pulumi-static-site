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
