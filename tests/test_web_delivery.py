from meterloss.delivery import MODULES, application


def test_real_overview_returns_operational_evidence():
    response = application.test_client().get("/api/overview")
    assert response.status_code == 200
    model = response.get_json()
    assert len(model["run_id"]) == 12
    assert 4 <= len(model["metrics"]) <= 6
    assert len(model["stages"]) == len(MODULES)
    assert len(model["rows"]) >= 6
    assert all(model["columns"] for row in model["rows"] if row)
    assert len(model["series"]) >= 5


def test_scenario_result_and_stage_use_same_snapshot():
    client = application.test_client()
    response = client.post("/api/scenario", json={"capacity": 3})
    assert response.status_code == 200
    result = response.get_json()
    stage = client.get(f"/api/stages/{MODULES[-1][0]}?capacity=3").get_json()
    assert stage == {**result["stages"][-1], "run_id": result["run_id"]}


def test_malformed_capacity_and_unknown_stages_are_explicit():
    client = application.test_client()
    for data in ({"capacity": True}, {"capacity": 0}, {"capacity": 101}, {}, [1]):
        response = client.post("/api/scenario", json=data)
        assert response.status_code == 400
        assert response.get_json()["code"] == "INVALID_SCENARIO"
    assert client.get("/api/stages/unknown").get_json()["code"] == "UNKNOWN_STAGE"


def test_health_page_and_local_assets():
    client = application.test_client()
    assert client.get("/health").get_json()["service"] == "meterloss"
    body = client.get("/").get_data(as_text=True)
    assert "园区水量漏损排查" in body and "theme.css" in body
    assert client.get("/static/workbench.js").status_code == 200


def test_repeated_scenario_has_the_same_run_fingerprint():
    client = application.test_client()
    first = client.post("/api/scenario", json={"capacity": 5}).get_json()
    second = client.post("/api/scenario", json={"capacity": 5}).get_json()
    assert first == second


def test_large_request_returns_structured_error():
    response = application.test_client().post("/api/scenario", json={"capacity": "X" * 9000})
    assert response.status_code == 413
    assert response.get_json()["code"] == "REQUEST_TOO_LARGE"


def test_night_orders_endpoint_returns_one_order_per_zone():
    client = application.test_client()
    response = client.get("/api/night-orders")
    assert response.status_code == 200
    view = response.get_json()
    assert len(view["run_id"]) == 12
    sectors = {row["sector"] for row in view["records"]}
    assert sectors == {"南立管", "窑炉间", "中水站"}
    assert all(row["crew"] for row in view["records"])
    late = [row for row in view["errors"] if row["status"] == "late"]
    assert late and all(row["reason"] for row in late)
    # 与同参数快照保持同一指纹和同一批指令。
    scenario = client.post("/api/scenario", json={"capacity": 4}).get_json()
    scoped = client.get("/api/night-orders?capacity=4").get_json()
    assert scoped == {**scenario["night_orders"], "run_id": scenario["run_id"]}
    bad = client.get("/api/night-orders?capacity=0")
    assert bad.status_code == 400 and bad.get_json()["code"] == "INVALID_SCENARIO"
