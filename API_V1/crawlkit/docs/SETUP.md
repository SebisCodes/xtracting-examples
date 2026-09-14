# crawlkit - setting it up

There is nothing to start. The library is built into the crawler's and the
dashboard's images by their compose files, which set the build context to
`API_V1/` so that `crawlkit/` is copied next to each service's own code.

## The one rule that spans three files

`requirements.txt` here pins `playwright`, and the Playwright base-image tags
in `crawler/Dockerfile` and `dashboard/standard/Dockerfile` must name the
same version. A pip package newer than the browsers in the image fails at
the first rendered fetch with "Executable doesn't exist", a message that
never mentions versions. Raise one, raise the other two.

## Working on it directly

```sh
cd API_V1
pip install -r crawlkit/requirements.txt
python -m pytest crawlkit -q                 # doctests and tests, no database, no network
python -m playwright install chromium        # only for the rendered engine
```

`API_V1/` is the import root: `import crawlkit` resolves when that folder is
on `sys.path`, which is what each service's `pytest.ini` and the
`PYTHONPATH=/srv` in each Dockerfile arrange.

## Rebuilding the services after a change

A change here is a change to both services:

```sh
(cd crawler            && docker compose build && docker compose up -d)
(cd dashboard/standard && docker compose build && docker compose up -d)
```
