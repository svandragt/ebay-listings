"""Run with `python test_build.py`. Builds from the fixture into a temp dir and checks the output."""
import json, re, tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

import build

ROOT = Path(__file__).parent
build.load_config(ROOT / "fixtures" / "site.toml")
items = json.loads((ROOT / "fixtures" / "items.json").read_text())
out = Path(tempfile.mkdtemp())
sold = json.loads((ROOT / "fixtures" / "sold.json").read_text())
build.build(items, out, sold=sold)
site = build.C["site_url"]


def read(path):
    return (out / path.strip("/") / "index.html").read_text()


def jsonld(html):
    return [json.loads(m) for m in re.findall(r'<script type="application/ld\+json">(.*?)</script>', html, re.S)]


item_dirs = sorted(p.name for p in (out / "item").iterdir())
assert len(item_dirs) == len(items) + 1  # live items plus the sold page
sold_url = build.prepare(sold[0]["item"])["url"]
assert all(re.fullmatch(r"\d+-[a-z0-9]+(-[a-z0-9]+)*", d) for d in item_dirs), item_dirs

for d in item_dirs:
    lid = d.split("-")[0]
    if f"/item/{d}/" == sold_url:
        continue
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
# more like this
for d in item_dirs:
    if f"/item/{d}/" == sold_url:
        continue
    html = read(f"/item/{d}/")
    if "<h2>More like this</h2>" not in html:
        continue  # nothing relevant enough: the section is left out rather than padded
    assert html.count("<h2>More like this</h2>") == 1
    links = re.findall(r'<a class="u-url" href="(/item/[^"]+)">', html.split("<h2>More like this</h2>")[1])
    assert 0 < len(links) <= 6 and f"/item/{d}/" not in links, (d, links)
    assert 'fetchpriority="high"' not in html.split("<h2>More like this</h2>")[1]
    assert "<h3 class=\"p-name\">" in html.split("<h2>More like this</h2>")[1]
watch = read("/item/" + next(d for d in item_dirs if d.startswith("110000000004"))).split("<h2>More like this</h2>")[1]
assert "110000000006" in watch and "110000000007" in watch and "110000000001" not in watch  # other watches yes, the same seller's teapot no

# sold page
sp = read(sold_url)
assert 'name="robots" content="noindex,follow"' in sp and 'rel="canonical"' not in sp
assert {j["@type"] for j in jsonld(sp)} == {"BreadcrumbList"} and '"Product"' not in sp
assert "<h1>Casio Vintage Digital Watch, Sold Example</h1>" in sp and "has sold, or the listing has ended" in sp
assert '<a href="/seller/example-seller-a/">' in sp and "<h2>More like this</h2>" in sp and 'loading="lazy"' in sp
assert sold_url not in "".join(urls) and sold_url not in (out / "feed.xml").read_text() and sold_url not in (out / "search.json").read_text()
assert sold_url not in read("/") and sold_url not in read("/category/wristwatches/") and sold_url not in read("/seller/example-seller-a/")
assert read("/category/wristwatches/").count('class="h-product"') == 3  # the sold item is not counted
assert "Wristwatches (3)" in read("/")
live_same_id = [{**sold[0], "item": {**sold[0]["item"], "legacyItemId": "110000000004"}}]
build.build(items, Path(tempfile.mkdtemp()), sold=live_same_id)  # live wins, no sold page, no crash

# price reduced note
day = timedelta(days=1)
raw = lambda price: [{"legacyItemId": "1", "price": {"value": price}}]
h = build.track_prices({}, raw("15.00"), now := datetime(2026, 9, 1, tzinfo=timezone.utc))
assert h["1"] == {"price": "15.00", "since": "2026-09-01T00:00:00Z"}
h8 = build.track_prices(h, raw("12.00"), now + 8 * day)
assert h8["1"]["reduced"] == {"was": "15.00", "on": "2026-09-09T00:00:00Z"} and h8["1"]["since"] == "2026-09-09T00:00:00Z"
assert "reduced" not in build.track_prices(h, raw("12.00"), now + 3 * day)["1"]
assert "reduced" not in build.track_prices(h8, raw("14.00"), now + 10 * day)["1"]  # a rise clears it
assert "reduced" in build.track_prices(h8, raw("12.00"), now + 21 * day)["1"]
assert "reduced" not in build.track_prices(h8, raw("12.00"), now + 22 * day)["1"]  # expires after 14 days
assert build.track_prices(h8, raw("12.00") + [{"legacyItemId": "2", "price": {"value": "1"}}], now + 9 * day).keys() == {"1", "2"}
assert build.track_prices(h8, [], now + 9 * day) == {}  # delisted items are pruned
pout = Path(tempfile.mkdtemp())
build.build(items, pout, prices={"110000000001": {"price": "24.99", "since": "x", "reduced": {"was": "30.00", "on": "x"}}})
assert '<small class="reduced">Price lowered from £30.00</small>' in (pout / "item").glob("110000000001-*/index.html").__next__().read_text()
assert 'class="reduced"' not in read("/") and "lowered" not in pout.joinpath("index.html").read_text()  # cards stay plain
assert "lowered" not in "".join(re.findall(r'ld\+json">(.*?)</script>', (pout / "item").glob("110000000001-*/index.html").__next__().read_text(), re.S))

# Cloudflare Web Analytics
assert "script-src 'self' https://static.cloudflareinsights.com;" in hdr and "connect-src 'self' https://cloudflareinsights.com;" in hdr

# sold cache lifecycle
tmp = Path(tempfile.mkdtemp())
real_cache, build.CACHE = build.CACHE, tmp / "items"
try:
    build.CACHE.mkdir()
    now = datetime(2026, 9, 30, 12, 0, tzinfo=timezone.utc)
    (build.CACHE / "1.json").write_text('{"legacyItemId": "1"}')
    (build.CACHE / "2.json").write_text('{"legacyItemId": "2"}')
    got = build.retire_cache({"2"}, now)  # 1 ends
    assert [w["item"]["legacyItemId"] for w in got] == ["1"] and got[0]["ended"] == "2026-09-30T12:00:00Z"
    assert not (build.CACHE / "1.json").exists() and (build.CACHE / "2.json").exists()
    got = build.retire_cache({"2"}, now + timedelta(days=5))  # keeps the original ended time
    assert got[0]["ended"] == "2026-09-30T12:00:00Z"
    got = build.retire_cache({"1", "2"}, now + timedelta(days=6))  # relisted
    assert got == [] and not (tmp / "sold" / "1.json").exists()
    (build.CACHE / "1.json").write_text('{"legacyItemId": "1"}')
    build.retire_cache({"2"}, now)
    assert len(build.retire_cache({"2"}, now + timedelta(days=29))) == 1
    assert build.retire_cache({"2"}, now + timedelta(days=31)) == [] and not list((tmp / "sold").glob("*.json"))
finally:
    build.CACHE = real_cache
print("ok")
