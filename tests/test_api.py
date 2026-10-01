from fastapi.testclient import TestClient

from pdf_notion_mvp.api import create_app, create_demo_app


def test_api_job_lifecycle_and_safe_retry(tmp_path, source):
    client = TestClient(create_demo_app(tmp_path / "jobs.sqlite"))
    assert client.get("/health").json()["mode"] == "synthetic_demo"
    request = {"request_key": "one", "input": source.model_dump()}
    response = client.post("/jobs", json=request)
    assert response.status_code == 200
    job = response.json()
    assert client.post("/jobs", json=request).json()["job_id"] == job["job_id"]
    base = f"/jobs/{job['job_id']}"
    assert client.get(base + "/publish-plan").status_code == 409
    assert client.post(base + "/resume", json={"expected_revision": 0}).status_code == 409
    assert client.post(base + "/advance", json={}).status_code == 422
    job = client.post(base + "/advance", json={"expected_revision": 0}).json()
    assert job["status"] == "extracted"
    assert client.post(base + "/advance", json={"expected_revision": 0}).status_code == 409
    for _ in range(4):
        response = client.post(base + "/advance", json={"expected_revision": job["revision"]})
        assert response.status_code == 200
        job = response.json()
    assert job["status"] == "ready"
    assert len(client.get(base + "/publish-plan").json()["operations"]) == 7
    assert client.get(base).json() == job
    source.document.blocks[1].text = "changed input"
    assert client.post("/jobs", json={"request_key": "one", "input": source.model_dump()}).status_code == 409
    assert client.get("/jobs/nonexistent").status_code == 404


def test_api_rejects_real_pdf_stub(tmp_path):
    client = TestClient(create_app(tmp_path / "jobs.sqlite"))
    assert client.post("/jobs", json={"request_key": "a", "input": {"kind": "pdf", "path": "/private.pdf"}}).status_code == 422


def test_default_api_rejects_client_selected_synthetic_mode(tmp_path,source):
    client=TestClient(create_app(tmp_path / "jobs.sqlite"))
    assert client.get("/health").json()["mode"]=="local_ir_validation"
    assert client.post("/jobs",json={"request_key":"a","input":source.model_dump()}).status_code==403
    assert client.post("/jobs",json={"request_key":"a","input":source.model_dump(),"allow_synthetic":True}).status_code==422


def test_default_api_cannot_advance_or_publish_existing_demo_job(tmp_path,source):
    path=tmp_path / "jobs.sqlite"
    demo=TestClient(create_demo_app(path))
    job=demo.post("/jobs",json={"request_key":"a","input":source.model_dump()}).json()
    for _ in range(5):
        job=demo.post(f"/jobs/{job['job_id']}/advance",json={"expected_revision":job['revision']}).json()
    assert job["status"]=="ready"
    client=TestClient(create_app(path))
    base=f"/jobs/{job['job_id']}"
    assert client.post(base+"/advance",json={"expected_revision":job['revision']}).status_code==403
    assert client.post(base+"/resume",json={"expected_revision":job['revision']}).status_code==403
    assert client.get(base+"/publish-plan").status_code==403
