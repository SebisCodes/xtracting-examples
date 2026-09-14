# crawlkit

A library, not a service. Nothing in here runs on its own and there is no
container: the dashboard and the crawler import it, and each of their images
carries a copy.

## Why it exists

The dashboard has a "Test this configuration" button that crawls a list page
and shows what the crawler *would* collect. The crawler later collects it. If
those two were separate code, they would drift - a trailing slash handled one
way here and another way there - and the drift would show up as pages the
preview promised and the crawler never fetched, or as documents submitted and
paid for twice because two spellings of one address counted as two pages.

So both services run literally the same functions: one `canonical_uri()`, one
notion of the *form* of an address, one `learn()`, one `decide()`, and (in the
I/O half) one robots.txt verdict and one link extraction.

## There is nothing to start

`docker compose up -d` in `../crawler` and `../dashboard/standard` builds this
folder into their images. To work on it directly you need Python 3.12 and the
packages of the I/O half:

```sh
cd API_V1
pip install -r crawlkit/requirements.txt
python -m pytest crawlkit -q
```

## Then

- [`docs/PURPOSE.md`](docs/PURPOSE.md) - what it is and what it is for
- [`docs/SETUP.md`](docs/SETUP.md) - how it gets into the images, and how to
  run it on its own
- [`docs/USAGE.md`](docs/USAGE.md) - the functions the services call and how
  to use the stand-in sites
- [`docs/DESIGN.md`](docs/DESIGN.md) - every module, how the rules fit
  together, the tests
