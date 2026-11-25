"""
Reusable Pulumi ComponentResource for S3 + CloudFront static sites on AWS.

Creates: ACM cert (us-east-1), S3 bucket (private + OAC), CloudFront distribution,
CloudFront Function (index.html rewrite), bucket policy, Route53 A + AAAA aliases.

Requires pulumi-aws >= 7 (uses aws.s3.Bucket, which replaced BucketV2 in v7).
"""

import json
import pulumi
import pulumi_aws as aws

# Origin id for the optional API origin (see `api_origin_domain`).
API_ORIGIN_ID = "api"

# CloudFront price classes (which edge locations serve the site), per AWS's pricing table:
# PriceClass_100: North America, Europe, Israel.
# PriceClass_200: adds Asia (Japan, India, Singapore, Hong Kong, ...), the Middle East
#                 and Africa. It does NOT include Australia/NZ or South America.
# PriceClass_All: every edge, the only class that includes Australia and New Zealand.
PRICE_CLASSES = ("PriceClass_100", "PriceClass_200", "PriceClass_All")

# Type token the bucket had before v0.4.0. The alias lets existing stacks adopt the
# same bucket under aws.s3.Bucket instead of replacing it.
LEGACY_BUCKET_TYPE = "aws:s3/bucketV2:BucketV2"

# AWS managed cache policy "CachingDisabled" (TTLs all 0, nothing in the cache key).
# https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-cache-policies.html
CACHE_POLICY_CACHING_DISABLED = "4135ea2d-6df8-44a3-9df3-4b5a84be39ad"

# AWS managed origin request policy "AllViewerExceptHostHeader": forwards all viewer
# headers, cookies and query strings except `Host`.
# https://docs.aws.amazon.com/AmazonCloudFront/latest/DeveloperGuide/using-managed-origin-request-policies.html
ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST_HEADER = "b689b0a8-53d0-40ab-baf2-68738e2966ac"


class StaticSite(pulumi.ComponentResource):
    """A complete static-site stack: ACM cert, S3, CloudFront, Route53."""

    bucket_name: pulumi.Output[str]
    distribution_id: pulumi.Output[str]
    distribution_domain: pulumi.Output[str]

    def __init__(
        self,
        name: str,
        *,
        domain: str,
        zone_id: pulumi.Input[str],
        bucket_name: str,
        spa_mode: bool = False,
        api_origin_domain: pulumi.Input[str] | None = None,
        api_path_pattern: str = "/api/*",
        price_class: str = "PriceClass_100",
        error_page_path: str | None = None,
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("lukerohde:static-site:StaticSite", name, None, opts)

        if price_class not in PRICE_CLASSES:
            raise ValueError(f"price_class must be one of {PRICE_CLASSES}, got {price_class!r}")
        if spa_mode and error_page_path:
            raise ValueError("error_page_path cannot be combined with spa_mode (spa_mode already maps 403/404 to /index.html)")
        if error_page_path is not None and not error_page_path.startswith("/"):
            raise ValueError("error_page_path must be an absolute path such as '/404.html'")

        child_opts = pulumi.ResourceOptions(parent=self)

        # ── Provider — ACM certs for CloudFront must live in us-east-1 ────────────
        us_east_1 = aws.Provider(
            f"{name}-us-east-1",
            region="us-east-1",
            opts=child_opts,
        )

        # ── CloudFront Function — rewrite /path/ and /path to /path/index.html ───
        index_rewrite = aws.cloudfront.Function(
            f"{name}-index-rewrite",
            name=f"{bucket_name}-index-rewrite",
            runtime="cloudfront-js-2.0",
            code="""
async function handler(event) {
    var uri = event.request.uri;
    if (uri.endsWith('/')) {
        event.request.uri += 'index.html';
    } else if (!uri.split('/').pop().includes('.')) {
        event.request.uri += '/index.html';
    }
    return event.request;
}
""",
            publish=True,
            opts=child_opts,
        )

        # ── ACM Certificate (us-east-1) ──────────────────────────────────────────
        cert = aws.acm.Certificate(
            f"{name}-cert",
            domain_name=domain,
            validation_method="DNS",
            opts=pulumi.ResourceOptions(parent=self, provider=us_east_1),
        )

        validation_record = aws.route53.Record(
            f"{name}-cert-validation",
            zone_id=zone_id,
            name=cert.domain_validation_options[0].resource_record_name,
            type=cert.domain_validation_options[0].resource_record_type,
            records=[cert.domain_validation_options[0].resource_record_value],
            ttl=60,
            allow_overwrite=True,
            opts=child_opts,
        )

        cert_validation = aws.acm.CertificateValidation(
            f"{name}-cert-validated",
            certificate_arn=cert.arn,
            validation_record_fqdns=[validation_record.fqdn],
            opts=pulumi.ResourceOptions(parent=self, provider=us_east_1),
        )

        # ── S3 Bucket (private — served only via CloudFront OAC) ─────────────────
        bucket = aws.s3.Bucket(
            f"{name}-bucket",
            bucket=bucket_name,
            # Stacks created before v0.4.0 have this bucket as aws.s3.BucketV2; the alias
            # makes Pulumi adopt it in place rather than delete + recreate it.
            opts=pulumi.ResourceOptions(
                parent=self,
                aliases=[pulumi.Alias(type_=LEGACY_BUCKET_TYPE)],
            ),
        )

        aws.s3.BucketPublicAccessBlock(
            f"{name}-public-access-block",
            bucket=bucket.id,
            block_public_acls=True,
            block_public_policy=True,
            ignore_public_acls=True,
            restrict_public_buckets=True,
            opts=child_opts,
        )

        # ── CloudFront Origin Access Control ─────────────────────────────────────
        oac = aws.cloudfront.OriginAccessControl(
            f"{name}-oac",
            name=bucket_name,
            origin_access_control_origin_type="s3",
            signing_behavior="always",
            signing_protocol="sigv4",
            opts=child_opts,
        )

        # ── CloudFront Distribution ──────────────────────────────────────────────
        # WARNING: with spa_mode=True, 403 and 404 are mapped to /index.html with a 200
        # for the WHOLE distribution, including the API behavior below. CloudFront custom
        # error responses cannot be scoped to one cache behavior, and CloudFront Functions
        # on viewer-response do not fire for origin statuses >= 400, so it cannot be undone
        # at the edge. An API served via `api_path_pattern` MUST NOT return 403 or 404;
        # express "not found" another way (e.g. 200 with a `found: false` body).
        custom_error_responses = []
        if error_page_path:
            # Static (non-SPA) sites: a private S3 origin answers a missing key with 403,
            # so map both 403 and 404 to the site's own error page, served as a real 404.
            for error_code in [403, 404]:
                custom_error_responses.append(
                    aws.cloudfront.DistributionCustomErrorResponseArgs(
                        error_code=error_code,
                        response_code=404,
                        response_page_path=error_page_path,
                        error_caching_min_ttl=60,
                    )
                )
        if spa_mode:
            for error_code in [403, 404]:
                custom_error_responses.append(
                    aws.cloudfront.DistributionCustomErrorResponseArgs(
                        error_code=error_code,
                        response_code=200,
                        response_page_path="/index.html",
                        error_caching_min_ttl=0,
                    )
                )

        origins = [
            aws.cloudfront.DistributionOriginArgs(
                domain_name=bucket.bucket_regional_domain_name,
                origin_id=f"s3-{bucket_name}",
                origin_access_control_id=oac.id,
            )
        ]
        # Left as an empty list (-> None below) unless an API origin is requested, so
        # callers that don't opt in send exactly the same Distribution args as v0.2.0.
        ordered_cache_behaviors = []
        if api_origin_domain is not None:
            origins.append(
                aws.cloudfront.DistributionOriginArgs(
                    domain_name=api_origin_domain,
                    origin_id=API_ORIGIN_ID,
                    custom_origin_config=aws.cloudfront.DistributionOriginCustomOriginConfigArgs(
                        http_port=80,
                        https_port=443,
                        origin_protocol_policy="https-only",
                        origin_ssl_protocols=["TLSv1.2"],
                        origin_keepalive_timeout=60,
                    ),
                )
            )
            ordered_cache_behaviors.append(
                aws.cloudfront.DistributionOrderedCacheBehaviorArgs(
                    path_pattern=api_path_pattern,
                    target_origin_id=API_ORIGIN_ID,
                    viewer_protocol_policy="https-only",
                    # Read-only methods on purpose: CloudFront caps the origin response
                    # timeout at 30 s by default (60 s without a quota increase), and an
                    # API write path may legitimately run for minutes. Write traffic must
                    # go direct to its own origin, not through this distribution.
                    allowed_methods=["GET", "HEAD", "OPTIONS"],
                    cached_methods=["GET", "HEAD"],
                    compress=True,
                    cache_policy_id=CACHE_POLICY_CACHING_DISABLED,
                    # AllViewerExceptHostHeader, NOT AllViewer: a Lambda function URL
                    # origin rejects a forwarded viewer `Host` header.
                    origin_request_policy_id=ORIGIN_REQUEST_POLICY_ALL_VIEWER_EXCEPT_HOST_HEADER,
                    # No function_associations here: the index_rewrite function is
                    # attached to default_cache_behavior only, so API paths are never
                    # rewritten.
                    #
                    # NOTE: the path prefix is NOT stripped. The origin receives
                    # `/api/...` exactly as the viewer sent it; the origin is expected
                    # to ignore the path.
                )
            )

        distribution = aws.cloudfront.Distribution(
            f"{name}-cdn",
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object="index.html",
            aliases=[domain],
            price_class=price_class,
            origins=origins,
            default_cache_behavior=aws.cloudfront.DistributionDefaultCacheBehaviorArgs(
                target_origin_id=f"s3-{bucket_name}",
                viewer_protocol_policy="redirect-to-https",
                allowed_methods=["GET", "HEAD"],
                cached_methods=["GET", "HEAD"],
                compress=True,
                forwarded_values=aws.cloudfront.DistributionDefaultCacheBehaviorForwardedValuesArgs(
                    query_string=False,
                    cookies=aws.cloudfront.DistributionDefaultCacheBehaviorForwardedValuesCookiesArgs(
                        forward="none",
                    ),
                ),
                min_ttl=0,
                default_ttl=3600,
                max_ttl=86400,
                function_associations=[
                    aws.cloudfront.DistributionDefaultCacheBehaviorFunctionAssociationArgs(
                        event_type="viewer-request",
                        function_arn=index_rewrite.arn,
                    )
                ],
            ),
            ordered_cache_behaviors=ordered_cache_behaviors or None,
            custom_error_responses=custom_error_responses,
            restrictions=aws.cloudfront.DistributionRestrictionsArgs(
                geo_restriction=aws.cloudfront.DistributionRestrictionsGeoRestrictionArgs(
                    restriction_type="none",
                ),
            ),
            viewer_certificate=aws.cloudfront.DistributionViewerCertificateArgs(
                acm_certificate_arn=cert_validation.certificate_arn,
                ssl_support_method="sni-only",
                minimum_protocol_version="TLSv1.2_2021",
            ),
            opts=child_opts,
        )

        # ── S3 bucket policy — allow CloudFront to read objects ──────────────────
        aws.s3.BucketPolicy(
            f"{name}-bucket-policy",
            bucket=bucket.id,
            policy=pulumi.Output.all(bucket.arn, distribution.arn).apply(
                lambda args: json.dumps({
                    "Version": "2012-10-17",
                    "Statement": [{
                        "Sid": "AllowCloudFrontOAC",
                        "Effect": "Allow",
                        "Principal": {"Service": "cloudfront.amazonaws.com"},
                        "Action": "s3:GetObject",
                        "Resource": f"{args[0]}/*",
                        "Condition": {
                            "StringEquals": {"AWS:SourceArn": args[1]},
                        },
                    }],
                })
            ),
            opts=child_opts,
        )

        # ── Route 53 A + AAAA alias records → CloudFront ────────────────────────
        for record_type in ["A", "AAAA"]:
            aws.route53.Record(
                f"{name}-{record_type.lower()}",
                zone_id=zone_id,
                name=domain,
                type=record_type,
                aliases=[
                    aws.route53.RecordAliasArgs(
                        name=distribution.domain_name,
                        zone_id=distribution.hosted_zone_id,
                        evaluate_target_health=False,
                    )
                ],
                opts=child_opts,
            )

        # ── Outputs ──────────────────────────────────────────────────────────────
        self.bucket_name = bucket.bucket
        self.distribution_id = distribution.id
        self.distribution_domain = distribution.domain_name

        self.register_outputs({
            "bucket_name": self.bucket_name,
            "distribution_id": self.distribution_id,
            "distribution_domain": self.distribution_domain,
        })
