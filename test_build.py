"""Run with `python test_build.py`. Builds from the fixture into a temp dir and checks the output."""
import json, re, tempfile
from pathlib import Path

import build

ROOT = Path(__file__).parent
build.load_config(ROOT / "fixtures" / "site.toml")
items = json.loads((ROOT / "fixtures" / "items.json").read_text())
out = Path(tempfile.mkdtemp())
build.build(items, out)
site = build.C["site_url"]


def read(path):
    return (out / path.strip("/") / "index.html").read_text()


def jsonld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


item_dirs = sorted(p.name for p in (out / "item").iterdir())
assert len(item_dirs) == len(items)
assert all(re.fullmatch(r"\d+-[a-z0-9]+(-[a-z0-9]+)*", d) for d in item_dirs), item_dirs

for d in item_dirs:
    lid = d.split("-")[0]
    html = read(f"/item/{d}/")
    canonical = f"https://{build.C['domain']}/itm/{lid}"
    assert f'<link rel="canonical" href="{canonical}">' in html
    types = {j["@type"] for j in jsonld(html)}
    assert types == {"Product", "BreadcrumbList"}, types
    assert jsonld(html)[0]["offers"]["url"] == canonical

sitemap = (out / "sitemap.xml").read_text()
urls = re.findall(r"<loc>(.*?)</loc>", sitemap)
assert urls and not any("/item/" in u for u in urls)
for u in urls:
    assert (out / u.removeprefix(site).strip("/") / "index.html").exists(), u

assert '<meta name="robots" content="noindex,follow">' in read("/brand/lego/")  # 1 item
assert '<meta name="robots" content="noindex,follow">' not in read("/brand/casio/")  # 3 items
assert f"{site}/brand/lego/" not in urls and f"{site}/brand/casio/" in urls
assert 'name="robots" content="noindex' in read("/search/")
assert "<p>Example-seller-a" not in read("/seller/example-seller-a/") and "Every listing links straight to the eBay page" in read("/seller/example-seller-a/")

# untrusted description: no script, style, attributes or javascript: links survive
bad = read("/item/" + next(d for d in item_dirs if d.startswith("110000000005")))
assert "<script>alert" not in bad and "alert('xss')" not in bad and "display:none" not in bad
assert "onclick" not in bad and "javascript:" not in bad and "Dishwasher safe" in bad

# crafted text can't close the ld+json script tag
crafted = build.ld({"name": "</script><script>alert(1)</script><!--"})
assert "</" not in crafted and "<!--" not in crafted and json.loads(crafted)["name"].startswith("</script>")

for f in ("feed.xml", "robots.txt", "404.html", "search.json"):
    assert (out / f).exists(), f
assert not (out / "CNAME").exists()
assert re.search(r'<ul class="grid h-feed">.*?<img [^>]*srcset="[^"]+ 300w, [^"]+ 500w"', read("/"), re.S)
assert "<style>:root" in read("/") and 'rel="stylesheet"' not in read("/")

def product(lid):
    return jsonld(read("/item/" + next(d for d in item_dirs if d.startswith(lid))))[0]


hub_ld = jsonld(read("/seller/example-seller-a/"))
assert "BreadcrumbList" in {j["@type"] for j in hub_ld}
assert "BreadcrumbList" not in {j["@type"] for j in jsonld(read("/"))}
assert "h-feed" in read("/seller/example-seller-a/")
a = read("/item/" + next(d for d in item_dirs if d.startswith("110000000001")))
assert '<meta property="og:type" content="product">' in a and 'property="product:price:amount" content="24.99"' in a
assert 'content="used"' in a and 'og:type" content="website"' in read("/")
assert 'class="h-product' in a
o1, o2 = product("110000000001")["offers"], product("110000000002")["offers"]
assert o1["shippingDetails"]["shippingRate"]["value"] == "3.00" and o1["shippingDetails"]["shippingDestination"]["addressCountry"] == "GB"
assert o1["hasMerchantReturnPolicy"]["returnPolicyCategory"].endswith("MerchantReturnFiniteReturnWindow")
assert o1["hasMerchantReturnPolicy"]["returnFees"].endswith("ReturnShippingFees") and o1["hasMerchantReturnPolicy"]["merchantReturnDays"] == 30
assert product("110000000001")["gtin"] == "5012345678900"
assert o2["hasMerchantReturnPolicy"]["returnPolicyCategory"].endswith("MerchantReturnNotPermitted") and "shippingDetails" not in o2
hdr = (out / "_headers").read_text()
assert "Content-Security-Policy:" in hdr and "'sha256-" in hdr and "unsafe-inline" not in hdr, hdr
assert 'style="' not in read("/search/"), "inline style attributes break the CSP"
try:
    build.legacy_id("../../etc")
    raise AssertionError("legacy_id accepted a path")
except ValueError:
    pass

# hub titles and descriptions
assert re.search(r"<title>Casio for sale \| " + re.escape(build.C["site_name"]) + "</title>", read("/brand/casio/"))
assert f"<title>example-seller-a on eBay | {build.C['site_name']}</title>" in read("/seller/example-seller-a/")
for path in ("/", "/category/wristwatches/", "/brand/casio/", "/seller/example-seller-b/"):
    d = re.search(r'<meta name="description" content="(.*?)">', read(path)).group(1)
    assert len(d) <= 160 and "Prices from" in d, d
hub_items = [{"title": "A very long listing title that keeps going and going for ages", "price": p, "price_text": "£" + p, "seller": "s"} for p in ("9.50", "10.00")]
d1, d2 = build.hub_description(hub_items, "Thing"), build.hub_description(hub_items, "Thing", 2)
assert d1 != d2 and d2.endswith(" Page 2.") and len(d2) <= 160 and "Prices from £9.50" in d1, (d1, d2)
assert "…" in d1 and build.hub_description([{**hub_items[0], "price": ""}], "") .count("Prices from") == 0

# Cloudflare preview hosts are noindex
assert "https://:project.pages.dev/*\n  X-Robots-Tag: noindex" in hdr
assert "https://:version.:project.pages.dev/*\n  X-Robots-Tag: noindex" in hdr

# brand merge: "p louise" x2 and "PLouise" x1 are one indexable hub
assert read("/brand/p-louise/").count('class="h-product"') == 3
assert f"{site}/brand/p-louise/" in urls and not (out / "brand" / "plouise").exists()
assert "noindex" not in read("/brand/p-louise/").split("</head>")[0].split('name="robots"')[-1][:40]
assert build.short_category("Home, Furniture & DIY|Cookware") == "Home & DIY"
build.C["tagline"], saved = "{categories} from {shops} shops", build.CACHE.parent / "tagline.json"
saved.unlink(missing_ok=True)
fake = [{"raw_category": c} for c in ["Music|CDs"] * 3 + ["Health & Beauty|Make-up"] * 2 + ["Toys & Games|X", "Books, Comics & Magazines|Y"]]
assert build.weekly_tagline(fake) == "Music, Health & Beauty and Toys & Games from two shops", build.weekly_tagline(fake)
assert build.weekly_tagline([{"raw_category": "Other"}]) == "Music, Health & Beauty and Toys & Games from two shops", "should reuse this week's text"
saved.unlink()
print("ok")
