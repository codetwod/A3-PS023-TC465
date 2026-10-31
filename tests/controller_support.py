import json
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa

from controller.auth import password_hash


def auth_files(directory):
    directory = Path(directory)
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    heartbeat = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    private_path = directory / "controller.pem"
    private_path.write_bytes(key.private_bytes(serialization.Encoding.PEM,
        serialization.PrivateFormat.PKCS8, serialization.NoEncryption()))
    public_path = directory / "controller-public.pem"
    public_path.write_bytes(key.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    heartbeat_path = directory / "heartbeat-public.pem"
    heartbeat_path.write_bytes(heartbeat.public_key().public_bytes(serialization.Encoding.PEM,
        serialization.PublicFormat.SubjectPublicKeyInfo))
    users = directory / "users.json"
    users.write_text(json.dumps({name: {"password_hash": password_hash("test-password-123"), "role": role}
        for name, role in (("alice", "user"), ("bob", "user"), ("admin", "admin"))}))
    return {"private_key_path": private_path, "users_path": users,
            "heartbeat_public_key_path": heartbeat_path}, key, heartbeat, public_path
