"""
Reusable Pulumi ComponentResource for S3 + CloudFront static sites on AWS.

Creates: ACM cert (us-east-1), S3 bucket (private + OAC), CloudFront distribution,
CloudFront Function (index.html rewrite), bucket policy, Route53 A + AAAA aliases.
"""

import json
import pulumi
import pulumi_aws as aws


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
        opts: pulumi.ResourceOptions | None = None,
    ):
        super().__init__("lukerohde:static-site:StaticSite", name, None, opts)

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
        bucket = aws.s3.BucketV2(
            f"{name}-bucket",
            bucket=bucket_name,
            opts=child_opts,
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
        custom_error_responses = []
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

        distribution = aws.cloudfront.Distribution(
            f"{name}-cdn",
            enabled=True,
            is_ipv6_enabled=True,
            default_root_object="index.html",
            aliases=[domain],
            price_class="PriceClass_100",
            origins=[
                aws.cloudfront.DistributionOriginArgs(
                    domain_name=bucket.bucket_regional_domain_name,
                    origin_id=f"s3-{bucket_name}",
                    origin_access_control_id=oac.id,
                )
            ],
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
