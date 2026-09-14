# Vendored browser libraries

Copied into the repository on purpose: the dashboard runs on a customer's
network, which may not reach a CDN, and a page that silently loses its charts
because a third party changed a URL is the kind of failure nobody notices for a
month. Nothing here is modified. To upgrade, download the new file with curl,
replace the row below and re-run the checksum command at the end.

| Library | Version | File | Source | Licence |
|---|---|---|---|---|
| Chart.js | 4.5.0 | `chart.js/chart.umd.js` | https://cdnjs.cloudflare.com/ajax/libs/Chart.js/4.5.0/chart.umd.js | MIT |
| Leaflet | 1.9.4 | `leaflet/leaflet.js`, `leaflet/leaflet.css`, `leaflet/images/*` | https://cdnjs.cloudflare.com/ajax/libs/leaflet/1.9.4/ | BSD-2-Clause |
| Leaflet.markercluster | 1.5.3 | `leaflet.markercluster/leaflet.markercluster.js`, `MarkerCluster.css`, `MarkerCluster.Default.css` | https://cdnjs.cloudflare.com/ajax/libs/leaflet.markercluster/1.5.3/ | MIT |
| Leaflet.heat | 0.2.0 | `leaflet.heat/leaflet-heat.js` | https://cdn.jsdelivr.net/npm/leaflet.heat@0.2.0/dist/leaflet-heat.js | BSD-2-Clause |
| html2canvas | 1.4.1 | `html2canvas/html2canvas.min.js` | https://cdnjs.cloudflare.com/ajax/libs/html2canvas/1.4.1/html2canvas.min.js | MIT |
| jsPDF | 2.5.1 | `jspdf/jspdf.umd.min.js` | https://cdnjs.cloudflare.com/ajax/libs/jspdf/2.5.1/jspdf.umd.min.js | MIT |

Leaflet's CSS refers to `images/` relative to itself, which is why the folder
structure is kept rather than flattened.

## Checksums (SHA-256)

Produced with `find . -type f ! -name VENDOR.md | sort | xargs sha256sum`
from this folder; the same command verifies them.

```
b7929ad4d5323b8244f85d79bba0b3d1495e66f79a2f0023be7f4de5ba4fbac8  ./chart.js/chart.umd.js
e87e550794322e574a1fda0c1549a3c70dae5a93d9113417a429016838eab8cb  ./html2canvas/html2canvas.min.js
98ccf17aa10c20bb1301762618fcc9b6ab3a4e7f26b6071d64d0b41154df3875  ./jspdf/jspdf.umd.min.js
eb952aae5806a1102729f291bab887dde783ace859819a354827a776e73e486a  ./leaflet.heat/leaflet-heat.js
066daca850d8ffbef007af00b06eac0015728dee279c51f3cb6c716df7c42edf  ./leaflet/images/layers-2x.png
1dbbe9d028e292f36fcba8f8b3a28d5e8932754fc2215b9ac69e4cdecf5107c6  ./leaflet/images/layers.png
00179c4c1ee830d3a108412ae0d294f55776cfeb085c60129a39aa6fc4ae2528  ./leaflet/images/marker-icon-2x.png
574c3a5cca85f4114085b6841596d62f00d7c892c7b03f28cbfa301deb1dc437  ./leaflet/images/marker-icon.png
264f5c640339f042dd729062cfc04c17f8ea0f29882b538e3848ed8f10edb4da  ./leaflet/images/marker-shadow.png
a7837102824184820dfa198d1ebcd109ff6d0ff9a2672a074b9a1b4d147d04c6  ./leaflet/leaflet.css
db49d009c841f5ca34a888c96511ae936fd9f5533e90d8b2c4d57596f4e5641a  ./leaflet/leaflet.js
1e4e1d22972a3926f48598e0caf14e3fe7049835d428a344fed4f9e3665b3508  ./leaflet.markercluster/leaflet.markercluster.js
614dea0a98ff3f4ead74f04918f6b1d1b9ba435c25b5fc23b21a394d1e3e4d87  ./leaflet.markercluster/MarkerCluster.css
61258232d98d64dc2a7b1e02130d67421bc5b9bda5994eef70228ff97570c170  ./leaflet.markercluster/MarkerCluster.Default.css
```
