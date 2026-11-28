"""Makes the sample report's data: one real run of the whole pipeline against the test
suite's fake website, with a stand-in AI assistant, saved to bvm/sample.json. The site
shows it at /sample so a visitor can read a full report before checking their own site.
Run from the project folder:  python tools/make_sample.py"""
import json
import pathlib
import sys
import tempfile
import threading
from dataclasses import asdict
from http.server import ThreadingHTTPServer

sys.path.insert(0, str(pathlib.Path.cwd()))

from bvm.collectors.ai_visibility import AiVisibility  # noqa: E402
from bvm.collectors.site_crawler import SiteCrawler  # noqa: E402
from bvm.config import ClientConfig  # noqa: E402
from bvm.pipeline import run  # noqa: E402
from bvm.store import Store  # noqa: E402
from tests.conftest import Handler  # noqa: E402
from tests.demo import ANSWERS, QUESTIONS  # noqa: E402
from tests.test_ai_and_web import FakeAssistant  # noqa: E402

SHOWN = "https://www.acme-demo.co.il"

server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
threading.Thread(target=server.serve_forever, daemon=True).start()
site = f"http://127.0.0.1:{server.server_port}"
cfg = ClientConfig(name="Acme", slug="acme", site=site, competitors=["Globex", "Initech"],
                   buyer_questions=QUESTIONS, delay_seconds=0)
with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as folder:
    store = Store(str(pathlib.Path(folder) / "sample.sqlite3"))
    result = run(cfg, store, collectors=[SiteCrawler(), AiVisibility([FakeAssistant("עוזר AI", ANSWERS)])])
    store.close()
server.shutdown()
assert not result.failures, result.failures
data = {"config": cfg.to_dict(), "facts": [asdict(f) for f in result.facts],
        "findings": [asdict(f) for f in result.findings]}
text = json.dumps(data, ensure_ascii=False, indent=1).replace(site, SHOWN)
pathlib.Path("bvm/sample.json").write_text(text + "\n", encoding="utf-8")
print(len(result.facts), "facts,", len(result.findings), "findings")
