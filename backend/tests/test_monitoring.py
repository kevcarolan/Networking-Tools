from netmiko.exceptions import NetmikoAuthenticationException


def test_prtg_endpoint_is_off_without_token(client):
    assert client.get("/api/monitoring/prtg").status_code == 404


def test_prtg_endpoint(app, client, admin, device, fetcher, settings):
    settings.monitoring_token = "s3cret-token"
    assert client.get("/api/monitoring/prtg").status_code == 401
    assert client.get("/api/monitoring/prtg",
                      headers={"Authorization": "Bearer wrong"}).status_code == 401

    def channels():
        r = client.get("/api/monitoring/prtg", headers={"Authorization": "Bearer s3cret-token"})
        assert r.status_code == 200, r.text
        return {c["channel"]: c for c in r.json()["prtg"]["result"]}

    ch = channels()
    assert ch["Devices never backed up"]["value"] == 1
    assert ch["Devices without a good backup in 48 h"]["value"] == 1
    assert ch["Hours since server backup"]["value"] == 9999.0
    assert 0 < ch["Data disk free"]["value"] <= 100

    fetcher.errors["core-sw1"] = NetmikoAuthenticationException("bad")
    app.state.backup_service.run(device["id"])
    assert channels()["Devices failing backup"]["value"] == 1

    del fetcher.errors["core-sw1"]
    app.state.backup_service.run(device["id"])
    (settings.data_dir / "last-backup").write_text("now")
    ch = channels()
    assert ch["Devices failing backup"]["value"] == 0
    assert ch["Devices without a good backup in 48 h"]["value"] == 0
    assert ch["Hours since server backup"]["value"] < 1
    # query-string token also accepted (for PRTG versions without custom headers)
    assert client.get("/api/monitoring/prtg?token=s3cret-token").status_code == 200
