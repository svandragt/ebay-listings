# tinylistings

Small, fast listing pages for people selling on eBay. It turns the listings of your eBay seller accounts into crawlable landing pages, while buying, payment and delivery stay on eBay. A Python script fetches the listings from the eBay Browse API and renders plain HTML. GitHub Actions rebuilds the site every six hours and deploys it to Cloudflare Pages.

[listings.vandragt.com](https://listings.vandragt.com) is a tinylistings instance.

Item pages link back to eBay and use the eBay listing as their canonical URL. Category, brand, seller and "new this week" pages are the pages that compete in search. A hub page with fewer than three items is marked `noindex` and left out of the sitemap.

## Build locally

You need Python 3.11 or later and `jinja2`.

```sh
pip install -r requirements.txt
python build.py --config fixtures/site.toml --fixture fixtures/items.json
python -m http.server -d _site
python test_build.py
```

The `--fixture` option skips the eBay API and loads sample listings. The `fixtures/site.toml` file holds fake seller names for the tests.

To build from the live API, set the secrets below and run `python build.py`. The build fails if any seller returns no items or if any request fails, so an outage never replaces the live site with an empty one.

## Configuration

Edit `site.toml`:

| Key | Meaning |
|---|---|
| `sellers` | eBay seller user names |
| `site_url` | Public URL of the site. |
| `site_name` | Name shown in page titles |
| `marketplace` | `EBAY_GB`, `EBAY_US`, `EBAY_DE` or `EBAY_AU` |
| `tagline` | Optional. Short description of the sellers. The home page uses it as its title and H1 instead of the default "Listings from ..." heading. |
| `top_category_ids` | Optional. Category IDs to search for each seller. By default the build reads every top-level category from the eBay Taxonomy API. |
| `image_transform` | Optional. URL prefix for a first-party image resizer, with `{width}` filled in, for example `/cdn-cgi/image/width={width},quality=80,format=auto/`. Tracker blockers hide images from `i.ebayimg.com`, so this keeps them visible. It needs Cloudflare Images transformations on the zone, with `i.ebayimg.com` as an allowed origin. Leave it out to link to eBay's images directly. |

To add an intro above a listing page, create `content/{seller|category|brand}/{slug}.md`. Separate paragraphs with a blank line. Without a file, the page shows a line such as "23 listings from 2 sellers, updated every 6 hours".

## Refresh rate

eBay's licence requires listing data to be no more than six hours old, so the workflow runs every six hours (`0 */6 * * *`). Change the `cron` line in `.github/workflows/build.yml` if the licence changes.

Each run refreshes title, price and availability from the search results. It calls `getItem` only for listings missing from `.cache/items/`, and the workflow keeps that folder between runs. The Browse API allows 5,000 calls a day.

## Secrets and variables

Create a production keyset in the eBay developer programme. In the GitHub repository, go to **Settings > Secrets and variables > Actions** and add these secrets:

- `EBAY_CLIENT_ID`
- `EBAY_CLIENT_SECRET`
- `CLOUDFLARE_API_TOKEN`, an API token with the **Cloudflare Pages: Edit** permission
- `CLOUDFLARE_ACCOUNT_ID`

On the **Variables** tab, add `CF_PAGES_PROJECT` with your Pages project name.

## Cloudflare Pages

1. Create the project once: `npx wrangler pages project create <name> --production-branch=main`.
2. In the Cloudflare dashboard, open the project, go to **Custom domains** and add your domain, for example `listings.example.com`.
3. If the parent zone is on Cloudflare, it adds the DNS record for you. Otherwise, add `listings CNAME <project>.pages.dev.` at your DNS host.
4. If you use CAA records, allow Cloudflare's certificate authorities: `letsencrypt.org`, `pki.goog` and `ssl.com`.

The site includes a `404.html`. Cloudflare Pages serves it with a 404 status for unknown paths.

To see visits, enable Web Analytics on the Pages project. It sets no cookies, and the CSP already allows its script.

## Use this for your own eBay listings

1. Fork the repository.
2. Edit `site.toml` with your sellers, site URL, site name and marketplace.
3. Add the secrets and the `CF_PAGES_PROJECT` variable.
4. Create the Cloudflare Pages project and add your domain.

## Licence

This project is licensed under the GNU General Public License v3.0 or later. See [LICENSE](LICENSE).
