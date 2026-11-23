"""Helper of test/smoke_container.sh: a throwaway self-signed certificate for the reserved name router.example.net.

Runs inside the router image as root, read from stdin (`python - < smoke_container_cert.py`), and uses the image's own
cryptography package, so the host needs no openssl. Writes /etc/skirnir-router/certs/fullchain.pem and privkey.pem
(the paths router.py --init puts into config.yaml) and gives them to group 10001 as router-layout.sh --tls does on a
host: folder 0750, files 0640, owner root. Valid for two days; used only by the smoke test.
"""
import datetime
import os

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.x509.oid import NameOID

FOLDER = "/etc/skirnir-router/certs"
GROUP = 10001
NAME = "router.example.net"

key = ec.generate_private_key(ec.SECP256R1())
subject = x509.Name([x509.NameAttribute(NameOID.COMMON_NAME, NAME)])
now = datetime.datetime.now(datetime.timezone.utc)
cert = (x509.CertificateBuilder().subject_name(subject).issuer_name(subject).public_key(key.public_key())
        .serial_number(x509.random_serial_number()).not_valid_before(now - datetime.timedelta(minutes=5))
        .not_valid_after(now + datetime.timedelta(days=2))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(NAME)]), critical=False)
        .sign(key, hashes.SHA256()))
os.makedirs(FOLDER, exist_ok=True)
files = {
    "privkey.pem": key.private_bytes(serialization.Encoding.PEM, serialization.PrivateFormat.PKCS8,
                                     serialization.NoEncryption()),
    "fullchain.pem": cert.public_bytes(serialization.Encoding.PEM),
}
for name, data in files.items():
    path = os.path.join(FOLDER, name)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o640)
    with os.fdopen(fd, "wb") as f:
        f.write(data)
    os.chown(path, 0, GROUP)
    os.chmod(path, 0o640)
os.chown(FOLDER, 0, GROUP)
os.chmod(FOLDER, 0o750)
print(f"certificate for {NAME} written to {FOLDER}")
