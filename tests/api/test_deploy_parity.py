"""Deploy-parity guard (KAN-13 post-merge fix).

`api/deploy/deploy.sh` ships ONLY `api/` and `scripts/` to the VM (tarred
from the repo root - see its own docstring). CI, by contrast, always runs
inside a full checkout, so an endpoint that reaches into `data/` at request
time (as a since-removed generics endpoint once did, via a module that
needed `data/produce_words.txt`/`data/pricez_images.json`) passes every
existing test and then 500s on the real box, where `data/` was never
deployed.

This test reproduces the deploy layout exactly: copy only `api/` and
`scripts/` into an empty temp directory, point `APP_DB` at a fixture app.db
living OUTSIDE that copy (matching the real split between
`/srv/grocery/app` and `/srv/grocery/data`), and run the actual server as a
subprocess with that directory as its cwd and nothing else on its import
path - a stray `data/` reach fails exactly the way it failed in production,
instead of silently succeeding against the full repo checkout under it.
"""
import os
import shutil
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

import pytest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import catalog_fixture as cf  # noqa: E402

REPO_ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "..")


def _free_port():
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _copy_deploy_tree(dest_dir):
    """Copy only api/ and scripts/ into dest_dir, exactly like
    `tar -C "$REPO_ROOT" -czf ... --exclude='__pycache__' --exclude='*.pyc'
    api scripts` in api/deploy/deploy.sh."""
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for name in ("api", "scripts"):
        shutil.copytree(os.path.join(REPO_ROOT, name),
                         os.path.join(dest_dir, name), ignore=ignore)


def _wait_for(url, timeout=15.0):
    deadline = time.monotonic() + timeout
    last_exc = None
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=1) as resp:
                return resp.status
        except (urllib.error.URLError, ConnectionError) as exc:
            last_exc = exc
            time.sleep(0.2)
    raise TimeoutError(f"server never answered {url}: {last_exc}")


@pytest.fixture(scope="module")
def deploy_copy_server(tmp_path_factory):
    """Build a fixture app.db, copy the deploy tree, and start uvicorn
    against it as a real subprocess - cwd is the deploy copy, exactly like
    `WorkingDirectory=/srv/grocery/app` in grocery-api.service."""
    tmp = tmp_path_factory.mktemp("deploy-parity")

    prices_db = str(tmp / "prices.db")
    cf.build_prices_db(prices_db)
    app_db = str(tmp / "app.db")
    cf.build_app_db_from_fixture(prices_db, app_db, tmp)

    deploy_dir = str(tmp / "deploy_copy")
    os.makedirs(deploy_dir, exist_ok=True)
    _copy_deploy_tree(deploy_dir)

    port = _free_port()
    uvicorn_bin = os.path.join(os.path.dirname(sys.executable), "uvicorn")
    env = dict(os.environ)
    env["APP_DB"] = app_db
    # No PYTHONPATH pointing back at the full repo checkout - a stray
    # `import scripts.generics` or a `data/...` open() must fail here the
    # same way it failed on the real box, not quietly succeed because the
    # test process happens to have the full repo on sys.path too.
    env.pop("PYTHONPATH", None)

    proc = subprocess.Popen(
        [uvicorn_bin, "api.main:app", "--host", "127.0.0.1", "--port", str(port)],
        cwd=deploy_dir, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    base_url = f"http://127.0.0.1:{port}"
    try:
        _wait_for(f"{base_url}/health")
    except TimeoutError:
        proc.terminate()
        out, _ = proc.communicate(timeout=5)
        pytest.fail(f"server never started; output:\n{out}")

    yield base_url, proc

    proc.terminate()
    try:
        proc.communicate(timeout=5)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.communicate()


def _get(base_url, path):
    safe_path = urllib.parse.quote(path, safe="/?=&")
    req = urllib.request.Request(f"{base_url}{safe_path}")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


def _post(base_url, path, body_bytes):
    req = urllib.request.Request(
        f"{base_url}{path}", data=body_bytes,
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=5) as resp:
            return resp.status
    except urllib.error.HTTPError as exc:
        return exc.code


@pytest.mark.parametrize("path,expected", [
    ("/health", {200}),
    ("/meta", {200}),
    (f"/product/{cf.DAIRY}", {200}),
    (f"/product/0000000000000", {404}),
    ("/search?q=test", {200}),
])
def test_endpoint_never_500s_from_the_deploy_copy(deploy_copy_server, path, expected):
    """Every endpoint must answer 200/404 from a process that only has
    api/ and scripts/ on disk - never crash reaching for something under
    data/ that deploy.sh never shipped."""
    base_url, proc = deploy_copy_server
    status = _get(base_url, path)
    if status not in expected:
        out = proc.stdout.read(4000) if proc.stdout else ""
        pytest.fail(f"{path} returned {status}, expected one of {expected}\n"
                    f"server output:\n{out}")


def test_post_products_never_500s_from_the_deploy_copy(deploy_copy_server):
    import json
    base_url, proc = deploy_copy_server
    body = json.dumps({"items": [{"barcode": cf.DAIRY}]}).encode()
    status = _post(base_url, "/products", body)
    if status != 200:
        out = proc.stdout.read(4000) if proc.stdout else ""
        pytest.fail(f"POST /products returned {status}, expected 200\n"
                    f"server output:\n{out}")
