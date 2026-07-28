"""
Genera un certificado autofirmado para correr el chat por HTTPS en la red local.

Uso:
    pip install cryptography
    python generar_certificado.py 192.168.1.50

Reemplazá 192.168.1.50 por TU IP local (la que ves con `ipconfig`).
Esto crea dos archivos: cert.pem y key.pem, en la misma carpeta.
"""

import sys
import datetime
import ipaddress

from cryptography import x509
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from cryptography.x509.oid import NameOID


def main():
    if len(sys.argv) < 2:
        print("Uso: python generar_certificado.py TU_IP_LOCAL")
        print("Ejemplo: python generar_certificado.py 192.168.1.50")
        sys.exit(1)

    ip_str = sys.argv[1]

    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)

    subject = issuer = x509.Name([
        x509.NameAttribute(NameOID.COMMON_NAME, ip_str),
    ])

    san_entries = [
        x509.IPAddress(ipaddress.ip_address(ip_str)),
        x509.DNSName("localhost"),
        x509.IPAddress(ipaddress.ip_address("127.0.0.1")),
    ]

    cert = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(issuer)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(datetime.datetime.now(datetime.timezone.utc))
        .not_valid_after(datetime.datetime.now(datetime.timezone.utc) + datetime.timedelta(days=3650))
        .add_extension(x509.SubjectAlternativeName(san_entries), critical=False)
        .sign(key, hashes.SHA256())
    )

    with open("key.pem", "wb") as f:
        f.write(key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.TraditionalOpenSSL,
            encryption_algorithm=serialization.NoEncryption(),
        ))

    with open("cert.pem", "wb") as f:
        f.write(cert.public_bytes(serialization.Encoding.PEM))

    print(f"Listo: se generaron cert.pem y key.pem, válidos para {ip_str} y localhost (10 años).")


if __name__ == "__main__":
    main()
