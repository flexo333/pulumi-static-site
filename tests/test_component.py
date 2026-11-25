"""Unit tests with Pulumi mocks: no AWS calls."""
import pulumi
import pytest


class Mocks(pulumi.runtime.Mocks):
    def __init__(self):
        self.resources = []

    def new_resource(self, args: pulumi.runtime.MockResourceArgs):
        self.resources.append(args)
        outputs = dict(args.inputs)
        if args.typ == "aws:acm/certificate:Certificate":
            outputs["domainValidationOptions"] = [
                {"resourceRecordName": "_x.example.com", "resourceRecordType": "CNAME", "resourceRecordValue": "_y.acm"}
            ]
            outputs["arn"] = "arn:aws:acm:us-east-1:1:certificate/abc"
        if args.typ == "aws:acm/certificateValidation:CertificateValidation":
            outputs["certificateArn"] = "arn:aws:acm:us-east-1:1:certificate/abc"
        if args.typ == "aws:s3/bucket:Bucket":
            outputs.update(arn="arn:aws:s3:::b", bucketRegionalDomainName="b.s3.amazonaws.com")
        if args.typ == "aws:cloudfront/distribution:Distribution":
            outputs.update(arn="arn:aws:cloudfront::1:distribution/D", domainName="d.cloudfront.net", hostedZoneId="Z2FDTNDATAQYW2")
        if args.typ == "aws:cloudfront/function:Function":
            outputs["arn"] = "arn:aws:cloudfront::1:function/f"
        return [args.name + "_id", outputs]

    def call(self, args):
        return {}


@pytest.fixture
def mocks():
    m = Mocks()
    pulumi.runtime.set_mocks(m, preview=False)
    return m


def build(**kw):
    from pulumi_static_site import StaticSite

    return StaticSite("t", domain="example.com", zone_id="Z1", bucket_name="flexo333-t", **kw)


def distribution(m):
    return next(r for r in m.resources if r.typ == "aws:cloudfront/distribution:Distribution")


@pulumi.runtime.test
def test_defaults_unchanged(mocks):
    site = build()

    def check(_):
        d = distribution(mocks).inputs
        assert d["priceClass"] == "PriceClass_100"
        assert not d.get("customErrorResponses")
        bucket = next(r for r in mocks.resources if r.typ.startswith("aws:s3/bucket"))
        assert bucket.typ == "aws:s3/bucket:Bucket"
        assert bucket.inputs["bucket"] == "flexo333-t"

    return site.distribution_id.apply(check)


@pulumi.runtime.test
def test_price_class_and_error_page(mocks):
    site = build(price_class="PriceClass_200", error_page_path="/404.html")

    def check(_):
        d = distribution(mocks).inputs
        assert d["priceClass"] == "PriceClass_200"
        errs = sorted(d["customErrorResponses"], key=lambda e: e["errorCode"])
        assert [(e["errorCode"], e["responseCode"], e["responsePagePath"]) for e in errs] == [
            (403, 404, "/404.html"),
            (404, 404, "/404.html"),
        ]

    return site.distribution_id.apply(check)


def test_rejects_bad_options(mocks):
    with pytest.raises(ValueError):
        build(price_class="PriceClass_300")
    with pytest.raises(ValueError):
        build(spa_mode=True, error_page_path="/404.html")
    with pytest.raises(ValueError):
        build(error_page_path="404.html")
