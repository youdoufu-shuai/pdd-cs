import importlib.util
from pathlib import Path

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("check_publication", ROOT / "scripts/check-publication.py")
check = importlib.util.module_from_spec(spec)
spec.loader.exec_module(check)


@pytest.mark.parametrize("name", sorted(check.SAMPLES))
def test_example_config_has_no_notification_recipients(name):
    data = (ROOT / name).read_bytes()
    alerts = yaml.safe_load(data)["alerts"]
    assert alerts["email_to"] == []
    assert alerts["sms_phone"] == ""
    assert check.inspect(name, data) == []
    assert check.inspect(name, data.replace(b'  sms_phone: ""', b'  sms_phone: "private-number"'))
    assert check.inspect(name, data.replace(b"  email_to: []", b"  email_to: [sample@example.test]"))


def test_private_paths_and_credential_signatures():
    for name in ("config/config.yaml", "config/secrets/shop.json", "data/private.db",
                 ".env.production", "pdd-login-shop.json", "outputs/screenshot.png"):
        assert check.private_path(name)
    assert not check.private_path("deploy/.env.example")
    assert not check.private_path("config/secrets/.gitkeep")
    signature = b"ghp_" + b"A" * 36
    assert check.inspect("sample.txt", signature) == ["存在疑似凭据；请核对并移除后再发布"]
