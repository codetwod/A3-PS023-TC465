import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from fastapi.testclient import TestClient

from worker.auth import JWTVerifier
from worker.config import Settings
from worker.main import create_app


class JWTTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        cls.other_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        public_path = Path(self.temp.name) / "public.pem"
        public_path.write_bytes(self.key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo))
        self.settings = Settings(jwt_public_key_path=public_path, jwt_audience="worker-1")
        self.backend = SimpleNamespace(
            initialize=AsyncMock(), close=AsyncMock(), list_vms=AsyncMock(return_value=[]),
            resources=AsyncMock(return_value={"allocatable_memory_mib": 1024}),
            create_vm=AsyncMock(), start_vm=AsyncMock(), stop_vm=AsyncMock(),
        )
        self.app = create_app(self.settings, self.backend)
        self.client = self.enterContext(TestClient(self.app))

    def token(self, **changes):
        now = int(time.time())
        claims = {"iss": "vm-controller", "aud": "worker-1", "sub": "controller",
                  "iat": now - 1, "exp": now + 300}
        claims.update(changes)
        return jwt.encode(claims, self.key, algorithm="RS256")

    def headers(self, token=None):
        return {"Authorization": f"Bearer {token or self.token()}"}

    def test_valid_token_and_public_health(self):
        self.assertEqual(self.client.get("/health").status_code, 200)
        self.assertEqual(self.client.get("/vms", headers=self.headers()).json(), [])
        self.assertEqual(self.client.get("/resources", headers=self.headers()).status_code, 200)

    def test_all_operation_routes_require_token(self):
        for method, path in [("GET", "/vms"), ("POST", "/vms"),
                             ("POST", "/vms/demo/start"), ("POST", "/vms/demo/stop"),
                             ("GET", "/resources")]:
            with self.subTest(path=path, method=method):
                response = self.client.request(method, path)
                self.assertEqual(response.status_code, 401)
                self.assertEqual(response.headers["www-authenticate"], "Bearer")
        self.backend.create_vm.assert_not_awaited()
        self.backend.start_vm.assert_not_awaited()
        self.backend.stop_vm.assert_not_awaited()
        self.backend.list_vms.assert_not_awaited()
        self.backend.resources.assert_not_awaited()

    def test_bad_headers_and_malformed_tokens(self):
        for authorization in ("Basic abc", "Bearer", "Bearer nonsense", "Bearer a.b.c"):
            response = self.client.get("/vms", headers={"Authorization": authorization})
            self.assertEqual(response.status_code, 401)

    def test_invalid_claims(self):
        now = int(time.time())
        for claims in ({"exp": now - 10}, {"iss": "attacker"}, {"aud": "worker-2"},
                       {"sub": "other"}, {"nbf": now + 300}, {"iat": now + 300},
                       {"exp": "not-a-date"}, {"exp": True}, {"iat": "1"}):
            with self.subTest(claims=claims):
                self.assertEqual(self.client.get("/vms", headers=self.headers(
                    self.token(**claims))).status_code, 401)

    def test_required_claims(self):
        claims = jwt.decode(self.token(), self.key.public_key(), algorithms=["RS256"],
                            audience="worker-1")
        for name in ("exp", "iat", "iss", "aud", "sub"):
            incomplete = {key: value for key, value in claims.items() if key != name}
            token = jwt.encode(incomplete, self.key, algorithm="RS256")
            self.assertEqual(self.client.get("/vms", headers=self.headers(token)).status_code, 401)

    def test_wrong_signature_or_algorithm(self):
        claims = {"iss": "vm-controller", "aud": "worker-1", "sub": "controller",
                  "iat": int(time.time()), "exp": int(time.time()) + 300}
        for token in (jwt.encode(claims, self.other_key, algorithm="RS256"),
                      jwt.encode(claims, "x" * 32, algorithm="HS256"),
                      jwt.encode(claims, key=None, algorithm="none")):
            self.assertEqual(self.client.get("/vms", headers=self.headers(token)).status_code, 401)

    def test_missing_key_fails_startup(self):
        backend = SimpleNamespace(initialize=AsyncMock(), close=AsyncMock())
        with self.assertRaisesRegex(ValueError, "WORKER_JWT_PUBLIC_KEY_PATH"):
            with TestClient(create_app(Settings(), backend)):
                pass
        backend.initialize.assert_not_awaited()

    def test_invalid_key_rejected(self):
        self.settings.jwt_public_key_path.write_text("invalid")
        with self.assertRaises(ValueError):
            JWTVerifier(self.settings)